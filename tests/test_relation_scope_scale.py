"""Algorithmic work budgets for incremental relation-frequency statistics."""

from __future__ import annotations

import json
import sqlite3

import pytest

from scope_recall.relation_extraction import (
    scope_high_frequency_relation_entities_by_scope,
)
from scope_recall.relation_frequency_maintenance import drain_relation_frequency_work
from scope_recall.relation_scope_state import blocked_entities_receipt_hash
from scope_recall.sql_store import ensure_schema


def _seed_frequency_receipt_fixture(
    conn: sqlite3.Connection,
    *,
    row_count: int,
    scope_id: str = "scope-scale",
    entity: str = "novel hub",
) -> None:
    """Seed large truth plus its already-built companion without timing setup work."""

    conn.executemany(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES(?, ?, 'fixture', 'project', 'fact', 'fact',
                 '2026-07-20T00:00:00Z', '2026-07-20T00:00:00Z',
                 '{"entities":["Novel Hub"]}')
        """,
        ((f"memory-{index:06d}", scope_id) for index in range(row_count)),
    )
    # This fixture represents a completed prior backfill.  The test budget below
    # measures the steady-state read contract, not one-time migration work.
    conn.execute("DELETE FROM relation_frequency_changes")
    conn.execute(
        """
        UPDATE relation_frequency_backfill
        SET status='complete', cursor_memory_id=?, processed_memories=?,
            updated_at='2026-07-20T00:00:00Z', completed_at='2026-07-20T00:00:00Z'
        WHERE scope_id=?
        """,
        (f"memory-{row_count - 1:06d}", row_count, scope_id),
    )
    conn.execute(
        """
        INSERT INTO relation_scope_entity_frequency(
            scope_id, entity, document_count, updated_at
        ) VALUES(?, ?, ?, '2026-07-20T00:00:00Z')
        """,
        (scope_id, entity, row_count),
    )
    revision = int(
        conn.execute(
            "SELECT corpus_revision FROM relation_scope_statistics WHERE scope_id=?",
            (scope_id,),
        ).fetchone()[0]
    )
    blocked_json = json.dumps([entity], separators=(",", ":"))
    blocked_sha = blocked_entities_receipt_hash(scope_id, revision, {entity})
    conn.execute(
        """
        UPDATE relation_scope_statistics
        SET visible_memory_count=?, statistics_revision=?,
            blocked_entities_json=?, blocked_entities_sha256=?
        WHERE scope_id=?
        """,
        (row_count, revision, blocked_json, blocked_sha, scope_id),
    )
    conn.commit()


@pytest.mark.parametrize("row_count", (10_000, 100_000))
def test_relation_frequency_read_budget_is_independent_of_scope_size(
    row_count: int,
) -> None:
    """The steady-state lookup reads counters, never all truth rows."""

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _seed_frequency_receipt_fixture(conn, row_count=row_count)

    vm_steps = 0

    def count_vm_steps() -> int:
        nonlocal vm_steps
        vm_steps += 100
        return 0

    conn.set_progress_handler(count_vm_steps, 100)
    blocked_by_scope = scope_high_frequency_relation_entities_by_scope(
        conn, ["scope-scale"]
    )
    conn.set_progress_handler(None, 0)

    assert blocked_by_scope == {"scope-scale": {"novel hub"}}
    assert vm_steps <= 10_000
    conn.close()


def test_relation_frequency_statistics_follow_truth_not_a_stale_recall_index() -> None:
    """The semantic companion is updated from truth, never ``memory_entities``."""

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    conn.executemany(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES(?, 'scope-stale', 'fixture', 'project', 'fact', 'fact',
                 '2026-07-20T00:00:00Z', '2026-07-20T00:00:00Z',
                 '{"entities":["Truth Hub"]}')
        """,
        ((f"memory-{index:03d}",) for index in range(20)),
    )
    conn.execute(
        """
        INSERT INTO memory_entities(memory_id, entity)
        SELECT id, 'Stale Hub' FROM memories
        """
    )
    drain_relation_frequency_work(
        conn,
        change_limit=100,
        backfill_limit=100,
        reclassification_limit=100,
    )

    blocked_by_scope = scope_high_frequency_relation_entities_by_scope(
        conn, ["scope-stale"]
    )

    assert blocked_by_scope == {"scope-stale": {"truth hub"}}
    conn.close()
