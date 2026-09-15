"""Capture acknowledgement and recovery through the actual Codex host boundary."""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from scope_recall.adapters.codex import CodexHookHandler
from scope_recall.adapters.codex.handler import emit_result
from scope_recall.contracts import ContractError
from scope_recall.core.candidate_storage import CandidateLifecycle
from scope_recall.runtime.instance import RuntimeInstanceConfig, build_runtime_instance


def payload(project, turn="TEST-recovery-turn"):
    return dict(hook_event_name="UserPromptSubmit", session_id="TEST-recovery-session",
                cwd=str(project), turn_id=turn, prompt="TEST recovery source with exact evidence")


def test_post_ingress_failure_survives_host_exit_and_replays_once(installed, capsys):
    config, core, clock, project = installed
    handler = CodexHookHandler(config, core=core, clock=clock)
    with patch.object(CandidateLifecycle, "observe_source", side_effect=ContractError("DEADLINE_EXCEEDED")):
        result = handler.handle_payload(payload(project))
    assert result == {}
    with sqlite3.connect(config.data_directory / "memory.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 0
    assert handler.diagnostics.capture_durability == "queued"
    emit_result(result, diagnostics=handler.diagnostics)
    captured = capsys.readouterr()
    assert "TEST recovery source" not in captured.err
    detail = json.loads(next(line.split(":", 1)[1] for line in captured.err.splitlines() if line.startswith("CODEX_CAPTURE:")))
    assert detail["stage"] == "durable_inbox" and detail["error_code"] == "DEADLINE_EXCEEDED"
    assert detail["elapsed_ms"] >= 0
    handler.close()
    runtime_config = RuntimeInstanceConfig(binding=config.to_binding(), session_id="TEST-new-process",
        allowed_scope_ids=config.scope_ids, host_adapter="codex")
    runtime = build_runtime_instance(runtime_config)
    try:
        runtime.drain(max_items=1, remaining_seconds=5)
        runtime.drain(max_items=1, remaining_seconds=5)
    finally:
        runtime.close()
    with sqlite3.connect(config.data_directory / "memory.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 1


def test_pre_ingress_failure_is_explicit_without_raw_exception(installed, capsys):
    config, core, clock, project = installed
    handler = CodexHookHandler(config, core=core, clock=clock)
    with patch("scope_recall.core.capture_inbox.enqueue", side_effect=OSError("SECRET-token-file-content")):
        result = handler.handle_payload(payload(project))
    emit_result(result, diagnostics=handler.diagnostics)
    output = capsys.readouterr()
    assert "SECRET" not in output.err
    assert handler.diagnostics.capture_stage == "ingress"
    assert handler.diagnostics.capture_error_type == "OSError"
    assert handler.diagnostics.capture_durability != "persisted"
    with sqlite3.connect(config.data_directory / "memory.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 0


def test_queued_capture_wakes_configured_worker_without_claiming_source_commit(installed):
    config, core, clock, project = installed
    handler = CodexHookHandler(config, core=core, clock=clock)
    calls = []
    handler._host_runtime = SimpleNamespace(
        configured=True, capability_gaps=(), hook_processing_seconds=2.0,
        rebind_session=lambda *args: None,
        maybe_launch_bounded_worker=lambda **kwargs: calls.append(kwargs) or (),
    )
    with patch.object(CandidateLifecycle, "observe_source", side_effect=ContractError("DEADLINE_EXCEEDED")):
        assert handler.handle_payload(payload(project)) == {}
    assert len(calls) == 1
    assert not handler._persisted_this_call
    assert handler._queued_this_call


def test_a_collapsed_error_code_keeps_its_original_on_the_local_line(installed, capsys):
    """The host contract stays frozen; the operator still learns what happened.

    ``_CAPTURE_ERROR_CODES`` is what the host is allowed to see, so anything
    outside it becomes ``CAPTURE_ERROR``.  That collapse is why a real flake --
    one failure in thirteen runs of the MCP stdio suite -- reported only
    ``CAPTURE_ERROR / not_persisted`` with no way to learn the cause.
    """
    config, core, clock, project = installed
    handler = CodexHookHandler(config, core=core, clock=clock)
    with patch.object(CandidateLifecycle, "observe_source", side_effect=ContractError("SOMETHING_SPECIFIC")):
        handler.handle_payload(payload(project, turn="TEST-collapse-turn"))
    assert handler.diagnostics.capture_error_code == "CAPTURE_ERROR"
    assert handler.diagnostics.capture_error_detail == "SOMETHING_SPECIFIC"

    emit_result({}, diagnostics=handler.diagnostics)
    line = next(l for l in capsys.readouterr().err.splitlines() if l.startswith("CODEX_CAPTURE:"))
    detail = json.loads(line.split(":", 1)[1])
    assert detail["error_code"] == "CAPTURE_ERROR"
    assert detail["error_detail"] == "SOMETHING_SPECIFIC"
    handler.close()


def test_an_allowlisted_code_adds_no_redundant_detail(installed, capsys):
    config, core, clock, project = installed
    handler = CodexHookHandler(config, core=core, clock=clock)
    with patch.object(CandidateLifecycle, "observe_source", side_effect=ContractError("DEADLINE_EXCEEDED")):
        handler.handle_payload(payload(project, turn="TEST-allowlisted-turn"))
    assert handler.diagnostics.capture_error_code == "DEADLINE_EXCEEDED"

    emit_result({}, diagnostics=handler.diagnostics)
    line = next(l for l in capsys.readouterr().err.splitlines() if l.startswith("CODEX_CAPTURE:"))
    assert "error_detail" not in json.loads(line.split(":", 1)[1])
    handler.close()


def test_only_something_shaped_like_a_code_reaches_the_diagnostic_line():
    """The line is for codes, so it cannot become a channel for payload text."""
    from scope_recall.adapters.codex.handler import _error_detail

    assert _error_detail("VERSION_CONFLICT") == "VERSION_CONFLICT"
    assert _error_detail("capture.gap:not_persisted-1") == "capture.gap:not_persisted-1"
    assert _error_detail("") is None
    assert _error_detail(None) is None
    assert _error_detail("x" * 65) is None
    assert _error_detail("leaked user secret") is None
    assert _error_detail("line\nbreak") is None
