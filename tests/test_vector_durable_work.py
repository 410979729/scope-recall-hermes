from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from scope_recall.vector_durable_work import (
    disabled_vector_outbox_durable_health,
    vector_outbox_durable_health,
    vector_outbox_event_descriptor,
    vector_outbox_event_item,
)
from scope_recall.vector_generation import (
    enqueue_vector_event,
    ensure_vector_generation_schema,
)


NOW = "2026-08-27T12:00:00+00:00"


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_vector_generation_schema(conn)
    conn.commit()
    return conn


def _enqueue(
    conn: sqlite3.Connection,
    *,
    event_key: str,
    generation_id: str = "active-v1",
    memory_id: str = "memory-1",
) -> int:
    row = enqueue_vector_event(
        conn,
        event_key=event_key,
        generation_id=generation_id,
        memory_id=memory_id,
        operation="upsert",
        payload={"text": "TOP-SECRET-PAYLOAD"},
        timestamp=NOW,
    )
    conn.commit()
    return int(row["id"])


def test_vector_event_projection_is_finite_and_content_free():
    conn = _connection()
    event_id = _enqueue(conn, event_key="event-1")
    conn.execute(
        "UPDATE vector_outbox SET status='retry', attempts=2, "
        "last_error='TOP-SECRET-ERROR' WHERE id=?",
        (event_id,),
    )
    conn.commit()

    descriptor = vector_outbox_event_descriptor(conn, event_id)
    item = vector_outbox_event_item(conn, event_id)

    assert descriptor is not None
    assert descriptor.frozen_upper_bound == 1
    assert descriptor.idempotency_key == "event-1"
    assert item is not None
    assert item.state == "retry"
    assert item.last_error_class == "retriable"
    assert item.last_error_code == "vector_outbox_retry"
    projected = json.dumps(
        {"descriptor": descriptor.as_dict(), "item": item.as_dict()}
    )
    assert "TOP-SECRET-PAYLOAD" not in projected
    assert "TOP-SECRET-ERROR" not in projected
    conn.close()


def test_vector_dead_letter_projects_as_terminal_poison():
    conn = _connection()
    event_id = _enqueue(conn, event_key="event-poison")
    conn.execute(
        "UPDATE vector_outbox SET status='dead_letter', attempts=8 WHERE id=?",
        (event_id,),
    )
    conn.commit()

    item = vector_outbox_event_item(conn, event_id)

    assert item is not None
    assert item.state == "poisoned"
    assert item.terminal is True
    assert item.last_error_class == "poison"
    conn.close()


def test_vector_health_is_scoped_to_current_generation():
    conn = _connection()
    current_event = _enqueue(conn, event_key="current")
    inactive_event = _enqueue(
        conn,
        event_key="inactive",
        generation_id="inactive-v0",
        memory_id="memory-2",
    )
    conn.execute(
        "UPDATE vector_outbox SET status='completed', completed_at=? WHERE id=?",
        (NOW, current_event),
    )
    conn.execute(
        "UPDATE vector_outbox SET status='dead_letter', attempts=8 WHERE id=?",
        (inactive_event,),
    )
    conn.commit()

    active = vector_outbox_durable_health(
        conn,
        "active-v1",
        now=datetime(2026, 8, 27, 12, 1, tzinfo=timezone.utc),
    )
    inactive = vector_outbox_durable_health(conn, "inactive-v0")

    assert active["state"] == "ready"
    assert active["item_counts"]["completed"] == 1
    assert active["item_counts"]["poisoned"] == 0
    assert inactive["state"] == "needs_repair"
    assert inactive["item_counts"]["poisoned"] == 1
    conn.close()


def test_vector_health_reports_replay_debt_without_payload():
    conn = _connection()
    _enqueue(conn, event_key="pending")

    health = vector_outbox_durable_health(
        conn,
        "active-v1",
        now=datetime(2026, 8, 27, 12, 1, tzinfo=timezone.utc),
    )

    assert health["state"] == "degraded"
    assert health["reason_code"] == "replay_debt_present"
    assert health["runnable_count"] == 1
    assert health["oldest_age_seconds"] == 60.0
    assert health["fairness"]["scope"] == "current_generation_only"
    assert "TOP-SECRET-PAYLOAD" not in json.dumps(health)
    conn.close()


def test_vector_projection_is_read_only():
    conn = _connection()
    event_id = _enqueue(conn, event_key="read-only")
    before = conn.total_changes
    conn.execute("PRAGMA query_only=ON")

    assert vector_outbox_event_descriptor(conn, event_id) is not None
    assert vector_outbox_event_item(conn, event_id) is not None
    assert vector_outbox_durable_health(conn, "active-v1")["runnable_count"] == 1
    assert conn.total_changes == before
    conn.close()


def test_vector_enqueue_remains_owned_by_caller_transaction():
    conn = _connection()
    conn.execute("BEGIN")
    enqueue_vector_event(
        conn,
        event_key="rolled-back",
        generation_id="active-v1",
        memory_id="memory-rollback",
        operation="delete",
        timestamp=NOW,
    )
    conn.rollback()

    assert (
        conn.execute(
            "SELECT COUNT(*) FROM vector_outbox WHERE event_key='rolled-back'"
        ).fetchone()[0]
        == 0
    )
    conn.close()


def test_disabled_vector_health_keeps_the_shared_schema_stable():
    health = disabled_vector_outbox_durable_health(
        reason_code="truth_database_absent"
    )

    assert health["state"] == "disabled"
    assert health["reason_code"] == "truth_database_absent"
    assert health["runnable_count"] == 0
    assert health["native_status_counts"] == {}


def test_unobservable_registered_vector_state_can_fail_closed_for_repair():
    health = disabled_vector_outbox_durable_health(
        reason_code="current_generation_manifest_missing",
        generation_id="missing-generation",
        state="needs_repair",
        operator_action_required=True,
    )

    assert health["state"] == "needs_repair"
    assert health["operator_action_required"] is True
    assert health["auto_recoverable"] is False
