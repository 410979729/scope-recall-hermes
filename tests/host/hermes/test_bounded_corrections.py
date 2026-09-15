"""Bounded P11 correction behavior tests for manifest, audience, durability, and hooks."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from scope_recall.adapters.hermes import HermesIdentityError, ScopeRecallHermesAdapter, install_hermes_scope_recall
from scope_recall.adapters.hermes.boundary import pre_llm_source_event
from scope_recall.adapters.hermes.installation import build_installation_manifest, write_installation_manifest
from scope_recall.core.retrieval import RetrievalResult
from tests.host.hermes.conftest import FixedClock


def test_explicit_install_test_mode_false_then_initialize(hermes_home, initialize_kwargs):
    clock = FixedClock()
    binding, core = install_hermes_scope_recall(
        hermes_home,
        agent_id=initialize_kwargs["agent_identity"],
        platform=initialize_kwargs["platform"],
        user_id=initialize_kwargs["user_id"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        test_mode=False,
        clock=clock,
    )
    assert binding.test_mode is False
    provider = ScopeRecallHermesAdapter(core=core, clock=clock)
    provider.initialize("TEST-session-1", **initialize_kwargs)
    assert provider.is_available()
    provider.shutdown()


def test_initialize_without_manifest_refuses(hermes_home, initialize_kwargs):
    provider = ScopeRecallHermesAdapter()
    with pytest.raises(HermesIdentityError, match="installation manifest"):
        provider.initialize("TEST-session-1", **initialize_kwargs)


def test_initialize_without_database_refuses(hermes_home, initialize_kwargs):
    manifest = build_installation_manifest(
        hermes_home,
        agent_id=initialize_kwargs["agent_identity"],
        platform=initialize_kwargs["platform"],
        user_id=initialize_kwargs["user_id"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        test_mode=False,
    )
    write_installation_manifest(manifest)
    provider = ScopeRecallHermesAdapter()
    with pytest.raises(HermesIdentityError, match="verified core database"):
        provider.initialize("TEST-session-1", **initialize_kwargs)


def test_reinitialize_binding_mismatch_rejects(initialize_kwargs, hermes_home):
    clock = FixedClock()
    install_hermes_scope_recall(
        hermes_home,
        agent_id=initialize_kwargs["agent_identity"],
        platform=initialize_kwargs["platform"],
        user_id=initialize_kwargs["user_id"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        test_mode=False,
        clock=clock,
    )
    _other_binding, other_core = install_hermes_scope_recall(
        hermes_home.parent / "other-install",
        agent_id="OTHER-agent",
        platform=initialize_kwargs["platform"],
        user_id=initialize_kwargs["user_id"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        test_mode=False,
        clock=clock,
    )
    other = ScopeRecallHermesAdapter(core=other_core, clock=clock)
    with pytest.raises(HermesIdentityError, match="injected core binding mismatch"):
        other.initialize("TEST-session-1", **initialize_kwargs)


def test_owner_private_retained_across_session_switch(adapter, initialize_kwargs):
    provider, _clock = adapter
    owner_before = provider._identity.owner_private_scope_id
    provider.on_session_switch("TEST-session-2", parent_session_id="TEST-session-1")
    assert provider._identity.owner_private_scope_id == owner_before
    assert owner_before in provider._identity.runtime_audience.allowed_scope_ids


def test_group_audience_denies_owner_private(hermes_home, initialize_kwargs):
    install_hermes_scope_recall(
        hermes_home,
        agent_id=initialize_kwargs["agent_identity"],
        platform="telegram",
        user_id=initialize_kwargs["user_id"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        test_mode=False,
    )
    provider = ScopeRecallHermesAdapter()
    kwargs = dict(initialize_kwargs, platform="telegram", chat_type="group", chat_id="group-1", thread_id="main")
    provider.initialize("TEST-session-group", **kwargs)
    assert provider._identity.owner_private_scope_id not in provider._identity.runtime_audience.allowed_scope_ids
    assert any("owner_private" in gap for gap in provider.diagnostics.capability_gaps)
    provider.on_turn_start(1, "hello", turn_id="1")
    provider.observe_pre_llm(session_id="TEST-session-group", turn_id="1", user_message="secret")
    assert provider._identity.local_scope_id != provider._identity.owner_private_scope_id
    provider.shutdown()


def test_unknown_audience_denies_owner_private(hermes_home, initialize_kwargs):
    install_hermes_scope_recall(
        hermes_home,
        agent_id=initialize_kwargs["agent_identity"],
        platform="telegram",
        user_id=initialize_kwargs["user_id"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        test_mode=False,
    )
    provider = ScopeRecallHermesAdapter()
    kwargs = dict(initialize_kwargs, platform="telegram", chat_type="unknown", chat_id="x")
    provider.initialize("TEST-session-unknown", **kwargs)
    assert provider._identity.owner_private_scope_id not in provider._identity.runtime_audience.allowed_scope_ids
    assert any("owner_private" in gap or "audience_unmapped" in gap for gap in provider.diagnostics.capability_gaps)
    provider.shutdown()


def test_current_source_receipt_passed_to_recall(adapter):
    provider, _clock = adapter
    provider.on_turn_start(1, "hello", turn_id="turn-current")
    provider.observe_pre_llm(
        session_id="TEST-session-1",
        turn_id="turn-current",
        user_message="TEST capture for current source exclusion",
    )
    assert provider.diagnostics.current_source_refs
    core = provider._core
    ctx = provider._identity.trusted_context()
    observed_refs = {"passed": None}

    class ObservingPipeline:
        storage_reader = core.recall_pipeline.storage_reader

        def search(self, search_context):
            observed_refs["passed"] = search_context.current_source_refs
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

    core.recall_pipeline = ObservingPipeline()
    provider.prefetch("继续 TEST 项目")
    assert observed_refs["passed"] == tuple(provider.diagnostics.current_source_refs)


def test_failed_write_retry_after_ledger_rollback(adapter):
    provider, _clock = adapter
    with patch.object(provider._core, "record_host_event", side_effect=RuntimeError("disk full")):
        provider.observe_pre_llm(
            session_id="TEST-session-1",
            turn_id="retry-turn",
            user_message="retry me",
        )
    assert any("capture_failure" in item for item in provider.diagnostics.capture_failures)
    provider.observe_pre_llm(
        session_id="TEST-session-1",
        turn_id="retry-turn",
        user_message="retry me",
    )
    assert provider.diagnostics.current_source_refs


def test_post_tool_call_failure_records_gap_not_success(adapter):
    provider, _clock = adapter
    provider.observe_post_tool_call(
        session_id="TEST-session-1",
        turn_id="tool-1",
        tool_call_id="call-1",
        tool_name="search",
        status="error",
        result=None,
    )
    gaps = provider.diagnostics.pending_outcome_gaps
    assert any("failure" in gap or "outcome_gap" in gap for gap in gaps)
    assert not provider.diagnostics.current_source_refs


def test_on_pre_compress_and_session_end_bounded_gaps(adapter):
    provider, _clock = adapter
    provider.on_pre_compress([{"role": "tool", "content": ""}])
    assert any("on_pre_compress_gap" in gap for gap in provider.diagnostics.pending_outcome_gaps)
    provider.on_session_end([{"role": "assistant", "tool_calls": [{"id": "1"}], "content": ""}])
    assert any("on_session_end_gap" in gap for gap in provider.diagnostics.pending_outcome_gaps)


def test_shutdown_reports_unpersisted_pending(adapter):
    provider, _clock = adapter
    ctx = provider._identity.trusted_context()
    _event, _gaps, identity = pre_llm_source_event(
        provider._ledger,
        ctx,
        session_id="TEST-session-1",
        turn_id="pending-turn",
        user_message="pending only",
        recorded_at="2026-09-06T12:00:00Z",
    )
    assert identity is not None
    provider.shutdown()
    assert provider.diagnostics.shutdown_state is not None
    assert provider.diagnostics.shutdown_state.get("pending_capture_status") == "unpersisted"
    assert int(provider.diagnostics.shutdown_state.get("pending_captures", 0)) >= 1
