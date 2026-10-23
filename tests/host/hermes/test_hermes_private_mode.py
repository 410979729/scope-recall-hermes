"""Hermes private intervals cover live callbacks, delayed sync and kept capture replay."""

from dataclasses import replace
from unittest.mock import Mock

import pytest
from scope_recall.adapters.hermes import kept_captures
from scope_recall.adapters.hermes.capture import RetryCapture
from scope_recall.core.management import MemoryManagement
from scope_recall.core.restore import InstallationMaintenance

from tests.v11_support import source_event


def _owner(provider):
    ctx = provider._require_identity().trusted_context()
    return MemoryManagement(provider._require_core(), InstallationMaintenance(ctx)), ctx


def _contents(provider, ctx):
    with provider._require_core().storage.read(ctx) as tx:
        return [row[0] for row in tx._check().execute("SELECT content FROM source_events")]


@pytest.mark.parametrize("mode", ["full", "read-only"])
def test_all_live_capture_callbacks_drop_private_content_and_attachments(adapter, mode):
    provider, _clock = adapter
    owner, ctx = _owner(provider)
    owner.private_on(target="session", session=ctx.session_id, mode=mode)
    provider.on_turn_start(1, "TEST private", turn_id="TEST-turn")
    provider.observe_pre_llm(
        session_id="TEST-session-1",
        turn_id="TEST-turn",
        user_message="TEST private",
        attachments=[dict(filename="TEST-file.svg", svg="<svg xmlns='http://www.w3.org/2000/svg'></svg>")],
    )
    provider.observe_post_tool_call(
        session_id="TEST-session-1",
        turn_id="TEST-turn",
        tool_call_id="TEST-tool",
        tool_name="TEST-tool",
        result="TEST private tool",
    )
    provider.sync_turn("TEST private", "TEST private reply", session_id="TEST-session-1")
    messages = [dict(role="user", content="TEST private unanswered", timestamp=provider._utc_now())]
    provider.on_pre_compress(messages)
    provider.on_session_end(messages)
    assert _contents(provider, ctx) == []
    assert provider._retry.captures == {}
    assert list(kept_captures.folder(ctx.binding).glob("*.json")) == []
    with provider._core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 0


def test_sync_after_off_cannot_recapture_a_private_opener(adapter):
    provider, clock = adapter
    owner, ctx = _owner(provider)
    owner.private_on(target="session", session=ctx.session_id)
    provider.on_turn_start(1, "TEST private opener", turn_id="TEST-turn")
    provider.observe_pre_llm(session_id="TEST-session-1", turn_id="TEST-turn", user_message="TEST private opener")
    clock.now = "2026-09-06T13:00:00Z"
    owner.private_off(target="session", session=ctx.session_id)
    provider.sync_turn("TEST private opener", "TEST public reply", session_id="TEST-session-1")
    assert "TEST private opener" not in _contents(provider, ctx)
    assert "TEST public reply" in _contents(provider, ctx)


def test_private_retry_and_disk_preservation_drop_original_events(adapter, monkeypatch):
    provider, clock = adapter
    owner, ctx = _owner(provider)
    identity = provider._require_identity()
    key = ("TEST-private-retry", 1)
    event = source_event(
        source_event_key=key[0], occurred_at=clock.now, recorded_at=clock.now, content="TEST private retry"
    )
    pending = RetryCapture(ctx, event, (), identity.local_scope_id, identity.scope, 0)
    owner.private_on(target="session", session=ctx.session_id)
    provider._retry.captures[key] = pending
    clock.now = "2026-09-06T13:00:00Z"
    owner.private_off(target="session", session=ctx.session_id)
    with provider._lock:
        provider._writer.write(ctx, event, identity=key, gaps=(), scope_id=identity.local_scope_id, replay=True)
    assert provider._retry.captures == {}
    disk = Mock(return_value=True)
    monkeypatch.setattr(kept_captures, "keep", disk)
    assert provider._retry.keep_on_disk(key, pending)
    assert disk.call_count == 0
    assert _contents(provider, ctx) == []


def test_loaded_kept_capture_is_discarded_by_original_event_time(adapter, monkeypatch):
    provider, clock = adapter
    owner, ctx = _owner(provider)
    identity = provider._require_identity()
    key = ("TEST-private-kept", 1)
    pending = RetryCapture(
        ctx,
        source_event(source_event_key=key[0], occurred_at=clock.now, recorded_at=clock.now),
        (),
        identity.local_scope_id,
        identity.scope,
        0,
    )
    owner.private_on(target="session", session=ctx.session_id)
    # A synthetic file models a capture kept by a host that had not yet received the owner's interval.
    assert kept_captures.keep(ctx.binding, key, pending)
    clock.now = "2026-09-06T13:00:00Z"
    owner.private_off(target="session", session=ctx.session_id)
    monkeypatch.setattr(provider._retry, "start", lambda: None)
    with provider._lock:
        provider._retry.load_kept()
        assert key in provider._retry.captures
        provider._writer.write(ctx, pending.event, identity=key, gaps=(), scope_id=pending.scope_id, replay=True)
    assert provider._retry.captures == {}
    assert list(kept_captures.folder(ctx.binding).glob("*.json")) == []
    assert _contents(provider, ctx) == []


def test_full_prefetch_is_a_notice_without_search_and_tools_do_not_switch_mode(adapter, monkeypatch):
    provider, _clock = adapter
    owner, ctx = _owner(provider)
    owner.private_on(target="global")
    search = Mock(side_effect=AssertionError("TEST recall must not start"))
    monkeypatch.setattr(provider._core, "recall_packet", search)
    assert "not being remembered" in provider.prefetch("TEST recall question", session_id="TEST-session-1")
    assert search.call_count == 0
    assert not any("private" in tool["name"] for tool in provider.get_tool_schemas())


def test_hermes_full_read_tools_reach_the_shared_core_gate(adapter):
    import json

    provider, _clock = adapter
    owner, _ctx = _owner(provider)
    owner.private_on(target="global")
    for tool in ("profile", "entity", "trace"):
        extra = {"action": "probe"} if tool == "entity" else {}
        answer = json.loads(
            provider.handle_tool_call(tool, dict(protocol_version="1.1", subject="TEST-subject", **extra))
        )
        assert answer["error"] == dict(code="ACCESS_DENIED", field="private_mode")


@pytest.mark.parametrize("reply", ["TEST final", "TEST public reply"])
def test_post_llm_without_host_timestamps_keeps_its_first_seen_time_after_off(adapter, reply):
    provider, clock = adapter
    owner, ctx = _owner(provider)
    owner.private_on(target="global")
    provider.on_turn_start(1, "TEST private opener", turn_id="TEST-turn")
    provider.observe_pre_llm(session_id="TEST-session-1", turn_id="TEST-turn", user_message="TEST private opener")
    provider.observe_post_llm_call(
        session_id="TEST-session-1",
        turn_id="TEST-turn",
        assistant_response="TEST final",
        conversation_history=[
            dict(role="user", content="TEST private opener"),
            dict(role="assistant", content="TEST private interim", tool_calls=[dict(id="TEST-tool")]),
            dict(role="tool", content="TEST result", tool_call_id="TEST-tool"),
            dict(role="assistant", content="TEST final"),
        ],
    )
    clock.now = "2026-09-06T13:00:00Z"
    owner.private_off(target="global")
    provider.sync_turn("TEST private opener", reply, session_id="TEST-session-1")
    assert "TEST private interim" not in _contents(provider, ctx)
    assert _contents(provider, ctx) == []
