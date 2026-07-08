"""Read-only doctor checks for event-driven candidate extraction."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _config_bool(raw: Any, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if raw is None:
        return default
    return bool(raw)


def _age_hours(iso_value: Any) -> float:
    if not iso_value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
    except Exception:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, round((datetime.now(timezone.utc) - parsed).total_seconds() / 3600, 3))


def event_digest_config_summary(runtime_config: dict[str, Any]) -> dict[str, Any]:
    raw = runtime_config.get("event_digest")
    config = raw if isinstance(raw, dict) else {}
    return {
        "enabled": _config_bool(config.get("enabled"), True),
        "write_candidates": _config_bool(config.get("write_candidates"), False),
        "dry_run_log": _config_bool(config.get("dry_run_log"), True),
        "max_events_per_turn": int(config.get("max_events_per_turn") or 3),
    }


def event_digest_report(hermes_home: Path, runtime_config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Inspect event-digest config and persisted candidate/audit evidence read-only."""
    config = event_digest_config_summary(runtime_config)
    db_path = hermes_home / "scope-recall" / "memory.sqlite3"
    recommendations: list[str] = []
    payload: dict[str, Any] = {
        "status": "disabled" if not config["enabled"] else "ready",
        "config": config,
        "path": str(db_path),
        "candidates_persisted": 0,
        "oldest_candidate_at": "",
        "oldest_candidate_age_hours": 0,
        "high_risk_candidate_count": 0,
        "audit_events": 0,
        "insert_candidate_events": 0,
        "updated_existing_events": 0,
        "audit_missing": 0,
    }
    if not db_path.exists():
        payload["status"] = "missing_db"
        return payload, {"ok": True, "failures": []}, recommendations
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "memories" in tables:
                payload["candidates_persisted"] = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM memories
                        WHERE source = 'event-digest'
                          AND json_valid(metadata)
                          AND LOWER(COALESCE(json_extract(metadata, '$.lifecycle'), '')) = 'candidate'
                        """
                    ).fetchone()[0]
                )
                oldest_row = conn.execute(
                    """
                    SELECT MIN(created_at) FROM memories
                    WHERE source = 'event-digest'
                      AND json_valid(metadata)
                      AND LOWER(COALESCE(json_extract(metadata, '$.lifecycle'), '')) = 'candidate'
                    """
                ).fetchone()
                oldest_at = str(oldest_row[0] or "") if oldest_row is not None else ""
                payload["oldest_candidate_at"] = oldest_at
                payload["oldest_candidate_age_hours"] = _age_hours(oldest_at)
                payload["high_risk_candidate_count"] = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM memories
                        WHERE source = 'event-digest'
                          AND json_valid(metadata)
                          AND LOWER(COALESCE(json_extract(metadata, '$.lifecycle'), '')) = 'candidate'
                          AND (
                            LOWER(COALESCE(json_extract(metadata, '$.risk_class'), '')) = 'high'
                            OR LOWER(COALESCE(json_extract(metadata, '$.risk'), '')) = 'high'
                            OR LOWER(COALESCE(json_extract(metadata, '$.risk_flags'), '')) LIKE '%high%'
                          )
                        """
                    ).fetchone()[0]
                )
            if "governance_audit_events" in tables:
                payload["audit_events"] = int(
                    conn.execute("SELECT COUNT(*) FROM governance_audit_events WHERE event_type='event_candidate'").fetchone()[0]
                )
                payload["insert_candidate_events"] = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM governance_audit_events WHERE event_type='event_candidate' AND action='insert_candidate'"
                    ).fetchone()[0]
                )
                payload["updated_existing_events"] = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM governance_audit_events WHERE event_type='event_candidate' AND action='dedupe_existing_candidate'"
                    ).fetchone()[0]
                )
            payload["audit_missing"] = max(0, int(payload["candidates_persisted"]) - int(payload["audit_events"]))
        finally:
            conn.close()
    except Exception as exc:
        payload["status"] = "error"
        payload["error"] = str(exc)
        return payload, {"ok": False, "failures": [f"event digest report failed: {exc}"]}, [
            "Inspect the SQLite truth DB before enabling event-digest candidate writes."
        ]
    failures: list[str] = []
    if payload["audit_missing"]:
        failures.append(f"event-digest candidate audit coverage missing for {payload['audit_missing']} row(s)")
        recommendations.append("Review event-digest candidate rows and backfill governance audit evidence before enabling unattended writes.")
    if config["write_candidates"] and not config["dry_run_log"]:
        recommendations.append("event_digest.write_candidates is enabled; keep dry_run_log enabled during rollout for observability.")
    return payload, {"ok": not failures, "failures": failures}, recommendations
