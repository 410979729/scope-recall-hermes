"""Convert observed Codex hook payloads into core SourceEvent DTOs."""

from __future__ import annotations

import json
import re
from typing import Any

from scope_recall.contracts import ContractError, SourceEvent, utc_timestamp

_SCOPE_RECALL_TOOL = re.compile(r"(?:^|__)(?:scope[-_]?recall)", re.IGNORECASE)
_MAX_TOOL_CHARS = 65536
#: What a hook with nothing to add writes to stdout, by client.  WorkBuddy puts a prompt hook's whole stdout in front
#: of the prompt unless it carries ``additionalContext`` (its ``executeUserPromptSubmitHooks``), so "{}" would stand
#: before every prompt with nothing recalled; for it an empty answer is nothing at all.
EMPTY_ANSWER = {"codex": "{}", "claude-code": "{}", "workbuddy": "", "dsh": "{}"}
#: Turn identifiers use the host-specific fields below.  Claude Code uses ``prompt_id``, while Codex uses
#: ``turn_id``.  Claude Code tool output is not captured, avoiding large volumes of tool traffic and the embedding
#: work those captures would generate.
TURN_FIELD = {"codex": "turn_id", "claude-code": "prompt_id", "workbuddy": "generation_id", "dsh": "turn_id"}


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


#: How Claude Code opens the prompt it hands the model when a background task finishes.  The session
#: record marks such an entry ``origin.kind: task-notification``, never the owner's; the prompt hook sees
#: only the text.
TASK_NOTIFICATION_PREFIX = "<task-notification>"


def is_task_notification(prompt: str) -> bool:
    """Whether a prompt is Claude Code's notice that a background task finished, not the owner's words."""
    return prompt.lstrip().startswith(TASK_NOTIFICATION_PREFIX)


#: A reminder Claude's desktop app puts in the person's message, addressed to the model (a session started without a
#: project folder, and others): a text block of its own, or ahead of what was typed in one string.  Such a block opens
#: a line; the person's own words about one, inside a line, stay theirs.  The blank lines after one go with it.  The
#: opening tag is matched within its line, and its end found by a plain search, so a text of many unclosed openings
#: costs one pass, not one pass per opening.
_CLAUDE_CODE_REMINDER_OPEN = re.compile(r"^[ \t]*<system-reminder\b[^>\n]*>", re.MULTILINE)
_CLAUDE_CODE_REMINDER_CLOSE = "</system-reminder>"
_BLANK_LINES = re.compile(r"(?:[ \t]*\r?\n)*")


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


def claude_code_person_text(text: str) -> str:
    """The person's own words in a Claude Code prompt or session-record message: without the ``<system-reminder>``
    blocks the app put in it, or the text as it came when it holds none.  Complete reminder blocks are excluded from
    the evidence used to derive the person's claims.  A block that never closes, and what follows it, stay."""
    kept: list[str] = []
    position = 0
    while (opening := _CLAUDE_CODE_REMINDER_OPEN.search(text, position)) is not None:
        closing = text.find(_CLAUDE_CODE_REMINDER_CLOSE, opening.end())
        if closing < 0:
            break
        kept.append(text[position : opening.start()])
        position = closing + len(_CLAUDE_CODE_REMINDER_CLOSE)
        blank = _BLANK_LINES.match(text, position)
        position = blank.end() if blank is not None else position
    if not kept:
        return text
    kept.append(text[position:])
    return "".join(kept).strip()


#: How WorkBuddy opens the message it hands its model, and so its prompt hook, when a Stop hook or a goal asks the
#: turn to go on (its record marks the message ``providerData.isMeta``).
WORKBUDDY_FEEDBACK_PREFIX = "Stop hook feedback:"


def is_workbuddy_notice(prompt: str) -> bool:
    """Whether a WorkBuddy prompt is WorkBuddy's own: a background task's notice, or a request to go on."""
    return is_task_notification(prompt) or prompt.lstrip().startswith(WORKBUDDY_FEEDBACK_PREFIX)


#: What WorkBuddy wraps around the person's words in a message: its reminders, and the block that holds what was typed.
#: Its prompt hook strips both itself (as of 5.3.14); its session record keeps them.
_WORKBUDDY_REMINDER = re.compile(r"<system-reminder\b[^>]*>.*?</system-reminder>\s*", re.DOTALL)
_WORKBUDDY_QUERY = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL)
#: A subagent's record is ``<session>/subagents/agent-*.jsonl``; its hooks name that record's id as ``agent_id``.
_WORKBUDDY_SUBAGENT_RECORD = re.compile(r"[\\/]subagents[\\/]")


def workbuddy_person_text(text: str) -> str:
    """The person's own words in a WorkBuddy prompt or record message: the last ``<user_query>`` block once its
    ``<system-reminder>`` blocks are removed, or, without such a block, the whole text without them."""
    cleaned = _WORKBUDDY_REMINDER.sub("", text)
    queries = _WORKBUDDY_QUERY.findall(cleaned)
    return (queries[-1] if queries else cleaned).strip()


def workbuddy_record_words(text: str) -> str:
    """The person's own words in a user message of WorkBuddy's session record: every ``<user_query>`` block once its
    ``<system-reminder>`` blocks are removed, or nothing when it has none.

    WorkBuddy keeps what the person sent inside such a block, and merges messages sent while a turn ran into one
    message with a block each (its prompt hook is handed the last only).  A user message without one is WorkBuddy's
    own: a local command or a shell command and their output, a teammate's report, a slash command's expansion."""
    queries = (query.strip() for query in _WORKBUDDY_QUERY.findall(_WORKBUDDY_REMINDER.sub("", text)))
    return "\n".join(query for query in queries if query)


def is_workbuddy_agent_run(payload: dict[str, Any]) -> bool:
    """Whether a WorkBuddy hook comes from one of its subagents rather than the session the person types into.

    A subagent's record id starts with ``agent-`` and its record lies in a ``subagents`` folder.  ``agent_type`` alone
    says nothing of the kind: WorkBuddy sets it to whichever agent runs the person's own session, on every turn after
    the first, and ``agent_id`` also names the record of a session loaded from a record named otherwise."""
    agent_id, record = payload.get("agent_id"), payload.get("transcript_path")
    return (type(agent_id) is str and agent_id.strip().startswith("agent-")) or (
        type(record) is str and _WORKBUDDY_SUBAGENT_RECORD.search(record) is not None
    )


#: How Codex opens the prompt it sends through the same hook as a message to ask the model what the owner might do
#: next.  The owner never wrote it: stored as theirs it put 11,000 to 15,000 characters of Codex's instructions
#: among their messages, claims were drawn from it as if they had said them, and its recall failed on its length.
_CODEX_SUGGESTIONS_PROMPT = re.compile(r"\bhyperpersonali[sz]ed\s+suggestions?\b", re.IGNORECASE)
_CODEX_SUGGESTIONS_HEADINGS = re.compile(
    r"^#{1,2}[ \t]*(?:overview|rules|examples|bad examples|response format)[ \t]*$", re.IGNORECASE | re.MULTILINE
)
#: How the desktop app opens the shorter request it sends after each turn for one suggested next message: a frame in
#: which Codex itself says the user did not write it.  Stored as the owner's, it would add Codex's instructions to
#: their messages after every turn, and its answer would come back in recall.
_CODEX_SUGGESTED_MESSAGE_PROMPT = re.compile(
    r"This is an app-generated request for a suggested next user message\.\s+The user did not write this message\.",
    re.IGNORECASE,
)


def is_codex_suggestions_prompt(prompt: str) -> bool:
    """Whether a prompt is Codex asking the model for suggestions, not the owner's words.

    Codex asks in two ways, each told by its own frame rather than by one phrase.  The request for one suggested next
    message opens with the two sentences in which Codex says the user did not write it.  The request for several
    suggestions opens with a heading, names its "hyperpersonalized suggestions" in its first lines, runs to
    11,000-15,000 characters and carries at least three of its own headings (Overview, Rules, Examples, Bad examples,
    Response format).  The owner's words about either, even long ones, stay theirs unless they open with that frame.
    """
    text = prompt.lstrip()
    if _CODEX_SUGGESTED_MESSAGE_PROMPT.match(text) is not None:
        return True
    if not text.startswith("#") or len(text) < 8000 or _CODEX_SUGGESTIONS_PROMPT.search(text[:600]) is None:
        return False
    return len({heading.strip("# \t").lower() for heading in _CODEX_SUGGESTIONS_HEADINGS.findall(text)}) >= 3


def is_codex_suggestions_reply(message: str) -> bool:
    """Whether a reply is the model's answer to such a request: a JSON object holding only a list of suggestions, or
    only the one suggested message."""
    text = message.strip()
    if not text.startswith("{") or len(text) > _MAX_TOOL_CHARS:
        return False
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return False
    if not isinstance(value, dict) or len(value) != 1:
        return False
    if "suggestions" in value:
        return isinstance(value["suggestions"], list)
    return isinstance(value.get("suggestion"), str)


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


def without_lone_surrogates(value):
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
