"""API tests against the real application, a real DuckDB file and real pipeline outputs.

Nothing in the API is mocked. A ~1 km synthetic site inside the Sierrita bbox
is pushed through the real Phase 2-5 pipelines (``build_full_chain``); the
resulting Zarr products are served by ``create_app`` through FastAPI's
TestClient, with start-up (database build) included. Inputs are synthetic and
labelled as such; the Brumadinho series written here is a SYNTHETIC stand-in
in the documented CSV schema.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from fastapi.testclient import TestClient
from pyproj import Transformer

from app.api.endpoints import create_app
from app.api.schemas import Alert, Facility, Matrix, PixelResponse
from app.config import Settings
from app.pipelines import coupling as cp
from app.pipelines import kinematics as kin
from tests.test_coupling import build_full_chain

SIERRITA = (-111.12, 31.83, -111.04, 31.90)
SITE = (-111.11, 31.86, -111.10, 31.87)  # inside the Sierrita bbox
TO_LONLAT = Transformer.from_crs("EPSG:32612", "EPSG:4326", always_xy=True)
REQUIRED = {"sierrita", "mission", "bingham", "brumadinho"}

def _site_grid():
    from app.pipelines.hydrology import utm_grid_bounds

    return utm_grid_bounds(SITE, "EPSG:32612")


# Patches east of the moving block, clear of the pond, block and toe.
def VEGETATED(X, Y):
    gx0, _, _, gy1 = _site_grid()
    return (X > gx0 + 720) & (X < gx0 + 900) & (Y < gy1 - 360) & (Y > gy1 - 480)


def DECORRELATED(X, Y):
    gx0, _, _, gy1 = _site_grid()
    return (X > gx0 + 720) & (X < gx0 + 900) & (Y < gy1 - 600) & (Y > gy1 - 720)


@pytest.fixture(scope="module")
def chain(tmp_path_factory):
    root = tmp_path_factory.mktemp("api")
    settings = Settings(DATA_DIR=str(root / "data"))
    return build_full_chain(root, facility="Sierrita", bbox=SITE, settings=settings, vegetated=VEGETATED, decorrelated=DECORRELATED)


@pytest.fixture(scope="module")
def client(chain):
    with TestClient(create_app(chain["settings"])) as c:
        yield c


def block_cell(chain):
    """UTM centre of a coupled-grid cell well inside the accelerating block, and its lon/lat."""
    ds = xr.open_zarr(chain["settings"].cache_dir / "Sierrita_coupled.zarr")
    gx0, _, _, gy1 = chain["grid_bounds"]
    xs, ys = ds["x"].values, ds["y"].values
    x = xs[np.argmin(np.abs(xs - (gx0 + 450 + 15)))]
    y = ys[np.argmin(np.abs(ys - (gy1 - 660 - 15)))]
    lon, lat = TO_LONLAT.transform(x, y)
    return float(x), float(y), float(lon), float(lat)


# --------------------------------------------------------------------------- #
# Database and contracts
# --------------------------------------------------------------------------- #


def test_database_populates_on_launch(chain, client):
    db_path = chain["settings"].data_path / "lithocreep.duckdb"
    assert client.get("/api/health").json()["facilities"] == 4
    # The running app holds the DuckDB file open; query it through the app's own connection.
    rows = client.app.state.db.cursor().execute("SELECT id, mine_type, embankment_type, status FROM facilities ORDER BY id").fetchall()
    assert {r[0] for r in rows} == REQUIRED
    assert ("brumadinho", "iron ore", "upstream", "FAILED") in rows
    alerts = client.app.state.db.cursor().execute("SELECT facility_id, hazard_level FROM alerts").fetchall()
    assert alerts == [("sierrita", "CRITICAL_TIER3")]  # from Sierrita_coupled.zarr
    assert db_path.exists()


def test_facilities_match_schema(client):
    r = client.get("/api/facilities")
    assert r.status_code == 200
    facilities = [Facility.model_validate(f) for f in r.json()]
    assert {f.id for f in facilities} == REQUIRED
    sierrita = next(f for f in facilities if f.id == "sierrita")
    assert (sierrita.bbox_xmin, sierrita.bbox_ymin, sierrita.bbox_xmax, sierrita.bbox_ymax) == SIERRITA
    assert sierrita.crest_line_geom is None and sierrita.embankment_type == "unknown"
    assert set(r.json()[0]) == set(Facility.model_fields)  # exactly the specified fields
    assert client.get("/api/facilities/sierrita").json()["name"] == "Freeport-McMoRan Sierrita TSF"
    assert client.get("/api/facilities/nowhere").status_code == 404


def test_openapi_publishes_the_contracts(client):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    assert set(schemas["Facility"]["properties"]) == {
        "id", "name", "country", "latitude", "longitude", "bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax",
        "mine_type", "embankment_type", "crest_line_geom", "status"}
    assert set(schemas["Alert"]["properties"]) == {
        "id", "facility_id", "timestamp", "hazard_level", "failure_date_est", "failure_date_error_days", "diagnostic_evidence"}
    assert schemas["Alert"]["properties"]["hazard_level"]["enum"] == ["NONE", "WATCH_TIER1", "ALERT_TIER2", "CRITICAL_TIER3"]


def test_alerts_from_coupled_product(chain, client):
    r = client.get("/api/facilities/sierrita/alerts")
    alerts = [Alert.model_validate(a) for a in r.json()]
    assert len(alerts) == 1
    a = alerts[0]
    assert a.hazard_level == "CRITICAL_TIER3" and a.failure_date_est is not None and a.failure_date_error_days > 0
    assert a.diagnostic_evidence["contains_synthetic_or_mock_inputs"] is True
    assert "NOT a guaranteed" in a.diagnostic_evidence["disclaimer"]
    assert a.diagnostic_evidence["regime_counts"]["regime4-active-instability-tier3-critical"] > 0
    assert client.get("/api/facilities/mission/alerts").json() == []


# --------------------------------------------------------------------------- #
# Matrices
# --------------------------------------------------------------------------- #


def matrix_value(m: Matrix, lon: float, lat: float):
    xmin, ymin, xmax, ymax = m.bounds
    dx, dy = m.resolution_deg
    return m.values[int((ymax - lat) // dy)][int((lon - xmin) // dx)]


def test_regime_matrix_exact_bounds_and_values(chain, client):
    r = client.get("/api/facilities/sierrita/matrices", params={"layer": "anomaly_regime"})
    assert r.status_code == 200
    m = Matrix.model_validate(r.json())
    assert m.bounds == list(SIERRITA)
    assert m.shape == [252, 288]  # 0.07 x 0.08 degrees at 1 arc-second
    assert m.resolution_deg[0] * m.shape[1] == pytest.approx(0.08) and m.resolution_deg[1] * m.shape[0] == pytest.approx(0.07)
    assert m.categorical and "regime4" in m.flag_meanings
    finite = {v for row in m.values for v in row if v is not None}
    assert finite <= {0, 1, 2, 3, 4, 5} and 4 in finite
    _, _, lon, lat = block_cell(chain)
    assert matrix_value(m, lon, lat) == 4
    assert matrix_value(m, -111.05, 31.84) is None  # outside the processed site: null, not NaN
    assert 0 < m.valid_fraction < 0.1
    assert m.source.product == "coupled" and m.source.synthetic_or_mock is True


def test_time_dependent_matrix_selects_nearest_epoch(client):
    latest = Matrix.model_validate(client.get("/api/facilities/sierrita/matrices", params={"layer": "displacement"}).json())
    assert len(latest.available_times) == 60 and latest.time == latest.available_times[-1]
    wanted = latest.available_times[10] + pd.Timedelta(days=2)
    m = Matrix.model_validate(client.get("/api/facilities/sierrita/matrices", params={"layer": "displacement", "t": wanted.isoformat()}).json())
    assert m.time == latest.available_times[10] and m.requested_time == wanted
    static = client.get("/api/facilities/sierrita/matrices", params={"layer": "elevation", "t": wanted.isoformat()}).json()
    assert static["time"] is None and "static" in static["note"]


def test_hydraulic_distance_layer(chain, client):
    m = Matrix.model_validate(client.get("/api/facilities/sierrita/matrices", params={"layer": "hydraulic_distance"}).json())
    gx0, _, _, gy1 = chain["grid_bounds"]
    pond_lon, pond_lat = TO_LONLAT.transform(gx0 + 450, gy1 - 60)
    _, _, lon, lat = block_cell(chain)
    assert matrix_value(m, pond_lon, pond_lat) == pytest.approx(0.0, abs=1e-6)
    assert 350 < matrix_value(m, lon, lat) < 600  # block ~450-500 m below the shoreline
    assert "not a flow-path distance" in m.note


def test_observability_mask_layer(chain, client):
    m = Matrix.model_validate(client.get("/api/facilities/sierrita/matrices", params={"layer": "observability_mask"}).json())
    assert m.categorical and m.flag_meanings.split()[1:3] == ["unobservable-vegetated", "unobservable-low-coherence"]
    assert m.source.variable == "classification_flags"
    gx0, _, _, gy1 = _site_grid()
    veg = TO_LONLAT.transform(gx0 + 810, gy1 - 420)
    dec = TO_LONLAT.transform(gx0 + 810, gy1 - 660)
    _, _, lon, lat = block_cell(chain)
    assert matrix_value(m, *veg) == 1
    assert matrix_value(m, *dec) == 2
    assert matrix_value(m, lon, lat) == 0
    assert matrix_value(m, -111.05, 31.84) is None
    # The masks agree with the pipeline layers they summarise.
    coh = Matrix.model_validate(client.get("/api/facilities/sierrita/matrices", params={"layer": "coherence"}).json())
    assert matrix_value(coh, *dec) < 0.65 < matrix_value(coh, lon, lat)
    regime = Matrix.model_validate(client.get("/api/facilities/sierrita/matrices", params={"layer": "anomaly_regime"}).json())
    assert matrix_value(regime, *dec) == 2


def test_explicit_synthetic_stamp_overrides_filename_provenance(tmp_path):
    from app.api import layers as L

    ds = xr.Dataset(attrs={"source": "OPERA_L3_DISP-S1_V1"})
    assert not L.describe_source("kinematics", tmp_path / "X_kinematics.zarr", "velocity", ds).synthetic_or_mock
    ds.attrs.update(synthetic_or_mock=1, synthetic_note="demo site")
    src = L.describe_source("kinematics", tmp_path / "X_kinematics.zarr", "velocity", ds)
    assert src.synthetic_or_mock and src.label == "SYNTHETIC: demo site"
    for product in ("hydrology", "spectral", "coupled"):
        assert L.describe_source(product, tmp_path / "x.zarr", "v", ds).synthetic_or_mock


def test_matrix_errors(client):
    assert client.get("/api/facilities/sierrita/matrices", params={"layer": "bogus"}).status_code == 422
    assert client.get("/api/facilities/sierrita/matrices").status_code == 422
    r = client.get("/api/facilities/mission/matrices", params={"layer": "sai"})
    assert r.status_code == 404 and "Mission_coupled.zarr not found" in r.json()["detail"]
    assert client.get("/api/facilities/nowhere/matrices", params={"layer": "sai"}).status_code == 404


def test_layers_listing(client):
    layers = {l["layer"]: l for l in client.get("/api/facilities/sierrita/layers").json()}
    assert layers["anomaly_regime"]["available"] and layers["velocity"]["time_dependent"]
    assert len(layers["water_class"]["times"]) == 2
    assert not client.get("/api/facilities/mission/layers").json()[0]["available"]


# --------------------------------------------------------------------------- #
# Pixel
# --------------------------------------------------------------------------- #


def test_pixel_exact_cell(chain, client):
    x, y, lon, lat = block_cell(chain)
    r = client.get("/api/facilities/sierrita/pixel", params={"lat": lat, "lon": lon})
    assert r.status_code == 200
    p = PixelResponse.model_validate(r.json())
    assert p.unavailable == {}
    k = p.kinematics
    assert k.match.method == "exact" and k.match.nearest_cell_distance_m < 0.5
    assert len(k.times) == len(k.displacement_mm) == len(k.velocity_mm_yr) == 60
    assert k.status == "accelerating-forecast" and k.instability_time is not None and "NOT a guaranteed" in k.disclaimer
    assert all(lo <= v <= hi for lo, v, hi in zip(k.velocity_lower_mm_yr, k.velocity_mm_yr, k.velocity_upper_mm_yr) if v is not None)
    c = p.classification
    assert c.regime == cp.REGIME_4 and c.alert_tier == cp.TIER3_CRITICAL and c.failure_time_estimate is not None
    assert p.spectral.sai > 0.25 and set(p.spectral.abundance) >= {"Jarosite", "Goethite"}
    assert p.hydrology.d_crest_m.values == [150.0, 90.0] and p.hydrology.water_class.values == [0.0, 0.0]


def test_pixel_interpolates_between_cells(chain, client):
    x, y, _, _ = block_cell(chain)
    lon, lat = TO_LONLAT.transform(x + 11.0, y - 7.0)
    p = PixelResponse.model_validate(client.get("/api/facilities/sierrita/pixel", params={"lat": lat, "lon": lon}).json())
    assert p.kinematics.match.method == "bilinear" and 10 < p.kinematics.match.nearest_cell_distance_m < 15
    ds = xr.open_zarr(chain["settings"].cache_dir / "Sierrita_kinematics.zarr")
    ux, uy = Transformer.from_crs("EPSG:4326", "EPSG:32612", always_xy=True).transform(lon, lat)
    expected = ds["displacement"].astype(float).interp(x=ux, y=uy, method="linear").values
    np.testing.assert_allclose(p.kinematics.displacement_mm, expected, rtol=1e-6, atol=1e-6)
    assert p.classification.match.method == "nearest" and p.classification.regime == cp.REGIME_4


def test_pixel_errors(client):
    assert client.get("/api/facilities/sierrita/pixel", params={"lat": 10.0, "lon": -111.1}).status_code == 422
    assert client.get("/api/facilities/sierrita/pixel", params={"lat": 95.0, "lon": -111.1}).status_code == 422
    r = client.get("/api/facilities/sierrita/pixel", params={"lat": 31.84, "lon": -111.05})  # inside bbox, no products there
    assert r.status_code == 404 and set(r.json()["detail"]["unavailable"]) == {"kinematics", "spectral", "hydrology", "classification"}
    assert client.get("/api/facilities/nowhere/pixel", params={"lat": 0, "lon": 0}).status_code == 404


# --------------------------------------------------------------------------- #
# Fallbacks for unreadable caches
# --------------------------------------------------------------------------- #


def test_unreadable_cache_falls_back(chain, tmp_path):
    data = tmp_path / "data"
    shutil.copytree(chain["settings"].data_path, data, ignore=shutil.ignore_patterns("*.duckdb*"))
    coupled = data / "cache" / "Sierrita_coupled.zarr"
    for meta in (".zmetadata", ".zgroup", ".zattrs"):
        (coupled / meta).write_text("{ not json")
    with TestClient(create_app(Settings(DATA_DIR=str(data)))) as c:
        assert c.get("/api/health").json()["alerts"] == 0  # unreadable product: no alert, but the API starts
        sai = c.get("/api/facilities/sierrita/matrices", params={"layer": "sai"})
        assert sai.status_code == 200 and sai.json()["source"]["product"] == "spectral"
        assert "fallback source used" in sai.json()["note"]
        regime = c.get("/api/facilities/sierrita/matrices", params={"layer": "anomaly_regime"})
        assert regime.status_code == 503 and "unreadable" in regime.json()["detail"]
        _, _, lon, lat = block_cell(chain)
        p = c.get("/api/facilities/sierrita/pixel", params={"lat": lat, "lon": lon}).json()
        assert p["classification"] is None and p["unavailable"]["classification"].startswith("unreadable")
        assert p["kinematics"]["status"] == "accelerating-forecast"


# --------------------------------------------------------------------------- #
# Brumadinho: historical CSV instead of DISP-S1
# --------------------------------------------------------------------------- #


def write_brumadinho_csv(path: Path):
    """SYNTHETIC stand-in for the published series (schema test only)."""
    times = pd.date_range("2017-06-01", periods=60, freq="12D")
    ty = ((times - times[0]).days / kin.DAYS_PER_YEAR).to_numpy()
    tf = ty[-1] + 30 / kin.DAYS_PER_YEAR
    rows = []
    for pid, lon, lat, series in (("toe", -44.121, -20.119, (1 / 0.03) * (np.log(tf - ty) - np.log(tf))),
                                  ("crest", -44.118, -20.116, -1.0 * ty)):
        noisy = series + np.random.default_rng(len(pid)).normal(0, 0.3, ty.size)
        rows += [{"date": d.date().isoformat(), "point_id": pid, "displacement_mm": v, "temporal_coherence": 0.85,
                  "longitude": lon, "latitude": lat, "source": "SYNTHETIC test series"} for d, v in zip(times, noisy)]
    pd.DataFrame(rows).to_csv(path, index=False)


def test_brumadinho_pixel_reads_historic_csv(tmp_path):
    settings = Settings(DATA_DIR=str(tmp_path / "data"))
    with TestClient(create_app(settings)) as c:
        r = c.get("/api/facilities/brumadinho/pixel", params={"lat": -20.12, "lon": -44.12})
        assert r.status_code == 404 and "brumadinho_historic.csv not found" in r.json()["detail"]["unavailable"]["kinematics"]

        write_brumadinho_csv(settings.data_path / "brumadinho_historic.csv")
        p = PixelResponse.model_validate(c.get("/api/facilities/brumadinho/pixel", params={"lat": -20.1192, "lon": -44.1212}).json())
        k = p.kinematics
        assert k.source.product == "historical_csv" and k.source.synthetic_or_mock is True
        assert k.point_id == "toe" and k.match.method == "nearest" and k.match.nearest_cell_distance_m < 50
        assert len(k.displacement_mm) == 60 and k.velocity_mm_yr is None  # raw series until analysed

        # Once the Phase 3 backtest has run, its GP analysis is served for the same point.
        kin.run_kinematics("Brumadinho", (-44.15, -20.14, -44.10, -20.10), settings.cache_dir / "Brumadinho_kinematics.zarr", settings=settings)
        k2 = PixelResponse.model_validate(c.get("/api/facilities/brumadinho/pixel", params={"lat": -20.1192, "lon": -44.1212}).json()).kinematics
        assert k2.source.product == "kinematics" and k2.status == "accelerating-forecast" and len(k2.velocity_mm_yr) == 60
        assert k2.instability_time is not None


# --------------------------------------------------------------------------- #
# CORS and registry merge
# --------------------------------------------------------------------------- #


def test_cors_policy(client):
    ok = client.options("/api/facilities", headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == "http://localhost:3000"
    bad = client.get("/api/facilities", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in bad.headers


def test_ingest_registry_update_keeps_curated_metadata(tmp_path):
    from app import main

    settings = Settings(DATA_DIR=str(tmp_path / "data"))
    registry = settings.data_path / "global_tailings.geojson"
    shutil.copy(Path(main.__file__).resolve().parents[2] / "data" / "global_tailings.geojson", registry)
    main.update_tailings_registry(registry, "Brumadinho", (-44.15, -20.14, -44.10, -20.10), "run1", "2026-01-01T00:00:00Z",
                                  [{"dataset_id": "OPERA_L3_DISP-S1_V1", "status": "cache_fallback"}])
    with TestClient(create_app(settings)) as c:
        b = Facility.model_validate(c.get("/api/facilities/brumadinho").json())
        assert (b.mine_type, b.embankment_type, b.status) == ("iron ore", "upstream", "FAILED")
        assert len(c.get("/api/facilities").json()) == 4
