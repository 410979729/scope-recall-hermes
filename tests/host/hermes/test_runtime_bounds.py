"""P11 runtime bounds: turn-local echo fences, worker coalescing, and hooks."""
from __future__ import annotations

import json
import threading
import sqlite3

import pytest

from scope_recall.adapters.hermes import ScopeRecallHermesAdapter, install_hermes_scope_recall
from scope_recall.adapters.hermes.hooks import _global_callback, _register_adapter_instance, _unregister_adapter_instance
from scope_recall.adapters.hermes.provider import GAP_CURRENT_SOURCE_REFS_LIMIT
from scope_recall.adapters.hermes.worker import AdapterWorker
from scope_recall.contracts import validate_payload
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
    Hermes gave up on (30 s) was skipped for the rest of the session, with no gap anywhere."""
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
