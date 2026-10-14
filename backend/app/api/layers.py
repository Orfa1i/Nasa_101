"""Read the cached pipeline products (Zarr) for the API: layer matrices and point samples.

Products per facility live in ``DATA_DIR/cache/<prefix>_{spectral,kinematics,hydrology,coupled}.zarr``.

**Fallbacks.** Each layer lists its sources in priority order. A missing file
or variable moves on to the next source; so does a file that exists but cannot
be read (logged as a warning). If no source works, the result is
``AssetMissing`` (HTTP 404) when nothing exists, or ``AssetUnreadable``
(HTTP 503) when something exists but could not be read.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
import pandas as pd
import rioxarray  # noqa: F401  (registers the .rio accessor)
import xarray as xr
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from scipy import ndimage

from app.config import Settings

logger = logging.getLogger("lithocreep.api")

PRODUCT_FILES = {
    "spectral": "{prefix}_spectral.zarr",
    "kinematics": "{prefix}_kinematics.zarr",
    "hydrology": "{prefix}_hydrology.zarr",
    "coupled": "{prefix}_coupled.zarr",
}
GEO_RESOLUTION_DEG = 1.0 / 3600.0  # 1 arc-second, the GLO-30 posting (~30 m)
EXACT_MATCH_M = 0.5


class AssetMissing(LookupError):
    """The requested product or layer does not exist for this facility."""


class AssetUnreadable(RuntimeError):
    """Product files exist but none could be read."""


@dataclass(frozen=True)
class LayerSpec:
    units: str
    categorical: bool
    time_dependent: bool
    sources: tuple[tuple[str, str], ...]  # (product, variable), in priority order
    derived: Optional[str] = None
    circular: bool = False  # angles: never interpolate


LAYERS: dict[str, LayerSpec] = {
    "sai": LayerSpec("index", False, False, (("coupled", "sai"), ("spectral", "sai"))),
    "spectral_rmse": LayerSpec("reflectance", False, False, (("coupled", "spectral_rmse"), ("spectral", "rmse"))),
    "displacement": LayerSpec("mm", False, True, (("kinematics", "displacement_smooth"), ("kinematics", "displacement"))),
    "velocity": LayerSpec("mm/yr", False, True, (("kinematics", "velocity"),)),
    "acceleration": LayerSpec("mm/yr^2", False, True, (("kinematics", "acceleration"),)),
    "coherence": LayerSpec("unitless", False, False, (("kinematics", "temporal_coherence"), ("coupled", "temporal_coherence"))),
    "anomaly_regime": LayerSpec("regime code", True, False, (("coupled", "regime"),)),
    "alert_tier": LayerSpec("tier", True, False, (("coupled", "alert_tier"),)),
    "elevation": LayerSpec("m", False, False, (("hydrology", "elevation"), ("coupled", "elevation"))),
    "slope": LayerSpec("degree", False, False, (("hydrology", "slope_deg"),)),
    "aspect": LayerSpec("degree", False, False, (("hydrology", "aspect_deg"), ("coupled", "aspect_deg")), circular=True),
    "flow_accumulation": LayerSpec("cells", False, False, (("hydrology", "flow_accumulation_cells"),)),
    "stream_network": LayerSpec("boolean", True, False, (("hydrology", "stream_network"),)),
    "water_class": LayerSpec("DSWx WTR class", True, True, (("hydrology", "water_class"),)),
    "hydraulic_distance": LayerSpec("m", False, True, (("hydrology", "water_class"),), derived="distance_to_water"),
    "observability_mask": LayerSpec(
        "mask code",
        True,
        False,
        (("coupled", "classification_flags"), ("spectral", "observability"), ("kinematics", "kinematic_status")),
        derived="observability_mask",
    ),
}

# observability_mask codes; null = no data. Built from the coupled flags, else from whichever
# single product exists (then only that product's half of the mask is known).
MASK_OBSERVABLE, MASK_VEGETATED, MASK_DECORRELATED, MASK_BOTH = 0, 1, 2, 3
MASK_MEANINGS = "observable unobservable-vegetated unobservable-low-coherence vegetated-and-low-coherence"


@dataclass
class Source:
    product: str
    file: str
    variable: str
    synthetic_or_mock: bool
    label: str

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class ResolvedLayer:
    data: xr.DataArray
    source: Source
    note: Optional[str] = None
    notes: list[str] = field(default_factory=list)


def product_path(settings: Settings, prefix: str, product: str) -> Path:
    return settings.cache_dir / PRODUCT_FILES[product].format(prefix=prefix)


def open_product(settings: Settings, prefix: str, product: str) -> tuple[xr.Dataset, Path]:
    path = product_path(settings, prefix, product)
    if not path.exists():
        raise AssetMissing(f"{path.name} not found (run the `{_producer(product)}` command for this facility)")
    try:
        ds = xr.open_zarr(path, decode_coords="all")
    except Exception as exc:
        raise AssetUnreadable(f"{path.name} exists but cannot be read ({type(exc).__name__}: {exc})") from exc
    return ds, path


def _producer(product: str) -> str:
    return {"spectral": "unmix", "kinematics": "kinematics", "hydrology": "hydrology", "coupled": "couple"}[product]


def describe_source(product: str, path: Path, variable: str, ds: xr.Dataset) -> Source:
    attrs = ds.attrs
    if product == "spectral":
        synthetic = str(attrs.get("source_origin", "")).lower() == "mock"
        label = str(attrs.get("source_data_label") or ("MOCK INPUT" if synthetic else "NASA EMIT L2A"))
    elif product == "coupled":
        synthetic = bool(attrs.get("contains_synthetic_or_mock_inputs", 0))
        label = "Contains SYNTHETIC/MOCK inputs" if synthetic else "Derived from satellite observations"
    elif product == "kinematics":
        citation = str(attrs.get("source_citation", ""))
        synthetic = "synthetic" in citation.lower()
        src = str(attrs.get("source", ""))
        label = f"{src}; {citation}" if citation else (src or "OPERA DISP-S1")
    else:
        synthetic = False
        label = f"GLO-30 DEM; DSWx: {attrs.get('dswx_source', 'none')}"
    if int(attrs.get("synthetic_or_mock", 0) or 0):
        # Explicit stamp (e.g. scripts/build_demo_cache.py) wins over filename-based provenance.
        synthetic = True
        label = f"SYNTHETIC: {attrs.get('synthetic_note') or label}"
    return Source(product, path.name, variable, synthetic, label)


def resolve_layer(settings: Settings, prefix: str, layer: str) -> ResolvedLayer:
    """First readable source of ``layer`` (see module docstring for the fallback rules)."""
    spec = LAYERS[layer]
    missing, unreadable = [], []
    for product, variable in spec.sources:
        try:
            ds, path = open_product(settings, prefix, product)
            if variable not in ds:
                missing.append(f"{path.name} has no variable {variable!r}")
                continue
            da = ds[variable]
            if spec.time_dependent and "time" not in da.dims:
                missing.append(f"{path.name}:{variable} has no time axis")
                continue
            if not {"y", "x"} <= set(da.dims):
                missing.append(f"{path.name}:{variable} is not a raster (point backtest data)")
                continue
            crs = ds.rio.crs or da.rio.crs
            if crs is None:
                raise AssetUnreadable(f"{path.name} has no CRS")
            resolved = ResolvedLayer(da.rio.write_crs(crs), describe_source(product, path, variable, ds))
            if missing or unreadable:
                resolved.note = "fallback source used: " + "; ".join(missing + unreadable)
            return resolved
        except AssetMissing as exc:
            missing.append(str(exc))
        except AssetUnreadable as exc:
            logger.warning("Layer %s for %s: %s", layer, prefix, exc)
            unreadable.append(str(exc))
        except Exception as exc:  # corrupt chunks etc.
            logger.warning("Layer %s for %s: %s: %s", layer, prefix, type(exc).__name__, exc)
            unreadable.append(f"{product}: {type(exc).__name__}: {exc}")
    if unreadable:
        raise AssetUnreadable("; ".join(unreadable + missing))
    raise AssetMissing("; ".join(missing))


def layer_times(da: xr.DataArray) -> list[datetime]:
    return [pd.Timestamp(t).to_pydatetime() for t in da["time"].values] if "time" in da.dims else []


def select_time(da: xr.DataArray, when: Optional[datetime]) -> tuple[xr.DataArray, Optional[datetime]]:
    """Nearest epoch to ``when`` (latest epoch when ``when`` is None)."""
    if "time" not in da.dims:
        return da, None
    times = pd.to_datetime(da["time"].values)
    if when is None:
        idx = len(times) - 1
    else:
        target = pd.Timestamp(when)
        target = target.tz_convert(None) if target.tzinfo else target
        idx = int(np.argmin(np.abs((times - target).total_seconds())))
    return da.isel(time=idx), times[idx].to_pydatetime()


def distance_to_water(water_class: xr.DataArray) -> xr.DataArray:
    """Euclidean distance (m) to the nearest DSWx water cell (WTR 1/2). Not a flow-path distance."""
    from app.pipelines.hydrology import WATER_CLASSES

    water = np.isin(water_class.values, WATER_CLASSES)
    res = abs(float(water_class.rio.resolution()[0]))
    if water.any():
        dist = ndimage.distance_transform_edt(~water) * res
    else:
        dist = np.full(water.shape, np.nan)
    return water_class.copy(data=dist.astype(np.float64))


def observability_mask(da: xr.DataArray, variable: str) -> tuple[xr.DataArray, Optional[str]]:
    """Mask codes (see ``MASK_MEANINGS``) where spectra are vegetated (NDVI > 0.35) or radar is decorrelated."""
    from app.pipelines import coupling as cp
    from app.pipelines import kinematics as kin
    from app.pipelines import unmixing as um

    v = da.values
    finite = np.isfinite(v.astype(np.float64))
    flags = np.where(finite, v, 0).astype(np.int64)
    note = None
    if variable == "classification_flags":
        vegetated = (flags & cp.FLAG_VEGETATED) != 0
        decorrelated = (flags & cp.FLAG_LOW_COHERENCE) != 0
        no_data = (flags & (cp.FLAG_NO_SPECTRAL | cp.FLAG_NO_KINEMATICS)) == (cp.FLAG_NO_SPECTRAL | cp.FLAG_NO_KINEMATICS)
    elif variable == "observability":
        vegetated, decorrelated, no_data = flags == um.VEGETATED, np.zeros_like(finite), flags == um.NO_DATA
        note = "spectral product only: radar decorrelation unknown"
    elif variable == "kinematic_status":
        vegetated, decorrelated, no_data = np.zeros_like(finite), flags == kin.UNOBSERVABLE, flags == kin.NO_DATA
        note = "kinematic product only: vegetation unknown"
    else:
        raise ValueError(f"no observability mask for variable {variable!r}")
    codes = vegetated * MASK_VEGETATED + decorrelated * MASK_DECORRELATED
    out = np.where(finite & ~no_data, codes, np.nan)
    return da.copy(data=out.astype(np.float64)), note


def geographic_matrix(da: xr.DataArray, bbox: Sequence[float], *, categorical: bool, circular: bool = False) -> tuple[np.ndarray, list[float]]:
    """Reproject a 2-D UTM layer onto an EPSG:4326 grid whose outer bounds equal ``bbox`` exactly."""
    xmin, ymin, xmax, ymax = bbox
    nx = max(1, int(round((xmax - xmin) / GEO_RESOLUTION_DEG)))
    ny = max(1, int(round((ymax - ymin) / GEO_RESOLUTION_DEG)))
    transform = from_bounds(xmin, ymin, xmax, ymax, nx, ny)
    src = da.astype(np.float64).rio.write_nodata(np.nan, encoded=False)
    resampling = Resampling.nearest if (categorical or circular) else Resampling.bilinear
    out = src.rio.reproject("EPSG:4326", shape=(ny, nx), transform=transform, resampling=resampling, nodata=np.nan)
    return out.values, [(xmax - xmin) / nx, (ymax - ymin) / ny]


def json_grid(values: np.ndarray, decimals: int = 6) -> list[list[Optional[float]]]:
    rounded = np.round(values.astype(np.float64), decimals)
    return [[float(v) if math.isfinite(v) else None for v in row] for row in rounded]


def json_list(values: Any) -> list[Optional[float]]:
    arr = np.asarray(values, dtype=np.float64).ravel()
    return [float(v) if math.isfinite(v) else None for v in arr]


def json_scalar(value: Any) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def json_time(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    ts = pd.Timestamp(value)
    return None if pd.isna(ts) else ts.to_pydatetime()


# --------------------------------------------------------------------------- #
# Point sampling
# --------------------------------------------------------------------------- #


def to_utm(lon: float, lat: float, crs: Any) -> tuple[float, float]:
    return Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(lon, lat)


@dataclass
class PointLocator:
    x: float
    y: float
    xn: float
    yn: float
    distance_m: float

    @property
    def exact(self) -> bool:
        return self.distance_m < EXACT_MATCH_M


def locate(ds: xr.Dataset, x: float, y: float) -> Optional[PointLocator]:
    """Nearest cell centre, or None if (x, y) lies outside the grid."""
    xs, ys = ds["x"].values, ds["y"].values
    dx = abs(float(xs[1] - xs[0])) if xs.size > 1 else 30.0
    dy = abs(float(ys[1] - ys[0])) if ys.size > 1 else 30.0
    if not (xs.min() - dx / 2 <= x <= xs.max() + dx / 2 and ys.min() - dy / 2 <= y <= ys.max() + dy / 2):
        return None
    xn = float(xs[np.argmin(np.abs(xs - x))])
    yn = float(ys[np.argmin(np.abs(ys - y))])
    return PointLocator(x, y, xn, yn, math.hypot(x - xn, y - yn))


def sample(da: xr.DataArray, loc: PointLocator, *, interpolate: bool) -> tuple[np.ndarray, str]:
    """Bilinear (``interpolate``) or nearest sample; falls back to nearest where bilinear hits NaN."""
    nearest = da.sel(x=loc.xn, y=loc.yn).values
    if not interpolate or loc.exact:
        return np.asarray(nearest), "exact" if loc.exact else "nearest"
    lin = np.asarray(da.astype(np.float64).interp(x=loc.x, y=loc.y, method="linear").values)
    near = np.asarray(nearest, dtype=np.float64)
    if np.isnan(lin).any() and np.isfinite(near[np.isnan(lin)]).any():
        lin = np.where(np.isnan(lin), near, lin)
        return lin, "bilinear"  # partially nearest-filled at data edges; reported in the note
    return lin, "bilinear"


def haversine_m(lon1: float, lat1: float, lon2: np.ndarray, lat2: np.ndarray) -> np.ndarray:
    r = 6_371_000.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi, dlmb = p2 - p1, np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(a))
