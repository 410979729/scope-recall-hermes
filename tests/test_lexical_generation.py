"""Shadow lexical-generation state machine and trigger contract tests."""

from __future__ import annotations

import json
import sqlite3

import pytest

from scope_recall.lexical_generation import (
    LEXICAL_GENERATION_ID,
    LEXICAL_SHADOW_TABLE,
    LexicalGenerationError,
    activate_generation,
    backfill_generation,
    create_shadow_generation,
    current_generation_id,
    ensure_lexical_generation_schema,
    generation_integrity_report,
    generation_status,
    lexical_schema_status,
    mark_generation_ready,
    rollback_generation,
)
from scope_recall.sql_store import ensure_schema, store_row


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_lexical_generation_schema(conn)
    conn.commit()
    return conn


def _store(
    conn: sqlite3.Connection,
    row_id: str,
    content: str,
    *,
    lifecycle: str = "promoted",
    target: str = "memory",
) -> None:
    store_row(
        conn,
        memory_id=row_id,
        scope_id="scope-a",
        platform="telegram",
        user_id="user-a",
        chat_id="chat-a",
        thread_id="",
        gateway_session_key="",
        agent_identity="aria",
        agent_workspace="workspace-a",
        session_id="session-a",
        source="user",
        target=target,
        content=content,
        metadata=json.dumps({"lifecycle": lifecycle}),
        commit=False,
        enqueue_vector_intent=False,
    )


def _quality_receipt() -> dict[str, object]:
    return {
        "ok": True,
        "status": "ready",
        "cjk_queries": 3,
        "cjk_expected_found": 3,
        "english_regressions": 0,
    }


def test_schema_metadata_is_additive_and_does_not_auto_create_shadow_storage():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    ensure_lexical_generation_schema(conn)

    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    }
    status = lexical_schema_status(conn)

    assert status["current"] is True
    assert {"lexical_generations", "lexical_generation_state"} <= tables
    assert LEXICAL_SHADOW_TABLE not in tables
    assert current_generation_id(conn) == ""


def test_shadow_creation_is_explicit_and_backfill_is_bounded_and_resumable():
    conn = _conn()
    for index in range(5):
        _store(conn, f"visible-{index}", f"数据库迁移记录 {index}")
    _store(conn, "hidden", "隐藏数据库迁移记录", lifecycle="candidate")
    conn.commit()

    created = create_shadow_generation(conn)
    first = backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=2)
    conn.commit()

    assert created["status"] == "building"
    assert first["processed"] == 2
    assert first["complete"] is False
    assert generation_status(conn, LEXICAL_GENERATION_ID)["last_backfilled_rowid"] > 0

    # A concurrent write after the build watermark is maintained by the trigger
    # and must survive resumed backfill without duplicate rows.
    _store(conn, "late", "数据库切换窗口新增记录")
    conn.commit()
    while not bool(backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=2)["complete"]):
        conn.commit()
    conn.commit()

    report = generation_integrity_report(conn, LEXICAL_GENERATION_ID)
    indexed_ids = {
        str(row[0])
        for row in conn.execute(
            f"SELECT memory_id FROM {LEXICAL_SHADOW_TABLE}"
        ).fetchall()
    }
    assert report["healthy"] is True
    assert report["expected_rows"] == 6
    assert report["indexed_rows"] == 6
    assert indexed_ids == {*(f"visible-{index}" for index in range(5)), "late"}


def test_shadow_triggers_track_updates_lifecycle_visibility_and_deletes():
    conn = _conn()
    create_shadow_generation(conn)
    _store(conn, "memory-1", "数据库迁移原稿")
    conn.commit()

    initial = conn.execute(
        f"SELECT content FROM {LEXICAL_SHADOW_TABLE} WHERE memory_id='memory-1'"
    ).fetchone()
    assert initial is not None and initial[0] == "数据库迁移原稿"

    conn.execute(
        "UPDATE memories SET content=?, summary=? WHERE id=?",
        ("数据库迁移修订稿", "数据库迁移修订稿", "memory-1"),
    )
    updated = conn.execute(
        f"SELECT content FROM {LEXICAL_SHADOW_TABLE} WHERE memory_id='memory-1'"
    ).fetchall()
    assert [row[0] for row in updated] == ["数据库迁移修订稿"]

    conn.execute(
        "UPDATE memories SET metadata=? WHERE id=?",
        (json.dumps({"lifecycle": "archived"}), "memory-1"),
    )
    assert conn.execute(
        f"SELECT 1 FROM {LEXICAL_SHADOW_TABLE} WHERE memory_id='memory-1'"
    ).fetchone() is None

    conn.execute(
        "UPDATE memories SET target=?, metadata=? WHERE id=?",
        ("general", json.dumps({"lifecycle": "scratch"}), "memory-1"),
    )
    assert conn.execute(
        f"SELECT 1 FROM {LEXICAL_SHADOW_TABLE} WHERE memory_id='memory-1'"
    ).fetchone() is not None

    conn.execute("DELETE FROM memories WHERE id='memory-1'")
    assert conn.execute(
        f"SELECT 1 FROM {LEXICAL_SHADOW_TABLE} WHERE memory_id='memory-1'"
    ).fetchone() is None


def test_ready_gate_rejects_integrity_drift_and_accepts_reviewed_quality_receipt():
    conn = _conn()
    _store(conn, "memory-1", "数据库迁移方案")
    conn.commit()
    create_shadow_generation(conn)
    backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=10)
    conn.execute(
        f"DELETE FROM {LEXICAL_SHADOW_TABLE} WHERE memory_id='memory-1'"
    )

    with pytest.raises(LexicalGenerationError, match="integrity"):
        mark_generation_ready(
            conn,
            LEXICAL_GENERATION_ID,
            quality_receipt=_quality_receipt(),
        )

    backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=10, reconcile=True)
    ready = mark_generation_ready(
        conn,
        LEXICAL_GENERATION_ID,
        quality_receipt=_quality_receipt(),
    )

    assert ready["status"] == "ready"
    assert generation_status(conn, LEXICAL_GENERATION_ID)["quality_ok"] is True


def test_activation_and_rollback_use_cas_and_retain_legacy_storage():
    conn = _conn()
    _store(conn, "memory-1", "数据库迁移方案")
    conn.commit()
    create_shadow_generation(conn)
    backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=10)
    mark_generation_ready(
        conn,
        LEXICAL_GENERATION_ID,
        quality_receipt=_quality_receipt(),
    )

    with pytest.raises(LexicalGenerationError, match="CAS"):
        activate_generation(
            conn,
            LEXICAL_GENERATION_ID,
            expected_current="unexpected",
        )

    activated = activate_generation(
        conn,
        LEXICAL_GENERATION_ID,
        expected_current="",
    )
    assert activated["status"] == "active"
    assert current_generation_id(conn) == LEXICAL_GENERATION_ID

    with pytest.raises(LexicalGenerationError, match="CAS"):
        rollback_generation(conn, expected_current="unexpected")

    rolled_back = rollback_generation(
        conn,
        expected_current=LEXICAL_GENERATION_ID,
    )
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert rolled_back["status"] == "legacy"
    assert current_generation_id(conn) == ""
    assert {"memories_fts", LEXICAL_SHADOW_TABLE} <= tables
