"""Governance cleanup planners and apply helpers for legacy archive/audit hygiene.

Cleanup paths must stay auditable and fail closed when evidence or transaction safety is missing."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from typing import Any, Sequence

from .capture_filters import sanitize_report_text
from .gating import compact_text
from .governance_rollback import (
    receipt_binds_current_archive,
    rollback_cleanup_batch as rollback_cleanup_batch,
    snapshot_memory_row,
)
from .lifecycle_service import transition_memory_lifecycle
from .lifecycle_registry import (
    LEGACY_ARCHIVE_BACKFILL,
    MEMORY_CLEANUP_ARCHIVE,
    archive_coverage_receipts,
    resolve_lifecycle_operation,
)
from .maintenance_ops import json_dumps_stable, make_batch_id, now_utc_iso
from .sql_store import ensure_schema, record_governance_audit_event

TEMPLATE_NOISE_REASONS = {
    "template.operations-workflow-summary",
    "template.journal-digest-memory",
    "transcript.role-prefix-user",
    "transcript.role-prefix-assistant",
}

RECOGNIZED_ARCHIVE_RECEIPTS = archive_coverage_receipts()


def _now_iso() -> str:
    return now_utc_iso()


def _json_loads(raw: Any) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    try:
        value = json.loads(str(raw))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}



def _json_dumps(value: Any) -> str:
    return json_dumps_stable(value)


def _is_archived(row: sqlite3.Row) -> bool:
    return str(_json_loads(row["metadata"]).get("lifecycle") or "").strip().lower() == "archived"


def _has_new_archive_marker(metadata: dict[str, Any]) -> bool:
    return any(str(metadata.get(key) or "").strip() for key in ("rollback_batch_id", "candidate_promotion_batch_id", "archived_batch_id"))


def _percent(part: int, total: int) -> float:
    if total <= 0:
        return 100.0
    return round((float(part) / float(total)) * 100.0, 3)


def _governance_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='governance_audit_events'").fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _recognized_archive_pair_sql() -> tuple[str, list[str]]:
    pairs = sorted(RECOGNIZED_ARCHIVE_RECEIPTS)
    clause = " OR ".join("(event_type = ? AND action = ?)" for _ in pairs)
    params = [value for pair in pairs for value in pair]
    return clause, params



def _audited_archive_ids(
    conn: sqlite3.Connection,
    archived_rows: Sequence[sqlite3.Row],
) -> set[str]:
    if not _governance_table_exists(conn):
        return set()
    pair_clause, pair_params = _recognized_archive_pair_sql()
    rows = conn.execute(
        f"""
        SELECT target_id, after_json
        FROM governance_audit_events
        WHERE dry_run = 0
          AND target_id != ''
          AND ({pair_clause})
        ORDER BY rowid ASC
        """,
        pair_params,
    ).fetchall()
    latest_receipts: dict[str, dict[str, Any]] = {}
    for row in rows:
        target_id = str(row["target_id"] if isinstance(row, sqlite3.Row) else row[0])
        after_raw = row["after_json"] if isinstance(row, sqlite3.Row) else row[1]
        if not target_id:
            continue
        # SQLite insertion order is authoritative when application timestamps tie.
        # Replacing oldest -> newest leaves exactly the latest recognized receipt,
        # so an older match cannot mask a newer stale or malformed receipt.
        latest_receipts[target_id] = _json_loads(after_raw)
    audited: set[str] = set()
    for row in archived_rows:
        current = snapshot_memory_row(row)
        target_id = str(current["id"])
        latest_after = latest_receipts.get(target_id)
        if latest_after is not None and receipt_binds_current_archive(
            latest_after, current
        ):
            audited.add(target_id)
    return audited


def classify_cleanup_reason(row: sqlite3.Row) -> str:
    """Return a stable cleanup reason for historical template/transcript noise."""

    content = str(row["content"] or "")
    lowered = content.lower().lstrip()
    if lowered.startswith("operations workflow summary from journal digest:") or lowered.startswith("operations workflow summary"):
        return "template.operations-workflow-summary"
    if lowered.startswith("journal digest memory"):
        return "template.journal-digest-memory"
    if re.search(r"(?:^|[\s。；;])user:\s*", lowered):
        return "transcript.role-prefix-user"
    if re.search(r"(?:^|[\s。；;])assistant:\s*", lowered):
        return "transcript.role-prefix-assistant"
    return ""


def _scope_clause(scope_ids: Sequence[str] | None) -> tuple[str, list[str]]:
    if scope_ids is None:
        return "", []
    scopes = [str(item) for item in scope_ids if str(item)]
    if not scopes:
        return " AND 0", []
    placeholders = ",".join("?" for _ in scopes)
    return f" AND scope_id IN ({placeholders})", scopes


def active_dirty_counts(conn: sqlite3.Connection, *, scope_ids: Sequence[str] | None = None) -> dict[str, int]:
    scope_sql, params = _scope_clause(scope_ids)
    rows = conn.execute(
        f"""
        SELECT content, metadata, scope_id
        FROM memories
        WHERE 1=1 {scope_sql}
        """,
        params,
    ).fetchall()
    counts = {reason: 0 for reason in sorted(TEMPLATE_NOISE_REASONS)}
    for row in rows:
        if _is_archived(row):
            continue
        reason = classify_cleanup_reason(row)
        if reason:
            counts[reason] += 1
    return counts


def find_cleanup_candidates(
    conn: sqlite3.Connection,
    *,
    scope_ids: Sequence[str] | None = None,
    include_archived: bool = False,
    limit: int = 500,
) -> list[dict[str, Any]]:
    scope_sql, params = _scope_clause(scope_ids)
    rows = conn.execute(
        f"""
        SELECT id, scope_id, source, target, content, summary, created_at, updated_at, metadata
        FROM memories
        WHERE 1=1 {scope_sql}
        ORDER BY updated_at DESC, id ASC
        """,
        params,
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    max_items = max(0, int(limit))
    for row in rows:
        if not include_archived and _is_archived(row):
            continue
        reason = classify_cleanup_reason(row)
        if not reason:
            continue
        candidates.append(
            {
                "id": str(row["id"]),
                "scope_id": str(row["scope_id"] or ""),
                "target": str(row["target"] or ""),
                "source": str(row["source"] or ""),
                "reason": reason,
                "updated_at": str(row["updated_at"] or ""),
                "preview": compact_text(sanitize_report_text(str(row["content"] or "")), 180),
            }
        )
        if max_items and len(candidates) >= max_items:
            break
    return candidates



def apply_cleanup(
    conn: sqlite3.Connection,
    *,
    scope_ids: Sequence[str] | None = None,
    dry_run: bool = True,
    limit: int = 500,
    reason: str = "historical-template-noise",
    actor: str = "governance.cleanup.py",
    batch_id: str | None = None,
) -> dict[str, Any]:
    """Apply a reviewed governance cleanup plan.

    The apply path must keep archive/delete counts, audit receipts, and rollback batch IDs aligned with the dry-run plan."""
    if not dry_run:
        ensure_schema(conn)
    batch = batch_id or make_batch_id("cleanup")
    candidates = find_cleanup_candidates(conn, scope_ids=scope_ids, include_archived=False, limit=limit)
    result = {
        "dry_run": bool(dry_run),
        "batch_id": batch,
        "candidate_count": len(candidates),
        "archived": 0,
        "archive_ids": [item["id"] for item in candidates],
        "reason_counts": {},
        "items": candidates,
    }
    reason_counts: dict[str, int] = {}
    for item in candidates:
        reason_counts[item["reason"]] = reason_counts.get(item["reason"], 0) + 1
    result["reason_counts"] = reason_counts
    if dry_run or not candidates:
        return result

    archived = 0
    now = _now_iso()
    for item in candidates:
        row = conn.execute(
            "SELECT id, scope_id, source, target, content, summary, updated_at, metadata FROM memories WHERE id = ?",
            (item["id"],),
        ).fetchone()
        if row is None or _is_archived(row):
            continue
        before = snapshot_memory_row(row)
        metadata = dict(before["metadata"])
        transition_memory_lifecycle(
            conn,
            memory_id=str(item["id"]),
            lifecycle="archived",
            metadata_updates={
                **metadata,
                "forget_reason": item["reason"],
                "archived_at": now,
                "archived_by": actor,
                "rollback_batch_id": batch,
                "cleanup_reason": reason,
            },
            expected_updated_at=str(row["updated_at"] or ""),
            actor=actor,
            reason=item["reason"],
            operation_id=MEMORY_CLEANUP_ARCHIVE,
            batch_id=batch,
            timestamp=now,
        )
        archived += 1
    conn.commit()
    result["archived"] = archived
    return result


def _archive_coverage_samples(rows: list[sqlite3.Row], *, limit: int) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    max_items = max(0, int(limit))
    for row in rows[:max_items]:
        metadata = _json_loads(row["metadata"])
        samples.append(
            {
                "id": str(row["id"]),
                "scope_id": str(row["scope_id"] or ""),
                "source": str(row["source"] or ""),
                "target": str(row["target"] or ""),
                "updated_at": str(row["updated_at"] or ""),
                "archived_by": str(metadata.get("archived_by") or ""),
                "rollback_batch_id": str(metadata.get("rollback_batch_id") or ""),
                "preview": compact_text(sanitize_report_text(str(row["content"] or row["summary"] or "")), 180),
            }
        )
    return samples


def governance_audit_coverage_report(
    conn: sqlite3.Connection,
    *,
    scope_ids: Sequence[str] | None = None,
    sample_limit: int = 8,
) -> dict[str, Any]:
    """Report which archived/cleaned rows have sufficient governance audit evidence.

    Coverage for a current archived row requires a recognized receipt whose
    after snapshot is bound to the current archived state, not merely any
    historical receipt for the same target_id.
    """
    required_columns = {"id", "scope_id", "source", "target", "content", "summary", "updated_at", "metadata"}
    memory_columns = _table_columns(conn, "memories")
    missing_columns = sorted(required_columns - memory_columns)
    if missing_columns:
        return {
            "status": "schema_missing",
            "missing_columns": missing_columns,
            "archived_total": 0,
            "archived_with_audit": 0,
            "archived_without_audit": 0,
            "coverage_percent": 100.0,
            "new_mutation_coverage": {"archived_total": 0, "with_audit": 0, "missing_audit": 0, "coverage_percent": 100.0, "ok": True},
            "legacy_coverage": {"archived_total": 0, "with_audit": 0, "missing_audit": 0, "coverage_percent": 100.0, "backfill_candidates": 0},
            "samples": {"new_missing_audit": [], "legacy_missing_audit": []},
        }
    scope_sql, params = _scope_clause(scope_ids)
    rows = conn.execute(
        f"""
        SELECT id, scope_id, source, target, content, summary, updated_at, metadata
        FROM memories
        WHERE 1=1 {scope_sql}
        ORDER BY updated_at DESC, id ASC
        """,
        params,
    ).fetchall()
    archived_rows = [row for row in rows if _is_archived(row)]
    audited_ids = _audited_archive_ids(conn, archived_rows)
    audited_rows = [row for row in archived_rows if str(row["id"]) in audited_ids]
    missing_rows = [row for row in archived_rows if str(row["id"]) not in audited_ids]
    new_rows = [row for row in archived_rows if _has_new_archive_marker(_json_loads(row["metadata"]))]
    new_missing_rows = [row for row in new_rows if str(row["id"]) not in audited_ids]
    legacy_rows = [row for row in archived_rows if not _has_new_archive_marker(_json_loads(row["metadata"]))]
    legacy_missing_rows = [row for row in legacy_rows if str(row["id"]) not in audited_ids]
    status = "ready"
    if new_missing_rows:
        status = "needs_repair"
    elif legacy_missing_rows:
        status = "needs_review"
    new_audited = len(new_rows) - len(new_missing_rows)
    legacy_audited = len(legacy_rows) - len(legacy_missing_rows)
    return {
        "status": status,
        "archived_total": len(archived_rows),
        "archived_with_audit": len(audited_rows),
        "archived_without_audit": len(missing_rows),
        "coverage_percent": _percent(len(audited_rows), len(archived_rows)),
        "new_mutation_coverage": {
            "archived_total": len(new_rows),
            "with_audit": new_audited,
            "missing_audit": len(new_missing_rows),
            "coverage_percent": _percent(new_audited, len(new_rows)),
            "ok": len(new_missing_rows) == 0,
        },
        "legacy_coverage": {
            "archived_total": len(legacy_rows),
            "with_audit": legacy_audited,
            "missing_audit": len(legacy_missing_rows),
            "coverage_percent": _percent(legacy_audited, len(legacy_rows)),
            "backfill_candidates": len(legacy_missing_rows),
        },
        "samples": {
            "new_missing_audit": _archive_coverage_samples(new_missing_rows, limit=sample_limit),
            "legacy_missing_audit": _archive_coverage_samples(legacy_missing_rows, limit=sample_limit),
        },
    }


def backfill_legacy_archive_audit(
    conn: sqlite3.Connection,
    *,
    scope_ids: Sequence[str] | None = None,
    dry_run: bool = True,
    limit: int = 500,
    batch_id: str | None = None,
    actor: str = "governance.audit_coverage.py",
) -> dict[str, Any]:
    """Backfill governance audit evidence for legacy archived rows.

    Backfill creates rollback context for old mutations without pretending it knows the original operator intent."""
    if not dry_run:
        ensure_schema(conn)
    batch = batch_id or make_batch_id("governance-audit-backfill")
    required_columns = {"id", "scope_id", "source", "target", "content", "summary", "updated_at", "metadata"}
    memory_columns = _table_columns(conn, "memories")
    missing_columns = sorted(required_columns - memory_columns)
    if missing_columns:
        return {
            "dry_run": bool(dry_run),
            "batch_id": batch,
            "candidate_count": 0,
            "backfilled": 0,
            "backfill_ids": [],
            "items": [],
            "status": "schema_missing",
            "missing_columns": missing_columns,
        }
    scope_sql, params = _scope_clause(scope_ids)
    rows = conn.execute(
        f"""
        SELECT id, scope_id, source, target, content, summary, updated_at, metadata
        FROM memories
        WHERE 1=1 {scope_sql}
        ORDER BY updated_at DESC, id ASC
        """,
        params,
    ).fetchall()
    archived_rows = [row for row in rows if _is_archived(row)]
    audited_ids = _audited_archive_ids(conn, archived_rows)
    candidates = [row for row in archived_rows if str(row["id"]) not in audited_ids and not _has_new_archive_marker(_json_loads(row["metadata"]))]
    max_items = max(0, int(limit))
    if max_items:
        candidates = candidates[:max_items]
    result = {
        "dry_run": bool(dry_run),
        "batch_id": batch,
        "candidate_count": len(candidates),
        "backfilled": 0,
        "backfill_ids": [str(row["id"]) for row in candidates],
        "items": _archive_coverage_samples(candidates, limit=len(candidates)),
    }
    if dry_run or not candidates:
        return result
    now = _now_iso()
    operation = resolve_lifecycle_operation(LEGACY_ARCHIVE_BACKFILL)
    for row in candidates:
        snapshot = snapshot_memory_row(row)
        record_governance_audit_event(
            conn,
            event_id=f"gov_{uuid.uuid4().hex}",
            event_type=operation.legacy_event_type,
            action=operation.legacy_action,
            scope_id=str(row["scope_id"] or ""),
            target_id=str(row["id"]),
            batch_id=batch,
            before=snapshot,
            after=snapshot,
            reason="legacy archived memory lacked governance audit; this event records existing archived state only",
            actor=actor,
            dry_run=False,
            created_at=now,
        )
    conn.commit()
    result["backfilled"] = len(candidates)
    return result
