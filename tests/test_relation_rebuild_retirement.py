"""Fail-closed contract for the retired full-scope rebuild queue."""

from __future__ import annotations

import sqlite3

import pytest

from scope_recall.relation_containment import relation_containment_report
from scope_recall.relation_rebuild_queue import (
    claim_relation_rebuild_events,
    drain_relation_rebuild_queue,
    enqueue_relation_rebuild,
    relation_rebuild_debt_exists,
    relation_rebuild_queue_report,
    resolve_relation_rebuild,
    seed_scope_relation_rebuilds,
)
from scope_recall.sql_store import ensure_schema


def _legacy_debt_fixture() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    conn.execute(
        """
        INSERT INTO relation_rebuild_queue(
            scope_id, focus_memory_id, requested_updated_at, reason,
            status, available_at, created_at, updated_at
        ) VALUES(
            'scope-a', 'focus', 'revision-1', 'legacy fixture',
            'pending', '2026-08-26T00:00:00+00:00',
            '2026-08-26T00:00:00+00:00', '2026-08-26T00:00:00+00:00'
        )
        """
    )
    conn.commit()
    return conn


def _queue_snapshot(conn: sqlite3.Connection) -> tuple[object, ...]:
    row = conn.execute(
        """
        SELECT id, scope_id, focus_memory_id, requested_updated_at, reason,
               status, available_at, created_at, updated_at
        FROM relation_rebuild_queue
        """
    ).fetchone()
    assert row is not None
    return tuple(row)


def test_every_legacy_execution_surface_is_a_zero_write_refusal() -> None:
    conn = _legacy_debt_fixture()
    before = _queue_snapshot(conn)
    before_changes = conn.total_changes

    with pytest.raises(RuntimeError, match="legacy relation rebuild enqueue is disabled"):
        enqueue_relation_rebuild(
            conn,
            scope_id="scope-a",
            focus_memory_id="focus",
            requested_updated_at="revision-2",
            reason="must stay retired",
            commit=True,
            force=True,
        )
    assert resolve_relation_rebuild(
        conn,
        scope_id="scope-a",
        focus_memory_id="focus",
        requested_updated_at="revision-1",
        commit=True,
    ) == 0
    assert claim_relation_rebuild_events(
        conn,
        worker_id="retired-worker",
        commit=True,
    ) == []
    drained = drain_relation_rebuild_queue(conn)
    seeded = seed_scope_relation_rebuilds(conn, scope_ids=["scope-a"], commit=True)

    assert drained["disabled"] is True
    assert drained["reason_code"] == "legacy_unbounded_drain_disabled"
    assert seeded["disabled"] is True
    assert seeded["reason_code"] == "legacy_unbounded_seed_disabled"
    assert conn.total_changes == before_changes
    assert _queue_snapshot(conn) == before


def test_legacy_debt_remains_visible_for_backup_first_operator_cleanup() -> None:
    conn = _legacy_debt_fixture()

    report = relation_rebuild_queue_report(conn)
    containment = relation_containment_report(conn)

    assert relation_rebuild_debt_exists(conn) is True
    assert report["status"] == "debt"
    assert report["unresolved"] == 1
    assert report["pending"] == 1
    assert report["samples"][0]["scope_id"] == "scope-a"
    assert containment["status"] == "blocked"
    assert containment["scope_count"] == 1
    assert containment["scopes"][0]["scope_id"] == "scope-a"
    assert containment["scopes"][0]["reason_code"] == "legacy_unbounded_work_present"
    assert containment["scopes"][0]["pending"] == 1
    assert containment["scopes"][0]["operator_action_required"] is True
