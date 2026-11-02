"""The hook preserves the reason a capture was refused."""

from scope_recall.adapters.clients import CodexHookHandler
from scope_recall.contracts import TrustedContext
from scope_recall.core.management import MemoryManagement
from scope_recall.core.restore import InstallationMaintenance

from tests.host.codex.test_merged_host_boundaries import client
from tests.host.codex.test_shared_client import store


def test_hook_reports_entry_state(client, tmp_path, monkeypatch):
    _root, home, host = client
    hook = CodexHookHandler.from_home(str(home), host)
    monkeypatch.setattr(hook, "ensure_runtime", lambda *args, **kwargs: None)
    binding = hook.core.storage.binding
    owner = MemoryManagement(
        hook.core, InstallationMaintenance(TrustedContext(binding, "TEST-owner", binding.scope_ids, "host_generated"))
    )
    owner.set_entry_state("test-boundary", "read-only", actor="TEST-owner")
    try:
        hook.handle_payload(
            dict(
                hook_event_name="UserPromptSubmit",
                session_id="TEST-session",
                prompt_id="TEST-1",
                generation_id="TEST-1",
                turn_id="TEST-1",
                cwd=str(tmp_path),
                prompt="TEST observatory palette amber.",
            )
        )
        assert "entry_state:read-only" in hook.diagnostics.capability_gaps
    finally:
        hook.close()


def test_read_only_hook_keeps_semantic_recall(client, tmp_path, monkeypatch):
    _root, home, host = client
    hook = CodexHookHandler.from_home(str(home), host)
    calls = []
    monkeypatch.setattr(hook, "ensure_runtime", lambda *args, **kwargs: calls.append(True))
    binding = hook.core.storage.binding
    owner = MemoryManagement(
        hook.core, InstallationMaintenance(TrustedContext(binding, "TEST-owner", binding.scope_ids, "host_generated"))
    )
    owner.set_entry_state("test-boundary", "read-only", actor="TEST-owner")
    try:
        hook.handle_payload(
            dict(
                hook_event_name="UserPromptSubmit",
                session_id="TEST-session",
                prompt_id="TEST-2",
                generation_id="TEST-2",
                turn_id="TEST-2",
                cwd=str(tmp_path),
                prompt="TEST observatory palette amber.",
            )
        )
        assert calls
    finally:
        hook.close()


def test_entry_pause_at_brief_confirmation_discards_all_prepared_memory(client, monkeypatch):
    import time

    _root, home, host = client
    hook = CodexHookHandler.from_home(str(home), host)
    try:
        binding = hook.core.storage.binding
        context = TrustedContext(binding, "TEST-session", binding.scope_ids, "host_generated", entry_id="test-boundary")
        hook._pending_brief = None
        hook._injection_started = hook.clock.monotonic()
        monkeypatch.setattr(hook.core, "confirm_session_brief", lambda *_args, **_kwargs: "entry_state:paused")
        result = {"hookSpecificOutput": {"additionalContext": "TEST brief\nTEST recalled memory"}}
        assert hook._confirm_brief(result, time.monotonic() + 10, context) == {}
        assert hook.diagnostics.last_reason == "entry_state:paused"
    finally:
        hook.close()


def test_paused_entry_session_start_reports_brief_refusal(client, tmp_path, monkeypatch):
    import pytest

    _root, home, host = client
    if host not in {"codex", "claude-code"}:
        pytest.skip("This host has no SessionStart event")
    hook = CodexHookHandler.from_home(str(home), host)
    monkeypatch.setattr(hook, "ensure_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hook, "launch_worker", lambda *_args, **_kwargs: None)
    binding = hook.core.storage.binding
    owner = MemoryManagement(
        hook.core, InstallationMaintenance(TrustedContext(binding, "TEST-owner", binding.scope_ids, "host_generated"))
    )
    owner.set_entry_state("test-boundary", "paused", actor="TEST-owner")
    try:
        result = hook.handle_payload(dict(hook_event_name="SessionStart", session_id="TEST-start", cwd=str(tmp_path)))
        assert not result.get("hookSpecificOutput", {}).get("additionalContext")
        assert (
            "entry_state:paused" in hook.diagnostics.capability_gaps
            or hook.diagnostics.last_reason == "entry_state:paused"
        )
    finally:
        hook.close()
