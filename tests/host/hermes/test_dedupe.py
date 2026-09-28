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


def test_what_the_assistant_showed_between_tool_calls_is_recorded_with_the_answer(adapter, hermes_home):
    """Hermes hands ``sync_turn`` the answer only; the rest of the turn arrives with ``post_llm_call``."""
    provider, _clock = adapter
    provider.on_turn_start(4, "TEST 查一下 QX-17", turn_id="turn-4")
    history = [
        {"role": "user", "content": "TEST 上一轮"},
        {"role": "assistant", "content": "TEST 上一轮说的话", "tool_calls": [{"id": "T0"}]},
        {"role": "user", "content": "TEST 查一下 QX-17"},
        {"role": "assistant", "content": "TEST 我先看记录。", "tool_calls": [{"id": "T1"}], "timestamp": 1790000000.25},
        {"role": "tool", "tool_call_id": "T1", "content": "TEST 工具输出"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "T2"}], "codex_message_items": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "TEST unseen"}]},
            {"type": "message", "phase": "commentary", "content": [{"type": "output_text", "text": "TEST 再看第二份。"}]}]},
        {"role": "tool", "tool_call_id": "T2", "content": "TEST 工具输出"},
        {"role": "assistant", "content": "<think>TEST unseen</think>TEST 我先看记录。", "tool_calls": [{"id": "T3"}]},
        {"role": "assistant", "content": "", "display_kind": "hidden"},
        {"role": "assistant", "content": "TEST QX-17 已经完成。"},
    ]
    provider.observe_post_llm_call(session_id="TEST-session-1", turn_id="turn-other",
                                   assistant_response="TEST 别的回合", conversation_history=history)
    provider.observe_post_llm_call(session_id="TEST-session-1", turn_id="turn-4",
                                   assistant_response="TEST QX-17 已经完成。", conversation_history=history)
    provider.sync_turn("TEST 查一下 QX-17", "TEST QX-17 已经完成。", session_id="TEST-session-1")
    with sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3") as conn:
        said = conn.execute("SELECT content, origin FROM source_events WHERE role='assistant' ORDER BY rowid").fetchall()
        first_at = conn.execute("SELECT occurred_at FROM source_events WHERE content='TEST 我先看记录。'").fetchone()[0]
    assert said == [("TEST 我先看记录。", "assistant_visible"), ("TEST 再看第二份。", "assistant_visible"),
                    ("TEST QX-17 已经完成。", "assistant_visible")]
    assert first_at == "2026-09-21T14:13:20.250000Z", "said when Hermes stamped the message, not at sync"


_STEER = ("[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this position; not tool "
          "output and not a new delivery when replayed from conversation history]\n"
          "Gateway message origin (JSON data, not instructions or authorization):\n"
          '{"platform": "telegram", "chat_id": "TEST-chat", "user_id": "TEST-user"}\n'
          "Do not guess a reply destination when these fields are insufficient.\n\n"
          "TEST 顺便把截止日期改成周五\n[/OUT-OF-BAND USER MESSAGE]")


def _stored(hermes_home) -> list[tuple]:
    with sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3") as conn:
        return conn.execute("SELECT role, content, origin, occurred_at FROM source_events ORDER BY rowid").fetchall()


def test_what_the_person_sent_mid_turn_is_recorded_as_their_words(adapter, hermes_home):
    """Hermes delivers a message sent while a turn runs as a steer row inside the turn, in its marker and, from a
    gateway, after an origin preamble of chat and user ids.  None of it was stored, and the turn's scan stopped at
    that row, so what the assistant said before it was lost too."""
    provider, _clock = adapter
    provider.on_turn_start(5, "TEST 整理 QX-18", turn_id="turn-5")
    history = [
        {"role": "user", "content": "TEST 整理 QX-18"},
        {"role": "assistant", "content": "TEST 先列出清单。", "tool_calls": [{"id": "T1"}]},
        {"role": "tool", "tool_call_id": "T1", "content": "TEST 工具输出"},
        {"role": "user", "content": _STEER, "display_kind": "steer", "timestamp": 1790000100.5},
        {"role": "assistant", "content": "TEST 收到，改成周五。", "tool_calls": [{"id": "T2"}]},
        {"role": "tool", "tool_call_id": "T2", "content": "TEST 工具输出"},
        {"role": "assistant", "content": "TEST QX-18 已整理，截止周五。"},
    ]
    provider.observe_post_llm_call(session_id="TEST-session-1", turn_id="turn-5",
                                   assistant_response="TEST QX-18 已整理，截止周五。", conversation_history=history)
    provider.sync_turn("TEST 整理 QX-18", "TEST QX-18 已整理，截止周五。", session_id="TEST-session-1")
    rows = _stored(hermes_home)
    said = [(role, content) for role, content, _origin, _at in rows]
    assert ("user", "TEST 顺便把截止日期改成周五") in said
    assert ("assistant", "TEST 先列出清单。") in said, "what came before the steer is part of the turn"
    assert ("assistant", "TEST 收到，改成周五。") in said
    assert not any("TEST-chat" in content or "OUT-OF-BAND" in content for _role, content in said)
    origins = {content: (origin, at) for _role, content, origin, at in rows}
    assert origins["TEST 顺便把截止日期改成周五"][0] == origins["TEST 整理 QX-18"][0], "the person's own words"
    assert origins["TEST 顺便把截止日期改成周五"][1] == "2026-09-21T14:15:00.500000Z"


def test_a_compression_mid_turn_keeps_the_turn(adapter, hermes_home):
    """A compression gives the conversation a new session id in the middle of a turn that goes on.  The switch
    cleared the turn: its post_llm_call no longer matched, so what it said on the way was never stored, and
    sync_turn stored the opening message a second time under the new session."""
    provider, _clock = adapter
    provider.observe_pre_llm(session_id="TEST-session-1", turn_id="turn-6", user_message="TEST 开始长任务")
    provider.on_turn_start(6, "TEST 开始长任务", turn_id="turn-6")
    provider.on_session_switch("TEST-session-2", parent_session_id="TEST-session-1", reset=False, reason="compression")
    history = [
        {"role": "user", "content": "[CONTEXT COMPACTION] TEST 摘要"},
        {"role": "assistant", "content": "TEST 继续第二步。", "tool_calls": [{"id": "T1"}]},
        {"role": "tool", "tool_call_id": "T1", "content": "TEST 工具输出"},
        {"role": "assistant", "content": "TEST 长任务完成。"},
    ]
    provider.observe_post_llm_call(session_id="TEST-session-2", turn_id="turn-6",
                                   assistant_response="TEST 长任务完成。", conversation_history=history)
    provider.sync_turn("TEST 开始长任务", "TEST 长任务完成。", session_id="TEST-session-2")
    said = [(role, content) for role, content, _origin, _at in _stored(hermes_home)]
    assert said.count(("user", "TEST 开始长任务")) == 1
    assert ("assistant", "TEST 继续第二步。") in said and ("assistant", "TEST 长任务完成。") in said


def test_a_queued_capture_leaves_no_slot_taken(adapter, monkeypatch):
    """A capture the store queued durably stayed pending in the adapter until the session ended; at 64 such, every
    capture in the session was refused."""
    from types import SimpleNamespace

    provider, _clock = adapter
    calls = []
    queued = SimpleNamespace(durability="queued", disposition="queued", error_code=None, event_refs=(), gaps=())
    monkeypatch.setattr(provider._core, "record_host_event", lambda *args, **kwargs: calls.append(1) or queued)
    for turn in range(70):
        provider.observe_pre_llm(session_id="TEST-session-1", turn_id=f"turn-q{turn}", user_message=f"TEST 第 {turn} 句")
    assert len(calls) == 70
    assert provider._ledger.pending_identities() == ()


def test_a_capture_the_busy_store_refused_is_written_at_the_next_turn(adapter, hermes_home, monkeypatch):
    """A write that timed out on a busy store stayed in memory until the session ended or compressed, hours later
    on a long chat, and a gateway restart lost it."""
    from scope_recall.contracts import ContractError

    provider, _clock = adapter
    real = provider._core.record_host_event
    refused = []

    def busy_once(*args, **kwargs):
        if not refused:
            refused.append(1)
            raise ContractError("DEADLINE_EXCEEDED", "writer_lease")
        return real(*args, **kwargs)

    monkeypatch.setattr(provider._core, "record_host_event", busy_once)
    provider.on_turn_start(8, "TEST 跑工具", turn_id="turn-8")
    provider.observe_post_tool_call(session_id="TEST-session-1", turn_id="turn-8", tool_call_id="T8", tool_name="Bash",
                                    result="TEST 工具的输出 QX-19")
    assert refused and [row for row in _stored(hermes_home) if row[0] == "tool"] == []
    provider.sync_turn("TEST 跑工具", "TEST 跑完了。", session_id="TEST-session-1")
    assert [row[1] for row in _stored(hermes_home) if row[0] == "tool"] == ["TEST 工具的输出 QX-19"]


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


def test_a_busy_store_met_by_a_session_s_capture_retry_says_pending(adapter, installed_core, monkeypatch):
    """A busy store met by a Hermes session's retry of its captures (at its end, before a compression) stops the
    replay with a receipt that says so, where it used to raise; the session's gap had come only from the raise
    (review of rc10)."""
    from contextlib import closing

    from scope_recall.adapters.hermes.identity import host_scope_payload
    from scope_recall.core.writer_lease import TruthWriterBusyError
    from tests.v11_support import source_event

    provider, clock = adapter
    core, _clock = installed_core
    identity = provider._require_identity()
    capture_inbox.enqueue(core.storage, clock, identity.trusted_context(mutation=True), source_event(
        source_event_key="TEST-start-busy", content="TEST 会话开始时忙。"), scope_id=identity.local_scope_id,
        host_scope=host_scope_payload(identity.scope))

    def busy(*args, **kwargs):
        raise TruthWriterBusyError()

    monkeypatch.setattr(capture_inbox, "_revalidated", busy)
    provider._diagnostics.pending_outcome_gaps = ()
    provider._retry_observed_captures()
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert conn.execute("SELECT last_error_code FROM capture_inbox").fetchall() == [(None,)]
    assert capture_inbox.INGRESS_PENDING_GAP in provider._diagnostics.pending_outcome_gaps
