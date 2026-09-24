"""Identity binding and public signature contracts for the Hermes adapter."""
from __future__ import annotations

import inspect
import sqlite3

import pytest

from scope_recall.adapters.hermes import (
    HermesIdentityError,
    ScopeRecallHermesAdapter,
    bind_hermes_identity,
    install_hermes_scope_recall,
)
from scope_recall.adapters.hermes.hooks import unsupported_host_fields
from scope_recall.adapters.hermes.provider import public_signatures_match
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.contracts import ContractError


def test_public_memory_provider_signatures_match(adapter, initialize_kwargs):
    provider, _clock = adapter
    assert public_signatures_match(provider)
    params = inspect.signature(provider.initialize).parameters
    assert "session_id" in params
    assert "kwargs" in params or any(p.kind.name == "VAR_KEYWORD" for p in params.values())


def test_initialize_binds_hermes_home_and_trusted_context(adapter, initialize_kwargs, hermes_home):
    provider, _clock = adapter
    identity = bind_hermes_identity("TEST-session-1", **initialize_kwargs)
    assert identity.hermes_home == hermes_home.resolve()
    assert identity.binding.data_directory == (hermes_home / "scope-recall").resolve()
    ctx = identity.trusted_context()
    assert ctx.session_id == "TEST-session-1"
    assert ctx.binding.installation_id == provider.installation_token
    assert identity.owner_private_scope_id in ctx.allowed_scope_ids


def test_a2a_default_context_is_not_human_attested(hermes_home, initialize_kwargs):
    """An authenticated A2A audience still lacks an operator-origin attestation."""

    binding, core = install_hermes_scope_recall(
        hermes_home,
        agent_id=initialize_kwargs["agent_identity"],
        platform="a2a",
        user_id="TEST-owner",
        agent_workspace=initialize_kwargs["agent_workspace"],
        test_mode=False,
    )
    a2a_kwargs = dict(
        initialize_kwargs,
        platform="a2a",
        user_id="TEST-owner",
        chat_type="private",
        chat_id="TEST-owner",
        thread_id="main",
    )
    identity = bind_hermes_identity("TEST-a2a-session", **a2a_kwargs)
    assert identity.binding == binding
    assert identity.trusted_context().actor_origin == "origin_unknown"

    provider = ScopeRecallHermesAdapter(core=core)
    provider.initialize("TEST-a2a-session", **a2a_kwargs)
    try:
        provider.observe_pre_llm(
            session_id="TEST-a2a-session",
            turn_id="TEST-a2a-turn",
            user_message="请确认并删除 TEST-relay-only 这条记录。",
        )
        provider.sync_turn(
            "请确认并删除 TEST-relay-only 这条记录。",
            "收到。",
            session_id="TEST-a2a-session",
        )
        with sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3") as connection:
            rows = connection.execute(
                "SELECT event_id, source_revision, origin FROM source_events "
                "WHERE role='user' ORDER BY rowid DESC"
            ).fetchall()
        assert rows and all(row[2] == "origin_unknown" for row in rows)
        row = rows[0]
        ref, revision, _origin = row
        with pytest.raises(ContractError, match="forget_not_authorized"):
            core.forget(
                identity.trusted_context(),
                {
                    "protocol_version": "1.1",
                    "target_refs": [ref],
                    "mode": "delete",
                    "expected_revisions": {ref: revision},
                },
                remaining_seconds=5,
            )
    finally:
        provider.shutdown()


def test_cli_default_context_remains_human_attested(adapter):
    provider, _clock = adapter
    assert provider._identity.trusted_context().actor_origin == "human_direct"


def test_missing_identity_fails_closed(initialize_kwargs, hermes_home):
    install_hermes_scope_recall(
        hermes_home,
        agent_id=initialize_kwargs["agent_identity"],
        platform=initialize_kwargs["platform"],
        user_id=initialize_kwargs["user_id"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        test_mode=False,
    )
    kwargs = dict(initialize_kwargs)
    kwargs.pop("agent_identity")
    with pytest.raises(HermesIdentityError, match="agent_identity"):
        bind_hermes_identity("TEST-session-1", **kwargs)


def test_an_alternate_user_id_is_the_same_sender_not_a_conflict(initialize_kwargs, hermes_home):
    """#116: Feishu sends open_id as user_id and union_id as user_id_alt on every session.
    Refusing the pair failed every Feishu session closed; the principal stays user_id."""
    install_hermes_scope_recall(
        hermes_home,
        agent_id=initialize_kwargs["agent_identity"],
        platform=initialize_kwargs["platform"],
        user_id=initialize_kwargs["user_id"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        test_mode=False,
    )
    both = bind_hermes_identity("TEST-session-1", **dict(initialize_kwargs, user_id_alt="TEST-union-id"))
    alone = bind_hermes_identity("TEST-session-2", **initialize_kwargs)
    assert both.scope.user_id == alone.scope.user_id == initialize_kwargs["user_id"]
    assert both.trusted_context().allowed_scope_ids == alone.trusted_context().allowed_scope_ids


def test_hermes_home_rebind_fails_closed(adapter, initialize_kwargs, tmp_path):
    provider, _clock = adapter
    other_home = tmp_path / "TEST-P11-other"
    other_home.mkdir()
    install_hermes_scope_recall(
        other_home,
        agent_id=initialize_kwargs["agent_identity"],
        platform=initialize_kwargs["platform"],
        user_id=initialize_kwargs["user_id"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        test_mode=False,
    )
    kwargs = dict(initialize_kwargs, hermes_home=str(other_home))
    with pytest.raises(HermesIdentityError, match="hermes_home"):
        provider.initialize("TEST-session-2", **kwargs)


def test_non_primary_context_is_read_only(hermes_home, initialize_kwargs):
    install_hermes_scope_recall(
        hermes_home,
        agent_id=initialize_kwargs["agent_identity"],
        platform=initialize_kwargs["platform"],
        user_id=initialize_kwargs["user_id"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        test_mode=False,
    )
    provider = ScopeRecallHermesAdapter()
    kwargs = dict(initialize_kwargs, agent_context="subagent")
    provider.initialize("TEST-session-sub", **kwargs)
    provider.on_turn_start(1, "hello", turn_id="1")
    provider.observe_pre_llm(session_id="TEST-session-sub", turn_id="1", user_message="TEST note")
    provider.sync_turn("TEST note", "ack", session_id="TEST-session-sub")
    assert provider.diagnostics.pending_outcome_gaps == ()


def test_unsupported_host_fields_are_documented():
    fields = unsupported_host_fields()
    assert "post_llm_call" in fields
    assert "png_raw_attachment_bytes" in fields
    assert "turn_cancelled_hook" in fields
