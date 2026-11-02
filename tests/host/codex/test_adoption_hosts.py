"""Every shared host reaches owner-bound adoption through its ordinary capture boundary."""

import pytest
from scope_recall.adapters import host_identity
from scope_recall.adapters.clients import CodexHookHandler
from scope_recall.adapters.clients.identity import resolve_runtime_audience
from scope_recall.core.adoption_storage import adoption_context
from scope_recall.core.worker_adoption import prepare_adoptions

from tests.contract.test_adoption import PLAN, model
from tests.host.codex.test_merged_host_boundaries import client
from tests.host.codex.test_shared_client import _hermes, store


def locate(core, ctx, clock):
    assert ctx.owner_resolver.func is host_identity.entry_owner
    with core.storage.read(ctx) as tx:
        row = (
            tx._check()
            .execute(
                "SELECT event_id,source_revision FROM source_events WHERE content=? ORDER BY source_id DESC LIMIT 1",
                ("好，就用方案二",),
            )
            .fetchone()
        )
        assert row is not None
        owner = tx.source(*row)
        assert owner.session_id == ctx.session_id
        snapshot = adoption_context(tx, owner)
        assert snapshot is not None and snapshot.assistant.event["content"] == PLAN
        assert snapshot.assistant.entry_id == owner.entry_id == ctx.entry_id
    judge, calls = model()
    result = prepare_adoptions(core.storage, ctx, (owner,), judge, clock=clock, remaining_seconds=10)
    assert len(result) == 1 and result[0].reason is None
    assert calls[0]["task"] == "locate_adoption"


def test_client_capture_reaches_adoption_with_public_owner_and_session(client, tmp_path, monkeypatch):
    _root, home, host = client
    hook = CodexHookHandler.from_home(str(home), host)
    monkeypatch.setattr(hook, "ensure_runtime", lambda *a, **k: None)
    monkeypatch.setattr(hook, "launch_worker", lambda *a, **k: None)

    def send(kind, turn, **fields):
        hook.handle_payload(
            dict(
                hook_event_name=kind,
                session_id="TEST-adoption",
                turn_id=turn,
                prompt_id=turn,
                generation_id=turn,
                cwd=str(tmp_path),
                **fields,
            )
        )

    try:
        send("UserPromptSubmit", "TEST-1", prompt="TEST 请给两个配置方案")
        send("Stop", "TEST-1", last_assistant_message=PLAN)
        send("UserPromptSubmit", "TEST-2", prompt="好，就用方案二")
        audience = resolve_runtime_audience(hook.config, None)
        ctx = hook.context(audience, "TEST-adoption", "human_direct")
        locate(hook.core, ctx, hook.clock)
    finally:
        hook.close()


def test_hermes_capture_reaches_adoption_with_public_owner_and_session(store):
    _root, homes, _client, _scope = store
    provider = _hermes(homes["agent3"])
    try:
        provider.observe_pre_llm(session_id="TEST-adoption", turn_id="TEST-1", user_message="TEST 请给两个配置方案")
        provider.sync_turn("TEST 请给两个配置方案", PLAN, session_id="TEST-adoption")
        provider.observe_pre_llm(session_id="TEST-adoption", turn_id="TEST-2", user_message="好，就用方案二")
        ctx = provider._require_identity().trusted_context(session_id="TEST-adoption")
        locate(provider._core, ctx, provider._core.clock)
    finally:
        provider.shutdown()


def test_hermes_and_clients_share_session_mapping(store, monkeypatch):
    from scope_recall.adapters.clients import identity as clients
    from scope_recall.adapters.hermes import identity as hermes

    assert getattr(hermes, "session_identity", None) is clients.session_identity
    _root, homes, _client, _scope = store
    provider = _hermes(homes["agent3"])
    original = hermes.session_identity
    seen = []

    def mapping(raw, *, entry_id=None):
        seen.append((raw, entry_id))
        return original(raw, entry_id=entry_id)

    monkeypatch.setattr(hermes, "session_identity", mapping)
    try:
        identity = provider._require_identity()
        assert identity.stored_session_id("TEST-raw") == original("TEST-raw", entry_id=identity.entry_id)
        assert seen == [("TEST-raw", identity.entry_id)]
    finally:
        provider.shutdown()
