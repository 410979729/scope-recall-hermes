"""Convert observed Codex hook payloads into core SourceEvent DTOs."""

from __future__ import annotations

import json
import re
from typing import Any

from scope_recall.contracts import ContractError, SourceEvent, utc_timestamp
from scope_recall.core.capture_filters import without_private

from ..host_profiles import (
    HOSTS,
)
from ..host_profiles import (
    claude_code_person_text as claude_code_person_text,
)
from ..host_profiles import (
    is_codex_suggestions_prompt as is_codex_suggestions_prompt,
)
from ..host_profiles import (
    is_codex_suggestions_reply as is_codex_suggestions_reply,
)
from ..host_profiles import (
    is_task_notification as is_task_notification,
)
from ..host_profiles import (
    is_workbuddy_agent_run as is_workbuddy_agent_run,
)
from ..host_profiles import (
    is_workbuddy_notice as is_workbuddy_notice,
)
from ..host_profiles import (
    workbuddy_person_text as workbuddy_person_text,
)
from ..host_profiles import (
    workbuddy_record_words as workbuddy_record_words,
)

_SCOPE_RECALL_TOOL = re.compile(r"(?:^|__)(?:scope[-_]?recall)", re.IGNORECASE)
_MAX_TOOL_CHARS = 65536
EMPTY_ANSWER = {host: profile.empty_answer for host, profile in HOSTS.items()}
TURN_FIELD = {host: profile.turn_field for host, profile in HOSTS.items()}


def filter_capture_reply(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Filter native spool text locally; None identifies an ordinary hook event."""
    if payload.get("hook_event_name") != "FilterCapture":
        return None
    text = payload.get("text")
    if not isinstance(text, str):
        return {}
    visible, count = without_private(without_lone_surrogates(text))
    return {"text": visible, "private_spans_removed": count}


def host_source_key(
    *,
    installation_id: str,
    session_id: str,
    event_kind: str,
    event_id: str,
    revision: int = 1,
    host: str = "codex",
) -> str:
    return f"{host}:{installation_id}:{session_id}:{event_kind}:{event_id}@{revision}"


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


#: Where a prompt's payload carries since when the host's model still holds the session's own turns
#: (``transcript.context_since``) to a recall that runs where the session record cannot be opened: the entry's resident
#: server, or the server of a client on another machine.  Only this package's own hook and client write it.
IN_CONTEXT_FIELD = "scope_recall_in_context_since"


def in_context_since(payload: dict[str, Any]) -> str | None:
    """The moment a payload carries in ``IN_CONTEXT_FIELD``, as a UTC timestamp; None when it carries none or
    not one."""
    value = payload.get(IN_CONTEXT_FIELD)
    if type(value) is not str:
        return None
    try:
        return utc_timestamp(value)
    except ContractError:
        return None


def user_prompt_source_event(
    *,
    installation_id: str,
    session_id: str,
    turn_id: str,
    prompt: str,
    recorded_at: str,
    host: str = "codex",
    gaps: tuple[str, ...] = (),
) -> SourceEvent | None:
    if not prompt.strip():
        return None
    capture_state = "partial" if gaps else "complete"
    return {
        "protocol_version": "1.1",
        "source_event_key": host_source_key(
            host=host,
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
    occurred_at: str | None = None,
    host: str = "codex",
) -> tuple[SourceEvent | None, tuple[str, ...]]:
    """A turn's reply under the turn's key, said at ``occurred_at`` when the client says when, else when recorded."""
    gaps: list[str] = []
    if not message.strip():
        gaps.append("outcome_gap:missing_assistant_body")
        return None, tuple(gaps)
    return {
        "protocol_version": "1.1",
        "source_event_key": host_source_key(
            host=host,
            installation_id=installation_id,
            session_id=session_id,
            event_kind="assistant",
            event_id=turn_id,
        ),
        "source_revision": 1,
        "origin": "assistant_visible",
        "role": "assistant",
        "content": message,
        "occurred_at": recorded_at if occurred_at is None else occurred_at,
        "recorded_at": recorded_at,
        "time_precision": "instant",
        "capture_state": "complete",
        "evidence_refs": [],
    }, tuple(gaps)


def recorded_source_event(
    *,
    installation_id: str,
    session_id: str,
    entry_id: str,
    role: str,
    text: str,
    occurred_at: str,
    recorded_at: str,
    host: str = "claude-code",
) -> SourceEvent:
    """A message read from the host's own session record, under the record's id for it."""
    return {
        "protocol_version": "1.1",
        "source_event_key": host_source_key(
            host=host,
            installation_id=installation_id,
            session_id=session_id,
            event_kind="record",
            event_id=entry_id,
        ),
        "source_revision": 1,
        "origin": "human_direct" if role == "user" else "assistant_visible",
        "role": role,
        "content": text,
        "occurred_at": occurred_at,
        "recorded_at": recorded_at,
        "time_precision": "instant",
        "capture_state": "complete",
        "evidence_refs": [],
    }


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
    host: str = "codex",
) -> tuple[SourceEvent | None, tuple[str, ...], str]:
    origin = "memory_reinjection" if is_scope_recall_tool(tool_name) else "tool_observation"
    input_text, input_truncated = _serialize_tool_value(tool_input)
    response_text, response_truncated = _serialize_tool_value(tool_response)
    content = json.dumps(
        {"tool_name": tool_name, "tool_input": input_text, "tool_response": response_text}, ensure_ascii=False
    )
    if not response_text.strip() and not input_text.strip():
        return None, ("outcome_gap:missing_tool_result",), origin
    gaps = ("capture_gap:tool_payload_truncated",) if input_truncated or response_truncated else ()
    return (
        {
            "protocol_version": "1.1",
            "source_event_key": host_source_key(
                host=host,
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
        },
        gaps,
        origin,
    )


def lifecycle_source_event(
    *,
    installation_id: str,
    session_id: str,
    event_kind: str,
    event_id: str,
    content: str,
    recorded_at: str,
    host: str = "codex",
    gaps: tuple[str, ...] = (),
) -> SourceEvent:
    return {
        "protocol_version": "1.1",
        "source_event_key": host_source_key(
            host=host,
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


def turn_id_from_payload(
    payload: dict[str, Any], *, required: bool, field: str = "turn_id"
) -> tuple[str | None, tuple[str, ...]]:
    """The host's id of this turn: Codex's ``turn_id``, Claude Code's ``prompt_id``."""
    turn_id = _bounded_turn_id(payload.get(field))
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


#: A lone surrogate: what a client writes for half of a broken emoji (JavaScript's ``JSON.stringify`` escapes it as
#: ``\\ud83d``).  Python keeps it in a string and cannot encode it, so a prompt or a reply holding one was refused
#: whole (``INPUT_INVALID``) and a record line holding one was skipped: the message was lost for one character.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def without_lone_surrogates(value: Any) -> Any:
    """``value`` with every lone surrogate in its strings replaced by U+FFFD, the character that stands for one.

    It walks the value with a stack of its own: walked by calling itself, a payload nested deeper than the
    interpreter allows (a tool's output, 499 levels on Python 3.11) would end the hook."""
    if isinstance(value, str):
        return _LONE_SURROGATE.sub("\ufffd", value)
    if not isinstance(value, (dict, list)):
        return value
    top: dict | list = {} if isinstance(value, dict) else []
    pending = [(value, top)]
    while pending:
        source, target = pending.pop()
        for key, item in source.items() if isinstance(source, dict) else enumerate(source):
            if isinstance(item, str):
                item = _LONE_SURROGATE.sub("\ufffd", item)
            elif isinstance(item, (dict, list)):
                copy: dict | list = {} if isinstance(item, dict) else []
                pending.append((item, copy))
                item = copy
            if isinstance(target, dict):
                target[_LONE_SURROGATE.sub("\ufffd", key) if isinstance(key, str) else key] = item
            else:
                target.append(item)
    return top


def visible_recall_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy a recall request with private spans removed before any process or embedding boundary."""
    prompt = payload.get("prompt")
    return {**payload, "prompt": without_private(prompt)[0]} if type(prompt) is str else dict(payload)
