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
from .graph import sync_memory_entities
from .maintenance_ops import json_dumps_stable, make_batch_id, now_utc_iso
from .sql_store import ensure_schema, record_governance_audit_event

TEMPLATE_NOISE_REASONS = {
    "template.operations-workflow-summary",
    "template.journal-digest-memory",
    "transcript.role-prefix-user",
    "transcript.role-prefix-assistant",
}


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


def _audited_archive_ids(conn: sqlite3.Connection) -> set[str]:
    if not _governance_table_exists(conn):
        return set()
    rows = conn.execute(
        """
        SELECT DISTINCT target_id
        FROM governance_audit_events
        WHERE dry_run = 0
          AND target_id != ''
          AND (
              action IN ('soft_archive', 'legacy_archive_backfill')
              OR (event_type = 'memory_candidate_promotion' AND action = 'archive')
              OR (event_type = 'memory_quality_lint' AND action = 'archive_lint_hit')
          )
        """
    ).fetchall()
    return {str(row["target_id"] if isinstance(row, sqlite3.Row) else row[0]) for row in rows if str(row["target_id"] if isinstance(row, sqlite3.Row) else row[0])}


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
    scopes = [str(item) for item in (scope_ids or []) if str(item)]
    if not scopes:
        return "", []
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


def _snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "scope_id": str(row["scope_id"] or ""),
        "source": str(row["source"] or ""),
        "target": str(row["target"] or ""),
        "summary": str(row["summary"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "metadata": _json_loads(row["metadata"]),
    }


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
        before = _snapshot_row(row)
        metadata = dict(before["metadata"])
        metadata["lifecycle"] = "archived"
        metadata["forget_reason"] = item["reason"]
        metadata["archived_at"] = now
        metadata["archived_by"] = actor
        metadata["rollback_batch_id"] = batch
        metadata["cleanup_reason"] = reason
        conn.execute(
            "UPDATE memories SET metadata = ?, updated_at = ? WHERE id = ?",
            (_json_dumps(metadata), now, item["id"]),
        )
        after = dict(before)
        after["updated_at"] = now
        after["metadata"] = metadata
        record_governance_audit_event(
            conn,
            event_id=f"gov_{uuid.uuid4().hex}",
            event_type="memory_cleanup",
            action="soft_archive",
            scope_id=str(item["scope_id"]),
            target_id=str(item["id"]),
            batch_id=batch,
            before=before,
            after=after,
            reason=item["reason"],
            actor=actor,
            dry_run=False,
            created_at=now,
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

    Coverage gaps are operational debt because rollback and accountability depend on those receipts."""
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
    audited_ids = _audited_archive_ids(conn)
    archived_rows = [row for row in rows if _is_archived(row)]
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
    audited_ids = _audited_archive_ids(conn)
    candidates = [row for row in rows if _is_archived(row) and str(row["id"]) not in audited_ids and not _has_new_archive_marker(_json_loads(row["metadata"]))]
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
    for row in candidates:
        snapshot = _snapshot_row(row)
        record_governance_audit_event(
            conn,
            event_id=f"gov_{uuid.uuid4().hex}",
            event_type="memory_cleanup",
            action="legacy_archive_backfill",
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


def rollback_cleanup_batch(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    dry_run: bool = True,
    actor: str = "governance.cleanup.py",
    event_types: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Rollback a previously applied governance cleanup batch where audit evidence is sufficient.

    Rollback stays evidence-driven: rows without matching governance receipts should not be guessed back into active state."""
    types = [str(item) for item in (event_types or ("memory_cleanup", "forgetting", "scope_recall_forget")) if str(item)]
    if not types:
        types = ["memory_cleanup", "forgetting", "scope_recall_forget"]
    if not dry_run:
        ensure_schema(conn)
    elif not _governance_table_exists(conn):
        return {
            "dry_run": True,
            "batch_id": batch_id,
            "rollback_candidates": 0,
            "restored": 0,
            "restore_ids": [],
            "status": "schema_missing",
        }
    placeholders = ",".join("?" for _ in types)
    rows = conn.execute(
        f"""
        SELECT id, event_type, target_id, scope_id, before_json, after_json, reason
        FROM governance_audit_events
        WHERE batch_id = ? AND event_type IN ({placeholders}) AND action = 'soft_archive' AND dry_run = 0
        ORDER BY created_at ASC, id ASC
        """,
        (batch_id, *types),
    ).fetchall()
    result = {
        "dry_run": bool(dry_run),
        "batch_id": batch_id,
        "rollback_candidates": len(rows),
        "restored": 0,
        "restore_ids": [str(row["target_id"]) for row in rows],
    }
    if dry_run or not rows:
        return result
    now = _now_iso()
    restored = 0
    for audit in rows:
        target_id = str(audit["target_id"])
        before = _json_loads(audit["before_json"])
        after = _json_loads(audit["after_json"])
        before_metadata = before.get("metadata") if isinstance(before.get("metadata"), dict) else {}
        after_metadata = after.get("metadata") if isinstance(after.get("metadata"), dict) else {}
        current = conn.execute("SELECT id, scope_id, source, target, content, summary, updated_at, metadata FROM memories WHERE id = ?", (target_id,)).fetchone()
        if current is None:
            continue
        current_snapshot = _snapshot_row(current)
        raw_current_metadata = current_snapshot.get("metadata")
        current_metadata = raw_current_metadata if isinstance(raw_current_metadata, dict) else {}
        # Defensive rollback gate: only undo the exact archived state produced by
        # this batch. Removing these checks makes rollback non-idempotent and can
        # overwrite a later operator/archive decision with stale metadata.
        if str(current_metadata.get("lifecycle") or "").strip().lower() != "archived":
            continue
        current_batch = str(current_metadata.get("rollback_batch_id") or "")
        if current_batch and current_batch != batch_id:
            continue
        if not current_batch and after_metadata and current_metadata != after_metadata:
            continue
        conn.execute(
            "UPDATE memories SET metadata = ?, updated_at = ? WHERE id = ?",
            (_json_dumps(before_metadata), str(before.get("updated_at") or now), target_id),
        )
        sync_memory_entities(
            conn,
            memory_id=target_id,
            content=str(current["content"] or ""),
            target=str(current["target"] or ""),
            metadata=dict(before_metadata or {}),
        )
        record_governance_audit_event(
            conn,
            event_id=f"gov_{uuid.uuid4().hex}",
            event_type=str(audit["event_type"] or "memory_cleanup"),
            action="rollback_soft_archive",
            scope_id=str(audit["scope_id"] or current["scope_id"] or ""),
            target_id=target_id,
            batch_id=batch_id,
            before=current_snapshot,
            after={"id": target_id, "metadata": before_metadata, "updated_at": str(before.get("updated_at") or now)},
            reason=str(audit["reason"] or "rollback"),
            actor=actor,
            dry_run=False,
            created_at=now,
        )
        restored += 1
    conn.commit()
    result["restored"] = restored
    return result
