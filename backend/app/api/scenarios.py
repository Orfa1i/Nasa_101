"""Scenario routes: the Brumadinho historical backtest and the geotechnical stress-test simulator.

| Route | Purpose |
|-------|---------|
| ``GET /api/backtest/brumadinho`` | Phase 3 kinematics + Fukuzono fit on the historical point series in ``DATA_DIR/brumadinho_historic.csv``, issued before the 2019-01-25 collapse |
| ``GET /api/facilities/{id}/stress-test`` | SIMULATED pore-pressure seepage scenario on an Arizona facility: the cached products are perturbed in memory and re-run through the real Phase 3 and Phase 5 code |

Nothing here writes to the cache; every stress-test response is labelled simulated.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any, Optional

import numpy as np
import pandas as pd
import xarray as xr
from fastapi import APIRouter, Depends, HTTPException, Query

from app.api import endpoints as E
from app.api import layers as L
from app.api.fits import fukuzono_fit
from app.api.schemas import (
    BacktestOutcome,
    BacktestPoint,
    BacktestResponse,
    DataSource,
    KinematicSeries,
    Matrix,
    PointMatch,
    StressCell,
    StressParameters,
    StressSetback,
    StressTestResponse,
)
from app.config import Settings

logger = logging.getLogger("lithocreep.api")
router = APIRouter(prefix="/api")

# --------------------------------------------------------------------------- #
# Brumadinho backtest
# --------------------------------------------------------------------------- #

BRUMADINHO_COLLAPSE = pd.Timestamp("2019-01-25")
BACKTEST_FILE = "brumadinho_historic.csv"
BACKTEST_COLUMNS = ["date", "displacement_mm", "point_id", "temporal_coherence", "longitude", "latitude", "zone", "source"]
MAX_SWEEP = 8


def _zones(path) -> dict[str, str]:
    table = pd.read_csv(path)
    if "zone" not in table or "point_id" not in table:
        return {}
    return {str(k): str(v) for k, v in table.groupby(table["point_id"].astype(str))["zone"].first().dropna().items()}


def _analyse_point(series: xr.Dataset, pid: str, file: str) -> KinematicSeries:
    from app.pipelines import kinematics as kin

    one = series.sel(point=[pid])
    analysed = kin.kinematics_dataset(xr.Dataset({"displacement": one["displacement"], "temporal_coherence": one["temporal_coherence"]}))
    analysed.attrs.update(series.attrs)
    for v in ("longitude", "latitude"):
        if v in one:
            analysed[v] = one[v]
    lon = float(one["longitude"].values[0]) if "longitude" in one else 0.0
    lat = float(one["latitude"].values[0]) if "latitude" in one else 0.0
    return E._point_series(analysed, file, lat, lon, analysed=True)


def _outcome(k: KinematicSeries) -> BacktestOutcome:
    issued = pd.Timestamp(k.times[-1])
    out = BacktestOutcome(issued=issued.to_pydatetime(), lead_days=(BRUMADINHO_COLLAPSE - issued).total_seconds() / 86400.0, status=k.status)
    if k.instability_time is not None:
        est = pd.Timestamp(k.instability_time)
        out.estimate = k.instability_time
        out.lower, out.upper = k.instability_time_lower, k.instability_time_upper
        out.error_days = round((est - BRUMADINHO_COLLAPSE).total_seconds() / 86400.0, 2)
        if k.instability_time_lower is not None and k.instability_time_upper is not None:
            out.collapse_within_95 = bool(pd.Timestamp(k.instability_time_lower) <= BRUMADINHO_COLLAPSE <= pd.Timestamp(k.instability_time_upper))
    return out


@router.get("/backtest/brumadinho", response_model=BacktestResponse)
def brumadinho_backtest(
    point: Optional[str] = Query(None, description="point_id; default: the first toe point"),
    cutoff_days: int = Query(0, ge=0, le=365, description="issue the forecast this many days before the collapse"),
    window_days: int = Query(40, ge=7, le=365, description="pre-collapse window shown"),
    sweep: bool = Query(True, description="also re-issue the forecast at the epochs inside the window"),
    settings: Settings = Depends(E.get_app_settings),
) -> BacktestResponse:
    from app.pipelines import kinematics as kin

    path = settings.data_path / BACKTEST_FILE
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail={
                "message": f"{BACKTEST_FILE} not found in DATA_DIR. OPERA DISP-S1 does not cover South America, so the backtest needs the "
                "published pre-collapse InSAR point series (e.g. Grebby et al. 2021, doi:10.1038/s43247-020-00079-2). It is not bundled.",
                "expected_file": f"DATA_DIR/{BACKTEST_FILE}",
                "expected_columns": BACKTEST_COLUMNS,
                "required_columns": ["date", "displacement_mm"],
            },
        )
    try:
        ds = kin.load_historic_csv(path)
    except (kin.KinematicsError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"{BACKTEST_FILE} is malformed: {exc}") from exc
    ds = ds.sel(time=ds["time"] < BRUMADINHO_COLLAPSE.to_datetime64())
    zones = _zones(path)
    pids = [str(p) for p in ds["point"].values]
    if point is None:
        point = next((p for p in pids if zones.get(p, "").lower() == "toe"), None) or next((p for p in pids if "toe" in p.lower()), pids[0])
    if point not in pids:
        raise HTTPException(status_code=404, detail=f"point {point!r} not in {BACKTEST_FILE} (points: {pids})")

    cut = BRUMADINHO_COLLAPSE - pd.Timedelta(days=cutoff_days)
    upto = ds.sel(time=ds["time"] <= cut.to_datetime64())
    if upto.sizes["time"] < kin.FUKUZONO_POINTS + 3:
        raise HTTPException(status_code=422, detail=f"only {upto.sizes['time']} epochs before {cut.date()}: too few to analyse")
    series = _analyse_point(upto, point, path.name)
    outcome = _outcome(series)

    issues: list[BacktestOutcome] = []
    if sweep:
        times = pd.to_datetime(ds["time"].values)
        recent = [t for t in times if BRUMADINHO_COLLAPSE - pd.Timedelta(days=window_days) <= t][-MAX_SWEEP:]
        for t in recent:
            sub = ds.sel(time=ds["time"] <= t.to_datetime64())
            if sub.sizes["time"] >= kin.FUKUZONO_POINTS + 3:
                issues.append(_outcome(_analyse_point(sub, point, path.name)))

    citation = str(ds.attrs.get("source_citation", "not given in file"))
    historical = not series.source.synthetic_or_mock
    points = [
        BacktestPoint(
            point_id=p,
            zone=zones.get(p) or ("toe" if "toe" in p.lower() else None),
            longitude=L.json_scalar(ds["longitude"].sel(point=p).values) if "longitude" in ds else None,
            latitude=L.json_scalar(ds["latitude"].sel(point=p).values) if "latitude" in ds else None,
            temporal_coherence=L.json_scalar(ds["temporal_coherence"].sel(point=p).values),
        )
        for p in pids
    ]
    return BacktestResponse(
        collapse_date=BRUMADINHO_COLLAPSE.to_pydatetime(),
        historical=historical,
        dataset_label=f"HISTORICAL DATASET: {citation}" if historical else "SYNTHETIC STAND-IN: not the published Brumadinho series",
        source=series.source,
        citation=citation,
        points=points,
        point_id=point,
        window_days=window_days,
        window_start=(BRUMADINHO_COLLAPSE - pd.Timedelta(days=window_days)).to_pydatetime(),
        cutoff_days=cutoff_days,
        series=series,
        outcome=outcome,
        sweep=issues,
        disclaimer=kin.DISCLAIMER,
    )


# --------------------------------------------------------------------------- #
# Stress-test simulator
# --------------------------------------------------------------------------- #

ARIZONA_BOUNDS = (-114.82, 31.33, -109.04, 37.00)  # lon/lat extent of the state of Arizona
DELTA_SAI_MAX = 0.35
FOOTPRINT_RADIUS_M = 90.0
TAPER_SIGMA_M = 45.0
CREEP_ONSET_DAYS = 180.0
CREEP_A_YR_MM = 0.03
D_CREST_TARGET_M = 5.0


def in_arizona(row: dict[str, Any]) -> bool:
    x0, y0, x1, y1 = ARIZONA_BOUNDS
    return x0 <= row["bbox_xmin"] and row["bbox_xmax"] <= x1 and y0 <= row["bbox_ymin"] and row["bbox_ymax"] <= y1


def creep_displacement(times: pd.DatetimeIndex, intensity: float) -> tuple[np.ndarray, pd.Timestamp, float]:
    """Injected tertiary creep (mm, negative LOS) following 1/v = A (t_f - t) from 180 days before the last epoch.

    The lead time to the synthetic t_f shrinks from 180 to 30 days as the intensity goes 0 -> 1.
    """
    from app.pipelines import kinematics as kin

    last = times[-1]
    onset = last - pd.Timedelta(days=CREEP_ONSET_DAYS)
    lead = 30.0 + 150.0 * (1.0 - intensity)
    tau = ((times - onset).total_seconds() / 86400.0 / kin.DAYS_PER_YEAR).to_numpy()
    tf = (CREEP_ONSET_DAYS + lead) / kin.DAYS_PER_YEAR
    d = np.where(tau > 0, -(1.0 / CREEP_A_YR_MM) * np.log(tf / (tf - np.clip(tau, 0, None))), 0.0)
    return d, onset, lead


def _float_days(ds: xr.Dataset) -> xr.Dataset:
    from app.pipelines.coupling import as_days

    for name in list(ds.data_vars):
        if np.issubdtype(ds[name].dtype, np.timedelta64):
            ds[name] = as_days(ds[name]).astype(np.float32)
    return ds


def _taper(xs: np.ndarray, ys: np.ndarray, x: float, y: float) -> np.ndarray:
    X, Y = np.meshgrid(xs, ys)
    r = np.hypot(X - x, Y - y)
    return np.where(r <= FOOTPRINT_RADIUS_M, np.exp(-(r**2) / (2 * TAPER_SIGMA_M**2)), 0.0)


def _cell(coupled: xr.Dataset, x: float, y: float) -> StressCell:
    from app.pipelines import coupling as cp
    from app.pipelines import kinematics as kin

    loc = L.locate(coupled, x, y)
    if loc is None:
        return StressCell()
    at = lambda v: coupled[v].sel(x=loc.xn, y=loc.yn).values  # noqa: E731
    regime = int(at("regime"))
    status = int(at("kinematic_status"))
    meanings = kin.STATUS_MEANINGS.split()
    return StressCell(
        sai=L.json_scalar(at("sai")),
        regime=regime,
        regime_meaning=cp.REGIME_MEANINGS.split()[regime],
        alert_tier=int(at("alert_tier")),
        kinematic_status=meanings[status] if 0 <= status < len(meanings) else None,
        velocity_mm_yr=L.json_scalar(at("velocity_mm_yr")),
        failure_time_estimate=L.json_time(at("failure_time_estimate")),
    )


def _nearest_series(ds: xr.Dataset, xn: float, yn: float, source: DataSource) -> KinematicSeries:
    from app.pipelines import kinematics as kin

    cell = ds.sel(x=xn, y=yn)
    get = lambda v: L.json_list(cell[v].values) if v in cell else None  # noqa: E731
    code = int(cell["kinematic_status"].values)
    meanings = kin.STATUS_MEANINGS.split()
    to_time = lambda v: L.json_time(cell[v].values) if v in cell else None  # noqa: E731
    return KinematicSeries(
        source=source,
        match=PointMatch(method="nearest", nearest_cell_distance_m=None, note="scenario centre cell"),
        times=[pd.Timestamp(t).to_pydatetime() for t in ds["time"].values],
        displacement_mm=get("displacement"),
        displacement_smooth_mm=get("displacement_smooth"),
        displacement_smooth_lower_mm=get("displacement_smooth_lower"),
        displacement_smooth_upper_mm=get("displacement_smooth_upper"),
        velocity_mm_yr=get("velocity"),
        velocity_lower_mm_yr=get("velocity_lower"),
        velocity_upper_mm_yr=get("velocity_upper"),
        acceleration_mm_yr2=get("acceleration"),
        acceleration_lower_mm_yr2=get("acceleration_lower"),
        acceleration_upper_mm_yr2=get("acceleration_upper"),
        inverse_velocity_yr_mm=get("inverse_velocity"),
        temporal_coherence=L.json_scalar(cell["temporal_coherence"].values),
        status=meanings[code] if 0 <= code < len(meanings) else None,
        instability_time=to_time("instability_time"),
        instability_time_lower=to_time("instability_time_lower"),
        instability_time_upper=to_time("instability_time_upper"),
        disclaimer="SIMULATED stress-test series (synthetic creep injected into the cached DISP-S1 series). " + kin.DISCLAIMER,
        fukuzono=fukuzono_fit(cell),
    )


@router.get("/facilities/{facility_id}/stress-test", response_model=StressTestResponse)
def stress_test(
    facility_id: str,
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    intensity: float = Query(0.5, ge=0, le=1, description="hazard slider, 0 (baseline) to 1"),
    cur: Any = Depends(E.get_cursor),
    settings: Settings = Depends(E.get_app_settings),
) -> StressTestResponse:
    from app.pipelines import coupling as cp
    from app.pipelines import hydrology as hyd
    from app.pipelines import kinematics as kin
    from app.pipelines.ingestion import TARGET_CRS

    row = E.facility_or_404(cur, facility_id)
    if not in_arizona(row):
        raise HTTPException(status_code=422, detail=f"Stress tests are limited to Arizona (AZ) facilities; {row['name']} is outside Arizona")
    xmin, ymin, xmax, ymax = E.bbox_of(row)
    if not (xmin <= lon <= xmax and ymin <= lat <= ymax):
        raise HTTPException(status_code=422, detail=f"({lat}, {lon}) lies outside the {facility_id} bbox")
    prefix = row["cache_prefix"]
    try:
        spectral, _ = L.open_product(settings, prefix, "spectral")
        kinem, _ = L.open_product(settings, prefix, "kinematics")
        hydro, _ = L.open_product(settings, prefix, "hydrology")
        base, base_path = L.open_product(settings, prefix, "coupled")
    except (L.AssetMissing, L.AssetUnreadable) as exc:
        raise E._http_from(exc) from exc
    if "y" not in kinem.dims:
        raise HTTPException(status_code=422, detail="the kinematics product is not a raster")
    x, y = L.to_utm(lon, lat, TARGET_CRS)
    loc = L.locate(kinem, x, y)
    if loc is None:
        raise HTTPException(status_code=422, detail="point lies outside the kinematics grid")

    try:
        kd = _float_days(kinem.load().copy(deep=True))
        sd = spectral.drop_vars([v for v in ("reflectance",) if v in spectral]).load().copy(deep=True)
        hd = hydro.load().copy(deep=True)
        times = pd.to_datetime(kd["time"].values)

        # 1. Creep: inject tertiary creep into the 3 x 3 cells around the pick and re-run Phase 3 there.
        inj, onset, lead = creep_displacement(times, intensity)
        ix = int(np.argmin(np.abs(kd["x"].values - loc.xn)))
        iy = int(np.argmin(np.abs(kd["y"].values - loc.yn)))
        xs = slice(max(ix - 1, 0), min(ix + 2, kd.sizes["x"]))
        ys = slice(max(iy - 1, 0), min(iy + 2, kd.sizes["y"]))
        win = kd.isel(x=xs, y=ys)
        w = _taper(win["x"].values, win["y"].values, loc.xn, loc.yn)
        baseline_disp = kd["displacement"].sel(x=loc.xn, y=loc.yn).values.copy()
        if intensity > 0:
            disp = win["displacement"].transpose("time", "y", "x") + intensity * inj[:, None, None] * w[None, :, :]
            refit = kin.kinematics_dataset(xr.Dataset({"displacement": disp, "temporal_coherence": win["temporal_coherence"]}), hyperparameters="hybrid")
            for name in refit.data_vars:
                if name in kd and set(refit[name].dims) <= set(kd[name].dims):
                    kd[name][{"x": xs, "y": ys}] = refit[name].transpose(*kd[name].dims).values

        # 2. Seepage: raise SAI around the pick (where SAI is defined; vegetated cells stay NaN).
        delta = DELTA_SAI_MAX * intensity
        ws = _taper(sd["x"].values, sd["y"].values, x, y)
        sd["sai"] = sd["sai"] + (delta * ws).astype(np.float32)

        # 3. Setback: ramp the valid D_crest epochs towards D_CREST_TARGET_M and re-derive the alerts.
        dists = hd["d_crest_m"].values.astype(np.float64).copy() if "d_crest_m" in hd else np.array([])
        statuses = hd["d_crest_status"].values if "d_crest_status" in hd else np.array([])
        scen = dists.copy()
        valid = np.flatnonzero(statuses == hyd.SETBACK_VALID) if dists.size else np.array([], dtype=int)
        if valid.size:
            d_last = dists[valid[-1]]
            for k, i in enumerate(valid):
                frac = 1.0 if valid.size == 1 else k / (valid.size - 1)
                scen[i] = max(dists[i] - intensity * frac * max(d_last - D_CREST_TARGET_M, 0.0), 0.0)
            alerts = hyd.setback_alerts(scen, statuses)
            hd["d_crest_m"] = ("time", scen.astype(hd["d_crest_m"].dtype))
            hd["setback_alert_flags"] = ("time", alerts["alert_flags"])

        # 4. Phase 5 on the perturbed products.
        scenario = cp.couple(sd, kd, hd)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Stress test %s failed", facility_id)
        raise HTTPException(status_code=503, detail=f"stress test could not run on the cached products: {type(exc).__name__}: {exc}") from exc

    alert_names = {hyd.ALERT_SHRINK_BASELINE: "shrink_vs_baseline", hyd.ALERT_SHRINK_PREVIOUS: "shrink_vs_previous", hyd.ALERT_PROXIMITY: "crest_proximity"}
    flags = hd["setback_alert_flags"].values if "setback_alert_flags" in hd else np.zeros(scen.size, dtype=int)
    counts = lambda ds: json.loads(ds.attrs["regime_counts"]) if "regime_counts" in ds.attrs else {}  # noqa: E731
    sim_source = DataSource(product="stress_test", file=base_path.name, variable="regime", synthetic_or_mock=True, label="SIMULATED STRESS TEST (test overlay)")
    values, res = L.geographic_matrix(scenario["regime"].rio.write_crs(hd.rio.crs), (xmin, ymin, xmax, ymax), categorical=True)
    regime_matrix = Matrix(
        facility_id=facility_id, layer="stress_test_regime", units="regime code", categorical=True, bounds=[xmin, ymin, xmax, ymax],
        shape=list(values.shape), resolution_deg=res, values=L.json_grid(values), valid_fraction=float(np.isfinite(values).mean()),
        flag_meanings=cp.REGIME_MEANINGS, source=sim_source, note="SIMULATED: scenario regimes after the synthetic perturbation",
    )
    from pyproj import Transformer

    to_ll = Transformer.from_crs(TARGET_CRS, "EPSG:4326", always_xy=True)
    ring = [list(to_ll.transform(x + FOOTPRINT_RADIUS_M * math.cos(a), y + FOOTPRINT_RADIUS_M * math.sin(a))) for a in np.linspace(0, 2 * math.pi, 33)]
    return StressTestResponse(
        facility_id=facility_id,
        lat=lat,
        lon=lon,
        parameters=StressParameters(
            intensity=intensity, delta_sai_max=round(delta, 4), footprint_radius_m=FOOTPRINT_RADIUS_M, creep_onset=onset.to_pydatetime(),
            creep_lead_days=lead, creep_A_yr_mm=CREEP_A_YR_MM, d_crest_target_m=D_CREST_TARGET_M,
        ),
        baseline=_cell(base, x, y),
        scenario=_cell(scenario, x, y),
        times=[t.to_pydatetime() for t in times],
        baseline_displacement_mm=L.json_list(baseline_disp),
        scenario_displacement_mm=L.json_list(kd["displacement"].sel(x=loc.xn, y=loc.yn).values),
        scenario_series=_nearest_series(kd, loc.xn, loc.yn, sim_source),
        setback=StressSetback(
            dates=[pd.Timestamp(t).to_pydatetime() for t in hd["time"].values] if dists.size else [],
            baseline_d_crest_m=L.json_list(dists),
            scenario_d_crest_m=L.json_list(scen),
            scenario_alerts=[[name for bit, name in alert_names.items() if int(f) & bit] for f in flags] if dists.size else [],
        ),
        baseline_hazard_level=str(base.attrs.get("facility_hazard_level", "UNKNOWN")),
        scenario_hazard_level=str(scenario.attrs.get("facility_hazard_level", "UNKNOWN")),
        baseline_regime_counts=counts(base),
        scenario_regime_counts=counts(scenario),
        footprint=ring,
        regime_matrix=regime_matrix,
    )
