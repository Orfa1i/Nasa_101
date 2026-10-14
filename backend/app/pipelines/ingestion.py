"""NASA Earthdata download, reprojection and clip pipeline.

``download_and_crop`` is the single entry point. For one product and one
bounding box it:

1. searches CMR through ``earthaccess`` for granules in the 2022-2026 window,
2. picks the highest-quality overpass among the granules touching the bbox,
3. downloads the granule's science asset to ``DATA_DIR/raw/<short_name>/``,
4. reprojects it to UTM (``EPSG:32612`` by default), clips it to the bbox and
   writes a compressed GeoTIFF or Zarr store plus an AOI GeoJSON and a
   ``.meta.json`` sidecar.

Fallbacks:

* ``OPERA_L3_DISP-S1_V1`` only exists over North America. A bbox outside CONUS
  never reaches CMR; instead ``DATA_DIR/cache`` is searched for a pre-processed
  historical backup covering the bbox. With no backup a ``CoverageError`` is
  raised.
* If CMR / the DAAC cannot be reached, a warning is logged and granule records
  are read from mock metadata files in ``DATA_DIR/mock/<dataset_id>/*.json``.
  Each file holds one record (or a list of records) shaped like a CMR UMM-G
  search hit, plus an optional ``local_asset`` path (relative to the JSON file)
  to a raster that stands in for the download::

      {"umm": {"GranuleUR": "...", "CloudCover": 3.0,
               "TemporalExtent": {"RangeDateTime": {"BeginningDateTime": "2024-05-01T18:00:00Z"}},
               "SpatialExtent": {"HorizontalSpatialDomain": {"Geometry": {"BoundingRectangles": [
                   {"WestBoundingCoordinate": -111.2, "EastBoundingCoordinate": -111.0,
                    "SouthBoundingCoordinate": 31.8, "NorthBoundingCoordinate": 32.0}]}}}},
       "local_asset": "sierrita_dswx_wtr.tif"}

  Everything produced from mock inputs is tagged ``origin="mock"`` in the
  raster attributes and the sidecar so it is never mistaken for an observation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import geopandas as gpd
import numpy as np
import pyogrio
import requests
import rioxarray  # noqa: F401  (registers the .rio accessor)
import xarray as xr
from rasterio.enums import Resampling
from rasterio.warp import transform_bounds
from shapely.geometry import Polygon, box, shape

from app.config import Settings, get_settings

logger = logging.getLogger("lithocreep.ingestion")

TARGET_YEARS = (2022, 2026)
# earthaccess appends the "Z" itself, so the bounds are given without a UTC suffix.
DEFAULT_TEMPORAL = (f"{TARGET_YEARS[0]}-01-01T00:00:00", f"{TARGET_YEARS[1]}-12-31T23:59:59")
TARGET_CRS = "EPSG:32612"  # WGS 84 / UTM zone 12N (Arizona targets)
UTM_12N_LON_RANGE = (-114.0, -108.0)

# Approximate CONUS envelope; OPERA DISP-S1 has no coverage outside North America.
CONUS_BBOX = (-125.0, 24.0, -66.5, 49.5)

# Margin added around the bbox before the first (source-CRS) clip so bilinear
# resampling has valid neighbours at the AOI edge.
CLIP_PAD_DEG = 0.01

MAX_GRANULES = 2000

# Exceptions treated as "Earthdata unreachable" -> mock metadata fallback.
OFFLINE_ERRORS: tuple[type[BaseException], ...] = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    ConnectionError,
    TimeoutError,
)


class IngestionError(RuntimeError):
    """Base class for ingestion failures."""


class CoverageError(IngestionError):
    """The product has no data for the requested bbox."""


class EarthdataAuthError(IngestionError):
    """Earthdata Login failed or no credentials are configured."""


@dataclass(frozen=True)
class ProductSpec:
    product_id: str
    short_name: str
    version: Optional[str]
    title: str
    daac: str
    resolution_m: int
    asset_pattern: str  # regex selecting the science file among a granule's links
    variable: Optional[str]  # NetCDF variable to read; None for GeoTIFF assets
    resampling: Resampling
    optical: bool  # cloud cover is a quality criterion
    conus_only: bool = False
    categorical: bool = False
    nodata: Optional[float] = None  # nodata value written for categorical outputs


PRODUCTS: dict[str, ProductSpec] = {
    spec.product_id: spec
    for spec in (
        ProductSpec(
            product_id="EMITL2ARFL.001",
            short_name="EMITL2ARFL",
            version="001",
            title="EMIT L2A Estimated Surface Reflectance 60 m",
            daac="LP DAAC",
            resolution_m=60,
            asset_pattern=r"EMIT_L2A_RFL_[^/]*\.nc$",
            variable="reflectance",
            resampling=Resampling.bilinear,
            optical=True,
        ),
        ProductSpec(
            product_id="OPERA_L3_DISP-S1_V1",
            short_name="OPERA_L3_DISP-S1_V1",
            version=None,
            title="OPERA Sentinel-1 Surface Displacement 30 m",
            daac="ASF DAAC",
            resolution_m=30,
            asset_pattern=r"\.nc$",
            variable="displacement",
            resampling=Resampling.bilinear,
            optical=False,
            conus_only=True,
        ),
        ProductSpec(
            product_id="OPERA_L3_DSWX-HLS_V1.0",
            short_name="OPERA_L3_DSWX-HLS_V1",
            version="1.0",
            title="OPERA HLS Dynamic Surface Water Extent 30 m (B01_WTR)",
            daac="LP DAAC",
            resolution_m=30,
            asset_pattern=r"_B01_WTR\.tif$",
            variable=None,
            resampling=Resampling.nearest,
            optical=True,
            categorical=True,
            nodata=255,
        ),
    )
}


@dataclass
class IngestionResult:
    dataset_id: str
    status: str  # "downloaded" | "mock" | "cache_fallback" | "metadata_only" | "no_granules"
    origin: str  # "earthdata" | "mock" | "historical_backup" | "none"
    bbox: tuple[float, float, float, float]
    granule_id: Optional[str] = None
    raw_paths: list[str] = field(default_factory=list)
    output_path: Optional[str] = None
    aoi_path: Optional[str] = None
    crs: Optional[str] = None
    bounds: Optional[tuple[float, float, float, float]] = None
    shape: Optional[tuple[int, ...]] = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #


def get_product(dataset_id: str) -> ProductSpec:
    try:
        return PRODUCTS[dataset_id]
    except KeyError:
        raise ValueError(f"Unknown dataset_id {dataset_id!r}; expected one of {sorted(PRODUCTS)}") from None


def validate_bbox(bbox: Sequence[float]) -> tuple[float, float, float, float]:
    """Return ``bbox`` as a float 4-tuple ``(xmin, ymin, xmax, ymax)`` in EPSG:4326."""
    if len(bbox) != 4:
        raise ValueError(f"bbox must have 4 values [xmin, ymin, xmax, ymax], got {len(bbox)}")
    xmin, ymin, xmax, ymax = (float(v) for v in bbox)
    if not all(math.isfinite(v) for v in (xmin, ymin, xmax, ymax)):
        raise ValueError("bbox values must be finite numbers")
    if not (-180.0 <= xmin < xmax <= 180.0):
        raise ValueError(f"bbox longitudes must satisfy -180 <= xmin < xmax <= 180, got {xmin}, {xmax}")
    if not (-90.0 <= ymin < ymax <= 90.0):
        raise ValueError(f"bbox latitudes must satisfy -90 <= ymin < ymax <= 90, got {ymin}, {ymax}")
    return xmin, ymin, xmax, ymax


def bbox_within_conus(bbox: Sequence[float]) -> bool:
    xmin, ymin, xmax, ymax = bbox
    cxmin, cymin, cxmax, cymax = CONUS_BBOX
    return cxmin <= xmin and xmax <= cxmax and cymin <= ymin and ymax <= cymax


# --------------------------------------------------------------------------- #
# Granule metadata (works for earthaccess.DataGranule and plain UMM dicts)
# --------------------------------------------------------------------------- #


def _umm(granule: Mapping[str, Any]) -> Mapping[str, Any]:
    return granule.get("umm", {}) or {}


def granule_id(granule: Mapping[str, Any]) -> str:
    umm = _umm(granule)
    return str(umm.get("GranuleUR") or granule.get("meta", {}).get("native-id") or "unknown-granule")


def granule_footprint(granule: Mapping[str, Any]):
    """Return the granule footprint as a shapely geometry in EPSG:4326, or ``None``."""
    geometry = (
        _umm(granule).get("SpatialExtent", {}).get("HorizontalSpatialDomain", {}).get("Geometry", {})
    )
    shapes = []
    for rect in geometry.get("BoundingRectangles", []) or []:
        shapes.append(
            box(
                rect["WestBoundingCoordinate"],
                rect["SouthBoundingCoordinate"],
                rect["EastBoundingCoordinate"],
                rect["NorthBoundingCoordinate"],
            )
        )
    for gpoly in geometry.get("GPolygons", []) or []:
        points = gpoly.get("Boundary", {}).get("Points", [])
        if len(points) >= 3:
            shapes.append(Polygon([(p["Longitude"], p["Latitude"]) for p in points]).buffer(0))
    if not shapes:
        return None
    footprint = shapes[0]
    for extra in shapes[1:]:
        footprint = footprint.union(extra)
    return footprint


def granule_cloud_cover(granule: Mapping[str, Any]) -> Optional[float]:
    umm = _umm(granule)
    if umm.get("CloudCover") is not None:
        return float(umm["CloudCover"])
    for attr in umm.get("AdditionalAttributes", []) or []:
        if re.search(r"cloud.?cover", str(attr.get("Name", "")), re.IGNORECASE):
            try:
                return float(attr["Values"][0])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
    return None


def granule_datetime(granule: Mapping[str, Any]) -> Optional[datetime]:
    temporal = _umm(granule).get("TemporalExtent", {})
    stamp = temporal.get("RangeDateTime", {}).get("BeginningDateTime") or temporal.get("SingleDateTime")
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None


def bbox_coverage(granule: Mapping[str, Any], bbox: Sequence[float]) -> Optional[float]:
    """Fraction of the bbox area covered by the granule footprint (``None`` if unknown)."""
    footprint = granule_footprint(granule)
    if footprint is None:
        return None
    aoi = box(*bbox)
    return float(footprint.intersection(aoi).area / aoi.area)


def select_best_granule(
    granules: Iterable[Mapping[str, Any]], bbox: Sequence[float], *, optical: bool
) -> Optional[Mapping[str, Any]]:
    """Pick the highest-quality overpass among granules touching ``bbox``.

    Ranking, most important first: full bbox coverage, lowest cloud cover
    (optical products only; unknown cloud cover ranks last), largest bbox
    coverage, most recent acquisition. Granules with a known footprint that
    misses the bbox are dropped.
    """
    epoch = datetime.min.replace(tzinfo=timezone.utc)

    def rank(granule: Mapping[str, Any]) -> tuple:
        coverage = bbox_coverage(granule, bbox)
        cloud = granule_cloud_cover(granule) if optical else 0.0
        acquired = granule_datetime(granule) or epoch
        if acquired.tzinfo is None:
            acquired = acquired.replace(tzinfo=timezone.utc)
        return (
            coverage is not None and coverage >= 0.999,
            -(cloud if cloud is not None else 101.0),
            coverage if coverage is not None else 0.0,
            acquired,
        )

    candidates = [g for g in granules if (bbox_coverage(g, bbox) or 0.0) > 0.0 or granule_footprint(g) is None]
    if not candidates:
        return None
    return max(candidates, key=rank)


def granule_links(granule: Mapping[str, Any]) -> list[str]:
    if hasattr(granule, "data_links"):
        return list(granule.data_links())
    return [
        url["URL"]
        for url in _umm(granule).get("RelatedUrls", []) or []
        if url.get("Type") == "GET DATA" and url.get("URL")
    ]


def select_asset_links(granule: Mapping[str, Any], spec: ProductSpec) -> list[str]:
    links = [link for link in granule_links(granule) if re.search(spec.asset_pattern, link)]
    if not links:
        raise IngestionError(f"Granule {granule_id(granule)} has no asset matching {spec.asset_pattern!r}")
    return links


# --------------------------------------------------------------------------- #
# Earthdata access
# --------------------------------------------------------------------------- #

_login_lock = threading.Lock()
_logged_in = False


def search_granules(
    spec: ProductSpec, bbox: Sequence[float], temporal: tuple[str, str], count: int = MAX_GRANULES
) -> list[Any]:
    import earthaccess

    query: dict[str, Any] = {
        "short_name": spec.short_name,
        "bounding_box": tuple(bbox),
        "temporal": temporal,
        "count": count,
    }
    if spec.version:
        query["version"] = spec.version
    logger.info("Searching CMR for %s bbox=%s temporal=%s", spec.product_id, bbox, temporal)
    granules = earthaccess.search_data(**query)
    logger.info("CMR returned %d granule(s) for %s", len(granules), spec.product_id)
    return granules


def ensure_earthdata_login(settings: Settings) -> None:
    global _logged_in
    import earthaccess

    with _login_lock:
        if _logged_in:
            return
        if settings.has_earthdata_credentials:
            # earthaccess' "environment" strategy reads these variables directly.
            os.environ.setdefault("EARTHDATA_USERNAME", settings.EARTHDATA_USERNAME or "")
            os.environ.setdefault("EARTHDATA_PASSWORD", settings.EARTHDATA_PASSWORD or "")
            strategy = "environment"
        else:
            strategy = "netrc"
        auth = earthaccess.login(strategy=strategy)
        if not getattr(auth, "authenticated", False):
            raise EarthdataAuthError(
                "Earthdata Login failed; set EARTHDATA_USERNAME/EARTHDATA_PASSWORD or configure ~/.netrc"
            )
        _logged_in = True


def download_assets(links: Sequence[str], raw_dir: Path, settings: Settings) -> list[Path]:
    import earthaccess

    ensure_earthdata_login(settings)
    raw_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading %d file(s) to %s", len(links), raw_dir)
    paths = earthaccess.download(list(links), local_path=str(raw_dir))
    if not paths:
        raise IngestionError(f"earthaccess downloaded no files for {list(links)}")
    return [Path(p) for p in paths]


def download_disp_stack(
    bbox: Sequence[float],
    settings: Settings,
    temporal: tuple[str, str] = DEFAULT_TEMPORAL,
    max_granules: int = MAX_GRANULES,
) -> list[Path]:
    """Download every DISP-S1 granule of the frame that best covers ``bbox`` (for the kinematics stack).

    Raises ``CoverageError`` outside CONUS without querying CMR. Earthdata
    connection errors propagate so the caller can fall back to local files.
    """
    spec = PRODUCTS["OPERA_L3_DISP-S1_V1"]
    bbox = validate_bbox(bbox)
    if not bbox_within_conus(bbox):
        raise CoverageError(f"{spec.product_id} has no coverage outside CONUS/North America for bbox {bbox}")
    granules = search_granules(spec, bbox, temporal, count=max_granules)
    frames: dict[str, list[Any]] = {}
    for granule in granules:
        match = re.search(r"_F(\d{5})_", granule_id(granule))
        if match:
            frames.setdefault(match.group(1), []).append(granule)
    if not frames:
        return []
    # Best frame: largest bbox coverage, then the longest stack.
    frame, members = max(
        frames.items(), key=lambda kv: (max(bbox_coverage(g, bbox) or 0.0 for g in kv[1]), len(kv[1]))
    )
    links = [select_asset_links(g, spec)[0] for g in members]
    logger.info("Downloading %d DISP-S1 granules of frame F%s", len(links), frame)
    return download_assets(links, settings.raw_dir / spec.short_name, settings)


# --------------------------------------------------------------------------- #
# Offline / cache fallbacks
# --------------------------------------------------------------------------- #


def load_mock_granules(spec: ProductSpec, mock_dir: Path) -> list[dict[str, Any]]:
    """Read mock granule records from ``mock_dir/<dataset_id>/*.json``."""
    folder = mock_dir / spec.product_id
    records: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.json")):
        payload = json.loads(path.read_text())
        for record in payload if isinstance(payload, list) else [payload]:
            record = dict(record)
            asset = record.get("local_asset")
            if asset:
                asset_path = Path(asset)
                record["local_asset"] = str(asset_path if asset_path.is_absolute() else (path.parent / asset_path))
            records.append(record)
    logger.info("Loaded %d mock granule record(s) from %s", len(records), folder)
    return records


def _raster_bounds_4326(path: Path) -> Optional[tuple[float, float, float, float]]:
    try:
        if path.suffix == ".zarr":
            with xr.open_zarr(path) as ds:
                return tuple(ds.rio.transform_bounds("EPSG:4326"))
        with rioxarray.open_rasterio(path) as da:
            return tuple(da.rio.transform_bounds("EPSG:4326"))
    except Exception:  # unreadable or CRS-less file: not a usable backup
        return None


def find_cached_backup(spec: ProductSpec, bbox: Sequence[float], cache_dir: Path) -> Optional[Path]:
    """Find a pre-processed backup in ``cache_dir`` that intersects ``bbox``.

    A ``<name>.meta.json`` sidecar with ``dataset_id`` and ``bbox`` keys is
    authoritative (this is how non-raster backups such as CSV profiles are
    registered). Rasters without a sidecar are matched on their bounds.
    """
    aoi = box(*bbox)
    for sidecar in sorted(cache_dir.rglob("*.meta.json")):
        try:
            meta = json.loads(sidecar.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("dataset_id") != spec.product_id or "bbox" not in meta:
            continue
        target = sidecar.with_name(sidecar.name[: -len(".meta.json")])
        if target.exists() and box(*meta["bbox"]).intersects(aoi):
            return target
    for candidate in sorted(cache_dir.rglob("*")):
        if candidate.suffix.lower() not in {".tif", ".tiff", ".zarr"}:
            continue
        if Path(f"{candidate}.meta.json").exists():
            continue  # already judged by its sidecar above
        if spec.short_name.lower() not in candidate.name.lower() and spec.product_id.lower() not in candidate.name.lower():
            continue
        bounds = _raster_bounds_4326(candidate)
        if bounds and box(*bounds).intersects(aoi):
            return candidate
    return None


# --------------------------------------------------------------------------- #
# Raster loading, reprojection and clipping
# --------------------------------------------------------------------------- #


def _padded(bbox: Sequence[float], pad: float) -> tuple[float, float, float, float]:
    xmin, ymin, xmax, ymax = bbox
    return xmin - pad, ymin - pad, xmax + pad, ymax + pad


def orthorectify_emit(
    path: Path, bbox: Sequence[float], pad: float = CLIP_PAD_DEG, variable: str = "reflectance"
) -> xr.DataArray:
    """Orthorectify the part of an EMIT swath variable that falls inside ``bbox``.

    EMIT NetCDFs store data in sensor geometry with a geometry lookup table
    (``location/glt_x``, ``location/glt_y``; 1-based, 0 = no data) that maps
    each EPSG:4326 output pixel to a swath pixel. Only the GLT window that
    covers the padded bbox is resolved, so the full scene is never loaded.

    ``variable`` may be 3-D ``(downtrack, crosstrack, bands)`` like L2A
    ``reflectance`` (returned as ``(band, y, x)`` with a ``wavelength``
    coordinate) or 2-D ``(downtrack, crosstrack)`` like the L2B MIN
    ``group_*_band_depth`` / ``group_*_mineral_id`` layers (returned as
    ``(y, x)``). Fill values (<= -9999) become NaN.
    """
    xmin, ymin, xmax, ymax = _padded(bbox, pad)
    with xr.open_dataset(path, engine="h5netcdf") as root, xr.open_dataset(
        path, engine="h5netcdf", group="location"
    ) as loc:
        x0, dx, _, y0, _, dy = (float(v) for v in root.attrs["geotransform"])
        n_rows, n_cols = loc["glt_x"].shape

        # The epsilon keeps bbox edges that fall exactly on pixel edges from pulling in a neighbour.
        eps = 1e-6
        col0 = max(int(math.floor((xmin - x0) / dx + eps)), 0)
        col1 = min(int(math.ceil((xmax - x0) / dx - eps)), n_cols)
        row0 = max(int(math.floor((ymax - y0) / dy + eps)), 0)
        row1 = min(int(math.ceil((ymin - y0) / dy - eps)), n_rows)
        if col0 >= col1 or row0 >= row1:
            raise CoverageError(f"EMIT scene {path.name} does not overlap bbox {tuple(bbox)}")

        glt_x = loc["glt_x"].values[row0:row1, col0:col1].astype(np.int64)
        glt_y = loc["glt_y"].values[row0:row1, col0:col1].astype(np.int64)
        valid = (glt_x > 0) & (glt_y > 0)
        if not valid.any():
            raise CoverageError(f"EMIT scene {path.name} has no valid pixels inside bbox {tuple(bbox)}")

        src_r0, src_r1 = int(glt_y[valid].min()) - 1, int(glt_y[valid].max())
        src_c0, src_c1 = int(glt_x[valid].min()) - 1, int(glt_x[valid].max())
        if variable not in root:
            raise IngestionError(f"{path.name} has no variable {variable!r}")
        swath = root[variable][src_r0:src_r1, src_c0:src_c1, ...].values.astype(np.float32)
        swath[swath <= -9999] = np.nan

    x = x0 + (np.arange(col0, col1) + 0.5) * dx
    y = y0 + (np.arange(row0, row1) + 0.5) * dy
    picked = swath[glt_y[valid] - 1 - src_r0, glt_x[valid] - 1 - src_c0, ...]

    if swath.ndim == 2:
        ortho = np.full((row1 - row0, col1 - col0), np.nan, dtype=np.float32)
        ortho[valid] = picked
        da = xr.DataArray(ortho, dims=("y", "x"), coords={"x": x, "y": y}, name=variable)
    else:
        with xr.open_dataset(path, engine="h5netcdf", group="sensor_band_parameters") as bands:
            wavelengths = bands["wavelengths"].values.astype(np.float32)
        n_bands = swath.shape[-1]
        ortho = np.full((n_bands, row1 - row0, col1 - col0), np.nan, dtype=np.float32)
        ortho[:, valid] = picked.T
        da = xr.DataArray(
            ortho,
            dims=("band", "y", "x"),
            coords={"band": np.arange(1, n_bands + 1), "wavelength": ("band", wavelengths), "x": x, "y": y},
            name=variable,
        )
    return da.rio.write_crs("EPSG:4326").rio.write_nodata(np.nan, encoded=False)


def load_granule_array(spec: ProductSpec, path: Path, bbox: Sequence[float]) -> xr.DataArray:
    """Open the science layer of a downloaded granule as a CRS-aware DataArray."""
    suffix = path.suffix.lower()
    if spec.short_name == "EMITL2ARFL" and suffix == ".nc":
        return orthorectify_emit(path, bbox)
    if suffix in {".tif", ".tiff"}:
        da = rioxarray.open_rasterio(path, masked=True)
    elif suffix in {".nc", ".h5"}:
        ds = xr.open_dataset(path, engine="h5netcdf", decode_coords="all")
        if spec.variable not in ds:
            raise IngestionError(f"{path.name} has no variable {spec.variable!r}")
        da = ds[spec.variable]
        if da.rio.crs is None and "spatial_ref" in ds:
            wkt = ds["spatial_ref"].attrs.get("crs_wkt") or ds["spatial_ref"].attrs.get("spatial_ref")
            if wkt:
                da = da.rio.write_crs(wkt)
        da = da.squeeze(drop=True)
    else:
        raise IngestionError(f"Unsupported granule format: {path.name}")
    if da.rio.crs is None:
        raise IngestionError(f"{path.name} carries no CRS; cannot reproject")
    return da


def aoi_geodataframe(bbox: Sequence[float], crs: str) -> gpd.GeoDataFrame:
    """The bbox as a polygon, transformed to ``crs`` (a rotated quadrilateral in UTM)."""
    aoi = gpd.GeoDataFrame({"name": ["aoi"]}, geometry=[box(*bbox)], crs="EPSG:4326")
    return aoi.to_crs(crs)


def reproject_and_clip(
    da: xr.DataArray, bbox: Sequence[float], target_crs: str, resampling: Resampling
) -> xr.DataArray:
    """Coarse-clip in the source CRS, reproject to ``target_crs``, then clip to the exact AOI."""
    from rioxarray.exceptions import NoDataInBounds, OneDimensionalRaster

    try:
        da = da.rio.clip_box(*_padded(bbox, CLIP_PAD_DEG), crs="EPSG:4326")
    except (NoDataInBounds, OneDimensionalRaster) as exc:
        raise CoverageError(f"Raster does not overlap bbox {tuple(bbox)}: {exc}") from exc

    if np.issubdtype(da.dtype, np.floating) and da.rio.nodata is None:
        da = da.rio.write_nodata(np.nan, encoded=False)
    da = da.rio.reproject(target_crs, resampling=resampling)

    aoi = aoi_geodataframe(bbox, target_crs)
    try:
        return da.rio.clip(aoi.geometry.values, crs=target_crs, all_touched=True, drop=True)
    except NoDataInBounds as exc:
        raise CoverageError(f"No valid pixels inside bbox {tuple(bbox)} after reprojection") from exc


def _prepare_for_write(da: xr.DataArray, spec: ProductSpec, attrs: Mapping[str, Any]) -> xr.DataArray:
    if spec.categorical:
        fill = spec.nodata if spec.nodata is not None else 255
        data = da.where(da.notnull(), fill).astype(np.uint8)
        da = data.rio.write_nodata(fill, encoded=False)
    else:
        da = da.astype(np.float32).rio.write_nodata(np.nan, encoded=False)
    da.attrs = {k: v for k, v in da.attrs.items() if k in {"_FillValue", "grid_mapping"}}
    da.attrs.update({k: (json.dumps(v) if isinstance(v, (list, tuple, dict)) else v) for k, v in attrs.items()})
    da.encoding = {}
    da.name = spec.variable or "value"
    return da


def write_output(da: xr.DataArray, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = output_path.suffix.lower()
    if suffix in {".tif", ".tiff"}:
        predictor = 3 if np.issubdtype(da.dtype, np.floating) else 2
        da.rio.to_raster(output_path, compress="DEFLATE", predictor=predictor, tiled=True, blockxsize=256, blockysize=256)
    elif suffix == ".zarr":
        import zarr

        ds = da.to_dataset()
        compressor = zarr.Blosc(cname="zstd", clevel=5, shuffle=zarr.Blosc.BITSHUFFLE)
        ds.to_zarr(output_path, mode="w", consolidated=True, encoding={da.name: {"compressor": compressor}})
    else:
        raise ValueError(f"output_path must end in .tif, .tiff or .zarr, got {output_path.name}")


def write_aoi(bbox: Sequence[float], crs: str, path: Path) -> None:
    aoi = aoi_geodataframe(bbox, crs)
    pyogrio.write_dataframe(aoi.to_crs("EPSG:4326"), path, driver="GeoJSON")


def crop_granule(
    spec: ProductSpec,
    raw_path: Path,
    bbox: Sequence[float],
    output_path: Path,
    target_crs: str,
    attrs: Mapping[str, Any],
) -> tuple[str, tuple[float, float, float, float], tuple[int, ...], Path]:
    da = load_granule_array(spec, raw_path, bbox)
    try:
        clipped = reproject_and_clip(da, bbox, target_crs, spec.resampling)
        clipped = _prepare_for_write(clipped, spec, attrs)
        write_output(clipped, output_path)
    finally:
        da.close()
    aoi_path = output_path.with_name(f"{output_path.stem}_aoi.geojson")
    write_aoi(bbox, target_crs, aoi_path)
    return str(clipped.rio.crs), tuple(float(v) for v in clipped.rio.bounds()), tuple(clipped.shape), aoi_path


def _write_sidecar(result: IngestionResult, output_path: Path, extra: Mapping[str, Any]) -> None:
    sidecar = Path(f"{output_path}.meta.json")
    sidecar.write_text(json.dumps({**result.to_dict(), **extra}, indent=2, default=str))


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


async def download_and_crop(
    dataset_id: str,
    bbox: Sequence[float],
    output_path: str | os.PathLike[str],
    *,
    settings: Optional[Settings] = None,
    temporal: tuple[str, str] = DEFAULT_TEMPORAL,
    target_crs: str = TARGET_CRS,
) -> IngestionResult:
    """Download the best granule of ``dataset_id`` over ``bbox`` and write a clipped UTM raster.

    ``output_path`` must end in ``.tif``/``.tiff`` (DEFLATE GeoTIFF) or ``.zarr``
    (Blosc/zstd Zarr). See the module docstring for the fallback behaviour.
    """
    spec = get_product(dataset_id)
    bbox = validate_bbox(bbox)
    settings = settings or get_settings()
    output_path = Path(output_path)
    result = IngestionResult(dataset_id=spec.product_id, status="no_granules", origin="none", bbox=bbox)

    center_lon = (bbox[0] + bbox[2]) / 2
    if target_crs == TARGET_CRS and not (UTM_12N_LON_RANGE[0] <= center_lon <= UTM_12N_LON_RANGE[1]):
        msg = f"bbox centre lon {center_lon:.3f} lies outside UTM zone 12N; {TARGET_CRS} output will be distorted"
        logger.warning(msg)
        result.warnings.append(msg)

    # 1. DISP-S1 is North-America only: never query CMR for other regions.
    if spec.conus_only and not bbox_within_conus(bbox):
        backup = await asyncio.to_thread(find_cached_backup, spec, bbox, settings.cache_dir)
        if backup is None:
            raise CoverageError(
                f"{spec.product_id} has no coverage outside CONUS/North America for bbox {bbox}, "
                f"and no pre-processed historical backup was found in {settings.cache_dir}"
            )
        msg = f"{spec.product_id} unavailable outside CONUS; using historical backup {backup}"
        logger.warning(msg)
        result.warnings.append(msg)
        result.status, result.origin, result.output_path = "cache_fallback", "historical_backup", str(backup)
        return result

    # 2. Search CMR, falling back to mock metadata when Earthdata is unreachable.
    origin = "earthdata"
    try:
        granules = await asyncio.to_thread(search_granules, spec, bbox, temporal)
    except OFFLINE_ERRORS as exc:
        msg = f"Earthdata unreachable during search for {spec.product_id} ({type(exc).__name__}); using mock metadata"
        logger.warning(msg)
        result.warnings.append(msg)
        granules, origin = await asyncio.to_thread(load_mock_granules, spec, settings.mock_dir), "mock"

    best = select_best_granule(granules, bbox, optical=spec.optical)
    if best is None:
        logger.warning("No %s granules (%s) intersect bbox %s", spec.product_id, origin, bbox)
        result.origin = origin
        return result
    result.granule_id = granule_id(best)
    logger.info(
        "Selected %s granule %s (cloud=%s, coverage=%s)",
        origin,
        result.granule_id,
        granule_cloud_cover(best),
        bbox_coverage(best, bbox),
    )

    # 3. Fetch the science asset to local storage.
    raw_paths: list[Path] = []
    if origin == "earthdata":
        links = select_asset_links(best, spec)
        try:
            raw_paths = await asyncio.to_thread(download_assets, links, settings.raw_dir / spec.short_name, settings)
        except OFFLINE_ERRORS as exc:
            msg = f"Earthdata unreachable during download of {result.granule_id} ({type(exc).__name__}); using mock metadata"
            logger.warning(msg)
            result.warnings.append(msg)
            origin = "mock"
            best = select_best_granule(
                await asyncio.to_thread(load_mock_granules, spec, settings.mock_dir), bbox, optical=spec.optical
            )
            result.granule_id = granule_id(best) if best is not None else None
    if origin == "mock" and best is not None:
        asset = best.get("local_asset")
        if asset and Path(asset).exists():
            raw_paths = [Path(asset)]
    result.origin = origin
    result.raw_paths = [str(p) for p in raw_paths]

    if not raw_paths:
        result.status = "metadata_only" if best is not None else "no_granules"
        logger.warning("No raster asset available for %s (%s); nothing to crop", spec.product_id, origin)
        return result

    # 4. Reproject, clip and write.
    attrs = {
        "dataset_id": spec.product_id,
        "granule_id": result.granule_id,
        "origin": origin,
        "data_label": "MOCK INPUT - not an authentic observation" if origin == "mock" else "NASA Earthdata observation",
        "bbox_epsg4326": list(bbox),
        "source_file": raw_paths[0].name,
    }
    crs, bounds, out_shape, aoi_path = await asyncio.to_thread(
        crop_granule, spec, raw_paths[0], bbox, output_path, target_crs, attrs
    )
    result.status = "downloaded" if origin == "earthdata" else "mock"
    result.output_path, result.aoi_path = str(output_path), str(aoi_path)
    result.crs, result.bounds, result.shape = crs, bounds, out_shape
    _write_sidecar(result, output_path, {"created_utc": datetime.now(timezone.utc).isoformat()})
    logger.info("Wrote %s %s -> %s (shape=%s)", spec.product_id, origin, output_path, out_shape)
    return result
