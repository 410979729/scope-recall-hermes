"""Tests for curated, SQLite, and vector storage read views.

They ensure lifecycle and scope filters are applied before recall merges candidates."""

from __future__ import annotations

import json
import sqlite3

from scope_recall.lexical_generation import (
    LEXICAL_GENERATION_ID,
    LEXICAL_SHADOW_TABLE,
    activate_generation,
)
from scope_recall.lexical_migration import build_lexical_generation
from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.storage_views import (
    search_curated_memories,
    search_db_memories,
    search_vector_memories,
    search_vector_memories_with_vector,
)


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
    metadata: dict[str, object] | None = None,
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
        metadata=metadata,
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


def test_search_db_memories_allows_exact_opaque_memory_identifier():
    conn = _conn()
    _store(
        conn,
        memory_id="c799ccd3",
        content="The release candidate passed the independent audit.",
    )
    provider = FakeProvider(conn)

    results = search_db_memories(provider, "c799ccd3", limit=5)

    assert [item.id for item in results] == ["c799ccd3"]
    assert results[0].metadata["exact_identifier_evidence"] is True


def test_exact_identifier_cannot_bypass_candidate_lifecycle():
    conn = _conn()
    _store(
        conn,
        memory_id="XAS-OPS-001",
        content="XAS-OPS-001 awaits explicit candidate review.",
        source="event-digest",
        metadata={"lifecycle": "candidate", "event_digest": True},
    )
    provider = FakeProvider(conn)

    assert search_db_memories(provider, "XAS-OPS-001", limit=5) == []


def test_indexed_lexical_hits_do_not_fall_through_to_leading_wildcard_like():
    conn = _conn()
    for index in range(2):
        _store(
            conn,
            memory_id=f"indexed-orion-{index}",
            content=f"Project Orion deployment checklist item {index}.",
        )
    provider = FakeProvider(conn)
    provider._retrieval_config["candidate_pool"] = 2
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    results = search_db_memories(provider, "Project Orion deployment", limit=1)

    assert results
    assert not any(" LIKE '%" in statement.upper() for statement in statements)


def test_leading_wildcard_like_remains_a_bounded_compatibility_fallback():
    conn = _conn()
    _store(
        conn,
        memory_id="substring-only",
        content="The deployment codename is preORIONpost and remains searchable.",
    )
    provider = FakeProvider(conn)
    provider._retrieval_config["candidate_pool"] = 2
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    results = search_db_memories(provider, "ORION", limit=1)

    assert [item.id for item in results] == ["substring-only"]
    assert any(" LIKE '%ORION%'" in statement.upper() for statement in statements)


def test_leading_wildcard_like_miss_scans_only_the_bounded_recent_window():
    conn = _conn()
    _store(
        conn,
        memory_id="old-substring-only",
        content="The legacy deployment codename is preORIONpost.",
    )
    _set_updated_at(conn, "old-substring-only", "2020-01-01T00:00:00+00:00")
    for index in range(8):
        memory_id = f"recent-unrelated-{index}"
        _store(
            conn,
            memory_id=memory_id,
            content=f"Recent unrelated deployment note {index}.",
        )
        _set_updated_at(
            conn,
            memory_id,
            f"2026-01-{index + 1:02d}T00:00:00+00:00",
        )
    provider = FakeProvider(conn)
    provider._retrieval_config["candidate_pool"] = 2
    provider._retrieval_config["like_fallback_scan_limit"] = 4

    results = search_db_memories(provider, "ORION", limit=1)

    assert results == []


def test_ready_unactivated_trigram_does_not_change_ordinary_search():
    conn = _conn()
    _store(
        conn,
        memory_id="old-ready-not-active",
        content="The legacy deployment codename is preORIONpost.",
    )
    _set_updated_at(conn, "old-ready-not-active", "2020-01-01T00:00:00+00:00")
    for index in range(8):
        memory_id = f"recent-ready-unrelated-{index}"
        _store(
            conn,
            memory_id=memory_id,
            content=f"Recent unrelated deployment note {index}.",
        )
        _set_updated_at(
            conn,
            memory_id,
            f"2026-01-{index + 1:02d}T00:00:00+00:00",
        )
    built = build_lexical_generation(
        conn,
        LEXICAL_GENERATION_ID,
        batch_size=16,
        sample_limit=4,
    )
    assert built["status"] == "ready"
    provider = FakeProvider(conn)
    provider._retrieval_config["candidate_pool"] = 2
    provider._retrieval_config["like_fallback_scan_limit"] = 4
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    results = search_db_memories(provider, "ORION", limit=1)
    conn.set_trace_callback(None)

    assert results == []
    assert not any(
        LEXICAL_SHADOW_TABLE.upper() in statement.upper()
        and " MATCH '" in statement.upper()
        for statement in statements
    )


def test_active_reviewed_trigram_finds_old_substring_beyond_97_newer_rows():
    conn = _conn()
    _store(
        conn,
        memory_id="old-substring-only",
        content="The legacy deployment codename is preORIONpost.",
    )
    _set_updated_at(conn, "old-substring-only", "2020-01-01T00:00:00+00:00")
    for index in range(97):
        memory_id = f"recent-unrelated-{index}"
        _store(
            conn,
            memory_id=memory_id,
            content=f"Recent unrelated deployment note {index}.",
        )
        _set_updated_at(conn, memory_id, "2026-01-01T00:00:00+00:00")
    built = build_lexical_generation(
        conn,
        LEXICAL_GENERATION_ID,
        batch_size=128,
        sample_limit=4,
    )
    assert built["status"] == "ready"
    activated = activate_generation(
        conn,
        LEXICAL_GENERATION_ID,
        expected_current="",
    )
    conn.commit()
    assert activated["status"] == "active"
    provider = FakeProvider(conn)
    provider._retrieval_config["candidate_pool"] = 2
    provider._retrieval_config["like_fallback_scan_limit"] = 64
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    results = search_db_memories(provider, "ORION", limit=1)
    conn.set_trace_callback(None)

    assert [item.id for item in results] == ["old-substring-only"]
    indexed_statements = [
        statement.upper()
        for statement in statements
        if LEXICAL_SHADOW_TABLE.upper() in statement.upper()
        and " MATCH '" in statement.upper()
    ]
    assert indexed_statements
    assert all(
        "INDEXED BY IDX_SCOPE_RECALL_SCOPE_UPDATED" in statement.upper()
        for statement in statements
        if "LIKE '%ORION%'" in statement.upper()
    )


def test_search_curated_memories_rejects_source_prior_only_noise(tmp_path):
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    (memories_dir / "USER.md").write_text(
        "User prefers concise Chinese release reports.\n",
        encoding="utf-8",
    )
    provider = FakeProvider(_conn())
    provider._config = {}
    provider._scope = type("Scope", (), {"user_id": ""})()
    provider._hermes_home = tmp_path

    results = search_curated_memories(provider, "quasar xylophone 7f3a9c")

    assert results == []


def test_search_curated_memories_keeps_relevant_prior_ranked_hits(tmp_path):
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    (memories_dir / "USER.md").write_text(
        "User prefers concise Chinese release reports.\n",
        encoding="utf-8",
    )
    provider = FakeProvider(_conn())
    provider._config = {}
    provider._scope = type("Scope", (), {"user_id": ""})()
    provider._hermes_home = tmp_path

    results = search_curated_memories(provider, "concise release reports")

    assert len(results) == 1
    assert results[0].source == "builtin-curated"
    assert results[0].score >= 0.18


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


def test_search_db_memories_respects_limit_after_final_scoring():
    conn = _conn()
    for idx in range(8):
        _store(
            conn,
            memory_id=f"candidate-{idx}",
            content=f"OpenClaw gateway candidate {idx} uses explicit rollout validation.",
        )
    provider = FakeProvider(conn)

    results = search_db_memories(provider, "OpenClaw gateway rollout", limit=3)

    assert len(results) == 3
    assert [item.score for item in results] == sorted(
        (item.score for item in results), reverse=True
    )


def test_search_vector_memories_respects_requested_limit():
    conn = _conn()
    for idx in range(8):
        _store(
            conn,
            memory_id=f"vector-{idx}",
            content=f"vector candidate {idx}",
        )
    provider = FakeProvider(conn)
    provider._vector_ready = True
    provider._embedder = type(
        "Embedder",
        (),
        {"embed_query": staticmethod(lambda _query: [1.0, 0.0])},
    )()
    truth_rows = {
        memory_id: _truth_vector_row(conn, memory_id)
        for memory_id in (f"vector-{idx}" for idx in range(8))
    }

    class VectorStore:
        @staticmethod
        def search(_vector, *, scope_id: str, limit: int):
            assert limit == 20
            rows = [
                {
                    **truth_rows[f"vector-{idx}"],
                    "scope_id": scope_id,
                    "_distance": float(idx) / 100.0,
                }
                for idx in range(8)
            ]
            rows.append(
                {
                    **truth_rows["vector-0"],
                    "scope_id": scope_id,
                    "_distance": 0.99,
                }
            )
            return rows

    provider._vector_store = VectorStore()
    provider._vector_config = {"top_k": 20}
    provider._retrieval_config["vector_min_score"] = 0.0

    results = search_vector_memories(provider, "vector candidate", limit=1)

    assert len(results) == 1
    assert results[0].id == "vector-0"


def test_transient_query_embedding_failure_recovers_without_vector_repair(monkeypatch):
    conn = _conn()
    _store(conn, memory_id="recovering-vector", content="recovering vector truth")
    truth = conn.execute(
        "SELECT id, scope_id, content, summary, source, target, updated_at "
        "FROM memories WHERE id = 'recovering-vector'"
    ).fetchone()
    provider = FakeProvider(conn)
    vector_row = {key: truth[key] for key in truth.keys()}
    vector_row["_distance"] = 0.0
    _attach_vector_rows(provider, [vector_row])

    class RecoveringEmbedder:
        def __init__(self):
            self.calls = 0

        def embed_query(self, _query):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("temporary provider timeout")
            return [1.0, 0.0]

    embedder = RecoveringEmbedder()
    provider._embedder = embedder
    clock = [100.0]
    monkeypatch.setattr(
        "scope_recall.storage_views.time.monotonic",
        lambda: clock[0],
    )

    assert search_vector_memories(provider, "recover", limit=5) == []
    assert provider._vector_ready is True
    assert getattr(provider, "_vector_status", "ready") != "needs_repair"
    clock[0] = 100.5
    assert search_vector_memories(provider, "recover", limit=5) == []
    assert embedder.calls == 1
    clock[0] = 101.1
    recovered = search_vector_memories(provider, "recover", limit=5)

    assert [item.id for item in recovered] == ["recovering-vector"]
    assert embedder.calls == 2
    assert provider._vector_ready is True
    assert provider._vector_query_failure_count == 0
    assert provider._vector_query_last_error == ""


def _attach_vector_rows(provider: FakeProvider, rows: list[dict[str, object]]) -> None:
    """Attach a vector companion whose rows may intentionally disagree with truth."""

    class VectorStore:
        @staticmethod
        def search(_vector, *, scope_id: str, limit: int):
            del scope_id
            return [dict(row) for row in rows[:limit]]

    provider._vector_ready = True
    provider._vector_store = VectorStore()
    provider._vector_config = {"top_k": 20}
    provider._retrieval_config["vector_min_score"] = 0.0


def _vector_row(memory_id: str, *, scope_id: str = "shared-scope") -> dict[str, object]:
    return {
        "id": memory_id,
        "scope_id": scope_id,
        "content": "stale vector content",
        "summary": "stale vector summary",
        "source": "stale-vector-source",
        "target": "stale-vector-target",
        "updated_at": "2025-01-01T00:00:00+00:00",
        "_distance": 0.01,
    }


def _truth_vector_row(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    distance: float = 0.01,
) -> dict[str, object]:
    truth = conn.execute(
        "SELECT id, scope_id, content, summary, source, target, updated_at "
        "FROM memories WHERE id = ?",
        (memory_id,),
    ).fetchone()
    assert truth is not None
    row = {key: truth[key] for key in truth.keys()}
    row["_distance"] = distance
    return row


def test_vector_hit_rehydrates_all_output_fields_from_sqlite_truth():
    conn = _conn()
    _store(
        conn,
        memory_id="truth-newer",
        content="authoritative SQLite truth content",
        source="truth-source",
        target="ops",
    )
    conn.execute(
        "UPDATE memories SET summary = ?, updated_at = ? WHERE id = ?",
        ("authoritative truth summary", "2026-08-24T12:00:00+00:00", "truth-newer"),
    )
    conn.commit()
    provider = FakeProvider(conn)
    _attach_vector_rows(provider, [_truth_vector_row(conn, "truth-newer")])

    results = search_vector_memories_with_vector(provider, [1.0, 0.0], limit=5)

    assert len(results) == 1
    item = results[0]
    assert item.content == "authoritative SQLite truth content"
    assert item.summary == "authoritative truth summary"
    assert item.source == "truth-source"
    assert item.target == "ops"
    assert item.updated_at == "2026-08-24T12:00:00+00:00"
    assert item.metadata is not None
    assert item.metadata["scope_id"] == "shared-scope"
    assert provider._vector_ready is True


def test_stale_vector_companion_cannot_score_current_truth_revision():
    conn = _conn()
    _store(
        conn,
        memory_id="truth-revised",
        content="authoritative revised truth content",
        source="truth-source",
        target="ops",
    )
    provider = FakeProvider(conn)
    _attach_vector_rows(provider, [_vector_row("truth-revised")])

    results = search_vector_memories_with_vector(provider, [1.0, 0.0], limit=5)

    assert results == []
    assert provider._vector_ready is False
    assert provider._vector_status == "needs_repair"


def test_vector_hit_cannot_spoof_an_accessible_scope_over_forbidden_truth():
    conn = _conn()
    _store(
        conn,
        memory_id="forbidden-truth",
        content="forbidden SQLite truth",
        scope_id="forbidden-scope",
    )
    provider = FakeProvider(conn)
    _attach_vector_rows(
        provider,
        [_vector_row("forbidden-truth", scope_id="shared-scope")],
    )

    results = search_vector_memories_with_vector(provider, [1.0, 0.0], limit=5)

    assert results == []


def test_vector_hit_cannot_surface_truth_hidden_by_lifecycle():
    conn = _conn()
    _store(conn, memory_id="archived-truth", content="archived truth")
    _store(conn, memory_id="scratch-durable", content="durable scratch truth")
    for memory_id, lifecycle in (
        ("archived-truth", "archived"),
        ("scratch-durable", "scratch"),
    ):
        row = conn.execute(
            "SELECT metadata FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        metadata = json.loads(str(row[0] or "{}"))
        metadata["lifecycle"] = lifecycle
        conn.execute(
            "UPDATE memories SET metadata = ? WHERE id = ?",
            (json.dumps(metadata, sort_keys=True), memory_id),
        )
    conn.commit()
    provider = FakeProvider(conn)
    _attach_vector_rows(
        provider,
        [_vector_row("archived-truth"), _vector_row("scratch-durable")],
    )
    results = search_vector_memories_with_vector(provider, [1.0, 0.0], limit=5)

    assert results == []


def test_vector_truth_rehydration_chunks_under_live_sqlite_parameter_limit():
    conn = _conn()
    memory_ids = [f"chunked-truth-{idx:02d}" for idx in range(37)]
    for memory_id in memory_ids:
        _store(conn, memory_id=memory_id, content=f"truth for {memory_id}")
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 12)
    provider = FakeProvider(conn)
    rows = [_truth_vector_row(conn, memory_id) for memory_id in memory_ids]
    _attach_vector_rows(provider, rows)
    provider._vector_config["top_k"] = len(rows)

    results = search_vector_memories_with_vector(
        provider,
        [1.0, 0.0],
        limit=len(rows),
    )

    assert {item.id for item in results} == set(memory_ids)
    assert all(item.content == f"truth for {item.id}" for item in results)
