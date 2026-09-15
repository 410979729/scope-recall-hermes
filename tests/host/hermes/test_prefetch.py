"""Single prefetch delivery path and render dedupe contracts."""
from __future__ import annotations

from scope_recall.adapters.hermes import bind_hermes_identity, install_hermes_scope_recall
from scope_recall.adapters.hermes.gating import is_trivial_prompt
from scope_recall.core import MemoryCore
from scope_recall.core.retrieval import RetrievalResult
from tests.v11_support import source_event


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


def _bind_context(core: MemoryCore, initialize_kwargs, *, session_id: str):
    identity = bind_hermes_identity(session_id, **initialize_kwargs)
    return identity.trusted_context(session_id=session_id)


def _seed_capture(core: MemoryCore, ctx):
    event = source_event(content="TEST 项目使用白色。", source_event_key="TEST-prefetch/1")
    core.record_event(ctx, event, scope_id=next(iter(ctx.allowed_scope_ids)), remaining_seconds=5)
