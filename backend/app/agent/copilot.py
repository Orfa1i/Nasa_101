"""GISTM audit copilot: Claude with deterministic inspection tools, and a grounding validator.

Flow (``AuditCopilot.run``):

1. A manual tool-use loop on Claude (``claude-opus-5-5`` by default) with the
   tools in ``tools.py``.
   * Forced ``tool_choice`` is not available on this model, so the prompt
     names the tools.
   * A brief whose facility has not been inspected with all four required
     tools is sent back with a request to call them (up to ``max_nudges``
     times).
   * Tool schemas are strict, and the final answer is constrained to
     ``BRIEF_SCHEMA`` with structured outputs.
2. ``validate_brief`` checks the brief against the raw tool outputs recorded
   during the run, and raises ``BriefValidationError`` on any discrepancy:
   * every cited fact equals the value at its tool/path;
   * every number and date in the prose appears in a tool output;
   * the risk class, hazard level, facility name and disclaimer are copied
     from ``fetch_dam_attributes``;
   * every trend-instability date is listed with its 95 % bounds;
   * no speculative certainty language is used.

``deterministic_brief`` writes the same brief without a model, from the same
tool outputs and through the same validator. It is used when no Anthropic
credentials are configured, and as a baseline.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.agent.tools import (
    HAZARD_TO_RISK,
    REQUIRED_TOOLS,
    RISK_CLASSES,
    RISK_INDETERMINATE,
    TOOL_DEFINITIONS,
    InspectionTools,
    ToolError,
)

logger = logging.getLogger("lithocreep.agent")

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16000
TOOL_NAMES = tuple(t["name"] for t in TOOL_DEFINITIONS)
HAZARD_LEVELS = ("NONE", "WATCH_TIER1", "ALERT_TIER2", "CRITICAL_TIER3", "UNKNOWN")
# Parts of a dashboard_validation_query result that stand in for the individual tools.
DASHBOARD_PARTS = {
    "attributes": "fetch_dam_attributes",
    "spectral": "get_spectral_alerts",
    "kinematics": "get_kinematics",
    "hydrological_setback": "get_hydrological_setback",
}

SYSTEM_PROMPT = """You are LithoCreep's geotechnical audit copilot. You compile evidence for a screening brief on one tailings facility, structured around the Global Industry Standard on Tailings Management (GISTM). Your evidence comes only from deterministic inspection tools that read the local LithoCreep database and its cached satellite products.

How to work:
- If the request names a facility rather than an id, resolve it with list_facilities.
- Inspect the facility with fetch_dam_attributes, get_spectral_alerts, get_kinematics and get_hydrological_setback. dashboard_validation_query returns all four in one call, plus consistency checks and rule-based GISTM screening candidates.
- Then answer with the brief JSON.

Grounding rules. A validator compares your brief with the tool results and rejects it on any discrepancy:
- Copy every number and date verbatim from a tool result. Do not compute, round, convert, sum or estimate new values. A value that is not in a tool result is described qualitatively or left out.
- Each fact cites the tool and the JSON path of its value within that tool's own result. Examples: counts.high_sai_pixels_confident, accelerating_pixels_by_earliest_date[0].instability_time, latest_valid.d_crest_m.
- Copy facility (the registry name), facility_id, risk_classification, hazard_level (use "UNKNOWN" if latest_alert is null) and disclaimer from fetch_dam_attributes.
- Trend-instability dates are statistical extrapolations, not failure predictions. Mention one only together with its 95% bounds, and list each one in kinematic_evidence.prediction_windows, with the path of its pixel entry.
- Write in cautious, evidential language. Never state certainty about failure or safety (for example "inevitable collapse", "guaranteed safety", "will fail", "no risk").
- When evidence is missing or a consistency check failed, say so in data_limitations rather than filling the gap.
- For GISTM references, use the principle strings given by the tools; do not invent requirement numbers."""

_FACT = {
    "type": "object",
    "properties": {
        "label": {"type": "string"},
        "tool": {"type": "string", "enum": list(TOOL_NAMES)},
        "path": {"type": "string"},
        "value": {"type": "string"},
        "unit": {"type": "string"},
    },
    "required": ["label", "tool", "path", "value", "unit"],
    "additionalProperties": False,
}
_SECTION = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "facts": {"type": "array", "items": _FACT}},
    "required": ["summary", "facts"],
    "additionalProperties": False,
}
_WINDOW = {
    "type": "object",
    "properties": {
        "label": {"type": "string"},
        "tool": {"type": "string", "enum": ["get_kinematics", "dashboard_validation_query"]},
        "path": {"type": "string"},
        "estimate": {"type": "string"},
        "lower_95": {"type": "string"},
        "upper_95": {"type": "string"},
    },
    "required": ["label", "tool", "path", "estimate", "lower_95", "upper_95"],
    "additionalProperties": False,
}
BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "facility": {"type": "string"},
        "facility_id": {"type": "string"},
        "risk_classification": {"type": "string", "enum": list(RISK_CLASSES)},
        "hazard_level": {"type": "string", "enum": list(HAZARD_LEVELS)},
        "executive_summary": {"type": "string"},
        "spectral_evidence": _SECTION,
        "kinematic_evidence": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "facts": {"type": "array", "items": _FACT},
                "prediction_windows": {"type": "array", "items": _WINDOW},
            },
            "required": ["summary", "facts", "prediction_windows"],
            "additionalProperties": False,
        },
        "hydrological_evidence": _SECTION,
        "compliance_issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "gistm_principle": {"type": "string"},
                    "issue": {"type": "string"},
                    "evidence_paths": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["gistm_principle", "issue", "evidence_paths"],
                "additionalProperties": False,
            },
        },
        "recommendations": {"type": "array", "items": {"type": "string"}},
        "data_limitations": {"type": "array", "items": {"type": "string"}},
        "disclaimer": {"type": "string"},
    },
    "required": [
        "facility", "facility_id", "risk_classification", "hazard_level", "executive_summary", "spectral_evidence",
        "kinematic_evidence", "hydrological_evidence", "compliance_issues", "recommendations", "data_limitations", "disclaimer",
    ],
    "additionalProperties": False,
}

BANNED_PATTERNS = (
    r"inevitabl[ey]",
    r"guarantee[ds]?",
    r"\bwill\s+(?:certainly\s+|definitely\s+|surely\s+)?(?:collapse|fail|breach)",
    r"\bcertain(?:ly)?\s+to\s+(?:collapse|fail|breach)",
    r"\bimminent\s+(?:collapse|failure|breach)",
    r"\bno\s+risk\b",
    r"\b(?:completely|perfectly|definitely|absolutely|totally)\s+safe\b",
    r"\bpredicted\s+(?:collapse|failure)\s+date",
)
# Only a negator directly before the phrase ("not guaranteed", "cannot be guaranteed") excuses it;
# "Without action an inevitable collapse ..." is still an absolute claim.
_NEGATION = re.compile(r"\b(?:not|never|cannot)\s+(?:be\s+|a\s+|an\s+)?$", re.IGNORECASE)
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_REFS = re.compile(
    r"GISTM(?:\s*\(?2020\)?)?|Principles?\s+\d+(?:\s*(?:,|and|&|or)\s*\d+)*|Requirements?\s+\d+(?:\.\d+)*|"
    r"Tier[-\s]?\d|REGIME[_\s-]?\d|Regime\s+\d|D_crest|EPSG:\d+|Phase\s+\d|2σ|2-sigma",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"(?<![\w.])[-+]?\d+(?:,\d{3})*(?:\.\d+)?(?![\w])")


class AuditError(RuntimeError):
    """The audit could not be produced."""


class AuditRefusedError(AuditError):
    """The model declined the request (stop_reason == "refusal")."""


class BriefValidationError(AuditError):
    def __init__(self, discrepancies: list[str]):
        self.discrepancies = discrepancies
        super().__init__(f"{len(discrepancies)} discrepancies between the brief and the tool outputs: " + "; ".join(discrepancies[:8]))


@dataclass
class ToolRecord:
    name: str
    arguments: dict[str, Any]
    output: Optional[dict[str, Any]] = None
    error: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {"tool": self.name, "input": self.arguments, "ok": self.error is None, "error": self.error}


@dataclass
class AuditResult:
    brief: dict[str, Any]
    validation: dict[str, Any]
    tool_calls: list[ToolRecord]
    generated_by: str
    turns: int = 0
    usage: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "brief": self.brief, "validation": self.validation, "generated_by": self.generated_by, "turns": self.turns,
            "usage": self.usage, "tool_calls": [r.as_dict() for r in self.tool_calls],
        }


# --------------------------------------------------------------------------- #
# Grounding validator
# --------------------------------------------------------------------------- #


def resolve_path(obj: Any, path: str) -> Any:
    """Resolve 'a.b[0].c' in nested dicts/lists; raises KeyError/IndexError/TypeError when absent."""
    for name, index in re.findall(r"([^.\[\]]+)|\[(\d+)\]", path):
        obj = obj[int(index)] if index else obj[name]
    return obj


def _walk(obj: Any, numbers: set[float], dates: set[str]) -> None:
    if isinstance(obj, dict):
        for v in obj.values():
            _walk(v, numbers, dates)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk(v, numbers, dates)
    elif isinstance(obj, bool) or obj is None:
        return
    elif isinstance(obj, (int, float)):
        numbers.add(float(obj))
    elif isinstance(obj, str):
        dates.update(_DATE.findall(obj))
        for token in _NUMBER.findall(_DATE.sub(" ", obj)):
            numbers.add(float(token.replace(",", "")))


def _decimals(token: str) -> int:
    return len(token.split(".")[1]) if "." in token else 0


def _number_matches(token: str, raw: float, allow_abs: bool) -> bool:
    value = float(token.replace(",", ""))
    tol = 0.5 * 10 ** (-_decimals(token)) + 1e-9
    if abs(value - raw) <= tol:
        return True
    return allow_abs and abs(abs(value) - abs(raw)) <= tol


def _value_matches(text: str, raw: Any) -> bool:
    if isinstance(raw, bool):
        return text.strip().lower() == str(raw).lower()
    if isinstance(raw, (int, float)):
        m = _NUMBER.search(text.replace(" ", ""))
        return bool(m) and _number_matches(m.group(0), float(raw), allow_abs=False)
    if isinstance(raw, str):
        return text.strip() == raw.strip()
    return False


def tool_outputs_for(records: list[ToolRecord], facility_id: str) -> dict[str, dict[str, Any]]:
    """Latest successful output per tool for one facility; a dashboard call also stands in for the four tools."""
    out: dict[str, dict[str, Any]] = {}
    for rec in records:
        if rec.output is None:
            continue
        if rec.name == "list_facilities":
            out["list_facilities"] = rec.output
            continue
        if rec.arguments.get("facility_id") != facility_id:
            continue
        out[rec.name] = rec.output
        if rec.name == "dashboard_validation_query":
            for part, tool in DASHBOARD_PARTS.items():
                out.setdefault(tool, rec.output[part])
    return out


def _texts(brief: dict[str, Any]) -> list[tuple[str, str]]:
    items = [("executive_summary", brief.get("executive_summary", ""))]
    for section in ("spectral_evidence", "kinematic_evidence", "hydrological_evidence"):
        sec = brief.get(section) or {}
        items.append((f"{section}.summary", sec.get("summary", "")))
        items += [(f"{section}.facts[{i}].label", f.get("label", "")) for i, f in enumerate(sec.get("facts", []))]
    for i, issue in enumerate(brief.get("compliance_issues", [])):
        items.append((f"compliance_issues[{i}].issue", issue.get("issue", "")))
    items += [(f"recommendations[{i}]", t) for i, t in enumerate(brief.get("recommendations", []))]
    items += [(f"data_limitations[{i}]", t) for i, t in enumerate(brief.get("data_limitations", []))]
    return items


def validate_brief(brief: dict[str, Any], records: list[ToolRecord]) -> dict[str, Any]:
    """Check every claim in ``brief`` against the recorded tool outputs; raise on any discrepancy."""
    errors: list[str] = []
    missing_keys = [k for k in BRIEF_SCHEMA["required"] if k not in brief]
    if missing_keys:
        raise BriefValidationError([f"brief is missing {missing_keys}"])
    fid = brief["facility_id"]
    outputs = tool_outputs_for(records, fid)
    for tool in REQUIRED_TOOLS:
        if tool not in outputs:
            errors.append(f"required tool {tool} was not called for facility {fid!r}")
    attrs = outputs.get("fetch_dam_attributes")
    if attrs is None:
        raise BriefValidationError(errors or [f"no fetch_dam_attributes output for {fid!r}"])

    # Fields copied from fetch_dam_attributes.
    latest = attrs.get("latest_alert") or {}
    expected = {
        "facility": attrs["name"],
        "risk_classification": attrs["risk_classification"],
        "hazard_level": latest.get("hazard_level") or "UNKNOWN",
        "disclaimer": attrs["required_disclaimer"],
    }
    for key, value in expected.items():
        if brief.get(key) != value:
            errors.append(f"{key} is {brief.get(key)!r}; fetch_dam_attributes says {value!r}")
    if attrs["risk_classification"] != HAZARD_TO_RISK.get(latest.get("hazard_level"), RISK_INDETERMINATE):
        errors.append("risk classification inconsistent with the database hazard level")

    # Cited facts.
    n_facts = 0
    for section in ("spectral_evidence", "kinematic_evidence", "hydrological_evidence"):
        for i, fact in enumerate(brief[section].get("facts", [])):
            n_facts += 1
            where = f"{section}.facts[{i}] ({fact.get('label')!r})"
            source = outputs.get(fact.get("tool"))
            if source is None:
                errors.append(f"{where}: tool {fact.get('tool')!r} was not called for {fid!r}")
                continue
            try:
                raw = resolve_path(source, fact["path"])
            except (KeyError, IndexError, TypeError):
                errors.append(f"{where}: path {fact['path']!r} not found in {fact['tool']} output")
                continue
            if raw is None or isinstance(raw, (dict, list)):
                errors.append(f"{where}: {fact['tool']}:{fact['path']} is not a scalar value")
            elif not _value_matches(str(fact.get("value", "")), raw):
                errors.append(f"{where}: value {fact.get('value')!r} != raw {raw!r} at {fact['tool']}:{fact['path']}")

    # Prediction windows: estimate and both bounds must come from one pixel entry.
    window_estimates: set[str] = set()
    for i, w in enumerate(brief["kinematic_evidence"].get("prediction_windows", [])):
        where = f"kinematic_evidence.prediction_windows[{i}]"
        source = outputs.get(w.get("tool"))
        try:
            entry = resolve_path(source, w["path"]) if source is not None else None
        except (KeyError, IndexError, TypeError):
            entry = None
        if not isinstance(entry, dict) or "instability_time" not in entry:
            errors.append(f"{where}: {w.get('tool')}:{w.get('path')} is not an accelerating-pixel entry")
            continue
        for key, raw_key in (("estimate", "instability_time"), ("lower_95", "instability_time_lower"), ("upper_95", "instability_time_upper")):
            if str(w.get(key)) != str(entry.get(raw_key)):
                errors.append(f"{where}: {key} {w.get(key)!r} != raw {entry.get(raw_key)!r}")
        window_estimates.add(str(w.get("estimate")))

    # Prose: dates and numbers must come from tool outputs; estimates need bounds; no certainty language.
    raw_numbers: set[float] = set()
    raw_dates: set[str] = set()
    for output in outputs.values():
        _walk(output, raw_numbers, raw_dates)
    estimate_dates: set[str] = set()
    kin_out = outputs.get("get_kinematics") or {}
    for entry in kin_out.get("accelerating_pixels_by_earliest_date", []) or []:
        if entry.get("instability_time"):
            estimate_dates.add(entry["instability_time"])
    if latest.get("failure_date_est"):
        estimate_dates.add(latest["failure_date_est"])
    n_numbers = 0
    for where, text in _texts(brief):
        for date in _DATE.findall(text):
            if date not in raw_dates:
                errors.append(f"{where}: date {date} does not appear in any tool output")
            elif date in estimate_dates and date not in window_estimates:
                errors.append(f"{where}: trend-instability date {date} is used without its 95% bounds in prediction_windows")
        scrubbed = _REFS.sub(" ", _DATE.sub(" ", text))
        for token in _NUMBER.findall(scrubbed):
            n_numbers += 1
            if not any(_number_matches(token, raw, allow_abs=True) for raw in raw_numbers):
                errors.append(f"{where}: number {token} does not appear in any tool output")
        for pattern in BANNED_PATTERNS:
            for m in re.finditer(pattern, text, re.IGNORECASE):
                if not _NEGATION.search(text[: m.start()]):
                    errors.append(f"{where}: speculative/absolute language {m.group(0)!r}")

    if errors:
        raise BriefValidationError(errors)
    return {
        "passed": True,
        "facts_checked": n_facts,
        "prose_numbers_checked": n_numbers,
        "prediction_windows_checked": len(window_estimates),
        "tools_used": sorted(t for t in outputs if t in TOOL_NAMES),
    }


# --------------------------------------------------------------------------- #
# Deterministic (no-LLM) brief
# --------------------------------------------------------------------------- #


def _fact(label: str, tool: str, source: dict[str, Any], path: str, unit: str = "") -> Optional[dict[str, Any]]:
    try:
        raw = resolve_path(source, path)
    except (KeyError, IndexError, TypeError):
        return None
    if raw is None or isinstance(raw, (dict, list)):
        return None
    return {"label": label, "tool": tool, "path": path, "value": str(raw).lower() if isinstance(raw, bool) else str(raw), "unit": unit}


def compose_brief(dashboard: dict[str, Any]) -> dict[str, Any]:
    """Template brief from a dashboard_validation_query result (every value cited by path)."""
    attrs, spec, kinem, hydro = (dashboard[k] for k in ("attributes", "spectral", "kinematics", "hydrological_setback"))
    latest = attrs.get("latest_alert") or {}
    F, S, K, H = "fetch_dam_attributes", "get_spectral_alerts", "get_kinematics", "get_hydrological_setback"
    keep = lambda xs: [x for x in xs if x is not None]  # noqa: E731

    summary = [f"{attrs['name']}: screening classification {attrs['risk_classification']}."]
    spectral_facts, spectral_summary = [], "No spectral evidence is available."
    if spec.get("available"):
        c = spec["counts"]
        spectral_summary = (
            f"{c['high_sai_pixels_confident']} confident pixels exceed the SAI threshold of {spec['sai_threshold']} "
            f"({spec['high_sai_area_ha_confident']} ha); maximum SAI {spec['sai_max']}."
        )
        spectral_facts = keep([
            _fact("Pixels with SAI above threshold (confident)", S, spec, "counts.high_sai_pixels_confident", "pixels"),
            _fact("Pixels with SAI above threshold (all)", S, spec, "counts.high_sai_pixels_all", "pixels"),
            _fact("Pixels whose 95% SAI interval straddles the threshold", S, spec, "counts.high_sai_pixels_interval_straddles_threshold", "pixels"),
            _fact("High-SAI area (confident)", S, spec, "high_sai_area_ha_confident", "ha"),
            _fact("Maximum SAI (valid pixels)", S, spec, "sai_max", "index"),
            _fact("Pixels excluded for low confidence or vegetation", S, spec, "counts.pixels_excluded_low_confidence_or_vegetated", "pixels"),
        ])
        summary.append(f"{c['high_sai_pixels_confident']} confident high-SAI pixels.")

    kin_facts, windows, kin_summary = [], [], "No InSAR kinematic evidence is available."
    if kinem.get("available"):
        kin_summary = (
            f"{kinem['accelerating_pixels']} accelerating pixels between {kinem['first_epoch']} and {kinem['last_epoch']} "
            f"({kinem['n_epochs']} epochs); {kinem['unobservable_fraction_pct']}% of pixels are unobservable."
        )
        kin_facts = keep([
            _fact("Accelerating pixels", K, kinem, "accelerating_pixels", "pixels"),
            _fact("Accelerating mid-slope pixels (coupled grid)", K, kinem, "accelerating_mid_slope_pixels_coupled_grid", "pixels"),
            _fact("Pixels with a trend-instability date", K, kinem, "pixels_with_instability_date", "pixels"),
            _fact("Unobservable (low-coherence) share", K, kinem, "unobservable_fraction_pct", "%"),
        ])
        for i, entry in enumerate(kinem.get("accelerating_pixels_by_earliest_date", [])[:3]):
            if entry.get("instability_time"):
                path = f"accelerating_pixels_by_earliest_date[{i}]"
                windows.append({"label": f"Trend-instability window, pixel {i + 1}", "tool": K, "path": path,
                                "estimate": entry["instability_time"], "lower_95": entry["instability_time_lower"],
                                "upper_95": entry["instability_time_upper"]})
                kin_facts += keep([
                    _fact(f"Pixel {i + 1} LOS velocity", K, kinem, f"{path}.velocity_mm_yr", "mm/yr"),
                    _fact(f"Pixel {i + 1} velocity lower 95% bound", K, kinem, f"{path}.velocity_lower_95", "mm/yr"),
                    _fact(f"Pixel {i + 1} velocity upper 95% bound", K, kinem, f"{path}.velocity_upper_95", "mm/yr"),
                ])
        if windows:
            w = windows[0]
            kin_summary += (f" Earliest trend-instability estimate {w['estimate']} (95% bounds {w['lower_95']} to "
                            f"{w['upper_95']}); these dates indicate a potential trend change and are biased late.")
        summary.append(f"{kinem['accelerating_pixels']} accelerating InSAR pixels.")

    hyd_facts, hyd_summary = [], "No pond setback history is available."
    if hydro.get("available") and hydro.get("latest_valid"):
        lv = hydro["latest_valid"]
        hyd_summary = (f"Latest valid setback {lv['d_crest_m']} m on {lv['date']} "
                       f"(change vs baseline {lv['delta_vs_baseline_m']} m); encroaching: {str(hydro['encroaching']).lower()}.")
        hyd_facts = keep([
            _fact("Latest valid D_crest", H, hydro, "latest_valid.d_crest_m", "m"),
            _fact("Latest valid epoch", H, hydro, "latest_valid.date", "date"),
            _fact("Change vs baseline", H, hydro, "latest_valid.delta_vs_baseline_m", "m"),
            _fact("Minimum valid D_crest", H, hydro, "min_valid_d_crest_m", "m"),
            _fact("Valid epochs", H, hydro, "n_valid_epochs", "epochs"),
            _fact("Setback encroaching", H, hydro, "encroaching", ""),
        ])
        summary.append(f"Latest valid setback {lv['d_crest_m']} m.")
    elif hydro.get("reason"):
        hyd_summary = f"Setback history unavailable: {hydro['reason']}."

    screening = dashboard["gistm_screening"]
    limitations = list(screening["data_limitations"])
    failed = [c["check"] for c in dashboard["consistency_checks"] if not c["passed"]]
    if failed:
        limitations.append("Consistency checks failed: " + ", ".join(failed) + ".")
    return {
        "facility": attrs["name"],
        "facility_id": attrs["facility_id"],
        "risk_classification": attrs["risk_classification"],
        "hazard_level": latest.get("hazard_level") or "UNKNOWN",
        "executive_summary": " ".join(summary),
        "spectral_evidence": {"summary": spectral_summary, "facts": spectral_facts},
        "kinematic_evidence": {"summary": kin_summary, "facts": kin_facts, "prediction_windows": windows},
        "hydrological_evidence": {"summary": hyd_summary, "facts": hyd_facts},
        "compliance_issues": [
            {"gistm_principle": i["gistm_principle"], "issue": i["issue"], "evidence_paths": i["evidence_paths"]}
            for i in screening["candidate_issues"]
        ],
        "recommendations": list(screening["recommendations"]),
        "data_limitations": limitations,
        "disclaimer": attrs["required_disclaimer"],
    }


def deterministic_brief(tools: InspectionTools, facility_id: str) -> AuditResult:
    record = ToolRecord("dashboard_validation_query", {"facility_id": facility_id})
    record.output = tools.dashboard_validation_query(facility_id)
    brief = compose_brief(record.output)
    validation = validate_brief(brief, [record])
    return AuditResult(brief, validation, [record], generated_by="deterministic-template", turns=0)


# --------------------------------------------------------------------------- #
# Claude tool-calling loop
# --------------------------------------------------------------------------- #


def credentials_available() -> bool:
    """True when the Anthropic SDK can find credentials (API key, auth token, CLI profile or WIF)."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_PROFILE"):
        return True
    if all(os.environ.get(v) for v in ("ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID", "ANTHROPIC_SERVICE_ACCOUNT_ID")):
        return True
    return (Path.home() / ".config" / "anthropic").is_dir()


class AuditCopilot:
    """Claude-driven audit with the deterministic tools and a mandatory validation step."""

    def __init__(
        self,
        tools: InspectionTools,
        client: Any = None,
        *,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        max_turns: int = 12,
        max_nudges: int = 2,
        use_fallbacks: bool = True,
    ):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.tools = tools
        self.model = model
        self.effort = effort
        self.max_turns = max_turns
        self.max_nudges = max_nudges
        self.use_fallbacks = use_fallbacks

    def _request(self, messages: list[dict[str, Any]]) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": SYSTEM_PROMPT,
            "tools": TOOL_DEFINITIONS,
            "messages": messages,
            "output_config": {"effort": self.effort, "format": {"type": "json_schema", "schema": BRIEF_SCHEMA}},
        }
        if self.use_fallbacks:
            # Server-side refusal fallback ("default" routing by refusal category).
            kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
        return self.client.beta.messages.create(**kwargs)

    def _run_tool(self, block: Any, records: list[ToolRecord]) -> dict[str, Any]:
        arguments = dict(block.input or {})
        record = ToolRecord(block.name, arguments)
        records.append(record)
        try:
            record.output = self.tools.call(block.name, arguments)
            content, is_error = json.dumps(record.output, default=str), False
        except (ToolError, TypeError) as exc:
            record.error = str(exc)
            content, is_error = f"Error: {exc}", True
        logger.info("tool %s(%s) -> %s", block.name, arguments, "error" if is_error else "ok")
        result = {"type": "tool_result", "tool_use_id": block.id, "content": content}
        if is_error:
            result["is_error"] = True
        return result

    def run(self, query: str, *, expected_facility: Optional[str] = None) -> AuditResult:
        messages: list[dict[str, Any]] = [{"role": "user", "content": query}]
        records: list[ToolRecord] = []
        usage = {"input_tokens": 0, "output_tokens": 0}
        nudges = 0
        for turn in range(1, self.max_turns + 1):
            response = self._request(messages)
            for key in usage:
                usage[key] += int(getattr(response.usage, key, 0) or 0)
            stop = response.stop_reason
            if stop == "refusal":
                details = getattr(response, "stop_details", None)
                raise AuditRefusedError(f"model declined the request ({getattr(details, 'category', None)})")
            if stop == "max_tokens":
                raise AuditError("response hit max_tokens before the brief was complete")
            messages.append({"role": "assistant", "content": response.content})
            if stop == "pause_turn":
                continue
            tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            if stop == "tool_use" or tool_uses:
                # All results of one turn go back in a single user message.
                messages.append({"role": "user", "content": [self._run_tool(b, records) for b in tool_uses]})
                continue

            text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", None) == "text")
            try:
                brief = json.loads(text)
            except json.JSONDecodeError as exc:
                raise AuditError(f"final answer is not valid JSON: {exc}") from exc
            fid = brief.get("facility_id")
            if expected_facility is not None and fid != expected_facility:
                raise BriefValidationError([f"brief is about {fid!r}, but {expected_facility!r} was requested"])
            called = tool_outputs_for(records, fid)
            missing = [t for t in REQUIRED_TOOLS if t not in called]
            if missing and nudges < self.max_nudges:
                nudges += 1
                messages.append({"role": "user", "content": (
                    f"Before finalising, call {', '.join(missing)} for facility_id '{fid}' "
                    "(or dashboard_validation_query), then return the brief again.")})
                continue
            validation = validate_brief(brief, records)  # raises BriefValidationError on any discrepancy
            served_by = getattr(response, "model", None) or self.model
            return AuditResult(brief, validation, records, generated_by=served_by, turns=turn, usage=usage)
        raise AuditError(f"no validated brief after {self.max_turns} turns")


def generate_audit(
    tools: InspectionTools,
    facility_id: str,
    *,
    query: Optional[str] = None,
    mode: str = "auto",
    client: Any = None,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
) -> AuditResult:
    """``mode``: ``llm`` (Claude, needs credentials), ``deterministic`` (template), ``auto`` (LLM if credentials exist)."""
    if mode not in {"auto", "llm", "deterministic"}:
        raise ValueError(f"mode must be auto, llm or deterministic, got {mode!r}")
    use_llm = mode == "llm" or (mode == "auto" and (client is not None or credentials_available()))
    if not use_llm:
        tools.fetch_dam_attributes(facility_id)  # raises ToolError for unknown ids
        return deterministic_brief(tools, facility_id)
    name = tools.fetch_dam_attributes(facility_id)["name"]
    query = query or f"Generate regulatory GISTM inspection brief for {name} (facility_id {facility_id})."
    return AuditCopilot(tools, client, model=model, effort=effort).run(query, expected_facility=facility_id)
