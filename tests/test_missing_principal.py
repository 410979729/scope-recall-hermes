"""Fail-closed runtime tests for missing non-CLI principals.

A chat identifier is routing context, not a user principal.  Adapters that omit
``user_id`` must not activate durable memory even when a chat alias exists.
"""

from __future__ import annotations

import json
from pathlib import Path

from plugins.memory import load_memory_provider


DISABLED_MISSING_PRINCIPAL = "disabled_missing_principal"


def _provider():
    provider = load_memory_provider("scope-recall")
    assert provider is not None
    return provider


def _write_config(hermes_home: Path, payload: dict) -> None:
    path = hermes_home / "scope-recall" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_missing_non_cli_principal_disables_provider_without_touching_storage(tmp_path):
    hermes_home = tmp_path / "missing-principal-home"
    provider = _provider()
    provider._open_runtime_connection = lambda: (_ for _ in ()).throw(
        AssertionError("missing-principal initialization must not open SQLite")
    )

    try:
        provider.initialize(
            "missing-principal-session",
            hermes_home=str(hermes_home),
            platform="telegram",
            user_id="   ",
            chat_id="chat-a",
            agent_identity="yuheng",
            agent_workspace="hermes",
            agent_context="primary",
        )

        assert provider.runtime_status == DISABLED_MISSING_PRINCIPAL
        assert provider.is_available() is False
        assert provider._scope.user_id == ""
        assert provider._scope_id == ""
        assert provider._shared_scope_id == ""
        assert provider._accessible_scope_ids == []
        assert provider._writable_scope_ids == []
        assert provider._conn is None
        assert provider._db_path is None
        assert not hermes_home.exists()

        assert provider.get_tool_schemas() == []
        assert provider.prefetch("do not recall this") == ""
        assert provider.on_pre_compress(
            [{"role": "user", "content": "do not capture this"}]
        ) == ""
        provider.sync_turn("do not capture this", "do not capture this either")
        provider.on_session_end([{"role": "tool", "content": "do not persist"}])
        assert provider.flush() is True
        assert provider.suppress_builtin_memory() is True
        assert DISABLED_MISSING_PRINCIPAL in provider.system_prompt_block()

        tool_result = json.loads(provider.handle_tool_call("scope_recall_search", {"query": "x"}))
        assert tool_result["error"] == DISABLED_MISSING_PRINCIPAL
    finally:
        provider.shutdown()


def test_chat_alias_cannot_replace_a_missing_runtime_principal(tmp_path):
    _write_config(
        tmp_path,
        {
            "identity": {
                "cross_platform_shared_scope": True,
                "chat_aliases": {"telegram:chat-a": "operator"},
            },
            "vector": {"enabled": False},
        },
    )
    db_path = tmp_path / "scope-recall" / "memory.sqlite3"
    provider = _provider()

    try:
        provider.initialize(
            "missing-principal-with-alias",
            hermes_home=str(tmp_path),
            platform="telegram",
            user_id="",
            chat_id="chat-a",
            agent_identity="yuheng",
            agent_workspace="hermes",
        )

        assert provider.runtime_status == DISABLED_MISSING_PRINCIPAL
        assert provider._accessible_scope_ids == []
        assert provider.get_tool_schemas() == []
        assert not db_path.exists()
    finally:
        provider.shutdown()


def test_present_non_cli_principal_keeps_normal_provider_activation(tmp_path):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    provider = _provider()

    try:
        provider.initialize(
            "present-principal",
            hermes_home=str(tmp_path),
            platform="telegram",
            user_id="user-a",
            chat_id="chat-a",
            agent_identity="yuheng",
            agent_workspace="hermes",
        )

        assert provider.runtime_status == "active"
        assert provider.is_available() is True
        assert provider._scope.user_id == "user-a"
        assert provider._scope_id
        assert provider._shared_scope_id
        assert provider._conn is not None
        assert (tmp_path / "scope-recall" / "memory.sqlite3").is_file()
        assert provider.get_tool_schemas()
    finally:
        provider.shutdown()
