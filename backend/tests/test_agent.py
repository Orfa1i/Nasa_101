"""Verification checks for the GISTM audit copilot.

* The tools run against real pipeline outputs (a synthetic site inside the
  Sierrita bbox pushed through the real Phase 2-5 pipelines) and are checked
  against the raw Zarr values, recomputed independently here.
* The grounding validator is checked against tampered briefs.
* The Claude tool-calling loop is driven offline by ``ScriptedClaude``, which
  returns real ``anthropic.types.beta.BetaMessage`` objects and records each
  request, so the loop, request shape, parallel tool results, nudges and
  refusals are all exercised. A live run against the Claude API is opt-in
  (``LITHOCREEP_LIVE_LLM=1`` plus credentials).
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from anthropic.types.beta import BetaMessage
from fastapi.testclient import TestClient

from app.agent import copilot as C
from app.agent.tools import REQUIRED_DISCLAIMER, REQUIRED_TOOLS, TOOL_DEFINITIONS, InspectionTools, ToolError
from app.api.endpoints import create_app
from app.api.schemas import AuditResponse
from app.config import Settings
from app.database import Database
from app.pipelines import kinematics as kin
from tests.test_coupling import build_full_chain

SITE = (-111.11, 31.86, -111.10, 31.87)
QUERY = "Generate regulatory GISTM inspection brief for Sierrita Complex"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("agent")
    settings = Settings(DATA_DIR=str(root / "data"))
    build_full_chain(root, facility="Sierrita", bbox=SITE, settings=settings)
    db = Database(settings.data_path / "agent.duckdb")
    db.initialize(settings)
    yield {"settings": settings, "db": db, "tools": InspectionTools(settings, db)}
    db.close()


@pytest.fixture(scope="module")
def tools(env):
    return env["tools"]


@pytest.fixture(scope="module")
def reference_brief(tools):
    return C.compose_brief(tools.dashboard_validation_query("sierrita"))


# --------------------------------------------------------------------------- #
# Scripted Claude (real SDK message types, no network)
# --------------------------------------------------------------------------- #


def message(content: list[dict[str, Any]], stop_reason: str, **extra) -> BetaMessage:
    return BetaMessage.model_validate({
        "id": f"msg_{abs(hash(json.dumps(content, default=str))) % 10**8}", "type": "message", "role": "assistant",
        "model": C.DEFAULT_MODEL, "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 20}, **extra,
    })


def tool_call(name: str, args: dict[str, Any], i: int) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_{name}_{i}", "name": name, "input": args}


def final(brief: dict[str, Any]) -> BetaMessage:
    return message([{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": json.dumps(brief)}], "end_turn")


class ScriptedClaude:
    """Stands in for anthropic.Anthropic: replays scripted turns and records every request."""

    def __init__(self, turns: list[Callable[[dict[str, Any]], BetaMessage] | BetaMessage]):
        self.turns = list(turns)
        self.requests: list[dict[str, Any]] = []
        self.beta = self
        self.messages = self

    def create(self, **kwargs: Any) -> BetaMessage:
        self.requests.append(copy.copy(kwargs) | {"messages": list(kwargs["messages"])})
        turn = self.turns.pop(0)
        return turn(kwargs) if callable(turn) else turn


def four_tools(fid: str = "sierrita", tools_: tuple[str, ...] = REQUIRED_TOOLS) -> BetaMessage:
    return message([tool_call(t, {"facility_id": fid}, i) for i, t in enumerate(tools_)], "tool_use")


# --------------------------------------------------------------------------- #
# Deterministic tools against the raw products
# --------------------------------------------------------------------------- #


def test_fetch_dam_attributes(tools, env):
    a = tools.fetch_dam_attributes("sierrita")
    assert a["name"] == "Freeport-McMoRan Sierrita TSF" and a["risk_classification"] == "Tier-3 / Active Instability"
    assert a["latest_alert"]["hazard_level"] == "CRITICAL_TIER3" and a["latest_alert"]["failure_date_est"]
    assert a["products_available"] == {"spectral": True, "kinematics": True, "hydrology": True, "coupled": True}
    assert a["required_disclaimer"] == REQUIRED_DISCLAIMER
    with pytest.raises(ToolError, match="Unknown facility"):
        tools.fetch_dam_attributes("atlantis")


def test_get_spectral_alerts_counts_match_raw(tools, env):
    out = tools.get_spectral_alerts("sierrita")
    ds = xr.open_zarr(env["settings"].cache_dir / "Sierrita_coupled.zarr")
    sai, flags = ds["sai"].values, ds["classification_flags"].values
    excluded = (flags & (2 | 8 | 16)) > 0
    assert out["counts"]["high_sai_pixels_all"] == int((sai > 0.25).sum())
    assert out["counts"]["high_sai_pixels_confident"] == int(((sai > 0.25) & ~excluded).sum()) > 0
    assert out["sai_max"] == round(float(np.nanmax(np.where(~excluded, sai, np.nan))), 4)
    assert out["sai_threshold"] == 0.25 and len(out["top_high_sai_pixels"]) <= 10
    top = out["top_high_sai_pixels"][0]
    assert top["sai_lower_95"] <= top["sai"] <= top["sai_upper_95"]


def test_get_kinematics_dates_and_bounds_match_raw(tools, env):
    out = tools.get_kinematics("sierrita")
    ds = xr.open_zarr(env["settings"].cache_dir / "Sierrita_kinematics.zarr")
    status = ds["kinematic_status"].values
    assert out["accelerating_pixels"] == int(np.isin(status, (kin.ACCEL_FORECAST, kin.ACCEL_NO_TREND)).sum()) > 0
    times = ds["instability_time"].values[status == kin.ACCEL_FORECAST]
    earliest = pd.Timestamp(times[~np.isnat(times)].min()).strftime("%Y-%m-%d")
    e = out["earliest_instability"]
    assert e["instability_time"] == earliest
    assert e["instability_time_lower"] < e["instability_time"] < e["instability_time_upper"]
    assert e["velocity_lower_95"] <= e["velocity_mm_yr"] <= e["velocity_upper_95"]
    listed = [p["instability_time"] for p in out["accelerating_pixels_by_earliest_date"]]
    assert listed == sorted(listed)
    assert "biased" not in out["disclaimer"] and "late" in out["known_bias"]


def test_get_hydrological_setback_history(tools):
    out = tools.get_hydrological_setback("sierrita")
    assert [s["d_crest_m"] for s in out["series"]] == [150.0, 90.0]
    assert out["latest_valid"]["delta_vs_baseline_m"] == -60.0
    assert set(out["latest_valid"]["alerts"]) == {"shrink_vs_baseline", "shrink_vs_previous"}
    assert out["encroaching"] is True and out["min_valid_d_crest_m"] == 90.0


def test_dashboard_validation_query_is_consistent(tools):
    d = tools.dashboard_validation_query("sierrita")
    assert d["all_checks_passed"], d["consistency_checks"]
    assert {c["check"] for c in d["consistency_checks"]} >= {"db_alert_matches_coupled_hazard", "regime4_iff_critical", "db_failure_date_matches_coupled"}
    principles = " ".join(i["gistm_principle"] for i in d["gistm_screening"]["candidate_issues"])
    assert "Principle 7" in principles and "Principle 13" in principles and "Principle 6" in principles
    assert any("SYNTHETIC" in lim for lim in d["gistm_screening"]["data_limitations"])
    assert d["spectral"] == tools.get_spectral_alerts("sierrita")  # the dashboard is the same tools combined


def test_missing_products_are_reported_not_invented(tools):
    d = tools.dashboard_validation_query("mission")
    assert d["risk_classification"] == "Indeterminate / Insufficient Data"
    assert not d["spectral"]["available"] and not d["kinematics"]["available"] and not d["hydrological_setback"]["available"]
    assert any(not c["passed"] for c in d["consistency_checks"])


def test_tool_definitions_are_strict():
    assert {t["name"] for t in TOOL_DEFINITIONS} == set(C.TOOL_NAMES)
    for t in TOOL_DEFINITIONS:
        assert t["strict"] is True and t["input_schema"]["additionalProperties"] is False


# --------------------------------------------------------------------------- #
# Grounding validator
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fid", ["sierrita", "mission", "bingham", "brumadinho"])
def test_deterministic_brief_validates_for_every_facility(tools, fid):
    result = C.deterministic_brief(tools, fid)
    assert result.validation["passed"] and result.generated_by == "deterministic-template"
    assert result.brief["disclaimer"] == REQUIRED_DISCLAIMER


def records_for(tools) -> list[C.ToolRecord]:
    recs = []
    for name in REQUIRED_TOOLS:
        rec = C.ToolRecord(name, {"facility_id": "sierrita"})
        rec.output = tools.call(name, {"facility_id": "sierrita"})
        recs.append(rec)
    return recs


def tamper(brief: dict[str, Any], fn: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    b = copy.deepcopy(brief)
    fn(b)
    return b


@pytest.mark.parametrize(
    "mutation, expected",
    [
        (lambda b: b["spectral_evidence"]["facts"][0].update(value="83"), "!= raw"),
        (lambda b: b["spectral_evidence"]["facts"][0].update(path="counts.nonexistent"), "not found"),
        (lambda b: b.update(executive_summary=b["executive_summary"] + " Roughly 4321 tonnes are at risk."), "number 4321"),
        (lambda b: b["recommendations"].append("Re-survey before 2031-05-05."), "date 2031-05-05"),
        (lambda b: b["recommendations"].append("Without action an inevitable collapse follows."), "speculative"),
        (lambda b: b["recommendations"].append("Remediation provides guaranteed safety."), "speculative"),
        (lambda b: b["data_limitations"].append("The embankment will fail."), "speculative"),
        (lambda b: b.update(risk_classification="Tier-1 / Legacy Seepage Watch"), "risk_classification"),
        (lambda b: b.update(hazard_level="NONE"), "hazard_level"),
        (lambda b: b.update(disclaimer="Safe."), "disclaimer"),
        (lambda b: b["kinematic_evidence"]["prediction_windows"][0].update(upper_95="2030-01-01"), "upper_95"),
        (lambda b: b["kinematic_evidence"].update(prediction_windows=[]), "without its 95% bounds"),
    ],
)
def test_validator_rejects_ungrounded_briefs(tools, reference_brief, mutation, expected):
    with pytest.raises(C.BriefValidationError) as exc:
        C.validate_brief(tamper(reference_brief, mutation), records_for(tools))
    assert any(expected in d for d in exc.value.discrepancies), exc.value.discrepancies


def test_validator_accepts_negated_and_rounded_language(tools, reference_brief):
    k = tools.get_kinematics("sierrita")
    v = k["earliest_instability"]["velocity_mm_yr"]  # e.g. -367.79
    ok = tamper(reference_brief, lambda b: b["recommendations"] + [
        b["recommendations"].append(f"Screening results are not guaranteed; the fastest pixel moves about {abs(v):.1f} mm/yr away from the satellite."),
    ])
    C.validate_brief(ok, records_for(tools))


def test_validator_requires_all_four_tools(tools, reference_brief):
    with pytest.raises(C.BriefValidationError) as exc:
        C.validate_brief(reference_brief, records_for(tools)[:3])
    assert any("get_hydrological_setback was not called" in d for d in exc.value.discrepancies)


# --------------------------------------------------------------------------- #
# Claude tool-calling loop (offline, scripted)
# --------------------------------------------------------------------------- #


def test_copilot_tool_calling_flow_for_sierrita_complex(tools, reference_brief):
    client = ScriptedClaude([
        message([tool_call("list_facilities", {}, 0)], "tool_use"),
        four_tools(),
        final(reference_brief),
    ])
    result = C.AuditCopilot(tools, client).run(QUERY, expected_facility="sierrita")

    assert result.validation["passed"] and result.turns == 3 and result.generated_by == C.DEFAULT_MODEL
    assert [r.name for r in result.tool_calls] == ["list_facilities", *REQUIRED_TOOLS]
    assert all(r.error is None for r in result.tool_calls)
    assert result.usage == {"input_tokens": 300, "output_tokens": 60}

    first, second, third = client.requests
    assert first["model"] == "claude-opus-5-5" and first["max_tokens"] == C.MAX_TOKENS
    assert "tool_choice" not in first  # forced tool use is not available on this model
    assert all(t["strict"] for t in first["tools"])
    assert first["output_config"]["format"]["schema"] is C.BRIEF_SCHEMA and first["output_config"]["effort"] == "high"
    assert first["betas"] == [C.FALLBACK_BETA] and first["fallbacks"] == "default"
    assert first["messages"] == [{"role": "user", "content": QUERY}]
    # Append-only history; all four parallel tool results in ONE user message, matched by id.
    assert third["messages"][: len(second["messages"])] == second["messages"]
    results = third["messages"][-1]["content"]
    assert third["messages"][-1]["role"] == "user" and len(results) == 4
    assert [r["tool_use_id"] for r in results] == [f"toolu_{t}_{i}" for i, t in enumerate(REQUIRED_TOOLS)]
    assert json.loads(results[0]["content"])["name"] == "Freeport-McMoRan Sierrita TSF"
    facilities = json.loads(second["messages"][-1]["content"][0]["content"])["facilities"]
    assert "sierrita" in {f["id"] for f in facilities}


def test_copilot_nudges_until_required_tools_are_called(tools, reference_brief):
    client = ScriptedClaude([
        four_tools(tools_=REQUIRED_TOOLS[:3]),
        final(reference_brief),  # premature: setback never inspected
        message([tool_call("get_hydrological_setback", {"facility_id": "sierrita"}, 9)], "tool_use"),
        final(reference_brief),
    ])
    result = C.AuditCopilot(tools, client).run(QUERY)
    nudge = client.requests[2]["messages"][-1]
    assert nudge["role"] == "user" and "get_hydrological_setback" in nudge["content"]
    assert result.validation["passed"] and result.turns == 4


def test_copilot_accepts_dashboard_query_as_the_four_tools(tools, reference_brief):
    client = ScriptedClaude([
        message([tool_call("dashboard_validation_query", {"facility_id": "sierrita"}, 0)], "tool_use"),
        final(reference_brief),
    ])
    assert C.AuditCopilot(tools, client).run(QUERY).validation["passed"]


def test_copilot_raises_on_hallucinated_numbers(tools, reference_brief):
    bad = tamper(reference_brief, lambda b: b["kinematic_evidence"]["facts"][0].update(value="999"))
    client = ScriptedClaude([four_tools(), final(bad)])
    with pytest.raises(C.BriefValidationError) as exc:
        C.AuditCopilot(tools, client).run(QUERY)
    assert any("999" in d for d in exc.value.discrepancies)


def test_copilot_tool_errors_go_back_to_the_model(tools, reference_brief):
    client = ScriptedClaude([
        message([tool_call("fetch_dam_attributes", {"facility_id": "sierrita_complex"}, 0)], "tool_use"),
        four_tools(),
        final(reference_brief),
    ])
    result = C.AuditCopilot(tools, client).run(QUERY)
    error_result = client.requests[1]["messages"][-1]["content"][0]
    assert error_result["is_error"] is True and "Unknown facility" in error_result["content"]
    assert result.tool_calls[0].error and result.validation["passed"]


def test_copilot_refusal_and_wrong_facility(tools, reference_brief):
    refusal = message([], "refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": "declined"})
    with pytest.raises(C.AuditRefusedError, match="cyber"):
        C.AuditCopilot(tools, ScriptedClaude([refusal])).run(QUERY)
    with pytest.raises(C.BriefValidationError, match="requested"):
        C.AuditCopilot(tools, ScriptedClaude([four_tools(), final(reference_brief)])).run(QUERY, expected_facility="mission")


def test_copilot_gives_up_after_max_turns(tools):
    client = ScriptedClaude([message([tool_call("list_facilities", {}, i)], "tool_use") for i in range(3)])
    with pytest.raises(C.AuditError, match="no validated brief"):
        C.AuditCopilot(tools, client, max_turns=3).run(QUERY)


# --------------------------------------------------------------------------- #
# API and CLI
# --------------------------------------------------------------------------- #


@pytest.fixture
def no_credentials(monkeypatch, tmp_path):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_FEDERATION_RULE_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))


def test_api_audit_endpoint(env, reference_brief, no_credentials):
    settings = env["settings"]
    with TestClient(create_app(settings)) as c:
        r = c.post("/api/facilities/sierrita/audit", json={"mode": "auto"})
        assert r.status_code == 200
        body = AuditResponse.model_validate(r.json())
        assert body.generated_by == "deterministic-template" and body.brief["risk_classification"] == "Tier-3 / Active Instability"
        assert c.post("/api/facilities/sierrita/audit", json={"mode": "llm"}).status_code == 503
        assert c.post("/api/facilities/atlantis/audit", json={}).status_code == 404
        assert c.post("/api/facilities/sierrita/audit", json={"mode": "bogus"}).status_code == 422
        dash = c.get("/api/facilities/sierrita/dashboard").json()
        assert dash["all_checks_passed"] and dash["risk_classification"] == "Tier-3 / Active Instability"

    good = ScriptedClaude([four_tools(), final(reference_brief)])
    with TestClient(create_app(settings, llm_client=good)) as c:
        r = c.post("/api/facilities/sierrita/audit", json={"mode": "llm", "query": QUERY})
        assert r.status_code == 200 and r.json()["generated_by"] == C.DEFAULT_MODEL
        assert good.requests[0]["messages"][0]["content"] == QUERY

    bad = ScriptedClaude([four_tools(), final(tamper(reference_brief, lambda b: b.update(hazard_level="NONE")))])
    with TestClient(create_app(settings, llm_client=bad)) as c:
        r = c.post("/api/facilities/sierrita/audit", json={"mode": "llm"})
        assert r.status_code == 502 and any("hazard_level" in d for d in r.json()["detail"]["discrepancies"])


def test_cli_audit_writes_validated_brief(env, monkeypatch, no_credentials):
    from app import config, main

    monkeypatch.setenv("DATA_DIR", env["settings"].DATA_DIR)
    config.get_settings.cache_clear()
    try:
        out = env["settings"].log_dir / "cli_brief.json"
        assert main.main(["audit", "--facility", "Sierrita", "--output", str(out)]) == 0
        saved = json.loads(out.read_text())
        assert saved["validation"]["passed"] and saved["brief"]["facility_id"] == "sierrita"
    finally:
        config.get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Live Claude API (opt-in)
# --------------------------------------------------------------------------- #


@pytest.mark.network
@pytest.mark.skipif(os.environ.get("LITHOCREEP_LIVE_LLM") != "1" or not C.credentials_available(),
                    reason="set LITHOCREEP_LIVE_LLM=1 with Anthropic credentials to call the real API")
def test_live_claude_brief_for_sierrita_complex(tools):
    result = C.AuditCopilot(tools).run(QUERY, expected_facility="sierrita")
    assert result.validation["passed"]
    assert {r.name for r in result.tool_calls} >= set(REQUIRED_TOOLS) or "dashboard_validation_query" in {r.name for r in result.tool_calls}
