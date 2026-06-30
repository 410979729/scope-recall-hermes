"""Tests for curated, SQLite, and vector storage read views.

They ensure lifecycle and scope filters are applied before recall merges candidates."""

from __future__ import annotations

import sqlite3

from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.storage_views import search_db_memories


class FakeProvider:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = __import__("threading").RLock()
        self._scope_id = "local-scope"
        self._shared_scope_id = "shared-scope"
        self._accessible_scope_ids = [self._scope_id, self._shared_scope_id]
        self._retrieval_config = {"candidate_pool": 12, "min_score": 0.18}

    def _require_conn(self) -> sqlite3.Connection:
        return self._conn

    def _config_value(self, key: str, default):
        return default


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _store(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    content: str,
    target: str = "ops",
    source: str = "tool-store",
    scope_id: str = "shared-scope",
) -> None:
    store_row(
        conn,
        memory_id=memory_id,
        scope_id=scope_id,
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="session",
        source=source,
        target=target,
        content=content,
    )


def _set_updated_at(conn: sqlite3.Connection, memory_id: str, updated_at: str) -> None:
    conn.execute("UPDATE memories SET updated_at = ? WHERE id = ?", (updated_at, memory_id))
    conn.commit()


def test_search_db_memories_does_not_backfill_unrelated_recent_durable_rows():
    conn = _conn()
    _store(
        conn,
        memory_id="ops-openclaw",
        content=(
            "OpenClaw sibling upgrade pitfall on home-yu-0001: even when 天璇/天权 "
            "systemd ExecStart uses instance-local OpenClaw, gateway/plugin CLI fallbacks "
            "may still resolve stale /usr/local/bin/openclaw."
        ),
    )
    provider = FakeProvider(conn)

    results = search_db_memories(provider, "普通无关对话测试：今天午饭吃什么比较好", limit=5)

    assert results == []


def test_search_db_memories_keeps_relevant_lexical_hits():
    conn = _conn()
    _store(
        conn,
        memory_id="ops-openclaw",
        content="OpenClaw gateway should set OPENCLAW_CLI_BIN for 天璇 and 天权.",
    )
    provider = FakeProvider(conn)

    results = search_db_memories(provider, "OpenClaw gateway 天璇", limit=5)

    assert [item.id for item in results] == ["ops-openclaw"]


def test_search_db_memories_finds_alias_expanded_lexical_hits_without_recent_backfill():
    conn = _conn()
    _store(
        conn,
        memory_id="user-reply-style",
        content="User prefers warm, concise replies when discussing production rollouts.",
        target="user",
    )
    provider = FakeProvider(conn)

    results = search_db_memories(provider, "response style", limit=5)

    assert [item.id for item in results] == ["user-reply-style"]


def test_fts_candidates_use_bm25_before_recency_cutoff():
    conn = _conn()
    _store(
        conn,
        memory_id="old-exact",
        content="Scope Recall BM25 ranking chooses strong lexical matches before recency.",
    )
    _set_updated_at(conn, "old-exact", "2025-01-01T00:00:00+00:00")
    for idx in range(3):
        memory_id = f"new-weak-{idx}"
        _store(
            conn,
            memory_id=memory_id,
            content=f"Scope unrelated newest chatter {idx}.",
        )
        _set_updated_at(conn, memory_id, f"2026-01-0{idx + 1}T00:00:00+00:00")
    provider = FakeProvider(conn)
    provider._retrieval_config["candidate_pool"] = 2

    results = search_db_memories(provider, "Scope Recall BM25 ranking", limit=1)

    assert "old-exact" in [item.id for item in results]
    exact = next(item for item in results if item.id == "old-exact")
    assert exact.metadata is not None
    assert "bm25_score" in exact.metadata
