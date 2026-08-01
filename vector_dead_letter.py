"""Audited recovery for terminal vector-outbox dead-letter events.

Normal replay remains fail-closed. This module is the explicit operator path that
inspects bounded debt, resets only selected terminal events, and records the
mutation in the authoritative operator ledger in the same SQLite transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Sequence

from .capture_filters import sanitize_report_text
from .operator_ledger import record_committed_operator_operation

_OPERATION_KIND = "vector_outbox.requeue_dead_letter"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_dicts(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    names = [str(column[0]) for column in cursor.description or []]
    return [dict(zip(names, tuple(row), strict=True)) for row in cursor.fetchall()]


def _clean_event_ids(event_ids: Sequence[int] | None, *, limit: int) -> list[int]:
    cleaned = sorted({int(event_id) for event_id in (event_ids or []) if int(event_id) > 0})
    if len(cleaned) > limit:
        raise ValueError(f"event_ids exceed bounded limit {limit}")
    return cleaned


def _select_dead_letter_rows(
    conn: sqlite3.Connection,
    *,
    event_ids: Sequence[int] | None = None,
    generation_id: str = "",
    limit: int = 100,
) -> tuple[list[dict[str, Any]], list[int], str]:
    bounded_limit = max(1, min(int(limit or 100), 500))
    ids = _clean_event_ids(event_ids, limit=bounded_limit)
    generation = str(generation_id or "").strip()
    clauses = ["status = 'dead_letter'"]
    params: list[Any] = []
    if ids:
        clauses.append(f"id IN ({','.join('?' for _ in ids)})")
        params.extend(ids)
    if generation:
        clauses.append("generation_id = ?")
        params.append(generation)
    cursor = conn.execute(
        "SELECT id, generation_id, memory_id, operation, attempts, last_error, "
        "updated_at, completed_at FROM vector_outbox WHERE "
        + " AND ".join(clauses)
        + " ORDER BY id ASC LIMIT ?",
        (*params, bounded_limit),
    )
    return _row_dicts(cursor), ids, generation


def _public_event(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "generation_id": str(row["generation_id"]),
        "memory_id": str(row["memory_id"]),
        "operation": str(row["operation"]),
        "attempts": int(row["attempts"] or 0),
        "updated_at": str(row["updated_at"] or ""),
    }


def dead_letter_vector_events_report(
    conn: sqlite3.Connection,
    *,
    generation_id: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """Return bounded dead-letter metadata without echoing stored error text."""

    rows, _ids, generation = _select_dead_letter_rows(
        conn,
        generation_id=generation_id,
        limit=limit,
    )
    return {
        "status": "needs_repair" if rows else "ready",
        "generation_id": generation,
        "dead_letter": len(rows),
        "events": [_public_event(row) for row in rows],
    }


def _request_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _idempotent_result(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    request_fingerprint: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT operation_kind, request_fingerprint, result_json "
        "FROM operator_operations WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    if row is None:
        return None
    if str(row[0]) != _OPERATION_KIND or str(row[1]) != request_fingerprint:
        raise ValueError("operation_id already belongs to a different requeue request")
    try:
        result = json.loads(str(row[2]))
    except json.JSONDecodeError as exc:
        raise ValueError("stored vector requeue receipt is corrupt") from exc
    if not isinstance(result, dict):
        raise ValueError("stored vector requeue receipt has an invalid result")
    return {**result, "idempotent_replay": True}


def requeue_dead_letter_vector_events(
    conn: sqlite3.Connection,
    *,
    event_ids: Sequence[int] | None = None,
    generation_id: str = "",
    apply: bool = False,
    operation_id: str = "",
    reason: str = "",
    backup_path: str = "",
    limit: int = 100,
    timestamp: str = "",
) -> dict[str, Any]:
    """Plan or atomically requeue selected terminal vector events.

    Apply owns the transaction and therefore requires an idle connection. An
    operation id is idempotent only for the exact same selected ids, generation,
    and reason. Backup evidence is recorded on the first committed operation.
    """

    bounded_limit = max(1, min(int(limit or 100), 500))
    requested_ids = _clean_event_ids(event_ids, limit=bounded_limit)
    generation = str(generation_id or "").strip()
    if not requested_ids and not generation:
        raise ValueError("event_ids or generation_id is required for dead-letter requeue")
    clean_reason = sanitize_report_text(str(reason or "").strip())[:500]
    clean_backup_path = sanitize_report_text(str(backup_path or "").strip())[:4000]
    request = {
        "event_ids": requested_ids,
        "generation_id": generation,
        "reason": clean_reason,
    }
    fingerprint = _request_fingerprint(request)
    clean_operation_id = str(operation_id or "").strip()
    if apply and clean_operation_id:
        replay = _idempotent_result(
            conn,
            operation_id=clean_operation_id,
            request_fingerprint=fingerprint,
        )
        if replay is not None:
            return replay

    rows, selected_ids, generation = _select_dead_letter_rows(
        conn,
        event_ids=requested_ids,
        generation_id=generation,
        limit=bounded_limit,
    )
    if selected_ids and len(rows) != len(selected_ids):
        raise ValueError(
            "every requested event id must identify a dead-letter event in the selected generation"
        )
    planned_ids = [int(row["id"]) for row in rows]
    plan = {
        "apply": bool(apply),
        "planned": len(rows),
        "ids": planned_ids,
        "generation_id": generation,
        "events": [_public_event(row) for row in rows],
    }
    if not apply:
        return plan
    if not rows:
        raise ValueError("no dead-letter vector events matched the requeue request")
    if len(clean_reason) < 8:
        raise ValueError("a specific requeue reason of at least 8 characters is required")
    if not clean_operation_id:
        raise ValueError("operation_id is required for audited dead-letter requeue")
    if not clean_backup_path:
        raise ValueError("backup_path is required for audited dead-letter requeue")
    if conn.in_transaction:
        raise RuntimeError("dead-letter requeue requires an idle SQLite connection")

    at = timestamp or _now_iso()
    placeholders = ",".join("?" for _ in planned_ids)
    before = {
        "reason": clean_reason,
        "events": [
            {
                **_public_event(row),
                "completed_at": str(row["completed_at"] or ""),
                "last_error": sanitize_report_text(str(row["last_error"] or ""))[:500],
            }
            for row in rows
        ],
    }
    result = {
        "apply": True,
        "planned": len(rows),
        "requeued": len(rows),
        "ids": planned_ids,
        "generation_id": generation,
        "operation_id": clean_operation_id,
    }
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE vector_outbox SET status = 'pending', attempts = 0, worker_id = '', "
            "last_error = '', available_at = ?, updated_at = ?, completed_at = '' "
            f"WHERE status = 'dead_letter' AND id IN ({placeholders})",
            (at, at, *planned_ids),
        )
        if cursor.rowcount != len(planned_ids):
            raise RuntimeError("dead-letter requeue CAS conflict")
        record_committed_operator_operation(
            conn,
            operation_id=clean_operation_id,
            operation_kind=_OPERATION_KIND,
            target_ref=generation or ",".join(str(event_id) for event_id in planned_ids),
            before=before,
            result=result,
            backup_path=clean_backup_path,
            request_fingerprint=fingerprint,
            commit=False,
        )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    return {**result, "idempotent_replay": False}


__all__ = [
    "dead_letter_vector_events_report",
    "requeue_dead_letter_vector_events",
]
