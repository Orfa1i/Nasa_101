"""Phase 10 scenario routes: Brumadinho backtest, stress-test simulator, audit status.

The backtest CSVs written here are SYNTHETIC stand-ins in the documented schema (the published
series is not bundled). The stress test runs on the synthetic Phase 2-5 chain of test_api.py.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from fastapi.testclient import TestClient

from app.api import scenarios as S
from app.api.endpoints import create_app
from app.api.schemas import BacktestResponse, StressTestResponse
from app.config import Settings
from app.pipelines import kinematics as kin
from tests.test_api import TO_LONLAT, _site_grid, block_cell, chain, client  # noqa: F401  (module fixtures)

COLLAPSE = pd.Timestamp("2019-01-25")


def write_collapse_csv(path: Path, *, source: str = "SYNTHETIC stand-in for the Brumadinho series (test)", zone_col: bool = False) -> Path:
    """Toe: tertiary creep with 1/v -> 0 at the collapse date; crest: slow linear settlement. 6-day cadence."""
    times = pd.date_range("2018-01-04", "2019-01-22", freq="6D")
    ty = ((times - times[0]).days / kin.DAYS_PER_YEAR).to_numpy()
    tf = (COLLAPSE - times[0]).days / kin.DAYS_PER_YEAR
    rng = np.random.default_rng(7)
    toe = (1 / 0.02) * (np.log(tf - ty) - np.log(tf)) + rng.normal(0, 0.3, ty.size)
    crest = -2.0 * ty + rng.normal(0, 0.3, ty.size)
    rows = []
    for pid, zone, lon, lat, series in (("P7", "toe", -44.121, -20.119, toe), ("P2", "crest", -44.118, -20.116, crest)):
        for d, v in zip(times, series):
            row = {"date": d.date().isoformat(), "point_id": pid if zone_col else zone, "displacement_mm": v,
                   "temporal_coherence": 0.85, "longitude": lon, "latitude": lat, "source": source}
            if zone_col:
                row["zone"] = zone
            rows.append(row)
    # an epoch after the collapse must be ignored
    rows.append({**rows[-1], "date": "2019-02-03", "displacement_mm": -500.0})
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


@pytest.fixture
def bt_client(tmp_path):
    settings = Settings(DATA_DIR=str(tmp_path / "data"))
    with TestClient(create_app(settings)) as c:
        yield c, settings


# --------------------------------------------------------------------------- #
# Backtest
# --------------------------------------------------------------------------- #


def test_backtest_without_csv_explains_what_is_needed(bt_client):
    c, _ = bt_client
    r = c.get("/api/backtest/brumadinho")
    assert r.status_code == 404
    d = r.json()["detail"]
    assert "not bundled" in d["message"] and "South America" in d["message"]
    assert d["expected_file"] == "DATA_DIR/brumadinho_historic.csv" and {"date", "displacement_mm"} <= set(d["expected_columns"])


def test_backtest_on_toe_series(bt_client):
    c, settings = bt_client
    write_collapse_csv(settings.data_path / "brumadinho_historic.csv")
    b = BacktestResponse.model_validate(c.get("/api/backtest/brumadinho").json())
    # A synthetic file is never presented as the historical dataset.
    assert b.historical is False and b.dataset_label.startswith("SYNTHETIC STAND-IN") and b.source.synthetic_or_mock
    assert b.point_id == "toe" and {p.point_id for p in b.points} == {"toe", "crest"}
    assert b.collapse_date == COLLAPSE.to_pydatetime() and b.window_days == 40
    assert pd.Timestamp(b.series.times[-1]) < COLLAPSE  # the post-collapse epoch is dropped
    assert b.series.status == "accelerating-forecast" and b.series.fukuzono is not None
    o = b.outcome
    last = pd.date_range("2018-01-04", "2019-01-22", freq="6D")[-1]
    assert o.lead_days == pytest.approx((COLLAPSE - last).days) and o.estimate is not None
    assert abs(o.error_days) < 30  # inverse velocity points close to the collapse on this idealised series
    # The sweep re-issues the forecast at each epoch of the 40-day window, chronologically.
    assert 1 <= len(b.sweep) <= S.MAX_SWEEP
    issued = [pd.Timestamp(s.issued) for s in b.sweep]
    assert issued == sorted(issued) and all(COLLAPSE - pd.Timedelta(days=40) <= t < COLLAPSE for t in issued)
    assert "NOT a guaranteed" in b.disclaimer


def test_backtest_cutoff_zone_column_and_errors(bt_client):
    c, settings = bt_client
    write_collapse_csv(settings.data_path / "brumadinho_historic.csv", zone_col=True, source="Example et al. (published series)")
    b = BacktestResponse.model_validate(c.get("/api/backtest/brumadinho", params={"cutoff_days": 30, "sweep": False}).json())
    assert b.point_id == "P7" and next(p for p in b.points if p.point_id == "P7").zone == "toe"  # chosen by the zone column
    assert b.historical is True and b.dataset_label == "HISTORICAL DATASET: Example et al. (published series)"
    assert pd.Timestamp(b.outcome.issued) <= COLLAPSE - pd.Timedelta(days=30) and b.outcome.lead_days >= 30 and b.sweep == []
    crest = BacktestResponse.model_validate(c.get("/api/backtest/brumadinho", params={"point": "P2", "sweep": False}).json())
    assert crest.series.status in {"stable-below-2mm-yr", "moving-linear"} and crest.outcome.estimate is None
    assert c.get("/api/backtest/brumadinho", params={"point": "nope"}).status_code == 404
    assert c.get("/api/backtest/brumadinho", params={"cutoff_days": 360}).status_code == 422
    assert c.get("/api/backtest/brumadinho", params={"window_days": 2}).status_code == 422


# --------------------------------------------------------------------------- #
# Stress test
# --------------------------------------------------------------------------- #


def stress(client, intensity, *, x_off=450, y_off=500):
    gx0, _, _, gy1 = _site_grid()
    lon, lat = TO_LONLAT.transform(gx0 + x_off, gy1 - y_off)
    r = client.get("/api/facilities/sierrita/stress-test", params={"lat": lat, "lon": lon, "intensity": intensity})
    assert r.status_code == 200, r.text
    return StressTestResponse.model_validate(r.json())


def test_stress_test_zero_intensity_reproduces_the_baseline(chain, client):
    s = stress(client, 0.0)
    assert s.simulated is True and "SIMULATED" in s.label
    assert s.scenario_regime_counts == s.baseline_regime_counts and s.scenario_hazard_level == s.baseline_hazard_level
    assert s.scenario.regime == s.baseline.regime and s.scenario.sai == pytest.approx(s.baseline.sai, abs=1e-6)
    assert s.scenario_displacement_mm == pytest.approx(s.baseline_displacement_mm, abs=1e-6)
    assert s.setback.scenario_d_crest_m == s.setback.baseline_d_crest_m


def test_stress_test_full_intensity_drives_the_real_pipelines(chain, client):
    before = {p.name: p.stat().st_mtime for p in chain["settings"].cache_dir.glob("*.zarr")}
    s = stress(client, 1.0)
    p = s.parameters
    assert p.intensity == 1.0 and p.delta_sai_max == pytest.approx(0.35) and p.creep_lead_days == pytest.approx(30.0)
    # Seepage: SAI raised above the 0.25 threshold at the centre cell.
    assert s.baseline.sai < 0.25 < s.scenario.sai
    # Creep: the Phase 3 GP + Fukuzono re-analysis sees tertiary creep with a near-term trend change.
    assert s.baseline.kinematic_status != "accelerating-forecast" and s.scenario.kinematic_status == "accelerating-forecast"
    f = s.scenario_series.fukuzono
    assert f is not None and pd.Timestamp(s.times[-1]) <= pd.Timestamp(f.crossing_estimate) <= pd.Timestamp(s.times[-1]) + pd.Timedelta(days=150)
    assert min(s.scenario_displacement_mm) < min(s.baseline_displacement_mm) - 20
    # Setback: the last valid epoch is pulled to 5 m from the crest, raising a proximity alert.
    assert s.setback.scenario_d_crest_m[-1] == pytest.approx(5.0) and "crest_proximity" in s.setback.scenario_alerts[-1]
    # Phase 5 on the perturbed products: the mid-slope cell becomes active instability.
    assert s.baseline.regime == 1 and s.scenario.regime == 4 and s.scenario.alert_tier == 3
    assert s.scenario_regime_counts["regime4-active-instability-tier3-critical"] > s.baseline_regime_counts["regime4-active-instability-tier3-critical"]
    # Test overlay is labelled simulated; the cache on disk is untouched.
    assert s.regime_matrix.source.synthetic_or_mock and "SIMULATED" in s.regime_matrix.note and len(s.footprint) == 33
    assert {p.name: p.stat().st_mtime for p in chain["settings"].cache_dir.glob("*.zarr")} == before


def test_stress_test_is_monotonic_in_intensity(chain, client):
    runs = [stress(client, h) for h in (0.0, 0.5, 1.0)]
    sai = [r.scenario.sai for r in runs]
    last_d = [r.setback.scenario_d_crest_m[-1] for r in runs]
    assert sai[0] < sai[1] < sai[2] and last_d[0] > last_d[1] > last_d[2]
    assert runs[1].parameters.creep_lead_days == pytest.approx(105.0)


def test_stress_test_limits(chain, client):
    gx0, _, _, gy1 = _site_grid()
    lon, lat = TO_LONLAT.transform(gx0 + 450, gy1 - 500)
    assert client.get("/api/facilities/bingham/stress-test", params={"lat": 40.52, "lon": -112.15}).status_code == 422  # Utah
    r = client.get("/api/facilities/brumadinho/stress-test", params={"lat": -20.12, "lon": -44.12})
    assert r.status_code == 422 and "Arizona" in r.json()["detail"]
    assert client.get("/api/facilities/sierrita/stress-test", params={"lat": 32.5, "lon": -111.1}).status_code == 422
    assert client.get("/api/facilities/sierrita/stress-test", params={"lat": lat, "lon": lon, "intensity": 1.5}).status_code == 422
    assert client.get("/api/facilities/mission/stress-test", params={"lat": 31.975, "lon": -111.04}).status_code == 404  # AZ, no products
    assert S.in_arizona({"bbox_xmin": -111.12, "bbox_ymin": 31.83, "bbox_xmax": -111.04, "bbox_ymax": 31.9})


def test_creep_injection_follows_inverse_velocity_law():
    times = pd.date_range("2024-01-01", periods=60, freq="6D")
    d, onset, lead = S.creep_displacement(times, 1.0)
    assert lead == 30.0 and onset == times[-1] - pd.Timedelta(days=180)
    assert np.all(d[times <= onset] == 0) and np.all(np.diff(d) <= 0)
    t = ((times - onset).days / kin.DAYS_PER_YEAR).to_numpy()
    v = np.gradient(d, t)
    late = times > onset + pd.Timedelta(days=30)
    tf = (180 + 30) / kin.DAYS_PER_YEAR
    assert 1 / np.abs(v[late]) == pytest.approx(S.CREEP_A_YR_MM * (tf - t[late]), rel=0.1)  # 1/v = A (t_f - t)
    assert np.all(S.creep_displacement(times, 0.0)[0] <= 0)


# --------------------------------------------------------------------------- #
# Audit status
# --------------------------------------------------------------------------- #


def test_audit_status_reports_available_modes(client, monkeypatch):
    from app.agent import copilot

    monkeypatch.setattr(copilot, "credentials_available", lambda: False)
    s = client.get("/api/audit/status").json()
    assert s["llm_available"] is False and s["auto_mode_uses"] == "deterministic" and s["modes"] == ["auto", "deterministic"]
    assert "ICMM" in s["standard"] and s["model"]
