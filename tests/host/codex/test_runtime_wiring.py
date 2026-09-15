"""Trusted local runtime wiring for Codex host adapters."""
from __future__ import annotations

import json
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid

import pytest

from scope_recall.adapters.codex import CodexHookHandler, install_codex_scope_recall
from scope_recall.adapters.codex.mcp_server import build_server
from scope_recall.adapters.codex.runtime_wiring import GAP_UNCONFIGURED, attach_trusted_host_runtime
from scope_recall.contracts import ContractError


def test_runtime_wiring_uses_this_source_checkout(record_property):
    from scope_recall.adapters.codex import runtime_wiring
    actual = Path(runtime_wiring.__file__).resolve()
    expected = Path(__file__).resolve().parents[3] / 'adapters' / 'codex' / 'runtime_wiring.py'
    record_property('runtime_wiring_file', str(actual))
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
    with patch("scope_recall.adapters.codex.runtime_wiring.launch_worker") as launch_worker:
        worker = Mock()
        worker.poll.return_value = None
        launch_worker.return_value = worker
        assert handler.handle_payload(
            {
                "hook_event_name": "SessionEnd",
                "session_id": "TEST-session",
                "cwd": str(project),
                "reason": "logout",
            }
        ) == {}
        launch_worker.assert_called_once()
    handler.close()


def test_session_end_without_runtime_config_does_not_launch_worker(codex_install):
    config, core, project, _runtime_path = codex_install
    handler = CodexHookHandler(config, core=core)
    with patch("scope_recall.adapters.codex.runtime_wiring.launch_worker") as launch_worker:
        handler.handle_payload(
            {
                "hook_event_name": "SessionEnd",
                "session_id": "TEST-session",
                "cwd": str(project),
                "reason": "logout",
            }
        )
        launch_worker.assert_not_called()


def test_real_owned_watchdog_outlives_short_hook_and_cleans_config(codex_install):
    config, _core, project, runtime_path = codex_install
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
    started = time.monotonic()
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
    ephemeral = (worker.config_path,)
    handler.close()

    assert time.monotonic() - started < 2.0
    assert worker.wait(timeout=30.0) == 0
    stdout, _stderr = worker.communicate(timeout=5.0)
    assert stdout == ""
    receipt = json.loads((config.data_directory / 'runtime-worker-status.json').read_text())
    assert receipt["status"] == "waiting"
    assert receipt["processed"] == 0 and receipt["failed_work"] == 0
    assert all(not path.exists() for path in ephemeral)


def test_generated_hook_and_mcp_attach_binding_directory_default(codex_install):
    config, _core, project, runtime_path = codex_install
    default_path = config.data_directory / "runtime-config.json"
    default_path.write_text(runtime_path.read_text(encoding="utf-8"), encoding="utf-8")
    handler = CodexHookHandler.from_config_path(str(config.config_path))
    assert handler._host_runtime is None
    handler._ensure_host_runtime()
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
