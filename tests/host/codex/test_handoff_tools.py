"""Every host exposes the handoff operations through its existing tool surface."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from scope_recall.adapters.clients.config import load_shared_client
from scope_recall.adapters.clients.identity import trusted_context
from scope_recall.adapters.clients.mcp_server import build_server
from scope_recall.adapters.hermes.shared_entries import attach_shared_record, client_entry_record
from scope_recall.contracts import ContractError
from scope_recall.core import CoreConfig, MemoryCore

from tests.contract.test_claims import Clock
from tests.host.codex.test_shared_client import store
from tests.v11_support import source_event


def test_claude_code_exposes_handoff_tools(store):
    """A receiving entry can list, accept, write and cancel handoffs."""
    _root, _homes, client, _capture = store
    server = build_server(load_shared_client(client, "claude-code"), workspace=None)
    offered = {tool.name for tool in server.server._tool_manager.list_tools()}
    assert {"handoff_write", "handoff_list", "handoff_accept", "handoff_cancel"} <= offered


@pytest.mark.parametrize("host", ["codex", "claude-code", "workbuddy", "dsh"])
def test_each_client_exposes_handoffs(store, host):
    _root, _homes, client, _capture = store
    config = replace(load_shared_client(client, "claude-code"), host=host)
    server = build_server(config, workspace=None)
    offered = {tool.name for tool in server.server._tool_manager.list_tools()}
    assert {"handoff_write", "handoff_list", "handoff_accept", "handoff_cancel"} <= offered


def test_real_mcp_call_transfers_between_authenticated_entries(store, tmp_path):
    root, _homes, client, scope = store
    sender_home = tmp_path / "TEST-codex"
    attach_shared_record(
        root,
        client_entry_record(
            host="codex",
            home=sender_home,
            entry_id="codex",
            display_name="Codex",
            attached_at="2026-09-06T12:00:00Z",
            allowed_scope_ids=[scope],
            writable_scope_ids=[scope],
            capture_scope_id=scope,
        ),
        now="2026-09-06T12:00:00Z",
    )
    sender_config = load_shared_client(sender_home, "codex")
    receiver_config = load_shared_client(client, "claude-code")
    core = MemoryCore(CoreConfig(sender_config.to_binding()), clock=Clock())
    core.initialize()
    workspace = tmp_path / "TEST-project"
    workspace.mkdir()
    sender = build_server(sender_config, workspace=workspace, core=core)
    receiver = build_server(receiver_config, workspace=workspace, core=core)
    context = trusted_context(sender_config, sender.audience, session_id="TEST-old", project_root=str(workspace))
    saved = core.record_event(
        context,
        source_event(content="TEST adjust layout", source_event_key="TEST-mcp"),
        scope_id=scope,
        remaining_seconds=5,
    )
    ref = saved.event_refs[0].ref
    task = core.episodes(context)[0]

    async def run():
        args = dict(
            protocol_version="1.1",
            report=dict(
                task_ref=task.ref,
                progress="TEST ready",
                next_step="TEST check",
                changed_files=["src/layout.py"],
                uncommitted_files=["src/layout.py"],
                owner_decisions=[],
                evidence_refs=[ref + "@1"],
                session_label="TEST-untrusted",
            ),
        )
        result = await sender.server.call_tool("handoff_write", args)
        assert not result.is_error, result
        item = result.structured_content["result"]
        assert item["sender_session"] is None
        listed = await receiver.server.call_tool("handoff_list", dict(protocol_version="1.1"))
        assert listed.structured_content["result"]["items"][0]["ref"] == item["ref"]
        accepted = await receiver.server.call_tool(
            "handoff_accept", dict(protocol_version="1.1", ref=item["ref"], expected_revision=1)
        )
        assert accepted.structured_content["result"]["accepted_entry"] == "claude-code"
        with pytest.raises(ToolError):
            await receiver.server.call_tool(
                "handoff_accept",
                dict(protocol_version="1.1", ref=item["ref"], expected_revision=1, sender_entry="codex"),
            )

    asyncio.run(run())


def test_client_write_permissions_still_apply(store):
    _root, _homes, client, _capture = store
    config = load_shared_client(client, "claude-code")
    server = build_server(config, workspace=None)
    server.audience = replace(server.audience, writable_scope_ids=frozenset())
    with pytest.raises(ContractError, match="ACCESS_DENIED"):
        server.handoff_cancel(SimpleNamespace(request_context=SimpleNamespace(meta={})), "1.1", "TEST-ref", 1)


def test_mcp_session_provenance_uses_only_reserved_host_metadata(store, tmp_path):
    from scope_recall.adapters.host_profiles import HOSTS

    _root, _homes, client, scope = store
    config = replace(load_shared_client(client, "claude-code"), host="codex")
    core = MemoryCore(CoreConfig(config.to_binding()), clock=Clock())
    core.initialize()
    server = build_server(config, workspace=tmp_path, core=core)
    session = "11111111-1111-4111-8111-111111111111"
    context = trusted_context(config, server.audience, session_id=session, project_root=str(tmp_path))
    source = core.record_event(
        context,
        source_event(source_event_key="TEST-host-meta", content="TEST layout"),
        scope_id=scope,
        remaining_seconds=5,
    ).event_refs[0]
    task = core.episodes(context)[0]
    request = SimpleNamespace(request_context=SimpleNamespace(meta={HOSTS["codex"].thread_meta: session}))
    result = server.handoff_write(
        request,
        "1.1",
        dict(
            task_ref=task.ref,
            progress="TEST ready",
            evidence_refs=[source.ref + "@1"],
            session_label="TEST-forged-label",
        ),
    )
    assert result["result"]["sender_session"] == context.session_id
    assert result["result"]["session_label"] == "TEST-forged-label"
