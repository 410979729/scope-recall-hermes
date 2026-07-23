"""Tests for SQLite FTS indexing and query behavior.

They ensure lexical recall remains available even without vector dependencies."""

from __future__ import annotations

import json
import sqlite3

from scope_recall.sql_store import (
    ensure_schema,
    fts_integrity_report,
    reconcile_fts_index,
    store_row,
    update_row,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _store(
    conn: sqlite3.Connection,
    memory_id: str,
    content: str,
    *,
    target: str = "memory",
    metadata: str = "{}",
) -> None:
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
        target=target,
        content=content,
        metadata=metadata,
    )


def test_update_row_is_transaction_neutral_and_caller_can_rollback():
    conn = _conn()
    _store(conn, "memory-1", "Original durable content.")

    updated, _summary, _updated_at = update_row(
        conn,
        memory_id="memory-1",
        content="Tentative replacement content.",
        scope_id="shared-scope",
    )

    assert updated is True
    assert conn.in_transaction is True
    assert conn.execute(
        "SELECT content FROM memories WHERE id = 'memory-1'"
    ).fetchone()[0] == "Tentative replacement content."
    conn.rollback()
    assert conn.execute(
        "SELECT content FROM memories WHERE id = 'memory-1'"
    ).fetchone()[0] == "Original durable content."
    assert conn.execute(
        "SELECT content FROM memories_fts WHERE memory_id = 'memory-1'"
    ).fetchone()[0] == "Original durable content."


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
        "expected_fts_rows": 2,
        "fts_rows": 2,
        "stale_fts_rows": 0,
        "hidden_fts_rows": 0,
        "missing_fts_rows": 0,
        "duplicate_fts_extra_rows": 0,
        "healthy": True,
    }
    assert [row["memory_id"] for row in conn.execute("SELECT memory_id FROM memories_fts ORDER BY memory_id")] == ["memory-1", "memory-2"]


def test_general_scratch_remains_an_expected_fts_member():
    conn = _conn()
    _store(
        conn,
        "general-scratch",
        "Current chat scratch remains locally recallable.",
        target="general",
    )

    report = fts_integrity_report(conn)

    assert report["memory_rows"] == 1
    assert report["expected_fts_rows"] == 1
    assert report["fts_rows"] == 1
    assert report["healthy"] is True


def test_hidden_lifecycle_rows_are_not_expected_fts_members():
    conn = _conn()
    _store(conn, "memory-visible", "Visible promoted memory.")
    _store(
        conn,
        "memory-candidate",
        "Candidate memory must remain outside ordinary lexical recall.",
        metadata=json.dumps({"lifecycle": "candidate"}),
    )

    report = fts_integrity_report(conn)

    assert report["memory_rows"] == 2
    assert report["expected_fts_rows"] == 1
    assert report["fts_rows"] == 1
    assert report["hidden_fts_rows"] == 0
    assert report["healthy"] is True
    assert [
        row["memory_id"]
        for row in conn.execute(
            "SELECT memory_id FROM memories_fts ORDER BY memory_id"
        )
    ] == ["memory-visible"]


def test_reconcile_fts_index_removes_legacy_hidden_rows():
    conn = _conn()
    _store(conn, "memory-visible", "Visible promoted memory.")
    _store(
        conn,
        "memory-candidate",
        "Candidate memory must remain outside ordinary lexical recall.",
        metadata=json.dumps({"lifecycle": "candidate"}),
    )
    conn.execute(
        "INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)",
        ("memory-candidate", "legacy hidden copy", "legacy hidden copy"),
    )
    conn.commit()

    before = fts_integrity_report(conn)
    repaired = reconcile_fts_index(conn)

    assert before["hidden_fts_rows"] == 1
    assert before["healthy"] is False
    assert repaired["rebuilt"] is True
    assert repaired["after"]["healthy"] is True
    assert repaired["after"]["hidden_fts_rows"] == 0
    assert [
        row["memory_id"]
        for row in conn.execute(
            "SELECT memory_id FROM memories_fts ORDER BY memory_id"
        )
    ] == ["memory-visible"]


def test_ensure_schema_does_not_silently_repair_partial_fts_drift():
    conn = _conn()
    _store(conn, "memory-1", "FTS drift requires an explicit repair receipt.")
    conn.execute(
        "INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)",
        ("stale-1", "old", "old"),
    )
    conn.commit()
    before_changes = conn.total_changes

    ensure_schema(conn)

    assert conn.total_changes == before_changes
    assert fts_integrity_report(conn)["healthy"] is False
    report = reconcile_fts_index(conn)
    assert report["rebuilt"] is True
    assert report["after"]["healthy"] is True
