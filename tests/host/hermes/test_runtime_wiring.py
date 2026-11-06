"""Trusted local runtime wiring for Hermes host adapters."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from scope_recall.adapters.hermes import ScopeRecallHermesAdapter, install_hermes_scope_recall
from scope_recall.adapters.hermes.capture import GAP_CURRENT_SOURCE_REFS_LIMIT
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


@pytest.fixture
def warm_binding(installed_core, initialize_kwargs, hermes_home):
    core, clock = installed_core
    payload = _runtime_payload(
        core.config.binding, session_id="TEST-warm", allowed_scope_ids=core.config.binding.scope_ids
    )
    path = _write_runtime_config(hermes_home / "TEST-warm-runtime.json", payload)
    provider = ScopeRecallHermesAdapter(core=core, clock=clock)
    yield provider, {**initialize_kwargs, "trusted_runtime_config_path": str(path)}
    provider.shutdown()


def test_bind_warms_without_waiting_and_coalesces(warm_binding, monkeypatch):
    from scope_recall.runtime.instance import RuntimeInstance

    provider, kwargs = warm_binding
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def warm(_runtime, seconds):
        calls.append(seconds)
        assert provider._lock.acquire(timeout=2)
        provider._lock.release()
        started.set()
        try:
            assert release.wait(5)
        finally:
            done.set()

    monkeypatch.setattr(RuntimeInstance, "warm_query_embedding", warm)
    try:
        provider.initialize("TEST-warm", **kwargs)
        assert started.wait(2)
        assert not done.is_set()
        provider.initialize("TEST-warm-again", **kwargs)
        assert calls == [8.0]
        provider.shutdown()
        assert not done.is_set()
    finally:
        release.set()
        if started.is_set():
            assert done.wait(5)


def test_recent_recall_suppresses_warm_binding(warm_binding, monkeypatch):
    from scope_recall.runtime.instance import RuntimeInstance

    provider, kwargs = warm_binding
    finished = threading.Event()
    calls = []

    def warm(_runtime, seconds):
        calls.append(seconds)
        finished.set()

    monkeypatch.setattr(RuntimeInstance, "warm_query_embedding", warm)
    provider.initialize("TEST-warm", **kwargs)
    assert finished.wait(2)
    provider._binding._warm_thread.join(timeout=2)
    provider._binding._last_query_at -= 61
    provider.prefetch("synthetic recent recall")
    provider.initialize("TEST-warm-again", **kwargs)
    assert calls == [8.0]


def test_idle_after_warm_starts_another(warm_binding, monkeypatch):
    from scope_recall.runtime.instance import RuntimeInstance

    provider, kwargs = warm_binding
    calls = []
    monkeypatch.setattr(RuntimeInstance, "warm_query_embedding", lambda _runtime, seconds: calls.append(seconds))
    provider.initialize("TEST-warm", **kwargs)
    provider._binding._warm_thread.join(timeout=2)
    assert calls == [8.0]
    provider._binding._last_query_at -= 61
    provider.initialize("TEST-warm-again", **kwargs)
    provider._binding._warm_thread.join(timeout=2)
    assert calls == [8.0, 8.0]


def test_warm_error_does_not_change_diagnostics(warm_binding, monkeypatch, caplog):
    from scope_recall.runtime.instance import RuntimeInstance

    provider, kwargs = warm_binding
    attempted = threading.Event()

    def warm(_runtime, _seconds):
        attempted.set()
        raise RuntimeError("synthetic warm failure")

    monkeypatch.setattr(RuntimeInstance, "warm_query_embedding", warm)
    with caplog.at_level("DEBUG", logger="scope_recall.adapters.hermes.provider"):
        provider.initialize("TEST-warm", **kwargs)
        assert attempted.wait(2)
        provider._binding._warm_thread.join(timeout=2)
        assert "Query embedding warm-up failed: RuntimeError" in caplog.text
        assert not provider.diagnostics.pending_outcome_gaps
        assert not provider.diagnostics.capture_failures
        assert not any("warm" in gap for gap in provider.diagnostics.capability_gaps)


@pytest.mark.parametrize("private", [False, True])
def test_warm_respects_private_mode_and_missing_vector(warm_binding, monkeypatch, private):
    from dataclasses import replace

    from scope_recall.core import private as private_module
    from scope_recall.runtime.instance import RuntimeInstance, _LazyVectorPort
    from scope_recall.runtime.instance_config import VectorRuntimeConfig

    provider, kwargs = warm_binding
    original = RuntimeInstance.warm_query_embedding
    done = threading.Event()
    requests = Mock()
    results = []
    monkeypatch.setattr(_LazyVectorPort, "_query_embedder", lambda _self: requests)
    monkeypatch.setattr(private_module, "outbound_blocked", lambda *_args: private)

    def warm(runtime, seconds):
        try:
            if private:
                config = runtime.config
                vector = VectorRuntimeConfig(
                    backend="sqlite",
                    storage_dir=config.binding.data_directory / "vectors" / config.embedding_space_id(),
                    table_name="TEST_vectors",
                    dimensions=config.embedding_space()["dimensions"],
                )
                runtime.config = replace(config, vector=vector)
            results.append(original(runtime, seconds))
        finally:
            done.set()

    monkeypatch.setattr(RuntimeInstance, "warm_query_embedding", warm)
    provider.initialize("TEST-warm", **kwargs)
    assert done.wait(2)
    assert results == [False]
    requests.assert_not_called()


def test_pending_upgrade_wakes_from_real_provider_initialize(hermes_home, initialize_kwargs, monkeypatch):
    from scope_recall.core import storage
    from scope_recall.runtime import worker_launch
    from v11_support import downgrade_store

    binding, core = install_hermes_scope_recall(
        hermes_home,
        agent_id=initialize_kwargs["agent_identity"],
        platform=initialize_kwargs["platform"],
        user_id=initialize_kwargs["user_id"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        test_mode=False,
    )
    path = _write_runtime_config(
        hermes_home / "TEST-runtime.json",
        _runtime_payload(binding, session_id="TEST-upgrade", allowed_scope_ids=binding.scope_ids),
    )
    downgrade_store(core.storage.path, 1111)
    monkeypatch.setattr(storage, "_store_bytes", lambda conn: storage.HEAVY_UPGRADE_BYTES)
    workers = []
    launch = worker_launch.launch_worker

    def observed(*args, **kwargs):
        kwargs["detach_output"] = False
        worker = launch(*args, **kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(worker_launch, "launch_worker", observed)
    provider = ScopeRecallHermesAdapter()
    try:
        provider.initialize("TEST-upgrade", **{**initialize_kwargs, "trusted_runtime_config_path": str(path)})
        assert workers, "pending provider did not wake schema maintenance"
        assert len(workers) == 1
        out, err = workers[0].communicate(timeout=20)
        assert workers[0].poll() == 0, (out, err)
        provider.initialize("TEST-next", **{**initialize_kwargs, "trusted_runtime_config_path": str(path)})
        assert (
            provider._require_core().status(provider._identity.trusted_context()).schema_version
            == storage.SCHEMA_VERSION
        )
    finally:
        provider.shutdown()
        for worker in workers:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=5)


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


def test_a_gateway_binds_the_sqlite_vector_backend(hermes_home, initialize_kwargs):
    """A configured gateway binds the direct SQLite factory across session changes."""

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
        runtime = provider._host_runtime.runtime
        store = runtime._vector_factory(runtime.config.vector)
        try:
            assert store.backend == "sqlite"
            assert store.requires_reopen, "binding leaves the companion connection unopened"
        finally:
            store.close()
    finally:
        provider.shutdown()


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


def test_two_hermes_agents_see_one_companion_and_close_their_own_connections(hermes_home, initialize_kwargs):
    """Independent runtime connections see committed vectors and close without invalidating each other."""
    from scope_recall.core.recall_policy import EMBEDDING_SPACE, embedding_space_id

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
    views = []
    try:
        for index, provider in enumerate(providers):
            provider.initialize(
                f"TEST-session-{index}", **{**initialize_kwargs, "trusted_runtime_config_path": str(config_path)}
            )
        runtimes = [provider._host_runtime.runtime for provider in providers]
        views = [runtime._vector_factory(runtime.config.vector) for runtime in runtimes]
        assert all(view.backend == "sqlite" for view in views)
        assert views[0].db_path == views[1].db_path
        views[0].open()
        views[1].open()
        values = [1.0] * space["dimensions"]
        row = dict(
            id="TEST-shared",
            scope_id="TEST-scope",
            source="TEST-shared",
            target="{}",
            content="",
            summary="",
            updated_at="",
            vector=values,
        )
        views[0].upsert_records([row])
        for runtime, view in zip(runtimes, views, strict=True):
            runtime._owned_resources.append(view)
            runtime._vector_store = view
        assert views[1].search(values, scope_id="TEST-scope", limit=1)[0]["id"] == row["id"]
        running.remove(providers[0])
        providers[0].shutdown()
        views[1].upsert_records([{**row, "id": "TEST-survivor"}])
        assert views[1].contains_id("TEST-survivor")
    finally:
        for view in views:
            view.close()
        for provider in running:
            provider.shutdown()


def test_session_end_detaches_bounded_worker_without_shared_drain(configured_provider, monkeypatch, metered_store):
    """A session's end launches the bounded worker detached and its shutdown waits for no drain: it is done within
    half a second on a clock that counts each store transaction 0.01 s, however long the runner's store takes."""
    clock = metered_store(0.01)
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
        started = clock()
        provider.shutdown()
        assert clock() - started < 0.5, clock() - started
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
