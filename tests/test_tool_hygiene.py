from __future__ import annotations

import json

import pytest

from plugins.memory import load_memory_provider


@pytest.fixture
def provider(tmp_path):
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-a",
    )
    yield plugin
    plugin.shutdown()


def _store(provider, content: str, target: str = "memory") -> dict:
    return json.loads(provider.handle_tool_call("scope_recall_store", {"content": content, "target": target}))


def _schema_names(provider) -> set[str]:
    return {str(schema["name"]) for schema in provider.get_tool_schemas()}


def _provider_with_config(tmp_path, config: dict):
    config_path = tmp_path / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-configured",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-a",
    )
    return plugin


def test_default_schema_surface_hides_low_frequency_secret_index_tool(provider):
    names = _schema_names(provider)

    assert "scope_recall_store" in names
    assert "scope_recall_search" in names
    assert "scope_recall_profile" in names
    assert "scope_recall_experience_preflight" in names
    assert "scope_recall_store_secret_index" not in names
    assert "scope_recall_export" in names
    assert "scope_recall_stats" in names
    assert "scope_recall_benchmark" in names
    assert "scope_recall_experience_stats" in names
    assert len(names) <= 21

    assert "secret_index_tools_enabled=true" in provider.handle_tool_call("scope_recall_store_secret_index", {"label": "test"})


def test_secret_index_schema_surface_is_explicit_opt_in(tmp_path):
    plugin = _provider_with_config(
        tmp_path,
        {
            "secret_index_tools_enabled": True,
            "vector": {"enabled": False},
        },
    )
    try:
        names = _schema_names(plugin)

        assert "scope_recall_store_secret_index" in names
        assert "scope_recall_export" in names
        assert "scope_recall_stats" in names
        assert "scope_recall_benchmark" in names
        assert "scope_recall_experience_stats" in names
        assert "scope_recall_govern" not in names
        assert "scope_recall_forgetting_run" not in names
    finally:
        plugin.shutdown()


def test_tool_store_uses_capture_filter_for_secret_like_content(provider):
    payload = _store(provider, "api_key = public-test-token should not become memory", "memory")

    assert payload["stored"] is False
    assert payload["skipped"] is True
    assert payload["skip_reason"] == "secret-like-content"


def test_tool_update_uses_capture_filter_for_secret_like_content(provider):
    created = _store(provider, "Joy prefers read-only SQLite viewers for memory inspection.", "user")
    assert created["stored"] is True

    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_update",
            {
                "id": created["id"],
                "content": "credential_private = public-test-token should not become memory",
                "target": "user",
            },
        )
    )

    assert payload["error"] == "content is not suitable for storage"
    assert payload["skipped"] is True
    assert payload["skip_reason"] == "secret-like-content"

    provider.on_turn_start(1, "What does Joy prefer for memory inspection?")
    assert "read-only sqlite viewers" in provider.prefetch("What does Joy prefer for memory inspection?").lower()


def test_tool_merge_uses_capture_filter_for_runtime_wrappers(provider):
    created = _store(provider, "Joy prefers stable memory facts over raw chat wrappers.", "project")
    assert created["stored"] is True

    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_merge",
            {
                "target_id": created["id"],
                "content": "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below.",
                "target": "project",
            },
        )
    )

    assert payload["error"] == "content is not suitable for storage"
    assert payload["skipped"] is True
    assert "CONTEXT COMPACTION" in payload["skip_reason"]

    provider.on_turn_start(1, "What memory facts does Joy prefer?")
    recalled = provider.prefetch("What memory facts does Joy prefer?")
    assert "stable memory facts" in recalled.lower()
    assert "context compaction" not in recalled.lower()
