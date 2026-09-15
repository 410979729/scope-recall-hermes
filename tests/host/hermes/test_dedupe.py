"""Cross-hook source dedupe and outcome gap contracts."""
from __future__ import annotations

import sqlite3

from scope_recall.adapters.hermes.boundary import SourceObservationLedger, pre_llm_source_event, sync_turn_source_events
from tests.v11_support import context


def test_same_event_identity_is_idempotent_across_hooks(tmp_path):
    ledger = SourceObservationLedger()
    ctx = context(tmp_path / "db")
    first, _, first_identity = pre_llm_source_event(
        ledger,
        ctx,
        session_id="TEST-session",
        turn_id="turn-1",
        user_message="same text",
        recorded_at="2026-09-06T12:00:00Z",
    )
    second, _, _ = pre_llm_source_event(
        ledger,
        ctx,
        session_id="TEST-session",
        turn_id="turn-1",
        user_message="same text",
        recorded_at="2026-09-06T12:00:01Z",
    )
    assert first is not None
    assert first_identity is not None
    assert second is None


def test_equal_text_without_shared_identity_stays_distinct(tmp_path):
    ledger = SourceObservationLedger()
    ctx = context(tmp_path / "db")
    pre, _, _ = pre_llm_source_event(
        ledger,
        ctx,
        session_id="TEST-session",
        turn_id="turn-1",
        user_message="same text",
        recorded_at="2026-09-06T12:00:00Z",
    )
    sync_events, gaps = sync_turn_source_events(
        ledger,
        ctx,
        session_id="TEST-session",
        turn_id="turn-2",
        user_content="same text",
        assistant_content="ok",
        recorded_at="2026-09-06T12:00:01Z",
        outcome="success",
    )
    assert pre is not None
    assert len(sync_events) == 2
    assert gaps == ()


def test_pre_llm_and_sync_turn_replay_same_stable_turn_once(tmp_path):
    ledger = SourceObservationLedger()
    ctx = context(tmp_path / "db")
    pre, _, _ = pre_llm_source_event(
        ledger,
        ctx,
        session_id="TEST-session",
        turn_id="turn-shared",
        user_message="same text",
        recorded_at="2026-09-06T12:00:00Z",
    )
    sync_events, gaps = sync_turn_source_events(
        ledger,
        ctx,
        session_id="TEST-session",
        turn_id="turn-shared",
        user_content="same text",
        assistant_content="ok",
        recorded_at="2026-09-06T12:00:01Z",
        outcome="success",
    )
    assert pre is not None
    assert [event[0]["role"] for event in sync_events] == ["assistant"]
    assert gaps == ()


def test_failure_and_truncated_outcomes_record_gaps(adapter):
    provider, _clock = adapter
    provider.on_turn_start(2, "fail", turn_id="turn-2")
    provider.observe_api_request_error(session_id="TEST-session-1", turn_id="turn-2", status="400")
    provider.sync_turn("question", "", session_id="TEST-session-1")
    gaps = provider.diagnostics.pending_outcome_gaps
    assert any("failure" in gap for gap in gaps)
    assert any("truncated" in gap or "missing_assistant" in gap for gap in gaps)


def test_success_sync_persists_with_trusted_context(adapter, hermes_home):
    provider, _clock = adapter
    provider.on_turn_start(3, "ok", turn_id="turn-3")
    provider.sync_turn("TEST 记住白色。", "好的。", session_id="TEST-session-1")
    db = hermes_home / "scope-recall" / "memory.sqlite3"
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT count(*) FROM source_events").fetchone()[0]
    assert count >= 1


def test_live_turn_events_carry_witnessed_occurrence_time(tmp_path):
    """Live host turns are witnessed: occurred_at grounds to the turn time so
    current-mode recall can serve them (imports keep occurred_at=None)."""
    ledger = SourceObservationLedger()
    ctx = context(tmp_path / "db")
    sync_events, _gaps = sync_turn_source_events(
        ledger,
        ctx,
        session_id="TEST-session",
        turn_id="turn-1",
        user_content="请记住我的靛蓝档案目录名称是 TEST-X。",
        assistant_content="好的。",
        recorded_at="2026-09-06T12:00:00Z",
        outcome="success",
    )
    assert sync_events
    for event, identity in sync_events:
        assert event["occurred_at"] == "2026-09-06T12:00:00Z"
        assert event["time_precision"] == "instant"
