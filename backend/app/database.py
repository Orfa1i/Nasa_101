"""DuckDB metadata store: facilities and alerts.

On start-up, ``initialize()`` rebuilds the database from three sources:

* ``facilities``:
  * the bundled seed registry ``<repo>/data/global_tailings.geojson``;
  * then ``DATA_DIR/global_tailings.geojson``, if present, which overrides
    the seed field by field (the ``ingest`` command keeps that file up to date).

  The four required facilities (Sierrita, Mission, Bingham Canyon,
  Brumadinho) must be present after the merge, or start-up fails.
* ``alerts``: one row per ``DATA_DIR/cache/<prefix>_coupled.zarr`` (Phase 5),
  taken from its facility hazard level, earliest REGIME_4 failure-time
  estimate and diagnostic evidence.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import duckdb
import numpy as np
import pandas as pd
import pyogrio

from app.config import BACKEND_DIR, Settings

logger = logging.getLogger("lithocreep.database")

SEED_REGISTRY = BACKEND_DIR.parent / "data" / "global_tailings.geojson"
REQUIRED_FACILITIES = ("sierrita", "mission", "bingham", "brumadinho")
HAZARD_LEVELS = ("NONE", "WATCH_TIER1", "ALERT_TIER2", "CRITICAL_TIER3")
FACILITY_COLUMNS = (
    "id", "name", "country", "latitude", "longitude", "bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax",
    "mine_type", "embankment_type", "crest_line_geom", "status", "cache_prefix",
)

SCHEMA = f"""
CREATE TABLE facilities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    country TEXT NOT NULL,
    latitude DOUBLE NOT NULL,
    longitude DOUBLE NOT NULL,
    bbox_xmin DOUBLE NOT NULL,
    bbox_ymin DOUBLE NOT NULL,
    bbox_xmax DOUBLE NOT NULL,
    bbox_ymax DOUBLE NOT NULL,
    mine_type TEXT NOT NULL,
    embankment_type TEXT NOT NULL,      -- "upstream" | "downstream" | "centerline" | "unknown"
    crest_line_geom TEXT,               -- GeoJSON LineString; NULL until a crest line is sourced
    status TEXT NOT NULL DEFAULT 'INACTIVE',
    cache_prefix TEXT NOT NULL,         -- prefix of DATA_DIR/cache/<prefix>_*.zarr products
    CHECK (bbox_xmin < bbox_xmax AND bbox_ymin < bbox_ymax)
);
CREATE SEQUENCE alert_id_seq START 1;
CREATE TABLE alerts (
    id INTEGER PRIMARY KEY DEFAULT nextval('alert_id_seq'),
    facility_id TEXT NOT NULL REFERENCES facilities(id),
    timestamp TIMESTAMP NOT NULL,
    hazard_level TEXT NOT NULL CHECK (hazard_level IN {HAZARD_LEVELS}),
    failure_date_est TIMESTAMP,
    failure_date_error_days DOUBLE,
    diagnostic_evidence TEXT NOT NULL,   -- JSON
    source TEXT NOT NULL
);
"""


class DatabaseError(RuntimeError):
    """Registry or database inconsistencies."""


def _clean(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    return value


def load_registry(settings: Settings) -> pd.DataFrame:
    """Merge the seed registry with ``DATA_DIR/global_tailings.geojson`` (the latter wins per field)."""
    frames = []
    for path in dict.fromkeys([SEED_REGISTRY, settings.data_path / "global_tailings.geojson"]):
        if Path(path).exists():
            frames.append(pyogrio.read_dataframe(path).drop(columns="geometry"))
    if not frames:
        raise DatabaseError(f"No facility registry found ({SEED_REGISTRY})")
    merged: dict[str, dict[str, Any]] = {}
    for frame in frames:
        for row in frame.to_dict(orient="records"):
            record = merged.setdefault(str(row["id"]), {})
            record.update({k: _clean(v) for k, v in row.items() if _clean(v) is not None})
    missing = [f for f in REQUIRED_FACILITIES if f not in merged]
    if missing:
        raise DatabaseError(f"Facility registry is missing required facilities {missing}")
    rows = []
    for fid, r in merged.items():
        crest = r.get("crest_line_geom")
        rows.append(
            {
                "id": fid,
                "name": r["name"],
                "country": r.get("country") or "unknown",
                "latitude": r.get("latitude", (r["bbox_ymin"] + r["bbox_ymax"]) / 2),
                "longitude": r.get("longitude", (r["bbox_xmin"] + r["bbox_xmax"]) / 2),
                "bbox_xmin": r["bbox_xmin"], "bbox_ymin": r["bbox_ymin"],
                "bbox_xmax": r["bbox_xmax"], "bbox_ymax": r["bbox_ymax"],
                "mine_type": r.get("mine_type") or "unknown",
                "embankment_type": r.get("embankment_type") or "unknown",
                "crest_line_geom": crest if isinstance(crest, str) or crest is None else json.dumps(crest),
                "status": r.get("status") or "INACTIVE",
                "cache_prefix": r.get("cache_prefix") or r["name"],
            }
        )
    return pd.DataFrame(rows, columns=FACILITY_COLUMNS)


def _alert_from_coupled(facility_id: str, path: Path) -> Optional[dict[str, Any]]:
    import xarray as xr

    from app.pipelines.coupling import REGIME_4, as_days

    try:
        ds = xr.open_zarr(path)
        attrs = dict(ds.attrs)
        regime = ds["regime"].values
        r4 = regime == REGIME_4
        failure, error_days = None, None
        if r4.any():
            days = as_days(ds["failure_days_after_last_epoch"]).values[r4]
            sig = as_days(ds["failure_days_2sigma"]).values[r4]
            ok = np.isfinite(days)
            if ok.any():
                i = int(np.argmin(np.where(ok, days, np.inf)))
                last = pd.Timestamp(attrs.get("last_insar_epoch") or ds["failure_time_estimate"].values[r4][i])
                failure = (last + pd.Timedelta(days=float(days[i]))).to_pydatetime() if abs(days[i]) < 73_000 else None
                error_days = float(sig[i]) if np.isfinite(sig[i]) else None
    except Exception as exc:  # unreadable cache: skip, but say so
        logger.warning("Cannot read %s (%s: %s); no alert recorded", path, type(exc).__name__, exc)
        return None
    created = attrs.get("created_utc")
    timestamp = pd.Timestamp(created).tz_convert(None) if created else pd.Timestamp(path.stat().st_mtime, unit="s")
    evidence = {
        "regime_counts": json.loads(attrs.get("regime_counts", "{}")),
        "setback_state": json.loads(attrs.get("setback_state", "{}")),
        "input_provenance": json.loads(attrs.get("input_provenance", "{}")),
        "contains_synthetic_or_mock_inputs": bool(attrs.get("contains_synthetic_or_mock_inputs", 0)),
        "coupled_product": path.name,
        "disclaimer": attrs.get("disclaimer", ""),
    }
    hazard = attrs.get("facility_hazard_level", "NONE")
    if hazard not in HAZARD_LEVELS:
        logger.warning("%s has unknown hazard level %r; skipped", path, hazard)
        return None
    return {
        "facility_id": facility_id,
        "timestamp": timestamp.to_pydatetime(),
        "hazard_level": hazard,
        "failure_date_est": failure,
        "failure_date_error_days": error_days,
        "diagnostic_evidence": json.dumps(evidence, default=str),
        "source": str(path),
    }


class Database:
    """Thin wrapper around one DuckDB file; use ``cursor()`` per request/thread."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(self.path))

    def cursor(self) -> duckdb.DuckDBPyConnection:
        return self._con.cursor()

    def close(self) -> None:
        self._con.close()

    def initialize(self, settings: Settings) -> dict[str, int]:
        """Create the schema and repopulate facilities and coupled-product alerts."""
        con = self._con
        facilities = load_registry(settings)
        con.execute("BEGIN TRANSACTION")
        try:
            # The database is a derived cache, rebuilt on every launch. Tables are dropped rather
            # than emptied: DuckDB 0.9 rejects re-inserting a deleted primary key in one transaction.
            con.execute("DROP TABLE IF EXISTS alerts; DROP TABLE IF EXISTS facilities; DROP SEQUENCE IF EXISTS alert_id_seq;")
            con.execute(SCHEMA)
            con.executemany(
                f"INSERT INTO facilities ({', '.join(FACILITY_COLUMNS)}) VALUES ({', '.join('?' * len(FACILITY_COLUMNS))})",
                [tuple(_clean(v) for v in row) for row in facilities.itertuples(index=False)],
            )
            n_alerts = 0
            for fid, prefix in facilities[["id", "cache_prefix"]].itertuples(index=False):
                path = settings.cache_dir / f"{prefix}_coupled.zarr"
                if not path.exists():
                    continue
                alert = _alert_from_coupled(fid, path)
                if alert is None:
                    continue
                con.execute(
                    "INSERT INTO alerts (facility_id, timestamp, hazard_level, failure_date_est, failure_date_error_days, "
                    "diagnostic_evidence, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [alert[k] for k in ("facility_id", "timestamp", "hazard_level", "failure_date_est",
                                        "failure_date_error_days", "diagnostic_evidence", "source")],
                )
                n_alerts += 1
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        counts = {"facilities": len(facilities), "alerts": n_alerts}
        logger.info("Database %s initialised: %s", self.path, counts)
        return counts


def fetch_facilities(cur: duckdb.DuckDBPyConnection, facility_id: Optional[str] = None) -> list[dict[str, Any]]:
    sql = f"SELECT {', '.join(FACILITY_COLUMNS)} FROM facilities"
    params: list[Any] = []
    if facility_id is not None:
        sql += " WHERE id = ?"
        params.append(facility_id)
    rows = cur.execute(sql + " ORDER BY id", params).fetchall()
    return [dict(zip(FACILITY_COLUMNS, row)) for row in rows]


def fetch_alerts(cur: duckdb.DuckDBPyConnection, facility_id: Optional[str] = None) -> list[dict[str, Any]]:
    cols = ("id", "facility_id", "timestamp", "hazard_level", "failure_date_est", "failure_date_error_days", "diagnostic_evidence")
    sql = f"SELECT {', '.join(cols)} FROM alerts"
    params: list[Any] = []
    if facility_id is not None:
        sql += " WHERE facility_id = ?"
        params.append(facility_id)
    rows = cur.execute(sql + " ORDER BY timestamp DESC, id DESC", params).fetchall()
    out = []
    for row in rows:
        rec = dict(zip(cols, row))
        rec["diagnostic_evidence"] = json.loads(rec["diagnostic_evidence"])
        out.append(rec)
    return out


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
