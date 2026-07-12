"""Freshness metadata helpers for durable factual memories.

Freshness is advisory evidence for ranking and dashboards; it should not overwrite the original memory truth."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from .capture_filters import sanitize_report_text, sanitize_structured_value
from .lifecycle_policy import durable_lifecycle_visible_sql, ordinary_recall_lifecycle_visible_sql

CURRENT_STATUSES = {"current", "fresh", "valid", "verified", "ok"}
NEEDS_CHECK_STATUSES = {"needs_live_check", "needs-live-check", "unknown", "unchecked", "expired"}
STALE_STATUSES = {"stale", "invalid", "superseded", "outdated"}
VALIDATOR_KINDS = {"file_exists", "command", "http", "manual", "none"}
FACTUAL_MEMORY_TYPES = {"environment_fact", "fact", "factual", "project_fact"}
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


def _normalized_fact_key(value: Any) -> str:
    text = str(value or "memory_fact").strip().lower().replace(" ", "_")
    cleaned = re.sub(r"[^a-z0-9_.:-]+", "_", text).strip("_.:-")
    return (cleaned or "memory_fact")[:120]


def _safe_validator_spec(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}

    sanitized, _ = sanitize_structured_value(value)
    if not isinstance(sanitized, dict):
        return {}

    def clean(item: Any) -> Any:
        if isinstance(item, dict):
            return {str(key)[:80]: clean(child) for key, child in list(item.items())[:50]}
        if isinstance(item, list):
            return [clean(child) for child in item[:50]]
        if isinstance(item, (bool, int, float)) or item is None:
            return item
        return sanitize_report_text(str(item))[:1000]

    return clean(sanitized)


def _freshness_spec_from_metadata(
    metadata: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    payload = dict(metadata or {})
    memory_type = str(payload.get("memory_type") or payload.get("category") or "").strip().lower()
    raw_freshness = payload.get("freshness")
    explicit = dict(raw_freshness) if isinstance(raw_freshness, dict) else {}
    if memory_type not in FACTUAL_MEMORY_TYPES and not explicit:
        return None

    now_dt = now or datetime.now(timezone.utc)
    valid_until_dt = _parse_iso(explicit.get("valid_until") or payload.get("valid_until"))
    requested_status = explicit.get("status") or payload.get("freshness_status") or payload.get("fact_freshness_status")
    status = normalize_freshness_status(requested_status or "needs_live_check", valid_until=valid_until_dt, now=now_dt)
    validator_kind = normalize_validator_kind(explicit.get("validator_kind") or payload.get("validator_kind") or "manual")
    try:
        ttl_days = max(0, int(explicit.get("ttl_days") or payload.get("ttl_days") or 0))
    except (TypeError, ValueError):
        ttl_days = 0
    checked_dt = _parse_iso(explicit.get("last_checked_at") or payload.get("last_checked_at"))
    if status in CURRENT_STATUSES and checked_dt is None:
        checked_dt = now_dt
    if status in CURRENT_STATUSES and ttl_days > 0 and valid_until_dt is None:
        valid_until_dt = (checked_dt or now_dt) + timedelta(days=ttl_days)

    return {
        "fact_key": _normalized_fact_key(explicit.get("fact_key") or payload.get("fact_key")),
        "truth_type": str(explicit.get("truth_type") or payload.get("truth_type") or memory_type or "factual")[:80],
        "validator_kind": validator_kind,
        "validator_spec": _safe_validator_spec(explicit.get("validator_spec") or payload.get("validator_spec")),
        "ttl_days": ttl_days,
        "last_checked_at": checked_dt.isoformat() if checked_dt is not None else "",
        "valid_until": valid_until_dt.isoformat() if valid_until_dt is not None else "",
        "status": status,
        "stale_reason": sanitize_report_text(explicit.get("stale_reason") or payload.get("stale_reason") or "")[:500],
        "superseded_by": str(explicit.get("superseded_by") or payload.get("superseded_by") or "")[:120],
    }


def upsert_memory_freshness(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    metadata: dict[str, Any] | None,
    now: datetime | None = None,
    commit: bool = True,
) -> str:
    """Idempotently write advisory freshness for one stored memory.

    Unverified factual writes fail closed to ``needs_live_check``. The helper
    never invents a current status.
    """

    spec = _freshness_spec_from_metadata(metadata, now=now)
    if spec is None or not memory_id or not _table_exists(conn, "fact_freshness"):
        return ""
    now_iso = (now or datetime.now(timezone.utc)).isoformat()
    existing = conn.execute(
        """
        SELECT id, created_at
        FROM fact_freshness
        WHERE subject_type = 'memory' AND subject_id = ? AND fact_key = ?
        ORDER BY updated_at DESC, id ASC
        LIMIT 1
        """,
        (str(memory_id), spec["fact_key"]),
    ).fetchone()
    freshness_id = str(existing["id"]) if existing is not None else uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"scope-recall:memory:{memory_id}:{spec['fact_key']}",
    ).hex
    created_at = str(existing["created_at"] or now_iso) if existing is not None else now_iso
    conn.execute(
        """
        INSERT INTO fact_freshness(
            id, subject_type, subject_id, fact_key, truth_type, validator_kind,
            validator_spec, ttl_days, last_checked_at, valid_until, status,
            stale_reason, superseded_by, created_at, updated_at
        ) VALUES (?, 'memory', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            truth_type = excluded.truth_type,
            validator_kind = excluded.validator_kind,
            validator_spec = excluded.validator_spec,
            ttl_days = excluded.ttl_days,
            last_checked_at = excluded.last_checked_at,
            valid_until = excluded.valid_until,
            status = excluded.status,
            stale_reason = excluded.stale_reason,
            superseded_by = excluded.superseded_by,
            updated_at = excluded.updated_at
        """,
        (
            freshness_id,
            str(memory_id),
            spec["fact_key"],
            spec["truth_type"],
            spec["validator_kind"],
            json.dumps(spec["validator_spec"], ensure_ascii=False, sort_keys=True),
            spec["ttl_days"],
            spec["last_checked_at"] or None,
            spec["valid_until"] or None,
            spec["status"],
            spec["stale_reason"],
            spec["superseded_by"],
            created_at,
            now_iso,
        ),
    )
    if commit:
        conn.commit()
    return freshness_id


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


def _metadata_memory_type_sql(alias: str) -> str:
    """Return normalized memory type with blank values falling back to category."""

    return f"""
        LOWER(COALESCE(
            NULLIF(TRIM(CASE WHEN json_valid({alias}.metadata) THEN json_extract({alias}.metadata, '$.memory_type') ELSE '' END), ''),
            NULLIF(TRIM(CASE WHEN json_valid({alias}.metadata) THEN json_extract({alias}.metadata, '$.category') ELSE '' END), ''),
            ''
        ))
    """.strip()


def backfill_untracked_memory_freshness(
    conn: sqlite3.Connection,
    *,
    scope_ids: Sequence[str] | None = None,
    apply: bool = False,
    limit: int = 500,
) -> dict[str, Any]:
    """Plan or apply a bounded freshness backfill for visible factual rows."""

    if not _table_exists(conn, "fact_freshness") or not _table_exists(conn, "memories"):
        return {"apply": bool(apply), "eligible": 0, "inserted": 0, "ids": []}
    clauses = [
        ordinary_recall_lifecycle_visible_sql("m"),
        f"{_metadata_memory_type_sql('m')} IN ('factual','fact','project_fact','environment_fact')",
        "NOT EXISTS (SELECT 1 FROM fact_freshness f WHERE f.subject_type = 'memory' AND f.subject_id = m.id)",
    ]
    params: list[Any] = []
    scopes = [str(scope_id) for scope_id in (scope_ids or []) if str(scope_id)]
    if scope_ids is not None:
        if not scopes:
            return {"apply": bool(apply), "eligible": 0, "inserted": 0, "ids": []}
        clauses.append(f"m.scope_id IN ({','.join('?' for _ in scopes)})")
        params.extend(scopes)
    bounded_limit = max(1, min(int(limit or 500), 5000))
    rows = conn.execute(
        f"SELECT m.id, m.metadata FROM memories m WHERE {' AND '.join(clauses)} ORDER BY m.updated_at DESC, m.id ASC LIMIT ?",
        [*params, bounded_limit],
    ).fetchall()
    ids = [str(row["id"]) for row in rows]
    inserted = 0
    if apply and rows:
        try:
            conn.execute("BEGIN")
            for row in rows:
                try:
                    metadata = json.loads(str(row["metadata"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}
                if upsert_memory_freshness(conn, memory_id=str(row["id"]), metadata=metadata, commit=False):
                    inserted += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"apply": bool(apply), "eligible": len(rows), "inserted": inserted, "ids": ids}


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
    lifecycle_sql = ordinary_recall_lifecycle_visible_sql("m")
    advisory_lifecycle_sql = durable_lifecycle_visible_sql("m")
    factual_type_sql = f"{_metadata_memory_type_sql('m')} IN ('factual', 'fact', 'project_fact', 'environment_fact')"
    if scope_filter is None:
        rows = []
    else:
        scope_sql, scope_params = scope_filter
        rows = conn.execute(
            f"""
            SELECT f.id, f.subject_type, f.subject_id, f.fact_key, f.truth_type, f.validator_kind,
                   f.last_checked_at, f.valid_until, f.status, f.stale_reason, f.superseded_by,
                   CASE WHEN ({lifecycle_sql}) AND ({factual_type_sql}) THEN 1 ELSE 0 END AS cohort_factual
            FROM fact_freshness AS f
            JOIN memories AS m ON m.id = f.subject_id
            WHERE f.subject_type = 'memory'
              AND {advisory_lifecycle_sql}
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
    tracked_memory_facts = len(
        {
            str(row["subject_id"])
            for row in rows
            if str(row["subject_type"] or "") == "memory" and bool(row["cohort_factual"])
        }
    )
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
                    WHERE {lifecycle_sql}
                      AND {factual_type_sql}
                      {scope_sql}
                    """,
                    scope_params,
                ).fetchone()[0]
            )
    coverage_percent = round((tracked_memory_facts / factual_memories) * 100.0, 3) if factual_memories else 0.0
    coverage_incomplete = tracked_memory_facts < factual_memories
    status = "needs_review" if needs_live_check or coverage_incomplete else "ready"
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
