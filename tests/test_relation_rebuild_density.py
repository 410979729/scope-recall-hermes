"""Density regressions for adaptive relation-rebuild queue chunks."""

from __future__ import annotations

# Historical counterexample for the removed legacy worker. The executable
# retirement contract is covered by test_relation_rebuild_retirement.py.
__test__ = False

import json
import sqlite3

from scope_recall.relation_rebuild_queue import (
    drain_relation_rebuild_queue,
    enqueue_relation_rebuild,
)
from scope_recall.sql_store import ensure_schema, store_row


def _store(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    content: str,
    timestamp: str,
) -> None:
    store_row(
        conn,
        memory_id=memory_id,
        scope_id="scope-dense",
        platform="test",
        user_id="joy",
        chat_id="",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="test",
        session_id="dense-relations",
        source="fixture",
        target="project",
        content=content,
        metadata=json.dumps(
            {
                "entities": ["Project Atlas", "Redis"],
                "memory_type": "factual",
            }
        ),
        allow_duplicate=True,
        timestamp=timestamp,
    )


def test_dense_normal_relation_data_is_adaptively_chunked_not_dead_lettered() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _store(
        conn,
        memory_id="focus",
        content=(
            "Project Atlas deployment depends on Redis availability and affects "
            "Project Atlas services."
        ),
        timestamp="2026-07-20T00:00:00+00:00",
    )
    for index in range(13):
        _store(
            conn,
            memory_id=f"peer-{index:02d}",
            content=(
                f"Project Atlas Redis service peer {index} deployment availability "
                "runbook and checklist."
            ),
            timestamp=f"2026-07-20T00:00:{index + 1:02d}+00:00",
        )
    conn.execute("DELETE FROM relation_rebuild_queue")
    revision = str(
        conn.execute("SELECT updated_at FROM memories WHERE id='focus'").fetchone()[0]
    )
    enqueue_relation_rebuild(
        conn,
        scope_id="scope-dense",
        focus_memory_id="focus",
        requested_updated_at=revision,
        reason="dense normal fixture",
        commit=True,
    )

    result = drain_relation_rebuild_queue(
        conn,
        max_events=10,
        pair_limit=250,
        max_candidates=24,
        max_failures=3,
        worker_id="dense-worker",
    )

    assert result == {
        "claimed": 3,
        "chunks_completed": 3,
        "events_completed": 1,
        "superseded": 0,
        "failed": 0,
        "dead_lettered": 0,
    }
    event = conn.execute(
        """
        SELECT status, cursor_memory_id, processed_pairs, attempts, failures,
               lease_owner, last_error
        FROM relation_rebuild_queue
        WHERE focus_memory_id='focus'
        """
    ).fetchone()
    assert tuple(event) == (
        "completed",
        "peer-12",
        13,
        3,
        0,
        "",
        "",
    )
    relation_count = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM memory_relations
            WHERE source_memory_id='focus' OR target_memory_id='focus'
            """
        ).fetchone()[0]
    )
    assert relation_count == 52
    conn.close()
