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
