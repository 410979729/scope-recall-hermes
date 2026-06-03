from __future__ import annotations

import json

from plugins.memory import load_memory_provider


def _write_config(hermes_home, values):
    config_path = hermes_home / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(values, ensure_ascii=False) + "\n", encoding="utf-8")


def _provider(tmp_path):
    _write_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "shared_pool": {"enabled": True, "pool_id": "beidou"},
            "retrieval": {"mode": "lexical", "min_score": 0.18, "temporal_decay_enabled": True},
        },
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-observe",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    return plugin


def test_inspect_explain_and_benchmark_tools_are_registered(tmp_path):
    plugin = _provider(tmp_path)
    try:
        names = {schema["name"] for schema in plugin.get_tool_schemas()}

        assert {"scope_recall_inspect", "scope_recall_explain", "scope_recall_benchmark"} <= names
    finally:
        plugin.shutdown()


def test_inspect_returns_row_metadata_feedback_and_relations(tmp_path):
    plugin = _provider(tmp_path)
    try:
        stored = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Scope Recall inspector returns row metadata and relation evidence.", "target": "memory"},
            )
        )
        feedback = json.loads(plugin.handle_tool_call("scope_recall_feedback", {"id": stored["id"], "rating": "helpful", "note": "inspection smoke"}))
        inspected = json.loads(plugin.handle_tool_call("scope_recall_inspect", {"id": stored["id"]}))

        assert feedback["updated"] is True
        assert inspected["found"] is True
        assert inspected["memory"]["id"] == stored["id"]
        assert inspected["feedback"]["count"] == 1
        assert "metadata" in inspected["memory"]
        assert "relations" in inspected
    finally:
        plugin.shutdown()


def test_explain_reports_component_scores_and_decay(tmp_path):
    plugin = _provider(tmp_path)
    try:
        json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Scope Recall explain tool shows BM25 lexical vector decay trust components.", "target": "memory"},
            )
        )
        explained = json.loads(plugin.handle_tool_call("scope_recall_explain", {"query": "Scope Recall explain components", "limit": 3}))

        assert explained["query"] == "Scope Recall explain components"
        assert explained["count"] >= 1
        component = explained["results"][0]["components"]
        assert {"lexical_score", "bm25_score", "vector_score", "base_score", "temporal_decay_multiplier", "trust"} <= set(component)
    finally:
        plugin.shutdown()


def test_benchmark_reports_query_latencies_and_shared_pool_status(tmp_path):
    plugin = _provider(tmp_path)
    try:
        json.loads(plugin.handle_tool_call("scope_recall_store", {"content": "Scope Recall benchmark query latency smoke.", "target": "memory"}))
        payload = json.loads(plugin.handle_tool_call("scope_recall_benchmark", {"queries": ["Scope Recall benchmark", "missing query"], "limit": 2}))
        stats = json.loads(plugin.handle_tool_call("scope_recall_stats", {}))

        assert payload["query_count"] == 2
        assert all(result["latency_ms"] >= 0 for result in payload["results"])
        assert stats["shared_pool"]["enabled"] is True
        assert stats["shared_pool"]["scope_id"] in stats["accessible_scope_ids"]
    finally:
        plugin.shutdown()
