"""Focused lifecycle wakeups for the Codex durable worker."""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import Mock, patch

import pytest

from scope_recall.adapters.codex import CodexHookHandler
from scope_recall.adapters.codex import install_codex_scope_recall
from scope_recall.contracts import ContractError


def _payload(project_root, event, **fields):
    return {
        "hook_event_name": event,
        "session_id": "TEST-lifecycle-session",
        "turn_id": "TEST-lifecycle-turn",
        "cwd": str(project_root),
        **fields,
    }


@pytest.fixture
def codex_install(tmp_path):
    root = tmp_path / "TEST-codex-install"
    project = tmp_path / "TEST-project"
    project.mkdir()
    config, core = install_codex_scope_recall(root, project_root=project, test_mode=True)
    runtime_path = root / "trusted-runtime.json"
    runtime_path.write_text(
        json.dumps(
            {
                "binding": {
                    "agent_id": config.agent_id,
                    "installation_id": config.installation_id,
                    "data_directory": str(config.data_directory),
                    "scope_ids": sorted(config.scope_ids),
                    "test_mode": True,
                },
                "session_id": "TEST-lifecycle-session",
                "allowed_scope_ids": sorted(config.scope_ids),
                "actor_origin": "human_direct",
                "owner_id": "TEST-lifecycle-worker",
                "request_seconds": 45.0,
                "drain_seconds": 120.0,
                "max_items": 32,
                "lease_seconds": 60.0,
                "auxiliary": {"external_embedding": False, "external_consolidation": False},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return config, core, project, runtime_path


def test_session_start_wakes_existing_pending_work_without_new_capture(codex_install):
    config, _core, project, runtime_path = codex_install
    handler = CodexHookHandler.from_config_path(
        str(config.config_path),
        trusted_runtime_config_path=str(runtime_path),
    )
    assert handler.handle_payload(
        _payload(project, "UserPromptSubmit", prompt="TEST pending prompt")
    ) == {}
    with sqlite3.connect(config.data_directory / "memory.sqlite3") as db:
        assert db.execute(
            "SELECT count(*) FROM source_events WHERE role='user' AND content=?",
            ("TEST pending prompt",),
        ).fetchone()[0] == 1
    worker = Mock()
    worker.poll.return_value = None
    with patch("scope_recall.adapters.codex.runtime_wiring.launch_worker", return_value=worker) as launch:
        assert handler.handle_payload(_payload(project, "SessionStart")) == {}
        launch.assert_called_once()
    handler.close()


def test_stop_persists_assistant_before_waking_worker(codex_install):
    config, _core, project, runtime_path = codex_install
    handler = CodexHookHandler.from_config_path(
        str(config.config_path),
        trusted_runtime_config_path=str(runtime_path),
    )
    worker = Mock()
    worker.poll.return_value = None
    observed = {}

    def launch_after_capture(*args, **kwargs):
        with sqlite3.connect(config.data_directory / "memory.sqlite3") as db:
            observed["count"] = db.execute(
                "SELECT count(*) FROM source_events WHERE role='assistant' AND content=?",
                ("TEST final visible answer",),
            ).fetchone()[0]
        return worker

    with patch(
        "scope_recall.adapters.codex.runtime_wiring.launch_worker",
        side_effect=launch_after_capture,
    ) as launch:
        assert handler.handle_payload(
            _payload(project, "Stop", last_assistant_message="TEST final visible answer")
        ) == {}
        launch.assert_called_once()
    assert observed["count"] == 1
    handler.close()


def test_denied_session_start_does_not_wake_worker(codex_install, tmp_path):
    config, _core, project, runtime_path = codex_install
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    handler = CodexHookHandler.from_config_path(
        str(config.config_path),
        trusted_runtime_config_path=str(runtime_path),
    )
    with patch("scope_recall.adapters.codex.runtime_wiring.launch_worker") as launch:
        assert handler.handle_payload(_payload(foreign, "SessionStart")) == {}
        launch.assert_not_called()
        assert handler.handle_payload(_payload(project, "SessionStart")) == {}
        launch.assert_called_once()
    handler.close()


def test_failed_status_and_failed_stop_capture_do_not_wake_worker(codex_install):
    config, _core, project, runtime_path = codex_install

    failed_status = CodexHookHandler.from_config_path(
        str(config.config_path),
        trusted_runtime_config_path=str(runtime_path),
    )
    with patch.object(failed_status.core, "status", side_effect=ContractError("status_failed")), patch(
        "scope_recall.adapters.codex.runtime_wiring.launch_worker"
    ) as launch:
        assert failed_status.handle_payload(_payload(project, "SessionStart")) == {}
        launch.assert_not_called()
    failed_status.close()

    failed_capture = CodexHookHandler.from_config_path(
        str(config.config_path),
        trusted_runtime_config_path=str(runtime_path),
    )
    with patch.object(
        failed_capture,
        "_capture",
        return_value=((), ("capture_gap:write_exception",)),
    ), patch("scope_recall.adapters.codex.runtime_wiring.launch_worker") as launch:
        assert failed_capture.handle_payload(
            _payload(project, "Stop", last_assistant_message="TEST not persisted")
        ) == {}
        launch.assert_not_called()
    failed_capture.close()
