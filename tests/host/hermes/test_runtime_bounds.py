"""P11 runtime bounds: turn-local echo fences, worker coalescing, and hooks."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from scope_recall.adapters.hermes import (
    ScopeRecallHermesAdapter,
    capture,
    capture_retry,
    hooks,
    install_hermes_scope_recall,
)
from scope_recall.adapters.hermes import prefetch as prefetch_module
from scope_recall.adapters.hermes import provider as provider_module
from scope_recall.adapters.hermes.hooks import (
    _SUPPORTED_HOOKS,
    _global_callback,
    _register_adapter_instance,
    _unregister_adapter_instance,
)
from scope_recall.adapters.hermes.provider import GAP_CURRENT_SOURCE_REFS_LIMIT
from scope_recall.adapters.hermes.worker import AdapterWorker
from scope_recall.contracts import validate_payload
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.events import MAX_SEGMENT_CHARS
from scope_recall.core.retrieval import MAX_CURRENT_SOURCE_REFS

_MODES = ("history", "current", "auto")


def _recall(provider, mode: str, request_id: str) -> str:
    return provider.handle_tool_call(
        "recall",
        {
            "protocol_version": "1.1",
            "request_id": request_id,
            "query": "where does the orca42 rollout run",
            "mode": mode,
            "max_items": 6,
            "budget_tokens": 4096,
        },
    )


def _tool_result(provider, turn_id: str, call_id: str, result: str, *, tool_name: str = "terminal") -> None:
    provider.observe_post_tool_call(
        session_id="TEST-session-1",
        turn_id=turn_id,
        tool_call_id=call_id,
        tool_name=tool_name,
        result=result,
        status="success",
    )


def _earlier_turn_ref(provider) -> str:
    """A fact from a finished turn, which recall must keep finding."""
    provider.observe_pre_llm(
        session_id="TEST-session-1",
        turn_id="turn-earlier",
        user_message="The orca42 rollout runs from the blue cluster.",
    )
    (ref,) = provider.diagnostics.current_source_refs
    return ref


def _assert_fenced_recall(provider, *, earlier_ref: str, marker: str) -> None:
    """Every mode recalls the earlier turn and nothing captured in this one.

    Every source of this turn carries ``marker``, so the content check does
    not depend on the provider's own bookkeeping of those refs.
    """
    this_turn = set(provider.diagnostics.current_source_refs)
    for mode in _MODES:
        reply = json.loads(_recall(provider, mode, f"final-{mode}"))
        assert "error" not in reply, (mode, reply)
        assert GAP_CURRENT_SOURCE_REFS_LIMIT not in reply["capability_gaps"]
        items = reply["result"]["items"]
        delivered = {f"{item['ref']}@{item['revision']}" for item in items}
        assert not [item["content"] for item in items if marker in item["content"]], mode
        assert not delivered & this_turn, mode
        assert earlier_ref in delivered, (mode, reply["result"])


def test_current_source_refs_are_turn_local_and_old_capture_can_return(adapter):
    provider, _clock = adapter
    provider.observe_pre_llm(
        session_id="TEST-session-1",
        turn_id="uuid-1",
        user_message="unique historical anchor turn 1",
    )
    provider.on_turn_start(1, "ordinal-1")
    provider.prefetch("unrelated first query")
    provider.observe_pre_llm(
        session_id="TEST-session-1",
        turn_id="uuid-2",
        user_message="unique historical anchor turn 2",
    )
    provider.on_turn_start(2, "ordinal-2")
    provider.observe_pre_llm(
        session_id="TEST-session-1",
        turn_id="uuid-2",
        user_message="unique historical anchor turn 2",
    )
    # Paraphrase so this exercises turn-local source refs rather than the
    # separate rule excluding an event identical to the automatic query.
    rendered = provider.prefetch("historical anchor turn 1 details")
    assert "unique historical anchor turn 1" in rendered
    assert "unique historical anchor turn 2" not in provider.prefetch("historical anchor turn 2 details")
    db_path = provider._identity.manifest.data_directory / "memory.sqlite3"
    with sqlite3.connect(db_path) as conn:
        before_sync = conn.execute("SELECT count(*) FROM source_events").fetchone()[0]
    provider.sync_turn("unique historical anchor turn 2", "ack", session_id="TEST-session-1")
    with sqlite3.connect(db_path) as conn:
        after_sync = conn.execute("SELECT count(*) FROM source_events").fetchone()[0]
    assert after_sync == before_sync + 1  # assistant only; user UUID was deduped
    for turn in range(3, 21):
        provider.observe_pre_llm(
            session_id="TEST-session-1",
            turn_id=f"uuid-{turn}",
            user_message=f"unique historical anchor turn {turn}",
        )
        provider.on_turn_start(turn, f"ordinal-{turn}")
        assert len(provider.diagnostics.current_source_refs) == 1
        provider.prefetch("unrelated query")


def test_same_text_new_uuid_is_distinct_and_overflow_is_degraded(adapter):
    provider, _clock = adapter
    provider.observe_pre_llm(session_id="TEST-session-1", turn_id="uuid-a", user_message="same text")
    first_ref = provider.diagnostics.current_source_refs
    provider.observe_pre_llm(session_id="TEST-session-1", turn_id="uuid-a", user_message="same text")
    assert provider.diagnostics.current_source_refs == first_ref
    db_path = provider._identity.manifest.data_directory / "memory.sqlite3"
    with sqlite3.connect(db_path) as conn:
        after_replay = conn.execute("SELECT count(*) FROM source_events").fetchone()[0]
    provider.observe_pre_llm(session_id="TEST-session-1", turn_id="uuid-b", user_message="same text")
    assert provider.diagnostics.current_source_refs != first_ref
    with sqlite3.connect(db_path) as conn:
        after_new_uuid = conn.execute("SELECT count(*) FROM source_events").fetchone()[0]
    assert after_replay == 1
    assert after_new_uuid == 2
    # A full fence, then one more captured source of the same turn overflows it.
    provider._current_source_refs = [f"ref-{index}" for index in range(MAX_CURRENT_SOURCE_REFS)]
    _tool_result(provider, "uuid-b", "overflow-1", "one source past the fence")
    assert provider.prefetch("overflow check") == ""
    assert "degraded:current_source_refs_limit" in provider.diagnostics.capability_gaps


def test_tool_heavy_turn_keeps_explicit_recall_working_and_fenced(adapter):
    provider, _clock = adapter
    earlier_ref = _earlier_turn_ref(provider)
    provider.observe_pre_llm(
        session_id="TEST-session-1",
        turn_id="turn-heavy",
        user_message="heavy-turn: check where the orca42 rollout runs",
    )
    provider.on_turn_start(2, "heavy-turn: check where the orca42 rollout runs", turn_id="turn-heavy")
    for index in range(40):
        if index % 2:
            # Scope Recall's own recall result is one of this turn's sources too.
            result = _recall(provider, _MODES[index % 3], f"heavy-turn-{index}")
            assert "error" not in json.loads(result), result
            _tool_result(provider, "turn-heavy", f"heavy-{index}", result, tool_name="recall")
        else:
            _tool_result(provider, "turn-heavy", f"heavy-{index}", f"heavy-turn step {index}: orca42 rollout log")
    assert len(provider.diagnostics.current_source_refs) == 41
    _assert_fenced_recall(provider, earlier_ref=earlier_ref, marker="heavy-turn")


def test_every_segment_of_a_long_tool_result_stays_fenced(adapter):
    provider, _clock = adapter
    earlier_ref = _earlier_turn_ref(provider)
    provider.observe_pre_llm(
        session_id="TEST-session-1", turn_id="turn-long", user_message="long-turn: read the orca42 rollout log"
    )
    line = "long-turn: orca42 rollout log line\n"
    log = line * (2 * MAX_SEGMENT_CHARS // len(line) + 64)
    assert len(log) > 2 * MAX_SEGMENT_CHARS
    _tool_result(provider, "turn-long", "long-1", log)
    assert len(provider.diagnostics.current_source_refs) == 1 + 3  # the user message and three segments
    _assert_fenced_recall(provider, earlier_ref=earlier_ref, marker="long-turn")


def test_overflowed_turn_degrades_recall_visibly_until_the_next_turn(adapter):
    provider, _clock = adapter
    earlier_ref = _earlier_turn_ref(provider)
    provider.observe_pre_llm(session_id="TEST-session-1", turn_id="turn-full", user_message="full-turn: orca42 rollout")
    # Stand-ins for sources this turn already captured, one short of the fence.
    provider._current_source_refs.extend(f"event-stand-in-{index}@1" for index in range(MAX_CURRENT_SOURCE_REFS - 2))
    _tool_result(provider, "turn-full", "full-last", "full-turn: the last source the fence holds")
    assert len(provider.diagnostics.current_source_refs) == MAX_CURRENT_SOURCE_REFS
    at_bound = json.loads(_recall(provider, "history", "at-bound"))
    assert "error" not in at_bound and at_bound["result"]["status"] != "unavailable"

    _tool_result(provider, "turn-full", "full-over", "full-turn: one source past the fence")
    assert len(provider.diagnostics.current_source_refs) == MAX_CURRENT_SOURCE_REFS
    assert provider.prefetch("where does the orca42 rollout run") == ""
    for mode in _MODES:
        reply = json.loads(_recall(provider, mode, f"over-{mode}"))
        assert "error" not in reply, reply
        assert GAP_CURRENT_SOURCE_REFS_LIMIT in reply["capability_gaps"]
        packet = validate_payload("recall_packet", reply["result"])
        assert (packet["status"], packet["items"], packet["request_id"]) == ("unavailable", [], f"over-{mode}")
        assert packet["gaps"] == ["current_source_refs_limit"]
    status = json.loads(provider.handle_tool_call("status", {}))
    assert GAP_CURRENT_SOURCE_REFS_LIMIT in status["capability_gaps"]

    provider.observe_pre_llm(session_id="TEST-session-1", turn_id="turn-next", user_message="next-turn: orca42 rollout")
    assert len(provider.diagnostics.current_source_refs) == 1
    assert GAP_CURRENT_SOURCE_REFS_LIMIT not in provider.diagnostics.capability_gaps
    assert "blue cluster" in provider.prefetch("where does the orca42 rollout run")
    _assert_fenced_recall(provider, earlier_ref=earlier_ref, marker="next-turn")


@pytest.mark.parametrize("reset", ["pre_llm_turn_id", "turn_start_ordinal", "session_switch", "initialize"])
def test_overflow_clears_exactly_where_current_refs_reset(adapter, initialize_kwargs, reset):
    provider, _clock = adapter
    provider._current_source_refs = [f"ref-{index}" for index in range(MAX_CURRENT_SOURCE_REFS)]
    _tool_result(provider, "turn-over", "over-1", "one source past the fence")
    assert GAP_CURRENT_SOURCE_REFS_LIMIT in provider.diagnostics.capability_gaps
    if reset == "pre_llm_turn_id":
        provider.observe_pre_llm(session_id="TEST-session-1", turn_id="uuid-next", user_message="next message")
    elif reset == "turn_start_ordinal":
        provider.on_turn_start(8, "next message")
    elif reset == "session_switch":
        provider.on_session_switch("TEST-session-2")
    else:
        provider.initialize("TEST-session-1", **initialize_kwargs)
    assert len(provider.diagnostics.current_source_refs) <= 1
    assert GAP_CURRENT_SOURCE_REFS_LIMIT not in provider.diagnostics.capability_gaps
    reply = json.loads(_recall(provider, "history", f"after-{reset}"))
    assert "error" not in reply and GAP_CURRENT_SOURCE_REFS_LIMIT not in reply["capability_gaps"]
    assert reply["result"]["status"] != "unavailable"


def test_worker_keeps_one_active_and_one_coalesced_wakeup():
    worker = AdapterWorker()
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def first():
        calls.append("first")
        started.set()
        release.wait(1.0)

    assert worker.submit(first)
    assert started.wait(1.0)
    for index in range(20):
        assert worker.submit(lambda index=index: calls.append(f"coalesced-{index}"))
    state = worker.shutdown(timeout=0.02)
    assert state["active_tasks"] == 1
    release.set()
    worker.shutdown(timeout=1.0)
    assert len(calls) <= 2


def test_global_hook_dispatch_is_session_scoped_and_conflict_closed(tmp_path, initialize_kwargs):
    home_a = tmp_path / "home-a"
    home_b = tmp_path / "home-b"
    home_a.mkdir()
    home_b.mkdir()
    _, core_a = install_hermes_scope_recall(
        home_a,
        agent_id="agent-a",
        platform="cli",
        user_id="local",
        agent_workspace="workspace-a",
        test_mode=False,
    )
    _, core_b = install_hermes_scope_recall(
        home_b,
        agent_id="agent-b",
        platform="cli",
        user_id="local",
        agent_workspace="workspace-b",
        test_mode=False,
    )
    provider_a = ScopeRecallHermesAdapter(core=core_a)
    provider_b = ScopeRecallHermesAdapter(core=core_b)
    common = dict(
        hermes_home=str(home_a),
        platform="cli",
        user_id="local",
        agent_context="primary",
        agent_identity="agent-a",
        agent_workspace="workspace-a",
    )
    provider_a.initialize("session-a", **common)
    provider_b.initialize(
        "session-b", **dict(common, hermes_home=str(home_b), agent_identity="agent-b", agent_workspace="workspace-b")
    )
    _register_adapter_instance(provider_a)
    _register_adapter_instance(provider_b)
    callback = _global_callback("pre_llm_call")
    callback(session_id="session-a", turn_id="a-1", platform="cli", sender_id="local", user_message="only A")
    callback(session_id="session-b", turn_id="b-1", platform="cli", sender_id="local", user_message="only B")
    assert provider_a.diagnostics.current_source_refs
    assert provider_b.diagnostics.current_source_refs

    provider_b.on_session_switch("session-a")
    before_a = provider_a.diagnostics.current_source_refs
    before_b = provider_b.diagnostics.current_source_refs
    callback(
        session_id="session-a", turn_id="collision", platform="cli", sender_id="local", user_message="must not write"
    )
    assert provider_a.diagnostics.current_source_refs == before_a
    assert provider_b.diagnostics.current_source_refs == before_b
    provider_a.shutdown()
    provider_b.shutdown()
    _unregister_adapter_instance(provider_a)
    _unregister_adapter_instance(provider_b)


def test_post_llm_call_does_not_wait_for_the_adapter_lock(adapter, hermes_home):
    """Hermes calls post_llm_call before it sends the reply, on a thread it waits for.  Under the adapter lock the
    reply waited behind whatever held it, a capture on a busy store or a recall still running, and a callback
    Hermes gave up on (30 s) was then skipped for a minute for every session, with no gap anywhere."""
    import time

    provider, _clock = adapter
    _register_adapter_instance(provider)
    try:
        provider.on_turn_start(9, "TEST 查一下 QX-29", turn_id="turn-9")
        history = [
            {"role": "user", "content": "TEST 查一下 QX-29"},
            {"role": "assistant", "content": "TEST 我先看记录。", "tool_calls": [{"id": "T1"}]},
            {"role": "tool", "tool_call_id": "T1", "content": "TEST 工具输出"},
            {"role": "assistant", "content": "TEST QX-29 已经完成。"},
        ]
        held, release, done = threading.Event(), threading.Event(), threading.Event()

        def busy_capture():
            with provider._lock:
                held.set()
                release.wait(10)

        holder = threading.Thread(target=busy_capture)
        holder.start()
        assert held.wait(5)
        caller = threading.Thread(
            target=lambda: (
                _global_callback("post_llm_call")(
                    session_id="TEST-session-1",
                    turn_id="turn-9",
                    platform="cli",
                    assistant_response="TEST QX-29 已经完成。",
                    conversation_history=history,
                ),
                done.set(),
            )
        )
        started = time.monotonic()
        caller.start()
        returned = done.wait(1.0)
        release.set()
        holder.join(5)
        caller.join(5)
        assert returned, "post_llm_call waited for the adapter lock"
        assert time.monotonic() - started < 1.0
        provider.sync_turn("TEST 查一下 QX-29", "TEST QX-29 已经完成。", session_id="TEST-session-1")
        with sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3") as conn:
            said = [
                row[0]
                for row in conn.execute("SELECT content FROM source_events WHERE role='assistant' ORDER BY rowid")
            ]
        assert said == ["TEST 我先看记录。", "TEST QX-29 已经完成。"]
    finally:
        _unregister_adapter_instance(provider)


def test_a_turn_writes_at_most_64_interim_messages(adapter, monkeypatch):
    """Each interim message is its own write after the reply, under the adapter lock and each waiting for the store:
    a turn of 300 tool steps held that lock for minutes, and the next turn's start waited on it."""
    from types import SimpleNamespace

    provider, _clock = adapter
    provider.on_turn_start(10, "TEST 跑三百步", turn_id="turn-10")
    history = [{"role": "user", "content": "TEST 跑三百步"}]
    for step in range(300):
        history.append({"role": "assistant", "content": f"TEST 第 {step} 步。", "tool_calls": [{"id": f"T{step}"}]})
        history.append({"role": "tool", "tool_call_id": f"T{step}", "content": "TEST 工具输出"})
    history.append({"role": "assistant", "content": "TEST 三百步都跑完了。"})
    provider.observe_post_llm_call(
        session_id="TEST-session-1",
        turn_id="turn-10",
        assistant_response="TEST 三百步都跑完了。",
        conversation_history=history,
    )
    keys = []
    queued = SimpleNamespace(durability="queued", disposition="queued", error_code=None, event_refs=(), gaps=())
    monkeypatch.setattr(
        provider._core,
        "record_host_event",
        lambda _context, event, **kwargs: keys.append(event["source_event_key"]) or queued,
    )
    monkeypatch.setattr(provider._core, "source_by_event_key", lambda *args, **kwargs: None, raising=False)
    provider.sync_turn("TEST 跑三百步", "TEST 三百步都跑完了。", session_id="TEST-session-1")
    assert sum(":interim:" in key for key in keys) == 64
    assert any(":sync_assistant:" in key for key in keys), keys[-3:]
    assert "capture_gap:interim_limit" in provider._diagnostics.pending_outcome_gaps


#: How long a call that must not wait gets to return; only a call that waits reaches it.
_PROMPTLY = 5.0


def _in_thread(call):
    done, box = threading.Event(), {}

    def run():
        try:
            box["value"] = call()
        except BaseException as exc:  # kept for the test to assert on, as a host's hook runner would see it
            box["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return done, box, thread


def _tool_hook(session_id: str, call_id: str):
    return lambda: _global_callback("post_tool_call")(
        session_id=session_id,
        turn_id="turn-1",
        tool_call_id=call_id,
        tool_name="terminal",
        result=f"TEST tool output {call_id}",
        status="success",
    )


def _held_capture(provider, monkeypatch, call_id: str):
    """The store write of the capture whose key names ``call_id`` waits until ``release`` is set."""
    real = provider._core.record_host_event
    entered, release = threading.Event(), threading.Event()

    def record_host_event(context, event, **kwargs):
        if call_id in event["source_event_key"]:
            entered.set()
            release.wait(10)
        return real(context, event, **kwargs)

    monkeypatch.setattr(provider._core, "record_host_event", record_host_event)
    return entered, release


def _held_message(provider, monkeypatch):
    """A message's capture holds its session while its store write waits until ``release`` is set: pre_llm_call
    writes with the session held, as a tool hook no longer does."""
    entered, release = _held_capture(provider, monkeypatch, "turn-held")
    done, _, thread = _in_thread(
        lambda: provider.observe_pre_llm(
            session_id="TEST-session-1", turn_id="turn-held", user_message="TEST a message whose write is held"
        )
    )
    return entered, release, done, thread


def _another_session(installed_core, initialize_kwargs):
    core, clock = installed_core
    other = ScopeRecallHermesAdapter(core=MemoryCore(CoreConfig(core.config.binding), clock=clock), clock=clock)
    other.initialize("TEST-session-2", **initialize_kwargs)
    return other


def _tool_rows(hermes_home) -> int:
    with sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3") as conn:
        return conn.execute("SELECT count(*) FROM source_events WHERE role='tool'").fetchone()[0]


def test_a_hook_does_not_wait_out_its_busy_session(adapter, monkeypatch, caplog):
    """A hook waited for its own session without a limit.  Past Hermes' hook timeout (30 s) the call was abandoned
    and Hermes 0.21.5 then skipped that hook for a minute for every session, Scope Recall registering one callback
    per hook (tianji 2026-09-26: three tool hooks behind their session's prefetch)."""
    provider, _clock = adapter
    monkeypatch.setattr(hooks, "_SESSION_WAIT_CAP_S", 0.05, raising=False)
    entered, release, first, first_thread = _held_message(provider, monkeypatch)
    _register_adapter_instance(provider)
    try:
        assert entered.wait(_PROMPTLY)
        second, _, second_thread = _in_thread(_tool_hook("TEST-session-1", "next-call"))
        assert second.wait(_PROMPTLY), "the hook waited for its busy session"
        assert not first.is_set()
    finally:
        release.set()
        first_thread.join(_PROMPTLY)
        second_thread.join(_PROMPTLY)
        _unregister_adapter_instance(provider)
    assert provider.diagnostics.host_backpressure == {"post_tool_call": 1}
    said = [record.getMessage() for record in caplog.records if " not taken: " in record.getMessage()]
    assert said and said[0].startswith(
        "scope-recall: post_tool_call not taken: this session has been busy in observe_pre_llm for "
    ), said


def test_a_steps_tool_results_are_written_side_by_side(adapter, hermes_home, monkeypatch):
    """Hermes calls the hook for each of a step's parallel tool calls at once.  A capture held the session across its
    store write (1.4-4.4 s on the shared store), and the hooks behind it past their bound were not taken: yuheng 6
    and tianji 2 tool results on 2026-10-03.  The write now runs without the session."""
    provider, _clock = adapter
    monkeypatch.setattr(hooks, "_SESSION_WAIT_CAP_S", 0.05, raising=False)
    entered, release = _held_capture(provider, monkeypatch, "slow-call")
    _register_adapter_instance(provider)
    try:
        first, _, first_thread = _in_thread(_tool_hook("TEST-session-1", "slow-call"))
        assert entered.wait(_PROMPTLY)
        second, _, second_thread = _in_thread(_tool_hook("TEST-session-1", "next-call"))
        assert second.wait(_PROMPTLY), "the hook waited for the other tool result's write"
        assert not first.is_set()
        assert _tool_rows(hermes_home) == 1, "the second tool result was not written while the first one waited"
    finally:
        release.set()
        first_thread.join(_PROMPTLY)
        second_thread.join(_PROMPTLY)
        _unregister_adapter_instance(provider)
    assert first.is_set()
    assert _tool_rows(hermes_home) == 2
    assert provider.diagnostics.host_backpressure is None
    assert len(provider.diagnostics.current_source_refs) == 2


def test_a_shutdown_waits_for_a_tool_result_being_written(adapter, hermes_home, monkeypatch):
    """A tool hook's write runs without the session, which ``sync_turn``'s guard does not cover: a shutdown that came
    meanwhile would close the runtime under it."""
    provider, _clock = adapter
    entered, release = _held_capture(provider, monkeypatch, "slow-call")
    _register_adapter_instance(provider)
    shut_thread = None
    try:
        first, _, first_thread = _in_thread(_tool_hook("TEST-session-1", "slow-call"))
        assert entered.wait(_PROMPTLY)
        shut, _, shut_thread = _in_thread(provider.shutdown)
        assert not shut.wait(0.3), "the shutdown closed the session under a tool result being written"
    finally:
        release.set()
        first_thread.join(_PROMPTLY)
        if shut_thread is not None:
            shut_thread.join(_PROMPTLY)
        _unregister_adapter_instance(provider)
    assert first.is_set() and shut.is_set()
    assert _tool_rows(hermes_home) == 1
    assert "pending_captures" not in provider.diagnostics.shutdown_state
    assert "captures_still_writing" not in provider.diagnostics.shutdown_state


def test_a_tool_result_outliving_the_shutdowns_wait_is_counted_not_raised(adapter, hermes_home, monkeypatch):
    """Past the shutdown's bounded wait the write still lands; its bookkeeping, done in a closed session, raised
    into the host's hook runner (review of 3.5.1)."""
    provider, _clock = adapter
    monkeypatch.setattr(provider_module, "_CAPTURE_DRAIN_WAIT_S", 0.3)
    entered, release = _held_capture(provider, monkeypatch, "slow-call")
    _register_adapter_instance(provider)
    try:
        first, first_box, first_thread = _in_thread(_tool_hook("TEST-session-1", "slow-call"))
        assert entered.wait(_PROMPTLY)
        shut, _, shut_thread = _in_thread(provider.shutdown)
        assert shut.wait(_PROMPTLY), "the shutdown's wait is bounded"
        release.set()
        assert first.wait(_PROMPTLY)
    finally:
        release.set()
        first_thread.join(_PROMPTLY)
        shut_thread.join(_PROMPTLY)
        _unregister_adapter_instance(provider)
    assert "error" not in first_box, first_box
    assert _tool_rows(hermes_home) == 1
    assert provider.diagnostics.shutdown_state["captures_still_writing"] == 1


def test_a_turns_retry_pass_leaves_a_tool_result_being_written_alone(adapter, hermes_home, monkeypatch, caplog):
    """A capture sat in the retry buffer while it wrote; once its write ran without the session, a turn's retry
    pass wrote it a second time, and one of the two was logged as not stored (review of 3.5.1)."""
    provider, _clock = adapter
    real = provider._core.record_host_event
    entered, release = threading.Event(), threading.Event()
    writes = []

    def record_host_event(context, event, **kwargs):
        if "slow-call" in event["source_event_key"]:
            writes.append(event["source_event_key"])
            if len(writes) == 1:
                entered.set()
                release.wait(10)
        return real(context, event, **kwargs)

    monkeypatch.setattr(provider._core, "record_host_event", record_host_event)
    _register_adapter_instance(provider)
    try:
        first, _, first_thread = _in_thread(_tool_hook("TEST-session-1", "slow-call"))
        assert entered.wait(_PROMPTLY)
        synced, sync_box, sync_thread = _in_thread(
            lambda: provider.sync_turn("TEST user words", "TEST reply words", session_id="TEST-session-1")
        )
        assert synced.wait(_PROMPTLY), "the turn waited for the tool result's write"
        assert len(writes) == 1, "the turn's retry pass wrote the tool result being written a second time"
    finally:
        release.set()
        first_thread.join(_PROMPTLY)
        sync_thread.join(_PROMPTLY)
        _unregister_adapter_instance(provider)
    assert "error" not in sync_box, sync_box
    assert _tool_rows(hermes_home) == 1
    assert not [failure for failure in provider.diagnostics.capture_failures if "slow-call" in failure]
    assert not [record for record in caplog.records if "not stored" in record.getMessage()]


@pytest.mark.parametrize("switch", ["session", "audience"])
def test_a_switch_during_a_tool_results_write_keeps_its_session_and_audience(adapter, hermes_home, monkeypatch, switch):
    """The write runs without the session, so a session or audience switch can come in meanwhile: the capture
    keeps the session and scope it was said in, and stays out of the new session's fence (review of 3.5.1)."""
    provider, _clock = adapter
    before = provider._identity
    real = provider._core.source_by_event_key
    entered, release = threading.Event(), threading.Event()

    def source_by_event_key(context, key, *args, **kwargs):
        if "slow-call" in key:
            entered.set()
            release.wait(10)
        return real(context, key, *args, **kwargs)

    monkeypatch.setattr(provider._core, "source_by_event_key", source_by_event_key)
    _register_adapter_instance(provider)
    kwargs = {} if switch == "session" else {"chat_type": "group", "chat_id": "TEST-group-9", "thread_id": "main"}
    try:
        first, first_box, first_thread = _in_thread(_tool_hook("TEST-session-1", "slow-call"))
        assert entered.wait(_PROMPTLY)
        switched, switch_box, switch_thread = _in_thread(lambda: provider.on_session_switch("TEST-session-2", **kwargs))
        assert switched.wait(_PROMPTLY), "the switch waited for the tool result's write"
    finally:
        release.set()
        first_thread.join(_PROMPTLY)
        switch_thread.join(_PROMPTLY)
        _unregister_adapter_instance(provider)
    assert "error" not in first_box and "error" not in switch_box, (first_box, switch_box)
    with sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3") as conn:
        rows = conn.execute("SELECT session_id, scope_id, origin FROM source_events WHERE role='tool'").fetchall()
    assert rows == [(before.stored_session_id(), before.local_scope_id, "tool_observation")]
    assert provider.diagnostics.current_source_refs == ()


def test_prefetch_does_not_wait_out_its_busy_session(adapter, monkeypatch):
    provider, _clock = adapter
    monkeypatch.setattr(prefetch_module, "_PREFETCH_STATE_WAIT_S", 0.05)
    entered, release, first, first_thread = _held_message(provider, monkeypatch)
    _register_adapter_instance(provider)
    try:
        assert entered.wait(_PROMPTLY)
        prefetched, box, prefetch_thread = _in_thread(lambda: provider.prefetch("TEST where does orca42 run"))
        assert prefetched.wait(_PROMPTLY), "prefetch waited for its busy session"
        assert box["value"] == "" and not first.is_set()
    finally:
        release.set()
        first_thread.join(_PROMPTLY)
        prefetch_thread.join(_PROMPTLY)
        _unregister_adapter_instance(provider)
    assert provider.diagnostics.host_backpressure == {"prefetch": 1}


def test_prefetch_does_not_hold_its_session_while_it_recalls(adapter, hermes_home, monkeypatch):
    """Hermes gives a prefetch 8 s and goes on with the turn; held through the recall, the session kept the turn's
    tool hooks waiting behind it (tianxuan 2026-09-30: the prefetch timed out, the tool hook 33 s later)."""
    provider, _clock = adapter
    provider.observe_pre_llm(session_id="TEST-session-1", turn_id="turn-1", user_message="TEST where does orca42 run")
    real = provider._core.recall_packet
    entered, release = threading.Event(), threading.Event()

    def recall_packet(*args, **kwargs):
        entered.set()
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(provider._core, "recall_packet", recall_packet)
    _register_adapter_instance(provider)
    try:
        prefetched, _, prefetch_thread = _in_thread(lambda: provider.prefetch("TEST where does orca42 run"))
        assert entered.wait(_PROMPTLY)
        hooked, _, hook_thread = _in_thread(_tool_hook("TEST-session-1", "during-recall"))
        assert hooked.wait(_PROMPTLY), "the tool hook waited for its session's recall"
        assert not prefetched.is_set()
    finally:
        release.set()
        prefetch_thread.join(_PROMPTLY)
        hook_thread.join(_PROMPTLY)
        _unregister_adapter_instance(provider)
    assert prefetched.is_set()
    assert _tool_rows(hermes_home) == 1


def test_a_prefetch_given_up_on_leaves_the_next_turn_its_own(adapter, monkeypatch):
    """Hermes stops waiting for a prefetch after 8 s and the next turn begins: its pre_llm_call marks its UUID
    pending, so that its turn start keeps that UUID and its current-source fence.  The late prefetch, recalling
    without the lock, must not clear that mark: the turn start then put the turn number in the UUID's place."""
    provider, _clock = adapter
    provider.observe_pre_llm(session_id="TEST-session-1", turn_id="turn-1", user_message="TEST where does orca42 run")
    real = provider._core.recall_packet
    entered, release = threading.Event(), threading.Event()

    def recall_packet(*args, **kwargs):
        entered.set()
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(provider._core, "recall_packet", recall_packet)
    prefetched, _, prefetch_thread = _in_thread(lambda: provider.prefetch("TEST where does orca42 run"))
    try:
        assert entered.wait(_PROMPTLY)
        provider.observe_pre_llm(session_id="TEST-session-1", turn_id="turn-2", user_message="TEST and orca43")
    finally:
        release.set()
        prefetch_thread.join(_PROMPTLY)
    assert prefetched.is_set()
    provider.on_turn_start(2, "TEST and orca43", session_id="TEST-session-1")
    assert provider._active_turn_id == "turn-2"


def test_the_next_turn_starts_while_the_last_one_is_written(adapter, monkeypatch):
    """sync_turn runs on Hermes' memory worker after the reply.  Holding the session for the whole turn, it kept the
    next turn's start waiting on it (3.56 s behind 14 writes of 0.25 s, measured on 3.4.9)."""
    provider, _clock = adapter
    provider.on_turn_start(10, "TEST 跑三步", turn_id="turn-10")
    history = [{"role": "user", "content": "TEST 跑三步"}]
    for step in range(3):
        history.append({"role": "assistant", "content": f"TEST 第 {step} 步。", "tool_calls": [{"id": f"T{step}"}]})
        history.append({"role": "tool", "tool_call_id": f"T{step}", "content": "TEST 工具输出"})
    history.append({"role": "assistant", "content": "TEST 三步都跑完了。"})
    provider.observe_post_llm_call(
        session_id="TEST-session-1",
        turn_id="turn-10",
        assistant_response="TEST 三步都跑完了。",
        conversation_history=history,
    )
    keys = []
    entered, release = threading.Event(), threading.Event()
    queued = SimpleNamespace(durability="queued", disposition="queued", error_code=None, event_refs=(), gaps=())

    def record_host_event(_context, event, **kwargs):
        keys.append(event["source_event_key"])
        if len(keys) == 1:
            entered.set()
            release.wait(10)
        return queued

    monkeypatch.setattr(provider._core, "record_host_event", record_host_event)
    monkeypatch.setattr(provider._core, "source_by_event_key", lambda *args, **kwargs: None, raising=False)
    synced, _, sync_thread = _in_thread(
        lambda: provider.sync_turn("TEST 跑三步", "TEST 三步都跑完了。", session_id="TEST-session-1")
    )
    start_thread = None
    try:
        assert entered.wait(_PROMPTLY)
        started, _, start_thread = _in_thread(lambda: provider.on_turn_start(11, "TEST next", turn_id="turn-11"))
        assert started.wait(_PROMPTLY), "the next turn's start waited for the last turn's writes"
        assert not synced.is_set()
    finally:
        release.set()
        sync_thread.join(_PROMPTLY)
        if start_thread is not None:
            start_thread.join(_PROMPTLY)
    assert synced.is_set()
    assert sum(":interim:" in key for key in keys) == 3
    assert any(":sync_assistant:" in key for key in keys), keys
    assert provider._active_turn_id == "turn-11"


def test_a_hook_past_the_host_timeout_is_said_and_counted(adapter, monkeypatch, caplog):
    provider, _clock = adapter
    monkeypatch.setattr(hooks, "host_hook_timeout", lambda: 0.02, raising=False)
    observe = provider._observe_post_tool_call

    def slow_observe(**kwargs):
        time.sleep(0.1)  # past the shortened host timeout by several 15.6 ms clock ticks
        return observe(**kwargs)

    # The dispatcher calls the unlocked observer, the seam that must be slowed (review of 3.5.1).
    monkeypatch.setattr(provider, "_observe_post_tool_call", slow_observe)
    _register_adapter_instance(provider)
    try:
        _tool_hook("TEST-session-1", "overrun-call")()
    finally:
        _unregister_adapter_instance(provider)
    said = [record.getMessage() for record in caplog.records if "past the host's" in record.getMessage()]
    assert len(said) == 1 and said[0].startswith("scope-recall: post_tool_call took "), said
    assert provider.diagnostics.host_backpressure == {"post_tool_call_overran": 1}


def test_a_host_that_never_times_out_a_hook_hears_of_no_skip(adapter, monkeypatch, caplog):
    """Hermes reads a hook timeout of 0 or less as none: it waits for the hook and skips nothing, so nothing may say
    that it does (review of 3.4.10)."""
    provider, _clock = adapter
    monkeypatch.setattr(hooks, "host_hook_timeout", lambda: None)
    _register_adapter_instance(provider)
    try:
        _tool_hook("TEST-session-1", "no-timeout-call")()
    finally:
        _unregister_adapter_instance(provider)
    assert not [record for record in caplog.records if "past the host's" in record.getMessage()]
    assert provider.diagnostics.host_backpressure is None


def test_a_hook_timeout_of_zero_is_read_as_none(monkeypatch):
    """As Hermes reads it: ``plugins.hook_callback_timeout`` of 0 or less waits for a hook however long it takes."""
    import sys
    import types

    plugins = types.ModuleType("hermes_cli.plugins")
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)
    for configured, read in ((0, None), (-5, None), (45, 45.0)):
        plugins._resolve_hook_callback_timeout = lambda value=configured: value
        assert hooks.host_hook_timeout() == read, configured


def _turn_with_interim(provider, turn: str, steps: int) -> None:
    provider.on_turn_start(10, "TEST 跑几步", turn_id=turn)
    history = [{"role": "user", "content": "TEST 跑几步"}]
    for step in range(steps):
        history.append({"role": "assistant", "content": f"TEST 第 {step} 步。", "tool_calls": [{"id": f"T{step}"}]})
        history.append({"role": "tool", "tool_call_id": f"T{step}", "content": "TEST 工具输出"})
    history.append({"role": "assistant", "content": "TEST 跑完了。"})
    provider.observe_post_llm_call(
        session_id="TEST-session-1", turn_id=turn, assistant_response="TEST 跑完了。", conversation_history=history
    )


def test_a_shutdown_waits_for_the_turn_being_written(adapter, monkeypatch):
    """sync_turn gives the session back between its captures; a shutdown that came in between closed the runtime
    under the rest of the turn, and the reply was never written (review of 3.4.10).  It waits, as on 3.4.9."""
    provider, _clock = adapter
    _turn_with_interim(provider, "turn-10", 3)
    keys = []
    entered, release = threading.Event(), threading.Event()
    queued = SimpleNamespace(durability="queued", disposition="queued", error_code=None, event_refs=(), gaps=())

    def record_host_event(_context, event, **kwargs):
        keys.append(event["source_event_key"])
        if len(keys) == 1:
            entered.set()
            release.wait(10)
        return queued

    monkeypatch.setattr(provider._core, "record_host_event", record_host_event)
    monkeypatch.setattr(provider._core, "source_by_event_key", lambda *args, **kwargs: None, raising=False)
    synced, _, sync_thread = _in_thread(
        lambda: provider.sync_turn("TEST 跑几步", "TEST 跑完了。", session_id="TEST-session-1")
    )
    shut_thread = None
    try:
        assert entered.wait(_PROMPTLY)
        shut, _, shut_thread = _in_thread(provider.shutdown)
        assert not shut.wait(0.3), "the shutdown closed the session under the turn being written"
    finally:
        release.set()
        sync_thread.join(_PROMPTLY)
        if shut_thread is not None:
            shut_thread.join(_PROMPTLY)
    assert synced.is_set() and shut.is_set()
    assert sum(":interim:" in key for key in keys) == 3
    assert any(":sync_assistant:" in key for key in keys), keys


def test_a_turn_is_dated_when_its_writing_begins(adapter, monkeypatch):
    """The next turn's hooks may write between this turn's captures; dated as each was reached, the reply was said
    after the next turn's message (review of 3.4.10)."""
    provider, _clock = adapter
    _turn_with_interim(provider, "turn-10", 2)
    times = iter(f"2026-10-01T12:00:{second:02d}Z" for second in range(60))
    monkeypatch.setattr(provider, "_utc_now", lambda: next(times))
    dated = {}
    queued = SimpleNamespace(durability="queued", disposition="queued", error_code=None, event_refs=(), gaps=())

    def record_host_event(_context, event, **kwargs):
        dated[event["source_event_key"]] = event.get("occurred_at")
        return queued

    monkeypatch.setattr(provider._core, "record_host_event", record_host_event)
    monkeypatch.setattr(provider._core, "source_by_event_key", lambda *args, **kwargs: None, raising=False)
    provider.sync_turn("TEST 跑几步", "TEST 跑完了。", session_id="TEST-session-1")
    assert [when for key, when in dated.items() if ":sync_assistant:" in key] == ["2026-10-01T12:00:00Z"], dated


def test_a_skipped_pre_llm_call_leaves_its_turn_id_for_the_turn(adapter, monkeypatch):
    """Hermes 0.21.5 starts a turn without its id: a pre_llm_call that could not wait for its session was the only
    call to bring it, and the interim messages post_llm_call names by it were dropped (review of 3.4.10)."""
    provider, _clock = adapter
    monkeypatch.setattr(hooks, "_SESSION_WAIT_CAP_S", 0.05)
    _register_adapter_instance(provider)
    provider._lock.acquire()
    try:
        done, _, thread = _in_thread(
            lambda: _global_callback("pre_llm_call")(
                session_id="TEST-session-1", turn_id="turn-uuid-7", user_message="TEST 第七轮"
            )
        )
        assert done.wait(_PROMPTLY)
        thread.join(_PROMPTLY)
    finally:
        provider._lock.release()
        _unregister_adapter_instance(provider)
    provider.on_turn_start(7, "TEST 第七轮", session_id="TEST-session-1")
    assert provider._active_turn_id == "turn-uuid-7"


def test_the_dispatcher_callbacks_carry_scope_recall_names():
    """Hermes names a callback in its timeout and skip lines; every plugin's closure called ``callback`` read alike."""
    assert {event: _global_callback(event).__name__ for event in _SUPPORTED_HOOKS} == {
        event: f"scope_recall_{event}" for event in _SUPPORTED_HOOKS
    }


def test_another_session_never_waits_for_this_one(adapter, installed_core, initialize_kwargs, hermes_home, monkeypatch):
    """Each Hermes session has an adapter and a lock of its own, and a hook goes to the one bound to its session."""
    provider, _clock = adapter
    other = _another_session(installed_core, initialize_kwargs)
    # Held by a message's capture, which keeps its session through its write, as a tool result's no longer does
    # (review of 3.5.1: held by a tool capture, one lock shared by both sessions passed).
    entered, release, first, first_thread = _held_message(provider, monkeypatch)
    _register_adapter_instance(provider)
    _register_adapter_instance(other)
    try:
        assert entered.wait(_PROMPTLY)
        second, _, second_thread = _in_thread(_tool_hook("TEST-session-2", "other-call"))
        assert second.wait(_PROMPTLY), "another session's hook waited for this one"
        assert not first.is_set()
    finally:
        release.set()
        first_thread.join(_PROMPTLY)
        second_thread.join(_PROMPTLY)
        _unregister_adapter_instance(provider)
        _unregister_adapter_instance(other)
        other.shutdown()
    assert first.is_set()
    assert _tool_rows(hermes_home) == 1


def test_another_sessions_hook_waits_only_its_write_budget_on_a_held_store(
    adapter, installed_core, initialize_kwargs, hermes_home
):
    """The store is shared: a session's write waits at most its capture's 1 s budget for another's, and what it
    could not write is kept to retry."""
    provider, _clock = adapter
    other = _another_session(installed_core, initialize_kwargs)
    held, release = threading.Event(), threading.Event()

    def long_write():
        with provider._core.storage.write(provider._identity.trusted_context(mutation=True), remaining_seconds=1.0):
            held.set()
            release.wait(10)

    writer = threading.Thread(target=long_write, daemon=True)
    writer.start()
    _register_adapter_instance(other)
    try:
        assert held.wait(_PROMPTLY)
        hooked, _, hook_thread = _in_thread(_tool_hook("TEST-session-2", "other-call"))
        assert hooked.wait(_PROMPTLY), "the hook waited for another session's write"
        assert writer.is_alive()
        assert other.diagnostics.pending_capture_identities
    finally:
        release.set()
        writer.join(_PROMPTLY)
        _unregister_adapter_instance(other)
    other._retry.write_buffered()
    other.shutdown()
    assert _tool_rows(hermes_home) == 1


def _busy_store(provider, monkeypatch, refusals: int, *, delay: float = 0.0):
    """The store refuses the next ``refusals`` writes as a writer held past a capture's budget does
    (DEADLINE_EXCEEDED); the rest go through, each ``delay`` seconds slow."""
    from scope_recall.contracts import ContractError

    real = provider._core.record_host_event
    left = {"refusals": refusals}

    def record_host_event(context, event, **kwargs):
        if left["refusals"] > 0:
            left["refusals"] -= 1
            raise ContractError("DEADLINE_EXCEEDED", "TEST writer busy")
        time.sleep(delay)
        return real(context, event, **kwargs)

    monkeypatch.setattr(provider._core, "record_host_event", record_host_event)
    return left


def _until(predicate, seconds=_PROMPTLY):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)
    return predicate()


def test_a_buffered_tool_result_is_written_again_without_a_turn_s_end(adapter, hermes_home, monkeypatch):
    """Hermes runs ``sync_turn`` only after a turn with a message and a reply: after a turn it injected, interrupted
    or got no reply for, a buffered tool result waited; an idle agent evicted from Hermes' cache keeps its adapter
    without a shutdown, and a gateway restart then dropped it (tianji, 10 tool results on 2026-10-04).  The retry
    thread writes it, and ends once the buffer is empty."""
    provider, _clock = adapter
    monkeypatch.setattr(capture_retry, "_RETRY_EVERY_S", 3600.0)  # held until the capture is seen kept
    _busy_store(provider, monkeypatch, 1)
    _tool_result(provider, "turn-1", "busy-call", "TEST tool output busy-call")
    assert len(provider._retry.captures) == 1 and _tool_rows(hermes_home) == 0
    monkeypatch.setattr(capture_retry, "_RETRY_EVERY_S", 0.05)
    provider._retry.wake.set()
    assert _until(lambda: _tool_rows(hermes_home) == 1), "no turn ended, and the buffer was not written again"
    assert _until(lambda: provider._retry.thread is None) and not provider._retry.captures


def test_a_retry_pass_writes_every_buffered_capture_it_has_time_for(adapter, hermes_home, monkeypatch):
    """At a capture's own 1 s a pass wrote about one of the buffered tool results, each write 1-4 s on the busy
    shared store (2026-10-04).  The thread's pass has 5 s; a turn's end keeps 1 s, on Hermes' single memory worker,
    which the next turn's writes queue behind (review of 3.6.1)."""
    provider, _clock = adapter
    monkeypatch.setattr(capture_retry, "_RETRY_EVERY_S", 3600.0)
    _busy_store(provider, monkeypatch, 4, delay=0.4)
    for index in range(4):
        _tool_result(provider, "turn-1", f"busy-call-{index}", f"TEST tool output {index}")
    assert len(provider._retry.captures) == 4
    provider._retry.write_buffered(release=True, seconds=capture_retry._RETRY_PASS_SECONDS)
    assert _tool_rows(hermes_home) == 4 and not provider._retry.captures

    passes = []
    real = provider._retry.write_buffered

    def recorded(**kwargs):
        passes.append((threading.current_thread().name, kwargs.get("seconds", capture.CAPTURE_TIMEOUT_S)))
        return real(**kwargs)

    monkeypatch.setattr(provider._retry, "write_buffered", recorded)
    _busy_store(provider, monkeypatch, 2)  # the capture, and the turn's retry of it: the thread then writes it
    _tool_result(provider, "turn-2", "busy-call-again", "TEST tool output again")
    provider.sync_turn("TEST user words", "TEST reply words", session_id="TEST-session-1")
    monkeypatch.setattr(capture_retry, "_RETRY_EVERY_S", 0.05)
    provider._retry.wake.set()
    assert _until(lambda: any(name == "scope-recall-capture-retry" for name, _ in passes))
    assert ("scope-recall-capture-retry", capture_retry._RETRY_PASS_SECONDS) in passes
    assert [seconds for name, seconds in passes if name != "scope-recall-capture-retry"] == [1.0], passes


def test_a_capture_that_cannot_be_written_is_given_up_and_said(adapter, hermes_home, monkeypatch, caplog):
    """Kept for good, a capture of a full inbox, or of an installation whose scopes changed under a running gateway,
    was retried every 30 s for the life of the process, and its thread held an evicted agent's adapter (review of
    3.6.1).  Past its time it is dropped and logged as lost, and the thread ends."""
    provider, _clock = adapter
    monkeypatch.setattr(capture_retry, "_RETRY_EVERY_S", 3600.0)
    monkeypatch.setattr(capture_retry, "_RETRY_GIVE_UP_S", 0.2)
    _busy_store(provider, monkeypatch, 10**6)
    _tool_result(provider, "turn-1", "never-call", "TEST tool output never")
    _tool_result(provider, "turn-1", "unverified-call", "TEST tool output unverified")
    assert len(provider._retry.captures) == 2
    time.sleep(0.3)
    provider._retry.write_buffered(seconds=_PROMPTLY)  # the store refuses at once: the time costs nothing
    assert not provider._retry.captures and _tool_rows(hermes_home) == 0
    assert len([record for record in caplog.records if "still failing after" in record.getMessage()]) == 2

    _tool_result(provider, "turn-2", "unverified-call-2", "TEST tool output unverified 2")
    monkeypatch.setattr(capture_retry, "load_binding_for_home", lambda home: (_ for _ in ()).throw(OSError()))
    time.sleep(0.3)
    provider._retry.write_buffered(seconds=0.1)
    assert not provider._retry.captures, "an authorization that could not be read kept it for good"
    monkeypatch.setattr(capture_retry, "_RETRY_EVERY_S", 0.05)
    provider._retry.wake.set()
    assert _until(lambda: provider._retry.thread is None), "the thread outlived an empty buffer"


def test_a_retry_pass_that_raises_still_gives_up_what_is_past_its_time(adapter, hermes_home, monkeypatch, caplog):
    """A manifest that is JSON but not an object raised past the pass's own handling, before any capture's give-up:
    the thread retried for good and held an evicted agent's adapter (review of 3.6.1)."""
    provider, _clock = adapter
    monkeypatch.setattr(capture_retry, "_RETRY_EVERY_S", 3600.0)
    monkeypatch.setattr(capture_retry, "_RETRY_GIVE_UP_S", 0.2)
    _busy_store(provider, monkeypatch, 10**6)
    _tool_result(provider, "turn-1", "raising-call", "TEST tool output raising")
    monkeypatch.setattr(
        capture_retry,
        "load_binding_for_home",
        lambda home: (_ for _ in ()).throw(AttributeError("TEST not an object")),
    )
    time.sleep(0.3)
    monkeypatch.setattr(capture_retry, "_RETRY_EVERY_S", 0.05)
    provider._retry.wake.set()
    assert _until(lambda: not provider._retry.captures), "a pass that raised kept the capture for good"
    assert _until(lambda: provider._retry.thread is None)
    assert [record for record in caplog.records if "still failing after" in record.getMessage()]


def test_a_capture_being_written_is_not_given_up_under_its_writer(adapter, monkeypatch):
    provider, _clock = adapter
    monkeypatch.setattr(capture_retry, "_RETRY_EVERY_S", 3600.0)
    monkeypatch.setattr(capture_retry, "_RETRY_GIVE_UP_S", 0.0)
    _busy_store(provider, monkeypatch, 1)
    _tool_result(provider, "turn-1", "busy-call", "TEST tool output busy-call")
    (key,) = provider._retry.captures
    time.sleep(0.05)  # past its 0 s by more than one tick of Windows' monotonic clock (15.6 ms)
    with provider._lock:
        provider._retry.in_flight.add(key)
        try:
            assert provider._retry.give_up_expired(tuple(provider._retry.captures.items())) == []
        finally:
            provider._retry.in_flight.discard(key)
        assert provider._retry.give_up_expired(tuple(provider._retry.captures.items())) == [key]


def test_the_adapter_s_parts_log_under_its_name():
    """Hermes writes the logger's name into each line, and a logging configuration may name it: the lines of the
    adapter's parts (capture, its retry, a turn, the session binding) kept the adapter's name when they moved."""
    from scope_recall.adapters.hermes import session_binding, turn_capture

    for module in (capture, capture_retry, session_binding, turn_capture):
        assert module._log.name == provider_module._log.name == "scope_recall.adapters.hermes.provider", module


def test_a_capture_kept_to_retry_is_said_once_until_it_is_stored(adapter, hermes_home, monkeypatch, caplog):
    """Each failed retry said its line again: driven by the thread, a line per capture every 30 s, idle or not
    (review of 3.6.1).  It is said once when it is kept, and once when it is stored."""
    import logging

    caplog.set_level(logging.INFO, logger="scope_recall.adapters.hermes.provider")
    provider, _clock = adapter
    monkeypatch.setattr(capture_retry, "_RETRY_EVERY_S", 3600.0)
    _busy_store(provider, monkeypatch, 6)
    _tool_result(provider, "turn-1", "busy-call", "TEST tool output busy-call")
    monkeypatch.setattr(capture_retry, "_RETRY_EVERY_S", 0.02)
    provider._retry.wake.set()
    # The row is in the store a moment before its line is said, on the retry thread.
    assert _until(lambda: any("stored on retry" in record.getMessage() for record in caplog.records))
    assert _tool_rows(hermes_home) == 1
    lines = [record for record in caplog.records if "busy-call" in record.getMessage()]
    assert [(record.levelname, record.getMessage().split(":")[1].strip()) for record in lines] == [
        ("WARNING", "not stored (exception), kept to retry"),
        ("INFO", "stored on retry"),
    ], lines


def test_a_shutdown_on_a_held_store_spends_at_most_its_short_pass(adapter, hermes_home, monkeypatch):
    """On a writer held past every budget a shutdown spent a whole 5 s pass for each adapter that kept anything,
    holding up a gateway's planned stop (review of 3.6.1)."""
    from scope_recall.contracts import ContractError

    provider, _clock = adapter
    monkeypatch.setattr(capture_retry, "_RETRY_EVERY_S", 3600.0)
    _busy_store(provider, monkeypatch, 1)
    _tool_result(provider, "turn-1", "held-call", "TEST tool output held")

    def held(context, event, *, remaining_seconds=1.0, **kwargs):
        time.sleep(remaining_seconds)  # waits out its whole time on the writer, as a held lease makes it
        raise ContractError("DEADLINE_EXCEEDED", "TEST writer held")

    monkeypatch.setattr(provider._core, "record_host_event", held)
    started = time.monotonic()
    provider.shutdown()
    assert time.monotonic() - started < capture_retry.SHUTDOWN_RETRY_SECONDS + 1.5
    assert capture_retry.SHUTDOWN_RETRY_SECONDS == 2.0


def test_a_turn_s_end_waits_for_a_held_capture_no_longer_than_a_capture_s_time(adapter, hermes_home, monkeypatch):
    """A turn's own message and reply were written only after a 5 s retry of a capture the store could not take,
    holding Hermes' single memory worker that long (review of 3.6.1)."""
    from scope_recall.contracts import ContractError

    provider, _clock = adapter
    monkeypatch.setattr(capture_retry, "_RETRY_EVERY_S", 3600.0)
    _busy_store(provider, monkeypatch, 1)
    _tool_result(provider, "turn-1", "held-call", "TEST tool output held")
    real = provider._core.record_host_event
    first_own = []

    def record_host_event(context, event, *, remaining_seconds=1.0, **kwargs):
        if "held-call" in event["source_event_key"]:
            time.sleep(remaining_seconds)  # a writer held past the retry's whole time
            raise ContractError("DEADLINE_EXCEEDED", "TEST writer held")
        first_own.append(time.monotonic())
        return real(context, event, remaining_seconds=remaining_seconds, **kwargs)

    monkeypatch.setattr(provider._core, "record_host_event", record_host_event)
    started = time.monotonic()
    provider.sync_turn("TEST user words", "TEST reply words", session_id="TEST-session-1")
    assert first_own and first_own[0] - started < capture.CAPTURE_TIMEOUT_S + 1.5, first_own


def test_a_shutdown_leaves_a_replay_still_in_flight_to_its_thread_and_says_its_end(
    adapter, hermes_home, monkeypatch, caplog
):
    """A replay that outlived the shutdown's drain was written once more by the shutdown's pass; and its end, once
    it came, was never said (reviews of 3.6.1)."""
    import logging

    caplog.set_level(logging.INFO, logger="scope_recall.adapters.hermes.provider")
    provider, _clock = adapter
    monkeypatch.setattr(capture_retry, "_RETRY_EVERY_S", 3600.0)
    monkeypatch.setattr(provider_module, "_CAPTURE_DRAIN_WAIT_S", 0.3)
    _busy_store(provider, monkeypatch, 1)
    _tool_result(provider, "turn-1", "slow-call", "TEST tool output slow")
    real = provider._core.record_host_event
    entered, release, writers = threading.Event(), threading.Event(), []

    def record_host_event(context, event, **kwargs):
        if "slow-call" in event["source_event_key"]:
            writers.append(threading.current_thread().name)
            entered.set()
            release.wait(10)
        return real(context, event, **kwargs)

    monkeypatch.setattr(provider._core, "record_host_event", record_host_event)
    monkeypatch.setattr(capture_retry, "_RETRY_EVERY_S", 0.02)
    provider._retry.wake.set()
    assert entered.wait(_PROMPTLY), "the thread did not write the kept capture again"
    shut, _, shut_thread = _in_thread(provider.shutdown)
    assert shut.wait(_PROMPTLY), "the shutdown waited out the replay"
    release.set()
    shut_thread.join(_PROMPTLY)
    assert _until(lambda: any("stored on retry" in record.getMessage() for record in caplog.records))
    assert writers == ["scope-recall-capture-retry"], writers
    assert _tool_rows(hermes_home) == 1
    assert [record for record in caplog.records if "still being written at shutdown" in record.getMessage()]


def test_a_shutdown_leaves_a_capture_another_pass_is_writing_to_it(adapter, hermes_home, monkeypatch, caplog):
    provider, _clock = adapter
    monkeypatch.setattr(capture_retry, "_RETRY_EVERY_S", 3600.0)
    _busy_store(provider, monkeypatch, 1)
    _tool_result(provider, "turn-1", "busy-call", "TEST tool output busy-call")
    (key,) = provider._retry.captures
    provider._retry.in_flight.add(key)  # the thread's write of it runs
    provider._retry.write_buffered(seconds=_PROMPTLY, force=True)
    assert _tool_rows(hermes_home) == 0, "a shutdown's pass wrote a capture another pass was writing"
    provider.shutdown()
    assert [record for record in caplog.records if "still being written at shutdown" in record.getMessage()]
    provider._retry.in_flight.discard(key)


@pytest.mark.parametrize("next_user", ["TEST-user", "TEST-unknown-user"])
def test_a_session_switch_keeps_the_buffer_and_writes_it_in_the_session_it_was_said_in(
    adapter, hermes_home, initialize_kwargs, monkeypatch, next_user
):
    """A session started again in the same adapter cleared the buffer, and the tool results that had only met a busy
    store were lost without a word.  Each is written under its own scope's grant: a next session of another audience,
    here one with none at all, does not take it away."""
    provider, _clock = adapter
    monkeypatch.setattr(capture_retry, "_RETRY_EVERY_S", 3600.0)
    _busy_store(provider, monkeypatch, 1)
    _tool_result(provider, "turn-1", "busy-call", "TEST tool output busy-call")
    (said_in,) = [pending.context.session_id for pending in provider._retry.captures.values()]
    provider.initialize("TEST-session-2", **{**initialize_kwargs, "user_id": next_user})
    assert len(provider._retry.captures) == 1, "the session switch dropped the buffer"
    provider._retry.write_buffered(seconds=_PROMPTLY)
    with sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3") as conn:
        rows = conn.execute("SELECT session_id FROM source_events WHERE role='tool'").fetchall()
    assert rows == [(said_in,)], "written in the session it was said in"
    assert said_in != provider._identity.stored_session_id()


def test_a_capture_whose_scope_was_taken_away_is_dropped_and_said(adapter, hermes_home, monkeypatch, caplog):
    """The scope the capture was said in is no longer granted: nothing is written to it, and the drop is logged,
    where it used to leave a gap only in the process's memory."""
    from dataclasses import replace

    provider, _clock = adapter
    monkeypatch.setattr(capture_retry, "_RETRY_EVERY_S", 3600.0)
    _busy_store(provider, monkeypatch, 1)
    _tool_result(provider, "turn-1", "busy-call", "TEST tool output busy-call")
    real = capture_retry.resolve_runtime_audience
    monkeypatch.setattr(
        capture_retry,
        "resolve_runtime_audience",
        lambda manifest, scope: replace(real(manifest, scope), writable_scope_ids=frozenset()),
    )
    provider._retry.write_buffered(seconds=_PROMPTLY)
    assert _tool_rows(hermes_home) == 0 and not provider._retry.captures
    assert [
        record
        for record in caplog.records
        if "not stored (authorization revoked), dropped" in record.getMessage() and "busy-call" in record.getMessage()
    ]


def test_a_shutdown_writes_the_buffer_once_more_and_says_what_it_could_not(
    adapter, installed_core, initialize_kwargs, hermes_home, monkeypatch, caplog
):
    """A shutdown reported the buffer as pending in memory and dropped it: every gateway restart lost what it held
    (2026-10-04)."""
    provider, _clock = adapter
    monkeypatch.setattr(capture_retry, "_RETRY_EVERY_S", 3600.0)
    _busy_store(provider, monkeypatch, 1)
    _tool_result(provider, "turn-1", "busy-call", "TEST tool output busy-call")
    provider.shutdown()
    assert _tool_rows(hermes_home) == 1, "the shutdown dropped what the store could take by then"
    assert not [record for record in caplog.records if "still failing at shutdown" in record.getMessage()]

    other = _another_session(installed_core, initialize_kwargs)
    monkeypatch.setattr(capture_retry, "_RETRY_EVERY_S", 3600.0)
    _busy_store(other, monkeypatch, 10**6)
    _tool_result(other, "turn-2", "still-busy-call", "TEST tool output still-busy-call")
    other.shutdown()
    assert [
        record
        for record in caplog.records
        if "not stored (still failing at shutdown), lost" in record.getMessage()
        and "still-busy-call" in record.getMessage()
    ]
    assert not other._retry.captures


def test_a_shutdown_ends_the_retry_thread_and_one_pass_runs_at_a_time(adapter, hermes_home, monkeypatch):
    provider, _clock = adapter
    monkeypatch.setattr(capture_retry, "_RETRY_EVERY_S", 3600.0)
    _busy_store(provider, monkeypatch, 1)  # the store takes the next write
    _tool_result(provider, "turn-1", "busy-call-a", "TEST tool output a")
    thread = provider._retry.thread
    assert thread is not None and thread.is_alive()
    with provider._lock:
        provider._retry.retrying = True  # another pass runs
    provider._retry.write_buffered(seconds=_PROMPTLY)
    assert len(provider._retry.captures) == 1 and _tool_rows(hermes_home) == 0, "a second pass ran beside the first"
    with provider._lock:
        provider._retry.retrying = False
    provider.shutdown()
    thread.join(_PROMPTLY)
    assert not thread.is_alive() and provider._retry.thread is None
