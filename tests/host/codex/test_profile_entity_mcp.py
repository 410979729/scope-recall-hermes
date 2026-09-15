"""In-process Codex MCP registration and Core parity for profile/entity."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from scope_recall.adapters.codex import install_codex_scope_recall
from scope_recall.adapters.codex.identity import resolve_runtime_audience, trusted_context
from scope_recall.contracts import SourceEvent


def _assert_candidate_mcp_server_import() -> None:
    from scope_recall.adapters.codex import mcp_server as mcp_server_mod

    imported = Path(mcp_server_mod.__file__).resolve()
    expected = Path(__file__).resolve().parents[3] / "adapters" / "codex" / "mcp_server.py"
    assert imported == expected, (imported, expected)


def test_mcp_profile_entity_registration_parity_and_forbid(tmp_path: Path) -> None:
    _assert_candidate_mcp_server_import()
    from scope_recall.adapters.codex.mcp_server import build_server

    project = tmp_path / "project"
    project.mkdir()
    config, core = install_codex_scope_recall(tmp_path / "install", project_root=project)
    adapter = build_server(config, workspace=project, core=core)
    names = []
    for name in ("recall", "inspect", "profile", "entity", "propose_memory", "revise", "forget", "status", "trace"):
        tool = adapter.server._tool_manager.get_tool(name)
        assert tool is not None, name
        names.append(name)
        extra = tool.fn_metadata.arg_model.model_config.get("extra")
        assert extra == "forbid", (name, extra)
        assert tool.parameters.get("additionalProperties") is False
    assert names == ["recall", "inspect", "profile", "entity", "propose_memory", "revise", "forget", "status", "trace"]

    profile_tool = adapter.server._tool_manager.get_tool("profile")
    entity_tool = adapter.server._tool_manager.get_tool("entity")
    for tool in (profile_tool, entity_tool):
        annotations = getattr(tool, "annotations", None)
        hint = getattr(annotations, "read_only_hint", None)
        if hint is None and annotations is not None:
            hint = getattr(annotations, "readOnlyHint", None)
        assert hint is True, annotations
        assert "read-only" in tool.description.lower()
        assert "utf-8" in tool.description.lower()

    audience = resolve_runtime_audience(config, str(project))
    thread_id = str(uuid4())
    human = trusted_context(config, audience, session_id=thread_id, actor_origin="human_direct")
    event: SourceEvent = {
        "protocol_version": "1.1", "source_event_key": "TEST-mcp-profile", "source_revision": 1,
        "origin": "human_direct", "role": "user", "content": "饮品 喜欢 茶。",
        "occurred_at": None, "recorded_at": core.clock.utc_now(), "time_precision": "unknown",
        "capture_state": "complete", "evidence_refs": [],
        "source_context": {"platform": "cli", "chat_type": "workspace"},
    }
    source_ref = core.record_event(human, event, scope_id=audience.capture_scope_id).event_refs[0].ref
    claim = core.accept_claim_proposals(human, {
        "protocol_version": "1.1", "source_refs": [f"{source_ref}@1"],
        "claim_proposals": [{
            "kind": "preference", "subject": "饮品", "predicate": "喜欢", "value_text": "茶",
            "conditions": [], "statement_kind": "assertion", "valid_from": None, "valid_to": None,
            "evidence_spans": [{"source_ref": source_ref, "source_revision": 1, "quote": "饮品 喜欢 茶。"}],
        }],
        "resume_proposals": [], "reference_proposals": [],
    }, scope_id=audience.capture_scope_id)
    request = {
        "protocol_version": "1.1", "request_id": "mcp-parity", "subject": "饮品",
        "max_items": 16, "budget_tokens": 4096,
    }
    core_view = core.profile(human, request)
    fake = SimpleNamespace(request_context=SimpleNamespace(meta={"threadId": thread_id}))
    mcp_view = profile_tool.fn(fake, "1.1", "饮品", 16, 4096, "mcp-parity")
    assert mcp_view["result"] == core_view
    entity_request = {
        "protocol_version": "1.1", "request_id": "mcp-entity", "subject": "饮品",
        "action": "related", "direction": "outgoing", "max_items": 16, "budget_tokens": 4096,
    }
    core_entity = core.entity(human, entity_request)
    mcp_entity = entity_tool.fn(fake, "1.1", "饮品", "related", "outgoing", None, 16, 4096, "mcp-entity")
    assert mcp_entity["result"] == core_entity
    assert core_entity["statements"][0]["value_text"] == "茶"
    assert claim.items[0].ref == core_entity["statements"][0]["ref"]
    trace_tool = adapter.server._tool_manager.get_tool("trace")
    trace_request = dict(protocol_version="1.1", request_id="mcp-trace", subject="饮品")
    direct = core.trace(human, trace_request)
    via_mcp = trace_tool.fn(fake, "1.1", "饮品", request_id="mcp-trace")["result"]
    for key in ("paths", "gaps", "answerability", "facts_written", "memory_epoch"):
        assert direct[key] == via_mcp[key]
