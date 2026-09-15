"""Evidence-bound rollback for governance archive batches.

This module is the sole rollback eligibility authority. Dry-run and apply use
the same classifier so a preview cannot promise a restoration that apply will
silently skip. Every rollback requires an explicit before lifecycle and an
after snapshot bound to all current truth fields, including content.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Sequence

from .lifecycle_service import transition_memory_lifecycle
from .lifecycle_registry import (
    default_rollback_event_actions,
    rollback_operation_for_event_type,
)
from .maintenance_ops import now_utc_iso
from .sql_store import ensure_schema

DEFAULT_ROLLBACK_EVENT_ACTIONS = dict(default_rollback_event_actions())

_BOUND_FIELDS = (
    "id",
    "scope_id",
    "source",
    "target",
    "content",
    "summary",
    "updated_at",
)


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if raw in (None, ""):
        return {}
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _metadata(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return {} if value in (None, "") else None


def snapshot_memory_row(row: sqlite3.Row) -> dict[str, Any]:
    """Return the complete truth fields required to bind an archive receipt."""

    return {
        "id": str(row["id"]),
        "scope_id": str(row["scope_id"] or ""),
        "source": str(row["source"] or ""),
        "target": str(row["target"] or ""),
        "content": str(row["content"] or ""),
        "summary": str(row["summary"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "metadata": _json_object(row["metadata"]),
    }


def receipt_binds_current_archive(
    after: dict[str, Any], current: dict[str, Any]
) -> bool:
    """Require an exact receipt binding to every mutable memory truth field."""

    for key in _BOUND_FIELDS:
        if key not in after or str(after.get(key) or "") != str(current.get(key) or ""):
            return False
    after_metadata = _metadata(after.get("metadata"))
    current_metadata = current.get("metadata")
    return (
        isinstance(after_metadata, dict)
        and isinstance(current_metadata, dict)
        and after_metadata == current_metadata
    )


def _before_snapshot(raw: Any) -> tuple[dict[str, Any] | None, dict[str, Any], str]:
    if raw in (None, ""):
        return None, {}, "missing_before_json"
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return None, {}, "malformed_before_json"
    if not isinstance(value, dict):
        return None, {}, "malformed_before_json"
    if "metadata" not in value:
        return None, value, "missing_before_metadata"
    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        return None, value, "malformed_before_metadata"
    lifecycle = str(metadata.get("lifecycle") or "").strip().lower()
    if not lifecycle:
        return None, value, "missing_before_lifecycle"
    if lifecycle == "archived":
        return None, value, "before_lifecycle_not_restorable"
    return dict(metadata), value, ""


@dataclass(frozen=True)
class _EligibleRollback:
    audit: sqlite3.Row
    current: sqlite3.Row
    before: dict[str, Any]
    before_metadata: dict[str, Any]


def _classify(
    conn: sqlite3.Connection,
    rows: Sequence[sqlite3.Row],
) -> tuple[list[_EligibleRollback], list[str], list[str]]:
    eligible: list[_EligibleRollback] = []
    invalid_ids: list[str] = []
    stale_ids: list[str] = []
    for audit in rows:
        target_id = str(audit["target_id"] or "")
        before_metadata, before, error = _before_snapshot(audit["before_json"])
        if error or before_metadata is None:
            invalid_ids.append(target_id)
            continue
        current = conn.execute(
            "SELECT id, scope_id, source, target, content, summary, updated_at, metadata "
            "FROM memories WHERE id = ?",
            (target_id,),
        ).fetchone()
        if current is None:
            stale_ids.append(target_id)
            continue
        current_snapshot = snapshot_memory_row(current)
        current_metadata = current_snapshot["metadata"]
        after = _json_object(audit["after_json"])
        if (
            str(current_metadata.get("lifecycle") or "").strip().lower()
            != "archived"
            or not receipt_binds_current_archive(after, current_snapshot)
        ):
            stale_ids.append(target_id)
            continue
        eligible.append(
            _EligibleRollback(
                audit=audit,
                current=current,
                before=before,
                before_metadata=before_metadata,
            )
        )
    return eligible, invalid_ids, stale_ids


def _status(*, total: int, eligible: int, applied: bool, restored: int = 0) -> str:
    if total == 0:
        return "empty"
    if applied:
        if restored == total:
            return "restored"
        if restored:
            return "partial"
        return "blocked"
    if eligible == total:
        return "ready"
    if eligible:
        return "partial"
    return "blocked"


def rollback_cleanup_batch(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    dry_run: bool = True,
    actor: str = "governance.cleanup.py",
    event_types: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Preview or restore one archive batch from exact governance evidence."""

    requested = list(
        dict.fromkeys(
            str(item)
            for item in (event_types or tuple(DEFAULT_ROLLBACK_EVENT_ACTIONS))
            if str(item)
        )
    )
    unsupported = [
        item for item in requested if item not in DEFAULT_ROLLBACK_EVENT_ACTIONS
    ]
    supported = [item for item in requested if item in DEFAULT_ROLLBACK_EVENT_ACTIONS]
    if not supported:
        return {
            "dry_run": bool(dry_run),
            "batch_id": batch_id,
            "rollback_candidates": 0,
            "restored": 0,
            "restore_ids": [],
            "invalid_ids": [],
            "stale_ids": [],
            "unsupported_event_types": unsupported,
            "status": "unsupported_event_type",
        }

    owns_transaction = False
    savepoint = "governance_rollback_batch"
    if not dry_run:
        caller_owns_transaction = conn.in_transaction
        ensure_schema(conn, commit=not caller_owns_transaction)
        if caller_owns_transaction:
            conn.execute(f"SAVEPOINT {savepoint}")
        else:
            conn.execute("BEGIN IMMEDIATE")
            owns_transaction = True
    else:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='governance_audit_events'"
        ).fetchone()
        if table is None:
            return {
                "dry_run": True,
                "batch_id": batch_id,
                "rollback_candidates": 0,
                "restored": 0,
                "restore_ids": [],
                "invalid_ids": [],
                "stale_ids": [],
                "unsupported_event_types": unsupported,
                "status": "schema_missing",
            }

    pairs = [(item, DEFAULT_ROLLBACK_EVENT_ACTIONS[item]) for item in supported]
    pair_clause = " OR ".join("(event_type = ? AND action = ?)" for _ in pairs)
    pair_params = [value for pair in pairs for value in pair]
    try:
        rows = conn.execute(
            f"""
            SELECT rowid AS audit_rowid, id, event_type, target_id, scope_id,
                   before_json, after_json, reason
            FROM governance_audit_events
            WHERE batch_id = ? AND dry_run = 0 AND ({pair_clause})
            ORDER BY rowid ASC
            """,
            (batch_id, *pair_params),
        ).fetchall()
        # A batch may contain retries for one target. Only its latest inserted
        # receipt is authoritative; applying older duplicates would conflict after
        # the first restore and roll back an otherwise valid transaction. Keep
        # malformed empty-target rows distinct so classification remains fail-closed.
        latest_by_target: dict[str, sqlite3.Row] = {}
        for row in rows:
            target_id = str(row["target_id"] or "")
            key = target_id if target_id else f"__rowid__:{row['audit_rowid']}"
            latest_by_target[key] = row
        rows = sorted(
            latest_by_target.values(), key=lambda row: int(row["audit_rowid"])
        )
        eligible, invalid_ids, stale_ids = _classify(conn, rows)
        result: dict[str, Any] = {
            "dry_run": bool(dry_run),
            "batch_id": batch_id,
            "rollback_candidates": len(rows),
            "restored": 0,
            "restore_ids": [str(item.audit["target_id"]) for item in eligible],
            "skipped_invalid": len(invalid_ids),
            "invalid_ids": invalid_ids,
            "skipped_stale": len(stale_ids),
            "stale_ids": stale_ids,
            "unsupported_event_types": unsupported,
            "restored_relation_count": 0,
            "skipped_relation_count": 0,
            "skipped_relations": [],
            "status": _status(total=len(rows), eligible=len(eligible), applied=False),
        }
        if dry_run:
            return result

        now = now_utc_iso()
        for item in eligible:
            audit = item.audit
            restore_relations = (
                item.before.get("relations")
                if isinstance(item.before.get("relations"), list)
                else []
            )
            transition_result = transition_memory_lifecycle(
                conn,
                memory_id=str(audit["target_id"]),
                lifecycle=str(item.before_metadata["lifecycle"]).strip().lower(),
                metadata_updates=item.before_metadata,
                replace_metadata=True,
                restore_relations=restore_relations,
                expected_updated_at=str(item.current["updated_at"] or ""),
                expected_lifecycle="archived",
                actor=actor,
                reason=str(audit["reason"] or "rollback"),
                operation_id=rollback_operation_for_event_type(
                    str(audit["event_type"] or "memory_cleanup")
                ).operation_id,
                batch_id=batch_id,
                timestamp=now,
            )
            if not bool(transition_result.get("applied")):
                target_id = str(audit["target_id"])
                result["restore_ids"] = [
                    memory_id
                    for memory_id in result["restore_ids"]
                    if memory_id != target_id
                ]
                result["stale_ids"].append(target_id)
                result["skipped_stale"] = len(result["stale_ids"])
                continue
            relation_receipt = transition_result.get("relation_restore") or {}
            result["restored_relation_count"] += int(
                relation_receipt.get("restored") or 0
            )
            skipped = relation_receipt.get("skipped") or []
            result["skipped_relation_count"] += len(skipped)
            result["skipped_relations"].extend(skipped)
            result["restored"] += 1

        result["status"] = _status(
            total=len(rows),
            eligible=len(eligible),
            applied=True,
            restored=int(result["restored"]),
        )
        if owns_transaction:
            conn.commit()
        else:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return result
    except Exception:
        if owns_transaction and conn.in_transaction:
            conn.rollback()
        elif not dry_run and conn.in_transaction:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


__all__ = [
    "DEFAULT_ROLLBACK_EVENT_ACTIONS",
    "receipt_binds_current_archive",
    "rollback_cleanup_batch",
    "snapshot_memory_row",
]
