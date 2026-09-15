"""Trusted local runtime wiring for Hermes host adapters."""
from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from unittest.mock import Mock

import pytest

from scope_recall.adapters.hermes import ScopeRecallHermesAdapter, install_hermes_scope_recall
from scope_recall.adapters.hermes.runtime_wiring import GAP_BINDING_MISMATCH, GAP_UNCONFIGURED, GAP_WORKER_BUSY, GAP_WORKER_LAUNCH_FAILED


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
    _write_runtime_config(default_path, _runtime_payload(binding, session_id="TEST-session-1", allowed_scope_ids=binding.scope_ids))
    provider = ScopeRecallHermesAdapter()
    provider.initialize("TEST-session-1", **initialize_kwargs)
    assert provider._host_runtime is not None and provider._host_runtime.configured
    assert provider._core is provider._host_runtime.core
    assert GAP_UNCONFIGURED not in provider.diagnostics.capability_gaps
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
    monkeypatch.setattr(provider._worker, "shutdown", lambda: shutdown(timeout=.01))
    try:
        provider.on_session_end([])
        provider.on_session_end([])
        assert GAP_WORKER_BUSY in provider.diagnostics.capability_gaps
        provider.on_session_switch("TEST-session-2")
        started = time.monotonic()
        provider.shutdown()
        assert time.monotonic() - started < .5
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
    launch.call_args.args[0].unlink()


def test_owned_worker_rejects_scope_widening_and_closed_runtime(configured_provider, monkeypatch):
    runtime = configured_provider._host_runtime
    launch = Mock()
    monkeypatch.setattr("scope_recall.adapters.hermes.runtime_wiring.launch_worker", launch)
    assert runtime.maybe_launch_bounded_worker(
        session_id="TEST-session", allowed_scope_ids=frozenset({"foreign"}),
    ) == (GAP_BINDING_MISMATCH,)
    runtime.close()
    assert runtime.maybe_launch_bounded_worker(
        session_id="TEST-session", allowed_scope_ids=configured_provider._identity.runtime_audience.allowed_scope_ids,
    ) == (GAP_UNCONFIGURED,)
    launch.assert_not_called()
