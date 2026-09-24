"""Exact installer audience mappings never broaden across chats or workspaces."""
from __future__ import annotations

import pytest
import sqlite3

from scope_recall.adapters.hermes import ScopeRecallHermesAdapter, install_hermes_scope_recall
from scope_recall.adapters.hermes.installation import HermesIdentityError, _build_audience_scope_ids


def _row(chat_type: str, chat_id: str, scope_id: str) -> dict[str, object]:
    return {
        "platform": "telegram",
        "user_id": "TEST-user",
        "chat_type": chat_type,
        "chat_id": chat_id,
        "thread_id": "main",
        "gateway_session_key": "",
        "agent_workspace": "TEST-workspace",
        "allowed_scope_ids": [scope_id],
        "writable_scope_ids": [scope_id],
        "capture_scope_id": scope_id,
        "kind": "project" if chat_type == "project" else "conversation",
    }


def _owner_row(*, platform: str, user_id: str, workspace: str) -> dict[str, object]:
    owner_scope = _build_audience_scope_ids(
        platform=platform,
        user_id=user_id,
        agent_identity="TEST-agent",
        agent_workspace=workspace,
        project_id=workspace,
        conversation_key="group-1",
    )["owner_private"]
    return {
        "platform": platform,
        "user_id": user_id,
        "chat_type": "private",
        "chat_id": user_id,
        "thread_id": "main",
        "gateway_session_key": "",
        "agent_workspace": workspace,
        "allowed_scope_ids": [owner_scope],
        "writable_scope_ids": [owner_scope],
        "capture_scope_id": owner_scope,
        "kind": "owner_private",
    }


def test_explicit_audiences_are_isolated(hermes_home, initialize_kwargs):
    audiences = [
        _owner_row(platform="telegram", user_id=initialize_kwargs["user_id"], workspace=initialize_kwargs["agent_workspace"]),
        _row("group", "group-a", "scope:group-a"),
        _row("group", "group-b", "scope:group-b"),
        _row("project", "project-a", "scope:project-a"),
        _row("project", "project-b", "scope:project-b"),
    ]
    _binding, core = install_hermes_scope_recall(
        hermes_home,
        agent_id=initialize_kwargs["agent_identity"],
        platform="telegram",
        user_id=initialize_kwargs["user_id"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        audiences=audiences,
        test_mode=False,
    )

    def identity(**overrides):
        provider = ScopeRecallHermesAdapter(core=core)
        provider.initialize("TEST-session", **dict(initialize_kwargs, platform="telegram", **overrides))
        return provider

    group_a = identity(chat_type="group", chat_id="group-a", thread_id="main")
    group_b = identity(chat_type="group", chat_id="group-b", thread_id="main")
    private = identity(chat_type="private", chat_id="TEST-user", thread_id="main")
    unknown_group = identity(chat_type="group", chat_id="group-z", thread_id="main")
    unknown_workspace = identity(
        chat_type="group", chat_id="group-a", thread_id="main", agent_workspace="other-workspace"
    )
    assert group_a._identity.runtime_audience.allowed_scope_ids == frozenset({"scope:group-a"})
    assert group_b._identity.runtime_audience.allowed_scope_ids == frozenset({"scope:group-b"})
    assert group_a._identity.local_scope_id != group_b._identity.local_scope_id
    assert private._identity.runtime_audience.includes_owner_private
    assert private._identity.owner_private_scope_id in private._identity.runtime_audience.allowed_scope_ids
    assert not unknown_group._identity.runtime_audience.allowed_scope_ids
    assert not unknown_workspace._identity.runtime_audience.allowed_scope_ids
    for provider in (group_a, group_b, private, unknown_group, unknown_workspace):
        provider.shutdown()


def test_owner_private_cannot_be_bound_to_group(hermes_home, initialize_kwargs):
    with pytest.raises(HermesIdentityError, match="owner_private"):
        install_hermes_scope_recall(
            hermes_home,
            agent_id=initialize_kwargs["agent_identity"],
            platform="telegram",
            user_id=initialize_kwargs["user_id"],
            agent_workspace=initialize_kwargs["agent_workspace"],
            audiences=[
                {
                    "platform": "telegram",
                    "user_id": initialize_kwargs["user_id"],
                    "chat_type": "group",
                    "chat_id": "group-a",
                    "thread_id": "main",
                    "gateway_session_key": "",
                    "agent_workspace": "TEST-workspace",
                    "allowed_scope_ids": ["scope:owner"],
                    "writable_scope_ids": ["scope:owner"],
                    "capture_scope_id": "scope:owner",
                    "kind": "owner_private",
                }
            ],
            test_mode=False,
        )


def test_explicit_unthreaded_chat_captures_without_widening_audience(hermes_home, initialize_kwargs):
    unthreaded = dict(
        _row("dm", "TEST-context", "scope:unthreaded"),
        platform="a2a",
        user_id="ip:127.0.0.1",
        thread_id="",
    )
    threaded = dict(
        unthreaded,
        thread_id="main",
        allowed_scope_ids=["scope:thread-main"],
        writable_scope_ids=["scope:thread-main"],
        capture_scope_id="scope:thread-main",
    )
    _binding, core = install_hermes_scope_recall(
        hermes_home, agent_id=initialize_kwargs["agent_identity"],
        platform="a2a", user_id="TEST-owner",
        agent_workspace=initialize_kwargs["agent_workspace"],
        audiences=[
            _owner_row(platform="a2a", user_id="TEST-owner", workspace=initialize_kwargs["agent_workspace"]),
            unthreaded,
            threaded,
        ], test_mode=False,
    )
    common = dict(
        initialize_kwargs, platform="a2a", user_id="ip:127.0.0.1",
        chat_type="dm", chat_id="TEST-context",
    )
    # Actual Hermes Gateway shape: no thread_id is supplied for a plain chat.
    common.pop("thread_id", None)
    providers = []
    try:
        for index, (overrides, expected) in enumerate([
            ({}, {"scope:unthreaded"}),
            ({"thread_id": "main"}, {"scope:thread-main"}),
            ({"thread_id": "other"}, set()),
            ({"chat_id": "other-chat"}, set()),
            ({"chat_id": ""}, set()),
            ({"agent_workspace": "other-workspace"}, set()),
        ]):
            provider = ScopeRecallHermesAdapter(core=core)
            providers.append(provider)
            session = f"TEST-unthreaded-{index}"
            provider.initialize(session, **dict(common, **overrides))
            assert provider._identity.runtime_audience.allowed_scope_ids == frozenset(expected)
            assert not provider._identity.runtime_audience.includes_owner_private
            provider.observe_pre_llm(session_id=session, turn_id="turn-1", user_message="TEST unthreaded capture")
        with sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3") as connection:
            rows = connection.execute("SELECT scope_id FROM source_events ORDER BY scope_id").fetchall()
        assert rows == [("scope:thread-main",), ("scope:unthreaded",)]
    finally:
        for provider in providers:
            provider.shutdown()


@pytest.mark.parametrize("thread_value", [None, 0, False, "unknown"])
def test_unthreaded_mapping_requires_explicit_string(hermes_home, initialize_kwargs, thread_value):
    row = dict(_row("dm", "TEST-context", "scope:unthreaded"), thread_id=thread_value)
    with pytest.raises(HermesIdentityError):
        install_hermes_scope_recall(
            hermes_home, agent_id=initialize_kwargs["agent_identity"],
            platform="telegram", user_id="TEST-owner",
            agent_workspace=initialize_kwargs["agent_workspace"],
            audiences=[row], test_mode=False,
        )


def _gateway_rows(user_id: str, workspace: str) -> list[dict[str, object]]:
    """Weixin DM rows as #124 found them: the host's real key nowhere, one row per kind of thread."""
    dm = {"platform": "weixin", "user_id": user_id, "chat_type": "dm", "chat_id": "", "gateway_session_key": "",
          "agent_workspace": workspace, "kind": "conversation"}
    return [
        _owner_row(platform="weixin", user_id=user_id, workspace=workspace),
        dict(dm, thread_id="", allowed_scope_ids=["scope:plain"], writable_scope_ids=["scope:plain"],
             capture_scope_id="scope:plain"),
        dict(dm, chat_id="pinned-chat", thread_id="", gateway_session_key="agent:main:weixin:dm:pinned-chat",
             allowed_scope_ids=["scope:pinned"], writable_scope_ids=["scope:pinned"], capture_scope_id="scope:pinned"),
        dict(dm, chat_id="main-chat", thread_id="main", allowed_scope_ids=["scope:main-row"],
             writable_scope_ids=["scope:main-row"], capture_scope_id="scope:main-row"),
    ]


def test_a_row_naming_no_session_key_matches_the_key_a_gateway_sends(hermes_home, initialize_kwargs):
    """#124: a gateway sends a session key on every session and the rows leave it empty, so
    every weixin, feishu and desktop route failed closed; the other six fields still decide."""
    workspace = initialize_kwargs["agent_workspace"]
    _binding, core = install_hermes_scope_recall(
        hermes_home, agent_id=initialize_kwargs["agent_identity"], platform="weixin", user_id="TEST-wx",
        agent_workspace=workspace, audiences=_gateway_rows("TEST-wx", workspace), test_mode=False)
    session = dict(initialize_kwargs, platform="weixin", user_id="TEST-wx", chat_type="dm", chat_id="")
    session.pop("thread_id", None)
    cases = [
        (dict(gateway_session_key="agent:main:weixin:dm:TEST-wx"), {"scope:plain"}),
        (dict(gateway_session_key=""), {"scope:plain"}),
        (dict(chat_id="pinned-chat", gateway_session_key="agent:main:weixin:dm:pinned-chat"), {"scope:pinned"}),
        (dict(chat_id="pinned-chat", gateway_session_key="agent:main:weixin:dm:someone-else"), set()),
        (dict(chat_id="other-chat", gateway_session_key="agent:main:weixin:dm:other-chat"), set()),
        (dict(user_id="TEST-other", gateway_session_key="agent:main:weixin:dm:TEST-wx"), set()),
    ]
    for index, (overrides, expected) in enumerate(cases):
        provider = ScopeRecallHermesAdapter(core=core)
        try:
            provider.initialize(f"TEST-gateway-{index}", **dict(session, **overrides))
            assert provider._identity.runtime_audience.allowed_scope_ids == frozenset(expected), overrides
        finally:
            provider.shutdown()


def test_a_row_that_differs_only_in_the_plain_thread_is_named_not_granted(hermes_home, initialize_kwargs):
    """The host sends an empty thread for a plain chat; a row copied with the CLI's "main" still
    grants nothing there (the two are different routes), but the gap now says which field."""
    workspace = initialize_kwargs["agent_workspace"]
    _binding, core = install_hermes_scope_recall(
        hermes_home, agent_id=initialize_kwargs["agent_identity"], platform="weixin", user_id="TEST-wx",
        agent_workspace=workspace, audiences=_gateway_rows("TEST-wx", workspace), test_mode=False)
    session = dict(initialize_kwargs, platform="weixin", user_id="TEST-wx", chat_type="dm", chat_id="main-chat",
                   gateway_session_key="agent:main:weixin:dm:main-chat")
    session.pop("thread_id", None)
    provider = ScopeRecallHermesAdapter(core=core)
    try:
        provider.initialize("TEST-thread-near-miss", **session)
        audience = provider._identity.runtime_audience
        assert audience.allowed_scope_ids == frozenset()
        assert "capability_gap:audience_thread_mismatch:row_says_main" in audience.capability_gaps
    finally:
        provider.shutdown()


def test_a_session_key_never_rides_on_the_cli_route(installed_core, initialize_kwargs):
    """The CLI sends no session key.  Hermes reports a relayed ``local`` gateway session to plugins
    as platform ``cli``, with its key; relaxing the key there would hand it the CLI's owner scope."""
    core, _clock = installed_core
    for index, (key, granted) in enumerate((("", True), ("agent:main:local:dm:TEST-relay", False))):
        provider = ScopeRecallHermesAdapter(core=core)
        try:
            provider.initialize(f"TEST-cli-{index}", **dict(initialize_kwargs, gateway_session_key=key))
            assert bool(provider._identity.runtime_audience.allowed_scope_ids) is granted, key
        finally:
            provider.shutdown()
