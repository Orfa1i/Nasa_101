"""Verification checks for the co-anomaly coupler and the 4-regime classifier.

The scenario is a 40 x 40 cell (30 m) slope draining due south:

* rows 0-3: tailings pond (DSWx water) in columns 0-19 only;
* rows 30-33: seepage toe (SAI 0.5) across the slope;
* rows 14-17: blocks of 4 columns, one per expected outcome:

  | Block | Columns | Set-up | Expected |
  |-------|---------|--------|----------|
  | A | 0-3 | High SAI, accelerating, below the pond | REGIME_4 |
  | B | 4-7 | High SAI, stable | REGIME_3 |
  | C | 8-11 | Low SAI, linear creep | REGIME_1 |
  | D | 20-23 | High SAI, accelerating, no pond upslope | REGIME_3, accelerating-not-coupled |
  | E | 24-27 | Low SAI, accelerating | kinematic-only (5) |
  | F | 28-31 | Coherence 0.4 | REGIME_2 |
  | G | 32-35 | Spectral RMSE 0.08 | rejected (0), low confidence |

Spectral layers are on a 60 m grid and kinematics on a 30 m grid offset by
10 m, so the alignment step is exercised; blocks are aligned to the 60 m
pixels and checked at their centres.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from pyproj import Transformer
from shapely.geometry import box, mapping

from app import config, main
from app.pipelines import coupling as cp
from app.pipelines import hydrology as hyd
from app.pipelines import kinematics as kin
from app.pipelines import unmixing

RES = 30.0
N = 40
TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32612", always_xy=True)
TO_LONLAT = Transformer.from_crs("EPSG:32612", "EPSG:4326", always_xy=True)
X0, Y0 = (round(v / 60.0) * 60.0 for v in TO_UTM.transform(-111.11, 31.89))
ROWS = slice(14, 18)
BLOCKS = {"A": slice(0, 4), "B": slice(4, 8), "C": slice(8, 12), "D": slice(20, 24), "E": slice(24, 28), "F": slice(28, 32), "G": slice(32, 36)}


def centre(block):
    return 15, BLOCKS[block].start + 1


def grid(nx, ny, res, x0=X0, y0=Y0):
    return {"x": x0 + (np.arange(nx) + 0.5) * res, "y": y0 - (np.arange(ny) + 0.5) * res}


def write_zarr(ds: xr.Dataset, path: Path) -> Path:
    ds = ds.rio.write_crs("EPSG:32612")
    for name in ds.data_vars:
        if {"y", "x"} <= set(ds[name].dims) and "grid_mapping" not in ds[name].encoding:
            ds[name].attrs["grid_mapping"] = "spatial_ref"
    ds.to_zarr(path, mode="w")
    return path


def hydrology_ds(*, encroaching=True, block_aspect_rows=None):
    rows, _ = np.indices((N, N))
    z = 1000.0 - 0.05 * rows * RES  # drains due south
    dem = xr.DataArray(z.astype(np.float32), dims=("y", "x"), coords=grid(N, N, RES)).rio.write_crs("EPSG:32612")
    ds = hyd.terrain_dataset(dem)
    if block_aspect_rows is not None:
        aspect = ds["aspect_deg"].values.copy()
        aspect[block_aspect_rows, :] = np.nan  # e.g. dense vegetation between pond and slope
        ds["aspect_deg"] = (("y", "x"), aspect)
    times = pd.date_range("2024-01-01", periods=3, freq="30D")
    wtr = np.zeros((3, N, N), dtype=np.uint8)
    wtr[:, 0:4, 0:20] = hyd.WTR_OPEN_WATER
    ds["water_class"] = (("time", "y", "x"), wtr)
    ds = ds.assign_coords(time=times)
    ds["d_crest_m"] = ("time", np.array([120.0, 110.0, 80.0], dtype=np.float32))
    ds["d_crest_status"] = ("time", np.zeros(3, dtype=np.int8))
    last_flag = hyd.ALERT_SHRINK_BASELINE if encroaching else 0
    ds["setback_alert_flags"] = ("time", np.array([0, 0, last_flag], dtype=np.int8))
    ds.attrs["dswx_source"] = hyd.DSWX_DATASET_ID
    return ds


def spectral_ds(*, origin="mock"):
    n = N // 2  # 60 m grid
    sai = np.full((n, n), 0.05)
    rmse = np.full((n, n), 0.01)
    obs = np.zeros((n, n), dtype=np.int8)
    toe = slice(15, 17)  # 60 m rows 15-16 -> 30 m rows 30-33
    sai[toe, :] = 0.5
    r60 = slice(7, 9)  # 30 m rows 14-17
    for block in ("A", "B", "D"):
        c = BLOCKS[block]
        sai[r60, c.start // 2 : c.stop // 2] = 0.5
    g = BLOCKS["G"]
    rmse[r60, g.start // 2 : g.stop // 2] = 0.08
    obs[r60, g.start // 2 : g.stop // 2] = 3
    wl, _ = unmixing.load_emit_wavelengths()
    ds = xr.Dataset(
        {"sai": (("y", "x"), sai.astype(np.float32)), "rmse": (("y", "x"), rmse.astype(np.float32)),
         "observability": (("y", "x"), obs)},
        coords={**grid(n, n, 60.0), "endmember": list(unmixing.ENDMEMBERS)},
    )
    ds.attrs.update(
        active_wavelengths_nm=json.dumps(wl[unmixing.active_band_mask(wl)].tolist()),
        endmember_library_file=unmixing.SPLIB06_EXTRACT_FILE.name,
        source_origin=origin,
    )
    return ds


def kinematics_ds():
    """30 m grid shifted by +10 m E / -10 m N: nearest alignment maps index (i, j) to (i, j)."""
    times = pd.date_range("2022-01-01", periods=30, freq="25D")
    v = np.zeros((N, N))
    a = np.zeros((N, N))
    status = np.full((N, N), kin.STABLE, dtype=np.int8)
    coh = np.full((N, N), 0.9)
    days = np.full((N, N), np.nan)
    for block in ("A", "D", "E"):
        v[ROWS, BLOCKS[block]] = -30.0
        a[ROWS, BLOCKS[block]] = -50.0
        status[ROWS, BLOCKS[block]] = kin.ACCEL_FORECAST
        days[ROWS, BLOCKS[block]] = 45.0
    v[ROWS, BLOCKS["C"]] = -10.0
    status[ROWS, BLOCKS["C"]] = kin.LINEAR
    coh[ROWS, BLOCKS["F"]] = 0.4
    status[ROWS, BLOCKS["F"]] = kin.UNOBSERVABLE
    v_sig, a_sig = 1.0, 5.0
    series = lambda arr: np.repeat(arr[None], times.size, axis=0).astype(np.float32)  # noqa: E731
    yx, tyx = ("y", "x"), ("time", "y", "x")
    ds = xr.Dataset(
        {
            "velocity": (tyx, series(v)), "velocity_lower": (tyx, series(v - 2 * v_sig)), "velocity_upper": (tyx, series(v + 2 * v_sig)),
            "acceleration": (tyx, series(a)), "acceleration_lower": (tyx, series(a - 2 * a_sig)), "acceleration_upper": (tyx, series(a + 2 * a_sig)),
            "temporal_coherence": (yx, coh.astype(np.float32)), "kinematic_status": (yx, status),
            # Same CF units as Phase 3 writes: xarray decodes these as timedelta64 on read.
            "instability_days_after_last_epoch": (yx, days.astype(np.float32), {"units": "days"}),
            "instability_days_2sigma": (yx, np.where(np.isfinite(days), 12.0, np.nan).astype(np.float32), {"units": "days"}),
        },
        coords={"time": times, **grid(N, N, RES, X0 + 10.0, Y0 - 10.0)},
    )
    ds.attrs.update(source=kin.DISP_DATASET_ID, last_epoch=str(times[-1]))
    return ds


@pytest.fixture
def scenario(tmp_path):
    def build(**hydro_kwargs):
        folder = tmp_path / f"s{len(list(tmp_path.iterdir()))}"
        folder.mkdir()
        return (
            write_zarr(spectral_ds(), folder / "Sierrita_spectral.zarr"),
            write_zarr(kinematics_ds(), folder / "Sierrita_kinematics.zarr"),
            write_zarr(hydrology_ds(**hydro_kwargs), folder / "Sierrita_hydrology.zarr"),
        )

    return build


def run(paths, **kw):
    spectral, kinematics, hydro = (xr.open_zarr(p, decode_coords="all") for p in paths)
    return cp.couple(spectral, kinematics, hydro, **kw)


# --------------------------------------------------------------------------- #
# Regimes and alert tiers
# --------------------------------------------------------------------------- #


def test_regimes_across_all_tiers(scenario):
    ds = run(scenario())
    regime, flags, tier = ds["regime"].values, ds["classification_flags"].values, ds["alert_tier"].values
    expected = {"A": cp.REGIME_4, "B": cp.REGIME_3, "C": cp.REGIME_1, "D": cp.REGIME_3,
                "E": cp.REGIME_KINEMATIC_ONLY, "F": cp.REGIME_2, "G": cp.REGIME_UNCLASSIFIED}
    for block, code in expected.items():
        assert regime[centre(block)] == code, block
    assert tier[centre("A")] == cp.TIER3_CRITICAL and tier[centre("B")] == cp.TIER1_WATCH and tier[centre("C")] == cp.TIER_NONE
    assert flags[centre("D")] & cp.FLAG_ACCEL_NOT_COUPLED
    assert flags[centre("F")] & cp.FLAG_LOW_COHERENCE and ds["low_confidence"].values[centre("F")]
    assert flags[centre("G")] & cp.FLAG_HIGH_RMSE and ds["low_confidence"].values[centre("G")]
    assert regime[25, 30] == cp.REGIME_1  # quiet background
    assert regime[31, 10] == cp.REGIME_3  # seepage toe itself: high SAI, stable
    assert ds.attrs["facility_hazard_level"] == "CRITICAL_TIER3"


def test_regime4_carries_failure_time_and_disclaimer(scenario):
    ds = run(scenario())
    a = centre("A")
    last = pd.Timestamp(kinematics_ds().attrs["last_epoch"])
    assert pd.Timestamp(ds["failure_time_estimate"].values[a]) == last + pd.Timedelta(days=45)
    assert pd.Timestamp(ds["failure_time_lower"].values[a]) == last + pd.Timedelta(days=33)
    assert pd.Timestamp(ds["failure_time_upper"].values[a]) == last + pd.Timedelta(days=57)
    assert np.isnat(ds["failure_time_estimate"].values[centre("D")])  # only REGIME_4 cells get a date
    assert "NOT a guaranteed" in ds.attrs["disclaimer"]


def test_no_encroachment_means_no_regime4(scenario):
    ds = run(scenario(encroaching=False))
    assert ds["regime"].values[centre("A")] == cp.REGIME_3
    assert ds["classification_flags"].values[centre("A")] & cp.FLAG_ACCEL_NOT_COUPLED
    assert ds.attrs["facility_hazard_level"] == "WATCH_TIER1"
    assert not json.loads(ds.attrs["setback_state"])["encroaching"]


def test_aspect_paths_must_confirm_the_flow_link(scenario):
    ds = run(scenario(block_aspect_rows=slice(8, 11)))  # aspect unknown between pond and slope
    a = centre("A")
    assert ds["upslope_water"].values[a]  # D-infinity still connects them...
    assert not ds["aspect_validated"].values[a]  # ...but the independent aspect check fails
    assert ds["regime"].values[a] == cp.REGIME_3


def test_earthworks_polygon_forces_regime2(scenario, tmp_path):
    paths = scenario()
    c = BLOCKS["C"]
    hydro = xr.open_zarr(paths[2], decode_coords="all")
    xs, ys = hydro["x"].values, hydro["y"].values
    mask = np.zeros((N, N), dtype=bool)
    mask[ROWS, c] = True
    ds = run(paths, earthworks_mask=mask)
    assert ds["regime"].values[centre("C")] == cp.REGIME_2
    assert ds["classification_flags"].values[centre("C")] & cp.FLAG_EARTHWORKS
    # Same via a polygon file through run_coupling.
    poly = box(xs[c.start] - 15, ys[ROWS.stop - 1] - 15, xs[c.stop - 1] + 15, ys[ROWS.start] + 15)
    lon, lat = TO_LONLAT.transform(*np.array(poly.exterior.coords).T)
    gj = tmp_path / "earthworks.geojson"
    gj.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [list(map(list, zip(lon, lat)))]}}]}))
    summary = cp.run_coupling("Sierrita", *paths, tmp_path / "out.zarr", earthworks_path=gj)
    out = xr.open_zarr(tmp_path / "out.zarr")
    assert out["regime"].values[centre("C")] == cp.REGIME_2
    assert summary["regime_counts"]["regime2-unobservable-anthropogenic"] >= 16 + 16


def test_quiet_facility_has_no_alert(scenario, tmp_path):
    paths = scenario()
    k = kinematics_ds()
    for name in ("velocity", "velocity_lower", "velocity_upper", "acceleration", "acceleration_lower", "acceleration_upper"):
        k[name] = k[name] * 0
    k["kinematic_status"] = k["kinematic_status"].where(k["kinematic_status"] == kin.UNOBSERVABLE, kin.STABLE)
    s = spectral_ds()
    s["sai"] = s["sai"] * 0 + 0.05
    quiet = (write_zarr(s, tmp_path / "s.zarr"), write_zarr(k, tmp_path / "k.zarr"), paths[2])
    ds = run(quiet)
    assert set(np.unique(ds["regime"].values)) <= {cp.REGIME_1, cp.REGIME_2, cp.REGIME_UNCLASSIFIED}
    assert ds.attrs["facility_hazard_level"] == "NONE"


# --------------------------------------------------------------------------- #
# Alignment
# --------------------------------------------------------------------------- #


def test_all_layers_share_the_hydrology_grid(scenario):
    paths = scenario()
    ds = run(paths)
    hydro = xr.open_zarr(paths[2], decode_coords="all")
    master = hydro["elevation"].rio.write_crs(hydro.rio.crs)
    assert cp.same_grid(ds["regime"].rio.write_crs(ds.rio.crs), master)
    assert ds.rio.crs.to_epsg() == 32612
    # 60 m spectral block interior keeps its value; the 10 m-shifted kinematics map index to index.
    assert ds["sai"].values[15, 1] == pytest.approx(0.5)
    np.testing.assert_array_equal(ds["velocity_mm_yr"].values[ROWS, BLOCKS["A"]], -30.0)
    np.testing.assert_array_equal(ds["kinematic_status"].values, kinematics_ds()["kinematic_status"].values)


# --------------------------------------------------------------------------- #
# Topology
# --------------------------------------------------------------------------- #


def test_dag_is_acyclic_on_rough_terrain():
    rng = np.random.default_rng(3)
    filled = hyd.fill_depressions(rng.normal(0, 3, (30, 30)) + np.indices((30, 30))[0] * 0.5)
    flow, order = cp.build_dag(filled, RES)
    assert cp.dag_is_acyclic(filled, flow)
    position = np.empty(order.size, dtype=int)
    position[order] = np.arange(order.size)
    for r in (flow.receiver1.ravel(), flow.receiver2.ravel()):
        has = r >= 0
        assert np.all(position[np.flatnonzero(has)] < position[r[has]])  # donors before receivers


def test_up_and_downslope_propagation_on_a_slope():
    z = hydrology_ds()["elevation_filled"].values.astype(float)
    flow, order = cp.build_dag(z, RES)
    seed = np.zeros(z.shape, dtype=bool)
    seed[0, 5] = True
    down = cp.propagate_downslope(seed, flow, order)
    assert down[:, 5].all() and not down[:, 6].any()  # flows straight down its column
    toe = np.zeros(z.shape, dtype=bool)
    toe[35, 7] = True
    up = cp.propagate_upslope(toe, flow, order)
    assert up[:36, 7].all() and not up[36:, 7].any()
    strict = cp.strict_descendant_hit(up, flow)
    assert strict[34, 7] and not strict[35, 7]


def test_aspect_path_follows_aspect_and_stops_at_gaps():
    aspect = np.full((10, 10), 180.0)
    assert cp.aspect_path(aspect, (0, 3)) == [(r, 3) for r in range(10)]
    aspect[5, 3] = np.nan
    assert cp.aspect_path(aspect, (0, 3))[-1] == (5, 3)
    diag = np.full((10, 10), 135.0)  # south-east
    assert cp.aspect_path(diag, (0, 0))[:3] == [(0, 0), (1, 1), (2, 2)]


def test_water_boundary_cells():
    wtr = np.zeros((6, 6), dtype=np.uint8)
    wtr[1:5, 1:5] = hyd.WTR_PARTIAL_WATER
    b = cp.water_boundary_cells(wtr)
    assert b[1, 1] and b[4, 2] and not b[2, 2] and not b[0, 0]


# --------------------------------------------------------------------------- #
# Uncertainty
# --------------------------------------------------------------------------- #


def test_sai_interval_from_unmixing_rmse(scenario):
    factor = cp.sai_sigma_factor(spectral_ds())
    assert factor == pytest.approx(0.70, abs=0.01)  # sqrt(w^T (M^T M)^-1 w) for the bundled library
    ds = run(scenario())
    c = (25, 30)
    assert ds["sai_upper"].values[c] - ds["sai"].values[c] == pytest.approx(2 * factor * 0.01, rel=1e-4)


def test_sai_marginal_flag():
    sai = np.array([[0.26, 0.40]])
    regime, flags = cp.classify(
        sai=sai, sai_sigma=np.array([[0.01, 0.01]]), rmse=np.full((1, 2), 0.01), vegetated=np.zeros((1, 2), bool),
        spectral_valid=np.ones((1, 2), bool), coherence=np.full((1, 2), 0.9), kin_status=np.full((1, 2), kin.STABLE),
        kin_valid=np.ones((1, 2), bool), accelerating=np.zeros((1, 2), bool), coupled=np.zeros((1, 2), bool),
        earthworks=np.zeros((1, 2), bool),
    )
    assert list(regime[0]) == [cp.REGIME_3, cp.REGIME_3]
    assert flags[0, 0] & cp.FLAG_SAI_MARGINAL and not flags[0, 1] & cp.FLAG_SAI_MARGINAL


def test_phase_noise_velocity_sigma():
    sigma = cp.phase_noise_velocity_sigma(np.array([1.0, 0.9, 0.5]), n_epochs=30, span_years=2.0)
    assert sigma[0] == 0.0
    expected = 55.465763 / (4 * math.pi) * math.sqrt((1 - 0.81) / (2 * 0.81)) * math.sqrt(12 / 30) / 2.0
    assert sigma[1] == pytest.approx(expected)
    assert sigma[2] > sigma[1]


def test_combined_velocity_interval_is_wider_than_gp_alone(scenario):
    ds = run(scenario())
    a = centre("A")
    half_width = (ds["velocity_upper"].values[a] - ds["velocity_lower"].values[a]) / 2
    assert half_width > 2 * 1.0  # GP sigma was 1 mm/yr
    assert half_width == pytest.approx(2 * math.hypot(1.0, ds["velocity_sigma_phase_noise"].values[a]), rel=1e-5)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


@pytest.fixture
def cli_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "cli_data"))
    config.get_settings.cache_clear()
    yield config.get_settings()
    config.get_settings.cache_clear()


def test_cli_couple_writes_sierrita_coupled(cli_settings):
    cache = cli_settings.cache_dir
    write_zarr(spectral_ds(), cache / "Sierrita_spectral.zarr")
    write_zarr(kinematics_ds(), cache / "Sierrita_kinematics.zarr")
    write_zarr(hydrology_ds(), cache / "Sierrita_hydrology.zarr")
    assert main.main(["couple", "--facility", "Sierrita"]) == 0
    out = xr.open_zarr(cache / "Sierrita_coupled.zarr", decode_coords="all")
    assert out["regime"].values[centre("A")] == cp.REGIME_4 and out.rio.crs.to_epsg() == 32612
    summary = json.loads(next(cli_settings.log_dir.glob("Sierrita_coupled_*.json")).read_text())
    assert summary["facility_hazard_level"] == "CRITICAL_TIER3"
    assert summary["contains_synthetic_or_mock_inputs"] is True  # mock spectral origin propagates
    assert "regime4_failure_time_earliest" in summary


def test_cli_couple_reports_missing_inputs(cli_settings):
    assert main.main(["couple", "--facility", "Sierrita"]) == 1
    assert "run `unmix` first" in next(cli_settings.log_dir.glob("couple_*.log")).read_text()


# --------------------------------------------------------------------------- #
# Cross-phase contract: real Phase 2/3/4 pipelines feeding the coupler
# --------------------------------------------------------------------------- #


CHAIN_BBOX = (-111.127, 31.896, -111.117, 31.906)


def build_full_chain(tmp_path, *, facility="Test", bbox=CHAIN_BBOX, settings=None, vegetated=None, decorrelated=None):
    """Run the real Phase 4, 2 and 3 pipelines on a ~1 km synthetic site, then Phase 5.

    The site has a pond at the top, a moving (accelerating) block mid-slope and a
    seepage toe; returns the settings, the coupling summary and the geometry helpers.
    ``vegetated`` / ``decorrelated`` are optional ``(X, Y) -> bool`` UTM regions that get a
    vegetation spectrum (NDVI > 0.35) or a DISP-S1 coherence of 0.4.
    """
    from app.config import Settings
    from tests.test_hydrology import dswx_name, write_wtr
    from tests.test_kinematics import disp_name, write_disp
    from tests.test_unmixing import make_cube, write_input_zarr

    settings = settings or Settings(DATA_DIR=str(tmp_path / "data"))
    gx0, gy0, gx1, gy1 = hyd.utm_grid_bounds(bbox, "EPSG:32612")
    pond_bottom = [gy1 - 150, gy1 - 210]  # the pond grows towards the crest between epochs
    crest_y = gy1 - 300
    block = lambda X, Y: (X > gx0 + 300) & (X < gx0 + 600) & (Y < gy1 - 600) & (Y > gy1 - 720)  # noqa: E731
    toe = lambda X, Y: (Y < gy1 - 900) & (Y > gy1 - 1020)  # noqa: E731

    # Phase 4 inputs: DEM sloping south, two DSWx epochs, a crest line.
    pad = 300.0
    x = gx0 - pad + (np.arange(int((gx1 - gx0 + 2 * pad) / RES)) + 0.5) * RES
    y = gy1 + pad - (np.arange(int((gy1 - gy0 + 2 * pad) / RES)) + 0.5) * RES
    X, Y = np.meshgrid(x, y)
    dem = xr.DataArray((1000.0 - 0.05 * (gy1 + pad - Y)).astype(np.float32), dims=("y", "x"), coords={"x": x, "y": y}).rio.write_crs("EPSG:32612")
    dem.rio.to_raster(tmp_path / "dem.tif")
    dswx = tmp_path / "dswx"
    dswx.mkdir()
    for date, bottom in zip(("20240105", "20240305"), pond_bottom):
        wtr = np.where((Y > bottom) & (Y < gy1) & (X > gx0 + 150) & (X < gx0 + 750), hyd.WTR_OPEN_WATER, hyd.WTR_NOT_WATER)
        write_wtr(dswx / dswx_name("12RVV", f"{date}T180000Z"), dem, wtr)
    (lon0, lon1), (lat0, lat1) = TO_LONLAT.transform([gx0 - 200, gx1 + 200], [crest_y, crest_y])
    crest = tmp_path / "crest.geojson"
    crest.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
        "geometry": {"type": "LineString", "coordinates": [[lon0, lat0], [lon1, lat1]]}}]}))
    hydro_out = settings.cache_dir / f"{facility}_hydrology.zarr"
    hyd.run_hydrology(facility, bbox, hydro_out, settings=settings, dem_paths=[tmp_path / "dem.tif"],
                      dswx_paths=sorted(dswx.glob("*.tif")), crest_path=crest)

    # Phase 2: EMIT-like cube on a 60 m grid; jarosite/montmorillonite-rich on the block and the toe.
    wl, _ = unmixing.load_emit_wavelengths()
    M_full = unmixing.build_endmember_matrix(wl, unmixing.load_spectral_library(unmixing.SPLIB06_EXTRACT_FILE))
    x0, y0 = gx0 - 120, gy1 + 120
    n60x, n60y = int((gx1 - gx0 + 240) / 60), int((gy1 - gy0 + 240) / 60)
    X60, Y60 = np.meshgrid(x0 + (np.arange(n60x) + 0.5) * 60, y0 - (np.arange(n60y) + 0.5) * 60)
    seep = block(X60, Y60) | toe(X60, Y60)
    f = np.zeros((7, n60y, n60x))
    names = list(unmixing.ENDMEMBERS)
    f[names.index("Jarosite")] = np.where(seep, 0.6, 0.0)
    f[names.index("Montmorillonite")] = np.where(seep, 0.3, 0.0)
    f[names.index("Quartz-Feldspar")] = np.where(seep, 0.1, 0.9)
    f[names.index("Water-Shade")] = np.where(seep, 0.0, 0.1)
    veg_cells = list(zip(*np.nonzero(vegetated(X60, Y60)))) if vegetated else ()
    cube = make_cube(M_full, wl, f, x0=x0, y0=y0, res=60.0, vegetated=veg_cells)
    spectral_out = settings.cache_dir / f"{facility}_spectral.zarr"
    unmixing.run_spectral_pipeline(write_input_zarr(cube, tmp_path / "rfl.zarr"), spectral_out, settings=settings)

    # Phase 3: DISP-S1 stack with an accelerating block.
    stack = tmp_path / "stack"
    stack.mkdir()
    shape = (60, 60)
    origin = TO_LONLAT.transform(gx0 - 300, gy1 + 300)
    dx0, dy0 = TO_UTM.transform(*origin)
    DX, DY = np.meshgrid(dx0 + (np.arange(shape[1]) + 0.5) * RES, dy0 - (np.arange(shape[0]) + 0.5) * RES)
    times = pd.date_range("2023-01-01", periods=60, freq="12D")
    ty = ((times - times[0]).days / kin.DAYS_PER_YEAR).to_numpy()
    tf = ty[-1] + 30 / kin.DAYS_PER_YEAR
    accel_mm = (1 / 0.03) * (np.log(tf - ty) - np.log(tf))
    rng = np.random.default_rng(0)
    moving = block(DX, DY)
    coherence = np.where(decorrelated(DX, DY), 0.4, 0.9) if decorrelated else np.full(shape, 0.9)
    for i in range(1, times.size):
        d = rng.normal(0, 0.3, shape) / 1000.0 + np.where(moving, accel_mm[i] / 1000.0, 0.0)
        write_disp(stack / disp_name(times[0].strftime("%Y%m%d"), times[i].strftime("%Y%m%d")), d, coherence, origin=origin)
    kin_out = settings.cache_dir / f"{facility}_kinematics.zarr"
    kin.run_kinematics(facility, bbox, kin_out, settings=settings, stack_paths=sorted(stack.glob("*.nc")), hyperparameters="hybrid")

    # Phase 5.
    summary = cp.run_coupling(facility, spectral_out, kin_out, hydro_out, settings.cache_dir / f"{facility}_coupled.zarr")
    return {"settings": settings, "summary": summary, "block": block, "toe": toe, "grid_bounds": (gx0, gy0, gx1, gy1)}


def test_full_chain_unmix_kinematics_hydrology_couple(tmp_path):
    """Synthetic inputs, real pipeline code: Phase 2-4 outputs must be valid coupler inputs."""
    chain = build_full_chain(tmp_path)
    settings, summary, block, toe = chain["settings"], chain["summary"], chain["block"], chain["toe"]
    gx0, gy0, gx1, gy1 = chain["grid_bounds"]
    ds = xr.open_zarr(settings.cache_dir / "Test_coupled.zarr", decode_coords="all")
    MX, MY = np.meshgrid(ds["x"].values, ds["y"].values)
    regime = ds["regime"].values
    inner_block = block(MX, MY) & block(MX - 60, MY) & block(MX + 60, MY) & block(MX, MY - 30) & block(MX, MY + 30)
    assert inner_block.any()
    assert (regime[inner_block] == cp.REGIME_4).mean() > 0.5
    # Phase 3 timelines (CF "days", decoded as timedelta) must arrive as days, not nanoseconds.
    days = cp.as_days(ds["failure_days_after_last_epoch"]).values[regime == cp.REGIME_4]
    assert np.all((days > 0) & (days < 365))
    assert summary["facility_hazard_level"] == "CRITICAL_TIER3"
    assert summary["setback_state"]["encroaching"] is True
    # Toe interior (its outer 30 m rows mix with background when 60 m SAI is resampled to 30 m).
    inner_toe = toe(MX, MY + 30) & toe(MX, MY - 30) & (MX > gx0 + 60) & (MX < gx1 - 60)
    assert inner_toe.any() and (regime[inner_toe] == cp.REGIME_3).mean() > 0.8
    assert summary["contains_synthetic_or_mock_inputs"] is True  # the cube carries origin "mock"
