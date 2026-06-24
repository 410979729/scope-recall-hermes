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
