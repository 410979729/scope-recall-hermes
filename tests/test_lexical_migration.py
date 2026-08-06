"""Operator lexical migration, resumable build, and quality-gate tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scope_recall.lexical_generation import (
    LEXICAL_GENERATION_ID,
    backfill_generation,
    create_shadow_generation,
    current_generation_id,
    ensure_lexical_generation_schema,
    generation_status,
)
from scope_recall.lexical_migration import (
    build_lexical_generation,
    plan_lexical_migration,
    validate_lexical_generation,
)
from scope_recall.sql_store import ensure_schema, store_row


def _store(
    conn: sqlite3.Connection,
    memory_id: str,
    content: str,
    *,
    timestamp: str,
) -> None:
    store_row(
        conn,
        memory_id=memory_id,
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
        target="memory",
        content=content,
        metadata=json.dumps({"lifecycle": "promoted"}),
        commit=False,
        timestamp=timestamp,
        enqueue_vector_intent=False,
    )


def _seed(conn: sqlite3.Connection) -> None:
    _store(
        conn,
        "target",
        "生产数据库迁移方案：先做全量备份，校验副本，安排切换窗口，并完成回滚演练。",
        timestamp="2020-01-01T00:00:00+00:00",
    )
    _store(
        conn,
        "english-target",
        "OAuth redirect validation preserves exact same-origin transport safety.",
        timestamp="2021-01-01T00:00:00+00:00",
    )
    for index in range(40):
        _store(
            conn,
            f"noise-{index:02d}",
            f"数据库监控日报第{index:02d}期：检查数据库容量、连接数和告警状态。",
            timestamp=f"2026-08-05T12:{index:02d}:00+00:00",
        )
    conn.commit()


def test_quality_gate_uses_synthetic_and_live_dual_reads_without_raw_text():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_lexical_generation_schema(conn)
    _seed(conn)
    create_shadow_generation(conn)
    while not bool(
        backfill_generation(
            conn,
            LEXICAL_GENERATION_ID,
            batch_size=7,
        )["complete"]
    ):
        conn.commit()

    receipt = validate_lexical_generation(
        conn,
        LEXICAL_GENERATION_ID,
        sample_limit=16,
    )
    encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True)

    assert receipt["ok"] is True
    assert receipt["synthetic_cjk_queries"] == 3
    assert receipt["synthetic_cjk_expected_found"] == 3
    assert receipt["live_cjk_expected_found"] == receipt["live_cjk_queries"]
    assert receipt["english_regressions"] == 0
    assert "生产数据库迁移方案" not in encoded
    assert "OAuth redirect validation" not in encoded
    conn.close()


def test_build_resumes_committed_backfill_and_marks_generation_ready(tmp_path: Path):
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_lexical_generation_schema(conn)
    _seed(conn)
    create_shadow_generation(conn)
    first = backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=3)
    conn.commit()
    assert first["complete"] is False
    first_last = int(first["last_backfilled_rowid"])
    conn.close()

    resumed = sqlite3.connect(db_path)
    resumed.row_factory = sqlite3.Row
    payload = build_lexical_generation(
        resumed,
        LEXICAL_GENERATION_ID,
        batch_size=5,
        sample_limit=12,
    )

    manifest = generation_status(resumed, LEXICAL_GENERATION_ID)
    assert payload["ok"] is True
    assert payload["status"] == "ready"
    assert payload["resumed_from_rowid"] == first_last
    assert payload["batches"] > 0
    assert manifest["status"] == "ready"
    assert manifest["quality_ok"] is True
    assert current_generation_id(resumed) == ""
    resumed.close()


def test_plan_is_read_only_for_database_without_lexical_schema():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    conn.execute("DROP TABLE IF EXISTS lexical_generation_state")
    conn.execute("DROP TABLE IF EXISTS lexical_generations")
    conn.commit()
    before = conn.total_changes

    plan = plan_lexical_migration(conn)

    assert plan["status"] == "schema_missing"
    assert plan["dry_run"] is True
    assert conn.total_changes == before
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "lexical_generations" not in tables
    conn.close()
