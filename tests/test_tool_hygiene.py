"""Tests for public tool schema compactness, diagnostics exposure, and sanitized tool errors.

They protect prompt budget and prevent dangerous tools from appearing by default."""

from __future__ import annotations

import json

import pytest

from plugins.memory import load_memory_provider
from scope_recall.tooling import ScopeRecallToolService


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
    return json.loads(
        provider.handle_tool_call(
            "scope_recall_store", {"content": content, "target": target}
        )
    )


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


def test_default_schema_surface_uses_compact_core_tools(provider):
    names = _schema_names(provider)

    assert names == {
        "scope_recall_store",
        "scope_recall_search",
        "scope_recall_context",
        "scope_recall_profile",
        "scope_recall_memory",
        "scope_recall_entity",
    }
    assert "scope_recall_store_secret_index" not in names
    assert "scope_recall_export" not in names
    assert "scope_recall_stats" not in names
    assert "scope_recall_benchmark" not in names
    assert "scope_recall_experience_stats" not in names

    assert "secret_index_tools_enabled=true" in provider.handle_tool_call(
        "scope_recall_store_secret_index", {"label": "test"}
    )
    assert "provider" in provider.handle_tool_call("scope_recall_stats", {})


def test_tool_governance_telemetry_is_content_free(provider):
    _store(provider, "Tool governance telemetry fixture.", "project")
    provider.handle_tool_call("lancepro_search", {"query": "telemetry fixture"})

    payload = json.loads(provider.handle_tool_call("scope_recall_stats", {}))
    governance = payload["tool_governance"]
    search = governance["usage"]["scope_recall_search"]

    assert governance["profile"] == "core"
    assert governance["schema_budget"]["tool_count"] == 6
    assert governance["content_free"] is True
    assert search["call_count"] == 1
    assert search["success_count"] == 1
    assert search["error_count"] == 0
    assert search["alias_usage_count"] == 1
    assert search["last_used_version"]
    assert search["maintenance_dependency"] is False
    assert "telemetry fixture" not in json.dumps(governance).lower()


def test_standard_schema_profile_restores_legacy_read_only_tools(tmp_path):
    plugin = _provider_with_config(
        tmp_path,
        {
            "tool_schema_profile": "standard",
            "vector": {"enabled": False},
        },
    )
    try:
        names = _schema_names(plugin)

        assert "scope_recall_memory" not in names
        assert "scope_recall_entity" not in names
        assert "scope_recall_probe" in names
        assert "scope_recall_related" in names
        assert "scope_recall_feedback" in names
        assert "scope_recall_export" in names
        assert "scope_recall_stats" in names
        assert "scope_recall_benchmark" in names
        assert "scope_recall_experience_stats" in names
        assert "scope_recall_store_secret_index" not in names
        assert "scope_recall_govern" not in names
        assert "scope_recall_forgetting_run" not in names
    finally:
        plugin.shutdown()


def test_schema_extra_tools_expose_selected_diagnostics_without_standard_profile(
    tmp_path,
):
    plugin = _provider_with_config(
        tmp_path,
        {
            "tool_schema_extra_tools": [
                "scope_recall_stats",
                "scope_recall_benchmark",
                "scope_recall_store_secret_index",
            ],
            "vector": {"enabled": False},
        },
    )
    try:
        names = _schema_names(plugin)

        assert "scope_recall_memory" in names
        assert "scope_recall_stats" in names
        assert "scope_recall_benchmark" in names
        assert "scope_recall_store_secret_index" not in names
    finally:
        plugin.shutdown()


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
        assert "scope_recall_export" not in names
        assert "scope_recall_stats" not in names
        assert "scope_recall_benchmark" not in names
        assert "scope_recall_experience_stats" not in names
        assert "scope_recall_govern" not in names
        assert "scope_recall_forgetting_run" not in names
    finally:
        plugin.shutdown()


def test_compact_memory_and_entity_tools_dispatch_to_legacy_operations(provider):
    created = _store(
        provider,
        "Compact schema memory entity AlphaProject prefers exact-id operations.",
        "project",
    )
    assert created["stored"] is True

    inspected = json.loads(
        provider.handle_tool_call(
            "scope_recall_memory", {"action": "inspect", "id": created["id"]}
        )
    )
    assert inspected["found"] is True
    assert inspected["memory"]["id"] == created["id"]

    feedback = json.loads(
        provider.handle_tool_call(
            "scope_recall_memory",
            {"action": "feedback", "id": created["id"], "rating": "helpful"},
        )
    )
    assert feedback["updated"] is True

    entity_probe = json.loads(
        provider.handle_tool_call(
            "scope_recall_entity",
            {"action": "probe", "entity": "AlphaProject", "limit": 5},
        )
    )
    assert entity_probe["count"] >= 1

    related = json.loads(
        provider.handle_tool_call(
            "scope_recall_entity",
            {"action": "related", "entity": "AlphaProject", "limit": 5},
        )
    )
    assert "related" in related

    updated = json.loads(
        provider.handle_tool_call(
            "scope_recall_memory",
            {
                "action": "update",
                "id": created["id"],
                "content": "Compact schema memory update keeps AlphaProject searchable.",
            },
        )
    )
    assert updated["updated"] is True

    archived = json.loads(
        provider.handle_tool_call(
            "scope_recall_memory",
            {"action": "forget", "id": created["id"], "reason": "test cleanup"},
        )
    )
    assert archived["archived"] == 1
    assert archived["deleted"] == 0
    assert archived["receipt"]["action"] == "soft_archive"
    inspected_after = json.loads(
        provider.handle_tool_call(
            "scope_recall_memory", {"action": "inspect", "id": created["id"]}
        )
    )
    assert inspected_after["found"] is True
    assert inspected_after["memory"]["metadata"]["lifecycle"] == "archived"


def test_scope_recall_forget_hard_delete_requires_maintenance_tools(provider):
    created = _store(
        provider, "Hard delete should require maintenance mode.", "project"
    )

    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_forget", {"id": created["id"], "hard_delete": True}
        )
    )

    assert (
        payload["error"]
        == "scope_recall_forget hard_delete requires maintenance_tools_enabled=true"
    )
    assert payload["mode"] == "hard_delete"
    assert payload["data_retained"] is True
    assert payload["reversible"] is False
    assert payload["privacy_purge"] is False
    assert payload["mutation_applied"] is False


def test_forget_hard_delete_reports_not_retained_and_not_purge(provider):
    created = _store(provider, "Maintenance hard delete response contract.", "project")
    provider._config["maintenance_tools_enabled"] = True

    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_forget", {"id": created["id"], "hard_delete": True}
        )
    )

    assert payload["deleted"] == 1
    assert payload["mode"] == "hard_delete"
    assert payload["data_retained"] is False
    assert payload["reversible"] is False
    assert payload["privacy_purge"] is False
    assert payload["mutation_applied"] is True


def test_tool_handler_fallback_errors_are_sanitized(monkeypatch):
    service = ScopeRecallToolService(object())
    secret = "sk-" + "TOOLHANDLERSECRET123456"

    def boom(_args):
        raise RuntimeError(
            f"provider failed api_key={secret} {'/tmp/' + 'hermes-secret-path'}"
        )

    monkeypatch.setattr(service, "_handle_stats", boom)

    payload = service.handle("scope_recall_stats", {})

    assert secret not in payload
    assert "api_key=" not in payload
    assert "/tmp/hermes" not in payload
    assert "[REDACTED_SECRET]" in payload
    assert "[REDACTED_PATH]" in payload


def test_tool_store_uses_capture_filter_for_secret_like_content(provider):
    payload = _store(
        provider, "api_key = public-test-token should not become memory", "memory"
    )

    assert payload["stored"] is False
    assert payload["skipped"] is True
    assert payload["skip_reason"] == "plaintext_secret_rejected"


def test_tool_update_uses_capture_filter_for_secret_like_content(provider):
    created = _store(
        provider, "Joy prefers read-only SQLite viewers for memory inspection.", "user"
    )
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
    assert payload["skip_reason"] == "plaintext_secret_rejected"

    provider.on_turn_start(1, "What does Joy prefer for memory inspection?")
    assert (
        "read-only sqlite viewers"
        in provider.prefetch("What does Joy prefer for memory inspection?").lower()
    )


def test_tool_merge_uses_capture_filter_for_runtime_wrappers(provider):
    created = _store(
        provider, "Joy prefers stable memory facts over raw chat wrappers.", "project"
    )
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


def test_writer_unknown_tool_does_not_echo_private_path_or_token():
    class _Owner:
        _truth_writer_role = "owner"

    service = ScopeRecallToolService(_Owner())
    adversarial_tool = (
        r"C:\Users\Administrator\token-"
        + "ghp_"
        + "abcdefghijklmnopqrstuvwxyz012345"
        + r"\scope_recall_store"
    )

    payload = json.loads(service.handle(adversarial_tool, {"content": "must not echo"}))
    serialized = json.dumps(payload)

    assert payload.get("error") == "unknown scope-recall tool"
    assert adversarial_tool not in serialized
    assert "Administrator" not in serialized
    assert "ghp_" not in serialized
    assert "C:\\Users" not in serialized
    assert r"C:\Users" not in serialized
    assert "truth_writer_busy" not in serialized


def test_unknown_role_cannot_invoke_durable_write_handler(monkeypatch):
    class _UnknownRole:
        _truth_writer_role = "unknown"

    service = ScopeRecallToolService(_UnknownRole())
    called: list[dict] = []
    monkeypatch.setattr(
        service,
        "_handle_store",
        lambda args: called.append(args) or json.dumps({"stored": True}),
    )

    payload = json.loads(
        service.handle(
            "scope_recall_store",
            {"content": "must not store from an unknown role", "target": "ops"},
        )
    )

    assert called == []
    assert "truth_writer_busy" in str(payload.get("error") or "")
    serialized = json.dumps(payload)
    assert "must not store from an unknown role" not in serialized
