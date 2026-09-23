"""Actual stdio MCP boundary tests; Hook tests remain in test_hooks.py."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from uuid import uuid4

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from scope_recall.adapters.codex import CodexHookHandler, install_codex_scope_recall
from scope_recall.adapters.codex.identity import resolve_runtime_audience, trusted_context
from scope_recall.contracts import SourceEvent


def test_mcp_stdio_exposes_public_tools_and_strict_boundary(tmp_path: Path) -> None:
    async def run() -> None:
        project = tmp_path / "项目 workspace"
        project.mkdir()
        config, _core = install_codex_scope_recall(tmp_path / "安装 data", project_root=project)
        env = dict(os.environ)
        repo = Path(__file__).parents[3]
        # The checkout first, the gate's own path kept: a child that drops it
        # resolves scope_recall through whatever the interpreter has installed.
        env["PYTHONPATH"] = os.pathsep.join(part for part in (str(repo), env.get("PYTHONPATH", "")) if part)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "scope_recall.adapters.codex.mcp_entry", "--config", str(config.config_path), "--workspace", str(project)],
            env=env,
            cwd=str(repo),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert [tool.name for tool in listed.tools] == list(_MCP_PUBLIC_TOOLS)
                status = await session.call_tool("status", {"protocol_version": "1.1", "request_id": "status-1"})
                assert not status.is_error
                assert status.structured_content["origin"] == "memory_reinjection"
                assert status.structured_content["result"]["session"] == "independent_mcp_server"
                recall = await session.call_tool("recall", {
                    "protocol_version": "1.1", "request_id": "recall-1", "query": "无命中查询",
                    "mode": "current", "max_items": 6, "budget_tokens": 1200,
                })
                assert not recall.is_error
                assert recall.structured_content["result"]["request_id"] == "recall-1"
                extra = await session.call_tool("status", {"protocol_version": "1.1", "request_id": "status-2", "agent_id": "forbidden"})
                assert extra.is_error

    asyncio.run(run())


def test_mcp_stdio_all_tools_and_host_thread_bound_mutations(tmp_path: Path) -> None:
    """Exercise real Core data, metadata binding, and the destructive path."""
    project = tmp_path / "project"
    project.mkdir()
    config, core = install_codex_scope_recall(tmp_path / "install", project_root=project)
    thread_id = str(uuid4())
    audience = resolve_runtime_audience(config, str(project))
    human = trusted_context(config, audience, session_id=thread_id, actor_origin="human_direct")

    handler = CodexHookHandler(config, core=core)

    def hook_capture(content: str, turn_id: str):
        handler.handle_payload({
            "hook_event_name": "UserPromptSubmit", "session_id": thread_id,
            "cwd": str(project), "turn_id": turn_id, "prompt": content,
        })
        with sqlite3.connect(config.data_directory / "memory.sqlite3") as db:
            row = db.execute("SELECT event_id FROM source_events WHERE session_id=? AND content=? ORDER BY rowid DESC LIMIT 1", (thread_id, content)).fetchone()
        # Spell the diagnostics out: the dataclass repr is long enough that
        # pytest elides ``capability_gaps``, which is the field that says why a
        # capture did not persist.
        assert row is not None, (
            f"capture did not persist: content={content!r} reason={handler.diagnostics.last_reason} "
            f"code={handler.diagnostics.capture_error_code} gaps={list(handler.diagnostics.capability_gaps)}"
        )
        return row[0]

    def capture(key: str, content: str, revision: int = 1):
        event: SourceEvent = {
            "protocol_version": "1.1", "source_event_key": key, "source_revision": revision,
            "origin": "human_direct", "role": "user", "content": content,
            "occurred_at": None, "recorded_at": core.clock.utc_now(), "time_precision": "unknown",
            "capture_state": "complete", "evidence_refs": [],
        }
        receipt = core.record_event(human, event, scope_id=audience.capture_scope_id)
        return receipt.event_refs[0].ref

    first = hook_capture("我喜欢茶。", "turn-1")
    historical = capture("human-history", "我喜欢咖啡。", revision=2)
    proposal = {
        "protocol_version": "1.1", "source_refs": [f"{first}@1"],
        "claim_proposals": [{
            "kind": "preference", "subject": "饮品", "predicate": "喜欢", "value_text": "茶",
            "conditions": [], "statement_kind": "assertion", "valid_from": None, "valid_to": None,
            "evidence_spans": [{"source_ref": first, "source_revision": 1, "quote": "我喜欢茶。"}],
        }],
        "resume_proposals": [], "reference_proposals": [],
    }
    claim_receipt = core.accept_claim_proposals(human, proposal, scope_id=audience.capture_scope_id)
    claim_ref = claim_receipt.items[0].ref
    correction = hook_capture(f"请把{claim_ref} 饮品改为咖啡。", "turn-2")

    async def run() -> None:
        env = dict(os.environ)
        repo = Path(__file__).parents[3]
        # The checkout first, the gate's own path kept: a child that drops it
        # resolves scope_recall through whatever the interpreter has installed.
        env["PYTHONPATH"] = os.pathsep.join(part for part in (str(repo), env.get("PYTHONPATH", "")) if part)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "scope_recall.adapters.codex.mcp_entry", "--config", str(config.config_path), "--workspace", str(project)],
            env=env,
            cwd=str(repo),
        )
        meta = {"threadId": thread_id}
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == set(_MCP_PUBLIC_TOOLS)
                source = await session.call_tool("inspect", {"protocol_version": "1.1", "ref": first}, meta=meta)
                assert not source.is_error, source
                assert source.structured_content["capability_gaps"] == []
                assert source.structured_content["result"]["value"]["event"]["content"] == "我喜欢茶。"
                historical_result = await session.call_tool("inspect", {"protocol_version": "1.1", "ref": f"{historical}@2"}, meta=meta)
                assert not historical_result.is_error, historical_result
                assert historical_result.structured_content["result"]["revision"] == 2
                current_historical = await session.call_tool("inspect", {"protocol_version": "1.1", "ref": historical}, meta=meta)
                assert not current_historical.is_error, current_historical
                assert current_historical.structured_content["result"]["revision"] == 2
                denied_candidate = await session.call_tool("propose_memory", {"protocol_version": "1.1", "content": "无宿主绑定的候选"})
                assert denied_candidate.is_error
                candidate = await session.call_tool("propose_memory", {"protocol_version": "1.1", "content": "候选，不是权威事实"}, meta=meta)
                assert not candidate.is_error, candidate
                assert candidate.structured_content["result"]["authority"] == "assistant_visible_only"
                revised = await session.call_tool("revise", {
                    "protocol_version": "1.1", "target_ref": claim_ref, "expected_revision": 1,
                    "new_value": "咖啡", "conditions": [], "source_evidence_refs": [f"{correction}@1"], "valid_from": None,
                }, meta=meta)
                assert not revised.is_error, revised
                # Core's authority rule requires the latest same-thread human
                # source to be the evidence for each destructive operation.
                hook_capture(f"删除 {claim_ref}。", "turn-3")
                forgotten = await session.call_tool("forget", {
                    "protocol_version": "1.1", "target_refs": [claim_ref], "mode": "delete",
                    "expected_revisions": {claim_ref: 2},
                }, meta=meta)
                assert not forgotten.is_error, forgotten
                denied = await session.call_tool("forget", {
                    "protocol_version": "1.1", "target_refs": [claim_ref], "mode": "delete",
                    "expected_revisions": {claim_ref: 2},
                })
                assert denied.is_error
                bad_meta = await session.call_tool("revise", {
                    "protocol_version": "1.1", "target_ref": claim_ref, "expected_revision": 2,
                    "new_value": "绿茶", "conditions": [], "source_evidence_refs": [f"{correction}@1"], "valid_from": None,
                }, meta={"threadId": "not-a-uuid"})
                assert bad_meta.is_error
                wrong_type = await session.call_tool("status", {"protocol_version": "1.1", "request_id": True})
                assert wrong_type.is_error

    asyncio.run(run())


def test_recall_epoch_race_scrubs_compiled_payload_surface(tmp_path: Path) -> None:
    """A deletion/epoch race cannot leave rendered text beside empty items."""
    project = tmp_path / "project"
    project.mkdir()
    config, real_core = install_codex_scope_recall(tmp_path / "install", project_root=project)

    from scope_recall.adapters.codex.mcp_server import build_server

    class RaceCore:
        class Clock:
            def utc_now(self): return "2026-09-06T12:00:00Z"
            def monotonic(self): return 1.0
        clock = real_core.clock

        def recall_packet(self, context, request, **kwargs):
            before = real_core.status(context).memory_epoch
            real_core.forget(human_context, {
                "protocol_version": "1.1", "target_refs": [claim_ref], "mode": "delete",
                "expected_revisions": {claim_ref: 1},
            })
            return {
                "protocol_version": "1.1", "request_id": request["request_id"], "status": "ok",
                "memory_epoch": before, "items": [{"content": "SECRET-DELETED", "ref": claim_ref, "revision": 1}],
                "gaps": [], "diagnostic_ref": None, "answerability": "supported",
                "coverage": "complete_for_query", "unmet_needs": [],
                "canonical_text": "SECRET-DELETED", "context": {"body": "SECRET-DELETED"},
            }

        def status(self, context):
            return real_core.status(context)

        def memory_epoch(self, context):
            return real_core.memory_epoch(context)

        def memory_retracted_since(self, context, epoch):
            return real_core.memory_retracted_since(context, epoch)

    adapter = build_server(config, workspace=project, core=RaceCore())
    human_context = trusted_context(config, resolve_runtime_audience(config, str(project)), session_id=adapter.context.session_id, actor_origin="human_direct")
    scope_id = config.audience_scopes["project"]
    source_event: SourceEvent = {
        "protocol_version": "1.1", "source_event_key": "race-human-1", "source_revision": 1,
        "origin": "human_direct", "role": "user", "content": "SECRET-DELETED state present。",
        "occurred_at": None, "recorded_at": real_core.clock.utc_now(), "time_precision": "unknown",
        "capture_state": "complete", "evidence_refs": [],
    }
    source_ref = real_core.record_event(human_context, source_event, scope_id=scope_id).event_refs[0].ref
    claim = real_core.accept_claim_proposals(human_context, {
        "protocol_version": "1.1", "source_refs": [f"{source_ref}@1"],
        "claim_proposals": [{"kind": "fact", "subject": "SECRET-DELETED", "predicate": "state", "value_text": "present", "conditions": [], "statement_kind": "assertion", "valid_from": None, "valid_to": None, "evidence_spans": [{"source_ref": source_ref, "source_revision": 1, "quote": "SECRET-DELETED state present。"}]}],
        "resume_proposals": [], "reference_proposals": [],
    }, scope_id=scope_id)
    claim_ref = claim.items[0].ref
    proof_event: SourceEvent = {
        "protocol_version": "1.1", "source_event_key": "race-human-2", "source_revision": 1,
        "origin": "human_direct", "role": "user", "content": f"删除 {claim_ref} SECRET-DELETED state present。",
        "occurred_at": None, "recorded_at": real_core.clock.utc_now(), "time_precision": "unknown",
        "capture_state": "complete", "evidence_refs": [],
    }
    real_core.record_event(human_context, proof_event, scope_id=scope_id)
    tool = adapter.server._tool_manager.get_tool("recall")
    fake_context = SimpleNamespace(request_context=SimpleNamespace(meta={}))
    result = tool.fn(fake_context, "1.1", "race-1", "current", 6, 1200)
    encoded = json.dumps(result, ensure_ascii=False)
    assert "SECRET-DELETED" not in encoded
    assert result["result"]["status"] == "unavailable"
    assert result["result"]["items"] == []


def test_mcp_recall_without_evidence_is_no_match_while_prompt_hook_keeps_background(installed) -> None:
    """An explicit lookup that finds nothing says so; automatic prompt recall is unchanged."""
    from scope_recall.adapters.codex.mcp_server import build_server

    config, core, clock, project = installed
    audience = resolve_runtime_audience(config, str(project))
    human = trusted_context(config, audience, session_id=str(uuid4()), actor_origin="human_direct")
    text = "TEST-project 表达偏好 简洁。"
    event: SourceEvent = {
        "protocol_version": "1.1", "source_event_key": "TEST-preference", "source_revision": 1,
        "origin": "human_direct", "role": "user", "content": text,
        "occurred_at": None, "recorded_at": clock.utc_now(), "time_precision": "unknown",
        "capture_state": "complete", "evidence_refs": [],
    }
    source = core.record_event(human, event, scope_id=audience.capture_scope_id).event_refs[0]
    claim_ref = core.accept_claim_proposals(human, {
        "protocol_version": "1.1", "source_refs": [f"{source.ref}@{source.revision}"],
        "claim_proposals": [{
            "kind": "preference", "subject": "TEST-project", "predicate": "表达偏好", "value_text": "简洁",
            "conditions": [], "statement_kind": "assertion", "valid_from": None, "valid_to": None,
            "evidence_spans": [{"source_ref": source.ref, "source_revision": source.revision, "quote": text}],
        }],
        "resume_proposals": [], "reference_proposals": [],
    }, scope_id=audience.capture_scope_id).items[0].ref
    query = "紫色海豚量子温泉"

    ambient = CodexHookHandler(config, core=core, clock=clock).handle_payload({
        "hook_event_name": "UserPromptSubmit", "session_id": "TEST-session-1", "turn_id": "TEST-turn-1",
        "cwd": str(project), "prompt": query,
    })
    assert claim_ref in ambient["hookSpecificOutput"]["additionalContext"]

    tool = build_server(config, workspace=project, core=core).server._tool_manager.get_tool("recall")
    fake_context = SimpleNamespace(request_context=SimpleNamespace(meta={}))
    explicit = tool.fn(fake_context, protocol_version="1.1", query=query, mode="auto", max_items=6)["result"]
    assert (explicit["status"], explicit["items"], explicit["answerability"]) == ("no_match", [], "unknown")


def test_mcp_inspect_resolves_old_episode_by_explicit_ref(tmp_path: Path) -> None:
    """An early episode remains addressable after more than 200 later events."""
    project = tmp_path / "project"
    project.mkdir()
    config, core = install_codex_scope_recall(tmp_path / "install", project_root=project)
    audience = resolve_runtime_audience(config, str(project))
    thread_id = str(uuid4())

    def event(index: int) -> SourceEvent:
        return {
            "protocol_version": "1.1", "source_event_key": f"episode-{index}", "source_revision": 1,
            "origin": "human_direct", "role": "user", "content": f"episode event {index}",
            "occurred_at": None, "recorded_at": core.clock.utc_now(), "time_precision": "unknown",
            "capture_state": "complete", "evidence_refs": [],
        }

    # A fresh session creates a fresh episode.  This makes the target truly
    # fall outside the bounded list instead of merely starting a second
    # segment in one episode after 200 events.
    for index in range(206):
        human = trusted_context(config, audience, session_id=str(uuid4()), actor_origin="human_direct")
        core.record_event(human, event(index), scope_id=audience.capture_scope_id)
    listing_context = trusted_context(config, audience, session_id=str(uuid4()), actor_origin="human_direct")
    first_page = core.episodes(listing_context, limit=200)
    first_page_refs = {item.ref for item in first_page}
    with sqlite3.connect(config.data_directory / "memory.sqlite3") as db:
        all_episode_refs = [row[0] for row in db.execute("SELECT episode_id FROM episodes ORDER BY episode_id")]
    assert len(all_episode_refs) > 200
    old_ref = next(ref for ref in all_episode_refs if ref not in first_page_refs)
    assert old_ref not in first_page_refs

    async def run() -> None:
        env = dict(os.environ)
        repo = Path(__file__).parents[3]
        # The checkout first, the gate's own path kept: a child that drops it
        # resolves scope_recall through whatever the interpreter has installed.
        env["PYTHONPATH"] = os.pathsep.join(part for part in (str(repo), env.get("PYTHONPATH", "")) if part)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "scope_recall.adapters.codex.mcp_entry", "--config", str(config.config_path), "--workspace", str(project)],
            env=env,
            cwd=str(repo),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                inspected = await session.call_tool("inspect", {"protocol_version": "1.1", "ref": old_ref}, meta={"threadId": thread_id})
                assert not inspected.is_error, inspected
                assert inspected.structured_content["result"]["kind"] == "episode"
                assert inspected.structured_content["result"]["ref"] == old_ref

    asyncio.run(run())


#: Registration order in ``adapters/codex/mcp_server.py``; ``trace`` registers
#: between ``profile`` and ``entity`` and is hardened with the rest at the
#: ``extra="forbid"`` pass, so it belongs to the frozen public surface.
_MCP_PUBLIC_TOOLS = ("recall", "inspect", "profile", "trace", "entity", "propose_memory", "revise", "forget", "status")


def _assert_candidate_mcp_server_import() -> None:
    from scope_recall.adapters.codex import mcp_server as mcp_server_mod

    imported = Path(mcp_server_mod.__file__).resolve()
    expected = Path(__file__).resolve().parents[3] / "adapters" / "codex" / "mcp_server.py"
    assert imported == expected, (imported, expected)


def _tool_input_schema(tool: object) -> dict:
    schema = getattr(tool, "inputSchema", None)
    if schema is None:
        schema = getattr(tool, "input_schema", None)
    if hasattr(schema, "model_dump"):
        schema = schema.model_dump(by_alias=True, exclude_none=False)
    assert isinstance(schema, dict), tool
    return schema


def _advertised_protocol_values(field: object) -> set[object]:
    if not isinstance(field, dict):
        return set()
    values: set[object] = set()
    if "const" in field:
        values.add(field["const"])
    enum = field.get("enum")
    if isinstance(enum, list):
        values.update(enum)
    return values


def _assert_unique_discoverable_protocol(schema: dict, *, name: str) -> None:
    from scope_recall.adapters.codex.mcp_server import PROTOCOL_VERSION

    props = schema.get("properties")
    assert isinstance(props, dict), (name, schema)
    field = props.get("protocol_version")
    advertised = _advertised_protocol_values(field)
    assert advertised == {PROTOCOL_VERSION}, (name, field)
    required = schema.get("required") or []
    if name == "status":
        assert isinstance(field, dict) and field.get("default") == PROTOCOL_VERSION
        assert "protocol_version" not in required
    else:
        assert "protocol_version" in required
    assert schema.get("additionalProperties") is False


def test_mcp_public_schema_advertises_only_protocol_1_1(tmp_path: Path) -> None:
    """Host-visible schema must publish the only legal protocol, not a free string."""
    _assert_candidate_mcp_server_import()
    from scope_recall.adapters.codex.mcp_server import build_server

    project = tmp_path / "project"
    project.mkdir()
    config, core = install_codex_scope_recall(tmp_path / "install", project_root=project)
    adapter = build_server(config, workspace=project, core=core)
    for name in _MCP_PUBLIC_TOOLS:
        tool = adapter.server._tool_manager.get_tool(name)
        assert tool is not None, name
        _assert_unique_discoverable_protocol(tool.parameters, name=name)

    async def run() -> None:
        env = dict(os.environ)
        repo = Path(__file__).parents[3]
        # The checkout first, the gate's own path kept: a child that drops it
        # resolves scope_recall through whatever the interpreter has installed.
        env["PYTHONPATH"] = os.pathsep.join(part for part in (str(repo), env.get("PYTHONPATH", "")) if part)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "scope_recall.adapters.codex.mcp_entry", "--config", str(config.config_path), "--workspace", str(project)],
            env=env,
            cwd=str(repo),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert [tool.name for tool in listed.tools] == list(_MCP_PUBLIC_TOOLS)
                for tool in listed.tools:
                    _assert_unique_discoverable_protocol(_tool_input_schema(tool), name=tool.name)

    asyncio.run(run())


_BUDGET_SCHEMA_MARKERS = (
    "utf-8",
    "byte",
    "metadata",
    "4096",
    "budget_token_cap",
    "budget_packet_cap",
)


def _assert_recall_budget_discoverable(schema: dict, *, description: str) -> None:
    props = schema.get("properties")
    assert isinstance(props, dict), schema
    field = props.get("budget_tokens")
    assert isinstance(field, dict), field
    assert field.get("default") == 4096, field
    assert field.get("type") == "integer", field
    advertised = " ".join(
        str(part) for part in (description, field.get("description")) if part
    ).lower()
    for marker in _BUDGET_SCHEMA_MARKERS:
        assert marker in advertised, (marker, advertised)
    required = schema.get("required") or []
    assert "budget_tokens" not in required
    assert "max_items" in required
    assert "query" in required
    assert "mode" in required
    _assert_unique_discoverable_protocol(schema, name="recall")


def test_mcp_recall_budget_schema_default_and_description(tmp_path: Path) -> None:
    """list_tools must publish UTF-8 byte units, metadata cost, and default 4096."""
    _assert_candidate_mcp_server_import()
    from scope_recall.adapters.codex.mcp_server import build_server

    project = tmp_path / "project"
    project.mkdir()
    config, core = install_codex_scope_recall(tmp_path / "install", project_root=project)
    adapter = build_server(config, workspace=project, core=core)
    registered = adapter.server._tool_manager.get_tool("recall")
    assert registered is not None
    _assert_recall_budget_discoverable(registered.parameters, description=registered.description)

    async def run() -> None:
        env = dict(os.environ)
        repo = Path(__file__).parents[3]
        # The checkout first, the gate's own path kept: a child that drops it
        # resolves scope_recall through whatever the interpreter has installed.
        env["PYTHONPATH"] = os.pathsep.join(part for part in (str(repo), env.get("PYTHONPATH", "")) if part)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "scope_recall.adapters.codex.mcp_entry", "--config", str(config.config_path), "--workspace", str(project)],
            env=env,
            cwd=str(repo),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                recall = next(tool for tool in listed.tools if tool.name == "recall")
                _assert_recall_budget_discoverable(_tool_input_schema(recall), description=recall.description)

    asyncio.run(run())


def test_mcp_recall_budget_default_tiny_clip_and_invalid_types(tmp_path: Path) -> None:
    """Omitted budget uses 4096; explicit 768 still clips; bool/string stay rejected."""
    _assert_candidate_mcp_server_import()
    from scope_recall.adapters.codex.mcp_server import BUDGET_RETRY_HINT, build_server

    project = tmp_path / "project"
    project.mkdir()
    config, core = install_codex_scope_recall(tmp_path / "install", project_root=project)
    audience = resolve_runtime_audience(config, str(project))
    human = trusted_context(config, audience, session_id=str(uuid4()), actor_origin="human_direct")
    fat = "TEST-BUDGET " + ("房间号启用窗口核对令牌封存舱" * 8)
    # Three, so a clipped packet is still visibly shorter than a full one now
    # that an episode with no summary no longer takes a slot of its own.
    for index, suffix in enumerate(("ALPHA", "BETA", "GAMMA"), start=1):
        event: SourceEvent = {
            "protocol_version": "1.1", "source_event_key": f"TEST-budget-{index}", "source_revision": 1,
            "origin": "human_direct", "role": "user", "content": f"{fat} {suffix}",
            "occurred_at": None, "recorded_at": core.clock.utc_now(), "time_precision": "unknown",
            "capture_state": "complete", "evidence_refs": [],
        }
        core.record_event(human, event, scope_id=audience.capture_scope_id)

    captured: list[dict] = []

    class CaptureCore:
        def recall_packet(self, context, request, **kwargs):
            captured.append(dict(request))
            return core.recall_packet(context, request, **kwargs)

        def status(self, context):
            return core.status(context)

        def memory_epoch(self, context):
            return core.memory_epoch(context)

        def memory_retracted_since(self, context, epoch):
            return core.memory_retracted_since(context, epoch)

    adapter = build_server(config, workspace=project, core=CaptureCore())
    tool = adapter.server._tool_manager.get_tool("recall")
    fake_context = SimpleNamespace(request_context=SimpleNamespace(meta={}))
    defaulted = tool.fn(fake_context, protocol_version="1.1", query="TEST-BUDGET 房间号", mode="auto", max_items=6)
    assert captured[0]["budget_tokens"] == 4096
    assert captured[0]["max_items"] == 6
    assert captured[0]["mode"] == "auto"
    assert len(defaulted["result"]["items"]) >= 2, defaulted
    assert "budget_token_cap" not in defaulted["result"]["gaps"]
    assert "budget_packet_cap" not in defaulted["result"]["gaps"]
    assert BUDGET_RETRY_HINT not in defaulted["result"].get("unmet_needs", [])

    clipped = tool.fn(fake_context, "1.1", "TEST-BUDGET 房间号", "auto", 6, 768)
    assert captured[1]["budget_tokens"] == 768
    assert len(clipped["result"]["items"]) < len(defaulted["result"]["items"])
    assert {"budget_token_cap", "budget_packet_cap"} & set(clipped["result"]["gaps"])
    assert BUDGET_RETRY_HINT in clipped["result"]["unmet_needs"]

    async def run() -> None:
        env = dict(os.environ)
        repo = Path(__file__).parents[3]
        # The checkout first, the gate's own path kept: a child that drops it
        # resolves scope_recall through whatever the interpreter has installed.
        env["PYTHONPATH"] = os.pathsep.join(part for part in (str(repo), env.get("PYTHONPATH", "")) if part)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "scope_recall.adapters.codex.mcp_entry", "--config", str(config.config_path), "--workspace", str(project)],
            env=env,
            cwd=str(repo),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                omitted = await session.call_tool("recall", {
                    "protocol_version": "1.1", "query": "TEST-BUDGET 房间号",
                    "mode": "auto", "max_items": 6,
                })
                assert not omitted.is_error, omitted
                omitted_items = omitted.structured_content["result"]["items"]
                assert len(omitted_items) >= 2
                tiny = await session.call_tool("recall", {
                    "protocol_version": "1.1", "query": "TEST-BUDGET 房间号",
                    "mode": "auto", "max_items": 6, "budget_tokens": 768,
                })
                assert not tiny.is_error, tiny
                assert len(tiny.structured_content["result"]["items"]) < len(omitted_items)
                assert {"budget_token_cap", "budget_packet_cap"} & set(tiny.structured_content["result"]["gaps"])
                assert BUDGET_RETRY_HINT in tiny.structured_content["result"]["unmet_needs"]
                for value in (True, False, "4096", "768", 4096.0):
                    rejected = await session.call_tool("recall", {
                        "protocol_version": "1.1", "query": "TEST-BUDGET 房间号",
                        "mode": "auto", "max_items": 6, "budget_tokens": value,
                    })
                    assert rejected.is_error, value
                bad_mode = await session.call_tool("recall", {
                    "protocol_version": "1.1", "query": "TEST-BUDGET 房间号",
                    "mode": "bogus", "max_items": 6, "budget_tokens": 4096,
                })
                assert bad_mode.is_error

    asyncio.run(run())


def test_mcp_illegal_protocol_values_rejected_and_legal_strict_path_kept(tmp_path: Path) -> None:
    """Aliases and non-strings stay rejected; legal 1.1 and extra=forbid stay."""
    _assert_candidate_mcp_server_import()
    project = tmp_path / "project"
    project.mkdir()
    config, _core = install_codex_scope_recall(tmp_path / "install", project_root=project)

    async def run() -> None:
        env = dict(os.environ)
        repo = Path(__file__).parents[3]
        # The checkout first, the gate's own path kept: a child that drops it
        # resolves scope_recall through whatever the interpreter has installed.
        env["PYTHONPATH"] = os.pathsep.join(part for part in (str(repo), env.get("PYTHONPATH", "")) if part)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "scope_recall.adapters.codex.mcp_entry", "--config", str(config.config_path), "--workspace", str(project)],
            env=env,
            cwd=str(repo),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for value in ("1", "0.1", "1.0", 1, 1.1, True, False):
                    rejected = await session.call_tool("status", {"protocol_version": value})
                    assert rejected.is_error, value
                recall_alias = await session.call_tool("recall", {
                    "protocol_version": "1", "query": "无命中查询",
                    "mode": "current", "max_items": 6, "budget_tokens": 1200,
                })
                assert recall_alias.is_error
                defaulted = await session.call_tool("status", {})
                assert not defaulted.is_error
                legal = await session.call_tool("status", {"protocol_version": "1.1", "request_id": "status-legal"})
                assert not legal.is_error
                assert legal.structured_content["origin"] == "memory_reinjection"
                extra = await session.call_tool("status", {"protocol_version": "1.1", "request_id": "status-extra", "agent_id": "forbidden"})
                assert extra.is_error

    asyncio.run(run())
