"""Freshness metadata helpers for durable factual memories.

Freshness is advisory evidence for ranking and dashboards; it should not overwrite the original memory truth."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

CURRENT_STATUSES = {"current", "fresh", "valid", "verified", "ok"}
NEEDS_CHECK_STATUSES = {"needs_live_check", "needs-live-check", "unknown", "unchecked", "expired"}
STALE_STATUSES = {"stale", "invalid", "superseded", "outdated"}
VALIDATOR_KINDS = {"file_exists", "command", "http", "manual", "none"}
_VALIDATOR_KIND_ALIASES = {
    "": "none",
    "none": "none",
    "no_validation": "none",
    "static": "none",
    "permanent": "none",
    "file": "file_exists",
    "path": "file_exists",
    "exists": "file_exists",
    "file_exists": "file_exists",
    "shell": "command",
    "cmd": "command",
    "command": "command",
    "http": "http",
    "https": "http",
    "url": "http",
    "http_get": "http",
    "http_head": "http",
    "manual": "manual",
    "manual_live_check": "manual",
    "human": "manual",
}

_SEVERITY = {
    "current": 0,
    "fresh": 0,
    "valid": 0,
    "verified": 0,
    "ok": 0,
    "unknown": 1,
    "unchecked": 1,
    "needs_live_check": 2,
    "needs-live-check": 2,
    "expired": 2,
    "stale": 3,
    "invalid": 3,
    "superseded": 3,
    "outdated": 3,
}


def normalize_validator_kind(kind: Any) -> str:
    """Normalize freshness validator kinds to the public contract.

    Unknown non-empty validators fail closed to `manual` so stale operational
    facts are not accidentally treated as timeless preferences.
    """
    normalized = str(kind or "").strip().lower().replace(" ", "_").replace("-", "_")
    if normalized in VALIDATOR_KINDS:
        return normalized
    return _VALIDATOR_KIND_ALIASES.get(normalized, "manual" if normalized else "none")


def _parse_iso(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_freshness_status(status: Any, *, valid_until: Any = None, now: datetime | None = None) -> str:
    normalized = str(status or "unknown").strip().lower().replace(" ", "_")
    if normalized == "needs-live-check":
        normalized = "needs_live_check"
    if normalized not in CURRENT_STATUSES | NEEDS_CHECK_STATUSES | STALE_STATUSES:
        normalized = "unknown"
    deadline = _parse_iso(valid_until)
    now_dt = now or datetime.now(timezone.utc)
    if deadline is not None and deadline < now_dt and normalized in CURRENT_STATUSES:
        return "expired"
    return normalized


def _row_payload(row: sqlite3.Row, *, now: datetime | None = None) -> dict[str, Any]:
    status = normalize_freshness_status(row["status"], valid_until=row["valid_until"], now=now)
    needs_live_check = status in NEEDS_CHECK_STATUSES or status in STALE_STATUSES
    return {
        "id": str(row["id"]),
        "subject_type": str(row["subject_type"]),
        "subject_id": str(row["subject_id"]),
        "fact_key": str(row["fact_key"]),
        "truth_type": str(row["truth_type"]),
        "validator_kind": normalize_validator_kind(row["validator_kind"]),
        "last_checked_at": str(row["last_checked_at"] or ""),
        "valid_until": str(row["valid_until"] or ""),
        "status": status,
        "stale_reason": str(row["stale_reason"] or ""),
        "superseded_by": str(row["superseded_by"] or ""),
        "needs_live_check": needs_live_check,
        "severity": _SEVERITY.get(status, 1),
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def memory_freshness_map(conn: sqlite3.Connection, memory_ids: Iterable[str], *, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    ids = sorted({str(memory_id) for memory_id in memory_ids if str(memory_id)})
    if not ids or not _table_exists(conn, "fact_freshness"):
        return {}
    placeholders = ",".join("?" for _ in ids)
    try:
        rows = conn.execute(
            f"""
            SELECT id, subject_type, subject_id, fact_key, truth_type, validator_kind,
                   last_checked_at, valid_until, status, stale_reason, superseded_by
            FROM fact_freshness
            WHERE subject_type = 'memory'
              AND subject_id IN ({placeholders})
            ORDER BY updated_at DESC, id ASC
            """,
            ids,
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = _row_payload(row, now=now)
        subject_id = str(payload["subject_id"])
        existing = result.get(subject_id)
        if existing is None or int(payload.get("severity") or 0) > int(existing.get("severity") or 0):
            result[subject_id] = payload
    return result


def freshness_penalty(freshness: dict[str, Any] | None, config: dict[str, Any] | None = None) -> float:
    if not freshness:
        return 0.0
    cfg = config or {}
    status = str(freshness.get("status") or "").strip().lower()
    try:
        if status in STALE_STATUSES:
            return max(0.0, min(0.9, float(cfg.get("fact_freshness_stale_penalty") or 0.35)))
        if status == "expired":
            return max(0.0, min(0.9, float(cfg.get("fact_freshness_expired_penalty") or 0.28)))
        if status in NEEDS_CHECK_STATUSES:
            return max(0.0, min(0.9, float(cfg.get("fact_freshness_needs_live_check_penalty") or 0.18)))
    except (TypeError, ValueError):
        return 0.0
    return 0.0


def attach_freshness_metadata(metadata: dict[str, Any], freshness: dict[str, Any] | None, *, config: dict[str, Any] | None = None) -> float:
    penalty = freshness_penalty(freshness, config)
    if not freshness:
        metadata.setdefault("fact_freshness_status", "untracked")
        metadata.setdefault("needs_live_check", False)
        metadata.setdefault("fact_freshness_penalty", 0.0)
        return 0.0
    metadata["fact_freshness_status"] = str(freshness.get("status") or "unknown")
    metadata["fact_key"] = str(freshness.get("fact_key") or "")
    metadata["truth_type"] = str(freshness.get("truth_type") or "")
    metadata["validator_kind"] = str(freshness.get("validator_kind") or "")
    metadata["last_checked_at"] = str(freshness.get("last_checked_at") or "")
    metadata["valid_until"] = str(freshness.get("valid_until") or "")
    metadata["stale_reason"] = str(freshness.get("stale_reason") or "")
    metadata["needs_live_check"] = bool(freshness.get("needs_live_check"))
    metadata["fact_freshness_penalty"] = penalty
    return penalty


def _scope_filter_sql(scope_ids: Sequence[str] | None) -> tuple[str, list[str]] | None:
    if scope_ids is None:
        return "", []
    scopes = [str(scope_id) for scope_id in scope_ids if str(scope_id)]
    if not scopes:
        return None
    placeholders = ",".join("?" for _ in scopes)
    return f" AND m.scope_id IN ({placeholders})", scopes


def fact_freshness_report(conn: sqlite3.Connection, *, scope_ids: Sequence[str] | None = None) -> dict[str, Any]:
    """Summarize freshness coverage and stale durable facts.

    Freshness reports guide review and ranking policy without rewriting the underlying factual memory. When a scope allowlist is provided, only memory-scoped freshness rows for those scopes are counted.
    """
    if not _table_exists(conn, "fact_freshness"):
        return {
            "status": "schema_missing",
            "tracked_facts": 0,
            "by_status": {},
            "needs_live_check": 0,
            "stale_facts": 0,
            "by_validator_kind": {},
            "coverage": {"factual_memories": 0, "tracked_memory_facts": 0, "coverage_percent": 0.0},
        }
    scope_filter = _scope_filter_sql(scope_ids)
    if scope_filter is None:
        rows = []
    elif scope_ids is None:
        rows = conn.execute(
            """
            SELECT f.id, f.subject_type, f.subject_id, f.fact_key, f.truth_type, f.validator_kind,
                   f.last_checked_at, f.valid_until, f.status, f.stale_reason, f.superseded_by
            FROM fact_freshness AS f
            JOIN memories AS m ON m.id = f.subject_id
            WHERE f.subject_type = 'memory'
            """
        ).fetchall()
    else:
        scope_sql, scope_params = scope_filter
        rows = conn.execute(
            f"""
            SELECT f.id, f.subject_type, f.subject_id, f.fact_key, f.truth_type, f.validator_kind,
                   f.last_checked_at, f.valid_until, f.status, f.stale_reason, f.superseded_by
            FROM fact_freshness AS f
            JOIN memories AS m ON m.id = f.subject_id
            WHERE f.subject_type = 'memory'
              {scope_sql}
            """,
            scope_params,
        ).fetchall()
    by_status: dict[str, int] = {}
    by_validator_kind: dict[str, int] = {}
    needs_live_check = 0
    stale_facts = 0
    for row in rows:
        payload = _row_payload(row)
        status = str(payload["status"])
        validator_kind = str(payload["validator_kind"] or "none")
        by_status[status] = by_status.get(status, 0) + 1
        by_validator_kind[validator_kind] = by_validator_kind.get(validator_kind, 0) + 1
        if bool(payload.get("needs_live_check")):
            needs_live_check += 1
        if status in STALE_STATUSES:
            stale_facts += 1
    factual_memories = 0
    tracked_memory_facts = len({str(row["subject_id"]) for row in rows if str(row["subject_type"]) == "memory"})
    if _table_exists(conn, "memories"):
        if scope_filter is None:
            factual_memories = 0
        else:
            scope_sql, scope_params = scope_filter
            factual_memories = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM memories AS m
                    WHERE COALESCE(
                            CASE WHEN json_valid(m.metadata) THEN json_extract(m.metadata, '$.lifecycle') ELSE '' END,
                            ''
                          ) NOT IN ('archived', 'superseded', 'obsolete', 'rejected')
                      AND LOWER(COALESCE(
                            CASE WHEN json_valid(m.metadata) THEN json_extract(m.metadata, '$.memory_type') ELSE '' END,
                            CASE WHEN json_valid(m.metadata) THEN json_extract(m.metadata, '$.category') ELSE '' END,
                            ''
                          )) IN ('factual', 'fact', 'project_fact', 'environment_fact')
                      {scope_sql}
                    """,
                    scope_params,
                ).fetchone()[0]
            )
    coverage_percent = round((tracked_memory_facts / factual_memories) * 100.0, 3) if factual_memories else 0.0
    status = "needs_review" if needs_live_check else "ready"
    return {
        "status": status,
        "tracked_facts": len(rows),
        "by_status": dict(sorted(by_status.items())),
        "by_validator_kind": dict(sorted(by_validator_kind.items())),
        "needs_live_check": needs_live_check,
        "stale_facts": stale_facts,
        "coverage": {
            "factual_memories": factual_memories,
            "tracked_memory_facts": tracked_memory_facts,
            "coverage_percent": coverage_percent,
        },
    }
