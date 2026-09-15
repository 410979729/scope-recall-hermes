"""Meaningful isolated Codex hook adapter tests over production handler paths."""
from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scope_recall.adapters.codex import CodexHookHandler
from scope_recall.adapters.codex.boundary import host_source_key, is_scope_recall_tool
from scope_recall.adapters.codex.config import CodexConfigError, install_codex_scope_recall, load_codex_config
from scope_recall.adapters.codex.handler import emit_result
from scope_recall.adapters.codex.identity import resolve_runtime_audience
from scope_recall.core.retrieval import RetrievalResult
from tests.host.codex.source_bootstrap import HOOK_ENTRY_BOOTSTRAP, subprocess_env
from tests.v11_support import recall_item, source_event


ROOT = Path(__file__).resolve().parents[3]


def _payload(handler_root: Path, event: str, session: str = "TEST-session-1", turn: str = "TEST-turn-1", **fields):
    payload = {
        "hook_event_name": event,
        "session_id": session,
        "turn_id": turn,
        "cwd": str(handler_root),
        "model": "TEST-model",
        "permission_mode": "default",
        **fields,
    }
    if "prompt" not in payload:
        payload["prompt"] = "TEST-default-prompt"
    return payload


def test_production_mode_user_prompt_capture_and_recall(handler, installed):
    hook, project_root, config = handler
    _, core, clock, _ = installed
    ctx = hook.core.config.binding
    from scope_recall.contracts import TrustedContext

    trusted = TrustedContext(ctx, "TEST-session-1", config.scope_ids, "human_direct")
    core.record_event(
        trusted,
        source_event(
            content="TEST 项目偏好白色。",
            source_event_key="codex-seed/1",
        ),
        scope_id=config.audience_scopes["project"],
        remaining_seconds=5,
    )

    calls = {"search": 0, "current_source_refs": None}

    class CountingPipeline:
        storage_reader = core.recall_pipeline.storage_reader

        def search(self, search_context):
            calls["search"] += 1
            calls["current_source_refs"] = search_context.current_source_refs
            return RetrievalResult(
                items=(),
                candidates=(),
                memory_epoch=core.status(trusted).memory_epoch,
                gaps=(),
                answerability_hint="unknown",
                coverage="unknown",
                candidate_count=0,
                admitted_count=0,
                request_id="TEST-request",
            )

    core.recall_pipeline = CountingPipeline()
    result = hook.handle_payload(_payload(project_root, "UserPromptSubmit", prompt="继续 TEST 项目"))
    assert calls["search"] == 1
    assert calls["current_source_refs"] and all(ref.endswith("@1") for ref in calls["current_source_refs"])
    assert result == {}


def test_continue_and_chinese_prompts_are_not_filtered(handler, installed):
    hook, project_root, config = handler
    for prompt in ("continue", "继续"):
        result = hook.handle_payload(_payload(project_root, "UserPromptSubmit", turn=f"turn-{prompt}", prompt=prompt))
        assert result == {}
        with sqlite3.connect(config.data_directory / "memory.sqlite3") as db:
            count = db.execute("SELECT count(*) FROM source_events WHERE content=?", (prompt,)).fetchone()[0]
        assert count == 1


def test_same_turn_replay_is_idempotent_but_distinct_turns_persist(handler, installed):
    hook, project_root, config = handler
    _, core, clock, _ = installed
    prompt = "same text"
    first = hook.handle_payload(_payload(project_root, "UserPromptSubmit", turn="turn-a", prompt=prompt))
    second = hook.handle_payload(_payload(project_root, "UserPromptSubmit", turn="turn-a", prompt=prompt))
    assert first == second == {}

    turn_b_refs: list[str] = []

    class TurnBCore:
        def __getattr__(self, name):
            return getattr(core, name)

        def recall_packet(self, context, request, *, current_source_refs=(), deadline_seconds=2.0):
            turn_b_refs.extend(current_source_refs)
            return core.recall_packet(
                context,
                request,
                current_source_refs=current_source_refs,
                deadline_seconds=deadline_seconds,
            )

    turn_b_handler = CodexHookHandler(config, core=TurnBCore(), clock=clock)
    third = turn_b_handler.handle_payload(_payload(project_root, "UserPromptSubmit", turn="turn-b", prompt=prompt))
    assert turn_b_refs and all(ref.endswith("@1") for ref in turn_b_refs)
    with sqlite3.connect(config.data_directory / "memory.sqlite3") as db:
        keys = [
            row[0]
            for row in db.execute("SELECT source_event_key FROM source_events WHERE role='user' ORDER BY source_event_key")
        ]
    assert len(keys) == 2
    assert keys[0] != keys[1]
    turn_a_key = host_source_key(
        installation_id=config.installation_id,
        session_id="TEST-session-1",
        event_kind="user",
        event_id="turn-a",
    )
    turn_b_key = host_source_key(
        installation_id=config.installation_id,
        session_id="TEST-session-1",
        event_kind="user",
        event_id="turn-b",
    )
    assert {keys[0], keys[1]} == {turn_a_key, turn_b_key}
    if third:
        assert "hookSpecificOutput" in third
        recalled = third["hookSpecificOutput"]["additionalContext"]
        assert prompt in recalled or "same text" in recalled


def test_current_source_exclusion_uses_capture_receipt(handler, installed):
    hook, project_root, config = handler
    _, core, clock, _ = installed
    captured_refs: list[str] = []

    class TrackingCore:
        def __getattr__(self, name):
            return getattr(core, name)

        def record_host_event(self, context, value, *, scope_id, host_scope, remaining_seconds=None):
            receipt = core.record_host_event(context, value, scope_id=scope_id, host_scope=host_scope, remaining_seconds=remaining_seconds)
            captured_refs.extend(f"{write.ref}@{write.revision}" for write in receipt.event_refs)
            return receipt

        def recall_packet(self, context, request, *, current_source_refs=(), deadline_seconds=2.0):
            assert current_source_refs == tuple(captured_refs)
            return core.recall_packet(
                context,
                request,
                current_source_refs=current_source_refs,
                deadline_seconds=deadline_seconds,
            )

    tracking = CodexHookHandler(config, core=TrackingCore(), clock=clock)
    tracking.handle_payload(_payload(project_root, "UserPromptSubmit", prompt="resume query"))


def test_resume_query_reaches_recall_with_seeded_memory(handler, installed):
    hook, project_root, config = handler
    _, core, _, _ = installed
    from scope_recall.contracts import TrustedContext

    trusted = TrustedContext(core.config.binding, "TEST-session-1", config.scope_ids, "human_direct")
    core.record_event(
        trusted,
        source_event(content="TEST 白色偏好", source_event_key="resume-seed/1"),
        scope_id=config.audience_scopes["project"],
        remaining_seconds=5,
    )

    class PacketCore:
        def __getattr__(self, name):
            return getattr(core, name)

        def recall_packet(self, context, request, *, current_source_refs=(), deadline_seconds=2.0):
            assert request["query"] == "继续 TEST 项目"
            assert request["mode"] == "auto"
            return {
                "protocol_version": "1.1",
                "request_id": request["request_id"],
                "status": "ok",
                "memory_epoch": 1,
                "items": [recall_item(content="TEST 白色偏好")],
                "gaps": [],
                "diagnostic_ref": None,
                "answerability": "supported",
                "coverage": "partial",
                "unmet_needs": [],
            }

    packet_handler = CodexHookHandler(config, core=PacketCore(), clock=installed[2])
    result = packet_handler.handle_payload(_payload(project_root, "UserPromptSubmit", prompt="继续 TEST 项目"))
    assert "hookSpecificOutput" in result
    assert result["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "TEST 白色偏好" in result["hookSpecificOutput"]["additionalContext"]


def test_unrecognized_project_root_and_missing_session_fail_closed(handler, installed, tmp_path):
    hook, project_root, _config = handler
    outside = tmp_path / "TEST-outside"
    outside.mkdir()
    payload = _payload(outside, "UserPromptSubmit", prompt="hello")
    assert hook.handle_payload(payload) == {}
    assert hook.diagnostics.last_reason == "no_audience"
    bad = dict(payload)
    bad["session_id"] = ""
    assert hook.handle_payload(bad) == {}
    assert hook.diagnostics.last_reason == "invalid_session"


def test_path_traversal_cwd_does_not_bind_foreign_root(handler, installed, tmp_path):
    hook, project_root, _config = handler
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    link = project_root / "escape"
    try:
        link.symlink_to(foreign, target_is_directory=True)
        cwd = str(link)
    except OSError:
        cwd = str(foreign)
    assert hook.handle_payload(_payload(Path(cwd), "UserPromptSubmit", prompt="hello")) == {}


def test_stop_never_loops_and_records_only_present_assistant_text(handler, installed):
    hook, project_root, config = handler
    assert hook.handle_payload(
        _payload(project_root, "Stop", last_assistant_message="final answer")
    ) == {}
    assert hook.handle_payload(_payload(project_root, "Stop", turn="turn-blank", last_assistant_message="")) == {}
    with sqlite3.connect(config.data_directory / "memory.sqlite3") as db:
        rows = db.execute("SELECT content FROM source_events WHERE role='assistant'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "final answer"


def test_post_tool_use_provenance_for_own_tools(handler, installed):
    hook, project_root, config = handler
    hook.handle_payload(
        _payload(
            project_root,
            "PostToolUse",
            tool_name="mcp__scope_recall__recall",
            tool_use_id="tool-1",
            tool_input={"query": "x"},
            tool_response={"items": []},
        )
    )
    hook.handle_payload(
        _payload(
            project_root,
            "PostToolUse",
            turn="turn-host",
            tool_name="Bash",
            tool_use_id="tool-2",
            tool_input={"command": "echo"},
            tool_response="ok",
        )
    )
    with sqlite3.connect(config.data_directory / "memory.sqlite3") as db:
        origins = [row[0] for row in db.execute("SELECT origin FROM source_events WHERE role='tool' ORDER BY source_event_key")]
    assert origins == ["memory_reinjection", "tool_observation"]
    assert is_scope_recall_tool("mcp__scope_recall__recall")
    assert not is_scope_recall_tool("Bash")


def test_interrupt_and_session_end_are_host_generated_without_model_calls(handler, installed):
    hook, project_root, config = handler
    _, core, _, _ = installed
    core_calls = {"recall": 0}

    class GuardCore:
        def __getattr__(self, name):
            return getattr(core, name)

        def recall_packet(self, *args, **kwargs):
            core_calls["recall"] += 1
            raise AssertionError("model recall on lifecycle hook")

    guarded = CodexHookHandler(config, core=GuardCore(), clock=installed[2])
    assert guarded.handle_payload(_payload(project_root, "Interrupt")) == {}
    assert guarded.handle_payload(_payload(project_root, "SessionEnd", turn="", reason="completed")) == {}
    assert core_calls["recall"] == 0
    with sqlite3.connect(config.data_directory / "memory.sqlite3") as db:
        origins = [row[0] for row in db.execute("SELECT origin FROM source_events WHERE role='system'")]
    assert origins == ["host_generated", "host_generated"]


def test_missing_public_fields_do_not_capture(handler, installed):
    hook, project_root, config = handler
    payload = _payload(project_root, "UserPromptSubmit")
    payload.pop("prompt")
    assert hook.handle_payload(payload) == {}
    assert hook.handle_payload(_payload(project_root, "PostToolUse")) == {}
    with sqlite3.connect(config.data_directory / "memory.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 0


def test_missing_turn_id_records_capability_gap_not_default_turn(handler, installed):
    hook, project_root, config = handler
    payload = _payload(project_root, "UserPromptSubmit", prompt="hello")
    payload.pop("turn_id")
    assert hook.handle_payload(payload) == {}
    with sqlite3.connect(config.data_directory / "memory.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 0


def test_capture_failure_surfaces_without_recall(handler, installed):
    hook, project_root, config = handler
    _, core, clock, _ = installed

    class FailingCore:
        def __getattr__(self, name):
            return getattr(core, name)

        def record_host_event(self, *args, **kwargs):
            from scope_recall.core.capture import CaptureReceipt

            return CaptureReceipt("unavailable", (), "unknown", "unknown", "unknown", error_code="STORAGE_UNAVAILABLE")

        def recall_packet(self, *args, **kwargs):
            raise AssertionError("recall after capture failure")

    failing = CodexHookHandler(config, core=FailingCore(), clock=clock)
    assert failing.handle_payload(_payload(project_root, "UserPromptSubmit", prompt="hello")) == {}


def test_attachment_without_authorization_records_gap(handler, installed):
    hook, project_root, config = handler
    hook.handle_payload(
        _payload(
            project_root,
            "UserPromptSubmit",
            prompt="see attachment",
            attachments=[{"filename": "x.png"}],
        )
    )
    assert hook.diagnostics.last_reason == "attachment_gap"
    assert "attachment_gap:host_authorization_unverified" in hook.diagnostics.capability_gaps
    with sqlite3.connect(config.data_directory / "memory.sqlite3") as db:
        row = db.execute("SELECT content, extra_json FROM source_events WHERE role='user'").fetchone()
    assert row is not None
    assert row[0] == "see attachment"
    extras = json.loads(row[1])
    assert "artifact_refs" not in extras


def test_config_path_must_be_absolute_and_existing_db(tmp_path, project_root):
    config, _core = install_codex_scope_recall(tmp_path / "install", project_root=project_root)
    with pytest.raises(CodexConfigError):
        load_codex_config("relative.json")
    broken = json.loads(config.config_path.read_text(encoding="utf-8"))
    broken["data_directory"] = str(tmp_path / "missing-data")
    config.config_path.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(CodexConfigError, match="database"):
        load_codex_config(config.config_path)


def test_emit_result_is_ascii_safe_on_legacy_windows_stdout(monkeypatch):
    result = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "中文记忆 🧪",
            "nested": {"emoji": "🚀", "text": "保持原文"},
        }
    }
    buffer = io.BytesIO()
    stdout = io.TextIOWrapper(buffer, encoding="cp936")
    monkeypatch.setattr(sys, "stdout", stdout)
    try:
        emit_result(result)
        stdout.flush()
        encoded = buffer.getvalue()
    finally:
        stdout.detach()
    assert encoded.isascii()
    assert json.loads(encoded.decode("ascii")) == result


def test_source_keys_use_session_turn_and_public_event_ids():
    key = host_source_key(
        installation_id="install-1",
        session_id="session-1",
        event_kind="tool",
        event_id="exec-123",
    )
    assert key == "codex:install-1:session-1:tool:exec-123@1"


def test_hook_entry_cli_uses_absolute_config_and_bounded_stdin(installed, project_root):
    config, _, _, _ = installed
    payload = json.dumps(_payload(project_root, "SessionStart", turn=""))
    env = subprocess_env()
    proc = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            HOOK_ENTRY_BOOTSTRAP,
            "--config",
            str(config.config_path),
        ],
        input=payload,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    assert json.loads(proc.stdout) == {}
    oversized = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            HOOK_ENTRY_BOOTSTRAP,
            "--config",
            str(config.config_path),
        ],
        input="x" * 70000,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    assert json.loads(oversized.stdout) == {}
    assert "input_too_large" in oversized.stderr


def test_new_session_uses_distinct_source_keys(handler, installed):
    hook, project_root, config = handler
    hook.handle_payload(_payload(project_root, "UserPromptSubmit", session="session-a", turn="turn-1", prompt="one"))
    hook.handle_payload(_payload(project_root, "UserPromptSubmit", session="session-b", turn="turn-1", prompt="one"))
    with sqlite3.connect(config.data_directory / "memory.sqlite3") as db:
        keys = [row[0] for row in db.execute("SELECT source_event_key FROM source_events")]
    assert len(keys) == 2
    assert keys[0] != keys[1]


def test_audience_resolution_matches_normalized_ancestor(handler, installed, project_root):
    config, _, _, _ = installed
    nested = project_root / "nested" / "dir"
    nested.mkdir(parents=True)
    audience = resolve_runtime_audience(config, str(nested))
    assert audience.capture_scope_id == config.audience_scopes["project"]
    assert config.audience_scopes["shared"] in audience.allowed_scope_ids


def test_live_hook_events_carry_witnessed_occurrence_time():
    """Live Codex hook/MCP events are witnessed by the host: occurred_at
    grounds to the event time so current-mode recall can serve them."""
    from scope_recall.adapters.codex.boundary import (
        assistant_stop_source_event, lifecycle_source_event, tool_use_source_event, user_prompt_source_event,
    )

    user_event = user_prompt_source_event(
        installation_id="TEST-install", session_id="TEST-session", turn_id="turn-1",
        prompt="请记住我的靛蓝档案目录名称是 TEST-X。", recorded_at="2026-09-06T12:00:00Z",
    )
    assert user_event is not None
    assert user_event["occurred_at"] == "2026-09-06T12:00:00Z"
    assert user_event["time_precision"] == "instant"

    assistant_event, _ = assistant_stop_source_event(
        installation_id="TEST-install", session_id="TEST-session", turn_id="turn-1",
        message="好的。", recorded_at="2026-09-06T12:00:01Z",
    )
    assert assistant_event is not None
    assert assistant_event["occurred_at"] == "2026-09-06T12:00:01Z"
    assert assistant_event["time_precision"] == "instant"

    tool_event, _gaps, _origin = tool_use_source_event(
        installation_id="TEST-install", session_id="TEST-session", turn_id="turn-1",
        tool_use_id="tool-1", tool_name="shell", tool_input={"cmd": "ls"}, tool_response={"out": "ok"},
        recorded_at="2026-09-06T12:00:02Z",
    )
    assert tool_event is not None
    assert tool_event["occurred_at"] == "2026-09-06T12:00:02Z"
    assert tool_event["time_precision"] == "instant"

    lifecycle_event = lifecycle_source_event(
        installation_id="TEST-install", session_id="TEST-session", event_kind="session_start",
        event_id="start-1", content="session started", recorded_at="2026-09-06T12:00:03Z",
    )
    assert lifecycle_event["occurred_at"] == "2026-09-06T12:00:03Z"
    assert lifecycle_event["time_precision"] == "instant"
