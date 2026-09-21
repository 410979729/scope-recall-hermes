"""Bounded Codex MCP facade over the existing Core.

This module deliberately contains no storage implementation.  The process is
given a verified installation config and a verified project workspace once at
startup; clients cannot supply identity, paths, scopes, or SQL.  The MCP
session id is an adapter capability (there is no Codex conversation id on the
stdio boundary), so it is reported as such and is never treated as a user
identity.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal
import uuid

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import Field, StrictInt, StrictStr

from scope_recall.contracts import ContractError, Origin, SourceEvent, TrustedContext, validate_model_request
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.read_views import DEFAULT_BUDGET_TOKENS, DEFAULT_MAX_ITEMS
from scope_recall.core.trace import TRACE_GUIDANCE, fence_trace_epoch
from ..runtime_wiring import FORGET_GUIDANCE, READ_VIEW_BUDGET_GUIDANCE, RECALL_CONTEXT_GUIDANCE, REVISE_GUIDANCE
from ..tool_common import (
    FENCED_ENTITY,
    FENCED_PROFILE,
    FENCED_RECALL,
    MAX_CONTENT,
    MAX_REFS,
    PROTOCOL_VERSION,
    check_protocol,
    envelope,
    fence_epoch,
    request_id as bounded_request_id,
    revision_ref,
    strict_object,
)
from .config import CodexInstallationConfig
from .identity import CodexRuntimeAudience, resolve_runtime_audience, trusted_context
from .runtime_wiring import TrustedHostRuntime, attach_trusted_host_runtime


OUTPUT_ORIGIN = "memory_reinjection"
RECOMMENDED_EXPLICIT_BUDGET_TOKENS = 4096
BUDGET_RETRY_HINT = "retry_once_with_budget_tokens_4096"
_RECALL_BUDGET_GUIDANCE = (
    "budget_tokens caps the character-calibrated token estimate of the complete canonical "
    "packet, including packet and source metadata; it is neither UTF-8 bytes nor an exact tokenizer count. "
    "Recommended 4096 for explicit retrieval; omit the field to use that default. "
    "Explicit smaller values are honored and may clip every item. "
    "If gaps include budget_token_cap or budget_packet_cap, retry once with budget_tokens=4096."
)
_BUDGET_CAP_GAPS = ("budget_token_cap", "budget_packet_cap")

_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
#: The public tool surface in registration order.  Each entry names the
#: ``CodexMCPServer`` method of the same name: the SDK derives the advertised
#: argument/output schema titles from the function name, so the method name is
#: part of the frozen contract.
_TOOLS: tuple[tuple[str, str, ToolAnnotations], ...] = (
    ("recall", "Run the shared bounded read-only recall pipeline. Protocol version 1.1. " + _RECALL_BUDGET_GUIDANCE + " " + RECALL_CONTEXT_GUIDANCE, _READ_ONLY),
    ("inspect", "Inspect one visible, versioned object or source. Protocol version 1.1.", _READ_ONLY),
    ("profile", "Read-only categorized current-fact profile for one explicitly named subject. Uses only admitted consolidated claims; does not dump raw chat or USER.md/MEMORY.md. Protocol version 1.1. " + READ_VIEW_BUDGET_GUIDANCE + " " + RECALL_CONTEXT_GUIDANCE, _READ_ONLY),
    ("trace", TRACE_GUIDANCE, _READ_ONLY),
    ("entity", "Read-only exact one-hop entity view. action=probe returns current facts about the subject; action=related returns direct recorded statements. Incoming matches full scalar value_text only. No multi-hop traversal or inferred identity merge. Protocol version 1.1. " + READ_VIEW_BUDGET_GUIDANCE + " " + RECALL_CONTEXT_GUIDANCE, _READ_ONLY),
    ("propose_memory", "Record an assistant-visible candidate without promoting it to authority. Protocol version 1.1.", ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)),
    ("revise", REVISE_GUIDANCE + " Protocol version 1.1.",ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)),
    ("forget", FORGET_GUIDANCE + " Protocol version 1.1.",ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)),
    ("status", "Read bounded Core status and adapter capability gaps. Protocol version 1.1.", _READ_ONLY),
)


def _budget_retry_hint(packet: object) -> None:
    """Tell the caller to retry once at 4096 when a small explicit budget clipped."""
    if type(packet) is not dict:
        return
    gaps = packet.get("gaps")
    if type(gaps) is not list:
        return
    if not any(type(gap) is str and gap.startswith(_BUDGET_CAP_GAPS) for gap in gaps):
        return
    needs = packet.get("unmet_needs")
    if type(needs) is not list:
        packet["unmet_needs"] = [BUDGET_RETRY_HINT]
        return
    if BUDGET_RETRY_HINT not in needs:
        needs.append(BUDGET_RETRY_HINT)


class CodexMCPServer:
    """MCP tool registration and trusted runtime binding."""

    def __init__(
        self,
        config: CodexInstallationConfig,
        *,
        workspace: Path,
        core: MemoryCore | None = None,
        host_runtime: TrustedHostRuntime | None = None,
        trusted_runtime_config_path: str | Path | None = None,
    ) -> None:
        if not workspace.is_absolute():
            raise ValueError("workspace must be absolute")
        self.config = config
        self.workspace = workspace.resolve()
        self.audience: CodexRuntimeAudience = resolve_runtime_audience(config, str(self.workspace))
        if self.audience.capability_gaps:
            raise ValueError("workspace is not mapped to a trusted project root")
        self._host_runtime = host_runtime
        if host_runtime is None:
            # With no CLI override, attach_trusted_host_runtime checks only
            # the verified binding data directory's runtime-config.json.
            partition_context = trusted_context(
                config,
                self.audience,
                session_id=f"codex-mcp:{config.installation_id}",
                actor_origin="host_generated",
            )
            host_runtime = attach_trusted_host_runtime(
                config_path=trusted_runtime_config_path,
                expected_binding=config.to_binding(),
                session_id=f"codex-mcp:{config.installation_id}",
                allowed_scope_ids=self.audience.allowed_scope_ids,
                core=core,
                project_id=partition_context.project_id,
                branch_id=partition_context.branch_id,
            )
            self._host_runtime = host_runtime
        if host_runtime is not None:
            if core is not None and core is not host_runtime.core:
                raise ValueError("injected core mismatch")
            self.core = host_runtime.core
        elif core is not None:
            self.core = core
        else:
            self.core = MemoryCore(CoreConfig(config.to_binding()))
        self.session_id = f"mcp-server:{uuid.uuid4().hex}"
        self.context = trusted_context(
            config,
            self.audience,
            session_id=self.session_id,
            actor_origin="memory_reinjection",
        )
        self.server = MCPServer(
            name="scope-recall-codex",
            version=PROTOCOL_VERSION,
            description="Scoped read and explicitly authorized memory tools",
        )
        self._register_tools()

    def _register_tools(self) -> None:
        for name, description, hints in _TOOLS:
            self.server.tool(name=name, description=description, annotations=hints, structured_output=True)(getattr(self, name))
            # mcp 2.1 builds argument models from signatures with Pydantic's
            # default ``extra=ignore``.  Public tools must reject forged
            # identity, path, scope, and host-session fields, so tighten the
            # generated model and the advertised JSON Schema.
            tool = self.server._tool_manager.get_tool(name)
            tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
            tool.fn_metadata.arg_model.model_rebuild(force=True)
            tool.parameters["additionalProperties"] = False

    # -- trusted binding -------------------------------------------------

    def _context(self, origin: Origin = "memory_reinjection") -> TrustedContext:
        if origin == self.context.actor_origin:
            return self.context
        return trusted_context(self.config, self.audience, session_id=self.session_id, actor_origin=origin)

    @staticmethod
    def _thread_id(ctx: Context) -> str | None:
        """Codex's reserved MCP thread metadata, or None when absent or malformed."""
        meta = getattr(ctx.request_context, "meta", None) or {}
        value = meta.get("threadId") if isinstance(meta, dict) else None
        if not isinstance(value, str):
            return None
        try:
            return str(uuid.UUID(value))
        except ValueError:
            return None

    def _request_context(self, ctx: Context, *, mutation: bool = False, origin: Origin = "memory_reinjection") -> TrustedContext:
        """Bind one call to Codex's reserved MCP thread metadata.

        The model cannot provide this value as a tool argument.  A missing or
        malformed host binding remains usable for reads with an explicit gap,
        while mutations fail closed because Core's same-session human evidence
        check cannot be satisfied by the independent server session.
        """
        thread_id = self._thread_id(ctx)
        if thread_id is None:
            if mutation:
                raise ContractError("ACCESS_DENIED", "codex_thread_id")
            return self._context(origin)
        if self._host_runtime is not None:
            self._host_runtime.rebind_session(thread_id, self.audience.allowed_scope_ids)
        # Codex Hook stores the raw host session_id; MCP must use the same
        # value so Core current_human evidence can interoperate.
        return trusted_context(self.config, self.audience, session_id=thread_id, actor_origin=origin)

    def _capability_gaps(self, ctx: Context) -> tuple[str, ...]:
        return () if self._thread_id(ctx) is not None else ("mcp_session_is_not_codex_conversation_id",)

    # -- per-call plumbing -----------------------------------------------

    def _request(self, **fields: Any) -> tuple[dict[str, Any], str]:
        """Rebuild the typed call as a strict v1.1 JSON object and pin its request id.

        Unset optionals are dropped so Core sees what a raw JSON caller would
        have sent.  The SDK already typed every field; this re-applies the
        bounded size/depth contract that free-form values like ``new_value``
        still need, on the same code path Hermes uses.
        """
        body = strict_object({key: value for key, value in fields.items() if value is not None}, allowed=frozenset(fields))
        check_protocol(body)
        call_id = bounded_request_id(body, prefix="mcp-call")
        body["request_id"] = call_id
        return body, call_id

    def _reply(self, ctx: Context, call_id: str, result: object) -> dict[str, Any]:
        return envelope(call_id, result, origin=OUTPUT_ORIGIN, capability_gaps=self._capability_gaps(ctx))

    # -- tools (method name == MCP tool name, see _TOOLS) ----------------

    def recall(
        self,
        ctx: Context,
        protocol_version: Literal["1.1"],
        query: StrictStr,
        mode: Literal["auto", "current", "history", "as_of", "method"],
        max_items: StrictInt,
        budget_tokens: Annotated[StrictInt, Field(description=_RECALL_BUDGET_GUIDANCE)] = RECOMMENDED_EXPLICIT_BUDGET_TOKENS,
        as_of: StrictStr | None = None,
        focus_refs: list[StrictStr] | None = None,
        request_id: StrictStr | None = None,
    ) -> dict[str, Any]:
        body, call_id = self._request(
            protocol_version=protocol_version, request_id=request_id, query=query, mode=mode,
            max_items=max_items, budget_tokens=budget_tokens, as_of=as_of, focus_refs=focus_refs,
        )
        # validate_model_request remains the single DTO authority; no MCP
        # identity fields are merged into this request.
        context = self._request_context(ctx)
        validate_model_request("recall_request", body, context)
        # Explicit tool calls get the bounded deep-search ceiling.  Auto
        # mode is still clamped by the trusted CoreConfig budget.  A lookup
        # that finds nothing says so; the prompt hook keeps background.
        packet = self.core.recall_packet(context, body, deadline_seconds=5.0, background_without_evidence=False)
        packet = fence_epoch(packet, self.core.memory_epoch(context), FENCED_RECALL)
        _budget_retry_hint(packet)
        return self._reply(ctx, call_id, packet)

    def inspect(self, ctx: Context, protocol_version: Literal["1.1"], ref: StrictStr, limit: StrictInt = 24, request_id: StrictStr | None = None) -> dict[str, Any]:
        body, call_id = self._request(protocol_version=protocol_version, request_id=request_id, ref=ref, limit=limit)
        context = self._request_context(ctx)
        ref, revision = revision_ref(body["ref"])
        if not 1 <= body["limit"] <= 24:
            raise ContractError("INPUT_INVALID", "limit")
        inspected = self.core.inspect_object(context, ref, revision, limit=body["limit"])
        result = {"kind": inspected.kind, "ref": inspected.ref, "revision": inspected.revision, "value": inspected.value, "memory_epoch": inspected.memory_epoch}
        return self._reply(ctx, call_id, result)

    def profile(
        self,
        ctx: Context,
        protocol_version: Literal["1.1"],
        subject: StrictStr,
        max_items: Annotated[StrictInt, Field(ge=1, le=30)] = DEFAULT_MAX_ITEMS,
        budget_tokens: Annotated[StrictInt, Field(description=READ_VIEW_BUDGET_GUIDANCE)] = DEFAULT_BUDGET_TOKENS,
        request_id: StrictStr | None = None,
    ) -> dict[str, Any]:
        body, call_id = self._request(
            protocol_version=protocol_version, request_id=request_id, subject=subject,
            max_items=max_items, budget_tokens=budget_tokens,
        )
        context = self._request_context(ctx)
        view = self.core.profile(context, body)
        view = fence_epoch(view, self.core.status(context).memory_epoch, FENCED_PROFILE)
        return self._reply(ctx, call_id, view)

    def trace(
        self,
        ctx: Context,
        protocol_version: Literal["1.1"],
        subject: StrictStr,
        target: StrictStr | None = None,
        max_hops: StrictInt = 2,
        max_nodes: StrictInt = 24,
        max_paths: StrictInt = 12,
        direction: Literal["incoming", "outgoing", "both"] = "both",
        budget_bytes: StrictInt = 16384,
        request_id: StrictStr | None = None,
    ) -> dict[str, Any]:
        body, call_id = self._request(
            protocol_version=protocol_version, request_id=request_id, subject=subject, target=target,
            max_hops=max_hops, max_nodes=max_nodes, max_paths=max_paths, direction=direction, budget_bytes=budget_bytes,
        )
        context = self._request_context(ctx)
        view = self.core.trace(context, body)
        view = fence_trace_epoch(view, self.core.status(context).memory_epoch)
        return self._reply(ctx, call_id, view)

    def entity(
        self,
        ctx: Context,
        protocol_version: Literal["1.1"],
        subject: StrictStr,
        action: Literal["probe", "related"],
        direction: Literal["outgoing", "incoming", "both"] = "both",
        predicate: StrictStr | None = None,
        max_items: Annotated[StrictInt, Field(ge=1, le=30)] = DEFAULT_MAX_ITEMS,
        budget_tokens: Annotated[StrictInt, Field(description=READ_VIEW_BUDGET_GUIDANCE)] = DEFAULT_BUDGET_TOKENS,
        request_id: StrictStr | None = None,
    ) -> dict[str, Any]:
        body, call_id = self._request(
            protocol_version=protocol_version, request_id=request_id, subject=subject, action=action,
            direction=direction, predicate=predicate, max_items=max_items, budget_tokens=budget_tokens,
        )
        context = self._request_context(ctx)
        view = self.core.entity(context, body)
        view = fence_epoch(view, self.core.status(context).memory_epoch, FENCED_ENTITY)
        return self._reply(ctx, call_id, view)

    def propose_memory(
        self,
        ctx: Context,
        protocol_version: Literal["1.1"],
        content: StrictStr,
        evidence_refs: list[StrictStr] | None = None,
        reason: StrictStr | None = None,
        request_id: StrictStr | None = None,
    ) -> dict[str, Any]:
        body, call_id = self._request(
            protocol_version=protocol_version, request_id=request_id, content=content,
            evidence_refs=evidence_refs, reason=reason,
        )
        context = self._request_context(ctx, mutation=True, origin="assistant_visible")
        if not 1 <= len(body["content"]) <= MAX_CONTENT:
            raise ContractError("INPUT_INVALID", "content")
        refs = body.get("evidence_refs", [])
        if len(refs) > MAX_REFS or any(not 1 <= len(ref) <= 240 for ref in refs):
            raise ContractError("INPUT_INVALID", "evidence_refs")
        if len(body.get("reason") or "") > 1024:
            raise ContractError("INPUT_INVALID", "reason")
        if self.audience.capture_scope_id is None:
            raise ContractError("ACCESS_DENIED", "capture_scope")
        now = self.core.clock.utc_now()
        event: SourceEvent = {
            "protocol_version": PROTOCOL_VERSION,
            "source_event_key": f"codex-mcp:{self.session_id}:{call_id}",
            "source_revision": 1,
            "origin": "assistant_visible",
            "role": "assistant",
            "content": body["content"],
            "occurred_at": now,
            "recorded_at": now,
            "time_precision": "instant",
            "capture_state": "complete",
            "evidence_refs": list(refs),
        }
        receipt = self.core.record_event(context, event, scope_id=self.audience.capture_scope_id, remaining_seconds=1.0)
        return self._reply(ctx, call_id, {"candidate": True, "authority": "assistant_visible_only", "receipt": receipt})

    def revise(
        self,
        ctx: Context,
        protocol_version: Literal["1.1"],
        target_ref: StrictStr,
        expected_revision: StrictInt,
        new_value: Any,
        conditions: list[StrictStr],
        source_evidence_refs: list[StrictStr],
        valid_from: StrictStr | None,
        request_id: StrictStr | None = None,
    ) -> dict[str, Any]:
        body, call_id = self._request(
            protocol_version=protocol_version, request_id=request_id, target_ref=target_ref,
            expected_revision=expected_revision, new_value=new_value, conditions=conditions,
            source_evidence_refs=source_evidence_refs,
        )
        body["valid_from"] = valid_from  # required by the DTO; None is a legitimate value, not "unset"
        body.pop("request_id")
        # The stdio server has no attested Codex user turn.  Core still
        # requires its own human evidence and expected revision before
        # changing state.
        receipt = self.core.revise(self._request_context(ctx, mutation=True, origin="human_direct"), body, remaining_seconds=1.0)
        return self._reply(ctx, call_id, receipt)

    def forget(
        self,
        ctx: Context,
        protocol_version: Literal["1.1"],
        target_refs: list[StrictStr],
        mode: Literal["suppress", "delete"],
        expected_revisions: dict[StrictStr, StrictInt],
        reason: StrictStr | None = None,
        request_id: StrictStr | None = None,
    ) -> dict[str, Any]:
        body, call_id = self._request(
            protocol_version=protocol_version, request_id=request_id, target_refs=target_refs,
            mode=mode, expected_revisions=expected_revisions, reason=reason,
        )
        body.pop("request_id")
        receipt = self.core.forget(self._request_context(ctx, mutation=True, origin="human_direct"), body, remaining_seconds=1.0)
        return self._reply(ctx, call_id, receipt)

    def status(self, ctx: Context, protocol_version: Literal["1.1"] = "1.1", request_id: StrictStr | None = None) -> dict[str, Any]:
        _body, call_id = self._request(protocol_version=protocol_version, request_id=request_id)
        result = {
            "status": self.core.status(self._request_context(ctx)),
            "session": "independent_mcp_server",
            "workspace": str(self.workspace),
            "agent_id": self.config.agent_id,
            "installation_id": self.config.installation_id,
        }
        return self._reply(ctx, call_id, result)


def build_server(
    config: CodexInstallationConfig,
    *,
    workspace: Path,
    core: MemoryCore | None = None,
    host_runtime: TrustedHostRuntime | None = None,
    trusted_runtime_config_path: str | Path | None = None,
) -> CodexMCPServer:
    return CodexMCPServer(
        config,
        workspace=workspace,
        core=core,
        host_runtime=host_runtime,
        trusted_runtime_config_path=trusted_runtime_config_path,
    )
