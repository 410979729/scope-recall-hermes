"""Real local hook calls enter the persistent core recall history."""

import pytest
from scope_recall.adapters.clients import CodexHookHandler, install_codex_scope_recall
from scope_recall.contracts import TrustedContext
from scope_recall.core.management import MemoryManagement
from scope_recall.core.restore import InstallationMaintenance

from tests.host.codex.test_shared_client import store as shared_log_store


def _installation(tmp_path):
    project = tmp_path / "TEST-project"
    project.mkdir()
    config, core = install_codex_scope_recall(tmp_path / "TEST-home", project_root=project, test_mode=True)
    ctx = TrustedContext(config.to_binding(), "TEST-owner", config.to_binding().scope_ids, "host_generated")
    return project, config, core, MemoryManagement(core, InstallationMaintenance(ctx))


def _payload(cwd, session, text):
    return dict(
        hook_event_name="UserPromptSubmit",
        session_id=session,
        prompt_id="TEST-prompt",
        turn_id="TEST-turn",
        prompt=text,
        cwd=str(cwd),
    )


def test_hook_recall_links_the_captured_prompt(tmp_path):
    project, config, core, owner = _installation(tmp_path)
    hook = CodexHookHandler(config, core=core)
    try:
        hook.handle_payload(_payload(project, "TEST-hook-session", "TEST telescope compass material"))
        assert hook.core.recall_diagnostics.flush()
        rows = owner.recalls(session="TEST-hook-session")["items"]
        assert len(rows) == 1
        assert rows[0]["capture"]["state"] == "persisted"
        assert rows[0]["query"]["refs"]
    finally:
        hook.close()


def test_unbound_hook_attempt_is_visible_and_private_attempt_is_absent(tmp_path):
    _project, config, core, owner = _installation(tmp_path)
    hook = CodexHookHandler(config, core=core)
    try:
        assert (
            hook.handle_payload(_payload(tmp_path / "TEST-outside", "TEST-unbound", "TEST telescope compass material"))
            == {}
        )
        assert core.recall_diagnostics.flush()
        rows = owner.recalls(session="TEST-unbound")["items"]
        assert len(rows) == 1
        assert "scope_unbound" in rows[0]["reasons"]
        owner.private_on(target="global")
        assert (
            hook.handle_payload(_payload(tmp_path / "TEST-outside", "TEST-private-unbound", "TEST private telescope"))
            == {}
        )
        assert core.recall_diagnostics.flush()
        assert owner.recalls(session="TEST-private-unbound")["items"] == []
    finally:
        hook.close()


@pytest.mark.parametrize("host", ["codex", "claude-code", "workbuddy", "dsh"])
def test_each_client_hook_uses_the_core_log(shared_log_store, tmp_path, host):
    from scope_recall.adapters.hermes.shared_entries import attach_shared_record, client_entry_record

    from tests.host.codex.test_shared_client import NOW

    root, _homes, _client, scope = shared_log_store
    home = tmp_path / ("TEST-log-" + host)
    entry = "test-log-" + host
    attach_shared_record(
        root,
        client_entry_record(
            host=host,
            home=home,
            entry_id=entry,
            display_name=host,
            attached_at=NOW,
            allowed_scope_ids=[scope],
            writable_scope_ids=[scope],
            capture_scope_id=scope,
        ),
        now=NOW,
    )
    hook = CodexHookHandler.from_home(str(home), host)
    try:
        payload = _payload(tmp_path, "TEST-shared-log", "TEST telescope compass material")
        payload["generation_id"] = "TEST-generation"
        hook.handle_payload(payload)
        core = hook.core
        assert core.recall_diagnostics.flush()
        owner_context = TrustedContext(
            core.storage.binding, "TEST-owner", core.storage.binding.scope_ids, "host_generated"
        )
        rows = MemoryManagement(core, InstallationMaintenance(owner_context)).recalls()["items"]
        assert len(rows) == 1
        assert rows[0]["entry_id"] == entry
        assert rows[0]["query"]["refs"]
    finally:
        hook.close()
