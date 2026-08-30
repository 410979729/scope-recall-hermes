"""Contracts for the incremental relation-frequency write path."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

import pytest

import scope_recall.relation_extraction as relation_extraction
import scope_recall.relation_frequency_maintenance as frequency_maintenance
from scope_recall.relation_extraction import sync_extracted_relations_for_memory
from scope_recall.relation_frequency_index import (
    ensure_relation_frequency_index_schema,
    sync_relation_frequency_memory,
)
from scope_recall.sql_store import delete_rows, ensure_schema, store_row


def _store(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    scope_id: str,
    entity: str,
    timestamp: str,
    commit: bool = False,
) -> None:
    stored_id, _summary, _updated_at, inserted = store_row(
        conn,
        memory_id=memory_id,
        scope_id=scope_id,
        platform="test",
        user_id="joy",
        chat_id="chat",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="test",
        session_id="session",
        source="fixture",
        target="project",
        content=f"{entity} service owns this durable fact",
        metadata=json.dumps({"entities": [entity]}),
        allow_duplicate=True,
        commit=commit,
        timestamp=timestamp,
        enqueue_vector_intent=False,
    )
    assert inserted is True
    assert stored_id == memory_id


def _frequency(conn: sqlite3.Connection, scope_id: str, entity: str) -> int:
    row = conn.execute(
        """
        SELECT document_count FROM relation_scope_entity_frequency
        WHERE scope_id=? AND entity=?
        """,
        (scope_id, entity),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _visible_count(conn: sqlite3.Connection, scope_id: str) -> int:
    row = conn.execute(
        "SELECT visible_memory_count FROM relation_scope_statistics WHERE scope_id=?",
        (scope_id,),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def test_frequency_schema_ensure_does_not_rewrite_current_generation_receipts() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES(
            'memory-pending', 'scope-a', 'fixture', 'project',
            'Alpha Service fact', 'Alpha Service fact',
            '2026-08-26T00:00:00+00:00', '2026-08-26T00:00:00+00:00',
            '{"entities":["Alpha Service"]}'
        )
        """
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM relation_frequency_changes"
    ).fetchone()[0] == 1

    before = conn.total_changes
    ensure_relation_frequency_index_schema(conn)

    assert conn.total_changes == before
    conn.close()


def test_relation_frequency_delta_is_atomic_across_lifecycle_move_and_delete() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    _store(
        conn,
        memory_id="memory-a",
        scope_id="scope-a",
        entity="Alpha Service",
        timestamp="2026-07-21T10:00:00+00:00",
    )
    assert _frequency(conn, "scope-a", "alpha service") == 1
    assert _visible_count(conn, "scope-a") == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM relation_frequency_changes"
    ).fetchone()[0] == 0

    before = conn.execute(
        "SELECT metadata FROM memories WHERE id='memory-a'"
    ).fetchone()
    metadata = json.loads(str(before[0]))
    metadata["entities"] = ["Beta Service"]
    conn.execute(
        """
        UPDATE memories
        SET metadata=?, updated_at='2026-07-21T10:01:00+00:00'
        WHERE id='memory-a'
        """,
        (json.dumps(metadata),),
    )
    sync_relation_frequency_memory(conn, "memory-a")
    assert _frequency(conn, "scope-a", "alpha service") == 0
    assert _frequency(conn, "scope-a", "beta service") == 1

    metadata["lifecycle"] = "archived"
    conn.execute(
        """
        UPDATE memories
        SET metadata=?, updated_at='2026-07-21T10:02:00+00:00'
        WHERE id='memory-a'
        """,
        (json.dumps(metadata),),
    )
    sync_relation_frequency_memory(conn, "memory-a")
    assert _frequency(conn, "scope-a", "beta service") == 0
    assert _visible_count(conn, "scope-a") == 0

    metadata["lifecycle"] = "active"
    conn.execute(
        """
        UPDATE memories
        SET metadata=?, updated_at='2026-07-21T10:03:00+00:00'
        WHERE id='memory-a'
        """,
        (json.dumps(metadata),),
    )
    sync_relation_frequency_memory(conn, "memory-a")
    assert _frequency(conn, "scope-a", "beta service") == 1
    assert _visible_count(conn, "scope-a") == 1

    conn.execute(
        """
        UPDATE memories
        SET scope_id='scope-b', updated_at='2026-07-21T10:04:00+00:00'
        WHERE id='memory-a'
        """
    )
    sync_relation_frequency_memory(conn, "memory-a")
    assert _frequency(conn, "scope-a", "beta service") == 0
    assert _visible_count(conn, "scope-a") == 0
    assert _frequency(conn, "scope-b", "beta service") == 1
    assert _visible_count(conn, "scope-b") == 1

    delete_rows(conn, ["memory-a"], commit=False)
    assert _frequency(conn, "scope-b", "beta service") == 0
    assert _visible_count(conn, "scope-b") == 0

    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM relation_entity_postings"
    ).fetchone()[0] == 0
    conn.close()


def _seed_indexed_scope(conn: sqlite3.Connection, row_count: int) -> None:
    scope_id = "scope-large"
    entity = "common hub"
    entities_json = json.dumps([entity], separators=(",", ":"))
    entities_hash = hashlib.sha256(entities_json.encode()).hexdigest()
    timestamp = "2026-07-21T09:00:00+00:00"
    rows = [
        (
            f"peer-{index:05d}",
            scope_id,
            "fixture",
            "project",
            f"Common Hub fact {index}",
            f"fact {index}",
            timestamp,
            timestamp,
            json.dumps({"entities": ["Common Hub"]}),
        )
        for index in range(row_count)
    ]
    conn.executemany(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.executemany(
        """
        INSERT INTO relation_indexed_memories(
            memory_id, scope_id, updated_at, visible,
            entities_json, entities_sha256, indexed_at
        ) VALUES(?, ?, ?, 1, ?, ?, ?)
        """,
        (
            (row[0], scope_id, timestamp, entities_json, entities_hash, timestamp)
            for row in rows
        ),
    )
    conn.executemany(
        """
        INSERT INTO relation_entity_postings(scope_id, entity, memory_id)
        VALUES(?, ?, ?)
        """,
        ((scope_id, entity, row[0]) for row in rows),
    )
    conn.execute(
        """
        INSERT INTO relation_scope_entity_frequency(
            scope_id, entity, document_count, updated_at
        ) VALUES(?, ?, ?, ?)
        """,
        (scope_id, entity, row_count, timestamp),
    )
    conn.execute("DELETE FROM relation_frequency_changes")
    conn.execute(
        """
        UPDATE relation_frequency_backfill
        SET status='complete', cursor_memory_id=?, processed_memories=?,
            updated_at=?, completed_at=?
        WHERE scope_id=?
        """,
        (rows[-1][0], row_count, timestamp, timestamp, scope_id),
    )
    conn.execute(
        """
        UPDATE relation_scope_statistics
        SET visible_memory_count=?, statistics_revision=-1,
            blocked_entities_json='[]', blocked_entities_sha256=''
        WHERE scope_id=?
        """,
        (row_count, scope_id),
    )
    conn.commit()


def test_immediate_relation_sync_never_scans_all_truth_in_a_large_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dynamic counterexample for the former per-write full-scope scan."""

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _seed_indexed_scope(conn, 10_000)
    _store(
        conn,
        memory_id="focus-memory",
        scope_id="scope-large",
        entity="Focus Service",
        timestamp="2026-07-21T10:00:00+00:00",
    )

    original_memory_rows = relation_extraction._memory_rows
    bounded_calls: list[list[str]] = []

    def bounded_memory_rows(
        connection: sqlite3.Connection,
        *,
        scope_ids: Any = None,
        memory_ids: Any = None,
    ) -> list[sqlite3.Row]:
        assert memory_ids is not None, "immediate sync attempted an unbounded scope read"
        ids = list(memory_ids)
        assert len(ids) <= 26
        bounded_calls.append(ids)
        return original_memory_rows(
            connection,
            scope_ids=scope_ids,
            memory_ids=ids,
        )

    monkeypatch.setattr(relation_extraction, "_memory_rows", bounded_memory_rows)
    statements: list[str] = []
    conn.set_trace_callback(lambda sql: statements.append(sql.lower()))
    result = sync_extracted_relations_for_memory(
        conn,
        memory_id="focus-memory",
        scope_ids=["scope-large"],
        local_peer_limit=25,
        commit=False,
    )
    conn.set_trace_callback(None)

    assert result["ok"] is True
    assert result["deferred"] is False
    assert result["status"] == "synced"
    assert result["selected_peer_count"] == result["total_peer_count"]
    assert result["total_peer_count"] <= 25
    assert bounded_calls and all(len(ids) <= 26 for ids in bounded_calls)
    assert not any("select count(*) from memories" in sql for sql in statements)
    assert not any(
        "select m.scope_id, m.target, m.content, m.metadata" in sql
        for sql in statements
    )
    conn.rollback()
    conn.close()


def test_relation_frequency_poison_row_does_not_block_healthy_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _store(
        conn,
        memory_id="a-poison",
        scope_id="scope-a",
        entity="Poison Service",
        timestamp="2026-07-21T10:00:00+00:00",
    )
    _store(
        conn,
        memory_id="z-healthy",
        scope_id="scope-a",
        entity="Healthy Service",
        timestamp="2026-07-21T10:00:01+00:00",
    )
    conn.execute(
        "UPDATE memories SET updated_at='2026-07-21T11:00:00+00:00' "
        "WHERE id IN ('a-poison', 'z-healthy')"
    )
    conn.commit()

    real_sync = frequency_maintenance.sync_relation_frequency_memory

    def fail_one(db: sqlite3.Connection, memory_id: str, **kwargs: Any):
        if memory_id == "a-poison":
            raise ValueError("injected malformed relation metadata")
        return real_sync(db, memory_id, **kwargs)

    monkeypatch.setattr(
        frequency_maintenance,
        "sync_relation_frequency_memory",
        fail_one,
    )

    assert frequency_maintenance._drain_change_rows(conn, 10) == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM relation_frequency_changes WHERE memory_id='z-healthy'"
    ).fetchone()[0] == 0
    assert tuple(
        conn.execute(
            "SELECT attempts, status FROM relation_frequency_failures "
            "WHERE memory_id='a-poison'"
        ).fetchone()
    ) == (1, "retry")

    assert frequency_maintenance._drain_change_rows(conn, 10) == 0
    assert frequency_maintenance._drain_change_rows(conn, 10) == 0
    failure = conn.execute(
        "SELECT attempts, status, last_error FROM relation_frequency_failures "
        "WHERE memory_id='a-poison'"
    ).fetchone()
    assert tuple(failure[:2]) == (3, "dead_letter")
    assert "ValueError" in failure[2]
    assert conn.execute(
        "SELECT COUNT(*) FROM relation_frequency_changes WHERE memory_id='a-poison'"
    ).fetchone()[0] == 1
    assert frequency_maintenance._drain_change_rows(conn, 10) == 0
    assert tuple(
        conn.execute(
            "SELECT attempts, status FROM relation_frequency_failures "
            "WHERE memory_id='a-poison'"
        ).fetchone()
    ) == (3, "dead_letter")
    report = frequency_maintenance.relation_frequency_index_report(conn)
    assert report["status"] == "error"
    assert report["dead_letter_failures"] == 1
