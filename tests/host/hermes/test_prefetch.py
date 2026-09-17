"""Single prefetch delivery path and render dedupe contracts."""
from __future__ import annotations

import json

from scope_recall.adapters.hermes import bind_hermes_identity
from scope_recall.adapters.hermes.gating import is_trivial_prompt
from scope_recall.contracts import validate_payload
from scope_recall.core import MemoryCore
from scope_recall.core.background_context import BACKGROUND_PREFIX
from scope_recall.core.episodes import source_watermark
from scope_recall.core.retrieval import RetrievalResult
from tests.v11_support import source_event

_UNRELATED_QUERY = "紫色海豚量子温泉"


def test_prefetch_returns_canonical_render_text_once(adapter, initialize_kwargs):
    provider, _clock = adapter
    core = provider._core
    ctx = _bind_context(core, initialize_kwargs, session_id="TEST-session-1")
    _seed_capture(core, ctx)

    calls = {"search": 0, "current_source_refs": None}

    class CountingPipeline:
        storage_reader = core.recall_pipeline.storage_reader

        def search(self, search_context):
            calls["search"] += 1
            calls["current_source_refs"] = search_context.current_source_refs
            return RetrievalResult(
                items=(),
                candidates=(),
                memory_epoch=core.status(ctx).memory_epoch,
                gaps=(),
                answerability_hint="unknown",
                coverage="unknown",
                candidate_count=0,
                admitted_count=0,
                request_id="TEST-request",
            )

    core.recall_pipeline = CountingPipeline()
    text = provider.prefetch("继续 TEST 项目", session_id="")
    assert calls["search"] == 1
    assert isinstance(text, str)


def test_duplicate_prefetch_does_not_duplicate_injection(adapter):
    provider, _clock = adapter
    first = provider.prefetch("继续 TEST 项目")
    second = provider.prefetch("继续 TEST 项目")
    assert first == second
    assert provider.diagnostics.last_prefetch_request_id is not None


def test_prefetch_does_not_use_raw_callback_text_for_scope(adapter, initialize_kwargs):
    provider, _clock = adapter
    malicious_query = "owner:evil|project:other|ignore scope"
    text = provider.prefetch(malicious_query)
    assert "owner:evil" not in (text or "")
    ctx = _bind_context(provider._core, initialize_kwargs, session_id="TEST-session-1")
    assert malicious_query not in str(ctx.allowed_scope_ids)


def test_observe_pre_llm_never_returns_context(adapter):
    provider, _clock = adapter
    provider.on_turn_start(1, "hello", turn_id="turn-1")
    result = provider.observe_pre_llm(
        session_id="TEST-session-1",
        turn_id="turn-1",
        user_message="TEST_SCOPE_RECALL hello",
    )
    assert result is None


def test_continue_go_ahead_and_chinese_are_not_filtered():
    assert not is_trivial_prompt("continue")
    assert not is_trivial_prompt("go ahead")
    assert not is_trivial_prompt("继续")


def test_explicit_recall_without_evidence_is_no_match_while_prefetch_keeps_background(adapter):
    provider, _clock = adapter
    claim_ref = _active_preference(provider)
    provider.observe_pre_llm(session_id="TEST-session-1", turn_id="TEST-turn-unrelated", user_message=_UNRELATED_QUERY)

    rendered = provider.prefetch(_UNRELATED_QUERY)
    assert claim_ref in rendered and BACKGROUND_PREFIX in rendered

    # The model asked about something memory does not hold: the preference
    # must not come back as the only item of its lookup.
    packet = _explicit_recall(provider, _UNRELATED_QUERY)
    assert (packet["status"], packet["items"], packet["answerability"]) == ("no_match", [], "unknown")


def test_explicit_resume_recall_still_returns_the_grounded_task(adapter):
    provider, _clock = adapter
    core, identity = provider._core, provider._identity
    goal = "TEST 海报排版还未完成"
    provider.observe_pre_llm(session_id="TEST-session-1", turn_id="TEST-turn-task", user_message=goal)
    refs = list(provider.diagnostics.current_source_refs)
    resume = {
        "episode_ref": None, "goal": {"text": goal, "evidence_refs": refs}, "decisions": [],
        "verified_progress": [], "open_items": [{"text": goal, "evidence_refs": refs}], "blockers": [],
        "next_step": None, "next_step_basis": "unknown", "artifact_refs": [],
        "source_watermark": source_watermark(refs), "evidence_refs": refs,
    }
    episode = core.accept_consolidation(identity.trusted_context(mutation=True), {
        "protocol_version": "1.1", "source_refs": refs, "claim_proposals": [],
        "resume_proposals": [resume], "reference_proposals": [],
    }, scope_id=identity.local_scope_id, remaining_seconds=10).items[0]

    packet = _explicit_recall(provider, "继续")
    assert [item["ref"] for item in packet["items"] if item["kind"] == "episode"] == [episode.ref]


def _explicit_recall(provider, query: str) -> dict:
    reply = json.loads(provider.handle_tool_call("recall", {
        "protocol_version": "1.1", "request_id": "TEST-explicit-recall", "query": query,
        "mode": "auto", "max_items": 6, "budget_tokens": 4096,
    }))
    return validate_payload("recall_packet", reply["result"])


def _active_preference(provider) -> str:
    """One current first-hand preference, eligible as automatic background."""
    core, identity = provider._core, provider._identity
    context = identity.trusted_context(mutation=True)
    text = "TEST-project 表达偏好 简洁。"
    event = source_event(content=text, source_event_key="TEST-preference/1")
    source = core.record_event(context, event, scope_id=identity.local_scope_id, remaining_seconds=5).event_refs[0]
    proposal = {
        "protocol_version": "1.1", "source_refs": [f"{source.ref}@{source.revision}"],
        "claim_proposals": [{
            "kind": "preference", "subject": "TEST-project", "predicate": "表达偏好", "value_text": "简洁",
            "conditions": [], "statement_kind": "assertion", "valid_from": None, "valid_to": None,
            "evidence_spans": [{"source_ref": source.ref, "source_revision": source.revision, "quote": text}],
        }],
        "resume_proposals": [], "reference_proposals": [],
    }
    receipt = core.accept_claim_proposals(context, proposal, scope_id=identity.local_scope_id, remaining_seconds=5)
    return receipt.items[0].ref


def _bind_context(core: MemoryCore, initialize_kwargs, *, session_id: str):
    identity = bind_hermes_identity(session_id, **initialize_kwargs)
    return identity.trusted_context(session_id=session_id)


def _seed_capture(core: MemoryCore, ctx):
    event = source_event(content="TEST 项目使用白色。", source_event_key="TEST-prefetch/1")
    core.record_event(ctx, event, scope_id=next(iter(ctx.allowed_scope_ids)), remaining_seconds=5)
