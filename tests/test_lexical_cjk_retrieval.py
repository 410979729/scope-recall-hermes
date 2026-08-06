"""CJK shadow dual-read discovery and rollback regression tests."""

from __future__ import annotations

import json
import sqlite3
import threading

from scope_recall.lexical_generation import (
    LEXICAL_GENERATION_ID,
    activate_generation,
    backfill_generation,
    create_shadow_generation,
    ensure_lexical_generation_schema,
    mark_generation_ready,
    rollback_generation,
)
from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.storage_views import search_db_memories


class _Provider:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.RLock()
        self._accessible_scope_ids = ["scope-a"]
        self._retrieval_config = {"candidate_pool": 20, "min_score": 0.18}

    def _require_conn(self) -> sqlite3.Connection:
        return self._conn

    @staticmethod
    def _config_value(_key: str, default):
        return default


def _store(
    conn: sqlite3.Connection,
    memory_id: str,
    content: str,
    *,
    timestamp: str,
    lifecycle: str = "promoted",
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
        metadata=json.dumps({"lifecycle": lifecycle}),
        commit=False,
        timestamp=timestamp,
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


def _corpus() -> tuple[sqlite3.Connection, _Provider]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_lexical_generation_schema(conn)
    _store(
        conn,
        "target",
        "生产数据库迁移方案：先做全量备份，校验副本，安排切换窗口，并在变更前完成回滚演练。",
        timestamp="2020-01-01T00:00:00+00:00",
    )
    _store(
        conn,
        "english-target",
        "OAuth redirect validation preserves exact same-origin transport safety.",
        timestamp="2021-01-01T00:00:00+00:00",
    )
    _store(
        conn,
        "hidden-cjk",
        "生产数据库切换与回滚演练隐藏草稿",
        timestamp="2026-07-01T00:00:00+00:00",
        lifecycle="candidate",
    )
    for index in range(40):
        _store(
            conn,
            f"noise-{index:02d}",
            f"数据库监控日报第{index:02d}期：检查数据库容量、连接数和告警状态。",
            timestamp=f"2026-08-05T12:{index:02d}:00+00:00",
        )
    conn.commit()
    create_shadow_generation(conn)
    while not bool(
        backfill_generation(
            conn,
            LEXICAL_GENERATION_ID,
            batch_size=7,
        )["complete"]
    ):
        conn.commit()
    mark_generation_ready(
        conn,
        LEXICAL_GENERATION_ID,
        quality_receipt=_quality_receipt(),
    )
    conn.commit()
    return conn, _Provider(conn)


def _ids(provider: _Provider, query: str, *, generation_override: str | None) -> list[str]:
    return [
        item.id
        for item in search_db_memories(
            provider,
            query,
            limit=10,
            generation_override=generation_override,
        )
    ]


def test_ready_shadow_override_recovers_cjk_target_under_newer_noise():
    conn, provider = _corpus()
    queries = (
        "数据库迁移方案",
        "生产库切换前需要做什么",
        "上线前怎么做回滚演练",
    )

    legacy = [_ids(provider, query, generation_override="") for query in queries]
    shadow = [
        _ids(provider, query, generation_override=LEXICAL_GENERATION_ID)
        for query in queries
    ]

    assert "target" not in legacy[0]
    assert "target" not in legacy[1]
    assert all("target" in result for result in shadow)
    assert all("hidden-cjk" not in result for result in shadow)
    conn.close()


def test_supplemental_mode_cannot_remove_english_legacy_candidates():
    conn, provider = _corpus()
    queries = (
        "OAuth redirect validation",
        "same origin transport safety",
        "exact redirect safety",
    )

    for query in queries:
        legacy = set(_ids(provider, query, generation_override=""))
        shadow = set(
            _ids(provider, query, generation_override=LEXICAL_GENERATION_ID)
        )
        assert legacy <= shadow
    conn.close()


def test_cjk_bigram_fallback_uses_one_bounded_sql_scan():
    conn, provider = _corpus()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    ids = _ids(
        provider,
        "生产库切换前需要做什么",
        generation_override=LEXICAL_GENERATION_ID,
    )
    conn.set_trace_callback(None)

    fallback_statements = [
        statement
        for statement in statements
        if "query_terms(term)" in statement and "cjk_match_count" in statement
    ]
    assert "target" in ids
    assert len(fallback_statements) == 1
    assert "LIMIT 20" in fallback_statements[0]
    conn.close()


def test_activation_and_rollback_switch_default_reads_without_deleting_shadow():
    conn, provider = _corpus()
    query = "生产库切换前需要做什么"

    assert "target" not in _ids(provider, query, generation_override=None)
    activate_generation(
        conn,
        LEXICAL_GENERATION_ID,
        expected_current="",
    )
    conn.commit()
    assert "target" in _ids(provider, query, generation_override=None)

    rollback_generation(
        conn,
        expected_current=LEXICAL_GENERATION_ID,
    )
    conn.commit()
    assert "target" not in _ids(provider, query, generation_override=None)
    assert "target" in _ids(
        provider,
        query,
        generation_override=LEXICAL_GENERATION_ID,
    )
    conn.close()


def test_active_cjk_generation_ranks_old_target_inside_requested_limit():
    conn, provider = _corpus()
    activate_generation(
        conn,
        LEXICAL_GENERATION_ID,
        expected_current="",
    )
    conn.commit()

    results = search_db_memories(provider, "数据库迁移方案", limit=5)

    assert len(results) <= 5
    assert "target" in [item.id for item in results]
    conn.close()
