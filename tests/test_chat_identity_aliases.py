"""Security contracts for operator-authorized whole-chat identity aliases."""

from __future__ import annotations

from scope_recall.models import RuntimeScope
from scope_recall.scope import build_shared_scope_id, canonical_user_id


def test_exact_chat_alias_precedes_account_alias_for_shared_identity():
    config = {
        "identity": {
            "cross_platform_shared_scope": True,
            "user_aliases": {"telegram:synthetic-user": "account-owner"},
            "chat_aliases": {"telegram:synthetic-chat": "chat-owner"},
        }
    }
    scope = RuntimeScope(
        platform="telegram",
        user_id="synthetic-user",
        chat_id="synthetic-chat",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )

    assert canonical_user_id(scope, config) == "chat-owner"
    assert build_shared_scope_id(scope, config) == (
        "workspace:6:hermes|agent:6:yuheng|canonical_user:10:chat-owner"
    )


def test_chat_alias_is_exact_and_requires_cross_platform_identity_gate():
    aliases = {"telegram:synthetic-chat": "chat-owner"}
    enabled = {
        "identity": {
            "cross_platform_shared_scope": True,
            "chat_aliases": aliases,
        }
    }
    disabled = {
        "identity": {
            "cross_platform_shared_scope": False,
            "chat_aliases": aliases,
        }
    }

    assert canonical_user_id(
        RuntimeScope(platform="telegram", chat_id="other-chat"),
        enabled,
    ) == ""
    assert canonical_user_id(
        RuntimeScope(platform="discord", chat_id="synthetic-chat"),
        enabled,
    ) == ""
    assert canonical_user_id(
        RuntimeScope(platform="telegram", chat_id="synthetic-chat"),
        disabled,
    ) == ""


def test_chat_alias_resolver_fails_closed_for_empty_platform_or_chat_id():
    config = {
        "identity": {
            "cross_platform_shared_scope": True,
            "chat_aliases": {
                "telegram:": "owner",
                ":synthetic-chat": "owner",
            },
        }
    }

    assert canonical_user_id(
        RuntimeScope(platform="telegram", chat_id=""), config
    ) == ""
    assert canonical_user_id(
        RuntimeScope(platform="", chat_id="synthetic-chat"), config
    ) == ""