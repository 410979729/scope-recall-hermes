"""Tests for relation-aware recall ranking and evidence.

They ensure graph edges improve retrieval without bypassing lifecycle or scope filters."""

from __future__ import annotations

import json
import sqlite3
import threading

from plugins.memory import load_memory_provider

from scope_recall.graph import ensure_graph_schema
from scope_recall.models import RecallItem
from scope_recall.recall import RecallService


def _write_config(hermes_home, values):
    config_path = hermes_home / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(values, ensure_ascii=False) + "\n", encoding="utf-8")


class DummyProvider:
    def __init__(self, retrieval_config, items):
        self._retrieval_config = dict(retrieval_config)
        self._scope_id = "local-scope"
        self._shared_scope_id = "shared-scope"
        self._accessible_scope_ids = [self._scope_id, self._shared_scope_id]
        self._items = list(items)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, scope_id TEXT NOT NULL DEFAULT '', metadata TEXT NOT NULL DEFAULT '{}')")
        for item in self._items:
            self._conn.execute(
                "INSERT OR REPLACE INTO memories(id, scope_id, metadata) VALUES (?, ?, ?)",
                (item.id, str((item.metadata or {}).get("scope_id") or self._shared_scope_id), json.dumps(item.metadata or {}, ensure_ascii=False, sort_keys=True)),
            )
        ensure_graph_schema(self._conn)
        self._conn.commit()

    def _search_db_memories(self, query, *, limit):
        return self._items[:limit]

    def _search_vector_memories(self, query, *, limit):
        return []

    def _search_curated_memories(self, query):
        return []

    def _dedup_key(self, content):
        return str(content).lower()

    def _config_value(self, key, default):
        return default

    def _require_conn(self):
        return self._conn

    def close(self):
        self._conn.close()


def _item(memory_id: str, score: float) -> RecallItem:
    return RecallItem(
        id=memory_id,
        content=f"Project Atlas deploy command candidate {memory_id}.",
        summary=f"Project Atlas deploy command candidate {memory_id}.",
        source="tool-store",
        target="project",
        score=score,
        updated_at="2026-06-01T00:00:00+00:00",
        metadata={"lexical_score": score, "scope_id": "shared-scope", "memory_type": "project"},
    )


def test_explain_surfaces_persisted_contradiction_relations(tmp_path):
    _write_config(tmp_path, {"vector": {"enabled": False}, "retrieval": {"mode": "lexical", "min_score": 0.01}})
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-relation-explain",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    try:
        first = json.loads(plugin.handle_tool_call("scope_recall_store", {"content": "Joy prefers verbose progress reports.", "target": "user"}))
        second = json.loads(plugin.handle_tool_call("scope_recall_store", {"content": "Joy no longer prefers verbose progress reports.", "target": "user"}))

        explained = json.loads(plugin.handle_tool_call("scope_recall_explain", {"query": "Joy verbose progress reports", "limit": 5}))
        by_id = {row["id"]: row for row in explained["results"]}

        assert first["id"] in by_id
        assert second["id"] in by_id
        assert by_id[second["id"]]["components"]["relation_evidence_count"] >= 1
        assert "contradicts" in by_id[second["id"]]["components"]["relation_evidence_types"]
    finally:
        plugin.shutdown()


def test_relation_evidence_ignores_lifecycle_hidden_peers():
    active = _item("active-deploy-command", 0.82)
    provider = DummyProvider(
        {
            "mode": "lexical",
            "min_score": 0.01,
            "relation_rerank_enabled": True,
            "relation_supports_boost": 0.08,
        },
        [active],
    )
    try:
        provider._require_conn().execute(
            "INSERT OR REPLACE INTO memories(id, scope_id, metadata) VALUES ('archived-peer', 'shared-scope', ?)",
            (json.dumps({"lifecycle": "archived", "scope_id": "shared-scope"}, ensure_ascii=False, sort_keys=True),),
        )
        provider._require_conn().execute(
            """
            INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
            VALUES ('active-deploy-command', 'archived-peer', 'supports', 1.0, 'hidden peer test', '2026-06-01T00:00:00+00:00')
            """
        )
        provider._require_conn().commit()

        results = RecallService(provider).search_memories("Project Atlas deploy command", limit=1)

        assert [item.id for item in results] == ["active-deploy-command"]
        assert results[0].metadata["relation_evidence_count"] == 0
        assert results[0].metadata["relation_evidence_ids"] == []
        assert results[0].metadata["relation_rerank_bonus"] == 0.0
    finally:
        provider.close()


def test_relation_evidence_ignores_candidate_and_in_progress_peers_for_ordinary_recall():
    active = _item("active-deploy-command", 0.82)
    provider = DummyProvider(
        {
            "mode": "lexical",
            "min_score": 0.01,
            "relation_rerank_enabled": True,
            "relation_supports_boost": 0.08,
        },
        [active],
    )
    try:
        for peer_id, lifecycle in (("candidate-peer", "candidate"), ("progress-peer", "in_progress")):
            provider._require_conn().execute(
                "INSERT OR REPLACE INTO memories(id, scope_id, metadata) VALUES (?, 'shared-scope', ?)",
                (peer_id, json.dumps({"lifecycle": lifecycle, "scope_id": "shared-scope"}, ensure_ascii=False, sort_keys=True)),
            )
        provider._require_conn().execute(
            """
            INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
            VALUES ('candidate-peer', 'active-deploy-command', 'supports', 1.0, 'candidate peer test', '2026-06-01T00:00:00+00:00')
            """
        )
        provider._require_conn().execute(
            """
            INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
            VALUES ('active-deploy-command', 'progress-peer', 'depends_on', 1.0, 'progress peer test', '2026-06-01T00:00:00+00:00')
            """
        )
        provider._require_conn().commit()

        results = RecallService(provider).search_memories("Project Atlas deploy command", limit=1)

        assert [item.id for item in results] == ["active-deploy-command"]
        assert results[0].metadata["relation_evidence_count"] == 0
        assert results[0].metadata["relation_evidence_ids"] == []
        assert results[0].metadata["relation_rerank_bonus"] == 0.0
    finally:
        provider.close()


def test_relation_rerank_boosts_superseding_candidate_when_enabled():
    older = _item("older-deploy-command", 0.82)
    newer = _item("newer-deploy-command", 0.78)
    provider = DummyProvider(
        {
            "mode": "lexical",
            "min_score": 0.01,
            "relation_rerank_enabled": True,
            "relation_supersedes_boost": 0.08,
        },
        [older, newer],
    )
    try:
        provider._require_conn().execute(
            """
            INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
            VALUES (?, ?, 'supersedes', 1.0, 'test supersedes relation', '2026-06-01T00:00:00+00:00')
            """,
            ("newer-deploy-command", "older-deploy-command"),
        )
        provider._require_conn().commit()

        results = RecallService(provider).search_memories("Project Atlas deploy command", limit=2)

        assert [item.id for item in results] == ["newer-deploy-command", "older-deploy-command"]
        assert results[0].metadata["relation_rerank_bonus"] > 0.0
        assert "supersedes" in results[0].metadata["relation_evidence_types"]
    finally:
        provider.close()


def test_relation_rerank_gives_small_auxiliary_boost_for_typed_graph_edges():
    dependent = _item("atlas-depends-on-redis", 0.78)
    unrelated = _item("atlas-unrelated", 0.775)
    provider = DummyProvider(
        {
            "mode": "lexical",
            "min_score": 0.01,
            "relation_rerank_enabled": True,
            "relation_supports_boost": 0.04,
            "relation_same_topic_boost": 0.01,
        },
        [unrelated, dependent],
    )
    try:
        provider._require_conn().execute(
            "INSERT OR REPLACE INTO memories(id, scope_id, metadata) VALUES ('redis-runbook', 'shared-scope', ?)",
            (json.dumps({"scope_id": "shared-scope"}, ensure_ascii=False, sort_keys=True),),
        )
        provider._require_conn().execute(
            """
            INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
            VALUES (?, ?, 'depends_on', 1.0, 'typed relation test', '2026-06-01T00:00:00+00:00')
            """,
            ("atlas-depends-on-redis", "redis-runbook"),
        )
        provider._require_conn().commit()

        results = RecallService(provider).search_memories("Project Atlas deploy command", limit=2)

        assert [item.id for item in results] == ["atlas-depends-on-redis", "atlas-unrelated"]
        assert 0.0 < results[0].metadata["relation_rerank_bonus"] <= 0.04
        assert "depends_on" in results[0].metadata["relation_evidence_types"]
    finally:
        provider.close()


def test_relation_rerank_high_config_cannot_override_large_lexical_gap():
    dependent = _item("atlas-depends-on-redis", 0.50)
    stronger = _item("atlas-strong-lexical", 0.65)
    provider = DummyProvider(
        {
            "mode": "lexical",
            "min_score": 0.01,
            "relation_rerank_enabled": True,
            "relation_supports_boost": 0.5,
        },
        [stronger, dependent],
    )
    try:
        provider._require_conn().execute(
            "INSERT OR REPLACE INTO memories(id, scope_id, metadata) VALUES ('redis-runbook', 'shared-scope', ?)",
            (json.dumps({"scope_id": "shared-scope"}, ensure_ascii=False, sort_keys=True),),
        )
        provider._require_conn().execute(
            """
            INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
            VALUES (?, ?, 'depends_on', 1.0, 'typed relation high config test', '2026-06-01T00:00:00+00:00')
            """,
            ("atlas-depends-on-redis", "redis-runbook"),
        )
        provider._require_conn().commit()

        results = RecallService(provider).search_memories("Project Atlas deploy command", limit=2)
        by_id = {item.id: item for item in results}

        assert [item.id for item in results] == ["atlas-strong-lexical", "atlas-depends-on-redis"]
        assert by_id["atlas-depends-on-redis"].metadata["relation_rerank_bonus"] <= 0.08
    finally:
        provider.close()

def test_relation_rerank_default_off_ignores_supersedes_edges():
    older = _item("older-deploy-command", 0.82)
    newer = _item("newer-deploy-command", 0.78)
    provider = DummyProvider(
        {
            "mode": "lexical",
            "min_score": 0.01,
        },
        [older, newer],
    )
    try:
        provider._require_conn().execute(
            """
            INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
            VALUES (?, ?, 'supersedes', 1.0, 'test supersedes relation', '2026-06-01T00:00:00+00:00')
            """,
            ("newer-deploy-command", "older-deploy-command"),
        )
        provider._require_conn().commit()

        results = RecallService(provider).search_memories("Project Atlas deploy command", limit=2)
        by_id = {item.id: item for item in results}

        assert [item.id for item in results] == ["older-deploy-command", "newer-deploy-command"]
        assert by_id["older-deploy-command"].metadata["relation_rerank_bonus"] == 0.0
        assert by_id["newer-deploy-command"].metadata["relation_rerank_bonus"] == 0.0
    finally:
        provider.close()


def test_relation_rerank_penalizes_superseded_candidate_when_enabled():
    older = _item("older-deploy-command", 0.82)
    newer = _item("newer-deploy-command", 0.78)
    provider = DummyProvider(
        {
            "mode": "lexical",
            "min_score": 0.01,
            "relation_rerank_enabled": True,
            "relation_supersedes_boost": 0.08,
            "relation_superseded_penalty": 0.04,
        },
        [older, newer],
    )
    try:
        provider._require_conn().execute(
            """
            INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
            VALUES (?, ?, 'supersedes', 1.0, 'new command supersedes old command', '2026-06-01T00:00:00+00:00')
            """,
            ("newer-deploy-command", "older-deploy-command"),
        )
        provider._require_conn().commit()

        results = RecallService(provider).search_memories("Project Atlas deploy command", limit=2)
        by_id = {item.id: item for item in results}

        assert [item.id for item in results] == ["newer-deploy-command", "older-deploy-command"]
        assert by_id["newer-deploy-command"].metadata["relation_rerank_bonus"] > 0.0
        assert by_id["older-deploy-command"].metadata["relation_rerank_bonus"] < 0.0
        assert "supersedes" in by_id["older-deploy-command"].metadata["relation_evidence_types"]
    finally:
        provider.close()


def test_relation_rerank_respects_explicit_zero_superseded_penalty():
    older = _item("older-deploy-command", 0.82)
    newer = _item("newer-deploy-command", 0.78)
    provider = DummyProvider(
        {
            "mode": "lexical",
            "min_score": 0.01,
            "relation_rerank_enabled": True,
            "relation_supersedes_boost": 0.08,
            "relation_superseded_penalty": 0.0,
        },
        [older, newer],
    )
    try:
        provider._require_conn().execute(
            """
            INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
            VALUES (?, ?, 'supersedes', 1.0, 'new command supersedes old command', '2026-06-01T00:00:00+00:00')
            """,
            ("newer-deploy-command", "older-deploy-command"),
        )
        provider._require_conn().commit()

        results = RecallService(provider).search_memories("Project Atlas deploy command", limit=2)
        by_id = {item.id: item for item in results}

        assert by_id["newer-deploy-command"].metadata["relation_rerank_bonus"] > 0.0
        assert by_id["older-deploy-command"].metadata["relation_rerank_bonus"] == 0.0
    finally:
        provider.close()


def test_relation_inspect_and_explain_hide_inaccessible_relation_peers(tmp_path):
    _write_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "retrieval": {
                "mode": "lexical",
                "min_score": 0.01,
                "relation_rerank_enabled": True,
                "relation_supports_boost": 0.2,
            },
        },
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-relation-scope",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    try:
        visible = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Project Atlas deploy command is make deploy-atlas.", "target": "project"},
            )
        )
        visible_id = visible["id"]
        conn = plugin._require_conn()
        with plugin._lock:
            visible_scope_id = str(conn.execute("SELECT scope_id FROM memories WHERE id = ?", (visible_id,)).fetchone()["scope_id"])
            conn.execute(
                """
                INSERT INTO memories(id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key, agent_identity, agent_workspace, session_id, source, target, content, summary, metadata, created_at, updated_at)
                VALUES ('hidden-peer', 'other-scope', 'cli', 'someone-else', '', '', '', 'yuheng', 'hermes', 'foreign-session', 'tool-store', 'project', 'Hidden Project Atlas deploy secret.', 'Hidden Project Atlas deploy secret.', '{}', '2026-06-01T00:00:00+00:00', '2026-06-01T00:00:00+00:00')
                """
            )
            conn.execute(
                """
                INSERT INTO memories(id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key, agent_identity, agent_workspace, session_id, source, target, content, summary, metadata, created_at, updated_at)
                VALUES ('archived-peer', ?, 'cli', 'joy', '', '', '', 'yuheng', 'hermes', 'archived-session', 'tool-store', 'project', 'Archived Project Atlas deploy command.', 'Archived Project Atlas deploy command.', '{"lifecycle":"archived"}', '2026-06-01T00:00:00+00:00', '2026-06-01T00:00:00+00:00')
                """,
                (visible_scope_id,),
            )
            conn.execute(
                """
                INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
                VALUES (?, 'hidden-peer', 'supports', 1.0, 'cross-scope relation should not leak', '2026-06-01T00:00:00+00:00')
                """,
                (visible_id,),
            )
            conn.execute(
                """
                INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
                VALUES (?, 'deleted-peer', 'supports', 1.0, 'deleted relation peer should not leak', '2026-06-01T00:00:01+00:00')
                """,
                (visible_id,),
            )
            conn.execute(
                """
                INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
                VALUES (?, 'archived-peer', 'supports', 1.0, 'archived relation peer should not leak', '2026-06-01T00:00:02+00:00')
                """,
                (visible_id,),
            )
            conn.commit()

        inspected = json.loads(plugin.handle_tool_call("scope_recall_inspect", {"id": visible_id}))
        explained = json.loads(plugin.handle_tool_call("scope_recall_explain", {"query": "Project Atlas deploy command", "limit": 5}))
        by_id = {row["id"]: row for row in explained["results"]}

        assert inspected["relations"]["count"] == 0
        assert inspected["relations"]["items"] == []
        assert visible_id in by_id
        assert by_id[visible_id]["components"]["relation_evidence_count"] == 0
        assert by_id[visible_id]["components"]["relation_evidence_ids"] == []
        assert by_id[visible_id]["components"]["relation_rerank_bonus"] == 0.0
    finally:
        plugin.shutdown()
