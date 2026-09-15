"""Focused Hermes source_context provenance: trusted capture, cross-session recall."""
from __future__ import annotations

import json

import pytest

from scope_recall.adapters.hermes import ScopeRecallHermesAdapter, install_hermes_scope_recall
from scope_recall.adapters.hermes.installation import _build_audience_scope_ids
from scope_recall.contracts import ContractError, validate_payload
from tests.host.hermes.conftest import FixedClock
from tests.unit.test_v11_context import test_scope_can_only_narrow_installation_binding as _assert_scope_binding_narrows
from v11_support import recall_item, recall_packet, recall_request, source_event


TELEGRAM_NOTE = "TEST-TELEGRAM-WHITE-PALETTE-UNIQUE"
DISCORD_NOTE = "TEST-DISCORD-HOST-PLATFORM-SPOOF telegram private"
MIXED_VALUE = "TEST-MIXED-PROVENANCE-PALETTE"
LEGACY_NOTE = "TEST-LEGACY-NO-PLATFORM-METADATA"


def _owner_private_scope(initialize_kwargs: dict) -> str:
    return _build_audience_scope_ids(
        platform="telegram",
        user_id=initialize_kwargs["user_id"],
        agent_identity=initialize_kwargs["agent_identity"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        project_id=initialize_kwargs["agent_workspace"],
        conversation_key="group-1",
    )["owner_private"]


def _install(hermes_home, initialize_kwargs):
    owner = _owner_private_scope(initialize_kwargs)
    clock = FixedClock()
    _binding, core = install_hermes_scope_recall(
        hermes_home,
        agent_id=initialize_kwargs["agent_identity"],
        platform="telegram",
        user_id=initialize_kwargs["user_id"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        audiences=[
            {
                "platform": "telegram",
                "user_id": initialize_kwargs["user_id"],
                "chat_type": "private",
                "chat_id": initialize_kwargs["user_id"],
                "thread_id": "main",
                "gateway_session_key": "",
                "agent_workspace": initialize_kwargs["agent_workspace"],
                "allowed_scope_ids": [owner],
                "writable_scope_ids": [owner],
                "capture_scope_id": owner,
                "kind": "owner_private",
            },
            {
                "platform": "discord",
                "user_id": initialize_kwargs["user_id"],
                "chat_type": "dm",
                "chat_id": initialize_kwargs["user_id"],
                "thread_id": "main",
                "gateway_session_key": "",
                "agent_workspace": initialize_kwargs["agent_workspace"],
                "allowed_scope_ids": [owner],
                "writable_scope_ids": [owner],
                "capture_scope_id": owner,
                "kind": "conversation",
            },
        ],
        test_mode=False,
        clock=clock,
    )
    return core, clock


def _kwargs(initialize_kwargs: dict, *, platform: str, chat_type: str) -> dict:
    return dict(
        initialize_kwargs,
        platform=platform,
        chat_type=chat_type,
        chat_id=initialize_kwargs["user_id"],
        thread_id="main",
    )


def _provider(core, clock, session_id: str, kwargs: dict) -> ScopeRecallHermesAdapter:
    provider = ScopeRecallHermesAdapter(core=core, clock=clock)
    provider.initialize(session_id, **kwargs)
    return provider


def _capture(provider: ScopeRecallHermesAdapter, session_id: str, turn_id: str, text: str) -> str:
    provider.observe_pre_llm(session_id=session_id, turn_id=turn_id, user_message=text)
    assert provider._current_source_refs
    return provider._current_source_refs[-1]


def _item_for(packet: dict, needle: str) -> dict:
    matches = [item for item in packet["items"] if needle in item["content"]]
    assert matches, packet
    return matches[0]


def test_telegram_private_capture_survives_core_readback_and_discord_recall_render(
    hermes_home, initialize_kwargs,
):
    core, clock = _install(hermes_home, initialize_kwargs)
    telegram_kwargs = _kwargs(initialize_kwargs, platform="telegram", chat_type="private")
    discord_kwargs = _kwargs(initialize_kwargs, platform="discord", chat_type="dm")
    telegram = _provider(core, clock, "TEST-session-telegram", telegram_kwargs)
    try:
        ref = _capture(telegram, "TEST-session-telegram", "tg-1", TELEGRAM_NOTE)
        event_id, revision = ref.rsplit("@", 1)
        stored = core.source(telegram._identity.trusted_context(), event_id, int(revision))
        assert stored is not None
        assert stored.event["origin"] == "human_direct"
        assert stored.event["content"] == TELEGRAM_NOTE
        assert stored.event["source_context"] == {"platform": "telegram", "chat_type": "private"}
        assert "discord" not in stored.event["content"]
    finally:
        telegram.shutdown()

    discord = _provider(core, clock, "TEST-session-discord", discord_kwargs)
    try:
        ctx = discord._identity.trusted_context()
        packet = core.recall_packet(
            ctx,
            recall_request(query=TELEGRAM_NOTE, mode="current", request_id="TEST-tg-in-discord"),
            deadline_seconds=5,
        )
        assert validate_payload("recall_packet", packet) == packet
        item = _item_for(packet, TELEGRAM_NOTE)
        assert item["origin"] == "human_direct"
        assert item["source_contexts"] == [{"platform": "telegram", "chat_type": "private"}]
        prepared = core.prepare_recall_render(ctx, packet)
        assert prepared.canonical_text is not None
        rendered = json.loads(prepared.canonical_text)
        rendered_item = next(entry for entry in rendered["items"] if TELEGRAM_NOTE in entry["content"])
        assert rendered_item["origin"] == "human_direct"
        assert rendered_item["source_contexts"] == [{"platform": "telegram", "chat_type": "private"}]
        assert rendered_item["source_contexts"][0]["platform"] != discord._identity.scope.platform
    finally:
        discord.shutdown()


def test_discord_session_does_not_overwrite_or_accept_spoofed_telegram_provenance(
    hermes_home, initialize_kwargs,
):
    core, clock = _install(hermes_home, initialize_kwargs)
    telegram_kwargs = _kwargs(initialize_kwargs, platform="telegram", chat_type="private")
    discord_kwargs = _kwargs(initialize_kwargs, platform="discord", chat_type="dm")
    telegram = _provider(core, clock, "TEST-session-telegram", telegram_kwargs)
    try:
        telegram_ref = _capture(telegram, "TEST-session-telegram", "tg-1", TELEGRAM_NOTE)
    finally:
        telegram.shutdown()

    discord = _provider(core, clock, "TEST-session-discord", discord_kwargs)
    try:
        discord_ref = _capture(discord, "TEST-session-discord", "dc-1", DISCORD_NOTE)
        tg_id, tg_rev = telegram_ref.rsplit("@", 1)
        dc_id, dc_rev = discord_ref.rsplit("@", 1)
        ctx = discord._identity.trusted_context()
        telegram_event = core.source(ctx, tg_id, int(tg_rev)).event
        discord_event = core.source(ctx, dc_id, int(dc_rev)).event
        assert telegram_event["source_context"] == {"platform": "telegram", "chat_type": "private"}
        assert telegram_event["content"] == TELEGRAM_NOTE
        assert discord_event["source_context"] == {"platform": "discord", "chat_type": "dm"}
        assert discord_event["origin"] == "human_direct"
        assert discord_event["content"] == DISCORD_NOTE
        assert "telegram" in discord_event["content"]
        packet = core.recall_packet(
            ctx,
            recall_request(query=TELEGRAM_NOTE, mode="current", request_id="TEST-no-overwrite"),
            deadline_seconds=5,
        )
        item = _item_for(packet, TELEGRAM_NOTE)
        assert item["source_contexts"] == [{"platform": "telegram", "chat_type": "private"}]
        spoof = core.recall_packet(
            ctx,
            recall_request(query=DISCORD_NOTE, mode="current", request_id="TEST-no-spoof"),
            deadline_seconds=5,
        )
        spoof_item = _item_for(spoof, DISCORD_NOTE)
        assert spoof_item["source_contexts"] == [{"platform": "discord", "chat_type": "dm"}]
    finally:
        discord.shutdown()


def test_derived_claim_keeps_visible_mixed_platform_evidence_provenance(
    hermes_home, initialize_kwargs,
):
    core, clock = _install(hermes_home, initialize_kwargs)
    telegram = _provider(core, clock, "TEST-session-telegram", _kwargs(initialize_kwargs, platform="telegram", chat_type="private"))
    discord = _provider(core, clock, "TEST-session-discord", _kwargs(initialize_kwargs, platform="discord", chat_type="dm"))
    try:
        telegram_ref = _capture(telegram, "TEST-session-telegram", "tg-mix", f"TEST-project palette is decided as {MIXED_VALUE} telegram evidence")
        discord_ref = _capture(discord, "TEST-session-discord", "dc-mix", f"TEST-project palette is decided as {MIXED_VALUE} discord evidence")
        tg_id, tg_rev = telegram_ref.rsplit("@", 1)
        dc_id, dc_rev = discord_ref.rsplit("@", 1)
        ctx = discord._identity.trusted_context()
        telegram_event = core.source(ctx, tg_id, int(tg_rev))
        claim = core.accept_claim_proposals(
            ctx,
            {
                "protocol_version": "1.1",
                "source_refs": [telegram_ref, discord_ref],
                "claim_proposals": [{
                    "kind": "decision",
                    "subject": "TEST-project",
                    "predicate": "palette",
                    "value_text": MIXED_VALUE,
                    "conditions": [],
                    "statement_kind": "decision",
                    "valid_from": telegram_event.event["occurred_at"],
                    "valid_to": None,
                    "evidence_spans": [
                        {"source_ref": tg_id, "source_revision": int(tg_rev), "quote": f"TEST-project palette is decided as {MIXED_VALUE} telegram evidence"},
                        {"source_ref": dc_id, "source_revision": int(dc_rev), "quote": f"TEST-project palette is decided as {MIXED_VALUE} discord evidence"},
                    ],
                }],
                "resume_proposals": [],
                "reference_proposals": [],
            },
            scope_id=discord._identity.local_scope_id,
            remaining_seconds=5,
        ).items[0]
        assert claim.state == "active"
        packet = core.recall_packet(
            ctx,
            recall_request(
                query=MIXED_VALUE,
                mode="current",
                request_id="TEST-mixed-claim",
                focus_refs=[f"{claim.ref}@{claim.revision}"],
            ),
            deadline_seconds=5,
        )
        item = next(entry for entry in packet["items"] if entry["ref"] == claim.ref)
        assert item["kind"] in {"claim", "procedure"}
        platforms = {entry["platform"] for entry in item["source_contexts"]}
        chat_types = {entry["chat_type"] for entry in item["source_contexts"]}
        assert platforms == {"telegram", "discord"}
        assert chat_types == {"private", "dm"}
        assert len(item["source_contexts"]) == 2
    finally:
        telegram.shutdown()
        discord.shutdown()


def test_old_events_and_bounded_schema_do_not_fabricate_platform(hermes_home, initialize_kwargs):
    core, clock = _install(hermes_home, initialize_kwargs)
    telegram = _provider(core, clock, "TEST-session-legacy", _kwargs(initialize_kwargs, platform="telegram", chat_type="private"))
    try:
        ctx = telegram._identity.trusted_context()
        saved = core.record_event(
            ctx,
            source_event(content=LEGACY_NOTE, source_event_key="TEST-legacy/1"),
            scope_id=telegram._identity.local_scope_id,
            remaining_seconds=5,
        )
        stored = core.source(ctx, saved.event_refs[0].ref, 1)
        assert "source_context" not in stored.event
        packet = core.recall_packet(
            ctx,
            recall_request(query=LEGACY_NOTE, mode="current", request_id="TEST-legacy"),
            deadline_seconds=5,
        )
        item = _item_for(packet, LEGACY_NOTE)
        assert "source_contexts" not in item
        assert item["origin"] == "human_direct"
        prepared = core.prepare_recall_render(ctx, packet)
        rendered = json.loads(prepared.canonical_text)
        rendered_item = next(entry for entry in rendered["items"] if LEGACY_NOTE in entry["content"])
        assert "source_contexts" not in rendered_item
    finally:
        telegram.shutdown()

    accepted = source_event(source_context={"platform": "telegram", "chat_type": "private"})
    assert validate_payload("source_event", accepted)["source_context"] == {
        "platform": "telegram",
        "chat_type": "private",
    }
    assert "source_context" not in validate_payload("source_event", source_event())
    item = recall_item(source_contexts=[{"platform": "telegram", "chat_type": "private"}])
    assert validate_payload("recall_packet", recall_packet(items=[item]))["items"][0]["source_contexts"] == item["source_contexts"]
    assert "source_contexts" not in validate_payload("recall_packet", recall_packet())["items"][0]
    for invalid in (
        source_event(source_context={"platform": "telegram"}),
        source_event(source_context={"platform": "telegram", "chat_type": "private", "chat_id": "123"}),
        source_event(source_context={"platform": "", "chat_type": "private"}),
        source_event(source_context={"platform": "telegram", "chat_type": "private", "user_id": "x"}),
        recall_packet(items=[recall_item(source_contexts=[{"platform": "telegram", "chat_type": "private", "account": "x"}])]),
    ):
        with pytest.raises(ContractError, match="INPUT_INVALID"):
            validate_payload("source_event" if "source_event_key" in invalid else "recall_packet", invalid)


def test_existing_scope_denial_still_prevents_disclosure(hermes_home, initialize_kwargs, tmp_path):
    _assert_scope_binding_narrows(tmp_path)

    core, clock = _install(hermes_home, initialize_kwargs)
    telegram = _provider(core, clock, "TEST-session-telegram", _kwargs(initialize_kwargs, platform="telegram", chat_type="private"))
    try:
        _capture(telegram, "TEST-session-telegram", "tg-deny", TELEGRAM_NOTE)
    finally:
        telegram.shutdown()

    denied = _provider(
        core,
        clock,
        "TEST-session-denied",
        dict(initialize_kwargs, platform="telegram", chat_type="group", chat_id="group-z", thread_id="main"),
    )
    try:
        assert not denied._identity.runtime_audience.allowed_scope_ids
        assert denied.prefetch(TELEGRAM_NOTE) == ""
        result = json.loads(denied.handle_tool_call("recall", {
            "protocol_version": "1.1",
            "query": TELEGRAM_NOTE,
            "mode": "current",
            "max_items": 6,
            "budget_tokens": 4000,
        }))
        encoded = json.dumps(result, ensure_ascii=False)
        assert result["error"]["code"] == "ACCESS_DENIED"
        assert TELEGRAM_NOTE not in encoded
    finally:
        denied.shutdown()
