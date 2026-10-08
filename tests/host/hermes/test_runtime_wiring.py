"""Trusted local runtime wiring for Hermes host adapters."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from scope_recall.adapters.hermes import ScopeRecallHermesAdapter, install_hermes_scope_recall
from scope_recall.adapters.hermes.provider import GAP_CURRENT_SOURCE_REFS_LIMIT
from scope_recall.adapters.hermes.runtime_wiring import (
    GAP_BINDING_MISMATCH,
    GAP_UNCONFIGURED,
    GAP_WORKER_BUSY,
    GAP_WORKER_LAUNCH_FAILED,
)
from scope_recall.core.retrieval import MAX_CURRENT_SOURCE_REFS


def _runtime_payload(binding, *, session_id: str, allowed_scope_ids) -> dict:
    return {
        "binding": {
            "agent_id": binding.agent_id,
            "installation_id": binding.installation_id,
            "data_directory": str(binding.data_directory),
            "scope_ids": sorted(binding.scope_ids),
            "test_mode": binding.test_mode,
        },
        "session_id": session_id,
        "allowed_scope_ids": sorted(allowed_scope_ids),
        "actor_origin": "human_direct",
        "owner_id": "TEST-hermes-worker",
        "request_seconds": 45.0,
        "drain_seconds": 120.0,
        "max_items": 32,
        "lease_seconds": 60.0,
        "auxiliary": {"external_embedding": False, "external_consolidation": False},
    }


def _write_runtime_config(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_missing_runtime_config_preserves_basic_behavior_and_reports_gap(adapter):
    provider, _clock = adapter
    assert GAP_UNCONFIGURED in provider.diagnostics.capability_gaps
    provider.observe_pre_llm(
        session_id="TEST-session-1",
        turn_id="turn-1",
        user_message="basic capture without runtime",
    )
    assert provider.diagnostics.current_source_refs
    rendered = provider.prefetch("basic capture without runtime")
    assert isinstance(rendered, str)


def test_missing_runtime_config_does_not_drain_on_session_end(adapter):
    provider, _clock = adapter
    assert provider._host_runtime is not None
    assert not provider._host_runtime.configured
    drain = Mock()
    provider._host_runtime.drain_background = drain
    provider.on_session_end([])
    provider._worker.shutdown(timeout=2.0)
    drain.assert_not_called()


def test_runtime_config_path_attaches_shared_core(hermes_home, initialize_kwargs):
    binding, _core = install_hermes_scope_recall(
        hermes_home,
        agent_id=initialize_kwargs["agent_identity"],
        platform=initialize_kwargs["platform"],
        user_id=initialize_kwargs["user_id"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        test_mode=False,
    )
    config_path = _write_runtime_config(
        hermes_home / "trusted-runtime.json",
        _runtime_payload(
            binding,
            session_id="TEST-session-1",
            allowed_scope_ids=binding.scope_ids,
        ),
    )
    provider = ScopeRecallHermesAdapter()
    provider.initialize(
        "TEST-session-1",
        **{**initialize_kwargs, "trusted_runtime_config_path": str(config_path)},
    )
    assert provider._host_runtime is not None
    assert provider._host_runtime.configured
    assert provider._core is provider._host_runtime.core
    assert GAP_UNCONFIGURED not in provider.diagnostics.capability_gaps
    provider.shutdown()


def test_binding_directory_runtime_config_attaches_without_host_kwarg(hermes_home, initialize_kwargs):
    binding, _core = install_hermes_scope_recall(
        hermes_home,
        agent_id=initialize_kwargs["agent_identity"],
        platform=initialize_kwargs["platform"],
        user_id=initialize_kwargs["user_id"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        test_mode=False,
    )
    default_path = binding.data_directory / "runtime-config.json"
    _write_runtime_config(
        default_path, _runtime_payload(binding, session_id="TEST-session-1", allowed_scope_ids=binding.scope_ids)
    )
    provider = ScopeRecallHermesAdapter()
    provider.initialize("TEST-session-1", **initialize_kwargs)
    assert provider._host_runtime is not None and provider._host_runtime.configured
    assert provider._core is provider._host_runtime.core
    assert GAP_UNCONFIGURED not in provider.diagnostics.capability_gaps
    provider.shutdown()


def test_a_gateway_starts_its_vector_helper_when_it_first_binds(hermes_home, initialize_kwargs, monkeypatch):
    """The first search opened the vector helper, and its LanceDB import could outrun that recall's budget: a probe
    run as a gateway's first turn after a start came back without its vector search."""
    from scope_recall.vector import process_store

    started = []
    # The helper itself is the only stand-in: the host runtime, its config and its vector section are the real ones.
    monkeypatch.setattr(process_store, "prestart", lambda **_options: started.append(True))
    binding, _core = install_hermes_scope_recall(
        hermes_home,
        agent_id=initialize_kwargs["agent_identity"],
        platform=initialize_kwargs["platform"],
        user_id=initialize_kwargs["user_id"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        test_mode=False,
    )
    from scope_recall.core.recall_policy import EMBEDDING_SPACE, embedding_space_id

    payload = _runtime_payload(binding, session_id="TEST-session-1", allowed_scope_ids=binding.scope_ids)
    space = dict(EMBEDDING_SPACE)
    payload["vector"] = {
        "backend": "lancedb",
        "table_name": "TEST_vectors",
        "dimensions": space["dimensions"],
        "storage_dir": str(binding.data_directory / "vectors" / embedding_space_id(space)),
    }
    config_path = _write_runtime_config(hermes_home / "trusted-runtime.json", payload)
    provider = ScopeRecallHermesAdapter()
    try:
        for session in ("TEST-session-1", "TEST-session-2"):
            provider.initialize(session, **{**initialize_kwargs, "trusted_runtime_config_path": str(config_path)})
        assert provider._host_runtime.configured, provider._host_runtime.capability_gaps
        assert provider._host_runtime.runtime.config.vector is not None
        assert started == ([True] if sys.platform == "win32" else []), "once, when the gateway first binds"
    finally:
        provider.shutdown()


@pytest.mark.skipif(sys.platform != "win32", reason="LanceDB runs in a helper process on Windows only")
def test_the_vector_helper_is_started_only_for_a_runtime_with_vectors(monkeypatch):
    from scope_recall.adapters.hermes import session_binding
    from scope_recall.vector import process_store

    started = []
    monkeypatch.setattr(process_store, "prestart", lambda **_options: started.append(True))

    def host(vector):
        return SimpleNamespace(runtime=SimpleNamespace(config=SimpleNamespace(vector=vector)))

    session_binding._start_vector_helper(host(object()))
    session_binding._start_vector_helper(host(None))
    session_binding._start_vector_helper(SimpleNamespace(runtime=None))
    assert started == [True]


@pytest.fixture
def configured_provider(hermes_home, initialize_kwargs):
    binding, _core = install_hermes_scope_recall(
        hermes_home,
        agent_id=initialize_kwargs["agent_identity"],
        platform=initialize_kwargs["platform"],
        user_id=initialize_kwargs["user_id"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        test_mode=False,
    )
    config_path = _write_runtime_config(
        hermes_home / "trusted-runtime.json",
        _runtime_payload(
            binding,
            session_id="TEST-session-1",
            allowed_scope_ids=binding.scope_ids,
        ),
    )
    provider = ScopeRecallHermesAdapter()
    provider.initialize(
        "TEST-session-1",
        **{**initialize_kwargs, "trusted_runtime_config_path": str(config_path)},
    )
    yield provider
    provider.shutdown()


def test_every_runtime_a_hermes_process_attaches_searches_one_store_of_a_table(configured_provider):
    """A gateway attaches a runtime for every agent it makes, and Hermes does not always shut down the one it made
    before: each runtime held a vector helper of its own, about 1.15 GB (yuheng's gateway held two on 2026-10-02, one
    per registration of the provider).  Hermes' attach makes the process share one store of each table, as a server
    does (``vector.process_store.share``), before the runtime builds its own."""
    import scope_recall.vector.process_store as process_store

    assert configured_provider._host_runtime is not None
    assert process_store._sharing is True
    first = process_store.store_for(Path("TEST-vectors"), table_name="scope_recall", dimensions=8)
    second = process_store.store_for(Path("TEST-vectors"), table_name="scope_recall", dimensions=8)
    assert isinstance(first, process_store.SharedStore) and first._shared is second._shared


@pytest.mark.skipif(sys.platform != "win32", reason="LanceDB runs in a helper process on Windows only")
def test_two_hermes_agents_build_views_of_one_vector_store_and_a_shutdown_leaves_it(
    hermes_home, initialize_kwargs, monkeypatch
):
    """Both runtimes' stores, built through their real factories (nothing is opened, no helper starts), are views of
    one store; a provider's shutdown leaves it to the process (review of 3.5.0rc4)."""
    import scope_recall.vector.process_store as process_store
    from scope_recall.core.recall_policy import EMBEDDING_SPACE, embedding_space_id

    started = []
    monkeypatch.setattr(process_store, "prestart", lambda **_options: started.append(True))
    binding, _core = install_hermes_scope_recall(
        hermes_home,
        agent_id=initialize_kwargs["agent_identity"],
        platform=initialize_kwargs["platform"],
        user_id=initialize_kwargs["user_id"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        test_mode=False,
    )
    space = dict(EMBEDDING_SPACE)
    payload = {
        **_runtime_payload(binding, session_id="TEST-session-1", allowed_scope_ids=binding.scope_ids),
        "vector": {
            "backend": "lancedb",
            "table_name": "TEST_vectors",
            "dimensions": space["dimensions"],
            "storage_dir": str(binding.data_directory / "vectors" / embedding_space_id(space)),
        },
    }
    config_path = _write_runtime_config(hermes_home / "trusted-runtime.json", payload)
    providers = [ScopeRecallHermesAdapter(), ScopeRecallHermesAdapter()]
    running = list(providers)
    try:
        for index, provider in enumerate(providers):
            provider.initialize(
                f"TEST-session-{index}", **{**initialize_kwargs, "trusted_runtime_config_path": str(config_path)}
            )
        runtimes = [provider._host_runtime.runtime for provider in providers]
        views = [runtime._vector_factory(runtime.config.vector) for runtime in runtimes]
        assert all(isinstance(view, process_store.SharedStore) for view in views)
        assert views[0]._shared is views[1]._shared, "one store, one helper, for both agents"
        assert started == [True, True], "each bind still asks for a spare; prestart decides"
        # As ``RuntimeInstance._ensure_vector_port`` leaves it after a first recall: the view is the runtime's.
        runtimes[0]._owned_resources.append(views[0])
        runtimes[0]._vector_store = views[0]
        running.remove(providers[0])
        providers[0].shutdown()  # closes the runtime, which closes its view
        assert len(process_store._shared) == 1 and not views[1]._store._closed
    finally:
        for provider in running:
            provider.shutdown()


def test_session_end_detaches_bounded_worker_without_shared_drain(configured_provider, monkeypatch):
    provider = configured_provider
    host_runtime = provider._host_runtime
    runtime = host_runtime.runtime
    assert runtime is not None
    entered, release = threading.Event(), threading.Event()
    # If the former shared-runtime route returns, this blocks until after
    # shutdown and exposes the active-drain/close race without model calls.
    runtime.drain = Mock(side_effect=lambda: (entered.set(), release.wait(2)))
    close = Mock(wraps=runtime.close)
    runtime.close = close
    worker = Mock()
    worker.poll.return_value = None
    worker.pid = 12345
    launch = Mock(return_value=worker)
    monkeypatch.setattr("scope_recall.adapters.hermes.runtime_wiring.launch_worker", launch)
    shutdown = provider._worker.shutdown
    monkeypatch.setattr(provider._worker, "shutdown", lambda: shutdown(timeout=0.01))
    try:
        provider.on_session_end([])
        provider.on_session_end([])
        assert GAP_WORKER_BUSY in provider.diagnostics.capability_gaps
        provider.on_session_switch("TEST-session-2")
        started = time.monotonic()
        provider.shutdown()
        assert time.monotonic() - started < 0.5
        assert not entered.is_set()
        runtime.drain.assert_not_called()
        close.assert_called_once()
        assert launch.call_count == 2
        assert launch.call_args_list[0].kwargs == {"cleanup_config": True, "detach_output": True}
        assert launch.call_args.kwargs["after_pid"] == 12345
        assert launch.call_args.kwargs["detach_output"] is True
        assert 0 < launch.call_args.kwargs["delay_seconds"] <= 30
        worker.terminate.assert_not_called()
        worker.communicate.assert_not_called()
        # The watchdog, not provider.close(), owns deletion after its drain.
        config_path = launch.call_args.args[0]
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        assert payload["session_id"] == "TEST-session-1"
        assert payload["allowed_scope_ids"] == sorted(provider._identity.runtime_audience.allowed_scope_ids)
        assert payload["request_seconds"] == 45
        assert payload["drain_seconds"] == 120
        assert payload["actor_origin"] == "human_direct"
        for call in launch.call_args_list:
            call.args[0].unlink()
    finally:
        release.set()
        shutdown(timeout=1)


def test_worker_launch_failure_reports_gap_and_next_session_end_retries(configured_provider, monkeypatch):
    provider = configured_provider
    worker = Mock()
    worker.poll.return_value = None
    launch = Mock(side_effect=[OSError("offline launch failure"), worker])
    monkeypatch.setattr("scope_recall.adapters.hermes.runtime_wiring.launch_worker", launch)
    provider.on_session_end([])
    failed_config = launch.call_args.args[0]
    assert GAP_WORKER_LAUNCH_FAILED in provider.diagnostics.capability_gaps
    assert not failed_config.exists()
    provider.on_session_end([])
    assert launch.call_count == 2
    assert GAP_WORKER_LAUNCH_FAILED not in provider.diagnostics.capability_gaps
    launch.call_args.args[0].unlink()


def test_worker_busy_gap_lasts_only_until_a_later_launch_is_not_busy(configured_provider, monkeypatch):
    provider = configured_provider
    session_gaps = provider.diagnostics.capability_gaps
    worker = Mock()
    worker.poll.return_value = None
    worker.pid = 12345
    launch = Mock(return_value=worker)
    monkeypatch.setattr("scope_recall.adapters.hermes.runtime_wiring.launch_worker", launch)

    def reply_gaps() -> list[str]:
        return json.loads(provider.handle_tool_call("status", {}))["capability_gaps"]

    try:
        provider.on_session_end([])
        provider.on_session_end([])  # a follower queues behind the live worker
        assert GAP_WORKER_BUSY in provider.diagnostics.capability_gaps
        assert GAP_WORKER_BUSY in reply_gaps()
        # A gap that is no launch result must outlive the replacement.  The
        # capture's own wake still finds both workers alive, so it stays busy.
        provider._current_source_refs = [f"ref-{index}" for index in range(MAX_CURRENT_SOURCE_REFS)]
        provider.observe_post_tool_call(
            session_id="TEST-session-1",
            turn_id="turn-1",
            tool_call_id="over-1",
            tool_name="terminal",
            result="one source past the fence",
            status="success",
        )
        assert GAP_WORKER_BUSY in reply_gaps()
        worker.poll.return_value = 0  # every worker has exited
        provider.on_session_end([])
        assert launch.call_count == 3
        assert GAP_WORKER_BUSY not in provider.diagnostics.capability_gaps
        assert GAP_WORKER_BUSY not in reply_gaps()
        assert provider.diagnostics.capability_gaps == (*session_gaps, GAP_CURRENT_SOURCE_REFS_LIMIT)
    finally:
        for call in launch.call_args_list:
            call.args[0].unlink(missing_ok=True)


def test_owned_worker_rejects_scope_widening_and_closed_runtime(configured_provider, monkeypatch):
    runtime = configured_provider._host_runtime
    launch = Mock()
    monkeypatch.setattr("scope_recall.adapters.hermes.runtime_wiring.launch_worker", launch)
    assert runtime.maybe_launch_bounded_worker(
        session_id="TEST-session",
        allowed_scope_ids=frozenset({"foreign"}),
    ) == (GAP_BINDING_MISMATCH,)
    runtime.close()
    assert runtime.maybe_launch_bounded_worker(
        session_id="TEST-session",
        allowed_scope_ids=configured_provider._identity.runtime_audience.allowed_scope_ids,
    ) == (GAP_UNCONFIGURED,)
    launch.assert_not_called()
