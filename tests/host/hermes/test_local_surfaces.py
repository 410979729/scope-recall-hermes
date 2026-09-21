"""A host surface that names no user is refused until the installer approves it as the owner's own.

Hermes Desktop's chat panel and ``hermes --tui`` hand a memory provider the dashboard login as
``user_id``, and nothing when nobody logged in (``tui_gateway/server.py``: platform ``desktop`` or
``tui``).  2.x minted a Desktop principal for that case.  3.x binds only what the installation
manifest approves, so every such session failed with ``user principal required for non-cli
platform`` and the provider never initialised (issue #94).  The approval is the installer's:
``apply-install --local-platform desktop``.
"""
from __future__ import annotations

import sqlite3

import pytest

from scope_recall.adapters.hermes import (
    HermesIdentityError,
    ScopeRecallHermesAdapter,
    bind_hermes_identity,
    install_hermes_scope_recall,
)
from scope_recall.adapters.hermes.audiences import normalize_local_platforms
from scope_recall.adapters.hermes.identity import switch_hermes_identity
from scope_recall.adapters.hermes.installation import (
    approve_local_platforms,
    build_installation_manifest,
    manifest_payload,
    unapproved_local_platforms,
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
    no local route, and no scope unless an audience names it."""
    _install(hermes_home, initialize_kwargs, local_platforms=("desktop",))

    identity = bind_hermes_identity("TEST-session-1", **_session(initialize_kwargs, "desktop", user_id="basic:TEST-visitor"))

    assert (identity.scope.user_id, identity.scope.chat_type, identity.scope.chat_id) == ("basic:TEST-visitor", "", "")
    assert identity.runtime_audience.allowed_scope_ids == frozenset()
    assert "capability_gap:audience_unmapped" in identity.runtime_audience.capability_gaps
    assert identity.read_only


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
