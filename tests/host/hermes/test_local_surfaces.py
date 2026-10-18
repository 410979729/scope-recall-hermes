"""A host surface that names no user is refused until the installer approves it as the owner's own.

Hermes Desktop's chat panel and ``hermes --tui`` hand a memory provider the dashboard login as
``user_id``, and nothing when nobody logged in (``tui_gateway/server.py``: platform ``desktop`` or
``tui``).  Only what the installation manifest approves is bound, so every such session fails
with ``user principal required for non-cli platform`` and the provider does not initialise.
The approval is the installer's: ``apply-install --local-platform desktop``.

Through Hermes's local session transport the gateway runs those conversations, and the CLI's, itself.
It names the account the OS says the client runs as (``sid:<SID>``, ``uid:<uid>``), and only this
gateway's own; each conversation is a direct chat of its own (``chat_id`` ``local-<64 hex>``) under
the session key ``agent:<profile>:local:dm:<chat id>``.  That account is the person at this machine,
bound as an unnamed session there is; the conversation is never a route.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from scope_recall.adapters.hermes import (
    HermesIdentityError,
    ScopeRecallHermesAdapter,
    bind_hermes_identity,
    install_hermes_scope_recall,
    unbound_sessions,
)
from scope_recall.adapters.hermes import identity as hermes_identity
from scope_recall.adapters.hermes.audiences import audience_entry, normalize_local_platforms, normalize_owner_logins
from scope_recall.adapters.hermes.identity import switch_hermes_identity
from scope_recall.adapters.hermes.installation import (
    approve_local_platforms,
    build_installation_manifest,
    load_installation_manifest,
    manifest_payload,
    unapproved_local_platforms,
    write_installation_manifest,
)
from scope_recall.adapters.hermes.shared_entries import attach_shared_entry, new_shared_payload, write_shared_payload
from scope_recall.maintenance import doctor
from scope_recall.maintenance import shared as shared_cli


def _install(hermes_home, initialize_kwargs, **options):
    return install_hermes_scope_recall(
        hermes_home,
        agent_id=initialize_kwargs["agent_identity"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        test_mode=False,
        **options,
    )


def _session(initialize_kwargs, platform, **given):
    """What the host sends from a surface with no login: a platform and nothing that names a person or a chat."""
    kwargs = {key: value for key, value in initialize_kwargs.items() if key != "user_id"}
    return dict(kwargs, platform=platform, **given)


def test_a_local_surface_is_refused_until_it_is_approved_and_the_refusal_says_how(hermes_home, initialize_kwargs):
    _install(hermes_home, initialize_kwargs)

    for platform in ("desktop", "tui"):
        with pytest.raises(HermesIdentityError, match="user principal required for non-cli platform") as refused:
            bind_hermes_identity("TEST-session-1", **_session(initialize_kwargs, platform))
        assert f"--local-platform {platform}" in str(refused.value)


def test_an_approved_local_surface_is_the_owner_with_the_memory_the_cli_has(hermes_home, initialize_kwargs):
    _install(hermes_home, initialize_kwargs, local_platforms=("desktop",))

    desktop = bind_hermes_identity("TEST-session-1", **_session(initialize_kwargs, "desktop"))
    cli = bind_hermes_identity("TEST-session-2", **_session(initialize_kwargs, "cli"))

    assert (desktop.scope.user_id, desktop.scope.chat_type, desktop.scope.chat_id, desktop.scope.thread_id) == (
        "local",
        "private",
        "local",
        "main",
    )
    audience = desktop.runtime_audience
    assert audience.includes_owner_private and audience.capability_gaps == ()
    assert audience.allowed_scope_ids == audience.writable_scope_ids == cli.runtime_audience.allowed_scope_ids
    assert audience.capture_scope_id == desktop.owner_private_scope_id == cli.owner_private_scope_id
    assert not desktop.read_only
    context = desktop.trusted_context()
    assert context.actor_origin == "human_direct", "a person is typing there, as on the CLI"
    assert (context.source_principal.kind, context.source_principal.resolution) == ("human", "verified")


def test_what_is_said_on_an_approved_surface_is_captured_as_the_owners(hermes_home, initialize_kwargs):
    _binding, core = _install(hermes_home, initialize_kwargs, local_platforms=("desktop",))
    provider = ScopeRecallHermesAdapter(core=core)
    provider.initialize("TEST-desktop-session", **_session(initialize_kwargs, "desktop"))
    try:
        provider.observe_pre_llm(
            session_id="TEST-desktop-session", turn_id="TEST-turn-1", user_message="TEST 桌面端的配色用蓝色。"
        )
        provider.sync_turn("TEST 桌面端的配色用蓝色。", "好的。", session_id="TEST-desktop-session")
        with sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3") as connection:
            rows = connection.execute("SELECT origin, scope_id FROM source_events WHERE role='user'").fetchall()
        assert rows and set(rows) == {("human_direct", provider._identity.owner_private_scope_id)}
    finally:
        provider.shutdown()


def test_approving_one_surface_approves_no_other(hermes_home, initialize_kwargs):
    _install(hermes_home, initialize_kwargs, local_platforms=("desktop",))

    with pytest.raises(HermesIdentityError, match="--local-platform tui"):
        bind_hermes_identity("TEST-session-1", **_session(initialize_kwargs, "tui"))


def test_a_scheduled_run_is_never_a_local_surface(hermes_home, initialize_kwargs):
    """Nobody is speaking in a cron run, a job can be created from any chat, and its prompt would be
    captured as the owner's own words.  Not even a manifest that names ``(cron, local)`` opens it."""
    with pytest.raises(HermesIdentityError, match="local platform must be one of"):
        normalize_local_platforms(["cron"])
    _install(hermes_home, initialize_kwargs, platform="cron", user_id="local")

    with pytest.raises(HermesIdentityError, match="user principal required for non-cli platform$"):
        bind_hermes_identity("TEST-session-1", **_session(initialize_kwargs, "cron"))


def test_a_gateway_platform_is_not_local_even_when_its_owner_is_called_local(hermes_home, initialize_kwargs):
    _install(hermes_home, initialize_kwargs, platform="telegram", user_id="local")

    with pytest.raises(HermesIdentityError, match="user principal required for non-cli platform$"):
        bind_hermes_identity("TEST-session-1", **_session(initialize_kwargs, "telegram"))


def test_a_login_on_an_approved_surface_is_a_user_like_any_other(hermes_home, initialize_kwargs):
    """The host passes a dashboard login as ``<provider>:<user>``.  That is a named user: no fallback,
    not the local owner's route, and no scope unless an owner principal or an audience names it."""
    _install(hermes_home, initialize_kwargs, local_platforms=("desktop",))

    identity = bind_hermes_identity(
        "TEST-session-1", **_session(initialize_kwargs, "desktop", user_id="basic:TEST-visitor")
    )

    assert (identity.scope.user_id, identity.scope.chat_type, identity.scope.chat_id) == (
        "basic:TEST-visitor",
        "private",
        "basic:TEST-visitor",
    )
    assert identity.runtime_audience.allowed_scope_ids == frozenset()
    assert "capability_gap:audience_unmapped" in identity.runtime_audience.capability_gaps
    assert identity.read_only


def test_a_host_that_names_its_user_local_on_a_surface_nobody_approved_keeps_no_route(hermes_home, initialize_kwargs):
    """Only a login is routed as a one-to-one chat with itself.  ``local`` sent by the host on a surface the
    owner never approved keeps the empty route it was sent with, so no row written for an approved surface's
    route, nor one written by hand, can match it."""
    _install(hermes_home, initialize_kwargs)

    identity = bind_hermes_identity("TEST-session-1", **_session(initialize_kwargs, "desktop", user_id="local"))

    assert (identity.scope.chat_type, identity.scope.chat_id) == ("", "")
    assert identity.runtime_audience.allowed_scope_ids == frozenset()


def test_a_session_switch_on_an_approved_surface_stays_the_owners(hermes_home, initialize_kwargs):
    _install(hermes_home, initialize_kwargs, local_platforms=("tui",))
    first = bind_hermes_identity("TEST-session-1", **_session(initialize_kwargs, "tui"))

    second = switch_hermes_identity(first, "TEST-session-2")

    assert second.scope == first.scope and second.session_id == "TEST-session-2"
    assert second.runtime_audience == first.runtime_audience and not second.read_only


def test_approving_an_existing_installation_adds_two_entries_and_changes_nothing_else(hermes_home, initialize_kwargs):
    workspace = initialize_kwargs["agent_workspace"]
    before = build_installation_manifest(
        hermes_home, agent_id=initialize_kwargs["agent_identity"], agent_workspace=workspace
    )
    assert unapproved_local_platforms(before, ["desktop", "tui"], agent_workspace=workspace) == ("desktop", "tui")

    after = approve_local_platforms(before, ["desktop"], agent_workspace=workspace)

    assert after.to_binding() == before.to_binding(), "the store is bound to the same scopes"
    assert (
        after.audiences[: len(before.audiences)] == before.audiences
        and len(after.audiences) == len(before.audiences) + 1
    )
    assert after.owner_principals == (*before.owner_principals, {"platform": "desktop", "user_id": "local"})
    granted = after.audiences[-1]
    assert (granted["platform"], granted["user_id"], granted["kind"]) == ("desktop", "local", "owner_private")
    assert granted["allowed_scope_ids"] == granted["writable_scope_ids"] == [before.audience_scopes["owner_private"]]
    assert unapproved_local_platforms(after, ["desktop", "tui"], agent_workspace=workspace) == ("tui",)
    again = approve_local_platforms(after, ["desktop"], agent_workspace=workspace)
    assert manifest_payload(again) == manifest_payload(after), "approving twice is approving once"


LOGIN = "basic:TEST-owner"


def _approve_login_by_hand(hermes_home, initialize_kwargs, *, platform="desktop", login=LOGIN):
    """What approving a login writes, spelled out: the owner principal ``(platform, login)`` and one grant of
    the owner's private scope on the route the adapter gives that login, a one-to-one chat with it."""
    manifest = load_installation_manifest(hermes_home)
    owner = manifest.audience_scopes["owner_private"]
    row = dict(
        platform=platform,
        user_id=login,
        chat_type="private",
        chat_id=login,
        thread_id="main",
        gateway_session_key="",
        agent_workspace=initialize_kwargs["agent_workspace"],
        allowed_scope_ids=[owner],
        writable_scope_ids=[owner],
        capture_scope_id=owner,
        kind="owner_private",
    )
    write_installation_manifest(
        replace(
            manifest,
            owner_principals=(*manifest.owner_principals, {"platform": platform, "user_id": login}),
            audiences=(*manifest.audiences, row),
        )
    )


def test_a_login_the_owner_approved_is_the_owner_on_that_surface_and_nowhere_else(hermes_home, initialize_kwargs):
    """Hermes passes a dashboard login as ``user_id`` and no chat at all.  Kept as that empty route, which no owner
    row can name (an owner_private row is an explicit private chat), even a login the owner approved would bind
    nothing.  So there a session is a one-to-one chat with the login it names."""
    _install(hermes_home, initialize_kwargs)
    _approve_login_by_hand(hermes_home, initialize_kwargs)

    login = bind_hermes_identity("TEST-session-1", **_session(initialize_kwargs, "desktop", user_id=LOGIN))
    cli = bind_hermes_identity("TEST-session-2", **_session(initialize_kwargs, "cli"))

    assert (login.scope.user_id, login.scope.chat_type, login.scope.chat_id, login.scope.thread_id) == (
        LOGIN,
        "private",
        LOGIN,
        "main",
    )
    audience = login.runtime_audience
    assert audience.includes_owner_private and audience.capability_gaps == ()
    assert audience.capture_scope_id == login.owner_private_scope_id == cli.owner_private_scope_id
    assert not login.read_only
    assert switch_hermes_identity(login, "TEST-session-3").runtime_audience == audience
    # The approval names one login on one surface.
    for platform, user in (("desktop", "basic:TEST-other"), ("tui", LOGIN)):
        other = bind_hermes_identity("TEST-session-4", **_session(initialize_kwargs, platform, user_id=user))
        assert other.runtime_audience.allowed_scope_ids == frozenset() and other.read_only, (platform, user)
    with pytest.raises(HermesIdentityError, match="--local-platform desktop"):
        bind_hermes_identity("TEST-session-5", **_session(initialize_kwargs, "desktop"))


def test_what_an_approved_login_says_is_captured_as_the_owners(hermes_home, initialize_kwargs):
    _binding, core = _install(hermes_home, initialize_kwargs)
    _approve_login_by_hand(hermes_home, initialize_kwargs)
    provider = ScopeRecallHermesAdapter(core=core)
    provider.initialize("TEST-login-session", **_session(initialize_kwargs, "desktop", user_id=LOGIN))
    try:
        provider.observe_pre_llm(
            session_id="TEST-login-session", turn_id="TEST-turn-1", user_message="TEST 第二台电脑上用绿色。"
        )
        provider.sync_turn("TEST 第二台电脑上用绿色。", "好的。", session_id="TEST-login-session")
        with sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3") as connection:
            rows = connection.execute("SELECT origin, scope_id FROM source_events WHERE role='user'").fetchall()
        assert rows and set(rows) == {("human_direct", provider._identity.owner_private_scope_id)}
    finally:
        provider.shutdown()


def test_a_session_that_binds_nothing_says_so_once_in_the_host_log_and_never_what_was_said(
    hermes_home, initialize_kwargs, caplog
):
    """The sessions of a login nobody approved write nothing, and must not do so silently, with no warning, no gap
    the host reads and doctor green.  Such a session still binds nothing; the host log names its route, its gaps
    and the approval, once."""
    _binding, core = _install(hermes_home, initialize_kwargs, local_platforms=("desktop",))
    caplog.set_level(logging.WARNING, logger="scope_recall")
    said, answer = "TEST 这句话只在会话里。", "TEST 这句回答也是。"
    provider = ScopeRecallHermesAdapter(core=core)
    provider.initialize("TEST-visitor-session", **_session(initialize_kwargs, "desktop", user_id="basic:TEST-visitor"))
    try:
        assert any(
            record.getMessage().startswith("scope-recall: session bound to no memory scope")
            for record in caplog.records
        ), "said when the session binds, before anything is said in it"
        provider.observe_pre_llm(session_id="TEST-visitor-session", turn_id="TEST-turn-1", user_message=said)
        assert provider.prefetch(said) == ""
        provider.sync_turn(said, answer, session_id="TEST-visitor-session")
        provider.on_session_switch("TEST-visitor-session-2", reason="compression")
        assert provider._identity.runtime_audience.allowed_scope_ids == frozenset() and provider._identity.read_only
        with sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3") as connection:
            assert connection.execute("SELECT count(*) FROM source_events").fetchone()[0] == 0
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.name.startswith("scope_recall") and record.levelno >= logging.WARNING
        ]
        assert len(warnings) == 1, warnings
        assert warnings[0].startswith("scope-recall: session bound to no memory scope: a desktop session")
        assert "not stored" not in warnings[0], "a capture that failed is another line"
        assert "capability_gap:audience_unmapped" in warnings[0]
        assert "--owner-login desktop=basic:TEST-visitor" in warnings[0]
        assert said not in warnings[0] and answer not in warnings[0]
    finally:
        provider.shutdown()


def test_an_unmapped_gateway_chat_says_nothing_and_a_login_cannot_break_the_line(
    hermes_home, initialize_kwargs, caplog
):
    """A gateway chat left unmapped is the owner's choice: a line for each would name its users, some by phone
    number.  And whatever a login holds stays in the one line it is named in."""
    _binding, core = _install(hermes_home, initialize_kwargs, local_platforms=("desktop",))
    caplog.set_level(logging.WARNING, logger="scope_recall")
    for session, given in (
        (
            "TEST-group-session",
            dict(
                initialize_kwargs, platform="telegram", user_id="TEST-member", chat_type="group", chat_id="TEST-group"
            ),
        ),
        (
            "TEST-visitor-session",
            _session(initialize_kwargs, "desktop", user_id="basic:TEST-visitor\nscope-recall: forged"),
        ),
    ):
        provider = ScopeRecallHermesAdapter(core=core)
        provider.initialize(session, **given)
        provider.shutdown()

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("scope_recall") and record.levelno >= logging.WARNING
    ]
    assert len(warnings) == 1 and "a desktop session" in warnings[0], warnings
    assert "\n" not in warnings[0] and "telegram" not in warnings[0]


def test_an_owner_login_is_never_a_reserved_name_in_any_case():
    """``desktop=Unknown`` is refused by the check, not passed to fail later with a traceback; ``desktop=LOCAL``
    does not become a principal of its own beside the local owner."""
    for value in ("desktop=local", "desktop=LOCAL", "tui=Unknown", "desktop=*"):
        with pytest.raises(HermesIdentityError, match="an owner login is"):
            normalize_owner_logins([value])
    assert normalize_owner_logins(["desktop=basic:alice"]) == (("desktop", "basic:alice"),)


def test_a_platform_that_names_its_chats_keeps_the_route_it_sent(hermes_home, initialize_kwargs):
    """Only a local surface that names no chat is read as a one-to-one chat.  Gateways send their own chat
    fields, a weixin DM even an empty chat id, and those routes stay exactly as sent."""
    _install(hermes_home, initialize_kwargs)
    for platform in ("weixin", "telegram", "a2a"):
        sent = bind_hermes_identity("TEST-session-1", **_session(initialize_kwargs, platform, user_id="TEST-user"))
        assert (sent.scope.chat_type, sent.scope.chat_id, sent.scope.thread_id) == ("", "", ""), platform
    named = bind_hermes_identity(
        "TEST-session-2",
        **_session(initialize_kwargs, "desktop", user_id=LOGIN, chat_type="group", chat_id="TEST-room"),
    )
    assert (named.scope.chat_type, named.scope.chat_id, named.scope.thread_id) == ("group", "TEST-room", "")


THIS_ACCOUNT = "sid:S-1-5-21-1000000001-2000000002-3000000003-1001"
OTHER_ACCOUNT = "sid:S-1-5-21-1000000001-2000000002-3000000003-1002"
CONVERSATION = "local-" + "0123456789abcdef" * 4
LOGIN_FORM = 'auth:v1:["basic","","TEST-visitor"]'


@pytest.fixture
def this_account(monkeypatch):
    """The account this gateway runs as, named as the transport names a peer the OS says runs as it."""
    monkeypatch.setattr(hermes_identity, "host_account_principal", lambda: THIS_ACCOUNT, raising=False)
    return THIS_ACCOUNT


def _conversation(initialize_kwargs, platform, *, user=THIS_ACCOUNT, chat=CONVERSATION, profile="main"):
    """What the transport sends for one conversation: the verified account, the conversation as a direct chat of its
    own, the session key the gateway built from it, and no thread."""
    kwargs = {key: value for key, value in initialize_kwargs.items() if key != "user_id"}
    return dict(
        kwargs,
        platform=platform,
        user_id=user,
        chat_type="dm",
        chat_id=chat,
        gateway_session_key=f"agent:{profile}:local:dm:{chat}",
    )


def _route(identity):
    scope = identity.scope
    return scope.user_id, scope.chat_type, scope.chat_id, scope.thread_id, scope.gateway_session_key


def _warnings(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("scope_recall") and record.levelno >= logging.WARNING
    ]


def test_a_desktop_conversation_of_this_account_is_the_owner_where_the_surface_is_approved(
    hermes_home, initialize_kwargs, this_account
):
    """The installer approved Desktop for the owner.  A conversation the transport runs binds the same scope under
    the same principal as an unnamed Desktop session, with no step of the operator's."""
    _install(hermes_home, initialize_kwargs, local_platforms=("desktop",))

    conversation = bind_hermes_identity("TEST-session-1", **_conversation(initialize_kwargs, "desktop"))
    unnamed = bind_hermes_identity("TEST-session-2", **_session(initialize_kwargs, "desktop"))

    assert _route(conversation) == ("local", "private", "local", "main", "")
    assert conversation.scope == unnamed.scope
    assert conversation.runtime_audience == unnamed.runtime_audience
    assert conversation.runtime_audience.includes_owner_private and not conversation.read_only
    assert conversation.trusted_context().source_principal == unnamed.trusted_context().source_principal


def test_what_is_said_in_a_desktop_conversation_of_the_transport_is_captured_as_the_owners(
    hermes_home, initialize_kwargs, this_account
):
    _binding, core = _install(hermes_home, initialize_kwargs, local_platforms=("desktop",))
    provider = ScopeRecallHermesAdapter(core=core)
    provider.initialize(CONVERSATION, **_conversation(initialize_kwargs, "desktop"))
    try:
        said = "TEST 桌面端换成深色主题。"
        provider.observe_pre_llm(session_id=CONVERSATION, turn_id="TEST-turn-1", user_message=said)
        provider.sync_turn(said, "好的。", session_id=CONVERSATION)
        with sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3") as connection:
            rows = connection.execute("SELECT origin, scope_id FROM source_events WHERE role='user'").fetchall()
        assert rows and set(rows) == {("human_direct", provider._identity.owner_private_scope_id)}
    finally:
        provider.shutdown()


def test_the_tui_and_the_cli_through_the_transport_bind_as_an_unnamed_session_there(
    hermes_home, initialize_kwargs, this_account
):
    """The transport runs ``hermes --tui``'s conversations and the CLI's turns too."""
    _install(hermes_home, initialize_kwargs, local_platforms=("tui",))

    for platform, route in (
        ("tui", ("local", "private", "local", "main", "")),
        ("cli", ("local", "cli", "local", "main", "")),
    ):
        conversation = bind_hermes_identity("TEST-session-1", **_conversation(initialize_kwargs, platform))
        unnamed = bind_hermes_identity("TEST-session-2", **_session(initialize_kwargs, platform))
        assert _route(conversation) == route, platform
        assert conversation.runtime_audience == unnamed.runtime_audience, platform
        assert conversation.runtime_audience.includes_owner_private and not conversation.read_only, platform


def test_a_conversation_the_transport_adopted_or_a_profile_runs_is_still_a_conversation(
    hermes_home, initialize_kwargs, this_account
):
    """A conversation from before the transport keeps its old id as its chat, and a named profile's key has its own
    namespace (``main~`` is the one named ``main``); neither makes the conversation a route."""
    _install(hermes_home, initialize_kwargs, local_platforms=("desktop",))

    for chat, profile in (("20261012_101500_a1b2c3", "main"), (CONVERSATION, "TEST-profile"), (CONVERSATION, "main~")):
        conversation = bind_hermes_identity(
            "TEST-session-1", **_conversation(initialize_kwargs, "desktop", chat=chat, profile=profile)
        )
        assert _route(conversation) == ("local", "private", "local", "main", ""), (chat, profile)
        assert conversation.runtime_audience.includes_owner_private, (chat, profile)


def test_another_account_or_a_login_through_the_transport_is_a_user_like_any_other(
    hermes_home, initialize_kwargs, this_account
):
    """The transport refuses a peer of another account and names a dashboard login in a form of its own; neither is
    the person at this machine.  Each is a one-to-one chat with itself and binds what the manifest gives it, here
    nothing."""
    _install(hermes_home, initialize_kwargs, local_platforms=("desktop",))

    for user in (OTHER_ACCOUNT, LOGIN_FORM, "basic:TEST-visitor"):
        identity = bind_hermes_identity("TEST-session-1", **_conversation(initialize_kwargs, "desktop", user=user))
        assert _route(identity) == (user, "private", user, "main", ""), user
        assert identity.runtime_audience.allowed_scope_ids == frozenset() and identity.read_only, user
        assert "capability_gap:audience_unmapped" in identity.runtime_audience.capability_gaps, user


def test_a_login_the_owner_approved_is_the_owner_through_the_transport(hermes_home, initialize_kwargs, this_account):
    owner_login = 'auth:v1:["basic","","TEST-owner"]'
    _install(hermes_home, initialize_kwargs, owner_logins=(f"desktop={owner_login}",))

    identity = bind_hermes_identity("TEST-session-1", **_conversation(initialize_kwargs, "desktop", user=owner_login))

    assert _route(identity) == (owner_login, "private", owner_login, "main", "")
    assert identity.runtime_audience.includes_owner_private and not identity.read_only


def test_this_account_on_a_surface_nobody_approved_binds_nothing_and_is_told_to_approve_the_surface(
    hermes_home, initialize_kwargs, this_account, caplog
):
    """Bound to no scope rather than refused: the account is somebody, the owner, whose surface the installer has not
    approved, as a login nobody approved binds nothing.  The line names the surface's approval, never the account."""
    _binding, core = _install(hermes_home, initialize_kwargs)
    caplog.set_level(logging.WARNING, logger="scope_recall")
    provider = ScopeRecallHermesAdapter(core=core)
    provider.initialize(CONVERSATION, **_conversation(initialize_kwargs, "desktop"))
    try:
        identity = provider._identity
        assert _route(identity) == ("local", "private", "local", "main", "")
        assert identity.runtime_audience.allowed_scope_ids == frozenset() and identity.read_only
        [said] = _warnings(caplog)
        assert said.startswith("scope-recall: session bound to no memory scope: a desktop session for local ")
        assert "approve the surface with apply-install --local-platform desktop" in said
        assert "--owner-login" not in said and THIS_ACCOUNT not in said
    finally:
        provider.shutdown()


def _store(tmp_path, initialize_kwargs):
    root = tmp_path / "TEST-shared"
    write_shared_payload(root, new_shared_payload(root, agent_id=initialize_kwargs["agent_identity"]))
    return root


def _entry(root, tmp_path, initialize_kwargs, entry_id="desk", **grants):
    """A home attached to the shared store at ``root`` with the grants its own installation would have had."""
    home = tmp_path / f"TEST-{entry_id}-home"
    home.mkdir()
    manifest = build_installation_manifest(
        home,
        agent_id=initialize_kwargs["agent_identity"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        **grants,
    )
    attach_shared_entry(root, manifest, entry_id=entry_id, display_name=f"TEST {entry_id}", now="2026-10-18T08:00:00Z")
    return dict(initialize_kwargs, hermes_home=str(home))


def test_a_shared_entry_whose_grants_approve_the_surface_binds_the_owner_through_the_transport(
    tmp_path, initialize_kwargs, this_account
):
    kwargs = _entry(_store(tmp_path, initialize_kwargs), tmp_path, initialize_kwargs, local_platforms=("desktop",))

    identity = bind_hermes_identity("TEST-session-1", **_conversation(kwargs, "desktop"))

    assert identity.entry_id == "desk"
    assert _route(identity) == ("local", "private", "local", "main", "")
    assert identity.runtime_audience.includes_owner_private and not identity.read_only


def test_a_shared_entry_given_the_surface_with_grants_binds_the_owner_through_the_transport(
    tmp_path, initialize_kwargs, this_account
):
    """What ``shared-store.md`` says to add with ``scope-recall grants``: the owner principal and the row the installer
    writes for an approved surface, into the scope the entry's CLI owner row captures into."""
    kwargs = _entry(_store(tmp_path, initialize_kwargs), tmp_path, initialize_kwargs)
    home = kwargs["hermes_home"]
    document = shared_cli.grants(instance_root=Path(home))["grants"]
    cli = next(row for row in document["audiences"] if row["kind"] == "owner_private" and row["platform"] == "cli")
    document["owner_principals"].append({"platform": "desktop", "user_id": "local"})
    document["audiences"].append(
        dict(cli, platform="desktop", user_id="local", chat_type="private", chat_id="local", gateway_session_key="")
    )
    edited = tmp_path / "TEST-grants-desk.json"
    edited.write_text(json.dumps(document), encoding="utf-8")
    assert shared_cli.grants(instance_root=Path(home), grants_from=edited, apply=True)["status"] == "applied"

    identity = bind_hermes_identity("TEST-session-1", **_conversation(kwargs, "desktop"))

    assert _route(identity) == ("local", "private", "local", "main", "")
    assert identity.runtime_audience.includes_owner_private and not identity.read_only
    assert identity.runtime_audience.capture_scope_id == cli["capture_scope_id"]


def test_a_shared_entry_is_told_to_add_its_row_with_grants_never_with_apply_install(
    tmp_path, initialize_kwargs, this_account, caplog
):
    """``apply-install`` refuses an entry of a shared store: its grants are in the store's manifest."""
    kwargs = _entry(_store(tmp_path, initialize_kwargs), tmp_path, initialize_kwargs)
    caplog.set_level(logging.WARNING, logger="scope_recall")
    for user in (THIS_ACCOUNT, LOGIN_FORM):
        provider = ScopeRecallHermesAdapter()
        provider.initialize("TEST-session-1", **_conversation(kwargs, "desktop", user=user))
        provider.shutdown()

    owner, login = _warnings(caplog)
    assert "scope-recall grants" in owner and "(desktop, local)" in owner and "apply-install" not in owner
    assert "scope-recall grants" in login and f"(desktop, {LOGIN_FORM})" in login and "apply-install" not in login
    with pytest.raises(HermesIdentityError, match="scope-recall grants") as refused:
        bind_hermes_identity("TEST-session-2", **_session(kwargs, "desktop"))
    assert "apply-install" not in str(refused.value)


def test_routes_that_are_no_conversation_of_the_transport_keep_their_routes_and_grants(
    hermes_home, initialize_kwargs, this_account
):
    """The CLI that runs its agent in its own process, a dashboard login that names no chat, a gateway's direct chat,
    and a chat that only looks like a conversation of the transport keep their routes and their grants."""
    workspace = initialize_kwargs["agent_workspace"]
    base = build_installation_manifest(
        hermes_home,
        agent_id=initialize_kwargs["agent_identity"],
        agent_workspace=workspace,
        local_platforms=("desktop",),
    )
    direct = audience_entry(
        platform="telegram",
        user_id="TEST-tg",
        chat_type="dm",
        chat_id="TEST-tg",
        thread_id="",
        gateway_session_key="",
        agent_workspace=workspace,
        allowed_scope_ids=["scope:TEST-telegram-dm"],
        writable_scope_ids=["scope:TEST-telegram-dm"],
        capture_scope_id="scope:TEST-telegram-dm",
        kind="conversation",
    )
    _install(
        hermes_home, initialize_kwargs, owner_principals=base.owner_principals, audiences=[*base.audiences, direct]
    )

    cli = bind_hermes_identity("TEST-session-1", **_session(initialize_kwargs, "cli"))
    login = bind_hermes_identity(
        "TEST-session-2", **_session(initialize_kwargs, "desktop", user_id="basic:TEST-visitor")
    )
    telegram = bind_hermes_identity(
        "TEST-session-3",
        **dict(
            initialize_kwargs,
            platform="telegram",
            user_id="TEST-tg",
            chat_type="dm",
            chat_id="TEST-tg",
            gateway_session_key="agent:main:telegram:dm:TEST-tg",
        ),
    )

    assert _route(cli) == ("local", "cli", "local", "main", "") and cli.runtime_audience.includes_owner_private
    assert _route(login) == ("basic:TEST-visitor", "private", "basic:TEST-visitor", "main", "") and login.read_only
    assert _route(telegram) == ("TEST-tg", "dm", "TEST-tg", "", "agent:main:telegram:dm:TEST-tg")
    assert telegram.runtime_audience.allowed_scope_ids == frozenset({"scope:TEST-telegram-dm"})
    # Only the transport's own key, on a local surface, makes a chat one of its conversations.
    looks_alike = f"agent:main:local:dm:{CONVERSATION}"
    for platform, key in (
        ("desktop", ""),
        ("desktop", f"agent:main:telegram:dm:{CONVERSATION}"),
        ("telegram", looks_alike),
        ("a2a", looks_alike),
        ("cron", looks_alike),
    ):
        sent = bind_hermes_identity(
            "TEST-session-4", **dict(_conversation(initialize_kwargs, platform), gateway_session_key=key)
        )
        assert _route(sent) == (THIS_ACCOUNT, "dm", CONVERSATION, "", key), (platform, key)
        assert sent.runtime_audience.allowed_scope_ids == frozenset(), (platform, key)


def test_a_session_switch_in_a_conversation_of_the_transport_stays_the_owners(
    hermes_home, initialize_kwargs, this_account
):
    _install(hermes_home, initialize_kwargs, local_platforms=("desktop",))
    first = bind_hermes_identity(CONVERSATION, **_conversation(initialize_kwargs, "desktop"))

    second = switch_hermes_identity(first, "TEST-compressed-session")

    assert second.scope == first.scope and second.runtime_audience == first.runtime_audience
    assert second.runtime_audience.includes_owner_private and not second.read_only


def test_this_process_s_account_is_named_as_the_transport_names_a_peer_that_runs_as_it():
    named = hermes_identity.host_account_principal()

    if os.name == "nt":
        whoami = os.path.join(os.environ["SystemRoot"], "System32", "whoami.exe")
        listed = subprocess.run([whoami, "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True)
        assert named == "sid:" + listed.stdout.strip().rsplit(",", 1)[1].strip('"')
    else:
        assert named == f"uid:{os.getuid()}"


def _local_sessions_check(report):
    return [check for check in report.checks if check["name"] == "local_sessions"]


def test_doctor_names_a_surface_whose_owner_sessions_bind_no_scope_until_one_binds(
    hermes_home, initialize_kwargs, this_account
):
    """The line in the host's log is read by no one.  ``doctor`` names the surface and what approves it, never who
    spoke, and asks for a look rather than an action: only the owner knows whether everyone who reaches a surface
    without logging in is the owner.  A login nobody approved is a visitor's, and no finding."""
    _binding, core = _install(hermes_home, initialize_kwargs)
    for user in (LOGIN_FORM, THIS_ACCOUNT):
        provider = ScopeRecallHermesAdapter(core=core)
        provider.initialize(CONVERSATION, **_conversation(initialize_kwargs, "desktop", user=user))
        provider.shutdown()
        if user == LOGIN_FORM:
            assert not _local_sessions_check(doctor.run_doctor(host="hermes", instance_root=hermes_home))

    report = doctor.run_doctor(host="hermes", instance_root=hermes_home)
    [check] = _local_sessions_check(report)
    assert "local_session_unbound" in report.capability_gaps
    assert check["result"] == "unbound" and check["detail"].startswith("desktop:")
    assert "approve the surface with apply-install --local-platform desktop" in check["detail"]
    shown = json.dumps(report.to_dict())
    assert THIS_ACCOUNT not in shown and "TEST-visitor" not in shown
    classified = doctor.DoctorReport(host="hermes", status="degraded", capability_gaps=["local_session_unbound"])
    doctor._classify_status(classified)
    assert classified.status == "attention"

    manifest = load_installation_manifest(hermes_home)
    approved = approve_local_platforms(manifest, ["desktop"], agent_workspace=initialize_kwargs["agent_workspace"])
    write_installation_manifest(approved)
    provider = ScopeRecallHermesAdapter(core=core)
    provider.initialize("TEST-next-session", **_conversation(initialize_kwargs, "desktop"))
    provider.shutdown()
    report = doctor.run_doctor(host="hermes", instance_root=hermes_home)
    assert "local_session_unbound" not in report.capability_gaps and not _local_sessions_check(report)


def test_doctor_names_a_surface_whose_unnamed_sessions_are_refused(hermes_home, initialize_kwargs):
    _binding, core = _install(hermes_home, initialize_kwargs)
    provider = ScopeRecallHermesAdapter(core=core)
    with pytest.raises(HermesIdentityError, match="--local-platform tui"):
        provider.initialize("TEST-session-1", **_session(initialize_kwargs, "tui"))

    report = doctor.run_doctor(host="hermes", instance_root=hermes_home)

    [check] = _local_sessions_check(report)
    assert "local_session_unbound" in report.capability_gaps and check["detail"].startswith("tui:")
    assert "approve the surface with apply-install --local-platform tui" in check["detail"]


def _ended_record(folder, *, entry_id, platform, pid):
    """A dead process record, including ids beyond POSIX's signed native int range."""
    folder.mkdir(exist_ok=True)
    path = folder / f"{pid}.json"
    session = {"entry_id": entry_id, "platform": platform, "gaps": ["capability_gap:audience_unmapped"], "since": ""}
    record = {"schema": "scope-recall.unbound-sessions.v1", "pid": pid, "start_token": None, "sessions": [session]}
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


@pytest.mark.parametrize("ended_pid", [0x7FFFFFFF, 0xFFFFFFF0])
def test_doctor_reads_what_live_processes_recorded_for_its_own_entry(
    tmp_path, initialize_kwargs, this_account, ended_pid
):
    root = _store(tmp_path, initialize_kwargs)
    desk = _entry(root, tmp_path, initialize_kwargs, entry_id="desk")
    lab = _entry(root, tmp_path, initialize_kwargs, entry_id="lab")
    provider = ScopeRecallHermesAdapter()
    provider.initialize(CONVERSATION, **_conversation(desk, "desktop"))
    provider.shutdown()
    _ended_record(root / "unbound-sessions", entry_id="lab", platform="tui", pid=ended_pid)

    at_desk = doctor.run_doctor(host="hermes", instance_root=desk["hermes_home"])
    at_lab = doctor.run_doctor(host="hermes", instance_root=lab["hermes_home"])

    [check] = _local_sessions_check(at_desk)
    assert check["detail"].startswith("desktop:") and "scope-recall grants" in check["detail"]
    assert "apply-install" not in check["detail"]
    assert not _local_sessions_check(at_lab), "another entry's record, and one whose process ended"


@pytest.mark.parametrize("ended_pid", [0x7FFFFFFF, 0xFFFFFFF0])
def test_a_record_a_process_left_when_it_ended_is_retired_when_this_one_writes(
    hermes_home, initialize_kwargs, this_account, ended_pid
):
    """A record that cannot be read is left: no pass can tell whose it is."""
    _binding, core = _install(hermes_home, initialize_kwargs)
    folder = hermes_home / "scope-recall" / "unbound-sessions"
    ended = _ended_record(folder, entry_id="", platform="tui", pid=ended_pid)
    unreadable = folder / "TEST-unreadable.json"
    unreadable.write_text("{", encoding="utf-8")

    provider = ScopeRecallHermesAdapter(core=core)
    provider.initialize(CONVERSATION, **_conversation(initialize_kwargs, "desktop"))
    provider.shutdown()

    assert (folder / f"{os.getpid()}.json").is_file()
    assert not ended.exists() and unreadable.exists()


@pytest.mark.parametrize("failure", ["partial", "replace"])
@pytest.mark.parametrize("next_platform", ["desktop", "tui"])
def test_an_unbound_record_write_is_retried_on_the_next_session_event(tmp_path, monkeypatch, failure, next_platform):
    target = tmp_path / "unbound-sessions" / f"{os.getpid()}.json"
    attempts = []
    write_text, replace_file = Path.write_text, os.replace

    def write_partial(path, *args, **kwargs):
        if path == target.with_suffix(".json.partial"):
            attempts.append(path)
            if len(attempts) == 1:
                raise PermissionError("TEST sharing violation")
        return write_text(path, *args, **kwargs)

    def replace_partial(source, destination):
        if destination == target:
            attempts.append(destination)
            if len(attempts) == 1:
                raise PermissionError("TEST sharing violation")
        return replace_file(source, destination)

    if failure == "partial":
        monkeypatch.setattr(Path, "write_text", write_partial)
    else:
        monkeypatch.setattr(os, "replace", replace_partial)
    gaps = ("capability_gap:audience_unmapped",)
    unbound_sessions.note_owner_session(tmp_path, entry_id="TEST-entry", platform="desktop", gaps=gaps)
    assert not target.exists()
    assert unbound_sessions.owner_sessions_unbound(tmp_path, entry_id="TEST-entry") == []

    unbound_sessions.note_owner_session(
        tmp_path, entry_id="TEST-entry", platform=next_platform, gaps=gaps if next_platform == "desktop" else None
    )

    [record] = unbound_sessions.owner_sessions_unbound(tmp_path, entry_id="TEST-entry")
    assert record["platform"] == "desktop" and record["gaps"] == list(gaps)
    assert len(attempts) == 2
    unbound_sessions.note_owner_session(tmp_path, entry_id="TEST-entry", platform="desktop", gaps=gaps)
    assert len(attempts) == 2, "a persisted session needs no further write"


@pytest.mark.parametrize("next_platform", ["desktop", "tui"])
def test_a_resolved_record_deletion_is_retried_on_the_next_session_event(tmp_path, monkeypatch, next_platform):
    target = tmp_path / "unbound-sessions" / f"{os.getpid()}.json"
    unbound_sessions.note_owner_session(
        tmp_path, entry_id="TEST-entry", platform="desktop", gaps=("capability_gap:audience_unmapped",)
    )
    other = target.with_name("TEST-other.json")
    other.write_text("{", encoding="utf-8")
    attempts = []
    unlink = Path.unlink

    def unlink_record(path, *args, **kwargs):
        if path == target:
            attempts.append(path)
            if len(attempts) == 1:
                raise PermissionError("TEST sharing violation")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_record)
    unbound_sessions.note_owner_session(tmp_path, entry_id="TEST-entry", platform="desktop", gaps=None)
    assert target.is_file()
    assert unbound_sessions.owner_sessions_unbound(tmp_path, entry_id="TEST-entry")

    unbound_sessions.note_owner_session(tmp_path, entry_id="TEST-entry", platform=next_platform, gaps=None)

    assert not target.exists() and other.is_file() and target.parent.is_dir()
    assert unbound_sessions.owner_sessions_unbound(tmp_path, entry_id="TEST-entry") == []
    assert len(attempts) == 2


def test_resolving_a_session_never_removes_a_directory_at_the_record_path(tmp_path):
    target = tmp_path / "unbound-sessions" / f"{os.getpid()}.json"
    unbound_sessions.note_owner_session(
        tmp_path, entry_id="TEST-entry", platform="desktop", gaps=("capability_gap:audience_unmapped",)
    )
    target.unlink()
    target.mkdir()

    for _ in range(2):
        unbound_sessions.note_owner_session(tmp_path, entry_id="TEST-entry", platform="desktop", gaps=None)

    assert target.is_dir()


def test_a_pending_update_keeps_other_entries_and_the_first_seen_time(tmp_path, monkeypatch):
    gaps = ("capability_gap:audience_unmapped",)
    for entry in ("TEST-desk", "TEST-lab"):
        unbound_sessions.note_owner_session(tmp_path, entry_id=entry, platform="desktop", gaps=gaps)
    [before] = unbound_sessions.owner_sessions_unbound(tmp_path, entry_id="TEST-desk")
    replace_file = os.replace
    attempts = []

    def replace_partial(source, destination):
        attempts.append(destination)
        if len(attempts) == 1:
            raise PermissionError("TEST sharing violation")
        return replace_file(source, destination)

    monkeypatch.setattr(os, "replace", replace_partial)
    updated = (*gaps, "capability_gap:no_allowed_scope")
    unbound_sessions.note_owner_session(tmp_path, entry_id="TEST-desk", platform="desktop", gaps=updated)
    assert unbound_sessions.owner_sessions_unbound(tmp_path, entry_id="TEST-desk") == [before]

    unbound_sessions.note_owner_session(tmp_path, entry_id="TEST-lab", platform="desktop", gaps=gaps)

    [after] = unbound_sessions.owner_sessions_unbound(tmp_path, entry_id="TEST-desk")
    assert after == dict(before, gaps=list(updated))
    [other] = unbound_sessions.owner_sessions_unbound(tmp_path, entry_id="TEST-lab")
    assert other["gaps"] == list(gaps) and len(attempts) == 2


def test_doctor_points_a_shared_entry_s_unverified_owner_row_at_grants(tmp_path, initialize_kwargs):
    root = _store(tmp_path, initialize_kwargs)
    home = tmp_path / "TEST-desk-home"
    home.mkdir()
    approved = build_installation_manifest(
        home,
        agent_id=initialize_kwargs["agent_identity"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        owner_logins=(f"desktop={LOGIN}",),
    )
    principals = tuple(item for item in approved.owner_principals if item["user_id"] != LOGIN)
    attach_shared_entry(
        root,
        replace(approved, owner_principals=principals),
        entry_id="desk",
        display_name="TEST desk",
        now="2026-10-18T08:00:00Z",
    )

    report = doctor.run_doctor(host="hermes", instance_root=home)

    [check] = [check for check in report.checks if check["name"] == "audiences"]
    assert check["detail"].startswith("desktop=1:") and "scope-recall grants" in check["detail"]
    assert "apply-install" not in check["detail"]
