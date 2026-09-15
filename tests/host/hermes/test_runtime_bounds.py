"""P11 runtime bounds: turn-local echo fences, worker coalescing, and hooks."""
from __future__ import annotations

import threading
import sqlite3

from scope_recall.adapters.hermes import ScopeRecallHermesAdapter, install_hermes_scope_recall
from scope_recall.adapters.hermes.hooks import _global_callback, _register_adapter_instance, _unregister_adapter_instance
from scope_recall.adapters.hermes.worker import AdapterWorker


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
    provider._current_source_refs = [f"ref-{index}" for index in range(17)]
    assert provider.prefetch("overflow check") == ""
    assert "degraded:current_source_refs_limit" in provider.diagnostics.capability_gaps


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
