"""Deterministic inspection tools for the audit copilot.

Every tool reads only the local DuckDB database and the cached pipeline
products (``DATA_DIR/cache/<prefix>_*.zarr``). Each returns a
JSON-serialisable dict and never calls a model. The copilot exposes these
tools to Claude; the brief validator compares every number in a brief with
what these tools returned.

| Tool | Returns |
|------|---------|
| ``list_facilities()`` | ids and names, so a free-text name ("Sierrita Complex") can be resolved |
| ``fetch_dam_attributes(id)`` | registry metadata, latest database alert, available products, risk class |
| ``get_spectral_alerts(id)`` | pixels with SAI > 0.25 (all and confident only), statistics, top pixels |
| ``get_kinematics(id)`` | accelerating pixels and their trend-instability dates with 95 % bounds |
| ``get_hydrological_setback(id)`` | the pond-to-crest D_crest(t) history, alerts and encroachment state |
| ``dashboard_validation_query(id)`` | all of the above in one call, plus cross-product consistency checks and GISTM screening candidates |

Floats are rounded at the source (SAI 4 dp, velocities 2 dp, distances 1 dp)
so that a brief can quote them verbatim.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd
from pyproj import Transformer

from app.config import Settings
from app.database import Database, fetch_alerts, fetch_facilities

SAI_THRESHOLD = 0.25
CONFIDENCE_LEVEL_PCT = 95
SIGMA = 2
MAX_LISTED_PIXELS = 10

HAZARD_TO_RISK = {
    "CRITICAL_TIER3": "Tier-3 / Active Instability",
    "ALERT_TIER2": "Tier-2 / Alert",
    "WATCH_TIER1": "Tier-1 / Legacy Seepage Watch",
    "NONE": "No Alert / Benign Consolidation",
}
RISK_INDETERMINATE = "Indeterminate / Insufficient Data"
RISK_CLASSES = tuple(HAZARD_TO_RISK.values()) + (RISK_INDETERMINATE,)

REQUIRED_DISCLAIMER = (
    "Satellite screening only. Trend-instability dates are statistical extrapolations with 95% bounds that "
    "indicate a potential change in deformation trend, not a predicted failure date, and are biased late in "
    "validation tests. This brief does not certify structural safety and does not replace on-site "
    "inspection, instrumentation or the Engineer of Record's assessment."
)

# GISTM (Global Industry Standard on Tailings Management, 2020) principles cited by the
# screening rules, at principle level, paraphrased. To be confirmed by a qualified reviewer.
GISTM_PRINCIPLES = {
    "2": "Principle 2: develop and maintain an interdisciplinary knowledge base for the facility",
    "6": "Principle 6: plan, build and operate the facility to manage risk at all lifecycle phases",
    "7": "Principle 7: design, implement and operate monitoring systems to manage risk",
    "9": "Principle 9: appoint and empower an Engineer of Record",
    "13": "Principle 13: prepare for emergency response to tailings facility failures",
}


class ToolError(ValueError):
    """A tool could not run for the given arguments (unknown facility, missing product)."""


def r(value: Any, ndigits: int) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return round(v, ndigits) if math.isfinite(v) else None


def iso_date(value: Any) -> Optional[str]:
    if value is None:
        return None
    ts = pd.Timestamp(value)
    return None if pd.isna(ts) else ts.strftime("%Y-%m-%d")


@dataclass
class _Product:
    ds: Any
    file: str
    synthetic: bool


class InspectionTools:
    """The deterministic tool set. One instance per request; it opens products lazily."""

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    # ------------------------------------------------------------------ helpers
    def _facility(self, facility_id: str) -> dict[str, Any]:
        cur = self.db.cursor()
        try:
            rows = fetch_facilities(cur, facility_id)
        finally:
            cur.close()
        if not rows:
            raise ToolError(f"Unknown facility id {facility_id!r}; call list_facilities to see valid ids")
        return rows[0]

    def _product(self, row: dict[str, Any], product: str) -> Optional[_Product]:
        from app.api import layers as L

        try:
            ds, path = L.open_product(self.settings, row["cache_prefix"], product)
        except L.AssetMissing:
            return None
        except L.AssetUnreadable as exc:
            raise ToolError(str(exc)) from exc
        source = L.describe_source(product, path, "*", ds)
        return _Product(ds, path.name, source.synthetic_or_mock)

    @staticmethod
    def _lonlat(ds: Any, rows: np.ndarray, cols: np.ndarray) -> list[tuple[float, float]]:
        crs = ds.rio.crs
        to_ll = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        lon, lat = to_ll.transform(ds["x"].values[cols], ds["y"].values[rows])
        return [(round(float(a), 6), round(float(b), 6)) for a, b in zip(np.atleast_1d(lon), np.atleast_1d(lat))]

    @staticmethod
    def _pixel_area_m2(ds: Any) -> float:
        xs, ys = ds["x"].values, ds["y"].values
        return abs(float(xs[1] - xs[0])) * abs(float(ys[1] - ys[0]))

    # ------------------------------------------------------------------ tools
    def list_facilities(self) -> dict[str, Any]:
        cur = self.db.cursor()
        try:
            rows = fetch_facilities(cur)
        finally:
            cur.close()
        return {"facilities": [{"id": r_["id"], "name": r_["name"], "country": r_["country"]} for r_ in rows]}

    def fetch_dam_attributes(self, facility_id: str) -> dict[str, Any]:
        row = self._facility(facility_id)
        cur = self.db.cursor()
        try:
            alerts = fetch_alerts(cur, facility_id)
        finally:
            cur.close()
        latest = alerts[0] if alerts else None
        products = {p: self._product(row, p) is not None for p in ("spectral", "kinematics", "hydrology", "coupled")}
        hazard = latest["hazard_level"] if latest else None
        return {
            "facility_id": row["id"],
            "name": row["name"],
            "country": row["country"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "bbox": [row["bbox_xmin"], row["bbox_ymin"], row["bbox_xmax"], row["bbox_ymax"]],
            "mine_type": row["mine_type"],
            "embankment_type": row["embankment_type"],
            "crest_line_available": bool(row["crest_line_geom"]),  # in the registry (the hydrology run may use a file)
            "status": row["status"],
            "products_available": products,
            "latest_alert": None if latest is None else {
                "alert_id": latest["id"],
                "timestamp": iso_date(latest["timestamp"]),
                "hazard_level": latest["hazard_level"],
                "failure_date_est": iso_date(latest["failure_date_est"]),
                "failure_date_error_days": r(latest["failure_date_error_days"], 1),
                "contains_synthetic_or_mock_inputs": latest["diagnostic_evidence"].get("contains_synthetic_or_mock_inputs"),
                "regime_counts": latest["diagnostic_evidence"].get("regime_counts", {}),
            },
            "risk_classification": HAZARD_TO_RISK.get(hazard, RISK_INDETERMINATE),
            "required_disclaimer": REQUIRED_DISCLAIMER,
        }

    def get_spectral_alerts(self, facility_id: str) -> dict[str, Any]:
        """Pixels with SAI > 0.25. Prefers the aligned 30 m coupled grid, else the 60 m spectral product."""
        from app.pipelines import coupling as cp

        row = self._facility(facility_id)
        coupled = self._product(row, "coupled")
        if coupled is not None:
            ds = coupled.ds
            sai = ds["sai"].values.astype(np.float64)
            flags = ds["classification_flags"].values.astype(np.int64)
            lower, upper = ds["sai_lower"].values, ds["sai_upper"].values
            excluded = (flags & (cp.FLAG_HIGH_RMSE | cp.FLAG_VEGETATED | cp.FLAG_NO_SPECTRAL)) > 0
            marginal = (flags & cp.FLAG_SAI_MARGINAL) > 0
            prod, grid = coupled, "coupled 30 m (EMIT aligned to the DEM grid)"
        else:
            spectral = self._product(row, "spectral")
            if spectral is None:
                return {"facility_id": facility_id, "available": False, "reason": "no spectral or coupled product for this facility"}
            ds = spectral.ds
            sai = ds["sai"].values.astype(np.float64)
            obs = ds["observability"].values
            rmse = ds["rmse"].values
            factor = cp.sai_sigma_factor(ds)
            lower, upper = sai - SIGMA * factor * rmse, sai + SIGMA * factor * rmse
            excluded = obs != 0
            marginal = (lower <= SAI_THRESHOLD) & (upper > SAI_THRESHOLD)
            prod, grid = spectral, "spectral native (EMIT 60 m)"

        finite = np.isfinite(sai)
        valid = finite & ~excluded
        high_all = finite & (sai > SAI_THRESHOLD)
        high_conf = valid & (sai > SAI_THRESHOLD)
        area_ha = self._pixel_area_m2(ds) / 1e4
        idx = np.argwhere(high_conf)
        order = np.argsort(-sai[high_conf])[:MAX_LISTED_PIXELS]
        rows_, cols_ = idx[order, 0], idx[order, 1]
        coords = self._lonlat(ds, rows_, cols_) if order.size else []
        top = [
            {"lon": lon, "lat": lat, "sai": r(sai[i, j], 4), "sai_lower_95": r(lower[i, j], 4), "sai_upper_95": r(upper[i, j], 4)}
            for (lon, lat), i, j in zip(coords, rows_, cols_)
        ]
        n_valid = int(valid.sum())
        return {
            "facility_id": facility_id,
            "available": True,
            "source_file": prod.file,
            "grid": grid,
            "synthetic_or_mock_inputs": prod.synthetic,
            "sai_threshold": SAI_THRESHOLD,
            "confidence_level_pct": CONFIDENCE_LEVEL_PCT,
            "counts": {
                "pixels_with_sai": int(finite.sum()),
                "pixels_valid_confidence": n_valid,
                "high_sai_pixels_all": int(high_all.sum()),
                "high_sai_pixels_confident": int(high_conf.sum()),
                "high_sai_pixels_interval_straddles_threshold": int((high_all & marginal).sum()),
                "pixels_excluded_low_confidence_or_vegetated": int((finite & excluded).sum()),
            },
            "high_sai_area_ha_confident": r(high_conf.sum() * area_ha, 2),
            "high_sai_fraction_of_valid_pct": r(100.0 * high_conf.sum() / n_valid, 2) if n_valid else None,
            "sai_max": r(np.nanmax(np.where(valid, sai, np.nan)), 4) if n_valid else None,
            "sai_mean_valid": r(np.nanmean(np.where(valid, sai, np.nan)), 4) if n_valid else None,
            "top_high_sai_pixels": top,
        }

    def get_kinematics(self, facility_id: str) -> dict[str, Any]:
        """Accelerating pixels (Phase 3 status) with trend-instability dates and 95 % bounds."""
        from app.pipelines import coupling as cp
        from app.pipelines import kinematics as kin

        row = self._facility(facility_id)
        prod = self._product(row, "kinematics")
        if prod is None:
            return {"facility_id": facility_id, "available": False, "reason": "no kinematics product (DISP-S1 stack or historical series) for this facility"}
        ds = prod.ds
        last = ds.isel(time=-1)
        status = ds["kinematic_status"].values
        meanings = kin.STATUS_MEANINGS.split()
        accel = np.isin(status, (kin.ACCEL_FORECAST, kin.ACCEL_NO_TREND))
        days = cp.as_days(ds["instability_days_after_last_epoch"]).values
        days_2s = cp.as_days(ds["instability_days_2sigma"]).values
        point_mode = "point" in ds.dims

        positions = np.argwhere(accel) if not point_mode else np.flatnonzero(accel)[:, None]
        key = np.where(np.isfinite(days[accel]), days[accel], np.inf)
        order = np.argsort(key, kind="stable")[:MAX_LISTED_PIXELS]
        listed = []
        for k in order:
            pos = tuple(positions[k])
            sel = {"point": pos[0]} if point_mode else {"y": pos[0], "x": pos[1]}
            px = last.isel(**sel)
            entry: dict[str, Any] = {}
            if point_mode:
                entry["point_id"] = str(ds["point"].values[pos[0]])
            else:
                (lon, lat), = self._lonlat(ds, np.array([pos[0]]), np.array([pos[1]]))
                entry.update(lon=lon, lat=lat)
            entry.update(
                status=meanings[int(status[pos])],
                velocity_mm_yr=r(px["velocity"].values, 2),
                velocity_lower_95=r(px["velocity_lower"].values, 2),
                velocity_upper_95=r(px["velocity_upper"].values, 2),
                acceleration_mm_yr2=r(px["acceleration"].values, 2),
                acceleration_lower_95=r(px["acceleration_lower"].values, 2),
                acceleration_upper_95=r(px["acceleration_upper"].values, 2),
                temporal_coherence=r(ds["temporal_coherence"].values[pos], 3),
                instability_time=iso_date(ds["instability_time"].values[pos]),
                instability_time_lower=iso_date(ds["instability_time_lower"].values[pos]),
                instability_time_upper=iso_date(ds["instability_time_upper"].values[pos]),
                instability_days_after_last_epoch=r(days[pos], 1),
                instability_days_2sigma=r(days_2s[pos], 1),
            )
            listed.append(entry)

        accel_mid_slope = None
        coupled = self._product(row, "coupled")
        if coupled is not None:
            cstat = coupled.ds["kinematic_status"].values
            accel_mid_slope = int((np.isin(cstat, (kin.ACCEL_FORECAST, kin.ACCEL_NO_TREND)) & coupled.ds["mid_slope"].values).sum())

        coh = ds["temporal_coherence"].values
        forecast = status == kin.ACCEL_FORECAST
        times = pd.to_datetime(ds["time"].values)
        return {
            "facility_id": facility_id,
            "available": True,
            "source_file": prod.file,
            "source": ds.attrs.get("source"),
            "synthetic_or_mock_inputs": prod.synthetic,
            "geometry": "points" if point_mode else "raster",
            "first_epoch": iso_date(times[0]),
            "last_epoch": iso_date(times[-1]),
            "n_epochs": int(times.size),
            "thresholds": {"coherence": kin.COHERENCE_THRESHOLD, "velocity_mm_yr": kin.VELOCITY_THRESHOLD_MM_YR,
                           "acceleration_sigma": kin.ACCELERATION_SIGMA, "fukuzono_points": kin.FUKUZONO_POINTS},
            "confidence_level_pct": CONFIDENCE_LEVEL_PCT,
            "status_counts": {m: int((status == c).sum()) for c, m in enumerate(meanings)},
            "accelerating_pixels": int(accel.sum()),
            "accelerating_mid_slope_pixels_coupled_grid": accel_mid_slope,
            "pixels_with_instability_date": int((forecast & np.isfinite(days)).sum()),
            "unobservable_fraction_pct": r(100.0 * float(np.mean(~(coh >= kin.COHERENCE_THRESHOLD))), 2),
            "earliest_instability": listed[0] if listed and listed[0]["instability_time"] else None,
            "accelerating_pixels_by_earliest_date": listed,
            "known_bias": "Validation on synthetic accelerating creep: dates were later than the true change (about 2x the lead time) and the 95% bounds were too narrow; treat dates as late upper estimates.",
            "disclaimer": ds.attrs.get("disclaimer", kin.DISCLAIMER),
        }

    def get_hydrological_setback(self, facility_id: str) -> dict[str, Any]:
        """Pond-to-crest D_crest(t) history from the Phase 4 hydrology product."""
        from app.pipelines import hydrology as hyd

        row = self._facility(facility_id)
        prod = self._product(row, "hydrology")
        if prod is None:
            return {"facility_id": facility_id, "available": False, "reason": "no hydrology product for this facility"}
        ds = prod.ds
        if "d_crest_m" not in ds:
            reason = "no crest line supplied" if "water_class" in ds else "no DSWx-HLS water series"
            return {"facility_id": facility_id, "available": False, "reason": f"setback not computed: {reason}",
                    "crest_line": ds.attrs.get("crest_line"), "source_file": prod.file}
        status_names = hyd.SETBACK_STATUS_MEANINGS.split()
        alert_names = {hyd.ALERT_SHRINK_BASELINE: "shrink_vs_baseline", hyd.ALERT_SHRINK_PREVIOUS: "shrink_vs_previous", hyd.ALERT_PROXIMITY: "crest_proximity"}
        series = []
        for i, t in enumerate(ds["time"].values):
            flags = int(ds["setback_alert_flags"].values[i])
            series.append({
                "date": iso_date(t),
                "d_crest_m": r(ds["d_crest_m"].values[i], 1),
                "status": status_names[int(ds["d_crest_status"].values[i])],
                "delta_vs_baseline_m": r(ds["delta_d_crest_baseline_m"].values[i], 1),
                "delta_vs_previous_m": r(ds["delta_d_crest_previous_m"].values[i], 1),
                "alerts": [name for bit, name in alert_names.items() if flags & bit],
                "observed_fraction_pct": r(100.0 * float(ds["observed_fraction"].values[i]), 1),
                "water_area_m2": r(ds["water_area_m2"].values[i], 0) if "water_area_m2" in ds else None,
            })
        valid = [s for s in series if s["status"] == "valid"]
        latest = valid[-1] if valid else None
        return {
            "facility_id": facility_id,
            "available": True,
            "source_file": prod.file,
            "crest_line": ds.attrs.get("crest_line"),
            "thresholds": {"shrink_alert_m": hyd.SETBACK_SHRINK_ALERT_M, "proximity_alert_m": hyd.PROXIMITY_ALERT_M},
            "n_epochs": len(series),
            "n_valid_epochs": len(valid),
            "latest_valid": latest,
            "min_valid_d_crest_m": min((s["d_crest_m"] for s in valid if s["d_crest_m"] is not None), default=None),
            "encroaching": bool(latest and latest["alerts"]),
            "series": series,
        }

    def dashboard_validation_query(self, facility_id: str) -> dict[str, Any]:
        """All four tools plus cross-product consistency checks and GISTM screening candidates."""
        from app.pipelines import coupling as cp
        from app.pipelines import kinematics as kin

        attrs = self.fetch_dam_attributes(facility_id)
        spectral = self.get_spectral_alerts(facility_id)
        kinem = self.get_kinematics(facility_id)
        setback = self.get_hydrological_setback(facility_id)
        row = self._facility(facility_id)
        coupled = self._product(row, "coupled")
        checks: list[dict[str, Any]] = []

        def check(name: str, passed: bool, detail: str) -> None:
            checks.append({"check": name, "passed": bool(passed), "detail": detail})

        alert = attrs["latest_alert"]
        if coupled is not None:
            regime = coupled.ds["regime"].values
            hazard = coupled.ds.attrs.get("facility_hazard_level")
            n_r4 = int((regime == cp.REGIME_4).sum())
            n_r34 = int(np.isin(regime, (cp.REGIME_3, cp.REGIME_4)).sum())
            check("db_alert_matches_coupled_hazard", alert is not None and alert["hazard_level"] == hazard,
                  f"database alert {alert and alert['hazard_level']} vs coupled product {hazard}")
            check("regime4_iff_critical", (n_r4 > 0) == (hazard == "CRITICAL_TIER3"), f"{n_r4} REGIME_4 cells, hazard {hazard}")
            if spectral.get("available") and spectral.get("source_file") == coupled.file:
                check("regime3_4_cells_have_high_sai", n_r34 <= spectral["counts"]["high_sai_pixels_all"],
                      f"{n_r34} REGIME_3/4 cells vs {spectral['counts']['high_sai_pixels_all']} SAI>0.25 cells")
            cstat = coupled.ds["kinematic_status"].values
            r4_accel = bool(np.all(np.isin(cstat[regime == cp.REGIME_4], (kin.ACCEL_FORECAST, kin.ACCEL_NO_TREND))))
            check("regime4_cells_are_accelerating", r4_accel, "every REGIME_4 cell has an accelerating kinematic status")
            state = json.loads(coupled.ds.attrs.get("setback_state", "{}"))
            if setback.get("available"):
                check("setback_state_matches_hydrology", bool(state.get("encroaching")) == setback["encroaching"],
                      f"coupled encroaching={state.get('encroaching')} vs hydrology encroaching={setback['encroaching']}")
            if n_r4 and alert and alert["failure_date_est"]:
                est = coupled.ds["failure_time_estimate"].values[regime == cp.REGIME_4]
                est = est[~np.isnat(est)]
                earliest = iso_date(est.min()) if est.size else None
                check("db_failure_date_matches_coupled", earliest == alert["failure_date_est"],
                      f"database {alert['failure_date_est']} vs earliest REGIME_4 estimate {earliest}")
        else:
            check("coupled_product_present", False, "no coupled classification: risk is indeterminate")

        issues, limitations, recommendations = self._gistm_screening(attrs, spectral, kinem, setback, coupled)
        return {
            "facility_id": facility_id,
            "risk_classification": attrs["risk_classification"],
            "attributes": attrs,
            "spectral": spectral,
            "kinematics": kinem,
            "hydrological_setback": setback,
            "consistency_checks": checks,
            "all_checks_passed": all(c["passed"] for c in checks),
            "gistm_screening": {"candidate_issues": issues, "recommendations": recommendations, "data_limitations": limitations},
        }

    # ------------------------------------------------------------------ GISTM screening rules
    def _gistm_screening(self, attrs, spectral, kinem, setback, coupled) -> tuple[list, list, list]:
        issues, limits, recs = [], [], []
        alert = attrs["latest_alert"]
        counts = (alert or {}).get("regime_counts", {})
        n_r4 = counts.get("regime4-active-instability-tier3-critical", 0)
        if n_r4:
            issues.append({"gistm_principle": GISTM_PRINCIPLES["7"],
                           "issue": "Satellite screening shows co-located accelerating deformation, high seepage alteration and an encroaching pond setback (REGIME_4 cells); monitoring-based trigger and response actions should be reviewed.",
                           "evidence_paths": ["fetch_dam_attributes:latest_alert.regime_counts", "get_kinematics:accelerating_pixels"]})
            issues.append({"gistm_principle": GISTM_PRINCIPLES["13"],
                           "issue": "A Tier-3 screening alert is present; emergency preparedness and response arrangements should be confirmed as current.",
                           "evidence_paths": ["fetch_dam_attributes:latest_alert.hazard_level"]})
            recs += ["Verify the flagged REGIME_4 area with in-situ instrumentation (inclinometers, piezometers, survey prisms) and a field inspection.",
                     "Notify the Engineer of Record and review the trigger-action response plan against the screening evidence."]
        if spectral.get("available") and spectral["counts"]["high_sai_pixels_confident"]:
            issues.append({"gistm_principle": GISTM_PRINCIPLES["7"],
                           "issue": "Spectral screening finds pixels above the SAI threshold, consistent with acid-sulfate seepage alteration on the embankment; seepage and water-quality monitoring coverage should be checked.",
                           "evidence_paths": ["get_spectral_alerts:counts.high_sai_pixels_confident"]})
            recs.append("Sample seepage water and surface precipitates at the high-SAI locations to confirm the mineral signature.")
        if setback.get("available") and setback["encroaching"]:
            issues.append({"gistm_principle": GISTM_PRINCIPLES["6"],
                           "issue": "The pond-to-crest setback triggered a shrink or proximity alert at the latest valid epoch; beach width and pond operation should be compared with design criteria.",
                           "evidence_paths": ["get_hydrological_setback:latest_valid"]})
            recs.append("Compare the observed pond extent with the design minimum beach width and freeboard.")
        if not attrs["crest_line_available"] and not setback.get("available"):
            issues.append({"gistm_principle": GISTM_PRINCIPLES["2"],
                           "issue": "No embankment crest geometry is held in the facility registry, which limits setback assessment from satellite data.",
                           "evidence_paths": ["fetch_dam_attributes:crest_line_available"]})
            recs.append("Add the surveyed crest line to the facility registry.")
        if kinem.get("available") and (kinem.get("unobservable_fraction_pct") or 0) > 20:
            issues.append({"gistm_principle": GISTM_PRINCIPLES["7"],
                           "issue": "A large share of InSAR pixels is unobservable (low coherence), leaving gaps in deformation monitoring coverage.",
                           "evidence_paths": ["get_kinematics:unobservable_fraction_pct"]})
        for name, out in (("spectral", spectral), ("kinematics", kinem), ("hydrological setback", setback)):
            if not out.get("available"):
                limits.append(f"No {name} evidence: {out.get('reason')}.")
        if (alert or {}).get("contains_synthetic_or_mock_inputs") or any(o.get("synthetic_or_mock_inputs") for o in (spectral, kinem)):
            limits.append("Some inputs are SYNTHETIC or MOCK data; this brief is not suitable for regulatory use.")
        if coupled is None:
            limits.append("No coupled classification exists, so the risk classification is indeterminate.")
        limits.append("Deformation is line-of-sight InSAR at about 30 m; spectral evidence is EMIT at 60 m; neither observes internal pore pressure.")
        return issues, limits, recs

    # ------------------------------------------------------------------ dispatch
    def registry(self) -> dict[str, Callable[..., dict[str, Any]]]:
        return {
            "list_facilities": self.list_facilities,
            "fetch_dam_attributes": self.fetch_dam_attributes,
            "get_spectral_alerts": self.get_spectral_alerts,
            "get_kinematics": self.get_kinematics,
            "get_hydrological_setback": self.get_hydrological_setback,
            "dashboard_validation_query": self.dashboard_validation_query,
        }

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        fn = self.registry().get(name)
        if fn is None:
            raise ToolError(f"Unknown tool {name!r}")
        return fn(**arguments)


def _facility_tool(name: str, description: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"facility_id": {"type": "string", "description": "Facility id from list_facilities, e.g. 'sierrita'"}},
            "required": ["facility_id"],
            "additionalProperties": False,
        },
    }


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_facilities",
        "description": "List the managed tailings facilities (id, name, country). Use it to resolve a facility named in the request to its id.",
        "strict": True,
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    _facility_tool("fetch_dam_attributes", "Facility registry metadata, the latest database alert (hazard level, failure-date estimate and error), which products exist, the deterministic risk classification and the disclaimer the brief must carry."),
    _facility_tool("get_spectral_alerts", "Counts of pixels with Seepage Alteration Index above 0.25 (all, and confident-only), SAI statistics and the highest-SAI pixels with 95% intervals."),
    _facility_tool("get_kinematics", "InSAR kinematics: status counts, accelerating pixels, and trend-instability dates with 95% bounds, sorted by earliest date; includes the known late bias of those dates."),
    _facility_tool("get_hydrological_setback", "Pond-to-crest setback D_crest(t) history with per-epoch status, changes and shrink/proximity alerts."),
    _facility_tool("dashboard_validation_query", "All four inspection results in one call, plus consistency checks between products and rule-based GISTM screening candidates (issues, recommendations, data limitations)."),
]
REQUIRED_TOOLS = ("fetch_dam_attributes", "get_spectral_alerts", "get_kinematics", "get_hydrological_setback")
