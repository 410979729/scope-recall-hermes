"""Cross-hook source dedupe and outcome gap contracts."""
from __future__ import annotations

import sqlite3

from scope_recall.adapters.hermes import ScopeRecallHermesAdapter
from scope_recall.adapters.hermes.boundary import SourceObservationLedger, pre_llm_source_event, sync_turn_source_events
from scope_recall.core import capture_inbox
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


def _sync_after_restart(core, clock, initialize_kwargs, *, at: str, turn: int, user: str, assistant: str):
    """One gateway process: its turn counter starts again, its session does not."""
    clock.now = at
    provider = ScopeRecallHermesAdapter(core=core, clock=clock)
    provider.initialize("TEST-session-1", **initialize_kwargs)
    try:
        provider.on_turn_start(turn, user)
        provider.sync_turn(user, assistant, session_id="TEST-session-1")
        context = provider._require_identity().trusted_context(session_id="TEST-session-1", mutation=True)
        capture_inbox.resolve_conflicted_ingress(
            core.storage, clock, context, authorize=lambda _scope: context.allowed_scope_ids, remaining_seconds=5)
    finally:
        provider.shutdown()


def _stored_times(core) -> dict[str, tuple[str, str]]:
    with sqlite3.connect(core.storage.path) as conn:
        rows = conn.execute("SELECT content, occurred_at, recorded_at FROM source_events").fetchall()
    return {content: (occurred, recorded) for content, occurred, recorded in rows}


def test_reused_turn_number_keeps_its_own_witnessed_time(installed_core, initialize_kwargs):
    """A restarted gateway numbers turns from 1 again inside the same session.

    beta's rc32 test report was written on 09-17 under turn number 8, which an
    unrelated turn of the same session had used on 09-16.  Storage re-keyed the
    new messages, but the adapter had already copied the older turn's time onto
    them, so the newest report in memory claimed to be a day old.
    """
    core, clock = installed_core
    _sync_after_restart(core, clock, initialize_kwargs, at="2026-09-06T13:15:04Z", turn=8,
                        user="TEST 整理整个文件夹", assistant="TEST 整理好了。")
    _sync_after_restart(core, clock, initialize_kwargs, at="2026-09-07T11:06:21Z", turn=8,
                        user="TEST 你测试下召回", assistant="TEST 测完一轮。")
    times = _stored_times(core)
    assert times["TEST 整理整个文件夹"] == ("2026-09-06T13:15:04Z", "2026-09-06T13:15:04Z")
    assert times["TEST 你测试下召回"] == ("2026-09-07T11:06:21Z", "2026-09-07T11:06:21Z")
    assert times["TEST 测完一轮。"] == ("2026-09-07T11:06:21Z", "2026-09-07T11:06:21Z")


def test_replayed_turn_keeps_its_first_witnessed_time(installed_core, initialize_kwargs):
    """The same message under the same key is a replay: one row, first time kept."""
    core, clock = installed_core
    _sync_after_restart(core, clock, initialize_kwargs, at="2026-09-06T13:15:04Z", turn=8,
                        user="TEST 整理整个文件夹", assistant="TEST 整理好了。")
    _sync_after_restart(core, clock, initialize_kwargs, at="2026-09-07T11:06:21Z", turn=8,
                        user="TEST 整理整个文件夹", assistant="TEST 整理好了。")
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT count(*) FROM source_events").fetchone()[0] == 2
    times = _stored_times(core)
    assert times["TEST 整理整个文件夹"] == ("2026-09-06T13:15:04Z", "2026-09-06T13:15:04Z")
    assert times["TEST 整理好了。"] == ("2026-09-06T13:15:04Z", "2026-09-06T13:15:04Z")
