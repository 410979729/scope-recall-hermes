"""Synthetic host hooks preserve host facts only when their session snapshot is eligible."""

import json
import os
import time

import pytest
from scope_recall.adapters.clients import CodexHookHandler
from scope_recall.adapters.hermes.shared_entries import attach_shared_record, client_entry_record

from tests.host.codex.test_shared_client import store
from tests.v11_support import source_event


@pytest.mark.parametrize("host", ["claude-code", "codex"])
@pytest.mark.parametrize(
    "case", ["enabled", "enabled-default", "disabled", "unknown", "modified", "missing", "no_start"]
)
def test_hook_deduplicates_only_eligible_host_snapshot(store, tmp_path, host, case):
    root, _homes, claude_home, capture_scope = store
    project = tmp_path / "TEST-project"
    project.mkdir()
    if host == "claude-code":
        home = claude_home
        slug = "".join(c if c.isascii() and c.isalnum() else "-" for c in str(project.resolve()))
        memory = (
            home / "projects" / slug / "memory" / "MEMORY.md"
            if case == "enabled-default"
            else tmp_path / "TEST-claude-memory" / "MEMORY.md"
        )
        config_path = home / "settings.json"
        settings = {} if case == "enabled-default" else {"autoMemoryDirectory": str(memory.parent)}
        if case != "unknown":
            settings["autoMemoryEnabled"] = case != "disabled"
        config = json.dumps(settings)
    else:
        home = tmp_path / "TEST-codex-home"
        attach_shared_record(
            root,
            client_entry_record(
                host="codex",
                home=home,
                entry_id="test-codex",
                display_name="TEST Codex",
                attached_at="2026-09-24T20:00:00Z",
                allowed_scope_ids=[capture_scope],
                writable_scope_ids=[capture_scope],
                capture_scope_id=capture_scope,
            ),
            now="2026-09-24T20:00:00Z",
        )
        memory = home / "memories" / "memory_summary.md"
        config_path = home / "config.toml"
        setting = "" if case == "unknown" else "use_memories = " + ("false" if case == "disabled" else "true") + "\n"
        config = "[features]\nmemories = true\n[memories]\n" + setting
    memory.parent.mkdir(parents=True, exist_ok=True)
    sentence = "TEST observatory palette is amber."
    memory.write_text("- " + sentence + "\n", encoding="utf-8")
    config_path.write_text(config, encoding="utf-8")
    old = time.time() - 60
    os.utime(memory, (old, old))
    os.utime(config_path, (old, old))
    hook = CodexHookHandler.from_home(str(home), host)
    payload = {"session_id": "TEST-live", "cwd": str(project), "turn_id": "TEST-turn", "prompt_id": "TEST-prompt"}
    try:
        audience = hook.audience_of(payload)
        context = hook.context(audience, "TEST-seed", "human_direct")
        hook.core.record_event(
            context,
            source_event(
                content=sentence,
                source_event_key="TEST-host-seed",
                occurred_at=hook.clock.utc_now(),
                recorded_at=hook.clock.utc_now(),
            ),
            scope_id=capture_scope,
        )
        if case != "no_start":
            hook.handle_payload({**payload, "hook_event_name": "SessionStart"})
        if case == "modified":
            os.utime(memory, (time.time() + 1, time.time() + 1))
        if case == "missing":
            memory.unlink()
        result = hook.handle_payload(
            {**payload, "hook_event_name": "UserPromptSubmit", "prompt": "TEST observatory palette"}
        )
        rendered = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert (sentence not in rendered) is (case in {"enabled", "enabled-default"})
        if case in {"enabled", "enabled-default"}:
            hook.core.recall_diagnostics.flush()
            logs = hook.core.recall_diagnostics.store.list()
            assert any(
                item.get("reason") == "host_memory_duplicate" and item.get("source_type") == "event"
                for row in logs
                for item in row.get("items", [])
            )
        if case in {"enabled", "no_start"}:
            resident = hook.resident_recall_for(
                {
                    **payload,
                    "hook_event_name": "UserPromptSubmit",
                    "turn_id": "TEST-resident",
                    "prompt_id": "TEST-resident",
                    "prompt": "TEST observatory palette",
                },
                (),
                (),
                5.0,
            )
            resident_text = resident.get("hookSpecificOutput", {}).get("additionalContext", "")
            assert (sentence not in resident_text) is (case in {"enabled", "enabled-default"})
    finally:
        hook.close()


def test_remote_payload_does_not_open_server_host_memory(store, tmp_path, monkeypatch):
    root, _homes, home, _capture_scope = store
    project = tmp_path / "TEST-project-remote"
    project.mkdir()
    hook = CodexHookHandler.from_home(str(home), "claude-code")
    payload = {
        "session_id": "TEST-remote",
        "cwd": str(project),
        "prompt_id": "TEST-prompt",
        "prompt": "TEST remote palette",
        "hook_event_name": "UserPromptSubmit",
    }
    monkeypatch.setattr(
        "scope_recall.adapters.clients.prompt_recall.read_host_memory",
        lambda *args, **kwargs: pytest.fail("remote payload read server memory"),
    )
    try:
        hook.handle_payload(payload, local_record=False)
    finally:
        hook.close()


def test_claude_without_payload_working_directory_does_not_guess_settings(store, tmp_path, monkeypatch):
    root, _homes, home, _scope = store
    hook = CodexHookHandler.from_home(str(home), "claude-code")
    try:
        audience = hook.audience_of({"cwd": str(tmp_path)})
        hook.bind_project_root({}, audience)
        monkeypatch.setattr(
            "scope_recall.adapters.clients.prompt_recall.read_host_memory", lambda *a, **kw: pytest.fail("guessed cwd")
        )
        context = hook.context(audience, "TEST-session", "human_direct")
        assert not hook.prompt_recall._host_entries(context, "TEST-session", 2.0)
    finally:
        hook.close()


@pytest.mark.parametrize("host", ["claude-code", "codex"])
def test_paused_entry_and_owner_command_never_read_host_memory(store, tmp_path, host, monkeypatch):
    from scope_recall.contracts import TrustedContext
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    root, _homes, claude_home, capture_scope = store
    home = claude_home
    entry_id = "claude-code"
    if host == "codex":
        home = tmp_path / "TEST-codex-home"
        entry_id = "test-codex"
        attach_shared_record(
            root,
            client_entry_record(
                host=host,
                home=home,
                entry_id=entry_id,
                display_name="TEST",
                attached_at="2026-09-24T20:00:00Z",
                allowed_scope_ids=[capture_scope],
                writable_scope_ids=[capture_scope],
                capture_scope_id=capture_scope,
            ),
            now="2026-09-24T20:00:00Z",
        )
    hook = CodexHookHandler.from_home(str(home), host)
    binding = hook.core.storage.binding
    owner = MemoryManagement(
        hook.core, InstallationMaintenance(TrustedContext(binding, "TEST-owner", binding.scope_ids, "host_generated"))
    )
    monkeypatch.setattr(
        "scope_recall.adapters.clients.prompt_recall.read_host_memory",
        lambda *a, **kw: pytest.fail("paused read host memory"),
    )
    payload = {"session_id": "TEST-paused", "cwd": str(tmp_path), "turn_id": "TEST-turn", "prompt_id": "TEST-prompt"}
    try:
        owner.set_entry_state(entry_id, "paused", actor="TEST-owner")
        assert not hook.handle_payload({**payload, "hook_event_name": "SessionStart"})
        assert not hook.handle_payload({**payload, "hook_event_name": "UserPromptSubmit", "prompt": "TEST question"})
        result = hook.handle_payload({**payload, "hook_event_name": "UserPromptSubmit", "prompt": "scope-recall pause"})
        assert "recording" in str(result).lower()
    finally:
        hook.close()


def test_compaction_without_observed_start_does_not_attest_host_snapshot(store, tmp_path):
    from scope_recall.core.session_brief import observed_session_start

    _root, _homes, home, _scope = store
    hook = CodexHookHandler.from_home(str(home), "claude-code")
    payload = {
        "session_id": "TEST-compact-only",
        "cwd": str(tmp_path),
        "hook_event_name": "SessionStart",
        "source": "compact",
    }
    try:
        audience = hook.audience_of(payload)
        hook.bind_project_root(payload, audience)
        context = hook.context(audience, payload["session_id"], "host_generated")
        hook.handle_payload(payload)
        assert observed_session_start(hook.core.storage, context, remaining_seconds=1) is None
    finally:
        hook.close()


def test_doctor_keeps_entry_privacy_and_host_memory_together(store, tmp_path):
    from scope_recall.contracts import TrustedContext
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance
    from scope_recall.maintenance.doctor import run_doctor

    _root, _homes, home, _scope = store
    (home / "settings.json").write_text(json.dumps({"autoMemoryEnabled": True}), encoding="utf-8")
    hook = CodexHookHandler.from_home(str(home), "claude-code")
    binding = hook.core.storage.binding
    owner = MemoryManagement(
        hook.core, InstallationMaintenance(TrustedContext(binding, "TEST-owner", binding.scope_ids, "host_generated"))
    )
    try:
        owner.set_entry_state("claude-code", "read-only", actor="TEST-owner")
        owner.private_on(target="session", session="TEST-doctor-session")
        report = run_doctor(host="claude-code", instance_root=home, project_root=tmp_path).to_dict()
        assert report["host_memory"]["switches"]["autoMemoryEnabled"] is True
        assert any(row["id"] == "claude-code" and row["state"] == "read-only" for row in report["entries"])
        assert any(row["session_id"] == "TEST-doctor-session" for row in report["private_rules"])
    finally:
        hook.close()


def test_unknown_entry_state_does_not_read_host_memory(store, tmp_path, monkeypatch):
    _root, _homes, home, _scope = store
    hook = CodexHookHandler.from_home(str(home), "claude-code")
    payload = {"session_id": "TEST-entry-unavailable", "cwd": str(tmp_path)}
    try:
        audience = hook.audience_of(payload)
        hook.bind_project_root(payload, audience)
        context = hook.context(audience, payload["session_id"], "human_direct")

        def unavailable(*_args, **_kwargs):
            raise RuntimeError("TEST state unavailable")

        monkeypatch.setattr(hook.core, "entry_state", unavailable)
        monkeypatch.setattr(
            "scope_recall.adapters.clients.prompt_recall.read_host_memory",
            lambda *a, **kw: pytest.fail("read without state"),
        )
        assert not hook.prompt_recall.own(
            context, "TEST question", "TEST-request", (), time.monotonic() + 2, (), session_id=payload["session_id"]
        )
    finally:
        hook.close()
