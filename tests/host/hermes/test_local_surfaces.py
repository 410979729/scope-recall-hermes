"""A host surface that names no user is refused until the installer approves it as the owner's own.

Hermes Desktop's chat panel and ``hermes --tui`` hand a memory provider the dashboard login as
``user_id``, and nothing when nobody logged in (``tui_gateway/server.py``: platform ``desktop`` or
``tui``).  2.x minted a Desktop principal for that case.  3.x binds only what the installation
manifest approves, so every such session failed with ``user principal required for non-cli
platform`` and the provider never initialised (issue #94).  The approval is the installer's:
``apply-install --local-platform desktop``.
"""
from __future__ import annotations

from dataclasses import replace
import logging
import sqlite3

import pytest

from scope_recall.adapters.hermes import (
    HermesIdentityError,
    ScopeRecallHermesAdapter,
    bind_hermes_identity,
    install_hermes_scope_recall,
)
from scope_recall.adapters.hermes.audiences import normalize_local_platforms, normalize_owner_logins
from scope_recall.adapters.hermes.identity import switch_hermes_identity
from scope_recall.adapters.hermes.installation import (
    approve_local_platforms,
    build_installation_manifest,
    load_installation_manifest,
    manifest_payload,
    unapproved_local_platforms,
    write_installation_manifest,
)


def _install(hermes_home, initialize_kwargs, **options):
    return install_hermes_scope_recall(
        hermes_home, agent_id=initialize_kwargs["agent_identity"], agent_workspace=initialize_kwargs["agent_workspace"],
        test_mode=False, **options)


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

    assert (desktop.scope.user_id, desktop.scope.chat_type, desktop.scope.chat_id, desktop.scope.thread_id) \
        == ("local", "private", "local", "main")
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
        provider.observe_pre_llm(session_id="TEST-desktop-session", turn_id="TEST-turn-1", user_message="TEST 桌面端的配色用蓝色。")
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

    identity = bind_hermes_identity("TEST-session-1", **_session(initialize_kwargs, "desktop", user_id="basic:TEST-visitor"))

    assert (identity.scope.user_id, identity.scope.chat_type, identity.scope.chat_id) \
        == ("basic:TEST-visitor", "private", "basic:TEST-visitor")
    assert identity.runtime_audience.allowed_scope_ids == frozenset()
    assert "capability_gap:audience_unmapped" in identity.runtime_audience.capability_gaps
    assert identity.read_only


def test_a_host_that_names_its_user_local_on_a_surface_nobody_approved_keeps_no_route(hermes_home, initialize_kwargs):
    """Only a login is routed as a one-to-one chat with itself (#175).  ``local`` sent by the host on a surface the
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
    before = build_installation_manifest(hermes_home, agent_id=initialize_kwargs["agent_identity"], agent_workspace=workspace)
    assert unapproved_local_platforms(before, ["desktop", "tui"], agent_workspace=workspace) == ("desktop", "tui")

    after = approve_local_platforms(before, ["desktop"], agent_workspace=workspace)

    assert after.to_binding() == before.to_binding(), "the store is bound to the same scopes"
    assert after.audiences[:len(before.audiences)] == before.audiences and len(after.audiences) == len(before.audiences) + 1
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
    row = dict(platform=platform, user_id=login, chat_type="private", chat_id=login, thread_id="main",
               gateway_session_key="", agent_workspace=initialize_kwargs["agent_workspace"],
               allowed_scope_ids=[owner], writable_scope_ids=[owner], capture_scope_id=owner, kind="owner_private")
    write_installation_manifest(replace(
        manifest, owner_principals=(*manifest.owner_principals, {"platform": platform, "user_id": login}),
        audiences=(*manifest.audiences, row)))


def test_a_login_the_owner_approved_is_the_owner_on_that_surface_and_nowhere_else(hermes_home, initialize_kwargs):
    """#175: Hermes passes a dashboard login as ``user_id`` and no chat at all.  The session kept that empty
    route, which no owner row can name (an owner_private row is an explicit private chat), so even a login
    the owner approved bound nothing.  There a session is a one-to-one chat with the login it names."""
    _install(hermes_home, initialize_kwargs)
    _approve_login_by_hand(hermes_home, initialize_kwargs)

    login = bind_hermes_identity("TEST-session-1", **_session(initialize_kwargs, "desktop", user_id=LOGIN))
    cli = bind_hermes_identity("TEST-session-2", **_session(initialize_kwargs, "cli"))

    assert (login.scope.user_id, login.scope.chat_type, login.scope.chat_id, login.scope.thread_id) \
        == (LOGIN, "private", LOGIN, "main")
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
        provider.observe_pre_llm(session_id="TEST-login-session", turn_id="TEST-turn-1", user_message="TEST 第二台电脑上用绿色。")
        provider.sync_turn("TEST 第二台电脑上用绿色。", "好的。", session_id="TEST-login-session")
        with sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3") as connection:
            rows = connection.execute("SELECT origin, scope_id FROM source_events WHERE role='user'").fetchall()
        assert rows and set(rows) == {("human_direct", provider._identity.owner_private_scope_id)}
    finally:
        provider.shutdown()


def test_a_session_that_binds_nothing_says_so_once_in_the_host_log_and_never_what_was_said(
        hermes_home, initialize_kwargs, caplog):
    """#175: the sessions of a login nobody approved wrote nothing for days, and nothing said so: no warning,
    no gap the host reads, doctor green.  Such a session still binds nothing; the host log now names its
    route, its gaps and the approval, once."""
    _binding, core = _install(hermes_home, initialize_kwargs, local_platforms=("desktop",))
    caplog.set_level(logging.WARNING, logger="scope_recall")
    said, answer = "TEST 这句话只在会话里。", "TEST 这句回答也是。"
    provider = ScopeRecallHermesAdapter(core=core)
    provider.initialize("TEST-visitor-session", **_session(initialize_kwargs, "desktop", user_id="basic:TEST-visitor"))
    try:
        assert any(record.getMessage().startswith("scope-recall: session bound to no memory scope")
                   for record in caplog.records), "said when the session binds, before anything is said in it"
        provider.observe_pre_llm(session_id="TEST-visitor-session", turn_id="TEST-turn-1", user_message=said)
        assert provider.prefetch(said) == ""
        provider.sync_turn(said, answer, session_id="TEST-visitor-session")
        provider.on_session_switch("TEST-visitor-session-2", reason="compression")
        assert provider._identity.runtime_audience.allowed_scope_ids == frozenset() and provider._identity.read_only
        with sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3") as connection:
            assert connection.execute("SELECT count(*) FROM source_events").fetchone()[0] == 0
        warnings = [record.getMessage() for record in caplog.records
                    if record.name.startswith("scope_recall") and record.levelno >= logging.WARNING]
        assert len(warnings) == 1, warnings
        assert warnings[0].startswith("scope-recall: session bound to no memory scope: a desktop session")
        assert "not stored" not in warnings[0], "a capture that failed is another line"
        assert "capability_gap:audience_unmapped" in warnings[0]
        assert "--owner-login desktop=basic:TEST-visitor" in warnings[0]
        assert said not in warnings[0] and answer not in warnings[0]
    finally:
        provider.shutdown()


def test_an_unmapped_gateway_chat_says_nothing_and_a_login_cannot_break_the_line(hermes_home, initialize_kwargs, caplog):
    """A gateway chat left unmapped is the owner's choice: a line for each would name its users, some by phone
    number.  And whatever a login holds stays in the one line it is named in (review of 3.4.10)."""
    _binding, core = _install(hermes_home, initialize_kwargs, local_platforms=("desktop",))
    caplog.set_level(logging.WARNING, logger="scope_recall")
    for session, given in (("TEST-group-session", dict(initialize_kwargs, platform="telegram", user_id="TEST-member",
                                                         chat_type="group", chat_id="TEST-group")),
                           ("TEST-visitor-session", _session(initialize_kwargs, "desktop",
                                                             user_id="basic:TEST-visitor\nscope-recall: forged"))):
        provider = ScopeRecallHermesAdapter(core=core)
        provider.initialize(session, **given)
        provider.shutdown()

    warnings = [record.getMessage() for record in caplog.records
                if record.name.startswith("scope_recall") and record.levelno >= logging.WARNING]
    assert len(warnings) == 1 and "a desktop session" in warnings[0], warnings
    assert "\n" not in warnings[0] and "telegram" not in warnings[0]


def test_an_owner_login_is_never_a_reserved_name_in_any_case():
    """``desktop=Unknown`` passed the check and failed later with a traceback; ``desktop=LOCAL`` became a principal
    of its own beside the local owner (review of 3.4.10)."""
    for value in ("desktop=local", "desktop=LOCAL", "tui=Unknown", "desktop=*"):
        with pytest.raises(HermesIdentityError, match="an owner login is"):
            normalize_owner_logins([value])
    assert normalize_owner_logins(["desktop=basic:alice"]) == (("desktop", "basic:alice"),)


def test_a_platform_that_names_its_chats_keeps_the_route_it_sent(hermes_home, initialize_kwargs):
    """Only a local surface that names no chat is read as a one-to-one chat.  Gateways send their own chat
    fields, a weixin DM even an empty chat id (#124), and those routes stay exactly as sent."""
    _install(hermes_home, initialize_kwargs)
    for platform in ("weixin", "telegram", "a2a"):
        sent = bind_hermes_identity("TEST-session-1", **_session(initialize_kwargs, platform, user_id="TEST-user"))
        assert (sent.scope.chat_type, sent.scope.chat_id, sent.scope.thread_id) == ("", "", ""), platform
    named = bind_hermes_identity("TEST-session-2", **_session(initialize_kwargs, "desktop", user_id=LOGIN,
                                                              chat_type="group", chat_id="TEST-room"))
    assert (named.scope.chat_type, named.scope.chat_id, named.scope.thread_id) == ("group", "TEST-room", "")
