"""Entry state through the four client hook adapters on synthetic shared stores."""

import sqlite3
from contextlib import closing

from scope_recall.adapters.clients import CodexHookHandler
from scope_recall.contracts import TrustedContext
from scope_recall.core.management import MemoryManagement
from scope_recall.core.restore import InstallationMaintenance

from tests.host.codex.test_merged_host_boundaries import client
from tests.host.codex.test_shared_client import store


def test_hook_entry_state_lifecycle(client, tmp_path, monkeypatch):
    root, home, host = client
    hook = CodexHookHandler.from_home(str(home), host)
    monkeypatch.setattr(hook, "ensure_runtime", lambda *args, **kwargs: None)
    binding = hook.core.storage.binding
    owner = MemoryManagement(
        hook.core, InstallationMaintenance(TrustedContext(binding, "TEST-owner", binding.scope_ids, "host_generated"))
    )

    def prompt(number):
        return dict(
            hook_event_name="UserPromptSubmit",
            session_id="TEST-session",
            prompt_id=f"TEST-{number}",
            generation_id=f"TEST-{number}",
            turn_id=f"TEST-{number}",
            cwd=str(tmp_path),
            prompt=f"TEST observatory palette amber {number}.",
        )

    def count():
        with closing(sqlite3.connect(root / "memory.sqlite3")) as db:
            return db.execute("SELECT count(*) FROM source_events WHERE entry_id='test-boundary'").fetchone()[0]

    try:
        hook.handle_payload(prompt(1))
        assert count() == 1
        owner.set_entry_state("test-boundary", "read-only", actor="TEST-owner")
        hook.handle_payload(prompt(2))
        assert count() == 1
        owner.set_entry_state("test-boundary", "paused", actor="TEST-owner")
        answer = hook.handle_payload(prompt(3))
        assert count() == 1
        assert "observatory palette amber 1" not in str(answer)
        owner.set_entry_state("test-boundary", "normal", actor="TEST-owner")
        hook.handle_payload(prompt(4))
        assert count() == 2
    finally:
        hook.close()


def test_pausing_one_shared_entry_does_not_block_another(store, client, tmp_path, monkeypatch):
    from tests.host.codex.test_merged_host_boundaries import _hermes

    root, homes, _client, _scope = store
    _root, home, host = client
    hook = CodexHookHandler.from_home(str(home), host)
    monkeypatch.setattr(hook, "ensure_runtime", lambda *args, **kwargs: None)
    provider = _hermes(homes["agent3"])
    binding = hook.core.storage.binding
    owner = MemoryManagement(
        hook.core, InstallationMaintenance(TrustedContext(binding, "TEST-owner", binding.scope_ids, "host_generated"))
    )
    try:
        owner.set_entry_state("test-boundary", "paused", actor="TEST-owner")
        hook.handle_payload(
            dict(
                hook_event_name="UserPromptSubmit",
                session_id="TEST-isolation",
                turn_id="TEST-1",
                cwd=str(tmp_path),
                prompt="TEST paused entry text",
            )
        )
        provider.observe_pre_llm(
            session_id="TEST-session-1", turn_id="TEST-isolation", user_message="TEST other entry text"
        )
        with closing(sqlite3.connect(root / "memory.sqlite3")) as db:
            counts = dict(db.execute("SELECT entry_id,count(*) FROM source_events GROUP BY entry_id"))
        assert counts.get("test-boundary", 0) == 0
        assert counts.get("agent3", 0) == 1
    finally:
        provider.shutdown()
        hook.close()


def test_mcp_query_obeys_entry_state(client, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4

    from scope_recall.adapters.clients.mcp_server import CodexMCPServer

    _root, home, host = client
    hook = CodexHookHandler.from_home(str(home), host)
    monkeypatch.setattr(hook, "ensure_runtime", lambda *args, **kwargs: None)
    binding = hook.core.storage.binding
    owner = MemoryManagement(
        hook.core, InstallationMaintenance(TrustedContext(binding, "TEST-owner", binding.scope_ids, "host_generated"))
    )
    server = CodexMCPServer(hook.config, workspace=tmp_path, core=hook.core)
    request = SimpleNamespace(request_context=SimpleNamespace(meta={"threadId": str(uuid4())}))
    try:
        hook.handle_payload(
            dict(
                hook_event_name="UserPromptSubmit",
                session_id="TEST-mcp",
                turn_id="TEST-1",
                prompt_id="TEST-1",
                generation_id="TEST-1",
                cwd=str(tmp_path),
                prompt="TEST mcp lantern turquoise",
            )
        )
        owner.set_entry_state("test-boundary", "read-only", actor="TEST-owner")
        read_only = server.recall(request, "1.1", "TEST mcp lantern turquoise", "current", 6)
        assert "entry_state:paused" not in str(read_only)
        assert "lantern turquoise" in str(read_only)
        owner.set_entry_state("test-boundary", "paused", actor="TEST-owner")
        paused = server.recall(request, "1.1", "TEST mcp lantern turquoise", "current", 6)
        assert "entry_state:paused" in str(paused)
        assert "lantern turquoise" not in str(paused)
    finally:
        if server._host_runtime is not None:
            server._host_runtime.close()
        hook.close()


def test_remote_entry_refuses_capture_and_reports_state(client, tmp_path, monkeypatch):
    from scope_recall.adapters.clients import remote_server

    root, home, host = client
    hook = CodexHookHandler.from_home(str(home), host)
    monkeypatch.setattr(hook, "ensure_runtime", lambda *args, **kwargs: None)
    binding = hook.core.storage.binding
    owner = MemoryManagement(
        hook.core, InstallationMaintenance(TrustedContext(binding, "TEST-owner", binding.scope_ids, "host_generated"))
    )
    try:
        owner.set_entry_state("test-boundary", "paused", actor="TEST-owner")
        config = remote_server.RemoteServerConfig(
            home=home, host=host, listen="127.0.0.1", port=1, token_sha256="0" * 64
        )
        reply = remote_server.handle_request(
            config,
            {
                "observed_at": "2026-09-06T12:00:00Z",
                "payload": dict(
                    hook_event_name="UserPromptSubmit",
                    session_id="TEST-remote",
                    turn_id="TEST-1",
                    prompt_id="TEST-1",
                    generation_id="TEST-1",
                    cwd=str(tmp_path),
                    prompt="TEST remote paused material",
                ),
            },
        )
        with closing(sqlite3.connect(root / "memory.sqlite3")) as db:
            assert db.execute("SELECT count(*) FROM source_events WHERE entry_id='test-boundary'").fetchone()[0] == 0
        assert "entry_state:paused" in str(reply)
        assert reply["retry"] is False
    finally:
        hook.close()


def test_mcp_tool_catalog_has_no_entry_state_mutation():
    from scope_recall.adapters.clients.mcp_server import _TOOLS

    assert {name for name, _description, _hints in _TOOLS}.isdisjoint({"entry-state", "entry_state", "set_entry_state"})


def test_shared_entry_list_reports_each_registered_grant(client):
    from types import SimpleNamespace

    from scope_recall.maintenance.memory import open_management

    root, _home, _host = client
    api = open_management(SimpleNamespace(root=str(root), host=None, instance_root=None))
    entries = {entry["id"]: entry for entry in api.entries()}
    selected = entries["test-boundary"]
    assert selected["read_scopes"] and selected["write_scopes"]
    assert set(selected["write_scopes"]) <= set(selected["read_scopes"])


def test_review_gate_in_each_hook_mcp_and_brief(client, tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from uuid import uuid4

    from scope_recall.adapters.clients.identity import resolve_runtime_audience, trusted_context
    from scope_recall.adapters.clients.mcp_server import CodexMCPServer

    from tests.contract.test_claims import draft

    _root, home, host = client
    hook = CodexHookHandler.from_home(str(home), host)
    monkeypatch.setattr(hook, "ensure_runtime", lambda *args, **kwargs: None)
    binding = hook.core.storage.binding
    owner_context = TrustedContext(binding, "TEST-owner", binding.scope_ids, "host_generated")
    owner = MemoryManagement(hook.core, InstallationMaintenance(owner_context))
    reader = trusted_context(hook.config, resolve_runtime_audience(hook.config, None), session_id="TEST-review")
    server = CodexMCPServer(hook.config, workspace=tmp_path, core=hook.core)
    try:
        for scope in binding.scope_ids:
            owner.set_review_policy(scope, True, expected_version=0)
        hook.handle_payload(
            dict(
                hook_event_name="UserPromptSubmit",
                session_id="TEST-review",
                turn_id="TEST-1",
                prompt_id="TEST-1",
                generation_id="TEST-1",
                cwd=str(tmp_path),
                prompt="TEST-project palette ultramarine.",
            )
        )
        with hook.core.storage.read(owner_context) as tx:
            row = (
                tx._check()
                .execute(
                    "SELECT event_id,source_revision,scope_id,project_id,branch_id FROM source_events "
                    "ORDER BY source_id DESC LIMIT 1"
                )
                .fetchone()
            )
        scoped = replace(reader, project_id=row["project_id"], branch_id=row["branch_id"])
        source = hook.core.source(scoped, row[0], row[1])
        pending = hook.core.records.accept_claim_proposals(
            scoped,
            dict(
                protocol_version="1.1",
                source_refs=[f"{source.ref}@{source.revision}"],
                claim_proposals=[
                    draft(source, "ultramarine", predicate="palette", kind="fact", statement_kind="assertion")
                ],
                resume_proposals=[],
                reference_proposals=[],
            ),
            scope_id=row["scope_id"],
            remaining_seconds=10,
        ).items[0]
        assert pending.state == "proposed"
        answer = hook.handle_payload(
            dict(
                hook_event_name="UserPromptSubmit",
                session_id="TEST-review-next",
                turn_id="TEST-2",
                prompt_id="TEST-2",
                generation_id="TEST-2",
                cwd=str(tmp_path),
                prompt="What is TEST-project palette?",
            )
        )
        request = SimpleNamespace(request_context=SimpleNamespace(meta={"threadId": str(uuid4())}))
        explicit = server.recall(request, "1.1", "TEST-project palette", "current", 6)
        assert pending.ref not in str(answer)
        assert pending.ref not in str(explicit)
        assert pending.ref not in str(hook.core.session_brief(scoped))
    finally:
        if server._host_runtime is not None:
            server._host_runtime.close()
        hook.close()
