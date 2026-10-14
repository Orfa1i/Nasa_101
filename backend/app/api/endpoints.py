"""FastAPI routes for LithoCreep.

| Route | Returns |
|-------|---------|
| ``GET /api/health`` | database status |
| ``GET /api/facilities`` | managed facilities (``Facility``) |
| ``GET /api/facilities/{id}`` | one facility |
| ``GET /api/facilities/{id}/alerts`` | alerts derived from the Phase 5 coupled products |
| ``GET /api/facilities/{id}/layers`` | which matrix layers exist, with their sources and epochs |
| ``GET /api/facilities/{id}/matrices?layer=&t=`` | one layer, reprojected to a ~30 m EPSG:4326 grid whose bounds are exactly the facility bbox |
| ``GET /api/facilities/{id}/pixel?lat=&lon=`` | time series and values at a point (bilinear between cells, nearest for categories) |
| ``GET /api/facilities/{id}/dashboard`` | the unified dashboard validation query (all inspection tools plus consistency checks) |
| ``POST /api/facilities/{id}/audit`` | a validated GISTM screening brief, from the Claude copilot or the deterministic template |
| ``GET /api/audit/status`` | which audit modes are usable (Claude credentials present or not) |
| ``GET /api/backtest/brumadinho``, ``GET /api/facilities/{id}/stress-test`` | scenarios (``app.api.scenarios``) |

Errors:
* 404: unknown facility, or a missing product or layer.
* 422: an invalid parameter or a point outside the bbox.
* 503: product files exist but cannot be read; readable fallback sources are tried first.

The database is rebuilt from the registry and the cached products on start-up.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from enum import Enum
from typing import Any, Iterator, Optional

import numpy as np
import pandas as pd
import xarray as xr
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api import layers as L
from app.api.fits import fukuzono_fit as _fukuzono_fit
from app.api.schemas import (
    Alert,
    AuditRequest,
    AuditResponse,
    ClassificationPoint,
    DataSource,
    Facility,
    Health,
    HydrologyPoint,
    KinematicSeries,
    LayerInfo,
    Matrix,
    PixelResponse,
    PointMatch,
    SpectralPoint,
    Spectrum,
    TimeValue,
)
from app.config import Settings, get_settings
from app.database import Database, fetch_alerts, fetch_facilities

logger = logging.getLogger("lithocreep.api")

LayerName = Enum("LayerName", {name: name for name in L.LAYERS}, type=str)  # type: ignore[misc]

router = APIRouter(prefix="/api")


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #


def get_cursor(request: Request) -> Iterator[Any]:
    cur = request.app.state.db.cursor()
    try:
        yield cur
    finally:
        cur.close()


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def facility_or_404(cur: Any, facility_id: str) -> dict[str, Any]:
    rows = fetch_facilities(cur, facility_id)
    if not rows:
        raise HTTPException(status_code=404, detail=f"Unknown facility {facility_id!r}")
    return rows[0]


def to_facility(row: dict[str, Any]) -> Facility:
    crest = row.get("crest_line_geom")
    data = {k: row[k] for k in Facility.model_fields if k in row}
    data["crest_line_geom"] = json.loads(crest) if isinstance(crest, str) and crest.strip() else None
    return Facility(**data)


def bbox_of(row: dict[str, Any]) -> tuple[float, float, float, float]:
    return row["bbox_xmin"], row["bbox_ymin"], row["bbox_xmax"], row["bbox_ymax"]


def _http_from(exc: Exception) -> HTTPException:
    if isinstance(exc, L.AssetMissing):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=503, detail=f"Cached product unreadable: {exc}")


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get("/health", response_model=Health)
def health(request: Request, cur: Any = Depends(get_cursor)) -> Health:
    n_fac = cur.execute("SELECT count(*) FROM facilities").fetchone()[0]
    n_alert = cur.execute("SELECT count(*) FROM alerts").fetchone()[0]
    return Health(status="ok", database=str(request.app.state.db.path), facilities=n_fac, alerts=n_alert)


@router.get("/facilities", response_model=list[Facility])
def list_facilities(cur: Any = Depends(get_cursor)) -> list[Facility]:
    return [to_facility(r) for r in fetch_facilities(cur)]


@router.get("/facilities/{facility_id}", response_model=Facility)
def get_facility(facility_id: str, cur: Any = Depends(get_cursor)) -> Facility:
    return to_facility(facility_or_404(cur, facility_id))


@router.get("/facilities/{facility_id}/alerts", response_model=list[Alert])
def facility_alerts(facility_id: str, cur: Any = Depends(get_cursor)) -> list[Alert]:
    facility_or_404(cur, facility_id)
    return [Alert(**a) for a in fetch_alerts(cur, facility_id)]


@router.get("/facilities/{facility_id}/layers", response_model=list[LayerInfo])
def facility_layers(facility_id: str, cur: Any = Depends(get_cursor), settings: Settings = Depends(get_app_settings)) -> list[LayerInfo]:
    row = facility_or_404(cur, facility_id)
    out = []
    for name, spec in L.LAYERS.items():
        info = LayerInfo(layer=name, units=spec.units, categorical=spec.categorical, time_dependent=spec.time_dependent, available=False)
        try:
            resolved = L.resolve_layer(settings, row["cache_prefix"], name)
            info.available = True
            info.source = DataSource(**resolved.source.as_dict())
            info.times = L.layer_times(resolved.data)
            info.detail = resolved.note
        except (L.AssetMissing, L.AssetUnreadable) as exc:
            info.detail = str(exc)
        out.append(info)
    return out


@router.get("/facilities/{facility_id}/matrices", response_model=Matrix)
def facility_matrix(
    facility_id: str,
    layer: LayerName = Query(..., description="Layer to return"),
    t: Optional[datetime] = Query(None, description="ISO timestamp; the nearest epoch is used (latest if omitted)"),
    cur: Any = Depends(get_cursor),
    settings: Settings = Depends(get_app_settings),
) -> Matrix:
    row = facility_or_404(cur, facility_id)
    name = layer.value
    spec = L.LAYERS[name]
    try:
        resolved = L.resolve_layer(settings, row["cache_prefix"], name)
        da = resolved.data
        times = L.layer_times(da)
        da, epoch = L.select_time(da, t)
        mask_note = None
        if spec.derived == "distance_to_water":
            da = L.distance_to_water(da)
        elif spec.derived == "observability_mask":
            da, mask_note = L.observability_mask(da, resolved.source.variable)
        values, res = L.geographic_matrix(da, bbox_of(row), categorical=spec.categorical, circular=spec.circular)
    except (L.AssetMissing, L.AssetUnreadable) as exc:
        raise _http_from(exc) from exc
    except Exception as exc:
        logger.exception("Matrix %s/%s failed", facility_id, name)
        raise HTTPException(status_code=503, detail=f"Cached product unreadable: {type(exc).__name__}: {exc}") from exc

    notes = [n for n in (resolved.note, mask_note) if n]
    if spec.derived == "distance_to_water":
        notes.append("Euclidean distance to the nearest DSWx water cell (WTR 1/2), not a flow-path distance")
    if t is not None and not spec.time_dependent:
        notes.append("layer is static; t ignored")
    flag_meanings = resolved.data.attrs.get("flag_meanings") if spec.categorical else None
    if spec.derived == "observability_mask":
        flag_meanings = L.MASK_MEANINGS
    return Matrix(
        facility_id=facility_id,
        layer=name,
        units=spec.units,
        categorical=spec.categorical,
        bounds=list(bbox_of(row)),
        shape=list(values.shape),
        resolution_deg=res,
        time=epoch,
        requested_time=t,
        available_times=times,
        values=L.json_grid(values),
        valid_fraction=float(np.isfinite(values).mean()),
        flag_meanings=flag_meanings,
        source=DataSource(**resolved.source.as_dict()),
        note="; ".join(notes) or None,
    )


@router.get("/facilities/{facility_id}/pixel", response_model=PixelResponse)
def facility_pixel(
    facility_id: str,
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    cur: Any = Depends(get_cursor),
    settings: Settings = Depends(get_app_settings),
) -> PixelResponse:
    from app.pipelines.ingestion import TARGET_CRS, bbox_within_conus

    row = facility_or_404(cur, facility_id)
    xmin, ymin, xmax, ymax = bbox_of(row)
    if not (xmin <= lon <= xmax and ymin <= lat <= ymax):
        raise HTTPException(status_code=422, detail=f"({lat}, {lon}) lies outside the {facility_id} bbox {bbox_of(row)}")
    prefix = row["cache_prefix"]
    utm_x, utm_y = L.to_utm(lon, lat, TARGET_CRS)
    resp = PixelResponse(facility_id=facility_id, lat=lat, lon=lon, utm_x=utm_x, utm_y=utm_y, utm_crs=TARGET_CRS)

    historic = not bbox_within_conus(bbox_of(row))
    sections = {
        "kinematics": lambda: _historic_kinematics(settings, row, lat, lon) if historic else _raster_kinematics(settings, prefix, utm_x, utm_y),
        "spectral": lambda: _spectral_point(settings, prefix, utm_x, utm_y),
        "hydrology": lambda: _hydrology_point(settings, prefix, utm_x, utm_y),
        "classification": lambda: _classification_point(settings, prefix, utm_x, utm_y),
    }
    for name, build in sections.items():
        try:
            setattr(resp, name, build())
        except L.AssetMissing as exc:
            resp.unavailable[name] = str(exc)
        except L.AssetUnreadable as exc:
            logger.warning("Pixel %s/%s: %s", facility_id, name, exc)
            resp.unavailable[name] = f"unreadable: {exc}"
        except Exception as exc:
            logger.exception("Pixel %s/%s failed", facility_id, name)
            resp.unavailable[name] = f"unreadable: {type(exc).__name__}: {exc}"
    if all(getattr(resp, n) is None for n in sections):
        raise HTTPException(status_code=404, detail={"message": f"No data for {facility_id} at this point", "unavailable": resp.unavailable})
    return resp


@router.get("/facilities/{facility_id}/dashboard", response_model=dict[str, Any])
def facility_dashboard(request: Request, facility_id: str, cur: Any = Depends(get_cursor), settings: Settings = Depends(get_app_settings)) -> dict[str, Any]:
    from app.agent.tools import InspectionTools, ToolError

    facility_or_404(cur, facility_id)
    try:
        return InspectionTools(settings, request.app.state.db).dashboard_validation_query(facility_id)
    except ToolError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/audit/status")
def audit_status(request: Request, settings: Settings = Depends(get_app_settings)) -> dict[str, Any]:
    """Which audit modes are usable: the Claude copilot needs credentials (or an injected client)."""
    from app.agent import copilot

    llm = copilot.credentials_available() or getattr(request.app.state, "llm_client", None) is not None
    return {
        "llm_available": llm,
        "model": settings.LLM_MODEL,
        "effort": settings.LLM_EFFORT,
        "auto_mode_uses": "llm" if llm else "deterministic",
        "modes": ["auto", "llm", "deterministic"] if llm else ["auto", "deterministic"],
        "standard": "GISTM (Global Industry Standard on Tailings Management, 2020; co-convened by ICMM, UNEP and PRI)",
    }


@router.post("/facilities/{facility_id}/audit", response_model=AuditResponse)
def facility_audit(
    request: Request,
    facility_id: str,
    body: AuditRequest = AuditRequest(),
    cur: Any = Depends(get_cursor),
    settings: Settings = Depends(get_app_settings),
) -> AuditResponse:
    import anthropic

    from app.agent import copilot
    from app.agent.tools import InspectionTools, ToolError

    facility_or_404(cur, facility_id)
    if body.mode == "llm" and not copilot.credentials_available() and getattr(request.app.state, "llm_client", None) is None:
        raise HTTPException(status_code=503, detail="mode=llm needs Anthropic credentials (ANTHROPIC_API_KEY or `ant auth login`)")
    tools = InspectionTools(settings, request.app.state.db)
    try:
        result = copilot.generate_audit(
            tools, facility_id, query=body.query, mode=body.mode, client=getattr(request.app.state, "llm_client", None),
            model=settings.LLM_MODEL, effort=settings.LLM_EFFORT,
        )
    except copilot.BriefValidationError as exc:
        raise HTTPException(status_code=502, detail={"message": "brief failed grounding validation", "discrepancies": exc.discrepancies}) from exc
    except copilot.AuditRefusedError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except copilot.AuditError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ToolError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except anthropic.RateLimitError as exc:
        raise HTTPException(status_code=503, detail=f"Claude API rate limited: {exc.message}") from exc
    except anthropic.APIStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Claude API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise HTTPException(status_code=502, detail="Claude API unreachable") from exc
    return AuditResponse(facility_id=facility_id, **result.as_dict())


# --------------------------------------------------------------------------- #
# Pixel helpers
# --------------------------------------------------------------------------- #


def _open_at(settings: Settings, prefix: str, product: str, x: float, y: float) -> tuple[xr.Dataset, Any, DataSource]:
    ds, path = L.open_product(settings, prefix, product)
    if not {"x", "y"} <= set(ds.dims):
        raise L.AssetMissing(f"{path.name} is not a raster product")
    loc = L.locate(ds, x, y)
    if loc is None:
        raise L.AssetMissing(f"point lies outside the {path.name} grid")
    return ds, loc, DataSource(**L.describe_source(product, path, "*", ds).as_dict())


def _match(loc: L.PointLocator, method: str, note: Optional[str] = None) -> PointMatch:
    return PointMatch(method=method, nearest_cell_distance_m=round(loc.distance_m, 3), note=note)


def _series(ds: xr.Dataset, name: str, loc: L.PointLocator) -> Optional[list[Optional[float]]]:
    if name not in ds:
        return None
    values, _ = L.sample(ds[name], loc, interpolate=True)
    return L.json_list(values)


def _raster_kinematics(settings: Settings, prefix: str, x: float, y: float) -> KinematicSeries:
    from app.pipelines import kinematics as kin

    ds, loc, source = _open_at(settings, prefix, "kinematics", x, y)
    _, method = L.sample(ds["displacement"], loc, interpolate=True)
    status_code = int(ds["kinematic_status"].sel(x=loc.xn, y=loc.yn).values)
    meanings = kin.STATUS_MEANINGS.split()
    near = lambda v: ds[v].sel(x=loc.xn, y=loc.yn).values if v in ds else None  # noqa: E731
    return KinematicSeries(
        source=source,
        match=_match(loc, method, None if loc.exact else "series: bilinear between the 4 nearest cells; status and timeline: nearest cell"),
        times=[pd.Timestamp(t).to_pydatetime() for t in ds["time"].values],
        displacement_mm=_series(ds, "displacement", loc),
        displacement_smooth_mm=_series(ds, "displacement_smooth", loc),
        displacement_smooth_lower_mm=_series(ds, "displacement_smooth_lower", loc),
        displacement_smooth_upper_mm=_series(ds, "displacement_smooth_upper", loc),
        velocity_mm_yr=_series(ds, "velocity", loc),
        velocity_lower_mm_yr=_series(ds, "velocity_lower", loc),
        velocity_upper_mm_yr=_series(ds, "velocity_upper", loc),
        acceleration_mm_yr2=_series(ds, "acceleration", loc),
        acceleration_lower_mm_yr2=_series(ds, "acceleration_lower", loc),
        acceleration_upper_mm_yr2=_series(ds, "acceleration_upper", loc),
        inverse_velocity_yr_mm=_series(ds, "inverse_velocity", loc),
        temporal_coherence=L.json_scalar(near("temporal_coherence")),
        status=meanings[status_code] if 0 <= status_code < len(meanings) else None,
        instability_time=L.json_time(near("instability_time")),
        instability_time_lower=L.json_time(near("instability_time_lower")),
        instability_time_upper=L.json_time(near("instability_time_upper")),
        disclaimer=ds.attrs.get("disclaimer"),
        fukuzono=_fukuzono_fit(ds.sel(x=loc.xn, y=loc.yn)),
    )


def _historic_kinematics(settings: Settings, row: dict[str, Any], lat: float, lon: float) -> KinematicSeries:
    """Non-CONUS sites (e.g. Brumadinho): no DISP-S1, so read the historical point series instead."""
    from app.pipelines import kinematics as kin

    prefix = row["cache_prefix"]
    analysed = L.product_path(settings, prefix, "kinematics")
    if analysed.exists():
        try:
            ds = xr.open_zarr(analysed)
            if "point" in ds.dims:
                return _point_series(ds, analysed.name, lat, lon, analysed=True)
        except Exception as exc:  # unreadable cache: fall back to the raw CSV
            logger.warning("%s unreadable (%s); falling back to the historical CSV", analysed.name, exc)
    csv = settings.data_path / f"{row['id']}_historic.csv"
    if not csv.exists():
        raise L.AssetMissing(
            f"{csv.name} not found: {row['name']} is outside OPERA DISP-S1 coverage and its historical "
            "displacement series has not been supplied"
        )
    ds = kin.load_historic_csv(csv)
    return _point_series(ds, csv.name, lat, lon, analysed=False)


def _point_series(ds: xr.Dataset, file: str, lat: float, lon: float, *, analysed: bool) -> KinematicSeries:
    from app.pipelines import kinematics as kin

    points = ds["point"].values
    if "longitude" in ds and "latitude" in ds:
        dist = L.haversine_m(lon, lat, ds["longitude"].values, ds["latitude"].values)
        i = int(np.nanargmin(dist))
        match = PointMatch(method="nearest", nearest_cell_distance_m=round(float(dist[i]), 1), note="nearest historical monitoring point")
    else:
        i = 0
        match = PointMatch(method="nearest", nearest_cell_distance_m=None, note="series has no coordinates; first point returned")
    p = ds.isel(point=i)
    citation = str(ds.attrs.get("source_citation", ""))
    source = DataSource(
        product="kinematics" if analysed else "historical_csv",
        file=file,
        variable="displacement",
        synthetic_or_mock="synthetic" in citation.lower(),
        label=f"{ds.attrs.get('source', 'historical backup')}; {citation}".strip("; "),
    )
    get = lambda v: L.json_list(p[v].values) if v in p else None  # noqa: E731
    status = None
    if "kinematic_status" in p:
        code = int(p["kinematic_status"].values)
        meanings = kin.STATUS_MEANINGS.split()
        status = meanings[code] if 0 <= code < len(meanings) else None
    return KinematicSeries(
        source=source,
        match=match,
        point_id=str(points[i]),
        times=[pd.Timestamp(t).to_pydatetime() for t in ds["time"].values],
        displacement_mm=L.json_list(p["displacement"].values),
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
        temporal_coherence=L.json_scalar(p["temporal_coherence"].values) if "temporal_coherence" in p else None,
        status=status,
        instability_time=L.json_time(p["instability_time"].values) if "instability_time" in p else None,
        instability_time_lower=L.json_time(p["instability_time_lower"].values) if "instability_time_lower" in p else None,
        instability_time_upper=L.json_time(p["instability_time_upper"].values) if "instability_time_upper" in p else None,
        disclaimer=ds.attrs.get("disclaimer") or kin.DISCLAIMER,
        fukuzono=_fukuzono_fit(p) if analysed else None,
    )


def _spectral_point(settings: Settings, prefix: str, x: float, y: float) -> SpectralPoint:
    from app.pipelines import unmixing

    ds, loc, source = _open_at(settings, prefix, "spectral", x, y)
    val = lambda v: L.json_scalar(L.sample(ds[v], loc, interpolate=True)[0]) if v in ds else None  # noqa: E731
    obs = int(ds["observability"].sel(x=loc.xn, y=loc.yn).values) if "observability" in ds else None
    meanings = unmixing.FLAG_MEANINGS.split()
    abundance = {}
    if "abundance" in ds:
        values, _ = L.sample(ds["abundance"], loc, interpolate=True)
        abundance = dict(zip([str(e) for e in ds["endmember"].values], L.json_list(values)))
    spectrum, why = _spectrum(ds, loc)
    return SpectralPoint(
        source=source,
        match=_match(loc, "exact" if loc.exact else "bilinear", None if loc.exact else "continuous values bilinear; observability nearest"),
        sai=val("sai"), rmse=val("rmse"), ndvi=val("ndvi"), ndwi_swir=val("ndwi_swir"), confidence=val("confidence"),
        observability=meanings[obs] if obs is not None and 0 <= obs < len(meanings) else None,
        abundance=abundance,
        spectrum=spectrum,
        spectrum_unavailable=why,
    )


def _spectrum(ds: xr.Dataset, loc: L.PointLocator) -> tuple[Optional[Spectrum], Optional[str]]:
    """Measured spectrum of the nearest cell and its NNLS reconstruction M @ f (same library as the run)."""
    from app.pipelines import unmixing

    if "reflectance" not in ds or "wavelength" not in ds.coords:
        return None, "this spectral product has no reflectance cube (re-run `unmix` to store it)"
    refl = ds["reflectance"].sel(x=loc.xn, y=loc.yn).values.astype(np.float64)
    if not np.isfinite(refl).any():
        return None, "no reflectance at this cell"
    wl = ds["wavelength"].values.astype(np.float64)
    active = ds["band_active"].values.astype(bool) if "band_active" in ds.coords else unmixing.active_band_mask(wl)
    names = [str(e) for e in ds["endmember"].values]
    f = ds["abundance"].sel(x=loc.xn, y=loc.yn).values.astype(np.float64)
    modelled = np.full(wl.shape, np.nan)
    titles: dict[str, str] = {}
    try:
        lib_file = ds.attrs.get("endmember_library_file")
        library = unmixing.load_spectral_library(unmixing.REFERENCE_DIR / lib_file if lib_file else None, endmembers=names)
        titles = dict(library.titles)
        if np.isfinite(f).all():
            modelled[active] = unmixing.build_endmember_matrix(wl[active], library) @ f
    except (FileNotFoundError, unmixing.SpectralError) as exc:
        logger.warning("Spectrum model unavailable: %s", exc)
    windows = json.loads(ds.attrs.get("water_vapor_windows_nm", json.dumps(unmixing.WATER_VAPOR_WINDOWS_NM)))
    near = lambda v: L.json_scalar(ds[v].sel(x=loc.xn, y=loc.yn).values) if v in ds else None  # noqa: E731
    return (
        Spectrum(
            match=_match(loc, "exact" if loc.exact else "nearest", None if loc.exact else "spectrum, fit and fractions of the nearest cell"),
            wavelengths_nm=[round(float(w), 4) for w in wl],
            reflectance=L.json_list(np.round(refl, 6)),
            active=[bool(a) for a in active],
            modelled=L.json_list(np.round(modelled, 6)),
            excluded_windows_nm=[[float(a), float(b)] for a, b in windows],
            abundance=dict(zip(names, L.json_list(f))),
            rmse=near("rmse"),
            sai=near("sai"),
            endmember_titles=titles,
        ),
        None,
    )


def _hydrology_point(settings: Settings, prefix: str, x: float, y: float) -> HydrologyPoint:
    ds, loc, source = _open_at(settings, prefix, "hydrology", x, y)
    lin = lambda v: L.json_scalar(L.sample(ds[v], loc, interpolate=True)[0]) if v in ds else None  # noqa: E731
    near = lambda v: L.json_scalar(ds[v].sel(x=loc.xn, y=loc.yn).values) if v in ds else None  # noqa: E731
    water = None
    if "water_class" in ds:
        water = TimeValue(
            times=[pd.Timestamp(t).to_pydatetime() for t in ds["time"].values],
            values=L.json_list(ds["water_class"].sel(x=loc.xn, y=loc.yn).values),
        )
    d_crest = None
    if "d_crest_m" in ds:
        d_crest = TimeValue(times=[pd.Timestamp(t).to_pydatetime() for t in ds["time"].values], values=L.json_list(ds["d_crest_m"].values))
    stream = bool(ds["stream_network"].sel(x=loc.xn, y=loc.yn).values) if "stream_network" in ds else None
    return HydrologyPoint(
        source=source,
        match=_match(loc, "exact" if loc.exact else "bilinear", None if loc.exact else "elevation/slope/accumulation bilinear; aspect, flow direction, classes nearest"),
        elevation_m=lin("elevation"), slope_deg=lin("slope_deg"), aspect_deg=near("aspect_deg"),
        flow_direction_deg=near("flow_direction_deg"), flow_accumulation_cells=lin("flow_accumulation_cells"),
        stream=stream, water_class=water, d_crest_m=d_crest,
    )


def _classification_point(settings: Settings, prefix: str, x: float, y: float) -> ClassificationPoint:
    from app.pipelines import coupling as cp

    ds, loc, source = _open_at(settings, prefix, "coupled", x, y)
    at = lambda v: ds[v].sel(x=loc.xn, y=loc.yn).values  # noqa: E731
    regime = int(at("regime"))
    flag_bits = int(at("classification_flags"))
    flag_names = cp.FLAG_MEANINGS.split()
    return ClassificationPoint(
        source=source,
        match=_match(loc, "exact" if loc.exact else "nearest", None if loc.exact else "classes are never interpolated: nearest cell"),
        regime=regime,
        regime_meaning=cp.REGIME_MEANINGS.split()[regime],
        alert_tier=int(at("alert_tier")),
        flags=[name for bit, name in enumerate(flag_names) if flag_bits & (1 << bit)],
        low_confidence=bool(at("low_confidence")),
        sai_interval=[L.json_scalar(at("sai_lower")), L.json_scalar(at("sai_upper"))],
        velocity_interval_mm_yr=[L.json_scalar(at("velocity_lower")), L.json_scalar(at("velocity_upper"))],
        failure_time_estimate=L.json_time(at("failure_time_estimate")),
        failure_time_lower=L.json_time(at("failure_time_lower")),
        failure_time_upper=L.json_time(at("failure_time_upper")),
    )


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #


def create_app(settings: Optional[Settings] = None, *, llm_client: Any = None) -> FastAPI:
    """Build the API. Settings default to the environment (``get_settings()``), read at start-up.

    ``llm_client`` replaces the default ``anthropic.Anthropic()`` used by the audit copilot
    (e.g. a configured client with a proxy, or a scripted client in offline tests).
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = settings or get_settings()
        db = Database(cfg.data_path / "lithocreep.duckdb")
        counts = db.initialize(cfg)
        app.state.settings, app.state.db, app.state.llm_client = cfg, db, llm_client
        logger.info("API ready: %s", counts)
        try:
            yield
        finally:
            db.close()

    # CORS is configured before start-up; read it without building Settings (which creates DATA_DIR).
    origins = settings.cors_origins if settings else [
        o.strip() for o in os.environ.get("CORS_ORIGINS", Settings.model_fields["CORS_ORIGINS"].default).split(",") if o.strip()
    ]
    app = FastAPI(
        title="LithoCreep API",
        version="1.0.0",
        description="Satellite screening of tailings facilities. Screening only: not a structural safety certification.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )
    from app.api import scenarios  # imports this module, so it is registered here

    app.include_router(router)
    app.include_router(scenarios.router)
    return app
