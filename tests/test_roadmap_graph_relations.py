"""Roadmap regression tests for graph relation features.

They keep planned relation behavior executable rather than only documented."""

from __future__ import annotations

import builtins
import json
import sqlite3

from plugins.memory import load_memory_provider

from scope_recall.governance import classify_memory
from scope_recall.graph import extract_entities
from scope_recall.models import RuntimeScope
from scope_recall.scope import build_shared_pool_scope_id
from scope_recall.sql_store import ensure_schema


def _write_config(hermes_home, values):
    config_path = hermes_home / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(values, ensure_ascii=False) + "\n", encoding="utf-8")


def test_jieba_entity_extraction_keeps_compound_chinese_terms():
    entities = extract_entities("fcitx5 配置使用自然码双拼，scope-recall 路线图包含中文实体增强。")

    assert "自然码" in entities
    assert "双拼" in entities
    assert "scope-recall" in entities


def test_chinese_compound_entity_extraction_falls_back_when_jieba_is_unavailable(monkeypatch):
    original_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if str(name).startswith("jieba"):
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)

    entities = extract_entities("fcitx5 配置使用自然码双拼，scope-recall 路线图包含中文实体增强。")

    assert "自然码" in entities
    assert "双拼" in entities
    assert "scope-recall" in entities


def test_source_trust_priors_distinguish_curated_user_tool_and_raw_assistant_sources():
    curated = classify_memory("Joy prefers direct concise answers.", "user", "builtin-curated")
    tool = classify_memory("Joy prefers direct concise answers.", "user", "tool-store")
    assistant = classify_memory("Joy prefers direct concise answers.", "user", "turn-assistant")

    assert curated["source_trust"] > tool["source_trust"] > assistant["source_trust"]
    assert curated["trust"] >= curated["source_trust"]
    assert assistant["trust"] < tool["trust"]


def test_conflicting_memory_store_marks_contradiction_relation_for_review(tmp_path):
    _write_config(tmp_path, {"vector": {"enabled": False}, "retrieval": {"mode": "lexical", "min_score": 0.18}})
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-conflict",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    try:
        first = json.loads(plugin.handle_tool_call("scope_recall_store", {"content": "Joy prefers verbose progress reports.", "target": "user"}))
        second = json.loads(plugin.handle_tool_call("scope_recall_store", {"content": "Joy no longer prefers verbose progress reports.", "target": "user"}))

        assert first["stored"] is True
        assert second["stored"] is True
        with plugin._lock:
            relation = plugin._require_conn().execute(
                "SELECT relation_type, source_memory_id, target_memory_id FROM memory_relations WHERE source_memory_id = ? AND target_memory_id = ?",
                (second["id"], first["id"]),
            ).fetchone()
            reverse_relation = plugin._require_conn().execute(
                "SELECT relation_type, source_memory_id, target_memory_id FROM memory_relations WHERE source_memory_id = ? AND target_memory_id = ?",
                (first["id"], second["id"]),
            ).fetchone()
            feedback_count = plugin._require_conn().execute(
                "SELECT COUNT(*) FROM memory_feedback WHERE memory_id = ? AND note LIKE '%conflict%'",
                (second["id"],),
            ).fetchone()[0]
            metadata = json.loads(
                plugin._require_conn().execute("SELECT metadata FROM memories WHERE id = ?", (second["id"],)).fetchone()["metadata"]
            )

        assert relation is not None
        assert reverse_relation is not None
        assert relation["relation_type"] == "contradicts"
        assert reverse_relation["relation_type"] == "contradicts"
        assert feedback_count == 0
        assert metadata["conflict_count"] >= 1
        assert metadata["conflict_review_count"] >= 1
        assert metadata["needs_conflict_review"] is True
        assert first["id"] in metadata["conflict_review_ids"]
        assert "contradicts" in metadata["relation_types"]
    finally:
        plugin.shutdown()


def test_shared_pool_scope_id_is_stable_across_agent_identities():
    yuheng = RuntimeScope(platform="telegram", user_id="joy", agent_workspace="hermes", agent_identity="yuheng")
    tianshu = RuntimeScope(platform="telegram", user_id="joy", agent_workspace="hermes", agent_identity="tianshu")

    assert build_shared_pool_scope_id(yuheng, "beidou") == build_shared_pool_scope_id(tianshu, "beidou")


def test_graph_schema_creates_typed_relation_table():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_relations)").fetchall()}

    assert {"source_memory_id", "target_memory_id", "relation_type", "confidence", "note", "created_at"} <= columns
