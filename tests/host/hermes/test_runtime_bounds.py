"""P11 runtime bounds: turn-local echo fences, worker coalescing, and hooks."""
from __future__ import annotations

import json
import threading
import time
import sqlite3
from types import SimpleNamespace

import pytest

from scope_recall.adapters.hermes import ScopeRecallHermesAdapter, install_hermes_scope_recall
from scope_recall.adapters.hermes import hooks, provider as provider_module
from scope_recall.adapters.hermes.hooks import (
    _SUPPORTED_HOOKS, _global_callback, _register_adapter_instance, _unregister_adapter_instance,
)
from scope_recall.adapters.hermes.provider import GAP_CURRENT_SOURCE_REFS_LIMIT
from scope_recall.adapters.hermes.worker import AdapterWorker
from scope_recall.contracts import validate_payload
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.events import MAX_SEGMENT_CHARS
from scope_recall.core.retrieval import MAX_CURRENT_SOURCE_REFS

_MODES = ("history", "current", "auto")


def _recall(provider, mode: str, request_id: str) -> str:
    return provider.handle_tool_call("recall", {
        "protocol_version": "1.1", "request_id": request_id, "query": "where does the orca42 rollout run",
        "mode": mode, "max_items": 6, "budget_tokens": 4096,
    })


def _tool_result(provider, turn_id: str, call_id: str, result: str, *, tool_name: str = "terminal") -> None:
    provider.observe_post_tool_call(session_id="TEST-session-1", turn_id=turn_id, tool_call_id=call_id,
                                    tool_name=tool_name, result=result, status="success")


def _earlier_turn_ref(provider) -> str:
    """A fact from a finished turn, which recall must keep finding."""
    provider.observe_pre_llm(session_id="TEST-session-1", turn_id="turn-earlier",
                             user_message="The orca42 rollout runs from the blue cluster.")
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
    provider.observe_pre_llm(session_id="TEST-session-1", turn_id="turn-heavy",
                             user_message="heavy-turn: check where the orca42 rollout runs")
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
    provider.observe_pre_llm(session_id="TEST-session-1", turn_id="turn-long",
                             user_message="long-turn: read the orca42 rollout log")
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
    common = dict(hermes_home=str(home_a), platform="cli", user_id="local", agent_context="primary", agent_identity="agent-a", agent_workspace="workspace-a")
    provider_a.initialize("session-a", **common)
    provider_b.initialize("session-b", **dict(common, hermes_home=str(home_b), agent_identity="agent-b", agent_workspace="workspace-b"))
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
    callback(session_id="session-a", turn_id="collision", platform="cli", sender_id="local", user_message="must not write")
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
        caller = threading.Thread(target=lambda: (_global_callback("post_llm_call")(
            session_id="TEST-session-1", turn_id="turn-9", platform="cli", assistant_response="TEST QX-29 已经完成。",
            conversation_history=history), done.set()))
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
            said = [row[0] for row in conn.execute("SELECT content FROM source_events WHERE role='assistant' ORDER BY rowid")]
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
    provider.observe_post_llm_call(session_id="TEST-session-1", turn_id="turn-10",
                                   assistant_response="TEST 三百步都跑完了。", conversation_history=history)
    keys = []
    queued = SimpleNamespace(durability="queued", disposition="queued", error_code=None, event_refs=(), gaps=())
    monkeypatch.setattr(provider._core, "record_host_event",
                        lambda _context, event, **kwargs: keys.append(event["source_event_key"]) or queued)
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
        finally:
            done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return done, box, thread


def _tool_hook(session_id: str, call_id: str):
    return lambda: _global_callback("post_tool_call")(
        session_id=session_id, turn_id="turn-1", tool_call_id=call_id, tool_name="terminal",
        result=f"TEST tool output {call_id}", status="success")


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
    entered, release = _held_capture(provider, monkeypatch, "slow-call")
    _register_adapter_instance(provider)
    try:
        first, _, first_thread = _in_thread(_tool_hook("TEST-session-1", "slow-call"))
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
        "scope-recall: post_tool_call not taken: this session has been busy in observe_post_tool_call for "), said


def test_prefetch_does_not_wait_out_its_busy_session(adapter, monkeypatch):
    provider, _clock = adapter
    monkeypatch.setattr(provider_module, "_PREFETCH_STATE_WAIT_S", 0.05, raising=False)
    entered, release = _held_capture(provider, monkeypatch, "slow-call")
    _register_adapter_instance(provider)
    try:
        first, _, first_thread = _in_thread(_tool_hook("TEST-session-1", "slow-call"))
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
    provider.observe_post_llm_call(session_id="TEST-session-1", turn_id="turn-10",
                                   assistant_response="TEST 三步都跑完了。", conversation_history=history)
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
    synced, _, sync_thread = _in_thread(lambda: provider.sync_turn("TEST 跑三步", "TEST 三步都跑完了。",
                                                                   session_id="TEST-session-1"))
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
    observe = provider.observe_post_tool_call

    def slow_observe(**kwargs):
        time.sleep(0.1)  # past the shortened host timeout by several 15.6 ms clock ticks
        return observe(**kwargs)

    monkeypatch.setattr(provider, "observe_post_tool_call", slow_observe)
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
    provider.observe_post_llm_call(session_id="TEST-session-1", turn_id=turn, assistant_response="TEST 跑完了。",
                                   conversation_history=history)


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
    synced, _, sync_thread = _in_thread(lambda: provider.sync_turn("TEST 跑几步", "TEST 跑完了。",
                                                                   session_id="TEST-session-1"))
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
        done, _, thread = _in_thread(lambda: _global_callback("pre_llm_call")(
            session_id="TEST-session-1", turn_id="turn-uuid-7", user_message="TEST 第七轮"))
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
        event: f"scope_recall_{event}" for event in _SUPPORTED_HOOKS}


def test_another_session_never_waits_for_this_one(adapter, installed_core, initialize_kwargs, hermes_home, monkeypatch):
    """Each Hermes session has an adapter and a lock of its own, and a hook goes to the one bound to its session."""
    provider, _clock = adapter
    other = _another_session(installed_core, initialize_kwargs)
    entered, release = _held_capture(provider, monkeypatch, "slow-call")
    _register_adapter_instance(provider)
    _register_adapter_instance(other)
    try:
        first, _, first_thread = _in_thread(_tool_hook("TEST-session-1", "slow-call"))
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
    assert _tool_rows(hermes_home) == 2


def test_another_sessions_hook_waits_only_its_write_budget_on_a_held_store(adapter, installed_core, initialize_kwargs,
                                                                            hermes_home):
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
    other._retry_buffered_captures()
    other.shutdown()
    assert _tool_rows(hermes_home) == 1
