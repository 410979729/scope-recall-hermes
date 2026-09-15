"""Bounded execution of one public P18 journey operation.

This module is an execution seam for the formal runner.  It deliberately does
not score answers or interpret private journey expectations.  Control actions
use the already validated Runtime/Core APIs; host actions require an explicit
host implementation and actual identifiers in its receipt.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Protocol

from scope_recall.contracts import ContractError, TrustedContext


class JourneyActionError(ValueError):
    """Malformed or unsafe operation; callers must fail the journey closed."""


class JourneyHostPort(Protocol):
    """Minimal host boundary supplied by the real Hermes/Codex owner."""

    def new_session(self, alias: str, *, action: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def host_turn(
        self,
        query: str,
        *,
        session_alias: str | None,
        action: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


_KINDS = frozenset(
    {
        "source_capture",
        "host_turn",
        "new_session",
        "authorized_state_change",
        "fault_injection",
        "deterministic_assertion",
    }
)
_FAULTS = frozenset({"sqlite_unavailable", "vector_unavailable", "deadline"})
_PROBES = frozenset({"status", "source_exists", "inspect_object", "claim_current", "claim_history"})
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CONTROL = frozenset(
    {
        "expected",
        "oracle",
        "gold",
        "answer",
        "required_facts",
        "prohibited_errors",
        "assertion",
        "instruction",
        "scorer",
        "model_output",
    }
)


def _now(runtime: Any) -> str:
    clock = getattr(getattr(runtime, "core", None), "clock", None)
    value = clock.utc_now() if callable(getattr(clock, "utc_now", None)) else None
    if isinstance(value, str) and value:
        return value
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def _safe(value: Any) -> Any:
    """Project receipts to JSON without retaining arbitrary host content."""
    if is_dataclass(value):
        return _safe(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(k): _safe(v)
            for k, v in value.items()
            if str(k).lower() not in {"content", "answer", "response", "body", "answer_text", "response_text", "response_body", "response_bytes"}
        }
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _host_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep transport identifiers/metrics while excluding arbitrary payloads."""
    allowed = frozenset(
        {
            "actual_host", "status", "request_id", "turn_id", "session_id",
            "session_alias", "transport", "error_code", "error_type",
            "latency_ms", "http_status", "retry_count", "model_call",
            "response_sha256", "answer_sha256", "executed", "fault_mode",
        }
    )
    return {key: _safe(value[key]) for key in allowed if key in value}


def _walk_control(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in _CONTROL:
                raise JourneyActionError("control_field_in_action_input")
            _walk_control(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _walk_control(child)


def _operation_id(action: Mapping[str, Any]) -> str:
    value = action.get("operation_id")
    if type(value) is not str or not _ID.fullmatch(value):
        raise JourneyActionError("operation_id_invalid")
    return value


def _artifact_root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve()
    lowered = str(root).replace("/", "\\").lower()
    if not root.is_dir() or lowered == "f:\\agents" or lowered.startswith("f:\\agents\\"):
        raise JourneyActionError("artifact_root_invalid")
    return root


def _load_input(action: Mapping[str, Any], root: Path) -> Mapping[str, Any]:
    ref = action.get("input_artifact_ref")
    if ref is None:
        value = action.get("model_input", {})
        if not isinstance(value, Mapping):
            raise JourneyActionError("model_input_invalid")
        _walk_control(value)
        return value
    if type(ref) is not str or not ref or "\x00" in ref:
        raise JourneyActionError("input_artifact_ref_invalid")
    path = Path(ref)
    candidate = path.resolve() if path.is_absolute() else (root / path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise JourneyActionError("input_artifact_outside_root") from exc
    if not candidate.is_file() or candidate.is_symlink():
        raise JourneyActionError("input_artifact_missing")
    expected = action.get("input_artifact_sha256")
    raw = candidate.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if expected is not None and (type(expected) is not str or actual != expected):
        raise JourneyActionError("input_artifact_hash_mismatch")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JourneyActionError("input_artifact_json_invalid") from exc
    if not isinstance(value, Mapping):
        raise JourneyActionError("input_artifact_object_required")
    _walk_control(value)
    return value


def _context(runtime: Any) -> TrustedContext:
    value = runtime.config.context()
    if not isinstance(value, TrustedContext):
        raise JourneyActionError("trusted_context_required")
    return value


def _scope(runtime: Any, action: Mapping[str, Any]) -> str:
    value = action.get("scope_id")
    allowed = frozenset(runtime.config.allowed_scope_ids)
    if value is None:
        if len(allowed) != 1:
            raise JourneyActionError("scope_id_required_for_multiple_scopes")
        return next(iter(allowed))
    if type(value) is not str or value not in allowed:
        raise JourneyActionError("scope_not_allowed")
    return value


def _remaining(action: Mapping[str, Any]) -> float:
    value = action.get("remaining_seconds", 1.0)
    if type(value) not in (int, float) or isinstance(value, bool) or value <= 0:
        raise JourneyActionError("remaining_seconds_invalid")
    return float(value)


def _monotonic(runtime: Any) -> float:
    clock = getattr(getattr(runtime, "core", None), "clock", None)
    value = clock.monotonic() if callable(getattr(clock, "monotonic", None)) else time.monotonic()
    if type(value) not in (int, float):
        raise JourneyActionError("clock_invalid")
    return float(value)


def _source_event(raw: Mapping[str, Any], *, operation_id: str, index: int, runtime: Any) -> dict[str, Any]:
    if {"protocol_version", "source_event_key", "source_revision", "origin", "role", "content", "occurred_at", "recorded_at", "time_precision", "capture_state", "evidence_refs"} <= set(raw):
        event = dict(raw)
    else:
        text = raw.get("text", raw.get("content"))
        if type(text) is not str:
            raise JourneyActionError("source_text_required")
        source_type = raw.get("source_type", "human_direct")
        speaker = raw.get("speaker_role", "user")
        origin = {"user": "human_direct", "assistant": "assistant_visible", "tool": "tool_observation", "document": "external_document"}.get(source_type, source_type)
        role = {"user": "user", "assistant": "assistant", "tool": "tool", "document": "document"}.get(speaker, speaker)
        occurred = raw.get("occurred_at")
        event = {
            "protocol_version": "1.1",
            "source_event_key": f"p18-journey/{operation_id}/{index}",
            "source_revision": 1,
            "origin": origin,
            "role": role,
            "content": text,
            "occurred_at": occurred,
            "recorded_at": _now(runtime),
            "time_precision": "instant" if occurred is not None else "unknown",
            "capture_state": "complete",
            "evidence_refs": [],
        }
    # The caller's trusted context remains the authority for actor origin.
    _walk_control(event)
    return event


def _source_capture(action: Mapping[str, Any], value: Mapping[str, Any], runtime: Any) -> dict[str, Any]:
    events = value.get("source_events")
    if not isinstance(events, list) or not events or len(events) > 32:
        raise JourneyActionError("source_events_invalid")
    ctx = _context(runtime)
    scope = _scope(runtime, action)
    deadline = _monotonic(runtime) + _remaining(action)
    writes = []
    for index, raw in enumerate(events, 1):
        if not isinstance(raw, Mapping):
            raise JourneyActionError("source_event_invalid")
        remaining = deadline - _monotonic(runtime)
        if remaining <= 0:
            raise JourneyActionError("action_deadline_exceeded")
        event = _source_event(raw, operation_id=str(action["operation_id"]), index=index, runtime=runtime)
        receipt = runtime.core.record_event(ctx, event, scope_id=scope, remaining_seconds=remaining)
        if getattr(receipt, "disposition", None) not in {"inserted", "duplicate"}:
            raise JourneyActionError("source_capture_not_persisted")
        writes.extend(getattr(receipt, "event_refs", ()))
    refs = [{"ref": item.ref, "revision": item.revision, "disposition": item.disposition} for item in writes]
    return {"source_capture_refs": refs, "count": len(refs), "durability": "persisted"}


def _state_change(action: Mapping[str, Any], runtime: Any) -> dict[str, Any]:
    change = action.get("state_change")
    if not isinstance(change, Mapping):
        raise JourneyActionError("state_change_required")
    method = change.get("method")
    if method not in {"revise", "forget", "purge_sqlite"}:
        raise JourneyActionError("state_change_method_invalid")
    ctx = _context(runtime)
    remaining = _remaining(action)
    if method == "purge_sqlite":
        operation_id = change.get("operation_id")
        if type(operation_id) is not str or not _ID.fullmatch(operation_id):
            raise JourneyActionError("purge_operation_id_invalid")
        result = runtime.core.purge_sqlite(ctx, operation_id, remaining_seconds=remaining)
    else:
        request = change.get("request")
        if not isinstance(request, Mapping):
            raise JourneyActionError("state_change_request_required")
        result = runtime.core.revise(ctx, dict(request), remaining_seconds=remaining) if method == "revise" else runtime.core.forget(ctx, dict(request), remaining_seconds=remaining)
    return {"method": method, "result": _safe(result)}


def _assertion(action: Mapping[str, Any], runtime: Any) -> dict[str, Any]:
    probe = action.get("probe", "status")
    if probe not in _PROBES:
        raise JourneyActionError("assertion_probe_invalid")
    ctx = _context(runtime)
    if probe == "status":
        result = runtime.status()
    elif probe == "source_exists":
        ref, revision = action.get("ref"), action.get("revision")
        if type(ref) is not str or type(revision) is not int or revision < 1:
            raise JourneyActionError("source_probe_invalid")
        source = runtime.core.source(ctx, ref, revision)
        result = {"exists": source is not None, "ref": ref, "revision": revision}
    elif probe == "inspect_object":
        ref, revision = action.get("ref"), action.get("revision")
        if type(ref) is not str or (revision is not None and (type(revision) is not int or revision < 1)):
            raise JourneyActionError("inspect_probe_invalid")
        result = runtime.core.inspect_object(ctx, ref, revision)
    elif probe == "claim_current":
        ref = action.get("ref")
        if type(ref) is not str:
            raise JourneyActionError("claim_probe_invalid")
        result = runtime.core.current_claim(ctx, ref, as_of=action.get("as_of"))
    else:
        ref = action.get("ref")
        if type(ref) is not str:
            raise JourneyActionError("claim_probe_invalid")
        result = runtime.core.claim_history(ctx, ref)
    return {"probe": probe, "observation": _safe(result)}


def _host_action(action: Mapping[str, Any]) -> dict[str, Any]:
    return {key: action[key] for key in ("operation_id", "journey_id", "session_alias") if key in action}


def _host_turn(action: Mapping[str, Any], value: Mapping[str, Any], host: Any) -> dict[str, Any]:
    query_value = value.get("query")
    query = query_value.get("text") if isinstance(query_value, Mapping) else value.get("query")
    if type(query) is not str or not query.strip() or len(query) > 8192:
        raise JourneyActionError("host_query_invalid")
    method = getattr(host, "host_turn", None)
    if not callable(method):
        return {"status": "UNSUPPORTED", "reason": "host_turn_not_configured"}
    result = method(query, session_alias=action.get("session_alias"), action=_host_action(action))
    if not isinstance(result, Mapping):
        raise JourneyActionError("host_receipt_invalid")
    required = ("request_id", "turn_id", "session_id")
    if result.get("actual_host") is not True or any(type(result.get(key)) is not str or not result[key] for key in required):
        return {"status": "UNSUPPORTED", "reason": "actual_host_identifiers_missing", "host_receipt": _safe(result)}
    answer = result.get("answer_text")
    response = result.get("response_bytes", result.get("response_body"))
    return {
        "status": "COMPLETED",
        "actual_host": True,
        "request_id": result["request_id"],
        "turn_id": result["turn_id"],
        "session_id": result["session_id"],
        "response_sha256": hashlib.sha256(response).hexdigest() if isinstance(response, bytes) else None,
        "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest() if isinstance(answer, str) else None,
        "host_receipt": _host_metadata(result),
    }


def _new_session(action: Mapping[str, Any], host: Any) -> dict[str, Any]:
    alias = action.get("session_alias")
    if type(alias) is not str or not alias.strip() or len(alias) > 240:
        raise JourneyActionError("session_alias_invalid")
    method = getattr(host, "new_session", None)
    if not callable(method):
        return {"status": "UNSUPPORTED", "reason": "new_session_not_configured"}
    result = method(alias, action=_host_action(action))
    if not isinstance(result, Mapping) or result.get("actual_host") is not True or type(result.get("session_id")) is not str or not result["session_id"]:
        return {"status": "UNSUPPORTED", "reason": "actual_session_identifier_missing", "host_receipt": _safe(result)}
    return {"status": "COMPLETED", "actual_host": True, "session_alias": alias, "session_id": result["session_id"], "host_receipt": _host_metadata(result)}


def _fault(action: Mapping[str, Any], host: Any) -> dict[str, Any]:
    mode = action.get("fault_mode")
    if mode not in _FAULTS:
        raise JourneyActionError("fault_mode_invalid")
    method = getattr(host, "fault_injection", None)
    if not callable(method):
        return {"status": "UNSUPPORTED", "reason": "fault_handler_not_configured", "fault_mode": mode}
    result = method(mode=mode, action=_host_action(action))
    if not isinstance(result, Mapping):
        raise JourneyActionError("fault_receipt_invalid")
    return {"status": "COMPLETED" if result.get("executed") is True else "FAILED", "fault_mode": mode, "fault_receipt": _host_metadata(result)}


def _write_receipt(root: Path, receipt: Mapping[str, Any]) -> Path:
    operation_id = str(receipt["operation_id"])
    directory = root / "operations"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{operation_id}.json"
    encoded = (json.dumps(dict(receipt), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        with path.open("xb") as handle:
            handle.write(encoded)
    except FileExistsError as exc:
        raise JourneyActionError("operation_already_recorded") from exc
    return path


def execute_action(
    action: Mapping[str, Any],
    runtime: Any,
    host: JourneyHostPort,
    artifact_root: str | Path,
) -> dict[str, Any]:
    """Execute one bounded action and persist one append-only receipt.

    The returned status describes execution only.  It never means that a
    semantic answer passed an independent scorer.
    """
    if not isinstance(action, Mapping):
        raise JourneyActionError("action_mapping_required")
    operation_id = _operation_id(action)
    kind = action.get("operation_kind")
    if kind not in _KINDS:
        raise JourneyActionError("operation_kind_invalid")
    root = _artifact_root(artifact_root)
    base: dict[str, Any] = {
        "schema": "scope-recall.p18.journey-action-receipt.v1",
        "formal_evaluation": False,
        "execution_evidence": "TEST_DIAGNOSTIC_ONLY",
        "operation_id": operation_id,
        "journey_id": action.get("journey_id"),
        "step_order": action.get("step_order", (action.get("source_step_orders") or [None])[0]),
        "operation_kind": kind,
        "status": "FAILED",
        "model_call": False,
        "source_capture_refs": [],
        "artifact_refs": [str(action["input_artifact_ref"])] if action.get("input_artifact_ref") is not None else [],
        "started_at": _now(runtime),
    }
    try:
        # The operation graph is control metadata.  A scorer/oracle/expected
        # field is never allowed to travel through this execution seam.
        _walk_control(action)
        value = _load_input(action, root)
        if kind == "source_capture":
            result = _source_capture(action, value, runtime)
        elif kind == "authorized_state_change":
            result = _state_change(action, runtime)
        elif kind == "deterministic_assertion":
            result = _assertion(action, runtime)
        elif kind == "new_session":
            result = _new_session(action, host)
        elif kind == "host_turn":
            result = _host_turn(action, value, host)
            base["model_call"] = result.get("status") == "COMPLETED" and result.get("actual_host") is True
        else:
            result = _fault(action, host)
        base.update(result)
        base["result"] = _safe(result)
        base["status"] = result.get("status", "COMPLETED")
    except (ContractError, JourneyActionError, OSError, TypeError, ValueError) as exc:
        base.update({"status": "FAILED", "error_code": getattr(exc, "code", None) or (str(exc) if isinstance(exc, JourneyActionError) else type(exc).__name__)})
    base["finished_at"] = _now(runtime)
    path = _write_receipt(root, base)
    base["receipt_path"] = str(path)
    return base


__all__ = ["JourneyActionError", "JourneyHostPort", "execute_action"]
