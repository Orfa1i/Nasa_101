"""Hydrology: DSWx-HLS water boundaries, pond-to-crest setback, GLO-30 terrain and D-infinity routing.

Pipeline for one facility:

1. **Inputs.**
   * Copernicus GLO-30 DEM tiles: public COGs on AWS
     (``copernicus-dem-30m``), downloaded over HTTPS.
   * OPERA DSWx-HLS ``B01_WTR`` granules (Earthdata).

   Both are cropped to the bbox and put on one 30 m UTM grid: bilinear
   resampling for the DEM, nearest for the WTR classes. DSWx tiles from the
   same acquisition date are mosaicked.
2. **Water boundaries.** Per epoch, water = WTR 1 (open water) or 2 (partial
   surface water) inside the facility boundary. WTR 252/253/254/255 (snow,
   cloud/shadow, ocean, fill) and vegetated cells are *unknown*.
3. **Setback.** ``D_crest(t)`` is the shortest Euclidean distance (metres, UTM)
   from the water polygons, i.e. pixel edges, to the embankment crest line.
   An epoch whose unknown area lies closer to the crest than the observed
   water is ``obscured``: D_crest is then only an upper bound and raises no
   alerts. Alerts: setback shrink ``Delta D_crest < -30 m`` (against the first
   valid epoch, or the previous valid epoch), and proximity ``D_crest < 15 m``.
4. **Terrain.** Horn slope/aspect, priority-flood depression filling (with
   epsilon so flats drain), Tarboton (1997) D-infinity flow directions,
   proportional flow accumulation, flow vectors and a stream network.
   Cells with NDVI > 0.35 (dense vegetation; GLO-30 is a surface model, so
   canopy hides the ground) are flagged unobservable and their slope, aspect
   and flow direction are masked.
5. **Outputs.** ``DATA_DIR/cache/<Facility>_hydrology.zarr``.
"""

from __future__ import annotations

import heapq
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

import geopandas as gpd
import numpy as np
import pandas as pd
import rioxarray  # noqa: F401  (registers the .rio accessor)
import xarray as xr
from rasterio.enums import Resampling
from rasterio.features import geometry_mask, shapes
from shapely.geometry import LineString, MultiLineString, Polygon, box, shape
from shapely.ops import unary_union

from app.config import Settings

logger = logging.getLogger("lithocreep.hydrology")

DSWX_DATASET_ID = "OPERA_L3_DSWX-HLS_V1.0"
# DSWx-HLS B01_WTR classes (PROTEUS dswx_hls.py, collapsed classes).
WTR_NOT_WATER, WTR_OPEN_WATER, WTR_PARTIAL_WATER = 0, 1, 2
WTR_SNOW, WTR_CLOUD, WTR_OCEAN, WTR_FILL = 252, 253, 254, 255
WATER_CLASSES = (WTR_OPEN_WATER, WTR_PARTIAL_WATER)
UNKNOWN_CLASSES = (WTR_SNOW, WTR_CLOUD, WTR_OCEAN, WTR_FILL)

GRID_RESOLUTION_M = 30.0
SETBACK_SHRINK_ALERT_M = -30.0
PROXIMITY_ALERT_M = 15.0
NDVI_VEGETATION_THRESHOLD = 0.35
STREAM_THRESHOLD_CELLS = 100
MIN_OBSERVED_FRACTION = 0.5  # below this share of known cells in the facility, an epoch is no-data
FILL_EPSILON_M = 1e-4

GLO30_URL = "https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif"
GLO30_ATTRIBUTION = (
    "Copernicus DEM GLO-30: (c) DLR e.V. 2010-2014 and (c) Airbus Defence and Space GmbH 2014-2018, "
    "provided under COPERNICUS by the European Union and ESA; all rights reserved"
)

# D_crest epoch status codes.
SETBACK_VALID, SETBACK_OBSCURED, SETBACK_NO_WATER, SETBACK_NO_DATA = 0, 1, 2, 3
SETBACK_STATUS_MEANINGS = "valid obscured-upper-bound no-water no-data"
# Alert bit flags.
ALERT_SHRINK_BASELINE, ALERT_SHRINK_PREVIOUS, ALERT_PROXIMITY = 1, 2, 4

# Tarboton (1997) facets: e1 (cardinal) and e2 (diagonal) offsets as (drow, dcol), rows
# increasing southwards; the facet angle is af * r + ac * pi/2, counter-clockwise from east.
DINF_FACETS = (
    ((0, 1), (-1, 1), 0, 1),
    ((-1, 0), (-1, 1), 1, -1),
    ((-1, 0), (-1, -1), 1, 1),
    ((0, -1), (-1, -1), 2, -1),
    ((0, -1), (1, -1), 2, 1),
    ((1, 0), (1, -1), 3, -1),
    ((1, 0), (1, 1), 3, 1),
    ((0, 1), (1, 1), 4, -1),
)


class HydrologyError(ValueError):
    """Invalid hydrology inputs."""


# --------------------------------------------------------------------------- #
# 1. Inputs: GLO-30 DEM and DSWx-HLS
# --------------------------------------------------------------------------- #


def glo30_tile_names(bbox: Sequence[float]) -> list[str]:
    """Copernicus GLO-30 1x1 degree tiles covering ``bbox``; names use the tile's SW corner."""
    xmin, ymin, xmax, ymax = bbox
    names = []
    for lat in range(math.floor(ymin), math.ceil(ymax) if ymax != math.floor(ymax) else int(ymax)):
        for lon in range(math.floor(xmin), math.ceil(xmax) if xmax != math.floor(xmax) else int(xmax)):
            ns = f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}_00"
            ew = f"{'E' if lon >= 0 else 'W'}{abs(lon):03d}_00"
            names.append(f"Copernicus_DSM_COG_10_{ns}_{ew}_DEM")
    return names


def download_glo30(bbox: Sequence[float], dest_dir: Path, *, timeout: int = 300) -> list[Path]:
    """Download the GLO-30 COG tiles for ``bbox`` (public HTTPS; existing files are reused).

    Tiles that do not exist (open ocean) return 404 and are skipped.
    """
    import requests

    dest_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in glo30_tile_names(bbox):
        path = dest_dir / f"{name}.tif"
        if not path.exists():
            url = GLO30_URL.format(name=name)
            logger.info("Downloading %s", url)
            response = requests.get(url, timeout=timeout, stream=True)
            if response.status_code == 404:
                logger.warning("GLO-30 tile %s does not exist (ocean?); skipped", name)
                continue
            response.raise_for_status()
            tmp = path.with_suffix(".part")
            with open(tmp, "wb") as fh:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
            tmp.replace(path)
        paths.append(path)
    return paths


def utm_grid_bounds(bbox: Sequence[float], crs: str, resolution: float = GRID_RESOLUTION_M) -> tuple[float, ...]:
    """Bounds in ``crs`` of the bbox, snapped outward to the grid resolution."""
    aoi = gpd.GeoSeries([box(*bbox)], crs="EPSG:4326").to_crs(crs)
    xmin, ymin, xmax, ymax = aoi.total_bounds
    snap = lambda v, f: f(v / resolution) * resolution  # noqa: E731
    return snap(xmin, math.floor), snap(ymin, math.floor), snap(xmax, math.ceil), snap(ymax, math.ceil)


def load_dem(paths: Sequence[Path], bbox: Sequence[float], target_crs: Optional[str] = None) -> xr.DataArray:
    """Mosaic, crop and reproject DEM tiles to a 30 m grid in ``target_crs`` (bilinear)."""
    from rioxarray.merge import merge_arrays

    from app.pipelines.ingestion import CLIP_PAD_DEG, TARGET_CRS

    if not paths:
        raise HydrologyError("No DEM tiles supplied")
    target_crs = target_crs or TARGET_CRS
    xmin, ymin, xmax, ymax = bbox
    pad = 2 * CLIP_PAD_DEG
    tiles = []
    for p in paths:
        da = rioxarray.open_rasterio(p, masked=True).squeeze("band", drop=True)
        try:
            tiles.append(da.rio.clip_box(xmin - pad, ymin - pad, xmax + pad, ymax + pad, crs="EPSG:4326"))
        except Exception:  # tile does not touch the bbox
            continue
    if not tiles:
        raise HydrologyError(f"No DEM tile overlaps bbox {tuple(bbox)}")
    dem = tiles[0] if len(tiles) == 1 else merge_arrays(tiles)
    gx0, gy0, gx1, gy1 = utm_grid_bounds(bbox, target_crs)
    nx, ny = int(round((gx1 - gx0) / GRID_RESOLUTION_M)), int(round((gy1 - gy0) / GRID_RESOLUTION_M))
    from affine import Affine

    transform = Affine(GRID_RESOLUTION_M, 0, gx0, 0, -GRID_RESOLUTION_M, gy1)
    out = dem.rio.reproject(target_crs, shape=(ny, nx), transform=transform, resampling=Resampling.bilinear)
    out = out.where(np.isfinite(out))
    out.name = "elevation"
    return out.rio.write_nodata(np.nan, encoded=False)


DSWX_FILENAME_RE = re.compile(r"OPERA_L3_DSWx-HLS_T(?P<tile>\w{5})_(?P<sensing>\d{8}T\d{6}Z)_\d{8}T\d{6}Z_(?P<sensor>\w+?)_30_v[\d.]+_B01_WTR")


def parse_dswx_filename(path: Path) -> tuple[str, datetime, str]:
    """(MGRS tile, sensing datetime, sensor) from a DSWx-HLS B01_WTR filename."""
    match = DSWX_FILENAME_RE.search(Path(path).name)
    if not match:
        raise HydrologyError(f"Not a DSWx-HLS B01_WTR filename: {Path(path).name}")
    return match["tile"], datetime.strptime(match["sensing"], "%Y%m%dT%H%M%SZ"), match["sensor"]


def load_dswx_series(paths: Sequence[Path], template: xr.DataArray) -> xr.DataArray:
    """WTR classes ``(time, y, x)`` on ``template``'s grid; same-date tiles mosaicked (first valid wins)."""
    by_date: dict[Any, list[tuple[datetime, Path]]] = {}
    for p in paths:
        _, sensed, _ = parse_dswx_filename(p)
        by_date.setdefault(sensed.date(), []).append((sensed, Path(p)))
    if not by_date:
        raise HydrologyError("No DSWx-HLS granules supplied")
    layers, times = [], []
    for day in sorted(by_date):
        merged = np.full(template.shape, WTR_FILL, dtype=np.uint8)
        for sensed, p in sorted(by_date[day]):
            da = rioxarray.open_rasterio(p).squeeze("band", drop=True)
            if da.rio.nodata is None:
                da = da.rio.write_nodata(WTR_FILL)
            values = da.rio.reproject_match(template, resampling=Resampling.nearest, nodata=WTR_FILL).values
            fill_here = merged == WTR_FILL
            merged[fill_here] = values[fill_here]
        layers.append(merged)
        times.append(min(s for s, _ in by_date[day]))
    return xr.DataArray(
        np.stack(layers),
        dims=("time", "y", "x"),
        coords={"time": pd.to_datetime(times), "y": template["y"].values, "x": template["x"].values},
        name="water_class",
    )


# --------------------------------------------------------------------------- #
# 2-3. Water boundaries, D_crest and alerts
# --------------------------------------------------------------------------- #


def read_line_geometry(path: Path, crs: str) -> LineString | MultiLineString:
    """Read a crest line (GeoJSON or any OGR vector) and return it in ``crs``."""
    gdf = gpd.read_file(path, engine="pyogrio")
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    geom = unary_union(gdf.to_crs(crs).geometry.values)
    if geom.geom_type not in {"LineString", "MultiLineString"}:
        raise HydrologyError(f"{Path(path).name} must contain LineString geometry, got {geom.geom_type}")
    return geom


def read_polygon_geometry(path: Path, crs: str):
    gdf = gpd.read_file(path, engine="pyogrio")
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    geom = unary_union(gdf.to_crs(crs).geometry.values)
    if geom.geom_type not in {"Polygon", "MultiPolygon"}:
        raise HydrologyError(f"{Path(path).name} must contain Polygon geometry, got {geom.geom_type}")
    return geom


def _mask_polygons(mask: np.ndarray, transform) -> Any:
    """Union of the pixel-edge polygons of ``mask`` (None when empty)."""
    if not mask.any():
        return None
    polys = [shape(geom) for geom, value in shapes(mask.astype(np.uint8), mask=mask, transform=transform) if value == 1]
    return unary_union(polys)


@dataclass
class SetbackEpoch:
    distance_m: float
    status: int
    water_area_m2: float
    unknown_distance_m: float
    observed_fraction: float


def setback_distance(
    water_class: np.ndarray,
    transform,
    crest,
    facility_mask: np.ndarray,
    unknown_extra: Optional[np.ndarray] = None,
) -> SetbackEpoch:
    """Shortest distance (m) from the facility's water polygons to the crest line, with obscuration check."""
    inside = facility_mask
    water = np.isin(water_class, WATER_CLASSES) & inside
    unknown = np.isin(water_class, UNKNOWN_CLASSES) & inside
    if unknown_extra is not None:
        unknown |= unknown_extra & inside & ~water
    n_inside = int(inside.sum())
    observed_fraction = 1.0 - unknown.sum() / n_inside if n_inside else 0.0
    cell_area = abs(transform.a * transform.e)

    water_geom = _mask_polygons(water, transform)
    unknown_geom = _mask_polygons(unknown, transform)
    distance = float(water_geom.distance(crest)) if water_geom is not None else float("nan")
    unknown_distance = float(unknown_geom.distance(crest)) if unknown_geom is not None else float("nan")

    if observed_fraction < MIN_OBSERVED_FRACTION:
        status = SETBACK_NO_DATA
    elif water_geom is None:
        status = SETBACK_NO_WATER
    elif np.isfinite(unknown_distance) and unknown_distance < distance:
        status = SETBACK_OBSCURED
    else:
        status = SETBACK_VALID
    return SetbackEpoch(distance, status, float(water.sum() * cell_area), unknown_distance, float(observed_fraction))


def setback_alerts(distances: np.ndarray, statuses: np.ndarray) -> dict[str, np.ndarray]:
    """Changes and alert flags; only ``valid`` epochs are compared or alerted on."""
    n = distances.size
    delta_baseline = np.full(n, np.nan)
    delta_previous = np.full(n, np.nan)
    flags = np.zeros(n, dtype=np.int8)
    baseline = previous = None
    for i in range(n):
        if statuses[i] != SETBACK_VALID:
            continue
        d = distances[i]
        if baseline is None:
            baseline = d
        delta_baseline[i] = d - baseline
        if previous is not None:
            delta_previous[i] = d - previous
        if delta_baseline[i] < SETBACK_SHRINK_ALERT_M:
            flags[i] |= ALERT_SHRINK_BASELINE
        if np.isfinite(delta_previous[i]) and delta_previous[i] < SETBACK_SHRINK_ALERT_M:
            flags[i] |= ALERT_SHRINK_PREVIOUS
        if d < PROXIMITY_ALERT_M:
            flags[i] |= ALERT_PROXIMITY
        previous = d
    return {"delta_baseline_m": delta_baseline, "delta_previous_m": delta_previous, "alert_flags": flags}


# --------------------------------------------------------------------------- #
# 4. Terrain: slope/aspect, depression filling, D-infinity
# --------------------------------------------------------------------------- #


def slope_aspect(dem: np.ndarray, resolution: float) -> tuple[np.ndarray, np.ndarray]:
    """Horn (1981) slope (degrees) and aspect (degrees clockwise from north, downslope direction).

    Edge cells and cells with a NaN neighbour are NaN; flat cells have NaN aspect.
    """
    z = np.pad(dem.astype(np.float64), 1, constant_values=np.nan)
    a, b, c = z[:-2, :-2], z[:-2, 1:-1], z[:-2, 2:]
    d, f = z[1:-1, :-2], z[1:-1, 2:]
    g, h, i = z[2:, :-2], z[2:, 1:-1], z[2:, 2:]
    dz_dx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8 * resolution)  # towards east
    dz_dy = ((a + 2 * b + c) - (g + 2 * h + i)) / (8 * resolution)  # towards north (row 0 is north)
    slope = np.degrees(np.arctan(np.hypot(dz_dx, dz_dy)))
    aspect = np.degrees(np.arctan2(-dz_dx, -dz_dy)) % 360.0  # azimuth of -grad z
    aspect[~(slope > 1e-6)] = np.nan
    return slope, aspect


def fill_depressions(dem: np.ndarray, epsilon: float = FILL_EPSILON_M) -> np.ndarray:
    """Priority-flood depression filling with epsilon gradients (Barnes et al., 2014).

    Grid edges and NaN cells are outlets. Every filled cell ends at least
    ``epsilon`` above the neighbour it drains to, so filled flats still drain.
    """
    filled = dem.astype(np.float64).copy()
    ny, nx = filled.shape
    closed = np.zeros(filled.shape, dtype=bool)
    heap: list[tuple[float, int, int, int]] = []
    counter = 0
    nan = ~np.isfinite(filled)
    for r in range(ny):
        for c in range(nx):
            if nan[r, c]:
                closed[r, c] = True
                continue
            edge = r in (0, ny - 1) or c in (0, nx - 1)
            near_nan = any(
                0 <= r + dr < ny and 0 <= c + dc < nx and nan[r + dr, c + dc]
                for dr in (-1, 0, 1) for dc in (-1, 0, 1)
            )
            if edge or near_nan:
                heapq.heappush(heap, (filled[r, c], counter, r, c))
                counter += 1
                closed[r, c] = True
    while heap:
        z, _, r, c = heapq.heappop(heap)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                rr, cc = r + dr, c + dc
                if (dr or dc) and 0 <= rr < ny and 0 <= cc < nx and not closed[rr, cc]:
                    closed[rr, cc] = True
                    if filled[rr, cc] <= z:
                        filled[rr, cc] = z + epsilon
                    heapq.heappush(heap, (filled[rr, cc], counter, rr, cc))
                    counter += 1
    return filled


@dataclass
class DinfResult:
    angle: np.ndarray  # radians counter-clockwise from east; NaN where no downslope outflow
    slope: np.ndarray  # tangent of the steepest downslope facet
    receiver1: np.ndarray  # flat index of the cardinal receiver, -1 if none
    receiver2: np.ndarray  # flat index of the diagonal receiver, -1 if none
    proportion1: np.ndarray  # share of flow to receiver1 (receiver2 gets the rest)


def dinf_flow_directions(dem: np.ndarray, resolution: float) -> DinfResult:
    """Tarboton (1997) D-infinity: steepest downslope direction over the 8 triangular facets."""
    z = np.pad(dem.astype(np.float64), 1, constant_values=np.nan)
    ny, nx = dem.shape
    e0 = z[1:-1, 1:-1]
    d1 = d2 = resolution
    theta_max = math.atan2(d2, d1)
    best_s = np.full(dem.shape, -np.inf)
    best_angle = np.full(dem.shape, np.nan)
    best_facet = np.full(dem.shape, -1, dtype=np.int8)
    best_r = np.zeros(dem.shape)
    for k, ((r1, c1), (r2, c2), ac, af) in enumerate(DINF_FACETS):
        e1 = z[1 + r1 : 1 + r1 + ny, 1 + c1 : 1 + c1 + nx]
        e2 = z[1 + r2 : 1 + r2 + ny, 1 + c2 : 1 + c2 + nx]
        s1 = (e0 - e1) / d1
        s2 = (e1 - e2) / d2
        r = np.arctan2(s2, s1)
        s = np.hypot(s1, s2)
        low = r < 0
        r = np.where(low, 0.0, r)
        s = np.where(low, s1, s)
        high = r > theta_max
        r = np.where(high, theta_max, r)
        s = np.where(high, (e0 - e2) / math.hypot(d1, d2), s)
        s = np.where(np.isfinite(s), s, -np.inf)
        better = s > best_s
        best_s = np.where(better, s, best_s)
        best_r = np.where(better, r, best_r)
        best_facet = np.where(better, k, best_facet)
        best_angle = np.where(better, af * r + ac * math.pi / 2, best_angle)

    flows = best_s > 0
    rows, cols = np.indices(dem.shape)
    receiver1 = np.full(dem.shape, -1, dtype=np.int64)
    receiver2 = np.full(dem.shape, -1, dtype=np.int64)
    proportion1 = np.zeros(dem.shape)
    for k, ((r1, c1), (r2, c2), _, _) in enumerate(DINF_FACETS):
        sel = flows & (best_facet == k)
        p1 = 1.0 - best_r[sel] / theta_max
        receiver1[sel] = np.where(p1 > 1e-12, (rows[sel] + r1) * nx + (cols[sel] + c1), -1)
        receiver2[sel] = np.where(p1 < 1 - 1e-12, (rows[sel] + r2) * nx + (cols[sel] + c2), -1)
        proportion1[sel] = p1
    angle = np.where(flows, np.mod(best_angle, 2 * math.pi), np.nan)
    return DinfResult(angle, np.where(flows, best_s, 0.0), receiver1, receiver2, proportion1)


def dinf_accumulation(dem: np.ndarray, flow: DinfResult, weights: Optional[np.ndarray] = None) -> np.ndarray:
    """Upslope contributing cells under D-infinity, processed from high to low elevation."""
    acc = (np.ones(dem.size) if weights is None else weights.astype(np.float64).ravel().copy())
    valid = np.isfinite(dem.ravel())
    acc[~valid] = 0.0
    order = np.argsort(-np.where(valid, dem.ravel(), -np.inf), kind="stable")
    r1, r2, p1 = flow.receiver1.ravel(), flow.receiver2.ravel(), flow.proportion1.ravel()
    for idx in order:
        if not valid[idx]:
            continue
        a = acc[idx]
        if r1[idx] >= 0:
            acc[r1[idx]] += a * p1[idx]
        if r2[idx] >= 0:
            acc[r2[idx]] += a * (1.0 - p1[idx])
    return acc.reshape(dem.shape)


def trace_flow_path(flow: DinfResult, start: tuple[int, int], max_steps: int = 10_000) -> list[tuple[int, int]]:
    """Steepest-descent path from ``start`` (row, col), following the dominant D-infinity receiver."""
    ny, nx = flow.angle.shape
    r1, r2, p1 = flow.receiver1.ravel(), flow.receiver2.ravel(), flow.proportion1.ravel()
    idx = start[0] * nx + start[1]
    path = [start]
    seen = {idx}
    for _ in range(max_steps):
        nxt = r1[idx] if (p1[idx] >= 0.5 and r1[idx] >= 0) or r2[idx] < 0 else r2[idx]
        if nxt < 0 or nxt in seen:
            break
        seen.add(nxt)
        idx = nxt
        path.append((int(idx // nx), int(idx % nx)))
    return path


# --------------------------------------------------------------------------- #
# 5. Assembly and I/O
# --------------------------------------------------------------------------- #


def terrain_dataset(
    dem: xr.DataArray,
    *,
    vegetated: Optional[np.ndarray] = None,
    stream_threshold: int = STREAM_THRESHOLD_CELLS,
) -> xr.Dataset:
    res = abs(float(dem.rio.resolution()[0]))
    z = dem.values.astype(np.float64)
    filled = fill_depressions(z)
    slope, aspect = slope_aspect(z, res)
    flow = dinf_flow_directions(filled, res)
    acc = dinf_accumulation(filled, flow)
    angle = flow.angle
    u, v = np.cos(angle), np.sin(angle)  # east, north components
    azimuth = np.mod(90.0 - np.degrees(angle), 360.0)
    area = acc * res * res
    veg = np.zeros(z.shape, dtype=bool) if vegetated is None else vegetated
    nodata = ~np.isfinite(z)
    for arr in (slope, aspect, azimuth, u, v):
        arr[veg | nodata] = np.nan
    observability = np.zeros(z.shape, dtype=np.int8)
    observability[veg] = 1
    observability[nodata] = 2
    yx = ("y", "x")
    ds = xr.Dataset(
        {
            "elevation": (yx, z.astype(np.float32), {"units": "m"}),
            "elevation_filled": (yx, filled.astype(np.float32), {"units": "m"}),
            "depression_depth": (yx, (filled - z).astype(np.float32), {"units": "m", "long_name": "priority-flood fill depth (closed depressions, e.g. ponds)"}),
            "slope_deg": (yx, slope.astype(np.float32), {"units": "degree"}),
            "aspect_deg": (yx, aspect.astype(np.float32), {"units": "degree", "long_name": "downslope azimuth, clockwise from north (Horn)"}),
            "flow_direction_deg": (yx, azimuth.astype(np.float32), {"units": "degree", "long_name": "D-infinity flow azimuth, clockwise from north"}),
            "flow_u": (yx, u.astype(np.float32), {"long_name": "D-infinity unit flow vector, east component"}),
            "flow_v": (yx, v.astype(np.float32), {"long_name": "D-infinity unit flow vector, north component"}),
            "flow_accumulation_cells": (yx, acc.astype(np.float32)),
            "contributing_area_m2": (yx, area.astype(np.float32), {"units": "m2"}),
            "accumulation_vector_u": (yx, (area * u).astype(np.float32), {"units": "m2", "long_name": "contributing area x flow_u"}),
            "accumulation_vector_v": (yx, (area * v).astype(np.float32), {"units": "m2", "long_name": "contributing area x flow_v"}),
            "stream_network": (yx, (acc >= stream_threshold) & ~nodata, {"long_name": f"D-infinity accumulation >= {stream_threshold} cells"}),
            "terrain_observability": (yx, observability, {"flag_values": [0, 1, 2], "flag_meanings": "observable unobservable-vegetated no-data"}),
        },
        coords={"y": dem["y"].values, "x": dem["x"].values},
    )
    ds.attrs.update(
        {
            "stream_threshold_cells": stream_threshold,
            "routing": "priority-flood fill (epsilon) + Tarboton (1997) D-infinity",
            "slope_method": "Horn (1981)",
        }
    )
    return ds


def vegetation_mask(ndvi_source: Optional[Path], template: xr.DataArray) -> tuple[Optional[np.ndarray], str]:
    """NDVI > 0.35 on the template grid, from a Phase 2 spectral Zarr (``ndvi``) or an NDVI raster."""
    if ndvi_source is None or not Path(ndvi_source).exists():
        return None, "unavailable (no NDVI source)"
    path = Path(ndvi_source)
    if path.suffix == ".zarr":
        ndvi = xr.open_zarr(path, decode_coords="all")["ndvi"]
    else:
        ndvi = rioxarray.open_rasterio(path, masked=True).squeeze("band", drop=True)
    ndvi = ndvi.rio.write_nodata(np.nan, encoded=False).rio.reproject_match(template, resampling=Resampling.bilinear)
    return np.asarray(ndvi.values > NDVI_VEGETATION_THRESHOLD), f"NDVI > {NDVI_VEGETATION_THRESHOLD} from {path.name}"


def facility_mask_for(template: xr.DataArray, bbox: Sequence[float], boundary=None) -> tuple[np.ndarray, Any]:
    crs = template.rio.crs
    geom = boundary if boundary is not None else gpd.GeoSeries([box(*bbox)], crs="EPSG:4326").to_crs(crs).iloc[0]
    mask = geometry_mask([geom], out_shape=template.shape, transform=template.rio.transform(), invert=True, all_touched=False)
    return mask, geom


def run_hydrology(
    facility: str,
    bbox: Sequence[float],
    output_path: Path,
    *,
    settings: Settings,
    dem_paths: Sequence[Path],
    dswx_paths: Sequence[Path] = (),
    crest_path: Optional[Path] = None,
    boundary_path: Optional[Path] = None,
    ndvi_source: Optional[Path] = None,
    stream_threshold: int = STREAM_THRESHOLD_CELLS,
) -> dict[str, Any]:
    """Build the hydrology dataset for one facility and write it as Zarr."""
    import zarr

    from app.pipelines.ingestion import TARGET_CRS, validate_bbox

    bbox = validate_bbox(bbox)
    dem = load_dem(dem_paths, bbox, TARGET_CRS)
    vegetated, veg_note = vegetation_mask(ndvi_source, dem)
    if vegetated is None:
        logger.warning("Vegetation filter %s", veg_note)
    ds = terrain_dataset(dem, vegetated=vegetated, stream_threshold=stream_threshold)

    crs = str(dem.rio.crs)
    boundary = read_polygon_geometry(boundary_path, crs) if boundary_path else None
    facility_mask, facility_geom = facility_mask_for(dem, bbox, boundary)
    crest = read_line_geometry(crest_path, crs) if crest_path else None

    summary: dict[str, Any] = {"facility": facility, "output": str(output_path), "n_dswx_epochs": 0, "alerts": []}
    if dswx_paths:
        wtr = load_dswx_series(dswx_paths, dem)
        ds["water_class"] = wtr
        ds["water_class"].attrs.update(
            {"flag_values": [0, 1, 2, 252, 253, 254, 255],
             "flag_meanings": "not-water open-water partial-surface-water snow-ice cloud-shadow ocean fill"}
        )
        ds["water_area_m2"] = ("time", np.array([float((np.isin(w, WATER_CLASSES) & facility_mask).sum()) * GRID_RESOLUTION_M**2 for w in wtr.values], dtype=np.float32))
        summary["n_dswx_epochs"] = int(wtr.sizes["time"])
        if crest is not None:
            transform = dem.rio.transform()
            epochs = [setback_distance(w, transform, crest, facility_mask, vegetated) for w in wtr.values]
            distances = np.array([e.distance_m for e in epochs])
            statuses = np.array([e.status for e in epochs], dtype=np.int8)
            alerts = setback_alerts(distances, statuses)
            ds["d_crest_m"] = ("time", distances.astype(np.float32), {"units": "m", "long_name": "shortest distance water boundary -> crest line"})
            ds["d_crest_status"] = ("time", statuses, {"flag_values": [0, 1, 2, 3], "flag_meanings": SETBACK_STATUS_MEANINGS})
            ds["d_crest_unknown_distance_m"] = ("time", np.array([e.unknown_distance_m for e in epochs], dtype=np.float32), {"units": "m"})
            ds["observed_fraction"] = ("time", np.array([e.observed_fraction for e in epochs], dtype=np.float32))
            ds["delta_d_crest_baseline_m"] = ("time", alerts["delta_baseline_m"].astype(np.float32), {"units": "m"})
            ds["delta_d_crest_previous_m"] = ("time", alerts["delta_previous_m"].astype(np.float32), {"units": "m"})
            ds["setback_alert_flags"] = ("time", alerts["alert_flags"], {"flag_masks": [1, 2, 4], "flag_meanings": "shrink-vs-baseline shrink-vs-previous proximity-lt-15m"})
            names = {ALERT_SHRINK_BASELINE: "SETBACK_SHRINK_VS_BASELINE", ALERT_SHRINK_PREVIOUS: "SETBACK_SHRINK_VS_PREVIOUS", ALERT_PROXIMITY: "CREST_PROXIMITY"}
            for t, d, flag in zip(wtr["time"].values, distances, alerts["alert_flags"]):
                for bit, name in names.items():
                    if flag & bit:
                        summary["alerts"].append({"time": str(pd.Timestamp(t)), "alert": name, "d_crest_m": float(d)})
            summary["d_crest_valid_epochs"] = int((statuses == SETBACK_VALID).sum())
        else:
            logger.warning("No crest line supplied: D_crest and setback alerts are not computed")
    else:
        logger.warning("No DSWx-HLS granules supplied: terrain products only")

    ds.attrs.update(
        {
            "facility": facility,
            "bbox_epsg4326": json.dumps(list(bbox)),
            "vegetation_filter": veg_note,
            "crest_line": str(crest_path) if crest is not None else "not supplied",
            "facility_boundary": str(boundary_path) if boundary_path else "bbox",
            "setback_shrink_alert_m": SETBACK_SHRINK_ALERT_M,
            "proximity_alert_m": PROXIMITY_ALERT_M,
            "dem_source": GLO30_ATTRIBUTION,
            "dem_files": json.dumps([Path(p).name for p in dem_paths]),
            "dswx_source": DSWX_DATASET_ID if dswx_paths else "none",
            "grid_resolution_m": GRID_RESOLUTION_M,
        }
    )
    ds = ds.rio.write_crs(dem.rio.crs)
    for name in ds.data_vars:
        if {"y", "x"} <= set(ds[name].dims):
            ds[name].attrs["grid_mapping"] = "spatial_ref"

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    compressor = zarr.Blosc(cname="zstd", clevel=5, shuffle=zarr.Blosc.BITSHUFFLE)
    ds.to_zarr(output_path, mode="w", consolidated=True, encoding={n: {"compressor": compressor} for n in ds.data_vars})

    stream = ds["stream_network"].values
    summary.update(
        {
            "grid_shape": list(dem.shape),
            "elevation_range_m": [float(np.nanmin(dem.values)), float(np.nanmax(dem.values))],
            "max_depression_depth_m": float(np.nanmax(ds["depression_depth"].values)),
            "stream_cells": int(stream.sum()),
            "vegetation_filter": veg_note,
            "crest_line": ds.attrs["crest_line"],
        }
    )
    logger.info("Hydrology %s -> %s: %d DSWx epochs, %d alerts", facility, output_path, summary["n_dswx_epochs"], len(summary["alerts"]))
    return summary
