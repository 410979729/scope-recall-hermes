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
