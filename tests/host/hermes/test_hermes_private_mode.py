"""Hermes private intervals cover live callbacks, delayed sync and kept capture replay."""

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from scope_recall.adapters.hermes import capture_retry, kept_captures
from scope_recall.adapters.hermes.boundary import STEER_KIND
from scope_recall.adapters.hermes.capture import RetryCapture
from scope_recall.contracts import ContractError, TrustedSourcePrincipal
from scope_recall.core import capture_inbox
from scope_recall.core.management import MemoryManagement
from scope_recall.core.restore import InstallationMaintenance

from tests.host.hermes.test_steers import _steer
from tests.v11_support import source_event


def _owner(provider):
    ctx = provider._require_identity().trusted_context()
    return MemoryManagement(provider._require_core(), InstallationMaintenance(ctx)), ctx


def _contents(provider, ctx):
    with provider._require_core().storage.read(ctx) as tx:
        return [row[0] for row in tx._check().execute("SELECT content FROM source_events")]


def test_masked_retry_keeps_safe_context_count_and_gaps_through_disk_and_inbox(adapter, monkeypatch):
    provider, clock = adapter
    bound = provider._require_identity()
    ctx = replace(
        bound.trusted_context(),
        source_principal=TrustedSourcePrincipal("human", "verified", "TEST-principal", 'TEST password: "Synthet1c"'),
    )
    key = ("TEST-masked-retry", 1)
    event = source_event(source_event_key=key[0], origin=ctx.actor_origin, content='TEST api_key: "a"')
    pending = provider._writer._snapshot(ctx, event, key, (), bound.local_scope_id, bound)
    assert pending.masked_secret_count == 2 and pending.capture_gaps == ("secret_values_masked",)
    assert kept_captures.keep(ctx.binding, key, pending)
    saved = next(kept_captures.folder(ctx.binding).glob("*.json")).read_text(encoding="utf-8")
    assert "Synthet1c" not in saved and json.loads(saved)["masked_secret_count"] == 2
    loaded, more = kept_captures.load(ctx.binding, skip=(), room=1)
    pending = loaded[0][1]
    assert not more and pending.masked_secret_count == 2
    provider._retry.captures[key] = pending
    record = capture_inbox.record_event

    def busy(*args, **kwargs):
        raise ContractError("STORAGE_UNAVAILABLE")

    monkeypatch.setattr(capture_inbox, "record_event", busy)
    with provider._lock:
        receipt = provider._writer.write(
            ctx,
            pending.event,
            identity=key,
            gaps=(),
            scope_id=bound.local_scope_id,
            replay=True,
        )
    assert receipt.durability == "queued" and receipt.masked_secret_count == 2
    with provider._core.storage.read(ctx) as tx:
        payload = tx._check().execute("SELECT payload_json FROM capture_inbox").fetchone()[0]
    assert "Synthet1c" not in payload and json.loads(payload)["masked_secret_count"] == 2
    monkeypatch.setattr(capture_inbox, "record_event", record)
    receipts = capture_inbox.replay_inbox(provider._core.storage, clock, ctx, authorize=lambda _: ctx.allowed_scope_ids)
    assert len(receipts) == 1 and receipts[0].masked_secret_count == 2
    source = provider._core.source(ctx, receipts[0].event_refs[0].ref, 1)
    assert source.capture_gaps == ("secret_values_masked",)
    assert source.event["content"] == 'TEST api_key: "[REDACTED_SECRET]"'


def test_a_long_masked_retry_keeps_its_parts_marked_truncated(adapter):
    """Split again from its masked text, a retried long message keeps the parts its first split made."""
    from scope_recall.core.events import prepare_capture

    provider, _clock = adapter
    bound = provider._require_identity()
    ctx = replace(
        bound.trusted_context(),
        source_principal=TrustedSourcePrincipal("human", "verified", "TEST-principal", 'TEST password: "Synthet1c"'),
    )
    key = ("TEST-long-masked-retry", 1)
    event = source_event(
        source_event_key=key[0], origin=ctx.actor_origin, content='TEST api_key: "a"\n' + "TEST words " * 7000
    )
    first = prepare_capture(event, ctx)
    assert len(first.events) == 2 and all(part["segment"]["truncated"] for part in first.events)
    pending = provider._writer._snapshot(ctx, event, key, (), bound.local_scope_id, bound)
    provider._retry.captures[key] = pending
    with provider._lock:
        receipt = provider._writer.write(
            ctx, pending.event, identity=key, gaps=(), scope_id=bound.local_scope_id, replay=True
        )
    assert receipt.durability == "persisted" and receipt.masked_secret_count == 2
    stored = [provider._core.source(ctx, write.ref, write.revision).event for write in receipt.event_refs]
    assert [part["segment"] for part in stored] == [part["segment"] for part in first.events]


def test_new_capture_does_not_inherit_another_messages_mask_count_or_gaps(adapter):
    provider, _clock = adapter
    bound = provider._require_identity()
    ctx = bound.trusted_context()
    key = ("TEST-reused-mask-key", 1)
    previous = source_event(
        source_event_key=key[0],
        origin=ctx.actor_origin,
        content='TEST api_key: "a"\npassword: "Synthet1c"\ndata:image/png;base64,QUJDRA==',
    )
    pending = provider._writer._snapshot(ctx, previous, key, (), bound.local_scope_id, bound)
    assert pending.masked_secret_count == 2 and "transport_payload_omitted" in pending.capture_gaps
    provider._retry.captures[key] = pending
    current = {**previous, "content": 'TEST api_key: "b"'}
    with provider._lock:
        receipt = provider._writer.write(ctx, current, identity=key, gaps=(), scope_id=bound.local_scope_id)
    assert receipt.durability == "persisted" and receipt.masked_secret_count == 1
    source = provider._core.source(ctx, receipt.event_refs[0].ref, 1)
    assert source.capture_gaps == ("secret_values_masked",)


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


@pytest.mark.parametrize("mode", ["full", "read-only"])
@pytest.mark.parametrize("cache", ["_interim_said", "_steer_said"])
def test_post_llm_keeps_only_metadata_for_private_observations(adapter, mode, cache):
    provider, clock = adapter
    owner, ctx = _owner(provider)
    owner.private_on(target="global", mode=mode)
    provider.on_turn_start(1, "TEST opener", turn_id="TEST-turn")
    provider.observe_pre_llm(session_id="TEST-session-1", turn_id="TEST-turn", user_message="TEST opener")
    provider.observe_post_llm_call(
        session_id="TEST-session-1",
        turn_id="TEST-turn",
        assistant_response="TEST final",
        conversation_history=[
            dict(role="user", content="TEST opener"),
            dict(role="assistant", content="TEST interim", tool_calls=[dict(id="TEST-tool")]),
            dict(role="tool", content="TEST result", tool_call_id="TEST-tool"),
            dict(role="user", content=_steer("TEST steer", origin=False), display_kind=STEER_KIND),
            dict(role="assistant", content="TEST final"),
        ],
    )
    assert getattr(provider, cache) == {}
    assert "TEST interim" not in repr(provider._ledger._moments)
    assert "TEST steer" not in repr(provider._ledger._moments)
    clock.now = "2026-09-06T13:00:00Z"
    owner.private_off(target="global")
    provider.sync_turn(
        "TEST opener",
        "TEST final",
        session_id="TEST-session-1",
        messages=[dict(role="user", content=_steer("TEST steer", origin=False), display_kind=STEER_KIND)],
    )
    assert _contents(provider, ctx) == []


@pytest.mark.parametrize("expire", [False, True])
def test_discarding_private_retry_removes_it_once_and_does_not_count_as_persisted(adapter, expire, monkeypatch):
    provider, clock = adapter
    owner, ctx = _owner(provider)
    identity = provider._require_identity()
    key = ("TEST-private-buffer", 1)
    owner.private_on(target="global")
    provider._retry.captures[key] = RetryCapture(
        ctx,
        source_event(source_event_key=key[0], occurred_at=clock.now, recorded_at=clock.now),
        (),
        identity.local_scope_id,
        identity.scope,
        0,
    )
    if expire:
        # Expiry reads the monotonic clock, which starts near zero on a freshly booted runner.
        monkeypatch.setattr(capture_retry, "_RETRY_GIVE_UP_S", -1.0)
        assert provider._retry.give_up_expired(tuple(provider._retry.captures.items())) == [key]
    else:
        assert provider._retry.keep_all_on_disk() == (0, 0)
    assert provider._retry.captures == {}
    assert key not in provider._retry.on_disk


def test_public_steer_is_cached_before_sync(adapter):
    provider, _clock = adapter
    provider.on_turn_start(1, "TEST opener", turn_id="TEST-turn")
    provider.observe_post_llm_call(
        session_id="TEST-session-1",
        turn_id="TEST-turn",
        assistant_response="TEST final",
        conversation_history=[
            dict(role="user", content=_steer("TEST public steer", origin=False), display_kind=STEER_KIND)
        ],
    )
    assert provider._steer_said["TEST-turn"][0].words == "TEST public steer"


@pytest.mark.parametrize("kind", ["interim", "steer"])
def test_historical_untimed_messages_are_checked_before_caching_after_off(adapter, kind):
    provider, clock = adapter
    owner, ctx = _owner(provider)
    provider.on_turn_start(1, "TEST opener", turn_id="TEST-turn")
    owner.private_on(target="global")
    clock.now = "2026-09-06T13:00:00Z"
    owner.private_off(target="global")
    history = [dict(role="assistant", content="TEST private interim", tool_calls=[dict(id="TEST-tool")])]
    if kind == "steer":
        history = [dict(role="user", content=_steer("TEST private steer", origin=False), display_kind=STEER_KIND)]
    provider.observe_post_llm_call(
        session_id="TEST-session-1",
        turn_id="TEST-turn",
        assistant_response="TEST public final",
        conversation_history=history,
    )
    assert provider._interim_said == provider._steer_said == {}


def test_late_writer_does_not_resurrect_a_discarded_private_capture(adapter):
    provider, clock = adapter
    owner, ctx = _owner(provider)
    identity = provider._require_identity()
    key = ("TEST-late-private", 1)
    pending = RetryCapture(
        ctx,
        source_event(source_event_key=key[0], occurred_at=clock.now, recorded_at=clock.now),
        (),
        identity.local_scope_id,
        identity.scope,
        0,
    )
    owner.private_on(target="global")
    provider._writer._closed_meanwhile(key, RuntimeError("TEST retryable write"), False, pending)
    assert key not in provider._retry.captures
    assert key not in provider._retry.on_disk
