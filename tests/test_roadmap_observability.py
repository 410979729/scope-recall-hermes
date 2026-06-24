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
        schemas = plugin.get_tool_schemas()
        names = {schema["name"] for schema in schemas}

        assert {"scope_recall_inspect", "scope_recall_explain", "scope_recall_benchmark"} <= names
        benchmark_schema = next(schema for schema in schemas if schema["name"] == "scope_recall_benchmark")
        properties = benchmark_schema["parameters"]["properties"]
        assert {variant["type"] for variant in properties["queries"]["anyOf"]} == {"array", "string"}
        case_properties = properties["cases"]["items"]["properties"]
        assert {variant["type"] for variant in case_properties["expected_ids"]["anyOf"]} == {"array", "string"}
        assert {variant["type"] for variant in case_properties["forbidden_ids"]["anyOf"]} == {"array", "string"}
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
        searched = json.loads(plugin.handle_tool_call("scope_recall_search", {"query": "Scope Recall explain components", "limit": 3}))

        assert explained["query"] == "Scope Recall explain components"
        assert explained["count"] >= 1
        assert searched["count"] >= 1
        assert explained["results"][0]["id"] == searched["results"][0]["id"]
        assert explained["results"][0]["score"] == searched["results"][0]["score"]
        assert explained["results"][0]["rank"] == 1
        component = explained["results"][0]["components"]
        assert {
            "lexical_score",
            "bm25_score",
            "vector_score",
            "rrf_score",
            "pre_quality_score",
            "quality_weight_applied",
            "entity_overlap_bonus",
            "entity_distance_score",
            "entity_distance_bonus",
            "relation_evidence_count",
            "relation_evidence_types",
            "relation_rerank_bonus",
            "base_score",
            "temporal_decay_multiplier",
            "temporal_decay_weight",
            "temporal_policy_class",
            "temporal_policy_weight",
            "recency_bonus",
            "final_score",
            "general_weight",
            "trust",
            "importance",
            "confidence",
            "min_score",
            "vector_only_min_score",
            "rejected_reason",
        } <= set(component)
        assert "rejected_candidates" in explained
    finally:
        plugin.shutdown()


def test_explain_reports_rejected_candidates_when_threshold_filters_hits(tmp_path):
    _write_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "retrieval": {"mode": "lexical", "min_score": 1.1, "candidate_pool": 10},
        },
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-observe-rejections",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    try:
        stored = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Scope Recall rejected candidate threshold sentinel.", "target": "memory"},
            )
        )
        explained = json.loads(plugin.handle_tool_call("scope_recall_explain", {"query": "Scope Recall rejected candidate threshold sentinel", "limit": 3}))

        assert explained["count"] == 0
        assert explained["rejected_count"] >= 1
        assert explained["rejected_candidates"][0]["id"] == stored["id"]
        assert explained["rejected_candidates"][0]["components"]["rejected_reason"] == "below_min_score"
    finally:
        plugin.shutdown()


def test_benchmark_reports_query_latencies_and_shared_pool_status(tmp_path):
    plugin = _provider(tmp_path)
    try:
        stored = json.loads(plugin.handle_tool_call("scope_recall_store", {"content": "Scope Recall benchmark query latency smoke.", "target": "memory"}))
        payload = json.loads(plugin.handle_tool_call("scope_recall_benchmark", {"queries": ["Scope Recall benchmark", "missing query"], "limit": 2}))
        passing_case = json.loads(
            plugin.handle_tool_call(
                "scope_recall_benchmark",
                {
                    "cases": [
                        {
                            "query": "Scope Recall benchmark",
                            "expected_ids": [stored["id"]],
                            "forbidden_ids": ["not-a-real-memory-id"],
                            "min_rank": 1,
                            "min_top_score": 0.1,
                        }
                    ],
                    "auto_explain_on_fail": True,
                    "limit": 2,
                },
            )
        )
        failing_case = json.loads(
            plugin.handle_tool_call(
                "scope_recall_benchmark",
                {
                    "cases": [{"query": "Scope Recall benchmark", "forbidden_ids": [stored["id"]]}],
                    "auto_explain_on_fail": True,
                    "limit": 2,
                },
            )
        )
        stats = json.loads(plugin.handle_tool_call("scope_recall_stats", {}))

        assert payload["query_count"] == 2
        assert all(result["latency_ms"] >= 0 for result in payload["results"])
        assert all("raw_top_score" in result for result in payload["results"])
        assert payload["results"][0]["top_score"] == round(payload["results"][0]["raw_top_score"], 4)
        assert passing_case["passed"] is True
        assert passing_case["failures"] == []
        assert passing_case["results"][0]["ids"][0] == stored["id"]
        assert failing_case["passed"] is False
        assert any("forbidden_id_present" in failure for failure in failing_case["failures"])
        assert "explain" in failing_case["results"][0]
        assert stats["shared_pool"]["enabled"] is True
        assert stats["shared_pool"]["scope_id"] in stats["accessible_scope_ids"]
    finally:
        plugin.shutdown()
