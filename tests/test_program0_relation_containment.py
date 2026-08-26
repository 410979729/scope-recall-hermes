"""Program 0 relation containment invariants and regression counterexamples."""

from __future__ import annotations

import json
import sqlite3

import scope_recall.relation_frequency_maintenance as maintenance
from scope_recall.relation_containment import generated_relation_scope_policy
from scope_recall.relation_frequency_index import (
    relation_frequency_snapshot,
    sync_relation_frequency_memory,
)
from scope_recall.sql_store import ensure_schema


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _insert_direct(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    scope_id: str,
    entity: str,
    updated_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES(?, ?, 'fixture', 'project', ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            scope_id,
            f"{entity} service owns this durable fact",
            f"{entity} fact",
            updated_at,
            updated_at,
            json.dumps({"entities": [entity]}),
        ),
    )


def _drain(conn: sqlite3.Connection) -> dict[str, object]:
    return maintenance.drain_relation_frequency_work(
        conn,
        change_limit=100,
        focus_limit=100,
        backfill_limit=100,
        relation_candidate_cap=100,
        wall_clock_seconds=5.0,
        commit=True,
    )


def test_frequency_work_generation_never_reuses_after_change_row_is_consumed() -> None:
    conn = _conn()
    _insert_direct(
        conn,
        memory_id="focus",
        scope_id="scope-a",
        entity="Alpha Service",
        updated_at="2026-08-26T00:00:00+00:00",
    )
    first = conn.execute(
        "SELECT work_generation FROM relation_frequency_changes WHERE memory_id='focus'"
    ).fetchone()
    assert int(first[0]) == 1
    sync_relation_frequency_memory(conn, "focus")
    assert conn.execute(
        "SELECT 1 FROM relation_frequency_changes WHERE memory_id='focus'"
    ).fetchone() is None

    conn.execute(
        "UPDATE memories SET updated_at='2026-08-26T00:00:01+00:00' WHERE id='focus'"
    )
    second = conn.execute(
        "SELECT work_generation FROM relation_frequency_changes WHERE memory_id='focus'"
    ).fetchone()
    assert int(second[0]) == 2
    sync_relation_frequency_memory(conn, "focus")

    conn.execute(
        "UPDATE memories SET updated_at='2026-08-26T00:00:02+00:00' WHERE id='focus'"
    )
    third = conn.execute(
        "SELECT work_generation FROM relation_frequency_changes WHERE memory_id='focus'"
    ).fetchone()
    assert int(third[0]) == 3
    queued = conn.execute(
        "SELECT work_generation FROM relation_focus_work WHERE memory_id='focus'"
    ).fetchone()
    assert int(queued[0]) == 2


def test_frequency_snapshot_is_zero_write_even_on_writable_connection() -> None:
    conn = _conn()
    _insert_direct(
        conn,
        memory_id="dirty",
        scope_id="scope-a",
        entity="Alpha Service",
        updated_at="2026-08-26T00:00:00+00:00",
    )
    before_changes = conn.total_changes
    before_dirty = conn.execute(
        "SELECT COUNT(*) FROM relation_frequency_changes"
    ).fetchone()[0]

    assert relation_frequency_snapshot(conn, "scope-a") is None

    assert conn.total_changes == before_changes
    assert conn.execute(
        "SELECT COUNT(*) FROM relation_frequency_changes"
    ).fetchone()[0] == before_dirty


def test_background_drain_consumes_every_focus_before_scope_is_ready() -> None:
    conn = _conn()
    _insert_direct(
        conn,
        memory_id="first",
        scope_id="scope-a",
        entity="Alpha Service",
        updated_at="2026-08-26T00:00:00+00:00",
    )
    _insert_direct(
        conn,
        memory_id="second",
        scope_id="scope-a",
        entity="Alpha Service",
        updated_at="2026-08-26T00:00:01+00:00",
    )
    result = _drain(conn)

    assert int(result["changed_memories"]) == 2
    assert int(result["focused_memories"]) == 2
    assert conn.execute("SELECT COUNT(*) FROM relation_focus_work").fetchone()[0] == 0
    policy = generated_relation_scope_policy(conn, ["scope-a"])["scope-a"]
    assert policy["state"] == "ready"
    assert policy["generated_signal_enabled"] is True


def test_scope_move_deletes_generated_cross_scope_edge_before_ready() -> None:
    conn = _conn()
    _insert_direct(
        conn,
        memory_id="focus",
        scope_id="scope-a",
        entity="Alpha Service",
        updated_at="2026-08-26T00:00:00+00:00",
    )
    _insert_direct(
        conn,
        memory_id="peer",
        scope_id="scope-a",
        entity="Alpha Service",
        updated_at="2026-08-26T00:00:01+00:00",
    )
    _drain(conn)
    assert conn.execute(
        """
        SELECT 1 FROM memory_relations
        WHERE (source_memory_id='focus' OR target_memory_id='focus')
          AND LOWER(COALESCE(note, '')) LIKE 'relation-extraction:%'
        """
    ).fetchone() is not None
    conn.execute(
        """
        UPDATE memories SET scope_id='scope-b',
            updated_at='2026-08-26T00:00:03+00:00'
        WHERE id='focus'
        """
    )

    _drain(conn)

    assert conn.execute(
        """
        SELECT 1 FROM memory_relations
        WHERE (source_memory_id='focus' OR target_memory_id='focus')
          AND LOWER(COALESCE(note, '')) LIKE 'relation-extraction:%'
        """
    ).fetchone() is None
    assert conn.execute("SELECT COUNT(*) FROM relation_focus_work").fetchone()[0] == 0
    policies = generated_relation_scope_policy(conn, ["scope-a", "scope-b"])
    assert all(item["state"] == "ready" for item in policies.values())


def test_new_generation_supersedes_terminal_frequency_failure() -> None:
    conn = _conn()
    _insert_direct(
        conn,
        memory_id="focus",
        scope_id="scope-a",
        entity="Alpha Service",
        updated_at="2026-08-26T00:00:00+00:00",
    )
    real_sync = maintenance.sync_relation_frequency_memory

    def fail_sync(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise ValueError("injected")

    maintenance.sync_relation_frequency_memory = fail_sync
    try:
        assert maintenance._drain_change_rows(conn, 10) == 0
        assert maintenance._drain_change_rows(conn, 10) == 0
        assert maintenance._drain_change_rows(conn, 10) == 0
    finally:
        maintenance.sync_relation_frequency_memory = real_sync
    failed = conn.execute(
        """
        SELECT work_generation, status FROM relation_frequency_failures
        WHERE memory_id='focus'
        """
    ).fetchone()
    assert tuple(failed) == (1, "dead_letter")

    conn.execute(
        "UPDATE memories SET updated_at='2026-08-26T00:00:01+00:00' WHERE id='focus'"
    )
    assert maintenance._drain_change_rows(conn, 10) == 1
    assert conn.execute(
        "SELECT 1 FROM relation_frequency_failures WHERE memory_id='focus'"
    ).fetchone() is None
    disposition = conn.execute(
        """
        SELECT terminal_state FROM relation_work_dispositions
        WHERE work_kind='frequency_change' AND work_key='focus' AND work_revision='1'
        """
    ).fetchone()
    assert str(disposition[0]) == "superseded"


def test_frequency_poison_terminally_blocks_dependent_focus_work() -> None:
    conn = _conn()
    _insert_direct(
        conn,
        memory_id="poison",
        scope_id="scope-a",
        entity="Alpha Service",
        updated_at="2026-08-26T00:00:00+00:00",
    )
    real_sync = maintenance.sync_relation_frequency_memory

    def fail_poison(
        target: sqlite3.Connection,
        memory_id: str,
        **kwargs: object,
    ) -> object:
        if memory_id == "poison":
            raise ValueError("injected terminal frequency failure")
        return real_sync(target, memory_id, **kwargs)

    maintenance.sync_relation_frequency_memory = fail_poison
    try:
        assert maintenance._drain_change_rows(conn, 10) == 0
        assert maintenance._drain_change_rows(conn, 10) == 0
        assert maintenance._drain_change_rows(conn, 10) == 0
    finally:
        maintenance.sync_relation_frequency_memory = real_sync
    assert conn.execute(
        "SELECT status FROM relation_frequency_failures WHERE memory_id='poison'"
    ).fetchone()[0] == "dead_letter"

    _insert_direct(
        conn,
        memory_id="healthy-focus",
        scope_id="scope-a",
        entity="Alpha Service",
        updated_at="2026-08-26T00:00:01+00:00",
    )
    assert maintenance._drain_change_rows(conn, 10) == 1
    queued = conn.execute(
        "SELECT status, attempts FROM relation_focus_work "
        "WHERE memory_id='healthy-focus'"
    ).fetchone()
    assert tuple(queued) == ("pending", 0)

    assert maintenance._drain_focus_rows(
        conn,
        10,
        relation_candidate_cap=100,
    ) == 0
    terminal = conn.execute(
        "SELECT status, attempts, last_error FROM relation_focus_work "
        "WHERE memory_id='healthy-focus'"
    ).fetchone()
    assert tuple(terminal[:2]) == ("dead_letter", 1)
    assert "frequency_maintenance_poisoned" in str(terminal[2])
    disposition = conn.execute(
        "SELECT terminal_state, reason_code FROM relation_work_dispositions "
        "WHERE work_kind='focus_sync' AND work_key LIKE 'healthy-focus|%'"
    ).fetchone()
    assert tuple(disposition) == ("poisoned", "frequency_maintenance_poisoned")

    assert maintenance._drain_focus_rows(
        conn,
        10,
        relation_candidate_cap=100,
    ) == 0
    assert conn.execute(
        "SELECT attempts FROM relation_focus_work WHERE memory_id='healthy-focus'"
    ).fetchone()[0] == 1
    policy = generated_relation_scope_policy(conn, ["scope-a"])["scope-a"]
    assert policy["state"] == "blocked"
    assert policy["generated_signal_enabled"] is False


def test_new_corpus_revision_supersedes_blocked_focus_target() -> None:
    conn = _conn()
    _insert_direct(
        conn,
        memory_id="focus",
        scope_id="scope-a",
        entity="Alpha Service",
        updated_at="2026-08-26T00:00:00+00:00",
    )
    _drain(conn)
    conn.execute(
        "UPDATE memories SET updated_at='2026-08-26T00:00:01+00:00' WHERE id='focus'"
    )
    sync_relation_frequency_memory(conn, "focus")
    blocked_generation = conn.execute(
        "SELECT target_revision FROM relation_scope_containment WHERE scope_id='scope-a'"
    ).fetchone()[0]
    conn.execute(
        """
        UPDATE relation_scope_containment
        SET state='blocked', reason_code='affected_candidate_cap_exceeded'
        WHERE scope_id='scope-a'
        """
    )

    conn.execute(
        "UPDATE memories SET updated_at='2026-08-26T00:00:02+00:00' WHERE id='focus'"
    )
    sync_relation_frequency_memory(conn, "focus")

    current = conn.execute(
        """
        SELECT state, reason_code, active_revision, target_revision
        FROM relation_scope_containment WHERE scope_id='scope-a'
        """
    ).fetchone()
    assert str(current[0]) == "degraded"
    assert str(current[1]) == "focus_relation_sync_pending"
    assert int(current[3]) > int(blocked_generation)
    disposed = conn.execute(
        """
        SELECT terminal_state FROM relation_work_dispositions
        WHERE work_kind='containment_target' AND work_key='scope-a'
          AND work_revision=?
        """,
        (str(int(blocked_generation)),),
    ).fetchone()
    assert str(disposed[0]) == "superseded"
