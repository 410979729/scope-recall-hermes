"""Read-only doctor reporting for the active lexical shadow generation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from scope_recall.doctor_sqlite import sqlite_report
from scope_recall.lexical_generation import (
    LEXICAL_GENERATION_ID,
    LEXICAL_SHADOW_TABLE,
    activate_generation,
)
from scope_recall.lexical_migration import build_lexical_generation
from scope_recall.sql_store import ensure_schema, store_row


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _database(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "hermes"
    storage = home / "scope-recall"
    storage.mkdir(parents=True)
    db_path = storage / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    store_row(
        conn,
        memory_id="target",
        scope_id="scope-a",
        platform="local",
        user_id="user-a",
        chat_id="chat-a",
        thread_id="",
        gateway_session_key="",
        agent_identity="aria",
        agent_workspace="workspace-a",
        session_id="session-a",
        source="user",
        target="memory",
        content="数据库迁移方案与回滚演练",
        metadata=json.dumps({"lifecycle": "promoted"}),
        commit=True,
        enqueue_vector_intent=False,
    )
    build_lexical_generation(
        conn,
        LEXICAL_GENERATION_ID,
        batch_size=2,
        sample_limit=4,
    )
    activate_generation(
        conn,
        LEXICAL_GENERATION_ID,
        expected_current="",
    )
    conn.commit()
    conn.close()
    return home, db_path


def test_doctor_reports_healthy_active_generation_without_writes(tmp_path: Path):
    home, db_path = _database(tmp_path)
    before = _sha(db_path)

    sqlite_payload, gate, _recommendations = sqlite_report(home)

    lexical = sqlite_payload["lexical_generation"]
    assert lexical["status"] == "active"
    assert lexical["healthy"] is True
    assert lexical["current_generation_id"] == LEXICAL_GENERATION_ID
    assert gate["ok"] is True
    assert _sha(db_path) == before


def test_doctor_fails_when_active_shadow_integrity_drifts(tmp_path: Path):
    home, db_path = _database(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        f"DELETE FROM {LEXICAL_SHADOW_TABLE} WHERE memory_id='target'"
    )
    conn.commit()
    conn.close()
    before = _sha(db_path)

    sqlite_payload, gate, recommendations = sqlite_report(home)

    lexical = sqlite_payload["lexical_generation"]
    assert lexical["status"] == "needs_repair"
    assert lexical["healthy"] is False
    assert lexical["integrity"]["missing_rows"] == 1
    assert gate["ok"] is False
    assert any("lexical shadow" in failure.lower() for failure in gate["failures"])
    assert any("lexical rollback" in item.lower() for item in recommendations)
    assert _sha(db_path) == before
