"""Read-only DurableWork projections for the causal vector outbox.

The vector outbox remains the physical persistence and execution authority.  This
module does not create tables, move payloads, claim events, or write receipts; it
only exposes existing causal events through the shared Program 2 contract.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from .durable_work import (
    DURABLE_WORK_ITEM_STATES,
    DurableWorkDescriptor,
    DurableWorkItem,
    canonical_snapshot_hash,
    durable_work_health,
)


VECTOR_OUTBOX_POLICY_VERSION = "vector-causal-outbox.v1"
VECTOR_OUTBOX_DOMAIN_TYPE = "vector_causal_outbox"
VECTOR_OUTBOX_DEFAULT_MAX_ATTEMPTS = 8

_NATIVE_TO_DURABLE_STATE = {
    "pending": "pending",
    "processing": "processing",
    "retry": "retry",
    "completed": "completed",
    "dead_letter": "poisoned",
}


def _normalized_iso(value: Any, *, fallback: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _event_row(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row | None:
    if not _table_exists(conn, "vector_outbox"):
        return None
    previous_factory = conn.row_factory
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT id, event_key, generation_id, memory_id, operation, status,
                   attempts, available_at, created_at, updated_at, completed_at
            FROM vector_outbox
            WHERE id=?
            """,
            (int(event_id),),
        ).fetchone()
    finally:
        conn.row_factory = previous_factory


def vector_outbox_event_descriptor(
    conn: sqlite3.Connection, event_id: int
) -> DurableWorkDescriptor | None:
    """Project one existing causal event without reading its payload or error."""

    row = _event_row(conn, event_id)
    if row is None:
        return None
    event_identity = {
        "event_id": int(row["id"]),
        "event_key": str(row["event_key"]),
        "generation_id": str(row["generation_id"]),
        "memory_id": str(row["memory_id"]),
        "operation": str(row["operation"]),
    }
    return DurableWorkDescriptor(
        work_id=f"vector-outbox:{int(row['id'])}",
        domain_type=VECTOR_OUTBOX_DOMAIN_TYPE,
        idempotency_key=str(row["event_key"]),
        scope_snapshot={
            "generation_id": str(row["generation_id"]),
            "memory_id": str(row["memory_id"]),
        },
        authority_snapshot={
            "truth_authority": "sqlite",
            "companion_authority": "vector_generation",
            "operation": str(row["operation"]),
        },
        policy_version=VECTOR_OUTBOX_POLICY_VERSION,
        generation=int(row["id"]),
        frozen_upper_bound=1,
        item_set_hash=canonical_snapshot_hash(event_identity),
        created_at=_normalized_iso(
            row["created_at"], fallback="1970-01-01T00:00:00+00:00"
        ),
    )


def vector_outbox_event_item(
    conn: sqlite3.Connection,
    event_id: int,
    *,
    max_attempts: int = VECTOR_OUTBOX_DEFAULT_MAX_ATTEMPTS,
) -> DurableWorkItem | None:
    """Project one native event state without exposing payload or error text."""

    row = _event_row(conn, event_id)
    if row is None:
        return None
    native_state = str(row["status"] or "").strip().lower()
    state = _NATIVE_TO_DURABLE_STATE.get(native_state)
    if state is None:
        raise ValueError(f"unsupported vector outbox state: {native_state or '<empty>'}")
    attempt = max(0, int(row["attempts"] or 0))
    error_class = ""
    error_code = ""
    if native_state == "retry":
        error_class = "retriable"
        error_code = "vector_outbox_retry"
    elif native_state == "dead_letter":
        error_class = "poison"
        error_code = "vector_outbox_dead_letter"
    return DurableWorkItem(
        item_identity=f"vector-outbox:{int(row['id'])}",
        state=state,
        attempt=attempt,
        max_attempts=max(1, int(max_attempts), attempt),
        not_before=(
            _normalized_iso(row["available_at"])
            if native_state in {"pending", "retry"}
            else ""
        ),
        last_error_class=error_class,
        last_error_code=error_code,
        last_progress_at=_normalized_iso(row["updated_at"]),
        receipt={
            "event_id": int(row["id"]),
            "generation_id": str(row["generation_id"]),
            "operation": str(row["operation"]),
        },
    )


def vector_outbox_durable_health(
    conn: sqlite3.Connection,
    generation_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return content-free shared health for only the selected generation."""

    selected_generation = str(generation_id or "").strip()
    if not selected_generation or not _table_exists(conn, "vector_outbox"):
        reason = "no_active_generation" if not selected_generation else "schema_missing"
        return disabled_vector_outbox_durable_health(
            reason_code=reason,
            generation_id=selected_generation,
        )

    native_counts = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            """
            SELECT status, COUNT(*)
            FROM vector_outbox
            WHERE generation_id=?
            GROUP BY status
            """,
            (selected_generation,),
        ).fetchall()
    }
    item_counts = {state: 0 for state in DURABLE_WORK_ITEM_STATES}
    unknown_count = 0
    for native_state, count in native_counts.items():
        mapped = _NATIVE_TO_DURABLE_STATE.get(native_state)
        if mapped is None:
            unknown_count += count
        else:
            item_counts[mapped] += count

    oldest_row = conn.execute(
        """
        SELECT MIN(created_at), MAX(updated_at), MIN(id)
        FROM vector_outbox
        WHERE generation_id=? AND status IN ('pending', 'processing', 'retry')
        """,
        (selected_generation,),
    ).fetchone()
    oldest_iso = _normalized_iso(oldest_row[0] if oldest_row else "")
    last_progress_at = _normalized_iso(oldest_row[1] if oldest_row else "")
    next_event_id = int(oldest_row[2]) if oldest_row and oldest_row[2] is not None else 0
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    oldest_age_seconds = 0.0
    if oldest_iso:
        oldest = datetime.fromisoformat(oldest_iso)
        oldest_age_seconds = max(0.0, (current - oldest).total_seconds())

    poisoned = item_counts["poisoned"]
    runnable = sum(item_counts[state] for state in ("pending", "processing", "retry"))
    if unknown_count:
        state = "needs_repair"
        reason_code = "unsupported_native_state"
        auto_recoverable = False
        operator_action_required = True
    elif poisoned:
        state = "needs_repair"
        reason_code = "dead_letter_present"
        auto_recoverable = False
        operator_action_required = True
    elif runnable:
        state = "degraded"
        reason_code = "replay_debt_present"
        auto_recoverable = True
        operator_action_required = False
    else:
        state = "ready"
        reason_code = "healthy"
        auto_recoverable = True
        operator_action_required = False

    report = durable_work_health(
        domain_type=VECTOR_OUTBOX_DOMAIN_TYPE,
        state=state,
        reason_code=reason_code,
        item_counts=item_counts,
        oldest_age_seconds=oldest_age_seconds,
        last_progress_at=last_progress_at,
        progress_rate=0.0,
        lease_expirations=0,
        lock_contention=0,
        auto_recoverable=auto_recoverable,
        operator_action_required=operator_action_required,
        fairness={
            "strategy": "causal_event_id_ascending",
            "next_event_id": next_event_id,
            "scope": "current_generation_only",
            "executor": "bounded_vector_outbox_replay",
        },
    )
    report.update(
        {
            "policy_version": VECTOR_OUTBOX_POLICY_VERSION,
            "current_generation_id": selected_generation,
            "native_status_counts": native_counts,
            "unknown_native_state_count": unknown_count,
        }
    )
    return report


def disabled_vector_outbox_durable_health(
    *,
    reason_code: str,
    generation_id: str = "",
    state: str = "disabled",
    operator_action_required: bool = False,
) -> dict[str, Any]:
    """Return the stable shared envelope when no outbox can be observed."""

    report = durable_work_health(
        domain_type=VECTOR_OUTBOX_DOMAIN_TYPE,
        state=state,
        reason_code=reason_code,
        auto_recoverable=False,
        operator_action_required=operator_action_required,
        fairness={"strategy": "causal_event_id_ascending"},
    )
    report.update(
        {
            "policy_version": VECTOR_OUTBOX_POLICY_VERSION,
            "current_generation_id": str(generation_id or ""),
            "native_status_counts": {},
        }
    )
    return report


__all__ = [
    "VECTOR_OUTBOX_DEFAULT_MAX_ATTEMPTS",
    "VECTOR_OUTBOX_DOMAIN_TYPE",
    "VECTOR_OUTBOX_POLICY_VERSION",
    "disabled_vector_outbox_durable_health",
    "vector_outbox_durable_health",
    "vector_outbox_event_descriptor",
    "vector_outbox_event_item",
]
