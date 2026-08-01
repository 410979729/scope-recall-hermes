"""Tests that SQLite doctor checks open live stores read-only.

Doctor must inspect runtime state without accidentally migrating or mutating it."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from scope_recall import doctor_sqlite  # type: ignore[attr-defined]
from scope_recall.operator_ledger import OPERATOR_LEDGER_MIGRATION_ID
from scope_recall.relation_frequency_index import (
    RELATION_FREQUENCY_FAILURE_MIGRATION_ID,
    RELATION_FREQUENCY_INDEX_MIGRATION_ID,
)
from scope_recall.relation_rebuild_queue import (
    RELATION_REBUILD_EXPIRY_MIGRATION_ID,
    RELATION_REBUILD_LEASE_MIGRATION_ID,
    RELATION_REBUILD_MIGRATION_ID,
    RELATION_REBUILD_PROGRESS_MIGRATION_ID,
)
from scope_recall.relation_scope_state import RELATION_SCOPE_RECEIPT_MIGRATION_ID
from scope_recall.sql_store import ensure_schema, schema_migration_status, store_row
from scope_recall.temporal_facts import FACT_CLAIMS_MIGRATION_ID
from scope_recall.vector_reconciliation import VECTOR_RECONCILIATION_MIGRATION_ID


def test_sqlite_report_opens_truth_db_read_only(tmp_path, monkeypatch):
    db_dir = tmp_path / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    calls: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []
    real_connect = sqlite3.connect

    def capture_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        calls.append((database, args, kwargs))
        return real_connect(database, *args, **kwargs)

    observed_query_only: list[int] = []
    real_schema_migration_status = doctor_sqlite.schema_migration_status

    def capture_schema_migration_status(conn: sqlite3.Connection) -> dict[str, Any]:
        observed_query_only.append(int(conn.execute("PRAGMA query_only").fetchone()[0]))
        return real_schema_migration_status(conn)

    monkeypatch.setattr(doctor_sqlite.sqlite3, "connect", capture_connect)
    monkeypatch.setattr(doctor_sqlite, "schema_migration_status", capture_schema_migration_status)

    payload, check, recommendations = doctor_sqlite.sqlite_report(Path(tmp_path))

    assert payload["status"] == "ready"
    assert payload["schema_migrations"]["current"] is False
    assert payload["schema_migrations"]["missing_migrations"] == [
        "0001_baseline_v1_6_0",
        FACT_CLAIMS_MIGRATION_ID,
        RELATION_REBUILD_MIGRATION_ID,
        OPERATOR_LEDGER_MIGRATION_ID,
        RELATION_REBUILD_LEASE_MIGRATION_ID,
        RELATION_SCOPE_RECEIPT_MIGRATION_ID,
        RELATION_FREQUENCY_INDEX_MIGRATION_ID,
        RELATION_REBUILD_PROGRESS_MIGRATION_ID,
        VECTOR_RECONCILIATION_MIGRATION_ID,
        RELATION_REBUILD_EXPIRY_MIGRATION_ID,
        RELATION_FREQUENCY_FAILURE_MIGRATION_ID,
    ]
    assert payload["relation_frequency_index"]["status"] == "schema_missing"
    assert check == {"ok": True, "failures": []}
    assert recommendations == [
        "Relation frequency index schema is missing; initialize with the current provider before relying on bounded relation sync.",
        "Relation rebuild queue schema is missing; initialize with the current provider before relying on bounded relation sync.",
        "Operator ledger schema is missing; initialize with the current provider before relying on filesystem receipts.",
        "SQLite schema migration ledger is not current; run the current scope-recall provider or installer doctor to apply baseline schema metadata before release rollout.",
    ]
    assert calls
    assert observed_query_only == [1]
    database, _args, kwargs = calls[0]
    assert str(database) == f"{db_path.resolve().as_uri()}?mode=ro"
    assert kwargs.get("uri") is True


def test_sqlite_report_fails_on_hidden_fts_membership_drift(tmp_path):
    db_dir = tmp_path / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    store_row(
        conn,
        memory_id="candidate-hidden",
        platform="telegram",
        user_id="user-a",
        chat_id="chat-a",
        thread_id="",
        gateway_session_key="session-a",
        agent_identity="default",
        agent_workspace="hermes",
        scope_id="scope-a",
        session_id="session-a",
        source="tool-store",
        target="memory",
        content="Candidate hidden memory.",
        metadata=json.dumps({"lifecycle": "candidate"}),
    )
    conn.execute(
        "INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)",
        ("candidate-hidden", "Candidate hidden memory.", "Candidate hidden memory."),
    )
    conn.commit()
    conn.close()

    payload, check, recommendations = doctor_sqlite.sqlite_report(Path(tmp_path))

    assert payload["status"] == "needs_repair"
    assert payload["fts_integrity"] == {
        "memory_rows": 1,
        "expected_fts_rows": 0,
        "fts_rows": 1,
        "stale_fts_rows": 0,
        "hidden_fts_rows": 1,
        "missing_fts_rows": 0,
        "duplicate_fts_extra_rows": 0,
        "healthy": False,
        "status": "needs_repair",
    }
    assert check["ok"] is False
    assert len(check["failures"]) == 1
    assert "hidden=1" in check["failures"][0]
    assert any("FTS lifecycle membership drift" in item for item in recommendations)


def test_schema_migration_status_handles_legacy_ledger_without_error_column():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE schema_migrations(
            id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL,
            plugin_version TEXT NOT NULL,
            description TEXT NOT NULL,
            checksum TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'applied'
        )
        """
    )
    conn.execute(
        "INSERT INTO schema_migrations(id, applied_at, plugin_version, description, checksum, status) VALUES ('legacy', 'now', '1.5.3', 'legacy', 'bad', 'applied')"
    )

    status = schema_migration_status(conn)
    ensure_schema(conn)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(schema_migrations)").fetchall()}

    assert status["applied_migrations"][0]["error"] == ""
    assert "error" in columns
