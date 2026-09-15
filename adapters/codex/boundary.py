"""Convert observed Codex hook payloads into core SourceEvent DTOs."""
from __future__ import annotations

import json
import re
from typing import Any

from scope_recall.contracts import SourceEvent

_SCOPE_RECALL_TOOL = re.compile(r"(?:^|__)(?:scope[-_]?recall)", re.IGNORECASE)
_MAX_TOOL_CHARS = 65536


def host_source_key(
    *,
    installation_id: str,
    session_id: str,
    event_kind: str,
    event_id: str,
    revision: int = 1,
) -> str:
    return f"codex:{installation_id}:{session_id}:{event_kind}:{event_id}@{revision}"


def is_scope_recall_tool(tool_name: object) -> bool:
    if type(tool_name) is not str or not tool_name.strip():
        return False
    return _SCOPE_RECALL_TOOL.search(tool_name) is not None


def _bounded_turn_id(value: object) -> str | None:
    if type(value) is not str or not value.strip() or len(value) > 240:
        return None
    return value.strip()


def _serialize_tool_value(value: object) -> tuple[str, bool]:
    if value is None:
        return "", False
    if isinstance(value, str):
        return value[:_MAX_TOOL_CHARS], len(value) > _MAX_TOOL_CHARS
    try:
        encoded = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        encoded = str(value)
    return encoded[:_MAX_TOOL_CHARS], len(encoded) > _MAX_TOOL_CHARS


def user_prompt_source_event(
    *,
    installation_id: str,
    session_id: str,
    turn_id: str,
    prompt: str,
    recorded_at: str,
    gaps: tuple[str, ...] = (),
) -> SourceEvent | None:
    if not prompt.strip():
        return None
    capture_state = "partial" if gaps else "complete"
    return {
        "protocol_version": "1.1",
        "source_event_key": host_source_key(
            installation_id=installation_id,
            session_id=session_id,
            event_kind="user",
            event_id=turn_id,
        ),
        "source_revision": 1,
        "origin": "human_direct",
        "role": "user",
        "content": prompt,
        "occurred_at": recorded_at,
        "recorded_at": recorded_at,
        "time_precision": "instant",
        "capture_state": capture_state,
        "evidence_refs": [],
    }


def assistant_stop_source_event(
    *,
    installation_id: str,
    session_id: str,
    turn_id: str,
    message: str,
    recorded_at: str,
) -> tuple[SourceEvent | None, tuple[str, ...]]:
    gaps: list[str] = []
    if not message.strip():
        gaps.append("outcome_gap:missing_assistant_body")
        return None, tuple(gaps)
    return {
        "protocol_version": "1.1",
        "source_event_key": host_source_key(
            installation_id=installation_id,
            session_id=session_id,
            event_kind="assistant",
            event_id=turn_id,
        ),
        "source_revision": 1,
        "origin": "assistant_visible",
        "role": "assistant",
        "content": message,
        "occurred_at": recorded_at,
        "recorded_at": recorded_at,
        "time_precision": "instant",
        "capture_state": "complete",
        "evidence_refs": [],
    }, tuple(gaps)


def tool_use_source_event(
    *,
    installation_id: str,
    session_id: str,
    turn_id: str,
    tool_use_id: str,
    tool_name: str,
    tool_input: object,
    tool_response: object,
    recorded_at: str,
) -> tuple[SourceEvent | None, tuple[str, ...], str]:
    origin = "memory_reinjection" if is_scope_recall_tool(tool_name) else "tool_observation"
    input_text, input_truncated = _serialize_tool_value(tool_input)
    response_text, response_truncated = _serialize_tool_value(tool_response)
    content = json.dumps({"tool_name": tool_name, "tool_input": input_text, "tool_response": response_text}, ensure_ascii=False)
    if not response_text.strip() and not input_text.strip():
        return None, ("outcome_gap:missing_tool_result",), origin
    gaps = ("capture_gap:tool_payload_truncated",) if input_truncated or response_truncated else ()
    return {
        "protocol_version": "1.1",
        "source_event_key": host_source_key(
            installation_id=installation_id,
            session_id=session_id,
            event_kind="tool",
            event_id=tool_use_id,
        ),
        "source_revision": 1,
        "origin": origin,
        "role": "tool",
        "content": content,
        "occurred_at": recorded_at,
        "recorded_at": recorded_at,
        "time_precision": "instant",
        "capture_state": "partial" if gaps else "complete",
        "evidence_refs": [],
    }, gaps, origin


def lifecycle_source_event(
    *,
    installation_id: str,
    session_id: str,
    event_kind: str,
    event_id: str,
    content: str,
    recorded_at: str,
    gaps: tuple[str, ...] = (),
) -> SourceEvent:
    return {
        "protocol_version": "1.1",
        "source_event_key": host_source_key(
            installation_id=installation_id,
            session_id=session_id,
            event_kind=event_kind,
            event_id=event_id,
        ),
        "source_revision": 1,
        "origin": "host_generated",
        "role": "system",
        "content": content,
        "occurred_at": recorded_at,
        "recorded_at": recorded_at,
        "time_precision": "instant",
        "capture_state": "partial" if gaps else "complete",
        "evidence_refs": [],
    }


def turn_id_from_payload(payload: dict[str, Any], *, required: bool) -> tuple[str | None, tuple[str, ...]]:
    turn_id = _bounded_turn_id(payload.get("turn_id"))
    if turn_id is None and required:
        return None, ("capability_gap:missing_turn_id",)
    return turn_id, ()


def authorized_attachment_refs(payload: dict[str, Any]) -> tuple[list[str], tuple[str, ...]]:
    """Do not treat probe metadata as host authorization for retained artifacts."""

    attachments = payload.get("attachments")
    if attachments is None:
        return [], ()
    if not isinstance(attachments, list):
        return [], ("attachment_gap:unsupported_shape",)
    if not attachments:
        return [], ()
    return [], ("attachment_gap:host_authorization_unverified",)
