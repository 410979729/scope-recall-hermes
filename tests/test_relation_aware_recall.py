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
