"""Regression tests for golden benchmark assertions, explain snapshots, funnel traces, and retrieval limits.

They ensure benchmark failures remain actionable instead of just reporting a vague recall miss."""

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
            "retrieval": {"mode": "lexical", "min_score": 0.0, "include_general": "same-scope"},
        },
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-benchmark-regression",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    return plugin


def test_benchmark_expected_and_forbidden_ids_guard_retrieval_regression(tmp_path):
    plugin = _provider(tmp_path)
    try:
        durable = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Project Atlas production deploy command is uv run atlas-server.",
                    "target": "project",
                    "memory_type": "procedure",
                },
            )
        )
        scratch = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Temporary dinner preference note: Joy likes warm soup tonight.",
                    "target": "general",
                    "memory_type": "episodic",
                },
            )
        )

        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_benchmark",
                {
                    "cases": [
                        {
                            "query": "Project Atlas production deploy command",
                            "expected_ids": [durable["id"]],
                            "forbidden_ids": [scratch["id"]],
                            "min_rank": 1,
                            "min_top_score": 0.1,
                        }
                    ],
                    "auto_explain_on_fail": True,
                    "limit": 3,
                },
            )
        )

        assert payload["passed"] is True
        assert payload["failures"] == []
        assert payload["results"][0]["passed"] is True
        assert payload["results"][0]["ids"][0] == durable["id"]
        assert scratch["id"] not in payload["results"][0]["ids"]
    finally:
        plugin.shutdown()


def test_benchmark_expected_metadata_guards_recall_signals(tmp_path):
    plugin = _provider(tmp_path)
    try:
        stored = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Project Northstar API base URL is https://api.northstar.example/v2.",
                    "target": "project",
                    "memory_type": "factual",
                    "entities": ["Northstar"],
                },
            )
        )

        passing = json.loads(
            plugin.handle_tool_call(
                "scope_recall_benchmark",
                {
                    "cases": [
                        {
                            "query": "Project Northstar API base URL",
                            "expected_ids": [stored["id"]],
                            "expected_metadata": {stored["id"]: {"memory_type": "factual"}},
                            "min_rank": 1,
                        }
                    ],
                    "limit": 3,
                },
            )
        )
        failing = json.loads(
            plugin.handle_tool_call(
                "scope_recall_benchmark",
                {
                    "cases": [
                        {
                            "query": "Project Northstar API base URL",
                            "expected_ids": [stored["id"]],
                            "expected_metadata": {stored["id"]: {"memory_type": "procedure"}},
                            "min_rank": 1,
                        }
                    ],
                    "limit": 3,
                },
            )
        )

        assert passing["passed"] is True
        assert failing["passed"] is False
        assert any("metadata_mismatch" in failure for failure in failing["failures"])
    finally:
        plugin.shutdown()


def test_benchmark_failure_case_exports_explain_snapshot(tmp_path):
    plugin = _provider(tmp_path)
    try:
        stored = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Project Atlas rollback command is uv run atlas-rollback.",
                    "target": "project",
                    "memory_type": "procedure",
                },
            )
        )

        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_benchmark",
                {
                    "cases": [
                        {
                            "query": "Project Atlas rollback command",
                            "expected_ids": ["missing-memory-id"],
                            "forbidden_ids": [stored["id"]],
                            "min_rank": 1,
                        }
                    ],
                    "auto_explain_on_fail": True,
                    "limit": 3,
                },
            )
        )

        assert payload["passed"] is False
        assert any("expected_id_missing:missing-memory-id" in failure for failure in payload["failures"])
        assert any(f"forbidden_id_present:{stored['id']}" in failure for failure in payload["failures"])
        assert payload["results"][0]["passed"] is False
        assert "explain" in payload["results"][0]
        assert payload["results"][0]["explain"]["results"][0]["id"] == stored["id"]
    finally:
        plugin.shutdown()



def test_search_explain_and_benchmark_include_recall_funnel_trace(tmp_path):
    plugin = _provider(tmp_path)
    try:
        stored = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Project Orion release checklist requires uv run pytest before deploy.",
                    "target": "project",
                    "memory_type": "procedure",
                    "entities": ["Project Orion"],
                },
            )
        )

        search_payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_search",
                {"query": "Project Orion release checklist", "limit": 3, "include_trace": True},
            )
        )
        trace = search_payload["funnel_trace"]
        assert trace["query"] == "Project Orion release checklist"
        assert trace["limit"] == 3
        assert trace["candidate_pool"] >= 3
        assert trace["vector_top_k"] >= trace["candidate_pool"]
        assert trace["accessible_scope_count"] >= 1
        assert trace["stages"]["lexical"]["count"] >= 1
        assert trace["stages"]["merge"]["output_count"] >= 1
        assert trace["final"]["returned_count"] >= 1
        assert stored["id"] in trace["final"]["returned_ids"]
        assert "timings_ms" in trace and "total" in trace["timings_ms"]

        explain_payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_explain",
                {"query": "Project Orion release checklist", "limit": 3},
            )
        )
        assert explain_payload["funnel_trace"]["final"]["returned_count"] >= 1

        benchmark_payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_benchmark",
                {
                    "cases": [
                        {
                            "query": "Project Orion release checklist",
                            "expected_ids": [stored["id"]],
                            "min_rank": 1,
                        }
                    ],
                    "limit": 3,
                    "include_trace": True,
                    "prompt_budget_chars": 1000,
                },
            )
        )
        assert benchmark_payload["passed"] is True
        assert benchmark_payload["metrics"]["known_answer_recall"] == 1.0
        assert benchmark_payload["metrics"]["top_k_accuracy"] == 1.0
        assert benchmark_payload["metrics"]["latency_ms_p50"] >= 0.0
        assert benchmark_payload["metrics"]["prompt_budget_hit_rate"] == 1.0
        assert "filter_counts" in benchmark_payload["metrics"]
        assert benchmark_payload["results"][0]["funnel_trace"]["final"]["returned_count"] >= 1
    finally:
        plugin.shutdown()


def test_retrieval_top_k_config_controls_default_tool_limit(tmp_path):
    _write_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "retrieval": {"mode": "lexical", "min_score": 0.0, "include_general": "same-scope", "top_k": 2, "candidate_pool": 6},
        },
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-top-k-default",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    try:
        for index in range(4):
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": f"Project Vega runbook step {index}: use command vega-{index} for deploy.",
                    "target": "memory",
                    "memory_type": "procedure",
                },
            )
        payload = json.loads(plugin.handle_tool_call("scope_recall_search", {"query": "Project Vega runbook deploy", "include_trace": True}))
        assert payload["count"] == 2
        assert payload["funnel_trace"]["configured_top_k"] == 2
        assert payload["funnel_trace"]["candidate_pool"] == 6

        explicit = json.loads(plugin.handle_tool_call("scope_recall_search", {"query": "Project Vega runbook deploy", "limit": 3}))
        assert explicit["count"] == 3
    finally:
        plugin.shutdown()
