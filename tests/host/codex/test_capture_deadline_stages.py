"""Codex hook Source persist must finish before Lance/embed/worker work."""
from __future__ import annotations

import sqlite3
import time
from unittest.mock import patch

import pytest

from scope_recall.adapters.codex import CodexHookHandler, install_codex_scope_recall
from scope_recall.adapters.codex.handler import _CAPTURE_TIMEOUT_S, _TOTAL_BUDGET_S


def _payload(project_root, prompt="TEST durable prompt", turn="turn-1"):
    return {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "TEST-deadline-session",
        "turn_id": turn,
        "cwd": str(project_root),
        "prompt": prompt,
    }


@pytest.fixture
def install(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config, core = install_codex_scope_recall(tmp_path / "install", project_root=project, test_mode=True)
    runtime = tmp_path / "install" / "trusted-runtime.json"
    runtime.write_text(
        core.config.binding.data_directory.joinpath("runtime-config.json").read_text(encoding="utf-8")
        if (core.config.binding.data_directory / "runtime-config.json").exists()
        else "{}",
        encoding="utf-8",
    )
    return config, core, project


def _count_user_sources(config, content: str) -> int:
    with sqlite3.connect(config.data_directory / "memory.sqlite3") as db:
        return db.execute(
            "SELECT count(*) FROM source_events WHERE role='user' AND content=?",
            (content,),
        ).fetchone()[0]


def test_timing_probe_records_stages_and_persists_before_runtime_attach(install):
    config, _core, project = install
    stages: dict[str, float] = {}
    started = time.monotonic()

    def mark(name: str) -> None:
        stages[name] = time.monotonic() - started

    mark("begin")
    handler = CodexHookHandler.from_config_path(
        str(config.config_path),
        hook_started_at=started,
    )
    mark("config_and_core")
    assert handler._host_runtime is None
    handler.handle_payload(_payload(project))
    mark("handle")
    assert handler._persisted_this_call is True
    assert _count_user_sources(config, "TEST durable prompt") == 1
    assert stages["config_and_core"] < _TOTAL_BUDGET_S
    assert stages["handle"] < _TOTAL_BUDGET_S
    handler.close()


def test_cold_start_source_persists_inside_deadline(install):
    config, _core, project = install
    started = time.monotonic()
    handler = CodexHookHandler.from_config_path(str(config.config_path), hook_started_at=started)
    handler.handle_payload(_payload(project, prompt="cold start source"))
    assert _count_user_sources(config, "cold start source") == 1
    handler.close()


def test_lance_unavailable_still_persists_source(install):
    config, _core, project = install

    def boom(*_args, **_kwargs):
        raise RuntimeError("lance unavailable")

    handler = CodexHookHandler.from_config_path(str(config.config_path))
    with patch("scope_recall.adapters.codex.handler.attach_trusted_host_runtime", side_effect=boom):
        handler.handle_payload(_payload(project, prompt="persist without lance"))
    assert handler._persisted_this_call is True
    assert _count_user_sources(config, "persist without lance") == 1
    assert "capability_gap:trusted_runtime_invalid" in handler.diagnostics.capability_gaps
    handler.close()


def test_slow_embedding_does_not_block_source_persist(install):
    config, _core, project = install
    recorded = {"durability": None}

    class SlowEmbedCore:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def record_host_event(self, *args, **kwargs):
            receipt = self._inner.record_host_event(*args, **kwargs)
            recorded["durability"] = receipt.durability
            return receipt

        def recall_packet(self, *args, **kwargs):
            time.sleep(0.2)
            return self._inner.recall_packet(*args, **kwargs)

    handler = CodexHookHandler(config, core=SlowEmbedCore(_core))
    handler.handle_payload(_payload(project, prompt="slow embed persist"))
    assert recorded["durability"] == "persisted"
    assert _count_user_sources(config, "slow embed persist") == 1


def test_slow_worker_launch_does_not_unpersist_source(install):
    config, _core, project = install
    handler = CodexHookHandler.from_config_path(str(config.config_path))
    handler.handle_payload(_payload(project, prompt="before worker"))
    assert _count_user_sources(config, "before worker") == 1

    def slow_launch(*_args, **_kwargs):
        time.sleep(0.2)
        raise RuntimeError("worker slow")

    with patch("scope_recall.adapters.codex.runtime_wiring.launch_worker", side_effect=slow_launch):
        handler.handle_payload(
            {
                "hook_event_name": "Stop",
                "session_id": "TEST-deadline-session",
                "turn_id": "turn-1",
                "cwd": str(project),
                "last_assistant_message": "done",
            }
        )
    assert _count_user_sources(config, "before worker") == 1
    handler.close()


def test_expired_deadline_does_not_report_persisted(install):
    config, _core, project = install
    handler = CodexHookHandler.from_config_path(
        str(config.config_path),
        hook_started_at=time.monotonic() - (_TOTAL_BUDGET_S + 1),
    )
    handler.handle_payload(_payload(project, prompt="too late"))
    assert handler._persisted_this_call is False
    assert _count_user_sources(config, "too late") == 0
    handler.close()


def test_next_turn_finds_persisted_source_in_sqlite(install):
    from scope_recall.contracts import TrustedContext

    config, _core, project = install
    first = CodexHookHandler.from_config_path(str(config.config_path))
    first.handle_payload(_payload(project, prompt="remember this source", turn="turn-a"))
    first.close()
    second = CodexHookHandler.from_config_path(str(config.config_path))
    assert _count_user_sources(config, "remember this source") == 1
    with sqlite3.connect(config.data_directory / "memory.sqlite3") as db:
        row = db.execute(
            "SELECT event_id, source_revision FROM source_events WHERE content=?",
            ("remember this source",),
        ).fetchone()
    assert row is not None
    ctx = TrustedContext(config.to_binding(), "TEST-deadline-session", config.scope_ids, "human_direct")
    stored = second.core.source(ctx, row[0], row[1])
    assert stored is not None
    assert stored.event["content"] == "remember this source"
    second.close()


def test_capture_budget_stays_at_one_second_constant():
    assert _CAPTURE_TIMEOUT_S == 1.0
    assert _TOTAL_BUDGET_S == 2.0
