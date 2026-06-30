"""Tests for SQLite FTS indexing and query behavior.

They ensure lexical recall remains available even without vector dependencies."""

from __future__ import annotations

import sqlite3

from scope_recall.sql_store import ensure_schema, reconcile_fts_index, store_row


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _store(conn: sqlite3.Connection, memory_id: str, content: str) -> None:
    store_row(
        conn,
        memory_id=memory_id,
        scope_id="shared-scope",
        platform="cli",
        user_id="joy",
        chat_id="",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="session",
        source="tool-store",
        target="memory",
        content=content,
    )


def test_reconcile_fts_index_removes_stale_rows_and_restores_missing_rows():
    conn = _conn()
    _store(conn, "memory-1", "Joy prefers clean FTS rows.")
    _store(conn, "memory-2", "Scope Recall should repair stale FTS rows.")

    conn.execute("INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)", ("stale-1", "deleted memory", "deleted memory"))
    conn.execute("INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)", ("memory-1", "duplicate old copy", "duplicate old copy"))
    conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", ("memory-2",))
    conn.commit()

    report = reconcile_fts_index(conn)

    assert report["rebuilt"] is True
    assert report["before"]["memory_rows"] == 2
    assert report["before"]["fts_rows"] == 3
    assert report["before"]["stale_fts_rows"] == 1
    assert report["before"]["missing_fts_rows"] == 1
    assert report["before"]["duplicate_fts_extra_rows"] == 1
    assert report["after"] == {
        "memory_rows": 2,
        "fts_rows": 2,
        "stale_fts_rows": 0,
        "missing_fts_rows": 0,
        "duplicate_fts_extra_rows": 0,
        "healthy": True,
    }
    assert [row["memory_id"] for row in conn.execute("SELECT memory_id FROM memories_fts ORDER BY memory_id")] == ["memory-1", "memory-2"]


def test_ensure_schema_reconciles_existing_stale_fts_rows():
    conn = _conn()
    _store(conn, "memory-1", "FTS should stay aligned after schema checks.")
    conn.execute("INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)", ("stale-1", "old", "old"))
    conn.commit()

    ensure_schema(conn)

    assert conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0] == 1
    assert conn.execute("SELECT memory_id FROM memories_fts").fetchone()[0] == "memory-1"
