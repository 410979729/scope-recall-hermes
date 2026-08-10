"""Runtime validation tests for the public Scope Recall tool schemas.

Schema declarations are executable contracts, not prompt-only documentation.
Invalid values must be rejected before handlers can coerce or persist them, and
validation errors must not echo user content.
"""
from __future__ import annotations

import json

from plugins.memory import load_memory_provider


def _provider(tmp_path):
    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "config.json").write_text(
        json.dumps({"vector": {"enabled": False}}), encoding="utf-8"
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-tool-validation",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="agent-a",
        agent_workspace="workspace-a",
        user_id="operator",
        chat_id="dm",
    )
    return plugin


def test_store_enum_is_enforced_before_content_reaches_handler(tmp_path):
    plugin = _provider(tmp_path)
    sentinel = "private-validation-sentinel"
    try:
        raw = plugin.handle_tool_call(
            "scope_recall_store",
            {"content": sentinel, "target": "not-a-target"},
        )
        payload = json.loads(raw)

        assert payload["invalid_arguments"] is True
        assert payload["field"] == "target"
        assert payload["constraint"] == "enum"
        assert sentinel not in raw
        assert plugin._require_conn().execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0] == 0
    finally:
        plugin.shutdown()


def test_store_importance_bounds_are_enforced_at_runtime(tmp_path):
    plugin = _provider(tmp_path)
    try:
        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Importance outside the public contract.",
                    "target": "memory",
                    "importance": 1.5,
                },
            )
        )

        assert payload["invalid_arguments"] is True
        assert payload["field"] == "importance"
        assert payload["constraint"] == "maximum"
        assert plugin._require_conn().execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0] == 0
    finally:
        plugin.shutdown()


def test_required_and_integer_constraints_are_enforced_on_direct_calls(tmp_path):
    plugin = _provider(tmp_path)
    try:
        missing = json.loads(plugin.handle_tool_call("scope_recall_search", {}))
        wrong_type = json.loads(
            plugin.handle_tool_call(
                "scope_recall_search", {"query": "validation", "limit": "5"}
            )
        )
        below_minimum = json.loads(
            plugin.handle_tool_call(
                "scope_recall_search", {"query": "validation", "limit": 0}
            )
        )

        assert missing["invalid_arguments"] is True
        assert missing["constraint"] == "required"
        assert wrong_type["invalid_arguments"] is True
        assert wrong_type["field"] == "limit"
        assert wrong_type["constraint"] == "type"
        assert below_minimum["invalid_arguments"] is True
        assert below_minimum["field"] == "limit"
        assert below_minimum["constraint"] == "minimum"
    finally:
        plugin.shutdown()


def test_evidence_set_search_bounds_are_enforced_on_direct_calls(tmp_path):
    plugin = _provider(tmp_path)
    try:
        cases = [
            ({"query": "validation", "limit": 51}, "limit", "maximum"),
            (
                {"query": "validation", "evidence_diversity_depth": 0},
                "evidence_diversity_depth",
                "minimum",
            ),
            (
                {"query": "validation", "evidence_diversity_depth": 7},
                "evidence_diversity_depth",
                "maximum",
            ),
            (
                {"query": "validation", "query_variants": [f"variant-{i}" for i in range(8)]},
                "query_variants",
                "maxItems",
            ),
            (
                {"query": "validation", "query_variants": ["x" * 1001]},
                "query_variants.0",
                "maxLength",
            ),
        ]

        for arguments, field, constraint in cases:
            payload = json.loads(
                plugin.handle_tool_call("scope_recall_search", arguments)
            )
            assert payload["invalid_arguments"] is True
            assert payload["field"] == field
            assert payload["constraint"] == constraint
    finally:
        plugin.shutdown()
