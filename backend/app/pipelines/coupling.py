"""Spatial-topological coupling of the spectral, kinematic and hydrological layers and the 4-regime classifier.

Pipeline for one facility:

1. **Alignment.** Every layer goes onto the hydrology DEM grid (30 m, EPSG:32612,
   snapped to the bbox).
   * EMIT layers (60 m): bilinear for continuous values (SAI, RMSE, NDVI),
     nearest for flags.
   * OPERA DISP-S1 layers: nearest, so each cell keeps one GP fit with its
     own velocity, uncertainty and status.
   The aligned grids are checked to share transform, shape and CRS.
2. **Topology.** A directed acyclic graph of cells: each cell links to its
   D-infinity receivers on the depression-filled DEM. Receivers are strictly
   lower, so the graph is acyclic and descending elevation is a topological
   order. A *candidate* is a mid-slope cell (has both donors and receivers;
   not water) with a kinematic anomaly (|v| > 2 sigma_v, or acceleration in
   the direction of motion > 2 sigma_a). It is **coupled** when all of these
   hold:
   * a pond-boundary cell is upslope of it (a DAG ancestor);
   * a high-seepage cell (SAI > 0.25) is downslope of it (a DAG descendant);
   * the facility setback is encroaching (latest valid Phase 4 epoch carries
     a shrink or proximity alert);
   * GLO-30 aspect paths (steepest descent by Horn aspect, D8 steps)
     independently confirm both links: a water-boundary aspect path passes
     within one cell of the candidate, and the candidate's aspect path passes
     within one cell of a seepage cell.
3. **Regimes** (codes in ``REGIME_MEANINGS``), first match wins:

   | Order | Condition | Result |
   |-------|-----------|--------|
   | 1 | Coherence < 0.65 (or unknown inside the stack) or active earthworks | **REGIME_2**, unobservable / anthropogenic |
   | 2 | Spectral RMSE > 0.05, vegetated, or missing data | **0**, rejected or unclassified |
   | 3 | High SAI, accelerating and coupled | **REGIME_4**, Tier-3 critical, with a failure-time estimate |
   | 4 | High SAI, otherwise | **REGIME_3**, Tier-1 watch. An accelerating but uncoupled cell gets an extra flag |
   | 5 | Low SAI, accelerating | **5**, kinematic-only anomaly (outside the 4-regime scheme; see DECISIONS.md) |
   | 6 | Low SAI, stable or linear | **REGIME_1**, benign consolidation |

4. **Uncertainty.** All bounds below are 95 % (+-2 sigma).
   * SAI: ``sigma = rmse * sqrt(w^T (M^T M)^-1 w)``, the linearised
     propagation of the unmixing residual through the endmember matrix.
   * Velocity: GP posterior variance combined with InSAR phase noise from
     temporal coherence. The Cramer-Rao single-look phase variance
     ``(1 - g^2) / (2 g^2)`` is converted to LOS mm with the Sentinel-1
     wavelength and propagated to a velocity over the stack span.
   * Acceleration: GP bounds.
   * Instability time: the kinematics bounds.
   Cells with RMSE > 0.05 or coherence < 0.65 are flagged low confidence.
5. **Output.** ``DATA_DIR/cache/<Facility>_coupled.zarr``.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
import pandas as pd
import rioxarray  # noqa: F401  (registers the .rio accessor)
import xarray as xr
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from scipy import ndimage

from app.pipelines import hydrology as hyd
from app.pipelines import kinematics as kin

logger = logging.getLogger("lithocreep.coupling")

SAI_THRESHOLD = 0.25
RMSE_THRESHOLD = 0.05
COHERENCE_THRESHOLD = 0.65
VELOCITY_THRESHOLD_MM_YR = 2.0
SIGMA = 2.0
SENTINEL1_WAVELENGTH_MM = 55.465763  # C-band
ASPECT_PATH_MAX_STEPS = 2000

REGIME_UNCLASSIFIED, REGIME_1, REGIME_2, REGIME_3, REGIME_4, REGIME_KINEMATIC_ONLY = 0, 1, 2, 3, 4, 5
REGIME_MEANINGS = (
    "unclassified-or-rejected regime1-benign-consolidation regime2-unobservable-anthropogenic "
    "regime3-legacy-seepage-tier1-watch regime4-active-instability-tier3-critical kinematic-anomaly-low-sai"
)
TIER_NONE, TIER1_WATCH, TIER3_CRITICAL = 0, 1, 3
HAZARD_LEVELS = {TIER_NONE: "NONE", TIER1_WATCH: "WATCH_TIER1", TIER3_CRITICAL: "CRITICAL_TIER3"}

# classification_flags bits
FLAG_LOW_COHERENCE = 1
FLAG_HIGH_RMSE = 2
FLAG_EARTHWORKS = 4
FLAG_VEGETATED = 8
FLAG_NO_SPECTRAL = 16
FLAG_NO_KINEMATICS = 32
FLAG_SAI_MARGINAL = 64  # the SAI 95% interval straddles the 0.25 threshold
FLAG_ACCEL_NOT_COUPLED = 128
FLAG_MEANINGS = (
    "low-coherence high-spectral-rmse active-earthworks vegetated no-spectral-data no-kinematic-data "
    "sai-interval-straddles-threshold accelerating-not-coupled"
)

DISCLAIMER = (
    "Screening classification from satellite data. REGIME_4 failure-time estimates are inverse-velocity trend "
    "extrapolations that indicate a potential change in deformation trend, NOT a guaranteed or predicted collapse "
    "date; they are biased late in tests (see IMPLEMENTATION_STATUS.md). This does not certify or replace "
    "on-site geotechnical monitoring."
)

D8_STEPS = ((-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1))  # N, NE, E, SE, S, SW, W, NW


class CouplingError(ValueError):
    """Invalid or inconsistent coupling inputs."""


# --------------------------------------------------------------------------- #
# 1. Alignment
# --------------------------------------------------------------------------- #


def _open(path: Path) -> xr.Dataset:
    if not Path(path).exists():
        raise FileNotFoundError(path)
    return xr.open_zarr(path, decode_coords="all")


def as_days(da: xr.DataArray) -> xr.DataArray:
    """Float days, whether the variable was decoded as CF timedelta (``units: days``) or left numeric."""
    if np.issubdtype(da.dtype, np.timedelta64):
        return (da / np.timedelta64(1, "D")).astype(np.float64)
    return da


def align(da: xr.DataArray, master: xr.DataArray, resampling: Resampling, nodata: float) -> np.ndarray:
    """Reproject ``da`` onto ``master``'s grid and return the values."""
    if da.rio.crs is None:
        raise CouplingError(f"{da.name} has no CRS")
    if np.issubdtype(da.dtype, np.timedelta64) or np.issubdtype(da.dtype, np.datetime64):
        raise CouplingError(f"{da.name} is a time type; convert it explicitly before alignment")
    src = da.astype(np.float64).rio.write_nodata(nodata, encoded=False)
    return src.rio.reproject_match(master, resampling=resampling, nodata=nodata).values


def same_grid(a: xr.DataArray, b: xr.DataArray) -> bool:
    return a.shape == b.shape and a.rio.crs == b.rio.crs and a.rio.transform().almost_equals(b.rio.transform())


# --------------------------------------------------------------------------- #
# 2. Topology: DAG and aspect paths
# --------------------------------------------------------------------------- #


def build_dag(elevation_filled: np.ndarray, resolution: float) -> tuple[hyd.DinfResult, np.ndarray]:
    """D-infinity receivers on the filled DEM plus a topological order (descending elevation)."""
    flow = hyd.dinf_flow_directions(elevation_filled, resolution)
    z = elevation_filled.ravel()
    order = np.argsort(-np.where(np.isfinite(z), z, -np.inf), kind="stable")
    return flow, order


def dag_is_acyclic(elevation_filled: np.ndarray, flow: hyd.DinfResult) -> bool:
    """Every edge goes strictly downhill, so descending elevation is a valid topological order."""
    z = elevation_filled.ravel()
    for receivers in (flow.receiver1.ravel(), flow.receiver2.ravel()):
        has = receivers >= 0
        if np.any(z[receivers[has]] >= z[np.flatnonzero(has)]):
            return False
    return True


def propagate_downslope(seed: np.ndarray, flow: hyd.DinfResult, order: np.ndarray) -> np.ndarray:
    """Cells reachable from ``seed`` by following flow (seed cells included)."""
    reach = seed.ravel().copy()
    r1, r2 = flow.receiver1.ravel(), flow.receiver2.ravel()
    for idx in order:
        if reach[idx]:
            if r1[idx] >= 0:
                reach[r1[idx]] = True
            if r2[idx] >= 0:
                reach[r2[idx]] = True
    return reach.reshape(seed.shape)


def propagate_upslope(seed: np.ndarray, flow: hyd.DinfResult, order: np.ndarray) -> np.ndarray:
    """Cells from which flow reaches ``seed`` (seed cells included)."""
    reach = seed.ravel().copy()
    r1, r2 = flow.receiver1.ravel(), flow.receiver2.ravel()
    for idx in order[::-1]:
        if not reach[idx] and ((r1[idx] >= 0 and reach[r1[idx]]) or (r2[idx] >= 0 and reach[r2[idx]])):
            reach[idx] = True
    return reach.reshape(seed.shape)


def strict_descendant_hit(reach_up: np.ndarray, flow: hyd.DinfResult) -> np.ndarray:
    """True where a receiver (hence a strict descendant) lies in ``reach_up``."""
    r1, r2 = flow.receiver1.ravel(), flow.receiver2.ravel()
    flat = reach_up.ravel()
    hit = np.zeros(flat.shape, dtype=bool)
    for r in (r1, r2):
        ok = r >= 0
        hit[ok] |= flat[r[ok]]
    return hit.reshape(reach_up.shape)


def aspect_path(aspect: np.ndarray, start: tuple[int, int], max_steps: int = ASPECT_PATH_MAX_STEPS) -> list[tuple[int, int]]:
    """Steepest-descent path following Horn aspect in D8 steps; stops at NaN aspect, the edge or a loop."""
    ny, nx = aspect.shape
    r, c = start
    path = [start]
    seen = {start}
    for _ in range(max_steps):
        a = aspect[r, c]
        if not np.isfinite(a):
            break
        dr, dc = D8_STEPS[int(round(a / 45.0)) % 8]
        r, c = r + dr, c + dc
        if not (0 <= r < ny and 0 <= c < nx) or (r, c) in seen:
            break
        seen.add((r, c))
        path.append((r, c))
    return path


# --------------------------------------------------------------------------- #
# 4. Uncertainty
# --------------------------------------------------------------------------- #


def sai_sigma_factor(spectral: xr.Dataset) -> float:
    """sqrt(w^T (M^T M)^-1 w) for the SAI fraction weights, from the endmember matrix used in Phase 2."""
    from app.pipelines import unmixing

    name = spectral.attrs.get("endmember_library_file")
    path = unmixing.REFERENCE_DIR / name if name and (unmixing.REFERENCE_DIR / name).exists() else None
    library = unmixing.load_spectral_library(path)
    wavelengths = np.asarray(json.loads(spectral.attrs["active_wavelengths_nm"]), dtype=np.float64)
    M = unmixing.build_endmember_matrix(wavelengths, library)
    names = list(library.endmembers)
    w = np.zeros(len(names))
    for endmember, weight in unmixing.SAI_FRACTION_WEIGHTS.items():
        w[names.index(endmember)] = weight
    return float(np.sqrt(w @ np.linalg.inv(M.T @ M) @ w))


def phase_noise_velocity_sigma(coherence: np.ndarray, n_epochs: int, span_years: float) -> np.ndarray:
    """Velocity std (mm/yr) from single-look interferometric phase noise.

    The per-epoch LOS std is ``lambda/(4 pi) * sqrt((1 - g^2) / (2 g^2))``.
    The slope std of n evenly spaced epochs over span T is
    ``sigma_d * sqrt(12 / n) / T``.
    """
    g = np.clip(coherence, 1e-3, 1.0)
    sigma_phase = np.sqrt((1 - g**2) / (2 * g**2))
    sigma_d = SENTINEL1_WAVELENGTH_MM / (4 * math.pi) * sigma_phase
    if n_epochs < 2 or span_years <= 0:
        return np.full(np.shape(coherence), np.nan)
    return sigma_d * math.sqrt(12.0 / n_epochs) / span_years


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


def latest_setback_state(hydro: xr.Dataset) -> dict[str, Any]:
    """Latest valid Phase 4 setback epoch: encroaching if it carries a shrink or proximity alert."""
    if "d_crest_status" not in hydro or "setback_alert_flags" not in hydro:
        return {"available": False, "encroaching": False, "reason": "no crest line / DSWx setback series"}
    status = hydro["d_crest_status"].values
    valid = np.flatnonzero(status == hyd.SETBACK_VALID)
    if valid.size == 0:
        return {"available": False, "encroaching": False, "reason": "no valid setback epoch"}
    i = int(valid[-1])
    flags = int(hydro["setback_alert_flags"].values[i])
    return {
        "available": True,
        "encroaching": bool(flags),
        "time": str(pd.Timestamp(hydro["time"].values[i])),
        "d_crest_m": float(hydro["d_crest_m"].values[i]),
        "alert_flags": flags,
        "epoch_index": i,
    }


def water_boundary_cells(water_class: np.ndarray) -> np.ndarray:
    """Water (WTR 1/2) cells with at least one non-water 8-neighbour."""
    water = np.isin(water_class, hyd.WATER_CLASSES)
    return water & ~ndimage.binary_erosion(water, structure=np.ones((3, 3)), border_value=1)


def classify(
    *,
    sai: np.ndarray,
    sai_sigma: np.ndarray,
    rmse: np.ndarray,
    vegetated: np.ndarray,
    spectral_valid: np.ndarray,
    coherence: np.ndarray,
    kin_status: np.ndarray,
    kin_valid: np.ndarray,
    accelerating: np.ndarray,
    coupled: np.ndarray,
    earthworks: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Per-cell regime code and classification flag bits (precedence as in the module docstring)."""
    flags = np.zeros(sai.shape, dtype=np.int16)
    # Low coherence: measured below 0.65, or unknown inside the stack (Phase 3 "unobservable").
    # Cells outside the InSAR footprint (status no-data, NaN coherence) are missing data instead.
    low_coh = (coherence < COHERENCE_THRESHOLD) | (kin_status == kin.UNOBSERVABLE)
    high_rmse = rmse > RMSE_THRESHOLD
    flags[low_coh] |= FLAG_LOW_COHERENCE
    flags[high_rmse] |= FLAG_HIGH_RMSE
    flags[earthworks] |= FLAG_EARTHWORKS
    flags[vegetated] |= FLAG_VEGETATED
    flags[~spectral_valid] |= FLAG_NO_SPECTRAL
    flags[~kin_valid & ~low_coh] |= FLAG_NO_KINEMATICS

    high_sai = sai > SAI_THRESHOLD
    lower, upper = sai - SIGMA * sai_sigma, sai + SIGMA * sai_sigma
    flags[spectral_valid & (lower <= SAI_THRESHOLD) & (upper > SAI_THRESHOLD)] |= FLAG_SAI_MARGINAL

    regime = np.full(sai.shape, REGIME_UNCLASSIFIED, dtype=np.int8)
    decided = np.zeros(sai.shape, dtype=bool)

    def assign(mask: np.ndarray, code: int) -> None:
        nonlocal decided
        m = mask & ~decided
        regime[m] = code
        decided |= m

    assign(low_coh | earthworks, REGIME_2)
    assign(high_rmse | vegetated | ~spectral_valid | ~kin_valid, REGIME_UNCLASSIFIED)
    assign(high_sai & accelerating & coupled, REGIME_4)
    flags[~decided & high_sai & accelerating & ~coupled] |= FLAG_ACCEL_NOT_COUPLED
    assign(high_sai, REGIME_3)
    assign(accelerating, REGIME_KINEMATIC_ONLY)
    assign(np.ones(sai.shape, dtype=bool), REGIME_1)
    return regime, flags


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def couple(
    spectral: xr.Dataset,
    kinematics: xr.Dataset,
    hydro: xr.Dataset,
    *,
    earthworks_mask: Optional[np.ndarray] = None,
) -> xr.Dataset:
    """Align the three layers on the hydrology grid, build the DAG, test coupling and classify."""
    if hydro.rio.crs is None:
        raise CouplingError("hydrology dataset has no CRS")
    master = hydro["elevation"].rio.write_crs(hydro.rio.crs)
    res = abs(float(master.rio.resolution()[0]))
    shape = master.shape

    # 1. Alignment ---------------------------------------------------------
    def spec(name, resampling=Resampling.bilinear, nodata=np.nan):
        return align(spectral[name].rio.write_crs(spectral.rio.crs), master, resampling, nodata)

    sai = spec("sai")
    rmse = spec("rmse")
    spectral_flags = spec("observability", Resampling.nearest, float(255))
    vegetated = spectral_flags == 1
    spectral_valid = np.isfinite(rmse) & ~np.isin(spectral_flags, (2, 255))

    kcrs = kinematics.rio.crs
    last = lambda name: kinematics[name].isel(time=-1).rio.write_crs(kcrs)  # noqa: E731
    near = Resampling.nearest
    velocity = align(last("velocity"), master, near, np.nan)
    v_sigma_gp = (align(last("velocity_upper"), master, near, np.nan) - align(last("velocity_lower"), master, near, np.nan)) / (2 * SIGMA)
    acceleration = align(last("acceleration"), master, near, np.nan)
    a_sigma = (align(last("acceleration_upper"), master, near, np.nan) - align(last("acceleration_lower"), master, near, np.nan)) / (2 * SIGMA)
    coherence = align(kinematics["temporal_coherence"].rio.write_crs(kcrs), master, near, np.nan)
    kin_status = align(kinematics["kinematic_status"].rio.write_crs(kcrs), master, near, float(kin.NO_DATA))
    days = align(as_days(kinematics["instability_days_after_last_epoch"]).rio.write_crs(kcrs), master, near, np.nan)
    days_2s = align(as_days(kinematics["instability_days_2sigma"]).rio.write_crs(kcrs), master, near, np.nan)
    kin_valid = np.isin(kin_status, (kin.STABLE, kin.LINEAR, kin.ACCEL_FORECAST, kin.ACCEL_NO_TREND))

    times = pd.to_datetime(kinematics["time"].values)
    span_years = float((times[-1] - times[0]).days) / kin.DAYS_PER_YEAR if times.size > 1 else 0.0
    v_sigma_phase = phase_noise_velocity_sigma(coherence, times.size, span_years)
    v_sigma = np.sqrt(v_sigma_gp**2 + np.nan_to_num(v_sigma_phase) ** 2)

    sai_factor = sai_sigma_factor(spectral)
    sai_sigma = rmse * sai_factor

    # 2. Topology ----------------------------------------------------------
    filled = hydro["elevation_filled"].values.astype(np.float64)
    flow, order = build_dag(filled, res)
    if not dag_is_acyclic(filled, flow):
        raise CouplingError("flow graph is not acyclic; the filled DEM is inconsistent")
    aspect = hydro["aspect_deg"].values.astype(np.float64)

    setback = latest_setback_state(hydro)
    if "water_class" in hydro:
        idx = setback.get("epoch_index", hydro.sizes["time"] - 1)
        water_class = hydro["water_class"].isel(time=idx).values
    else:
        water_class = np.zeros(shape, dtype=np.uint8)
    water = np.isin(water_class, hyd.WATER_CLASSES)
    boundary = water_boundary_cells(water_class)

    seepage = spectral_valid & ~vegetated & (sai > SAI_THRESHOLD)
    has_upslope_water = propagate_downslope(boundary, flow, order) & ~water
    has_downslope_seepage = strict_descendant_hit(propagate_upslope(seepage, flow, order), flow)
    has_donor = np.zeros(filled.size, dtype=bool)
    for r in (flow.receiver1.ravel(), flow.receiver2.ravel()):
        has_donor[r[r >= 0]] = True
    has_receiver = (flow.receiver1 >= 0) | (flow.receiver2 >= 0)
    mid_slope = has_donor.reshape(shape) & has_receiver & ~water

    moving = np.abs(velocity) >= VELOCITY_THRESHOLD_MM_YR
    accelerating = kin_valid & moving & (np.sign(velocity) * acceleration > SIGMA * a_sigma)
    anomaly = kin_valid & (((np.abs(velocity) > SIGMA * v_sigma) & moving) | accelerating)
    candidate = mid_slope & anomaly & has_upslope_water & has_downslope_seepage

    # Aspect-path validation (independent of D-infinity).
    water_paths = np.zeros(shape, dtype=bool)
    for r0, c0 in zip(*np.nonzero(boundary)):
        for r, c in aspect_path(aspect, (int(r0), int(c0))):
            water_paths[r, c] = True
    near_water_path = ndimage.binary_dilation(water_paths, structure=np.ones((3, 3)))
    near_seepage = ndimage.binary_dilation(seepage, structure=np.ones((3, 3)))
    aspect_validated = np.zeros(shape, dtype=bool)
    for r0, c0 in zip(*np.nonzero(candidate & near_water_path)):
        path = aspect_path(aspect, (int(r0), int(c0)))[1:]
        aspect_validated[r0, c0] = any(near_seepage[r, c] for r, c in path)
    coupled = candidate & aspect_validated & setback["encroaching"]

    # 3. Regimes -------------------------------------------------------------
    earthworks = np.zeros(shape, dtype=bool) if earthworks_mask is None else earthworks_mask
    regime, flags = classify(
        sai=sai, sai_sigma=sai_sigma, rmse=rmse, vegetated=vegetated, spectral_valid=spectral_valid,
        coherence=coherence, kin_status=kin_status, kin_valid=kin_valid, accelerating=accelerating,
        coupled=coupled, earthworks=earthworks,
    )
    tier = np.where(regime == REGIME_4, TIER3_CRITICAL, np.where(regime == REGIME_3, TIER1_WATCH, TIER_NONE)).astype(np.int8)
    low_confidence = (flags & (FLAG_LOW_COHERENCE | FLAG_HIGH_RMSE)) > 0

    last_epoch = pd.Timestamp(kinematics.attrs.get("last_epoch", str(times[-1])))
    r4 = regime == REGIME_4
    fail_days = np.where(r4, days, np.nan)
    fail_2s = np.where(r4, days_2s, np.nan)
    def to_time(d: np.ndarray) -> np.ndarray:
        out = np.full(d.shape, np.datetime64("NaT"), dtype="datetime64[ns]")
        # Weak inverse-velocity trends can extrapolate centuries ahead; beyond +-200 years the
        # nanosecond datetime overflows, so only the numeric days variables keep those values.
        ok = np.isfinite(d) & (np.abs(d) < 200 * 365.25)
        out[ok] = last_epoch.to_datetime64() + (d[ok] * 86400e9).astype("timedelta64[ns]")
        return out

    yx = ("y", "x")
    f32 = lambda a: a.astype(np.float32)  # noqa: E731
    ds = xr.Dataset(
        {
            "regime": (yx, regime, {"flag_values": [0, 1, 2, 3, 4, 5], "flag_meanings": REGIME_MEANINGS}),
            "alert_tier": (yx, tier, {"flag_values": [0, 1, 3], "flag_meanings": "none tier1-watch tier3-critical"}),
            "classification_flags": (yx, flags, {"flag_masks": [1, 2, 4, 8, 16, 32, 64, 128], "flag_meanings": FLAG_MEANINGS}),
            "low_confidence": (yx, low_confidence),
            "sai": (yx, f32(sai)),
            "sai_lower": (yx, f32(sai - SIGMA * sai_sigma)),
            "sai_upper": (yx, f32(sai + SIGMA * sai_sigma)),
            "spectral_rmse": (yx, f32(rmse)),
            "temporal_coherence": (yx, f32(coherence)),
            "velocity_mm_yr": (yx, f32(velocity)),
            "velocity_lower": (yx, f32(velocity - SIGMA * v_sigma), {"long_name": "GP + phase-noise 95% bound"}),
            "velocity_upper": (yx, f32(velocity + SIGMA * v_sigma)),
            "velocity_sigma_phase_noise": (yx, f32(v_sigma_phase)),
            "acceleration_mm_yr2": (yx, f32(acceleration)),
            "acceleration_lower": (yx, f32(acceleration - SIGMA * a_sigma)),
            "acceleration_upper": (yx, f32(acceleration + SIGMA * a_sigma)),
            "kinematic_status": (yx, kin_status.astype(np.int8)),
            "elevation": (yx, f32(hydro["elevation"].values)),
            "aspect_deg": (yx, f32(aspect)),
            "water_boundary": (yx, boundary),
            "upslope_water": (yx, has_upslope_water),
            "downslope_seepage": (yx, has_downslope_seepage),
            "mid_slope": (yx, mid_slope),
            "kinematic_anomaly": (yx, anomaly),
            "aspect_validated": (yx, aspect_validated),
            "coupled": (yx, coupled),
            "failure_days_after_last_epoch": (yx, f32(fail_days), {"units": "days", "long_name": "REGIME_4 trend-instability time after the last InSAR epoch"}),
            "failure_days_2sigma": (yx, f32(fail_2s), {"units": "days"}),
            "failure_time_estimate": (yx, to_time(fail_days)),
            "failure_time_lower": (yx, to_time(fail_days - fail_2s)),
            "failure_time_upper": (yx, to_time(fail_days + fail_2s)),
        },
        coords={"y": master["y"].values, "x": master["x"].values},
    )

    counts = {name: int((regime == code).sum()) for code, name in enumerate(REGIME_MEANINGS.split())}
    hazard = HAZARD_LEVELS[int(tier.max()) if tier.size else 0]
    provenance = {
        "spectral": spectral.attrs.get("source_origin", "unknown"),
        "kinematics": kinematics.attrs.get("source", "unknown"),
        "hydrology_dswx": hydro.attrs.get("dswx_source", "unknown"),
    }
    synthetic = any(str(v).lower() in {"mock", "synthetic"} for v in provenance.values())
    ds.attrs.update(
        {
            "facility_hazard_level": hazard,
            "last_insar_epoch": str(last_epoch),
            "regime_counts": json.dumps(counts),
            "setback_state": json.dumps(setback),
            "input_provenance": json.dumps(provenance),
            "contains_synthetic_or_mock_inputs": int(synthetic),
            "sai_threshold": SAI_THRESHOLD,
            "rmse_threshold": RMSE_THRESHOLD,
            "coherence_threshold": COHERENCE_THRESHOLD,
            "sai_sigma_factor": sai_factor,
            "grid": f"{shape[0]}x{shape[1]} @ {res} m, {master.rio.crs}",
            "disclaimer": DISCLAIMER,
        }
    )
    ds = ds.rio.write_crs(master.rio.crs)
    for name in ds.data_vars:
        ds[name].attrs["grid_mapping"] = "spatial_ref"
    return ds


def summarize(ds: xr.Dataset) -> dict[str, Any]:
    r4 = ds["regime"].values == REGIME_4
    summary: dict[str, Any] = {
        "facility_hazard_level": ds.attrs["facility_hazard_level"],
        "regime_counts": json.loads(ds.attrs["regime_counts"]),
        "low_confidence_cells": int(ds["low_confidence"].values.sum()),
        "coupled_cells": int(ds["coupled"].values.sum()),
        "setback_state": json.loads(ds.attrs["setback_state"]),
        "input_provenance": json.loads(ds.attrs["input_provenance"]),
        "contains_synthetic_or_mock_inputs": bool(ds.attrs["contains_synthetic_or_mock_inputs"]),
        "disclaimer": DISCLAIMER,
    }
    est = ds["failure_time_estimate"].values[r4]
    est = est[~np.isnat(est)]
    if est.size:
        summary["regime4_failure_time_earliest"] = str(pd.Timestamp(est.min()))
    return summary


def run_coupling(
    facility: str,
    spectral_path: Path,
    kinematics_path: Path,
    hydrology_path: Path,
    output_path: Path,
    *,
    earthworks_path: Optional[Path] = None,
) -> dict[str, Any]:
    import zarr

    spectral, kinematics, hydro = _open(spectral_path), _open(kinematics_path), _open(hydrology_path)
    if "y" not in kinematics.dims:
        raise CouplingError("kinematics input has no raster grid (point/CSV backtests cannot be coupled spatially)")
    earthworks = None
    if earthworks_path is not None:
        master = hydro["elevation"].rio.write_crs(hydro.rio.crs)
        geom = hyd.read_polygon_geometry(earthworks_path, str(master.rio.crs))
        earthworks = geometry_mask([geom], out_shape=master.shape, transform=master.rio.transform(), invert=True)
    ds = couple(spectral, kinematics, hydro, earthworks_mask=earthworks)
    ds.attrs["facility"] = facility
    ds.attrs["created_utc"] = pd.Timestamp.now(tz="UTC").isoformat()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    compressor = zarr.Blosc(cname="zstd", clevel=5, shuffle=zarr.Blosc.BITSHUFFLE)
    ds.to_zarr(output_path, mode="w", consolidated=True, encoding={n: {"compressor": compressor} for n in ds.data_vars})
    summary = {"facility": facility, "output": str(output_path), **summarize(ds)}
    logger.info("Coupling %s -> %s: hazard %s, regimes %s", facility, output_path, summary["facility_hazard_level"], summary["regime_counts"])
    return summary
