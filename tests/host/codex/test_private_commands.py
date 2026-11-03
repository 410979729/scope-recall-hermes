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

from tests.host.codex.test_dsh_remote import remote as dsh_remote
from tests.host.codex.test_remote import _client, _hook, _rows
from tests.host.codex.test_remote import served as served
from tests.host.codex.test_remote import store as store


@pytest.mark.parametrize("host", ["codex", "claude-code"])
def test_legacy_remote_pause_and_refused_resume(served, tmp_path, host, caplog):
    from scope_recall.adapters.clients import remote_server

    root, homes, _ports = served
    config = load_shared_client(homes[host], host)
    binding = config.to_binding()
    core = MemoryCore(CoreConfig(binding))
    owner = MemoryManagement(
        core, InstallationMaintenance(TrustedContext(binding, "TEST-owner", binding.scope_ids, "host_generated"))
    )
    session = "TEST-legacy-command"
    stored = stored_session_id(config, session)
    server = remote_server.RemoteServerConfig(homes[host], host, "127.0.0.1", 1, "0" * 64)
    payload = dict(
        hook_event_name="UserPromptSubmit",
        session_id=session,
        turn_id="TEST-turn",
        prompt_id="TEST-turn",
        cwd=str(tmp_path),
        prompt="scope-recall pause",
    )
    with caplog.at_level("INFO", logger="scope_recall.remote_server"):
        answer = remote_server.handle_request(server, {"payload": payload})["result"]
    assert "full" in str(answer)
    assert "4.0" in str(answer) and "upgrade" in str(answer).lower()
    assert owner.private_status(session=stored)["mode"] == "full"
    assert "legacy" in caplog.text.lower()
    remote_server.handle_request(server, {"payload": payload})
    assert owner.private_status(session=stored)["active_rules"] == 1
    assert owner.private_status(session=stored_session_id(config, "TEST-other"))["mode"] == "none"
    remote_server.handle_request(server, {"payload": {**payload, "prompt": "TEST private sentinel"}})
    result = remote_server.handle_request(server, {"payload": {**payload, "prompt": "scope-recall resume"}})["result"]
    assert "4.0" in str(result) and "upgrade" in str(result).lower()
    assert owner.private_status(session=stored)["mode"] == "full"
    assert all(
        "TEST private sentinel" not in row[2] and "scope-recall " not in row[2] for row in _rows(root, config.entry_id)
    )


def test_dsh_legacy_pause_and_resume(dsh_remote, tmp_path):
    from scope_recall.adapters.clients import remote_server

    root, home, _client_config, _bodies, _records = dsh_remote
    config = load_shared_client(home, "dsh")
    binding = config.to_binding()
    core = MemoryCore(CoreConfig(binding))
    owner = MemoryManagement(
        core, InstallationMaintenance(TrustedContext(binding, "TEST-owner", binding.scope_ids, "host_generated"))
    )
    stored = stored_session_id(config, "TEST-dsh-legacy")
    server = remote_server.RemoteServerConfig(home, "dsh", "127.0.0.1", 1, "0" * 64)
    payload = dict(
        hook_event_name="UserPromptSubmit",
        session_id="TEST-dsh-legacy",
        turn_id="TEST-turn",
        cwd=str(tmp_path),
        prompt="scope-recall pause",
    )
    assert "4.0" in str(remote_server.handle_request(server, {"payload": payload})["result"])
    remote_server.handle_request(server, {"payload": {**payload, "prompt": "TEST private sentinel"}})
    result = remote_server.handle_request(server, {"payload": {**payload, "prompt": "scope-recall resume"}})["result"]
    assert "4.0" in str(result) and "upgrade" in str(result).lower()
    assert owner.private_status(session=stored)["mode"] == "full"
    assert not any(
        "TEST private sentinel" in row[2] or "scope-recall " in row[2] for row in _rows(root, config.entry_id)
    )


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


@pytest.mark.parametrize(
    "text,extra",
    [
        ("scope-recall resume<private>TEST-hidden</private>", {}),
        ("scope-recall resume", {"agent_id": "agent-TEST"}),
        ("scope-recall resume", {"transcript_path": "/TEST/subagents/agent.jsonl"}),
        ("scope-recall resume", {"role": "assistant"}),
        ("scope-recall resume", {"origin": "tool_observation"}),
    ],
)
def test_remote_only_raw_direct_human_input_is_a_command(monkeypatch, tmp_path, text, extra):
    outgoing = []
    monkeypatch.setattr(remote_client, "_post", lambda _config, body, _timeout: outgoing.append(body) or {"result": {}})
    monkeypatch.setattr(
        remote_client, "_send_checked", lambda _config, body, _until: outgoing.append(body) or {"result": {}}
    )
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "TEST-owner", "prompt": text, **extra}
    remote_client.run_hook({"host": "workbuddy", "state_dir": tmp_path}, json.dumps(payload).encode())
    assert all(not body.get("direct_owner_command") for body in outgoing)


def test_replayed_resume_cannot_close_later_pause(store, tmp_path):
    from datetime import UTC, datetime, timedelta

    from scope_recall.adapters.clients import remote_server

    _root, homes = store
    home = homes["codex"]
    config = load_shared_client(home, "codex")
    binding = config.to_binding()
    core = MemoryCore(CoreConfig(binding))
    owner = MemoryManagement(
        core, InstallationMaintenance(TrustedContext(binding, "TEST-owner", binding.scope_ids, "host_generated"))
    )
    session = "TEST-replay-order"
    stored = stored_session_id(config, session)
    owner.private_on(target="session", session=stored)
    server = remote_server.RemoteServerConfig(home, "codex", "127.0.0.1", 1, "0" * 64)
    now = datetime.now(UTC)

    def body(text, identifier, seconds):
        return {
            "payload": {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session,
                "turn_id": identifier,
                "cwd": str(tmp_path),
                "prompt": text,
            },
            "direct_owner_command": True,
            "fresh_observation": True,
            "command_id": identifier,
            "observed_at": (now + timedelta(seconds=seconds)).isoformat(),
        }

    old_resume = body("scope-recall resume", "TEST-resume-A", -4)
    remote_server.handle_request(server, old_resume)
    with pytest.raises(remote_server.RemoteServerError):
        remote_server.handle_request(server, body("scope-recall resume", "TEST-resume-A", -3))
    remote_server.handle_request(server, body("scope-recall pause", "TEST-pause-B", -2))
    assert owner.private_status(session=stored)["mode"] == "full"
    with pytest.raises(remote_server.RemoteServerError):
        remote_server.handle_request(server, old_resume)
    assert owner.private_status(session=stored)["mode"] == "full"
    with pytest.raises(remote_server.RemoteServerError):
        remote_server.handle_request(server, body("scope-recall resume", "TEST-older-C", -3))
    assert owner.private_status(session=stored)["mode"] == "full"


def test_direct_command_keeps_source_fields_for_server_check(monkeypatch, tmp_path):
    outgoing = []
    monkeypatch.setattr(remote_client, "_post", lambda _config, body, _timeout: outgoing.append(body) or {"result": {}})
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "TEST-owner",
        "prompt": "scope-recall pause",
        "agent_id": "main-TEST",
        "transcript_path": "/TEST/main.jsonl",
        "source": {"kind": "user"},
    }
    remote_client.run_hook({"host": "workbuddy", "state_dir": tmp_path}, json.dumps(payload).encode())
    assert outgoing[0]["direct_owner_command"] and outgoing[0]["command_id"]
    assert all(outgoing[0]["payload"][key] == payload[key] for key in ("agent_id", "transcript_path", "source"))


def test_shared_exact_command_parser_rejects_private_and_variants():
    from scope_recall.core.private import parse_recording_command

    assert parse_recording_command("  scope-recall pause\n") == "pause"
    assert parse_recording_command("scope-recall resume") == "resume"
    for text in ("scope-recall resume<private>TEST</private>", "scope-recall  resume", "SCOPE-RECALL resume"):
        assert parse_recording_command(text) is None


def test_server_rechecks_source_before_command(store, tmp_path):
    from datetime import UTC, datetime

    from scope_recall.adapters.clients import remote_server

    _root, homes = store
    home = homes["codex"]
    config = load_shared_client(home, "codex")
    core = MemoryCore(CoreConfig(config.to_binding()))
    ctx = TrustedContext(config.to_binding(), "TEST-owner", config.scope_ids, "host_generated")
    owner = MemoryManagement(core, InstallationMaintenance(ctx))
    session = "TEST-server-subagent"
    stored = stored_session_id(config, session)
    owner.private_on(target="session", session=stored)
    body = {
        "payload": {
            "hook_event_name": "UserPromptSubmit",
            "session_id": session,
            "prompt": "scope-recall resume",
            "transcript_path": "/TEST/subagents/agent.jsonl",
            "cwd": str(tmp_path),
        },
        "direct_owner_command": True,
        "fresh_observation": True,
        "command_id": "TEST-forged",
        "observed_at": datetime.now(UTC).isoformat(),
    }
    server = remote_server.RemoteServerConfig(home, "workbuddy", "127.0.0.1", 1, "0" * 64)
    with pytest.raises(remote_server.RemoteServerError):
        remote_server.handle_request(server, body)
    assert owner.private_status(session=stored)["mode"] == "full"


def test_command_fence_rolls_back_with_interval(store, tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from scope_recall.adapters.clients import remote_server
    from scope_recall.core import private

    _root, homes = store
    home = homes["codex"]
    config = load_shared_client(home, "codex")
    core = MemoryCore(CoreConfig(config.to_binding()))
    ctx = TrustedContext(config.to_binding(), "TEST-owner", config.scope_ids, "host_generated")
    owner = MemoryManagement(core, InstallationMaintenance(ctx))
    session = "TEST-atomic-command"
    stored = stored_session_id(config, session)
    server = remote_server.RemoteServerConfig(home, "codex", "127.0.0.1", 1, "0" * 64)
    now = datetime.now(UTC)

    def body(command, ident, seconds):
        return {
            "payload": {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session,
                "turn_id": ident,
                "prompt": command,
                "cwd": str(tmp_path),
            },
            "direct_owner_command": True,
            "fresh_observation": True,
            "command_id": ident,
            "observed_at": (now + timedelta(seconds=seconds)).isoformat(),
        }

    remote_server.handle_request(server, body("scope-recall resume", "TEST-first", -2))
    original = private._open_in_transaction

    def fail_after_open(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("TEST interrupted action")

    monkeypatch.setattr(private, "_open_in_transaction", fail_after_open)
    with pytest.raises(RuntimeError, match="TEST interrupted action"):
        remote_server.handle_request(server, body("scope-recall pause", "TEST-next", -1))
    assert owner.private_status(session=stored)["mode"] == "none"
    with core.storage.read(ctx) as tx:
        assert (
            tx._check()
            .execute("SELECT command_id FROM private_command_fences WHERE session_id=?", (stored,))
            .fetchone()[0]
            == "TEST-first"
        )


def test_backfill_keeps_visible_text_when_private_removal_resembles_command():
    from scope_recall.adapters.clients.record_reader import _without_command_records
    from scope_recall.adapters.clients.transcript import Said

    line = Said("TEST-line", "user", "scope-recall resume", "2026-09-27T06:00:00Z", private_spans_removed=1)
    assert _without_command_records([(1, line)]) == [(1, line)]


def test_filtered_remote_message_never_resumes_paused_session(served, tmp_path):
    root, homes, ports = served
    client = _client(tmp_path, "codex", ports["codex"])
    config = load_shared_client(homes["codex"], "codex")
    binding = config.to_binding()
    core = MemoryCore(CoreConfig(binding))
    owner = MemoryManagement(
        core, InstallationMaintenance(TrustedContext(binding, "TEST-owner", binding.scope_ids, "host_generated"))
    )
    session = "TEST-filtered-not-command"
    stored = stored_session_id(config, session)
    owner.private_on(target="session", session=stored)
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": session,
        "turn_id": "TEST-raw",
        "cwd": str(tmp_path),
        "prompt": "scope-recall resume<private>TEST hidden</private>",
    }
    _hook(client, payload)
    assert owner.private_status(session=stored)["mode"] == "full"
    assert b"TEST hidden" not in (root / "memory.sqlite3").read_bytes()


def test_remote_command_fences_are_per_entry_and_session(served, tmp_path):
    import sqlite3

    root, _homes, ports = served
    for host in ("codex", "claude-code"):
        client = _client(tmp_path, host, ports[host])
        for session in ("TEST-session-a", "TEST-session-b"):
            payload = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session,
                "turn_id": "TEST-turn",
                "prompt_id": "TEST-turn",
                "cwd": str(tmp_path),
                "prompt": "scope-recall pause",
            }
            assert _hook(client, payload)["decision"] == "block"
    with sqlite3.connect(root / "memory.sqlite3") as db:
        rows = db.execute("SELECT entry_id,session_id FROM private_command_fences").fetchall()
    assert len(rows) == 4 and len({row[0] for row in rows}) == 2


def test_remote_command_does_not_consume_pending_compaction(monkeypatch, tmp_path):
    def forbidden(*_args, **_kwargs):
        pytest.fail("command touched pending compaction")

    monkeypatch.setattr(remote_client, "_pending_compaction", forbidden)
    monkeypatch.setattr(
        remote_client,
        "_ack_compaction",
        lambda _config, session, compact, _answer: forbidden() if session is not None or compact is not None else None,
    )
    outgoing = []
    monkeypatch.setattr(remote_client, "_post", lambda _config, body, _timeout: outgoing.append(body) or {"result": {}})
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "TEST-owner", "prompt": "scope-recall pause"}
    remote_client.run_hook({"host": "codex", "state_dir": tmp_path}, json.dumps(payload).encode())
    assert outgoing[0]["direct_owner_command"]
    assert "compaction_seq" not in outgoing[0]


def test_remote_command_preserves_compaction_for_next_ordinary_prompt(monkeypatch, tmp_path):
    from scope_recall.adapters.clients.session_marks import compaction_marker, mark_compaction

    config = _client(tmp_path, "codex", 12345)
    session = "TEST-command-compaction"
    mark_compaction(config, session, "2026-09-01T00:00:00Z")
    marker = compaction_marker(config, session)
    monkeypatch.setattr(remote_client, "_post", lambda *_args: {"result": {}})
    command = {"hook_event_name": "UserPromptSubmit", "session_id": session, "prompt": "scope-recall pause"}
    remote_client.run_hook(config, json.dumps(command).encode())
    assert compaction_marker(config, session) == marker
    sent = []

    def send(_config, body, _until):
        sent.append(body)
        return {"result": {}, "compaction_ack": body["compaction_seq"]}

    monkeypatch.setattr(remote_client, "_send_checked", send)
    remote_client.run_hook(config, json.dumps({**command, "prompt": "TEST ordinary"}).encode())
    assert sent[0]["compaction_seq"] == marker["ordinal"]
    assert sent[0]["compaction_epoch"] == marker["epoch"]
    assert compaction_marker(config, session) is None


def test_paused_entry_owner_commands_and_private_mode_are_independent(served, tmp_path):
    root, homes, ports = served
    client = _client(tmp_path, "codex", ports["codex"])
    config = load_shared_client(homes["codex"], "codex")
    binding = config.to_binding()
    core = MemoryCore(CoreConfig(binding))
    context = TrustedContext(binding, "TEST-owner", binding.scope_ids, "host_generated")
    owner = MemoryManagement(core, InstallationMaintenance(context))
    owner.set_entry_state(config.entry_id, "paused", actor="TEST-owner")
    session = "TEST-paused-owner-command"
    payload = dict(
        hook_event_name="UserPromptSubmit",
        session_id=session,
        turn_id="TEST-command",
        prompt_id="TEST-command",
        cwd=str(tmp_path),
        prompt="scope-recall pause",
    )
    response = _hook(client, payload)
    assert response["decision"] == "block"
    assert owner.private_status(session=stored_session_id(config, session))["mode"] == "full"
    assert owner.entry(config.entry_id)["state"] == "paused"
    assert all("scope-recall pause" not in row[2] for row in _rows(root, config.entry_id))
    resumed = _hook(
        client, {**payload, "turn_id": "TEST-resume", "prompt_id": "TEST-resume", "prompt": "scope-recall resume"}
    )
    assert resumed["decision"] == "block"
    assert owner.private_status(session=stored_session_id(config, session))["mode"] == "none"
    assert owner.entry(config.entry_id)["state"] == "paused"
