"""Hermes tool JSON boundary, schemas and trusted Core dispatch.

The provider owns session/capture lifecycle. This mixin consumes that trusted
identity and Core port; it never constructs identity from tool arguments.
"""
from __future__ import annotations
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
from typing import Any, Dict, List, cast
import uuid
from scope_recall.contracts import ContractError, validate_model_request
from scope_recall.core.read_views import DEFAULT_BUDGET_TOKENS, DEFAULT_MAX_ITEMS
from ..runtime_wiring import READ_VIEW_BUDGET_GUIDANCE
from .identity import HermesIdentityError

_PROTOCOL_VERSION = "1.1"
_MAX_CONTENT = 8192
_MAX_REFS = 32
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_JSON_DEPTH = 32
_TOOL_NAMES = frozenset({"recall", "inspect", "profile", "entity", "trace", "revise", "forget", "status"})


def _tool_error(code: str, field: str = "payload") -> ContractError:
    return ContractError(code, field)


def _strict_tool_object(
    value: object,
    *,
    allowed: frozenset[str],
    required: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Apply the frozen v1.1 JSON boundary before invoking Core."""

    if type(value) is not dict:
        raise _tool_error("INPUT_INVALID", "object")
    keys = frozenset(value)
    if not keys <= allowed:
        raise _tool_error("INPUT_INVALID", "unknown_field")
    if not required <= keys:
        raise _tool_error("INPUT_INVALID", "required_field")

    def walk(item: object, depth: int = 0) -> None:
        if depth > _MAX_JSON_DEPTH:
            raise _tool_error("INPUT_INVALID", "nesting")
        if type(item) in (str, int, float, bool) or item is None:
            return
        if type(item) is list:
            for child in item:
                walk(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise _tool_error("INPUT_INVALID", "json_key")
                walk(child, depth + 1)
            return
        raise _tool_error("INPUT_INVALID", "json_value")

    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
            raise _tool_error("INPUT_INVALID", "size")
        walk(value)
        decoded = json.loads(encoded)
    except ContractError:
        raise
    except (TypeError, ValueError, RecursionError):
        raise _tool_error("INPUT_INVALID", "json_value") from None
    if type(decoded) is not dict:
        raise _tool_error("INPUT_INVALID", "object")
    return decoded


def _check_tool_protocol(payload: dict[str, Any]) -> None:
    if payload.get("protocol_version") != _PROTOCOL_VERSION:
        raise _tool_error("INPUT_INVALID", "protocol_version")


def _tool_request_id(payload: dict[str, Any]) -> str:
    value = payload.get("request_id")
    if value is None:
        return f"hermes-tool:{uuid.uuid4().hex}"
    if type(value) is not str or not 1 <= len(value) <= 100:
        raise _tool_error("INPUT_INVALID", "request_id")
    return value


def _json_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {str(key): _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _tool_envelope(
    request_id: str,
    result: object,
    *,
    origin: str,
    capability_gaps: tuple[str, ...] = (),
) -> str:
    converted = _json_value(result)
    try:
        encoded = json.dumps(converted, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise _tool_error("OUTPUT_LIMIT", "result") from None
    if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
        raise _tool_error("OUTPUT_LIMIT", "result")
    return json.dumps(
        {
            "protocol_version": _PROTOCOL_VERSION,
            "request_id": request_id,
            "origin": origin,
            "capability_gaps": list(capability_gaps),
            "result": converted,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _tool_error_output(
    code: str,
    field: str,
    *,
    request_id: str,
    origin: str,
    capability_gaps: tuple[str, ...] = (),
) -> str:
    # Error output deliberately carries only the public contract code/field;
    # exception text could disclose a private ref, scope, or filesystem path.
    return json.dumps(
        {
            "protocol_version": _PROTOCOL_VERSION,
            "request_id": request_id,
            "origin": origin,
            "capability_gaps": list(capability_gaps),
            "error": {"code": code, "field": field},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _revision_ref(value: str) -> tuple[str, int | None]:
    if type(value) is not str or not 1 <= len(value) <= 240:
        raise _tool_error("INPUT_INVALID", "ref")
    if "@" not in value:
        return value, None
    ref, raw = value.rsplit("@", 1)
    try:
        revision = int(raw)
    except ValueError:
        raise _tool_error("INPUT_INVALID", "ref") from None
    if not ref or revision < 1 or raw != str(revision):
        raise _tool_error("INPUT_INVALID", "ref")
    return ref, revision


def _scrub_recall_output(value: object) -> object:
    """Remove rendered/body surfaces when a recall packet loses its epoch race."""

    surfaces = {
        "canonical_text", "context", "rendered", "additionalContext",
        "injection_text", "text", "body", "content",
    }
    if isinstance(value, dict):
        return {
            key: ("" if key in surfaces else _scrub_recall_output(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub_recall_output(item) for item in value]
    return value


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
                "query": {"type": "string", "minLength": 1, "maxLength": _MAX_CONTENT},
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
                "conditions": {"type": "array", "items": {"type": "string", "maxLength": _MAX_CONTENT}},
                "source_evidence_refs": {"type": "array", "maxItems": _MAX_REFS, "items": {"type": "string", "minLength": 1, "maxLength": 240}},
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
                "target_refs": {"type": "array", "minItems": 1, "maxItems": _MAX_REFS, "items": {"type": "string", "minLength": 1, "maxLength": 240}},
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
            raise _tool_error("ACCESS_DENIED", "origin")
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

    def _handle_recall(self, args: object) -> str:
        body = _strict_tool_object(
            args,
            allowed=frozenset({
                "protocol_version", "request_id", "query", "mode", "as_of",
                "max_items", "budget_tokens", "focus_refs",
            }),
            required=frozenset({"protocol_version", "query", "mode", "max_items", "budget_tokens"}),
        )
        _check_tool_protocol(body)
        request_id = _tool_request_id(body)
        body["request_id"] = request_id
        context = self._tool_context()
        validate_model_request("recall_request", body, context)
        packet = self._require_core().recall_packet(
            context,
            body,
            current_source_refs=tuple(self._current_source_refs),
            # Explicit tool calls get the bounded deep-search ceiling.  Auto
            # mode is still clamped by the trusted CoreConfig budget.
            deadline_seconds=5.0,
        )
        current_epoch = self._require_core().memory_epoch(context)
        if packet.get("memory_epoch") is not None and packet["memory_epoch"] != current_epoch:
            packet = dict(cast(dict[str, Any], _scrub_recall_output(packet)))
            packet.update(
                status="unavailable",
                memory_epoch=current_epoch,
                items=[],
                gaps=[*packet.get("gaps", []), "memory_epoch_changed_before_delivery"],
                answerability="unknown",
                coverage="unknown",
                unmet_needs=[*packet.get("unmet_needs", []), "retry_against_current_epoch"],
            )
        origin, gaps = self._tool_origin_and_gaps()
        return _tool_envelope(request_id, packet, origin=origin, capability_gaps=gaps)

    def _handle_inspect(self, args: object) -> str:
        body = _strict_tool_object(
            args,
            allowed=frozenset({"protocol_version", "request_id", "ref", "limit"}),
            required=frozenset({"protocol_version", "ref"}),
        )
        _check_tool_protocol(body)
        request_id = _tool_request_id(body)
        ref, revision = _revision_ref(body["ref"])
        limit = body.get("limit", 24)
        if type(limit) is not int or not 1 <= limit <= 24:
            raise _tool_error("INPUT_INVALID", "limit")
        context = self._tool_context()
        inspected = self._require_core().inspect_object(context, ref, revision, limit=limit)
        result = {
            "kind": inspected.kind,
            "ref": inspected.ref,
            "revision": inspected.revision,
            "value": inspected.value,
            "memory_epoch": inspected.memory_epoch,
        }
        origin, gaps = self._tool_origin_and_gaps()
        return _tool_envelope(request_id, result, origin=origin, capability_gaps=gaps)

    def _handle_profile(self, args: object) -> str:
        body = _strict_tool_object(
            args,
            allowed=frozenset({"protocol_version", "request_id", "subject", "max_items", "budget_tokens"}),
            required=frozenset({"protocol_version", "subject"}),
        )
        _check_tool_protocol(body)
        request_id = _tool_request_id(body)
        body["request_id"] = request_id
        body.setdefault("max_items", DEFAULT_MAX_ITEMS)
        body.setdefault("budget_tokens", DEFAULT_BUDGET_TOKENS)
        context = self._tool_context()
        view = self._require_core().profile(context, body)
        current_epoch = self._require_core().status(context).memory_epoch
        if view.get("memory_epoch") is not None and view["memory_epoch"] != current_epoch:
            view = dict(cast(dict[str, Any], _scrub_recall_output(view)))
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
        origin, gaps = self._tool_origin_and_gaps()
        return _tool_envelope(request_id, view, origin=origin, capability_gaps=gaps)

    def _handle_entity(self, args: object) -> str:
        body = _strict_tool_object(
            args,
            allowed=frozenset({
                "protocol_version", "request_id", "subject", "action", "direction",
                "predicate", "max_items", "budget_tokens",
            }),
            required=frozenset({"protocol_version", "subject", "action"}),
        )
        _check_tool_protocol(body)
        request_id = _tool_request_id(body)
        body["request_id"] = request_id
        body.setdefault("max_items", DEFAULT_MAX_ITEMS)
        body.setdefault("budget_tokens", DEFAULT_BUDGET_TOKENS)
        context = self._tool_context()
        view = self._require_core().entity(context, body)
        current_epoch = self._require_core().status(context).memory_epoch
        if view.get("memory_epoch") is not None and view["memory_epoch"] != current_epoch:
            view = dict(cast(dict[str, Any], _scrub_recall_output(view)))
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
        origin, gaps = self._tool_origin_and_gaps()
        return _tool_envelope(request_id, view, origin=origin, capability_gaps=gaps)

    def _handle_trace(self, args: object) -> str:
        from ...core.trace import trace_tool_schema, fence_trace_epoch
        schema = trace_tool_schema()["parameters"]
        body = _strict_tool_object(args, allowed=frozenset(schema["properties"]), required=frozenset(schema["required"]))
        _check_tool_protocol(body)
        body["request_id"] = _tool_request_id(body)
        core, context = self._require_core(), self._tool_context()
        view = core.trace(context, body)
        view = fence_trace_epoch(view, core.status(context).memory_epoch)
        origin, gaps = self._tool_origin_and_gaps()
        return _tool_envelope(body["request_id"], view, origin=origin, capability_gaps=gaps)

    def _handle_revise(self, args: object) -> str:
        context = self._tool_context(mutation=True)
        body = _strict_tool_object(
            args,
            allowed=frozenset({
                "protocol_version", "request_id", "target_ref", "expected_revision",
                "new_value", "conditions", "source_evidence_refs", "valid_from",
            }),
            required=frozenset({
                "protocol_version", "target_ref", "expected_revision", "new_value",
                "conditions", "source_evidence_refs", "valid_from",
            }),
        )
        _check_tool_protocol(body)
        request_id = _tool_request_id(body)
        request = dict(body)
        request.pop("request_id", None)
        # Core remains the only authority for current human evidence,
        # expected revisions, scope, and transaction/receipt semantics.
        receipt = self._require_core().revise(context, request, remaining_seconds=1.0)
        origin, gaps = self._tool_origin_and_gaps()
        return _tool_envelope(request_id, receipt, origin=origin, capability_gaps=gaps)

    def _handle_forget(self, args: object) -> str:
        context = self._tool_context(mutation=True)
        body = _strict_tool_object(
            args,
            allowed=frozenset({
                "protocol_version", "request_id", "target_refs", "mode",
                "expected_revisions", "reason",
            }),
            required=frozenset({"protocol_version", "target_refs", "mode", "expected_revisions"}),
        )
        _check_tool_protocol(body)
        request_id = _tool_request_id(body)
        request = dict(body)
        request.pop("request_id", None)
        receipt = self._require_core().forget(context, request, remaining_seconds=1.0)
        origin, gaps = self._tool_origin_and_gaps()
        return _tool_envelope(request_id, receipt, origin=origin, capability_gaps=gaps)

    def _handle_status(self, args: object) -> str:
        if args is None:
            args = {}
        body = _strict_tool_object(
            args,
            allowed=frozenset({"protocol_version", "request_id"}),
            required=frozenset(),
        )
        body.setdefault("protocol_version", _PROTOCOL_VERSION)
        _check_tool_protocol(body)
        request_id = _tool_request_id(body)
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
        origin, gaps = self._tool_origin_and_gaps()
        return _tool_envelope(request_id, result, origin=origin, capability_gaps=gaps)

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        """Dispatch frozen v1.1 tools through the existing Core boundary."""

        del kwargs  # Hermes 0.21.0 dispatch supplies only function name and args.
        request_id = self._tool_request_id_hint(args)
        if type(tool_name) is not str or tool_name not in _TOOL_NAMES:
            try:
                origin, gaps = self._tool_origin_and_gaps(memory_result=False)
            except (ContractError, HermesIdentityError):
                origin, gaps = "origin_unknown", ()
            return _tool_error_output(
                "INPUT_INVALID",
                "tool_name",
                request_id=request_id,
                origin=origin,
                capability_gaps=gaps,
            )
        try:
            if tool_name == "recall":
                return self._handle_recall(args)
            if tool_name == "inspect":
                return self._handle_inspect(args)
            if tool_name == "profile":
                return self._handle_profile(args)
            if tool_name == "entity":
                return self._handle_entity(args)
            if tool_name == "trace":
                return self._handle_trace(args)
            if tool_name == "revise":
                return self._handle_revise(args)
            if tool_name == "forget":
                return self._handle_forget(args)
            return self._handle_status(args)
        except ContractError as exc:
            try:
                origin, gaps = self._tool_origin_and_gaps()
            except (ContractError, HermesIdentityError):
                origin, gaps = "origin_unknown", ()
            return _tool_error_output(
                exc.code,
                exc.field,
                request_id=request_id,
                origin=origin,
                capability_gaps=gaps,
            )
        except HermesIdentityError:
            return _tool_error_output(
                "ACCESS_DENIED",
                "identity",
                request_id=request_id,
                origin="origin_unknown",
            )
        except (OSError, RuntimeError):
            # Do not expose exception text, refs, paths, or storage details.
            return _tool_error_output(
                "STORAGE_UNAVAILABLE",
                "storage",
                request_id=request_id,
                origin="origin_unknown",
            )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # Hermes retains the returned schema objects; return fresh objects so
        # host-side mutation cannot alter the adapter's contract for later runs.
        from ...core.trace import trace_tool_schema
        return [*json.loads(json.dumps(_TOOL_SCHEMAS, ensure_ascii=False)), trace_tool_schema()]

