"""Bounded Codex MCP facade over the existing Core.

This module deliberately contains no storage implementation.  The process is
given a verified installation config and a verified project workspace once at
startup; clients cannot supply identity, paths, scopes, or SQL.  The MCP
session id is an adapter capability (there is no Codex conversation id on the
stdio boundary), so it is reported as such and is never treated as a user
identity.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
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
from ..runtime_wiring import READ_VIEW_BUDGET_GUIDANCE, RECALL_CONTEXT_GUIDANCE
from .config import CodexInstallationConfig
from .identity import CodexRuntimeAudience, resolve_runtime_audience, trusted_context
from .runtime_wiring import TrustedHostRuntime, attach_trusted_host_runtime


PROTOCOL_VERSION = "1.1"
OUTPUT_ORIGIN = "memory_reinjection"
RECOMMENDED_EXPLICIT_BUDGET_TOKENS = 4096
BUDGET_RETRY_HINT = "retry_once_with_budget_tokens_4096"
_RECALL_BUDGET_GUIDANCE = (
    "budget_tokens is a conservative UTF-8 byte budget for the complete canonical "
    "packet, including packet and source metadata, not a tokenizer count. "
    "Recommended 4096 for explicit retrieval; omit the field to use that default. "
    "Explicit smaller values are honored and may clip every item. "
    "If gaps include budget_token_cap or budget_packet_cap, retry once with budget_tokens=4096."
)
_MAX_CONTENT = 8192
_MAX_REFS = 32
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_JSON_DEPTH = 32
_BUDGET_CAP_GAPS = ("budget_token_cap", "budget_packet_cap")


def _error(code: str, field: str = "payload") -> ContractError:
    return ContractError(code, field)


def _object(value: object, *, allowed: frozenset[str], required: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Make MCP's untyped JSON object strict before passing it to Core."""
    if type(value) is not dict:
        raise _error("INPUT_INVALID", "object")
    keys = frozenset(value)
    if not keys <= allowed:
        raise _error("INPUT_INVALID", "unknown_field")
    if not required <= keys:
        raise _error("INPUT_INVALID", "required_field")
    # The SDK has already decoded JSON, but this gives us the same bounded
    # JSON/depth semantics as the public contracts and removes custom objects.
    def walk(item: object, depth: int = 0) -> None:
        if depth > _MAX_JSON_DEPTH:
            raise _error("INPUT_INVALID", "nesting")
        if type(item) in (str, int, float, bool) or item is None:
            return
        if type(item) is list:
            for child in item:
                walk(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise _error("INPUT_INVALID", "json_key")
                walk(child, depth + 1)
            return
        raise _error("INPUT_INVALID", "json_value")

    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
            raise _error("INPUT_INVALID", "size")
        walk(value)
        decoded = json.loads(encoded)
    except ContractError:
        raise
    except (TypeError, ValueError, RecursionError):
        raise _error("INPUT_INVALID", "json_value") from None
    if type(decoded) is not dict:
        raise _error("INPUT_INVALID", "object")
    return decoded


def _protocol(payload: dict[str, Any]) -> None:
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise _error("INPUT_INVALID", "protocol_version")


def _request_id(payload: dict[str, Any]) -> str:
    value = payload.get("request_id")
    if value is None:
        return f"mcp-call:{uuid.uuid4().hex}"
    if type(value) is not str or not 1 <= len(value) <= 100:
        raise _error("INPUT_INVALID", "request_id")
    return value


def _json_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {str(k): _json_value(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _envelope(request_id: str, result: object, *, capability_gaps: tuple[str, ...] = ()) -> dict[str, Any]:
    converted = _json_value(result)
    if len(json.dumps(converted, ensure_ascii=False, allow_nan=False).encode("utf-8")) > _MAX_JSON_BYTES:
        raise _error("OUTPUT_LIMIT", "result")
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "origin": OUTPUT_ORIGIN,
        "capability_gaps": list(capability_gaps),
        "result": converted,
    }


def _scrub_no_content(value: object) -> object:
    """Remove every rendered/body surface from a raced stale packet."""
    surfaces = {"canonical_text", "context", "rendered", "additionalContext", "injection_text", "text", "body", "content"}
    if isinstance(value, dict):
        return {key: ("" if key in surfaces else _scrub_no_content(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_no_content(item) for item in value]
    return value


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


def _revision_ref(value: str) -> tuple[str, int | None]:
    if type(value) is not str or not 1 <= len(value) <= 240:
        raise _error("INPUT_INVALID", "ref")
    if "@" not in value:
        return value, None
    ref, raw = value.rsplit("@", 1)
    try:
        revision = int(raw)
    except ValueError:
        raise _error("INPUT_INVALID", "ref") from None
    if not ref or revision < 1 or raw != str(revision):
        raise _error("INPUT_INVALID", "ref")
    return ref, revision


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

    def _context(self, origin: Origin = "memory_reinjection") -> TrustedContext:
        if origin == self.context.actor_origin:
            return self.context
        return trusted_context(self.config, self.audience, session_id=self.session_id, actor_origin=origin)

    def _request_context(self, ctx: Context, *, mutation: bool = False, origin: Origin = "memory_reinjection") -> TrustedContext:
        """Bind one call to Codex's reserved MCP thread metadata.

        The model cannot provide this value as a tool argument.  A missing or
        malformed host binding remains usable for reads with an explicit gap,
        while mutations fail closed because Core's same-session human evidence
        check cannot be satisfied by the independent server session.
        """
        meta = getattr(ctx.request_context, "meta", None) or {}
        value = meta.get("threadId") if isinstance(meta, dict) else None
        if not isinstance(value, str):
            if mutation:
                raise _error("ACCESS_DENIED", "codex_thread_id")
            return self._context(origin)
        try:
            thread_id = str(uuid.UUID(value))
        except (ValueError, AttributeError, TypeError):
            if mutation:
                raise _error("ACCESS_DENIED", "codex_thread_id") from None
            return self._context(origin)
        if self._host_runtime is not None:
            self._host_runtime.rebind_session(thread_id, self.audience.allowed_scope_ids)
        return trusted_context(
            self.config,
            self.audience,
            # Codex Hook stores the raw host session_id; MCP must use the same
            # value so Core current_human evidence can interoperate.
            session_id=thread_id,
            actor_origin=origin,
        )

    @staticmethod
    def _capability_gaps(ctx: Context) -> tuple[str, ...]:
        meta = getattr(ctx.request_context, "meta", None) or {}
        value = meta.get("threadId") if isinstance(meta, dict) else None
        if isinstance(value, str):
            try:
                uuid.UUID(value)
                return ()
            except (ValueError, AttributeError, TypeError):
                pass
        return ("mcp_session_is_not_codex_conversation_id",)

    def _register_tools(self) -> None:
        @self.server.tool(name="recall", description="Run the shared bounded read-only recall pipeline. Protocol version 1.1. " + _RECALL_BUDGET_GUIDANCE + " " + RECALL_CONTEXT_GUIDANCE, annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False), structured_output=True)
        def recall(
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
            payload: dict[str, Any] = dict(protocol_version=protocol_version, query=query, mode=mode,
                           max_items=max_items, budget_tokens=budget_tokens)
            if request_id is not None:
                payload["request_id"] = request_id
            if as_of is not None:
                payload["as_of"] = as_of
            if focus_refs is not None:
                payload["focus_refs"] = focus_refs
            body = _object(
                payload,
                allowed=frozenset({"protocol_version", "request_id", "query", "mode", "as_of", "max_items", "budget_tokens", "focus_refs"}),
                required=frozenset({"protocol_version", "query", "mode", "max_items", "budget_tokens"}),
            )
            _protocol(body)
            call_id: str = _request_id(body)
            body["request_id"] = call_id
            # validate_model_request remains the single DTO authority; no MCP
            # identity fields are merged into this request.
            call_context = self._request_context(ctx)
            validate_model_request("recall_request", body, call_context)
            # Explicit tool calls get the bounded deep-search ceiling.  Auto
            # mode is still clamped by the trusted CoreConfig budget.
            packet = self.core.recall_packet(call_context, body, deadline_seconds=5.0)
            # A mutation can race the read-only compiler.  Never hand a stale
            # packet to a host: the caller can retry against the new epoch.
            current_epoch = self.core.memory_epoch(call_context)
            if packet.get("memory_epoch") is not None and packet["memory_epoch"] != current_epoch:
                scrubbed = _scrub_no_content(packet)
                packet = dict(scrubbed) if isinstance(scrubbed, dict) else {}
                packet.update(
                    status="unavailable",
                    memory_epoch=current_epoch,
                    items=[],
                    gaps=[*packet.get("gaps", []), "memory_epoch_changed_before_delivery"],
                    answerability="unknown",
                    coverage="unknown",
                    unmet_needs=[*packet.get("unmet_needs", []), "retry_against_current_epoch"],
                )
            _budget_retry_hint(packet)
            return _envelope(call_id, packet, capability_gaps=self._capability_gaps(ctx))

        @self.server.tool(name="inspect", description="Inspect one visible, versioned object or source. Protocol version 1.1.", annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False), structured_output=True)
        def inspect(ctx: Context, protocol_version: Literal["1.1"], ref: StrictStr, limit: StrictInt = 24, request_id: StrictStr | None = None) -> dict[str, Any]:
            payload = dict(protocol_version=protocol_version, ref=ref, limit=limit)
            if request_id is not None:
                payload["request_id"] = request_id
            body = _object(
                payload,
                allowed=frozenset({"protocol_version", "request_id", "ref", "limit"}),
                required=frozenset({"protocol_version", "ref"}),
            )
            _protocol(body)
            call_id: str = _request_id(body)
            call_context = self._request_context(ctx)
            ref, revision = _revision_ref(body["ref"])
            limit = body.get("limit", 24)
            if type(limit) is not int or not 1 <= limit <= 24:
                raise _error("INPUT_INVALID", "limit")
            inspected = self.core.inspect_object(call_context, ref, revision, limit=limit)
            return _envelope(call_id, {"kind": inspected.kind, "ref": inspected.ref, "revision": inspected.revision, "value": inspected.value, "memory_epoch": inspected.memory_epoch}, capability_gaps=self._capability_gaps(ctx))

        @self.server.tool(name="profile", description="Read-only categorized current-fact profile for one explicitly named subject. Uses only admitted consolidated claims; does not dump raw chat or USER.md/MEMORY.md. Protocol version 1.1. " + READ_VIEW_BUDGET_GUIDANCE + " " + RECALL_CONTEXT_GUIDANCE, annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False), structured_output=True)
        def profile(
            ctx: Context,
            protocol_version: Literal["1.1"],
            subject: StrictStr,
            max_items: Annotated[StrictInt, Field(ge=1, le=30)] = DEFAULT_MAX_ITEMS,
            budget_tokens: Annotated[StrictInt, Field(description=READ_VIEW_BUDGET_GUIDANCE)] = DEFAULT_BUDGET_TOKENS,
            request_id: StrictStr | None = None,
        ) -> dict[str, Any]:
            payload: dict[str, Any] = dict(
                protocol_version=protocol_version,
                subject=subject,
                max_items=max_items,
                budget_tokens=budget_tokens,
            )
            if request_id is not None:
                payload["request_id"] = request_id
            body = _object(
                payload,
                allowed=frozenset({"protocol_version", "request_id", "subject", "max_items", "budget_tokens"}),
                required=frozenset({"protocol_version", "subject"}),
            )
            _protocol(body)
            call_id: str = _request_id(body)
            body["request_id"] = call_id
            call_context = self._request_context(ctx)
            view = self.core.profile(call_context, body)
            current_epoch = self.core.status(call_context).memory_epoch
            if view.get("memory_epoch") is not None and view["memory_epoch"] != current_epoch:
                scrubbed = _scrub_no_content(view)
                view = dict(scrubbed) if isinstance(scrubbed, dict) else {}
                view.update(
                    status="unavailable",
                    memory_epoch=current_epoch,
                    resolved_subject=None,
                    alias_resolution="none",
                    sections={"facts": [], "preferences": [], "constraints": [], "decisions": [], "pending_intentions": []},
                    disputed=[],
                    gaps=[*view.get("gaps", []), "memory_epoch_changed_before_delivery"],
                    answerability="unknown",
                    coverage="unknown",
                    unmet_needs=[*view.get("unmet_needs", []), "retry_against_current_epoch"],
                )
            return _envelope(call_id, view, capability_gaps=self._capability_gaps(ctx))

        from ...core.trace import TRACE_GUIDANCE, fence_trace_epoch

        @self.server.tool(name="trace", description=TRACE_GUIDANCE, annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False), structured_output=True)
        def trace(ctx: Context, protocol_version: Literal["1.1"], subject: StrictStr,
                  target: StrictStr | None = None, max_hops: StrictInt = 2,
                  max_nodes: StrictInt = 24, max_paths: StrictInt = 12,
                  direction: Literal["incoming", "outgoing", "both"] = "both",
                  budget_bytes: StrictInt = 16384, request_id: StrictStr | None = None) -> dict[str, Any]:
            payload = dict(protocol_version=protocol_version, subject=subject, max_hops=max_hops,
                           max_nodes=max_nodes, max_paths=max_paths, direction=direction, budget_bytes=budget_bytes)
            if target is not None:
                payload["target"] = target
            if request_id is not None:
                payload["request_id"] = request_id
            _protocol(payload)
            call_id = _request_id(payload)
            payload["request_id"] = call_id
            context = self._request_context(ctx)
            view = self.core.trace(context, payload)
            view = fence_trace_epoch(view, self.core.status(context).memory_epoch)
            return _envelope(call_id, view, capability_gaps=self._capability_gaps(ctx))

        @self.server.tool(name="entity", description="Read-only exact one-hop entity view. action=probe returns current facts about the subject; action=related returns direct recorded statements. Incoming matches full scalar value_text only. No multi-hop traversal or inferred identity merge. Protocol version 1.1. " + READ_VIEW_BUDGET_GUIDANCE + " " + RECALL_CONTEXT_GUIDANCE, annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False), structured_output=True)
        def entity(
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
            payload: dict[str, Any] = dict(
                protocol_version=protocol_version,
                subject=subject,
                action=action,
                direction=direction,
                max_items=max_items,
                budget_tokens=budget_tokens,
            )
            if request_id is not None:
                payload["request_id"] = request_id
            if predicate is not None:
                payload["predicate"] = predicate
            body = _object(
                payload,
                allowed=frozenset({
                    "protocol_version", "request_id", "subject", "action", "direction",
                    "predicate", "max_items", "budget_tokens",
                }),
                required=frozenset({"protocol_version", "subject", "action"}),
            )
            _protocol(body)
            call_id: str = _request_id(body)
            body["request_id"] = call_id
            call_context = self._request_context(ctx)
            view = self.core.entity(call_context, body)
            current_epoch = self.core.status(call_context).memory_epoch
            if view.get("memory_epoch") is not None and view["memory_epoch"] != current_epoch:
                scrubbed = _scrub_no_content(view)
                view = dict(scrubbed) if isinstance(scrubbed, dict) else {}
                view.update(
                    status="unavailable",
                    memory_epoch=current_epoch,
                    resolved_subject=None,
                    alias_resolution="none",
                    statements=[],
                    gaps=[*view.get("gaps", []), "memory_epoch_changed_before_delivery"],
                    answerability="unknown",
                    coverage="unknown",
                    unmet_needs=[*view.get("unmet_needs", []), "retry_against_current_epoch"],
                )
            return _envelope(call_id, view, capability_gaps=self._capability_gaps(ctx))

        @self.server.tool(name="propose_memory", description="Record an assistant-visible candidate without promoting it to authority. Protocol version 1.1.", annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False), structured_output=True)
        def propose_memory(
            ctx: Context,
            protocol_version: Literal["1.1"],
            content: StrictStr,
            evidence_refs: list[StrictStr] | None = None,
            reason: StrictStr | None = None,
            request_id: StrictStr | None = None,
        ) -> dict[str, Any]:
            payload: dict[str, Any] = dict(protocol_version=protocol_version, content=content)
            if request_id is not None:
                payload["request_id"] = request_id
            if evidence_refs is not None:
                payload["evidence_refs"] = evidence_refs
            if reason is not None:
                payload["reason"] = reason
            body = _object(
                payload,
                allowed=frozenset({"protocol_version", "request_id", "content", "evidence_refs", "reason"}),
                required=frozenset({"protocol_version", "content"}),
            )
            _protocol(body)
            call_id: str = _request_id(body)
            call_context = self._request_context(ctx, mutation=True, origin="assistant_visible")
            content = body["content"]
            if type(content) is not str or not 1 <= len(content) <= _MAX_CONTENT:
                raise _error("INPUT_INVALID", "content")
            refs = body.get("evidence_refs", [])
            if type(refs) is not list or len(refs) > _MAX_REFS or any(type(ref) is not str or not 1 <= len(ref) <= 240 for ref in refs):
                raise _error("INPUT_INVALID", "evidence_refs")
            reason = body.get("reason")
            if reason is not None and (type(reason) is not str or len(reason) > 1024):
                raise _error("INPUT_INVALID", "reason")
            now = self.core.clock.utc_now()
            event: SourceEvent = {
                "protocol_version": PROTOCOL_VERSION,
                "source_event_key": f"codex-mcp:{self.session_id}:{call_id}",
                "source_revision": 1,
                "origin": "assistant_visible",
                "role": "assistant",
                "content": content,
                "occurred_at": now,
                "recorded_at": now,
                "time_precision": "instant",
                "capture_state": "complete",
                "evidence_refs": list(refs),
            }
            capture_scope_id = self.audience.capture_scope_id
            if capture_scope_id is None:
                raise _error("ACCESS_DENIED", "capture_scope")
            receipt = self.core.record_event(
                call_context,
                event,
                scope_id=capture_scope_id,
                remaining_seconds=1.0,
            )
            return _envelope(call_id, {"candidate": True, "authority": "assistant_visible_only", "receipt": receipt}, capability_gaps=self._capability_gaps(ctx))

        @self.server.tool(name="revise", description="Apply a Core-authorized, versioned revision. Protocol version 1.1.", annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False), structured_output=True)
        def revise(
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
            payload: dict[str, Any] = dict(protocol_version=protocol_version, target_ref=target_ref,
                           expected_revision=expected_revision, new_value=new_value, conditions=conditions,
                           source_evidence_refs=source_evidence_refs, valid_from=valid_from)
            if request_id is not None:
                payload["request_id"] = request_id
            body = _object(payload, allowed=frozenset({"protocol_version", "request_id", "target_ref", "expected_revision", "new_value", "conditions", "source_evidence_refs", "valid_from"}), required=frozenset({"protocol_version", "target_ref", "expected_revision", "new_value", "conditions", "source_evidence_refs", "valid_from"}))
            _protocol(body)
            call_id: str = _request_id(body)
            request = dict(body)
            request.pop("request_id", None)
            # The stdio server has no attested Codex user turn.  Keep the
            # adapter origin as memory_reinjection; Core still requires its
            # own human evidence and expected revision before changing state.
            receipt = self.core.revise(self._request_context(ctx, mutation=True, origin="human_direct"), request, remaining_seconds=1.0)
            return _envelope(call_id, receipt, capability_gaps=self._capability_gaps(ctx))

        @self.server.tool(name="forget", description="Apply a Core-authorized suppress or delete request. Protocol version 1.1.", annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False), structured_output=True)
        def forget(
            ctx: Context,
            protocol_version: Literal["1.1"],
            target_refs: list[StrictStr],
            mode: Literal["suppress", "delete"],
            expected_revisions: dict[StrictStr, StrictInt],
            reason: StrictStr | None = None,
            request_id: StrictStr | None = None,
        ) -> dict[str, Any]:
            payload: dict[str, Any] = dict(protocol_version=protocol_version, target_refs=target_refs,
                           mode=mode, expected_revisions=expected_revisions)
            if request_id is not None:
                payload["request_id"] = request_id
            if reason is not None:
                payload["reason"] = reason
            body = _object(payload, allowed=frozenset({"protocol_version", "request_id", "target_refs", "mode", "expected_revisions", "reason"}), required=frozenset({"protocol_version", "target_refs", "mode", "expected_revisions"}))
            _protocol(body)
            call_id: str = _request_id(body)
            request = dict(body)
            request.pop("request_id", None)
            receipt = self.core.forget(self._request_context(ctx, mutation=True, origin="human_direct"), request, remaining_seconds=1.0)
            return _envelope(call_id, receipt, capability_gaps=self._capability_gaps(ctx))

        @self.server.tool(name="status", description="Read bounded Core status and adapter capability gaps. Protocol version 1.1.", annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False), structured_output=True)
        def status(ctx: Context, protocol_version: Literal["1.1"] = "1.1", request_id: StrictStr | None = None) -> dict[str, Any]:
            payload = dict(protocol_version=protocol_version)
            if request_id is not None:
                payload["request_id"] = request_id
            body = _object(payload, allowed=frozenset({"protocol_version", "request_id"}), required=frozenset({"protocol_version"}))
            _protocol(body)
            call_id: str = _request_id(body)
            result = {
                "status": self.core.status(self._request_context(ctx)),
                "session": "independent_mcp_server",
                "workspace": str(self.workspace),
                "agent_id": self.config.agent_id,
                "installation_id": self.config.installation_id,
            }
            return _envelope(call_id, result, capability_gaps=self._capability_gaps(ctx))

        # mcp 2.1 builds argument models from signatures with Pydantic's
        # default ``extra=ignore``.  Public tools must reject forged identity,
        # path, scope, and host-session fields, so tighten the generated model
        # and the advertised JSON Schema after registration.
        for name in ("recall", "inspect", "profile", "entity", "trace", "propose_memory", "revise", "forget", "status"):
            tool = self.server._tool_manager.get_tool(name)
            if tool is not None:
                tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
                tool.fn_metadata.arg_model.model_rebuild(force=True)
                tool.parameters["additionalProperties"] = False


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
