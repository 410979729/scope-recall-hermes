"""Hermes tool JSON boundary, schemas and trusted Core dispatch.

The provider owns session/capture lifecycle. This mixin consumes that trusted
identity and Core port; it never constructs identity from tool arguments.
"""
from __future__ import annotations
import json
from typing import Any, Dict, List
import uuid
from scope_recall.contracts import ContractError, validate_model_request
from scope_recall.core.read_views import DEFAULT_BUDGET_TOKENS, DEFAULT_MAX_ITEMS
from scope_recall.core.trace import fence_trace_epoch, trace_tool_schema
from ..runtime_wiring import READ_VIEW_BUDGET_GUIDANCE
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
from .identity import HermesIdentityError

_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    {
        "name": "recall",
        "description": "Run the shared bounded read-only recall pipeline.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "protocol_version": {"type": "string", "const": "1.1"},
                "request_id": {"type": "string", "minLength": 1, "maxLength": 100},
                "query": {"type": "string", "minLength": 1, "maxLength": MAX_CONTENT},
                "mode": {"type": "string", "enum": ["auto", "current", "history", "as_of", "method"]},
                "as_of": {"type": "string", "minLength": 1, "maxLength": 240},
                "max_items": {"type": "integer"},
                "budget_tokens": {"type": "integer"},
                "focus_refs": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 240}},
            },
            "required": ["protocol_version", "query", "mode", "max_items", "budget_tokens"],
        },
    },
    {
        "name": "inspect",
        "description": "Inspect one visible, versioned object or source.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "protocol_version": {"type": "string", "const": "1.1"},
                "request_id": {"type": "string", "minLength": 1, "maxLength": 100},
                "ref": {"type": "string", "minLength": 1, "maxLength": 240},
                "limit": {"type": "integer", "minimum": 1, "maximum": 24, "default": 24},
            },
            "required": ["protocol_version", "ref"],
        },
    },
    {
        "name": "profile",
        "description": (
            "Read-only categorized current-fact profile for one explicitly named subject. "
            "Uses only admitted consolidated claims; does not dump raw chat or USER.md/MEMORY.md. "
            "Protocol version 1.1. " + READ_VIEW_BUDGET_GUIDANCE
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "protocol_version": {"type": "string", "const": "1.1"},
                "request_id": {"type": "string", "minLength": 1, "maxLength": 100},
                "subject": {"type": "string", "minLength": 1, "maxLength": 240},
                "max_items": {"type": "integer", "minimum": 1, "maximum": 30, "default": DEFAULT_MAX_ITEMS},
                "budget_tokens": {"type": "integer", "minimum": 64, "maximum": 8000, "default": DEFAULT_BUDGET_TOKENS},
            },
            "required": ["protocol_version", "subject"],
        },
    },
    {
        "name": "entity",
        "description": (
            "Read-only exact one-hop entity view. action=probe returns current facts about the subject; "
            "action=related returns direct recorded statements. Incoming matches full scalar value_text only. "
            "No multi-hop traversal or inferred identity merge. Protocol version 1.1. "
            + READ_VIEW_BUDGET_GUIDANCE
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "protocol_version": {"type": "string", "const": "1.1"},
                "request_id": {"type": "string", "minLength": 1, "maxLength": 100},
                "subject": {"type": "string", "minLength": 1, "maxLength": 240},
                "action": {"type": "string", "enum": ["probe", "related"]},
                "direction": {"type": "string", "enum": ["outgoing", "incoming", "both"], "default": "both"},
                "predicate": {"type": "string", "minLength": 1, "maxLength": 240},
                "max_items": {"type": "integer", "minimum": 1, "maximum": 30, "default": DEFAULT_MAX_ITEMS},
                "budget_tokens": {"type": "integer", "minimum": 64, "maximum": 8000, "default": DEFAULT_BUDGET_TOKENS},
            },
            "required": ["protocol_version", "subject", "action"],
        },
    },
    {
        "name": "revise",
        "description": "Apply a Core-authorized, versioned revision.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "protocol_version": {"type": "string", "const": "1.1"},
                "request_id": {"type": "string", "minLength": 1, "maxLength": 100},
                "target_ref": {"type": "string", "minLength": 1, "maxLength": 240},
                "expected_revision": {"type": "integer", "minimum": 1},
                "new_value": {},
                "conditions": {"type": "array", "items": {"type": "string", "maxLength": MAX_CONTENT}},
                "source_evidence_refs": {"type": "array", "maxItems": MAX_REFS, "items": {"type": "string", "minLength": 1, "maxLength": 240}},
                "valid_from": {"type": ["string", "null"]},
            },
            "required": ["protocol_version", "target_ref", "expected_revision", "new_value", "conditions", "source_evidence_refs", "valid_from"],
        },
    },
    {
        "name": "forget",
        "description": "Apply a Core-authorized suppress or delete request.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "protocol_version": {"type": "string", "const": "1.1"},
                "request_id": {"type": "string", "minLength": 1, "maxLength": 100},
                "target_refs": {"type": "array", "minItems": 1, "maxItems": MAX_REFS, "items": {"type": "string", "minLength": 1, "maxLength": 240}},
                "mode": {"type": "string", "enum": ["suppress", "delete"]},
                "expected_revisions": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 1}},
                "reason": {"type": "string", "maxLength": 1024},
            },
            "required": ["protocol_version", "target_refs", "mode", "expected_revisions"],
        },
    },
    {
        "name": "status",
        "description": "Read bounded Core status and adapter capability gaps.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "protocol_version": {"type": "string", "const": "1.1", "default": "1.1"},
                "request_id": {"type": "string", "minLength": 1, "maxLength": 100},
            },
            "required": [],
        },
    },
)
#: Every schema by tool name; the strict boundary reads allowed/required from here.
_SCHEMAS: dict[str, dict[str, Any]] = {schema["name"]: schema for schema in (*_TOOL_SCHEMAS, trace_tool_schema())}
_TOOL_NAMES = frozenset(_SCHEMAS)


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _unfenced_turn_packet(request_id: str) -> dict[str, Any]:
    """The contract's unavailable recall packet, for a turn that overflowed its source fence.

    The request is valid and nothing failed, so an error would wrongly blame
    the caller (INPUT_INVALID) or storage.  This is the shape the epoch fence
    already delivers when a packet cannot be handed over safely: no items,
    unknown answerability and coverage, and a gap saying why.
    """
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "status": "unavailable",
        "memory_epoch": None,
        "items": [],
        "gaps": ["current_source_refs_limit"],
        "diagnostic_ref": None,
        "answerability": "unknown",
        "coverage": "unknown",
        "unmet_needs": ["retry_next_turn"],
    }


def _error_output(code: str, field: str, *, request_id: str, origin: str, capability_gaps: tuple[str, ...] = ()) -> str:
    # Error output deliberately carries only the public contract code/field;
    # exception text could disclose a private ref, scope, or filesystem path.
    return _dumps({
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "origin": origin,
        "capability_gaps": list(capability_gaps),
        "error": {"code": code, "field": field},
    })


class HermesToolSurface:
    """Frozen public tool contract, bound to the provider session owner."""

    def _tool_context(self, *, mutation: bool = False):
        """Bind tool calls to initialized Hermes identity, never tool arguments."""

        identity = self._require_identity()
        context = identity.trusted_context(mutation=mutation)
        if mutation and (
            identity.scope.platform != "cli"
            or context.actor_origin != "human_direct"
            or identity.read_only
        ):
            # A2A is authenticated as an audience, not attested as an operator.
            # Do this check before decoding target refs so a remote caller gets
            # neither a private-scope lookup nor a ref-dependent error.
            raise ContractError("ACCESS_DENIED", "origin")
        return context

    def _tool_origin_and_gaps(self, *, memory_result: bool = True) -> tuple[str, tuple[str, ...]]:
        identity = self._require_identity()
        origin = "memory_reinjection" if memory_result else identity.trusted_context().actor_origin
        gaps = tuple(
            dict.fromkeys(
                (
                    *identity.runtime_audience.capability_gaps,
                    *self._diagnostics.capability_gaps,
                )
            )
        )
        return origin, gaps

    def _tool_request_id_hint(self, args: object) -> str:
        if isinstance(args, dict):
            value = args.get("request_id")
            if type(value) is str and 1 <= len(value) <= 100:
                return value
        return f"hermes-tool:{uuid.uuid4().hex}"

    def _tool_body(self, args: object, name: str) -> dict[str, Any]:
        """One strict pass per call: the tool's schema keys, the protocol, and a pinned request id."""
        schema = _SCHEMAS[name]["parameters"]
        body = strict_object(args, allowed=frozenset(schema["properties"]), required=frozenset(schema["required"]))
        # status is the only tool whose protocol_version is optional; its
        # schema default applies.  Everywhere else the field is required and
        # strict_object already rejected its absence.
        body.setdefault("protocol_version", PROTOCOL_VERSION)
        check_protocol(body)
        body["request_id"] = bounded_request_id(body, prefix="hermes-tool")
        return body

    def _reply(self, request_id: str, result: object) -> str:
        origin, gaps = self._tool_origin_and_gaps()
        return _dumps(envelope(request_id, result, origin=origin, capability_gaps=gaps))

    def _error_reply(self, code: str, field: str, request_id: str, *, memory_result: bool = True) -> str:
        try:
            origin, gaps = self._tool_origin_and_gaps(memory_result=memory_result)
        except (ContractError, HermesIdentityError):
            origin, gaps = "origin_unknown", ()
        return _error_output(code, field, request_id=request_id, origin=origin, capability_gaps=gaps)

    def _handle_recall(self, args: object) -> str:
        body = self._tool_body(args, "recall")
        context = self._tool_context()
        validate_model_request("recall_request", body, context)
        if self._current_source_refs_overflow:
            # Recalling without every ref of this turn could hand the turn's
            # own sources back; the provider reports the capability gap.
            return self._reply(body["request_id"], _unfenced_turn_packet(body["request_id"]))
        core = self._require_core()
        packet = core.recall_packet(
            context,
            body,
            current_source_refs=tuple(self._current_source_refs),
            # Explicit tool calls get the bounded deep-search ceiling.  Auto
            # mode is still clamped by the trusted CoreConfig budget.
            deadline_seconds=5.0,
            # A lookup that finds nothing says so; prefetch keeps background.
            background_without_evidence=False,
        )
        packet = fence_epoch(packet, core.memory_epoch(context), FENCED_RECALL)
        return self._reply(body["request_id"], packet)

    def _handle_inspect(self, args: object) -> str:
        body = self._tool_body(args, "inspect")
        ref, revision = revision_ref(body["ref"])
        limit = body.get("limit", 24)
        if type(limit) is not int or not 1 <= limit <= 24:
            raise ContractError("INPUT_INVALID", "limit")
        context = self._tool_context()
        inspected = self._require_core().inspect_object(context, ref, revision, limit=limit)
        result = {
            "kind": inspected.kind,
            "ref": inspected.ref,
            "revision": inspected.revision,
            "value": inspected.value,
            "memory_epoch": inspected.memory_epoch,
        }
        return self._reply(body["request_id"], result)

    def _handle_profile(self, args: object) -> str:
        body = self._tool_body(args, "profile")
        body.setdefault("max_items", DEFAULT_MAX_ITEMS)
        body.setdefault("budget_tokens", DEFAULT_BUDGET_TOKENS)
        context = self._tool_context()
        core = self._require_core()
        view = core.profile(context, body)
        view = fence_epoch(view, core.status(context).memory_epoch, FENCED_PROFILE)
        return self._reply(body["request_id"], view)

    def _handle_entity(self, args: object) -> str:
        body = self._tool_body(args, "entity")
        body.setdefault("max_items", DEFAULT_MAX_ITEMS)
        body.setdefault("budget_tokens", DEFAULT_BUDGET_TOKENS)
        context = self._tool_context()
        core = self._require_core()
        view = core.entity(context, body)
        view = fence_epoch(view, core.status(context).memory_epoch, FENCED_ENTITY)
        return self._reply(body["request_id"], view)

    def _handle_trace(self, args: object) -> str:
        body = self._tool_body(args, "trace")
        context = self._tool_context()
        core = self._require_core()
        view = core.trace(context, body)
        view = fence_trace_epoch(view, core.status(context).memory_epoch)
        return self._reply(body["request_id"], view)

    def _handle_revise(self, args: object) -> str:
        context = self._tool_context(mutation=True)
        body = self._tool_body(args, "revise")
        request_id = body.pop("request_id")
        # Core remains the only authority for current human evidence,
        # expected revisions, scope, and transaction/receipt semantics.
        receipt = self._require_core().revise(context, body, remaining_seconds=1.0)
        return self._reply(request_id, receipt)

    def _handle_forget(self, args: object) -> str:
        context = self._tool_context(mutation=True)
        body = self._tool_body(args, "forget")
        request_id = body.pop("request_id")
        receipt = self._require_core().forget(context, body, remaining_seconds=1.0)
        return self._reply(request_id, receipt)

    def _handle_status(self, args: object) -> str:
        body = self._tool_body({} if args is None else args, "status")
        context = self._tool_context()
        identity = self._require_identity()
        result = {
            "status": self._require_core().status(context),
            "session": identity.session_id,
            "agent_id": identity.scope.agent_identity,
            "workspace": identity.scope.agent_workspace,
            "installation_id": identity.binding.installation_id,
            "platform": identity.scope.platform,
            "chat_type": identity.scope.chat_type,
            "chat_id": identity.scope.chat_id,
            "thread_id": identity.scope.thread_id,
        }
        return self._reply(body["request_id"], result)

    _HANDLERS = {
        "recall": _handle_recall,
        "inspect": _handle_inspect,
        "profile": _handle_profile,
        "entity": _handle_entity,
        "trace": _handle_trace,
        "revise": _handle_revise,
        "forget": _handle_forget,
        "status": _handle_status,
    }

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        """Dispatch frozen v1.1 tools through the existing Core boundary."""

        del kwargs  # Hermes 0.21.0 dispatch supplies only function name and args.
        request_id = self._tool_request_id_hint(args)
        handler = self._HANDLERS.get(tool_name) if type(tool_name) is str else None
        if handler is None:
            return self._error_reply("INPUT_INVALID", "tool_name", request_id, memory_result=False)
        try:
            return handler(self, args)
        except ContractError as exc:
            return self._error_reply(exc.code, exc.field, request_id)
        except HermesIdentityError:
            return _error_output("ACCESS_DENIED", "identity", request_id=request_id, origin="origin_unknown")
        except (OSError, RuntimeError):
            # Do not expose exception text, refs, paths, or storage details.
            return _error_output("STORAGE_UNAVAILABLE", "storage", request_id=request_id, origin="origin_unknown")

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # Hermes retains the returned schema objects; return fresh objects so
        # host-side mutation cannot alter the adapter's contract for later runs.
        return [*json.loads(json.dumps(_TOOL_SCHEMAS, ensure_ascii=False)), trace_tool_schema()]
