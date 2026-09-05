"""CJK shadow dual-read discovery and rollback regression tests."""

from __future__ import annotations

import json
import sqlite3
import threading

from scope_recall.lexical_generation import (
    LEXICAL_GENERATION_ID,
    LEXICAL_POSTINGS_TABLE,
    LEXICAL_QUALITY_PROVENANCE,
    LEXICAL_SHADOW_TABLE,
    activate_generation,
    backfill_generation,
    create_shadow_generation,
    current_generation_id,
    ensure_lexical_generation_schema,
    generation_integrity_report,
    lexical_quality_evidence_fingerprint,
    lexical_source_binding,
    mark_generation_ready,
    rollback_generation,
    supplemental_table_for_search,
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


def _quality_receipt(conn: sqlite3.Connection) -> dict[str, object]:
    receipt = {
        "ok": True,
        "status": "ready",
        "generation_id": LEXICAL_GENERATION_ID,
        "synthetic_cjk_queries": 3,
        "synthetic_cjk_expected_found": 3,
        "live_cjk_queries": 0,
        "live_cjk_expected_found": 0,
        "english_queries": 1,
        "cjk_queries": 3,
        "cjk_expected_found": 3,
        "english_regressions": 0,
        "integrity": generation_integrity_report(conn, LEXICAL_GENERATION_ID),
        "source_binding": lexical_source_binding(conn),
        "provenance": dict(LEXICAL_QUALITY_PROVENANCE),
        "contains_raw_samples": False,
    }
    receipt["evidence_fingerprint"] = lexical_quality_evidence_fingerprint(receipt)
    return receipt


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
    for index in range(60):
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
        quality_receipt=_quality_receipt(conn),
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


_SHADOW_READ_TABLES = frozenset(
    {
        LEXICAL_SHADOW_TABLE,
        f"{LEXICAL_SHADOW_TABLE}_content",
        f"{LEXICAL_SHADOW_TABLE}_idx",
        LEXICAL_POSTINGS_TABLE,
    }
)


class _FetchedRows:
    def __init__(self, rows: list) -> None:
        self._rows = list(rows)
        self._index = 0

    def fetchall(self) -> list:
        return list(self._rows)

    def fetchone(self):
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row


def _result_ids(rows) -> list[str]:
    ids: list[str] = []
    for row in rows:
        keys = set(row.keys()) if hasattr(row, "keys") else set()
        if "id" in keys:
            ids.append(str(row["id"]))
        elif "memory_id" in keys:
            ids.append(str(row["memory_id"]))
    return ids


def _is_shadow_result_sql(sql: str) -> bool:
    compact = " ".join(str(sql).split())
    if LEXICAL_SHADOW_TABLE in compact and "MATCH" in compact and "SELECT m." in compact:
        return True
    return LEXICAL_POSTINGS_TABLE in compact and "JOIN memories" in compact


class _ShadowReadProxy:
    """Test-local execute proxy that records IDs returned by shadow-index SQL."""

    def __init__(self, inner: sqlite3.Connection, shadow_ids: list[str]) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_shadow_ids", shadow_ids)

    def execute(self, sql, parameters=()):
        cursor = self._inner.execute(sql, parameters)
        if not _is_shadow_result_sql(sql):
            return cursor
        rows = cursor.fetchall()
        self._shadow_ids.extend(_result_ids(rows))
        return _FetchedRows(rows)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def observe_shadow_channel(
    conn: sqlite3.Connection,
    provider: _Provider,
    query: str,
    *,
    generation: str | None,
    deny_tables: tuple[str, ...] = (),
) -> dict[str, object]:
    """Bind a generation, then observe shadow-index SQL under a read authorizer."""

    bound = supplemental_table_for_search(conn, generation)
    shadow_ids: list[str] = []
    tables_read: list[str] = []
    deny = frozenset(deny_tables)

    def authorizer(action, table, _column, _db, _trigger):
        if action != sqlite3.SQLITE_READ or not table:
            return sqlite3.SQLITE_OK
        tables_read.append(str(table))
        if table in deny:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    changes_before = conn.total_changes
    conn.execute("PRAGMA query_only=ON")
    conn.set_authorizer(authorizer)
    provider._conn = _ShadowReadProxy(conn, shadow_ids)
    error: BaseException | None = None
    ids: list[str] = []
    try:
        ids = _ids(provider, query, generation_override=generation)
    except Exception as exc:
        error = exc
    finally:
        provider._conn = conn
        conn.set_authorizer(None)
        conn.execute("PRAGMA query_only=OFF")
    return {
        "generation": generation,
        "bound_table": bound,
        "ids": ids,
        "error": error,
        "shadow_sql_ids": list(shadow_ids),
        "tables_read": frozenset(tables_read),
        "read_only": conn.total_changes == changes_before,
    }


def ablate_shadow_document(conn: sqlite3.Connection, memory_id: str) -> None:
    """Remove one truth id from the shadow FTS/postings tables only."""

    row = conn.execute("SELECT rowid FROM memories WHERE id=?", (memory_id,)).fetchone()
    if row is not None:
        conn.execute(
            f"DELETE FROM {LEXICAL_POSTINGS_TABLE} WHERE docid=?",
            (int(row[0]),),
        )
    conn.execute(
        f"DELETE FROM {LEXICAL_SHADOW_TABLE} WHERE memory_id=?",
        (memory_id,),
    )
    conn.commit()


def shadow_index_is_independent(evidence: dict[str, object], expected_id: str) -> bool:
    """True only when the bound shadow index itself produced the expected id."""

    tables = evidence["tables_read"]
    assert isinstance(tables, frozenset)
    ids = evidence["ids"]
    shadow_sql_ids = evidence["shadow_sql_ids"]
    assert isinstance(ids, list)
    assert isinstance(shadow_sql_ids, list)
    return (
        evidence["bound_table"] == LEXICAL_SHADOW_TABLE
        and evidence["error"] is None
        and evidence["read_only"] is True
        and bool(_SHADOW_READ_TABLES & tables)
        and expected_id in ids
        and expected_id in shadow_sql_ids
    )


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
    assert all("target" in result for result in shadow)
    assert all("hidden-cjk" not in result for result in shadow)

    natural_query = queries[1]
    isolated = observe_shadow_channel(
        conn,
        provider,
        natural_query,
        generation=LEXICAL_GENERATION_ID,
    )
    assert shadow_index_is_independent(isolated, "target"), isolated

    wrong_route = observe_shadow_channel(
        conn,
        provider,
        natural_query,
        generation=LEXICAL_GENERATION_ID,
        deny_tables=(LEXICAL_SHADOW_TABLE, LEXICAL_POSTINGS_TABLE),
    )
    assert not shadow_index_is_independent(wrong_route, "target"), wrong_route
    assert wrong_route["error"] is not None

    ablate_shadow_document(conn, "target")
    rescued = observe_shadow_channel(
        conn,
        provider,
        natural_query,
        generation=LEXICAL_GENERATION_ID,
    )
    assert not shadow_index_is_independent(rescued, "target"), rescued
    assert "target" not in rescued["shadow_sql_ids"]
    conn.close()


def test_common_cjk_trigram_is_filtered_before_fts_rank_fanout():
    conn, provider = _corpus()
    changes_before = conn.total_changes
    conn.execute("PRAGMA query_only=ON")
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    ids = _ids(
        provider,
        "数据库迁移方案",
        generation_override=LEXICAL_GENERATION_ID,
    )
    conn.set_trace_callback(None)

    shadow_matches = [
        statement
        for statement in statements
        if "FROM memories_fts_cjk_v1" in statement and " MATCH " in statement
    ]
    assert "target" in ids
    assert len(shadow_matches) == 1
    assert '"数据库"' not in shadow_matches[0]
    assert '"据库迁"' in shadow_matches[0]
    assert any(
        "lexical_cjk_postings_v1" in statement and "LIMIT 51" in statement
        for statement in statements
    )
    assert conn.total_changes == changes_before
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


def test_cjk_bigram_fallback_uses_indexed_postings_without_truth_scan():
    conn, provider = _corpus()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    ids = _ids(
        provider,
        "生产库切换前需要做什么",
        generation_override=LEXICAL_GENERATION_ID,
    )
    conn.set_trace_callback(None)

    postings_statements = [
        statement for statement in statements if "lexical_cjk_postings_v1" in statement
    ]
    assert "target" in ids
    assert postings_statements, "bigram fallback must use the indexed postings table"
    assert not any("instr(m.content" in statement for statement in statements), (
        "bigram fallback must not run correlated instr() scans over truth rows"
    )
    plan = conn.execute(
        "EXPLAIN QUERY PLAN " + postings_statements[0]
    ).fetchall()
    plan_text = " ".join(str(row[-1]) for row in plan)
    assert "SCAN memories" not in plan_text
    conn.close()


def test_shadow_rows_use_truth_rowid_identity():
    conn, _provider = _corpus()
    mismatched = conn.execute(
        """
        SELECT COUNT(*)
        FROM memories_fts_cjk_v1 f
        LEFT JOIN memories m ON m.rowid = f.rowid AND m.id = f.memory_id
        WHERE m.rowid IS NULL
        """
    ).fetchone()[0]
    conn.close()

    assert mismatched == 0


def test_postings_triggers_track_insert_update_delete():
    conn, _provider = _corpus()
    target_rowid = int(
        conn.execute("SELECT rowid FROM memories WHERE id='target'").fetchone()[0]
    )

    def posting_terms() -> set[str]:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT term FROM lexical_cjk_postings_v1 WHERE docid=?",
                (target_rowid,),
            ).fetchall()
        }

    assert "迁移" in posting_terms()

    conn.execute(
        "UPDATE memories SET content='全新缓存预热清单', summary='' WHERE id='target'"
    )
    updated_terms = posting_terms()
    assert "迁移" not in updated_terms
    assert "缓存" in updated_terms

    conn.execute("DELETE FROM memories WHERE id='target'")
    assert posting_terms() == set()
    conn.close()


def test_activation_and_rollback_switch_default_reads_without_deleting_shadow():
    conn, provider = _corpus()
    query = "生产库切换前需要做什么"

    before = observe_shadow_channel(conn, provider, query, generation=None)
    assert current_generation_id(conn) == ""
    assert before["bound_table"] == ""
    assert not shadow_index_is_independent(before, "target"), before

    activate_generation(
        conn,
        LEXICAL_GENERATION_ID,
        expected_current="",
    )
    conn.commit()
    active = observe_shadow_channel(conn, provider, query, generation=None)
    assert current_generation_id(conn) == LEXICAL_GENERATION_ID
    assert shadow_index_is_independent(active, "target"), active

    rollback_generation(
        conn,
        expected_current=LEXICAL_GENERATION_ID,
    )
    conn.commit()
    rolled = observe_shadow_channel(conn, provider, query, generation=None)
    assert current_generation_id(conn) == ""
    assert rolled["bound_table"] == ""
    assert not shadow_index_is_independent(rolled, "target"), rolled

    override = observe_shadow_channel(
        conn,
        provider,
        query,
        generation=LEXICAL_GENERATION_ID,
    )
    assert shadow_index_is_independent(override, "target"), override
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (LEXICAL_SHADOW_TABLE,),
    ).fetchone()
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
