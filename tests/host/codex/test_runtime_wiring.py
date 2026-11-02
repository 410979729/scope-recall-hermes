"""Trusted local runtime wiring for Codex host adapters."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from scope_recall.adapters.clients import CodexHookHandler, install_codex_scope_recall
from scope_recall.adapters.clients.handler import _TOTAL_BUDGET_S
from scope_recall.adapters.clients.mcp_server import build_server
from scope_recall.adapters.clients.runtime_wiring import GAP_UNCONFIGURED, attach_trusted_host_runtime
from scope_recall.contracts import ContractError, TrustedContext
from scope_recall.core.schema import SCHEMA_VERSION


def test_runtime_wiring_uses_this_source_checkout(record_property):
    from scope_recall.adapters.clients import runtime_wiring

    actual = Path(runtime_wiring.__file__).resolve()
    expected = Path(__file__).resolve().parents[3] / "adapters" / "clients" / "runtime_wiring.py"
    record_property("runtime_wiring_file", str(actual))
    assert actual == expected


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
        "owner_id": "TEST-codex-worker",
        "request_seconds": 45.0,
        "drain_seconds": 120.0,
        "max_items": 32,
        "lease_seconds": 60.0,
        "auxiliary": {"external_embedding": False, "external_consolidation": False},
    }


def _write_runtime_config(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"])
def test_pending_upgrade_wakes_from_real_client_hook(codex_install, monkeypatch, event):
    from scope_recall.adapters.clients import runtime_wiring
    from scope_recall.core import storage
    from scope_recall.runtime import worker_launch
    from v11_support import downgrade_store

    config, core, project, runtime_path = codex_install
    (core.storage.path.parent / "runtime-config.json").write_bytes(runtime_path.read_bytes())
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
    monkeypatch.setattr(runtime_wiring, "launch_worker", observed)
    handler = CodexHookHandler(config, core=core)
    payload = dict(
        hook_event_name=event,
        session_id="TEST-pending",
        turn_id="TEST-turn",
        cwd=str(project),
        prompt="TEST pending upgrade",
        last_assistant_message="TEST reply",
        reason="logout",
    )
    try:
        handler.handle_payload(payload)
        assert workers, "pending hook did not wake schema maintenance"
        assert len(workers) == 1
        out, err = workers[0].communicate(timeout=20)
        assert workers[0].poll() == 0, (out, err)
        assert core.storage.initialize().schema_version == storage.SCHEMA_VERSION
        handler.handle_payload({**payload, "turn_id": "TEST-next"})
        assert "SCHEMA_UNSUPPORTED" not in str(handler.diagnostics)
    finally:
        handler.close()
        for worker in workers:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=5)


def test_mcp_startup_wakes_pending_upgrade_without_status(codex_install, monkeypatch):
    from scope_recall.runtime import worker_launch
    from v11_support import downgrade_store

    config, core, project, runtime_path = codex_install
    downgrade_store(core.storage.path, 1111)
    launch = worker_launch.launch_worker
    workers = []

    def observed(*args, **kwargs):
        kwargs["detach_output"] = False
        worker = launch(*args, **kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(worker_launch, "launch_worker", observed)
    server = build_server(config, workspace=project, trusted_runtime_config_path=runtime_path)
    try:
        assert len(workers) == 1
        out, err = workers[0].communicate(timeout=20)
        assert workers[0].poll() == 0, (out, err)
        assert server.core.storage.initialize().schema_version == SCHEMA_VERSION
    finally:
        server._host_runtime.close(detach_worker=False)
        for worker in workers:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=5)


def test_actual_pending_wakes_even_when_startup_probe_misses_it(codex_install, monkeypatch):
    from scope_recall.adapters import runtime_wiring
    from scope_recall.core import storage
    from scope_recall.runtime import worker_launch
    from v11_support import downgrade_store

    config, core, _project, runtime_path = codex_install
    downgrade_store(core.storage.path, 1111)
    monkeypatch.setattr(storage, "_store_bytes", lambda conn: storage.HEAVY_UPGRADE_BYTES)
    monkeypatch.setattr(runtime_wiring, "store_upgrade_pending", lambda *args, **kwargs: False)
    launch = worker_launch.launch_worker
    workers = []

    def observed(*args, **kwargs):
        kwargs["detach_output"] = False
        worker = launch(*args, **kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(worker_launch, "launch_worker", observed)
    host = attach_trusted_host_runtime(
        config_path=runtime_path,
        expected_binding=config.to_binding(),
        session_id="TEST-pending",
        allowed_scope_ids=config.scope_ids,
    )
    context = TrustedContext(config.to_binding(), "TEST-pending", config.scope_ids, "human_direct")
    try:
        with pytest.raises(ContractError, match="upgrade_pending"):
            host.core.status(context)
        assert len(workers) == 1
        out, err = workers[0].communicate(timeout=20)
        assert workers[0].poll() == 0, (out, err)
        assert host.core.status(context).schema_version == SCHEMA_VERSION
    finally:
        host.close(detach_worker=False)
        for worker in workers:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=5)


@pytest.fixture
def codex_install(tmp_path):
    root = tmp_path / "codex-root"
    project = tmp_path / "project"
    project.mkdir()
    config, core = install_codex_scope_recall(root, project_root=project, test_mode=True)
    runtime_path = _write_runtime_config(
        root / "trusted-runtime.json",
        _runtime_payload(
            config.to_binding(),
            session_id="TEST-session",
            allowed_scope_ids=config.scope_ids,
        ),
    )
    return config, core, project, runtime_path


def test_missing_runtime_config_preserves_basic_hook_behavior(codex_install):
    config, core, project, _runtime_path = codex_install
    handler = CodexHookHandler(config, core=core)
    result = handler.handle_payload(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "TEST-session",
            "turn_id": "turn-1",
            "cwd": str(project),
            "prompt": "hello codex",
        }
    )
    assert isinstance(result, dict)
    assert GAP_UNCONFIGURED in handler.diagnostics.capability_gaps


def test_missing_optional_runtime_path_falls_back_to_basic(codex_install):
    config, core, project, _runtime_path = codex_install
    handler = CodexHookHandler.from_config_path(
        str(config.config_path),
        core=core,
        trusted_runtime_config_path=str(project / "missing-runtime.json"),
    )
    assert handler.core is core
    assert GAP_UNCONFIGURED in handler.diagnostics.capability_gaps
    handler.close()


def test_handler_and_mcp_share_runtime_core(codex_install):
    config, _core, project, runtime_path = codex_install
    host_runtime = attach_trusted_host_runtime(
        config_path=runtime_path,
        expected_binding=config.to_binding(),
        session_id="TEST-session",
        allowed_scope_ids=config.scope_ids,
    )
    handler = CodexHookHandler(config, host_runtime=host_runtime)
    server = build_server(config, workspace=project, host_runtime=host_runtime)
    assert handler.core is server.core
    assert handler.core is host_runtime.core
    handler.close()


def test_session_end_launches_owned_worker_without_foreground_model(codex_install):
    config, _core, project, runtime_path = codex_install
    handler = CodexHookHandler.from_config_path(
        str(config.config_path),
        trusted_runtime_config_path=str(runtime_path),
    )
    with patch("scope_recall.adapters.clients.runtime_wiring.launch_worker") as launch_worker:
        worker = Mock()
        worker.poll.return_value = None
        launch_worker.return_value = worker
        assert (
            handler.handle_payload(
                {
                    "hook_event_name": "SessionEnd",
                    "session_id": "TEST-session",
                    "cwd": str(project),
                    "reason": "logout",
                }
            )
            == {}
        )
        launch_worker.assert_called_once()
    handler.close()


def test_session_end_without_runtime_config_does_not_launch_worker(codex_install):
    config, core, project, _runtime_path = codex_install
    handler = CodexHookHandler(config, core=core)
    with patch("scope_recall.adapters.clients.runtime_wiring.launch_worker") as launch_worker:
        handler.handle_payload(
            {
                "hook_event_name": "SessionEnd",
                "session_id": "TEST-session",
                "cwd": str(project),
                "reason": "logout",
            }
        )
        launch_worker.assert_not_called()


def test_real_owned_watchdog_outlives_short_hook_and_cleans_config(codex_install, metered_store):
    """A SessionEnd stores its end, attaches the runtime and starts the owned watchdog, and it and the hook's close
    end within the hook's budget while the watchdog runs on; the watchdog then ends on its own and takes its config
    with it.  Each store transaction counts 0.05 s on the hook's clock, however long the runner's store takes, and
    everything else as long as it takes."""
    config, _core, project, runtime_path = codex_install
    clock = metered_store(0.05)
    handler = CodexHookHandler.from_config_path(
        str(config.config_path),
        trusted_runtime_config_path=str(runtime_path),
    )
    handler.handle_payload(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "TEST-short-hook",
            "turn_id": "turn-1",
            "cwd": str(project),
            "prompt": "persist before owned watchdog",
        }
    )
    started = clock()
    handler.handle_payload(
        {
            "hook_event_name": "SessionEnd",
            "session_id": "TEST-short-hook",
            "cwd": str(project),
            "reason": "short-hook-test",
        }
    )
    host_runtime = handler._host_runtime
    assert host_runtime is not None
    worker = host_runtime._owned_worker
    assert worker is not None
    assert worker.poll() is None, "the hook returned while its watchdog runs"
    ephemeral = (worker.config_path,)
    handler.close()

    assert clock() - started < _TOTAL_BUDGET_S, clock() - started
    assert worker.wait(timeout=30.0) == 0
    stdout, _stderr = worker.communicate(timeout=5.0)
    assert stdout == ""
    receipt = json.loads((config.data_directory / "runtime-worker-status.json").read_text())
    assert receipt["status"] == "waiting"
    assert receipt["processed"] == 0 and receipt["failed_work"] == 0
    assert all(not path.exists() for path in ephemeral)


def test_a_session_end_left_too_little_time_after_its_capture_starts_no_worker(codex_install, metered_store):
    """A SessionEnd left less time after its capture than an attach asks for (``RUNTIME_ATTACH_MIN_S``) attaches no
    runtime and wakes no worker, and ends within its budget: its process started a second before its handler was made
    (``hook_started_at``), as one slow to load does, and its capture's inbox write and commit take 0.4 s each, which
    leaves 0.2 s."""
    config, _core, project, runtime_path = codex_install
    clock = metered_store(0.4)
    hook_started_at = clock() - 1.0
    handler = CodexHookHandler.from_config_path(
        str(config.config_path), trusted_runtime_config_path=str(runtime_path), hook_started_at=hook_started_at
    )
    try:
        with patch("scope_recall.adapters.clients.runtime_wiring.launch_worker") as launch:
            handler.handle_payload(
                {"hook_event_name": "SessionEnd", "session_id": "TEST-late", "cwd": str(project), "reason": "late"}
            )
            elapsed = clock() - hook_started_at
        assert handler.diagnostics.capture_stage == "source_committed"
        assert handler._host_runtime is None and launch.call_count == 0
        assert elapsed < _TOTAL_BUDGET_S, elapsed
    finally:
        handler.close()


def test_generated_hook_and_mcp_attach_binding_directory_default(codex_install):
    config, _core, project, runtime_path = codex_install
    default_path = config.data_directory / "runtime-config.json"
    default_path.write_text(runtime_path.read_text(encoding="utf-8"), encoding="utf-8")
    handler = CodexHookHandler.from_config_path(str(config.config_path))
    assert handler._host_runtime is None
    handler.ensure_runtime()
    server = build_server(config, workspace=project)
    assert handler._host_runtime is not None and handler._host_runtime.configured
    assert server._host_runtime is not None and server._host_runtime.configured
    assert handler.core.config.binding == server.core.config.binding == config.to_binding()
    handler.close()
    server._host_runtime.close()


def test_mcp_binds_official_thread_metadata(codex_install):
    config, _core, project, runtime_path = codex_install
    server = build_server(
        config,
        workspace=project,
        trusted_runtime_config_path=str(runtime_path),
    )
    thread_id = str(uuid.uuid4())
    ctx = SimpleNamespace(request_context=SimpleNamespace(meta={"threadId": thread_id}))
    bound = server._request_context(ctx)
    assert bound.session_id == thread_id
    bound_mutation = server._request_context(ctx, mutation=True)
    assert bound_mutation.session_id == thread_id
    with pytest.raises(ContractError):
        server._request_context(SimpleNamespace(request_context=SimpleNamespace(meta={})), mutation=True)
    assert server._capability_gaps(SimpleNamespace(request_context=SimpleNamespace(meta={})))


def test_mcp_missing_optional_runtime_path_stays_basic(codex_install):
    config, _core, project, _runtime_path = codex_install
    server = build_server(
        config,
        workspace=project,
        trusted_runtime_config_path=str(project / "missing-runtime.json"),
    )
    assert server._host_runtime is not None
    assert GAP_UNCONFIGURED in server._host_runtime.capability_gaps
    server._host_runtime.close()
