"""Verification checks for the InSAR kinematics engine.

All displacement series are synthetic with known truth: stable and linear
creep, and accelerating creep generated from the Fukuzono model itself
(1/v = A (t_f - t), i.e. d(t) = (1/A) ln((t_f - t) / t_f)). The Brumadinho
backtest CSV written here is a synthetic stand-in in the documented schema;
the published series must be supplied as DATA_DIR/brumadinho_historic.csv.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import requests
import xarray as xr
from pyproj import Transformer

from app import config, main
from app.config import Settings
from app.pipelines import ingestion
from app.pipelines import kinematics as k

SIERRITA = (-111.12, 31.83, -111.04, 31.90)
BRUMADINHO = (-44.15, -20.14, -44.10, -20.10)
TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32612", always_xy=True)

TIMES = pd.date_range("2023-01-01", periods=120, freq="6D")
T_YEARS = ((TIMES - TIMES[0]).days / k.DAYS_PER_YEAR).to_numpy()


def fukuzono_series(lead_days=30.0, A=0.03):
    """Subsiding (negative LOS) creep with 1/|v| = A (t_f - t); t_f = last epoch + lead_days."""
    tf = T_YEARS[-1] + lead_days / k.DAYS_PER_YEAR
    return (1.0 / A) * (np.log(tf - T_YEARS) - np.log(tf))


def noisy(y, sigma, seed):
    return y + np.random.default_rng(seed).normal(0.0, sigma, y.shape)


@pytest.fixture
def settings(tmp_path):
    return Settings(DATA_DIR=str(tmp_path / "data"))


# --------------------------------------------------------------------------- #
# GP maths
# --------------------------------------------------------------------------- #


def test_matern_derivatives_match_finite_differences():
    tau = np.linspace(-2.0, 2.0, 81)
    h = 1e-5
    for derivative in (1, 2):
        analytic = k.matern52(tau, 1.7, 0.4, derivative)
        numeric = (k.matern52(tau + h, 1.7, 0.4, derivative - 1) - k.matern52(tau - h, 1.7, 0.4, derivative - 1)) / (2 * h)
        np.testing.assert_allclose(analytic, numeric, atol=1e-5)
    # Prior variances of f' and f'' (s q^2/3 and s q^4) from the Taylor expansion at 0.
    q = np.sqrt(5.0) / 0.4
    eps = 1e-3
    assert -k.matern52(np.array([0.0]), 1.7, 0.4, 2)[0] == pytest.approx(1.7 * q**2 / 3)
    fourth = (k.matern52(np.array([2 * eps]), 1.7, 0.4, 2) - 2 * k.matern52(np.array([eps]), 1.7, 0.4, 2) + k.matern52(np.array([0.0]), 1.7, 0.4, 2)) / eps**2
    assert fourth[0] == pytest.approx(1.7 * q**4, rel=0.05)


def test_gp_posterior_matches_scikit_learn():
    from sklearn.gaussian_process import GaussianProcessRegressor

    y = noisy(-8.0 * T_YEARS + 3 * np.sin(4 * T_YEARS), 1.0, 0)
    ys, _, _ = k._standardise(y)
    tc = T_YEARS - T_YEARS.mean()
    h = k.fit_hyperparameters(tc, ys)
    gp = GaussianProcessRegressor(k._sklearn_kernel(), optimizer=None)
    gp.kernel = gp.kernel.set_params(
        k1__k1__k1__constant_value=h.amplitude, k1__k1__k2__length_scale=h.length_scale,
        k1__k2__k1__constant_value=h.linear_scale, k1__k2__k2__sigma_0=h.sigma_0, k2__noise_level=h.noise,
    )
    gp.fit(tc[:, None], ys)
    t_pred = np.linspace(tc[0], tc[-1], 57)
    mean, std = gp.predict(t_pred[:, None], return_std=True)
    post = k.gp_posterior(tc, ys[:, None], t_pred, h)
    np.testing.assert_allclose(post["f_mean"][:, 0], mean, atol=1e-8)
    np.testing.assert_allclose(post["f_var"] + h.noise, std**2, atol=1e-8)  # sklearn includes the white noise
    step = 1e-4
    fd = (gp.predict((t_pred + step)[:, None]) - gp.predict((t_pred - step)[:, None])) / (2 * step)
    np.testing.assert_allclose(post["df_mean"][:, 0], fd, rtol=1e-4, atol=1e-6)


# --------------------------------------------------------------------------- #
# Stable, linear and accelerating series
# --------------------------------------------------------------------------- #


def test_stable_series_runs_no_prediction():
    res = k.analyze_series(TIMES, noisy(-1.0 * T_YEARS, 1.0, 1)[:, None], np.array([0.9]))
    assert res["status"][0] == k.STABLE
    v, v2s = res["velocity"][:, 0], 2 * res["velocity_std"][:, 0]
    assert np.all(np.isfinite(v)) and abs(v[-1]) < k.VELOCITY_THRESHOLD_MM_YR
    assert abs(np.median(v) + 1.0) < np.median(v2s)  # true -1 mm/yr inside the 95% band
    assert np.all(np.isnan(res["inverse_velocity"][:, 0]))
    assert np.isnan(res["instability_years_after_last_epoch"][0])


def test_linear_creep_velocity_and_zero_acceleration():
    res = k.analyze_series(TIMES, noisy(-10.0 * T_YEARS, 1.0, 2)[:, None], np.array([0.9]))
    assert res["status"][0] == k.LINEAR
    v, v_std = res["velocity"][:, 0], res["velocity_std"][:, 0]
    a, a_std = res["acceleration"][:, 0], res["acceleration_std"][:, 0]
    mid = slice(10, -10)
    assert np.all(np.abs(v[mid] + 10.0) < 2 * v_std[mid] + 0.5)
    assert np.all(np.abs(a[mid]) < 2 * a_std[mid])
    assert np.isnan(res["instability_years_after_last_epoch"][0])


def test_accelerating_creep_produces_trend_timeline():
    lead = 30.0
    res = k.analyze_series(TIMES, noisy(fukuzono_series(lead, 0.03), 0.5, 0)[:, None], np.array([0.9]))
    assert res["status"][0] == k.ACCEL_FORECAST
    v, a = res["velocity"][-1, 0], res["acceleration"][-1, 0]
    assert v < -k.VELOCITY_THRESHOLD_MM_YR and a < -2 * res["acceleration_std"][-1, 0]
    lam = res["inverse_velocity"][:, 0]
    assert np.all(np.diff(lam[-5:]) < 0)  # inverse velocity heading towards zero
    days = res["instability_years_after_last_epoch"][0] * k.DAYS_PER_YEAR
    days_2s = 2 * res["instability_years_std"][0] * k.DAYS_PER_YEAR
    assert res["fukuzono_A"][0] > 0 and res["fukuzono_lambda_0"][0] > 0
    assert days_2s > 0
    # Characterised limitation (see IMPLEMENTATION_STATUS.md): GP smoothing lags the final
    # acceleration, so the timeline is late. Pin the behaviour so changes are noticed.
    assert lead < days < 3 * lead


def test_forecast_bias_is_late_for_accelerating_creep():
    """Across seeds the estimated timeline is later than the true t_f (smoothing lag)."""
    for seed in range(3):
        res = k.analyze_series(TIMES, noisy(fukuzono_series(30.0, 0.03), 0.5, seed)[:, None], np.array([0.9]))
        assert res["status"][0] == k.ACCEL_FORECAST
        assert res["instability_years_after_last_epoch"][0] * k.DAYS_PER_YEAR > 30.0


def test_low_coherence_is_never_modelled(monkeypatch):
    calls = []
    real_fit = k.fit_hyperparameters
    monkeypatch.setattr(k, "fit_hyperparameters", lambda t, y: calls.append(y.copy()) or real_fit(t, y))
    accel = noisy(fukuzono_series(30.0, 0.03), 0.5, 0)
    stable = noisy(-1.0 * T_YEARS, 1.0, 1)
    disp = np.column_stack([accel, stable, accel])
    coherence = np.array([0.5, 0.9, np.nan])  # low, good, unknown

    res = k.analyze_series(TIMES, disp, coherence, hyperparameters="per_pixel")
    assert list(res["status"]) == [k.UNOBSERVABLE, k.STABLE, k.UNOBSERVABLE]
    assert len(calls) == 1  # only the coherent series was fitted
    for i in (0, 2):
        assert np.all(np.isnan(res["velocity"][:, i])) and np.all(np.isnan(res["inverse_velocity"][:, i]))
        assert np.isnan(res["instability_years_after_last_epoch"][i])


def test_short_series_is_no_data():
    res = k.analyze_series(TIMES[:5], np.zeros((5, 1)), np.array([0.9]))
    assert res["status"][0] == k.NO_DATA


def test_hybrid_mode_fits_movers_individually():
    """Quiet ground shares hyperparameters; moving/accelerating series match per-pixel fits."""
    rng = np.random.default_rng(5)
    quiet = [noisy(np.zeros_like(T_YEARS), 1.0, s) for s in range(15)]
    true_v = rng.uniform(5, 15, 4)
    movers = [noisy(-v * T_YEARS, 1.0, 100 + s) for s, v in enumerate(true_v)]
    accel = [noisy(fukuzono_series(30.0, 0.03), 0.5, 0)]
    disp = np.column_stack(quiet + movers + accel)
    coherence = np.full(disp.shape[1], 0.9)
    per = k.analyze_series(TIMES, disp, coherence, hyperparameters="per_pixel")
    hyb = k.analyze_series(TIMES, disp, coherence, hyperparameters="hybrid")
    assert hyb["meta"]["hyperparameter_mode"] == "hybrid"
    np.testing.assert_array_equal(hyb["status"], per["status"])
    # Linear movers share "moving ground" hyperparameters; their velocity bands still cover the truth.
    v, v_std = hyb["velocity"][10:-10, 15:19], hyb["velocity_std"][10:-10, 15:19]
    assert np.all(np.abs(v + true_v) < 2 * v_std + 0.5)
    # The forecast-bearing series is refitted alone, so it matches per-pixel (to optimiser precision).
    assert hyb["status"][-1] == k.ACCEL_FORECAST
    np.testing.assert_allclose(hyb["velocity"][:, -1], per["velocity"][:, -1], rtol=1e-3)
    np.testing.assert_allclose(hyb["instability_years_after_last_epoch"][-1], per["instability_years_after_last_epoch"][-1], rtol=1e-2)
    assert np.all(np.abs(hyb["velocity"][10:-10, :15]) < k.VELOCITY_THRESHOLD_MM_YR)
    assert set(hyb["status"][:15]) == {k.STABLE}


def test_bayesian_fit_recovers_line_and_inflates_for_scatter():
    x = np.linspace(-0.1, 0.0, 5)
    beta, cov = k.bayesian_linear_fit(x, 0.02 - 0.5 * x, np.full(5, 1e-3))
    np.testing.assert_allclose(beta, [0.02, -0.5], atol=1e-10)
    scattered = 0.02 - 0.5 * x + np.array([0.01, -0.01, 0.01, -0.01, 0.01])
    _, cov_scatter = k.bayesian_linear_fit(x, scattered, np.full(5, 1e-3))
    assert cov_scatter[1, 1] > 10 * cov[1, 1]


# --------------------------------------------------------------------------- #
# DISP-S1 stack loading
# --------------------------------------------------------------------------- #


def disp_name(ref, sec, gen="20250101T000000Z", frame=12345):
    return f"OPERA_L3_DISP-S1_IW_F{frame:05d}_VV_{ref}T120000Z_{sec}T120000Z_v1.0_{gen}.nc"


def write_disp(path: Path, disp_m: np.ndarray, coherence: np.ndarray, res=30.0):
    x0, y0 = TO_UTM.transform(-111.13, 31.91)
    ny, nx = disp_m.shape
    x = x0 + (np.arange(nx) + 0.5) * res
    y = y0 - (np.arange(ny) + 0.5) * res
    ds = xr.Dataset(
        {"displacement": (("y", "x"), disp_m.astype(np.float32)), "temporal_coherence": (("y", "x"), coherence.astype(np.float32))},
        coords={"x": x, "y": y},
    ).rio.write_crs("EPSG:32612")
    ds.to_netcdf(path, engine="h5netcdf")
    return path


@pytest.fixture
def disp_stack(tmp_path):
    """Two ministacks: ref 0101 -> (0113, 0125); ref 0125 -> (0206, 0218). 380x380 px covers the bbox."""
    folder = tmp_path / "stack"
    folder.mkdir()
    shape = (380, 380)
    ones = np.ones(shape)
    coh = np.full(shape, 0.9)
    coh_low = coh.copy()
    coh_low[100:120, 100:120] = 0.3  # inside the bbox
    specs = [("20230101", "20230113", 0.001), ("20230101", "20230125", 0.003), ("20230125", "20230206", 0.002), ("20230125", "20230218", 0.005)]
    for i, (ref, sec, value) in enumerate(specs):
        write_disp(folder / disp_name(ref, sec), value * ones, coh_low if i == 2 else coh)
    # Older production of a pair: must be superseded by the newer generation above.
    write_disp(folder / disp_name("20230101", "20230113", gen="20240101T000000Z"), 99 * ones, coh)
    return folder


def test_disp_stack_chains_ministacks_in_mm(disp_stack):
    ds = k.load_disp_stack(sorted(disp_stack.glob("*.nc")), SIERRITA)
    assert [str(t)[:10] for t in ds.time.values] == ["2023-01-01", "2023-01-13", "2023-01-25", "2023-02-06", "2023-02-18"]
    centre = ds["displacement"].isel(y=ds.sizes["y"] // 2, x=ds.sizes["x"] // 2).values
    np.testing.assert_allclose(centre, [0.0, 1.0, 3.0, 5.0, 8.0], atol=1e-4)  # mm, chained across the reference change
    assert ds.rio.crs.to_epsg() == 32612
    assert float(ds["temporal_coherence"].min()) < k.COHERENCE_THRESHOLD  # min over the stack keeps the bad patch
    assert ds.attrs["frame"] == 12345 and ds.attrs["n_granules"] == 4


def test_disp_stack_rejects_mixed_frames_and_gaps(tmp_path):
    a = tmp_path / disp_name("20230101", "20230113")
    b = tmp_path / disp_name("20230101", "20230125", frame=99999)
    with pytest.raises(k.KinematicsError, match="several frames"):
        k.select_disp_files([a, b])
    gap = tmp_path / disp_name("20230301", "20230313")
    with pytest.raises(k.KinematicsError, match="disconnected"):
        k.load_disp_stack([a, gap], SIERRITA)  # rejected from the filenames, before any raster is read
    with pytest.raises(k.KinematicsError, match="Not an OPERA"):
        k.parse_disp_filename(tmp_path / "random.nc")


def test_download_disp_stack_picks_best_frame(settings, monkeypatch):
    import earthaccess

    def granule(frame, sec, rect):
        name = disp_name("20230101", sec, frame=frame)[:-3]
        w, s, e, n = rect
        return {"umm": {"GranuleUR": name, "RelatedUrls": [{"Type": "GET DATA", "URL": f"https://x/{name}.nc"}],
                        "SpatialExtent": {"HorizontalSpatialDomain": {"Geometry": {"BoundingRectangles": [
                            {"WestBoundingCoordinate": w, "SouthBoundingCoordinate": s, "EastBoundingCoordinate": e, "NorthBoundingCoordinate": n}]}}}}}

    full, partial = (-112, 31, -110, 33), (-111.10, 31.85, -110, 33)
    found = [granule(11111, "20230113", partial), granule(22222, "20230113", full), granule(22222, "20230125", full)]
    got = {}

    class Auth:
        authenticated = True

    monkeypatch.setattr(ingestion, "_logged_in", False)
    monkeypatch.setattr(earthaccess, "search_data", lambda **kw: found)
    monkeypatch.setattr(earthaccess, "login", lambda strategy="all", persist=False: Auth())
    monkeypatch.setattr(earthaccess, "download", lambda links, local_path, **_: got.setdefault("links", list(links)) and [str(Path(local_path) / Path(l).name) for l in links])
    s = Settings(DATA_DIR=settings.DATA_DIR, EARTHDATA_USERNAME="u", EARTHDATA_PASSWORD="p")
    paths = ingestion.download_disp_stack(SIERRITA, s)
    assert len(paths) == 2 and all("F22222" in l for l in got["links"])
    with pytest.raises(ingestion.CoverageError):
        ingestion.download_disp_stack(BRUMADINHO, s)


# --------------------------------------------------------------------------- #
# Backtest (non-CONUS) and outputs
# --------------------------------------------------------------------------- #


def write_backtest_csv(path: Path):
    """Synthetic stand-in for the published Brumadinho series (schema test only)."""
    rows = []
    for pid, series, coh in (("toe", noisy(fukuzono_series(30.0, 0.03), 0.5, 0), 0.8),
                             ("crest", noisy(-1.0 * T_YEARS, 1.0, 1), 0.85),
                             ("decorrelated", noisy(fukuzono_series(30.0, 0.03), 0.5, 2), 0.4)):
        rows += [{"date": d.date().isoformat(), "point_id": pid, "displacement_mm": v, "temporal_coherence": coh,
                  "longitude": -44.12, "latitude": -20.12, "source": "SYNTHETIC test series"} for d, v in zip(TIMES, series)]
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def test_backtest_outside_conus_uses_historic_csv_without_queries(settings, monkeypatch):
    import earthaccess

    monkeypatch.setattr(earthaccess, "search_data", lambda **_: pytest.fail("CMR must not be queried outside CONUS"))
    monkeypatch.setattr(earthaccess, "download", lambda *a, **_: pytest.fail("no downloads outside CONUS"))
    write_backtest_csv(settings.data_path / "brumadinho_historic.csv")
    out = settings.cache_dir / "Brumadinho_kinematics.zarr"

    summary = k.run_kinematics("Brumadinho", BRUMADINHO, out, settings=settings)

    ds = xr.open_zarr(out)
    status = dict(zip(ds["point"].values, ds["kinematic_status"].values))
    assert status == {"crest": k.STABLE, "decorrelated": k.UNOBSERVABLE, "toe": k.ACCEL_FORECAST}
    toe = ds.sel(point="toe")
    assert np.isfinite(toe["instability_days_after_last_epoch"].values)
    assert toe["instability_time_lower"].values < toe["instability_time"].values < toe["instability_time_upper"].values
    assert "NOT a guaranteed" in ds.attrs["disclaimer"] and "NOT a guaranteed" in summary["disclaimer"]
    assert ds.attrs["source"].startswith("historical backup") and "SYNTHETIC" in ds.attrs["source_citation"]
    assert np.all(np.isnan(ds.sel(point="decorrelated")["velocity"].values))
    for name in ("velocity_lower", "velocity_upper", "acceleration_lower", "acceleration_upper", "inverse_velocity_lower", "inverse_velocity_upper", "displacement_smooth_lower"):
        assert name in ds


def test_backtest_without_csv_fails_clearly(settings):
    with pytest.raises(FileNotFoundError, match="brumadinho_historic.csv"):
        k.run_kinematics("Brumadinho", BRUMADINHO, settings.cache_dir / "b.zarr", settings=settings)


def test_historic_csv_without_coherence_is_marked(tmp_path):
    path = tmp_path / "h.csv"
    pd.DataFrame({"date": TIMES.date.astype(str), "displacement_mm": -10.0 * T_YEARS}).to_csv(path, index=False)
    ds = k.load_historic_csv(path)
    assert ds.sizes["point"] == 1 and "assumed pre-filtered" in ds.attrs["coherence_source"]


@pytest.fixture
def cli_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "cli_data"))
    monkeypatch.setattr(ingestion, "_logged_in", False)
    config.get_settings.cache_clear()
    yield config.get_settings()
    config.get_settings.cache_clear()


def test_cli_kinematics_raster_writes_sierrita_zarr(cli_settings, tmp_path, monkeypatch):
    """12-epoch synthetic DISP-S1 stack: an accelerating block, stable ground, a decorrelated patch."""
    import earthaccess

    monkeypatch.setattr(earthaccess, "search_data", lambda **_: (_ for _ in ()).throw(requests.exceptions.ConnectionError("offline")))
    stack = cli_settings.raw_dir / k.DISP_DATASET_ID
    stack.mkdir(parents=True)
    times = pd.date_range("2023-01-01", periods=60, freq="12D")
    ty = ((times - times[0]).days / k.DAYS_PER_YEAR).to_numpy()
    tf = ty[-1] + 30 / k.DAYS_PER_YEAR
    accel_mm = (1 / 0.03) * (np.log(tf - ty) - np.log(tf))
    shape = (380, 380)
    rng = np.random.default_rng(0)
    coh = np.full(shape, 0.9)
    coh[300:, 300:] = 0.4
    for i in range(1, times.size):
        d = np.zeros(shape) + rng.normal(0, 0.3, shape) / 1000.0
        d[180:192, 180:192] += accel_mm[i] / 1000.0  # accelerating 12x12 block (metres)
        write_disp(stack / disp_name(times[0].strftime("%Y%m%d"), times[i].strftime("%Y%m%d")), d, coh)

    assert main.main(["kinematics", "--facility", "Sierrita", "--download", "--hyperparameters", "hybrid"]) == 0

    ds = xr.open_zarr(cli_settings.cache_dir / "Sierrita_kinematics.zarr", decode_coords="all")
    assert ds.rio.crs.to_epsg() == 32612 and ds.attrs["hyperparameter_mode"] == "hybrid"
    status = ds["kinematic_status"].values
    counts = {s: int((status == s).sum()) for s in np.unique(status)}
    assert counts.get(k.UNOBSERVABLE, 0) > 0 and counts.get(k.STABLE, 0) > 0 and counts.get(k.ACCEL_FORECAST, 0) > 0
    assert np.all(np.isnan(ds["velocity"].values[:, status == k.UNOBSERVABLE]))
    assert np.all(np.isnan(ds["instability_days_after_last_epoch"].values[status != k.ACCEL_FORECAST]))
    logs = list(cli_settings.log_dir.glob("Sierrita_kinematics_*.json"))
    assert len(logs) == 1 and "disclaimer" in json.loads(logs[0].read_text())
    assert "Earthdata unreachable" in next(cli_settings.log_dir.glob("kinematics_*.log")).read_text()


def test_cli_kinematics_brumadinho_needs_csv(cli_settings):
    assert main.main(["kinematics", "--facility", "Brumadinho"]) == 1
