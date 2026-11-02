"""Direct owner commands over synthetic local and remote host boundaries."""

import json

import pytest
from scope_recall.adapters.clients import remote_client
from scope_recall.adapters.clients.config import load_shared_client
from scope_recall.adapters.clients.identity import stored_session_id
from scope_recall.contracts import TrustedContext
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.management import MemoryManagement
from scope_recall.core.restore import InstallationMaintenance

from tests.host.codex.test_remote import _client, _hook, _rows
from tests.host.codex.test_remote import served as served
from tests.host.codex.test_remote import store as store


@pytest.mark.parametrize("host", ["codex", "claude-code"])
def test_remote_human_pause_resume_without_spooling_commands(served, tmp_path, host):
    root, homes, ports = served
    client = _client(tmp_path, host, ports[host])
    config = load_shared_client(homes[host], host)
    binding = config.to_binding()
    core = MemoryCore(CoreConfig(binding))
    context = TrustedContext(binding, "TEST-owner", binding.scope_ids, "host_generated")
    owner = MemoryManagement(core, InstallationMaintenance(context))
    session = "TEST-remote-command"
    stored_session = stored_session_id(config, session)
    payload = dict(
        hook_event_name="UserPromptSubmit",
        session_id=session,
        turn_id="TEST-turn",
        prompt_id="TEST-turn",
        cwd=str(tmp_path),
        prompt="scope-recall pause",
    )
    assert _hook(client, payload)["decision"] == "block"
    assert owner.private_status(session=stored_session)["mode"] == "full"
    assert (
        _hook(
            client, {**payload, "turn_id": "TEST-resume", "prompt_id": "TEST-resume", "prompt": "scope-recall resume"}
        )["decision"]
        == "block"
    )
    assert owner.private_status(session=stored_session)["mode"] == "none"
    assert not list(remote_client._spool_dir(client).glob("*.json"))
    assert all(
        "scope-recall pause" not in row[2] and "scope-recall resume" not in row[2]
        for row in _rows(root, config.entry_id)
    )


def test_remote_replayed_hook_cannot_mint_an_owner_command(store, tmp_path):
    from scope_recall.adapters.clients import remote_server

    root, homes = store
    home = homes["codex"]
    config = load_shared_client(home, "codex")
    binding = config.to_binding()
    core = MemoryCore(CoreConfig(binding))
    owner = MemoryManagement(
        core, InstallationMaintenance(TrustedContext(binding, "TEST-owner", binding.scope_ids, "host_generated"))
    )
    session = "TEST-replayed-command"
    payload = dict(
        hook_event_name="UserPromptSubmit",
        session_id=session,
        turn_id="TEST-turn",
        cwd=str(tmp_path),
        prompt="scope-recall pause",
        scope_recall_direct_command=True,
    )
    remote_server.handle_request(
        remote_server.RemoteServerConfig(home, "codex", "127.0.0.1", 1, "0" * 64),
        dict(payload=payload),
    )
    assert owner.private_status(session=stored_session_id(config, session))["mode"] == "none"
    assert all("scope-recall pause" not in row[2] for row in _rows(root, config.entry_id))


def test_workbuddy_remote_command_failure_still_blocks(monkeypatch):
    monkeypatch.setattr(remote_client, "_post", lambda *_args: None)
    request = {"hook_event_name": "UserPromptSubmit", "session_id": "TEST-session", "prompt": "scope-recall pause"}
    result = remote_client.run_hook({"host": "workbuddy"}, json.dumps(request).encode())
    assert result["continue"] is False and "could not" in result["stopReason"]
