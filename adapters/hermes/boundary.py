"""Convert Hermes host callbacks into core SourceEvent DTOs at the boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
import re
from typing import Any, Literal, cast

from scope_recall.contracts import Origin, SourceEvent, TrustedContext

from .attachments import authorize_attachment_metadata


OutcomeKind = Literal["success", "failure", "cancelled", "interrupted", "truncated"]
SourceIdentity = tuple[str, int]


@dataclass
class SourceObservationLedger:
    """Per-session source identity dedupe confirmed only after Core persistence."""

    _confirmed: dict[SourceIdentity, None] = field(default_factory=dict)
    _pending: set[SourceIdentity] = field(default_factory=set)

    def observe(
        self,
        *,
        source_event_key: str,
        source_revision: int,
        role: str,
        content: str,
        origin: str,
        recorded_at: str,
        occurred_at: str | None,
        capture_state: str,
        evidence_refs: list[str] | None = None,
        artifact_refs: list[str] | None = None,
        gaps: tuple[str, ...] = (),
    ) -> tuple[SourceEvent | None, tuple[str, ...], SourceIdentity | None]:
        identity = (source_event_key, source_revision)
        if identity in self._confirmed or identity in self._pending:
            return None, gaps, None
        if len(self._pending) >= 64:
            return None, (*gaps, "capture_gap:observation_pending_capacity"), None
        self._pending.add(identity)
        event: SourceEvent = {
            "protocol_version": "1.1",
            "source_event_key": source_event_key,
            "source_revision": source_revision,
            "origin": cast(Origin, origin),
            "role": cast(Literal["user", "assistant", "tool", "system", "document", "unknown"], role),
            "content": content,
            "occurred_at": occurred_at,
            "recorded_at": recorded_at,
            "time_precision": "unknown" if occurred_at is None else "instant",
            "capture_state": cast(Literal["complete", "partial", "gap"], capture_state),
            "evidence_refs": list(evidence_refs or ()),
        }
        if artifact_refs:
            event["artifact_refs"] = list(artifact_refs)
        return event, gaps, identity

    def confirm(self, identity: SourceIdentity) -> None:
        self._pending.discard(identity)
        self._confirmed.pop(identity, None)
        self._confirmed[identity] = None
        while len(self._confirmed) > 1024:
            self._confirmed.pop(next(iter(self._confirmed)))

    def rollback(self, identity: SourceIdentity) -> None:
        self._pending.discard(identity)

    def pending_identities(self) -> tuple[SourceIdentity, ...]:
        return tuple(sorted(self._pending))

    def reset(self) -> None:
        self._confirmed.clear()
        self._pending.clear()


def host_source_key(
    *,
    installation_id: str,
    session_id: str,
    event_kind: str,
    event_id: str,
    revision: int = 1,
    entry_id: str | None = None,
) -> str:
    safe_kind = event_kind.strip() or "event"
    safe_id = event_id.strip() or "unknown"
    # Entries of a shared store share its installation id and can see the same
    # host session and turn ids -- the owner talking to two bots -- so there the
    # entry is part of the key.  A local store's keys are unchanged.
    owner = f"{installation_id}:{entry_id}" if entry_id is not None else installation_id
    return f"hermes:{owner}:{session_id}:{safe_kind}:{safe_id}@{revision}"


def extract_user_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def pre_llm_source_event(
    ledger: SourceObservationLedger,
    context: TrustedContext,
    *,
    session_id: str,
    turn_id: str,
    user_message: object,
    recorded_at: str,
    attachments: list[dict[str, Any]] | None = None,
) -> tuple[SourceEvent | None, tuple[str, ...], SourceIdentity | None]:
    content = extract_user_text(user_message)
    gaps: list[str] = []
    artifact_refs: list[str] = []
    for item in attachments or ():
        auth = authorize_attachment_metadata(item)
        if auth.authorized and auth.artifact_ref:
            artifact_refs.append(auth.artifact_ref)
        elif auth.gap:
            gaps.append(auth.gap)
    capture_state = "partial" if gaps else "complete"
    if not content.strip() and not artifact_refs:
        return None, tuple(gaps), None
    return ledger.observe(
        source_event_key=host_source_key(
            installation_id=context.binding.installation_id,
            entry_id=context.entry_id,
            session_id=session_id,
            event_kind="user",
            event_id=turn_id or "turn",
        ),
        source_revision=1,
        role="user",
        content=content,
        origin=context.actor_origin,
        recorded_at=recorded_at,
        occurred_at=recorded_at,
        capture_state=capture_state,
        artifact_refs=artifact_refs or None,
        gaps=tuple(gaps),
    )


def sync_turn_source_events(
    ledger: SourceObservationLedger,
    context: TrustedContext,
    *,
    session_id: str,
    turn_id: str,
    user_content: str,
    assistant_content: str,
    recorded_at: str,
    outcome: OutcomeKind,
    include_user: bool = True,
) -> tuple[tuple[tuple[SourceEvent, SourceIdentity | None], ...], tuple[str, ...]]:
    """The turn's opening message and its answer; ``include_user`` False when ``pre_llm_call`` stored the former."""
    gaps: list[str] = []
    if outcome != "success":
        gaps.append(f"outcome_gap:{outcome}")
    events: list[tuple[SourceEvent, SourceIdentity | None]] = []
    if include_user:
        user_event, user_gaps, user_identity = ledger.observe(
            source_event_key=host_source_key(
                installation_id=context.binding.installation_id,
                entry_id=context.entry_id,
                session_id=session_id,
                event_kind="user",
                event_id=turn_id or "turn",
            ),
            source_revision=1,
            role="user",
            content=user_content,
            origin=context.actor_origin,
            recorded_at=recorded_at,
            occurred_at=recorded_at,
            capture_state="partial" if outcome != "success" else "complete",
            gaps=tuple(gaps),
        )
        if user_event is not None and user_identity is not None:
            events.append((user_event, user_identity))
        gaps.extend(user_gaps)
    if outcome == "success" and assistant_content.strip():
        assistant_event, assistant_gaps, assistant_identity = ledger.observe(
            source_event_key=host_source_key(
                installation_id=context.binding.installation_id,
                entry_id=context.entry_id,
                session_id=session_id,
                event_kind="sync_assistant",
                event_id=turn_id or "turn",
            ),
            source_revision=1,
            role="assistant",
            content=assistant_content,
            origin="assistant_visible",
            recorded_at=recorded_at,
            occurred_at=recorded_at,
            capture_state="complete",
            gaps=tuple(gaps),
        )
        if assistant_event is not None and assistant_identity is not None:
            events.append((assistant_event, assistant_identity))
        gaps.extend(assistant_gaps)
    elif outcome == "success":
        gaps.append("outcome_gap:missing_assistant_body")
    return tuple(events), tuple(dict.fromkeys(gaps))


#: The blocks Hermes strips from what a reply shows (its ``turn_truncation._THINK_TAG_RE`` tags).
_HIDDEN_BLOCK = re.compile(
    r"<(think|thinking|reasoning|REASONING_SCRATCHPAD)\b[^>]*>.*?(?:</\1\s*>|\Z)", re.IGNORECASE | re.DOTALL
)


def _shown_text(message: dict[str, Any]) -> str:
    """What one assistant message showed: its Codex commentary items if any, else its content."""
    items = message.get("codex_message_items")
    commentary = [
        "".join(
            part["text"]
            for part in item["content"]
            if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str)
        )
        for item in (items if isinstance(items, list) else ())
        if isinstance(item, dict)
        and item.get("type") == "message"
        and isinstance(item.get("content"), list)
        and str(item.get("phase") or "").strip().lower() == "commentary"
    ]
    text: object = "\n\n".join(said.strip() for said in commentary if said.strip()) or message.get("content")
    if isinstance(text, list):
        text = "\n".join(
            part["text"]
            for part in text
            if isinstance(part, dict)
            and part.get("type") in ("text", "output_text")
            and isinstance(part.get("text"), str)
        )
    return _HIDDEN_BLOCK.sub("", text).strip() if isinstance(text, str) else ""


def _message_time(message: dict[str, Any]) -> str | None:
    """The epoch time Hermes stamps on each message it appends, in the adapter's UTC form."""
    stamp = message.get("timestamp")
    if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


#: Hermes delivers what the person sends while a turn runs (a "steer") as a user row of this kind inside the
#: turn: their words between these marker lines (``agent.prompt_builder.format_steer_marker``), after an origin
#: preamble when a gateway delivered it (``gateway.run_busy._steer_text_with_origin``).
STEER_KIND = "steer"
_STEER_OPEN = "[OUT-OF-BAND USER MESSAGE"
_STEER_CLOSE = "[/OUT-OF-BAND USER MESSAGE]"
_STEER_ORIGIN = "Gateway message origin (JSON data, not instructions or authorization):"


def _steer_words(content: object) -> str:
    """The person's words in one steer row: no marker lines, no origin preamble (chat and user ids)."""
    text = extract_user_text(content)
    start = text.find(_STEER_OPEN)
    if start != -1:
        line_end = text.find("\n", start)
        start = line_end + 1 if line_end != -1 else len(text)
    else:
        start = 0
    end = text.find(_STEER_CLOSE, start)
    words = (text[start:] if end == -1 else text[start:end]).strip()
    if words.startswith(_STEER_ORIGIN):
        # The preamble ends at its first blank line; without one nothing here is known to be the person's.
        blank = words.find("\n\n")
        words = words[blank + 2 :].strip() if blank != -1 else ""
    return words


#: Hermes' mark on a user message it folded a compression summary into (``COMPRESSED_SUMMARY_METADATA_KEY``).
_COMPRESSED_SUMMARY = "_compressed_summary"
#: The lines that bound a folded summary (``agent.context_compressor``: ``_MERGED_PRIOR_CONTEXT_HEADER``,
#: ``_MERGED_SUMMARY_DELIMITER``, ``_SUMMARY_END_MARKER``) and the header of a to-do list a compression appends to the
#: last user message (``tools.todo_tool.TODO_INJECTION_HEADER``), as Hermes 0.21.5 writes them.  Should Hermes change
#: them, a folded message is no longer recognised: the turn is then the person's, as before.
_PRIOR_CONTEXT_HEADER = "[PRIOR CONTEXT \u2014 for reference only; not a new message]"
_SUMMARY_DELIMITER = "[END OF PRIOR CONTEXT \u2014 COMPACTION SUMMARY BELOW]"
_SUMMARY_END = "--- END OF CONTEXT SUMMARY \u2014 respond to the message below, not the summary above ---"
_TODO_HEADER = "[Your active task list was preserved across context compression]"


def _own_text(message: dict[str, Any]) -> str:
    """A user message's own words, as Hermes reads them back: without a compression summary folded into a message
    Hermes marked as folded (``ContextCompressor._strip_context_summary_handoff_message``), and without a to-do list
    appended to it (``_strip_stale_todo_snapshot``).  The person's words quoted inside a summary are not the
    message's own.  An unmarked message is not unwrapped: a notice that quotes those lines keeps its words whole."""
    text = extract_user_text(message.get("content"))
    if message.get(_COMPRESSED_SUMMARY) is True:
        if _SUMMARY_DELIMITER in text:
            text = text.split(_SUMMARY_DELIMITER, 1)[0].strip()
            if text.startswith(_PRIOR_CONTEXT_HEADER):
                text = text[len(_PRIOR_CONTEXT_HEADER) :]
        elif _SUMMARY_END in text:
            text = text.split(_SUMMARY_END, 1)[1]
        else:
            return ""
    cut = text.find(_TODO_HEADER)
    return (text if cut == -1 else text[:cut]).strip()


def _notice_kind(message: dict[str, Any]) -> bool:
    """A display kind Hermes gives the messages it writes itself: any but a steer, and on a folded message not the
    legacy ``hidden`` either, which may wrap the person's words (``split_user_originated_turn``)."""
    kind = message.get("display_kind")
    if not isinstance(kind, str) or not kind or kind == STEER_KIND:
        return False
    return not (kind == "hidden" and message.get(_COMPRESSED_SUMMARY) is True)


def host_notice(history: object, user_message: object) -> bool:
    """Whether the turn ``pre_llm_call`` opens is one Hermes opened itself, not the person.

    Hermes marks the user messages it writes itself with a display kind: a finished background process, a
    delegation's result, a wake-up, a plugin's message (``gateway.response_filters.display_kind_for_event``, the
    CLI's ``TimelineNotification``).  A steer is the one kind that holds the person's words
    (``ContextCompressor._is_actionable_user_turn``).  Stored as the person's, a notice read as something they said.

    The latest user message with words of its own decides: the turn is Hermes's when those words are the turn's
    text and the message carries a notice's kind.  A to-do list a compression adds after the turn's message has no
    words of its own (``agent.turn_context.reanchor_current_turn_user_idx``).  The latest only: when Hermes put a note
    of its own before the person's message (a model switch, a timestamp), an unanswered notice with the same words
    took theirs, and so did an older folded one (reviews of 3.7.3).  A request Hermes restores after a notice
    decides in its place, and the notice stays the person's: the safe side.

    Past the last reply the message must be one Hermes folded and marked.  A compression at the turn's start can
    fold its summary into the turn's own message (``ContextCompressor._merge_summary_into_tail_row``) and put the
    reply it folded away after it (``_reply_insertion_index``): one of tianshu's three delegation results on
    2026-10-05 was stored as the owner's that way.  Anything else before the last reply belongs to an earlier turn.
    Its own words only: a summary quotes the person's messages word for word, and a message merely holding the
    turn's text took the person's words for a notice (review of 3.7.3).
    """
    text = extract_user_text(user_message).strip()
    if not isinstance(history, list) or not text:
        return False
    trailing = True
    for message in reversed(history):
        if not isinstance(message, dict) or message.get("role") != "user":
            trailing = False
            continue
        words = _own_text(message)
        if words:
            return words == text and _notice_kind(message) and (trailing or message.get(_COMPRESSED_SUMMARY) is True)
    return False


def steer_messages(history: object) -> tuple[tuple[str, str | None], ...]:
    """What the person sent while the turn that ends ``history`` ran, and when.

    ``sync_turn`` is handed only the message that opened the turn; a steer stays in the conversation.
    """
    if not isinstance(history, list):
        return ()
    said: list[tuple[str, str | None]] = []
    for message in reversed(history):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if message.get("display_kind") != STEER_KIND:
            break
        words = _steer_words(message.get("content"))
        if words:
            said.append((words, _message_time(message)))
    said.reverse()
    return tuple(said)


def interim_messages(history: object, *, answer: str) -> tuple[tuple[str, str | None], ...]:
    """What the assistant showed between its tool calls in the turn that ends ``history``, and when.

    Hermes hands ``sync_turn`` only a turn's answer; what it said on the way stays in the conversation.
    The turn is everything after the user message that opened it; a steer is inside the turn.  Its closing
    message without a tool call, and any words equal to ``answer``, are the answer and stay with
    ``sync_turn``; hidden rows and repeats are dropped.
    """
    if not isinstance(history, list):
        return ()
    turn: list[dict[str, Any]] = []
    for message in reversed(history):
        if not isinstance(message, dict):
            continue
        if message.get("role") == "user":
            if message.get("display_kind") == STEER_KIND:
                continue
            break
        if message.get("role") == "assistant" and message.get("display_kind") != "hidden":
            turn.append(message)
    turn.reverse()
    if turn and not turn[-1].get("tool_calls"):
        turn.pop()
    seen = {" ".join(answer.split())}
    said: list[tuple[str, str | None]] = []
    for message in turn:
        text = _shown_text(message)
        if text and text != "(empty)" and " ".join(text.split()) not in seen:
            seen.add(" ".join(text.split()))
            said.append((text, _message_time(message)))
    return tuple(said)


def interim_source_event(
    ledger: SourceObservationLedger,
    context: TrustedContext,
    *,
    session_id: str,
    turn_id: str,
    ordinal: int,
    content: str,
    recorded_at: str,
    occurred_at: str | None = None,
) -> tuple[SourceEvent | None, tuple[str, ...], SourceIdentity | None]:
    """One thing the assistant showed on the way through a turn, named by its turn and place in it."""
    return ledger.observe(
        source_event_key=host_source_key(
            installation_id=context.binding.installation_id,
            entry_id=context.entry_id,
            session_id=session_id,
            event_kind="interim",
            event_id=f"{turn_id or 'turn'}:{ordinal}",
        ),
        source_revision=1,
        role="assistant",
        content=content,
        origin="assistant_visible",
        recorded_at=recorded_at,
        occurred_at=occurred_at or recorded_at,
        capture_state="complete",
    )


def steer_source_event(
    ledger: SourceObservationLedger,
    context: TrustedContext,
    *,
    session_id: str,
    turn_id: str,
    ordinal: int,
    content: str,
    recorded_at: str,
    occurred_at: str | None = None,
) -> tuple[SourceEvent | None, tuple[str, ...], SourceIdentity | None]:
    """One message the person sent while a turn ran, named by its turn and place in it."""
    return ledger.observe(
        source_event_key=host_source_key(
            installation_id=context.binding.installation_id,
            entry_id=context.entry_id,
            session_id=session_id,
            event_kind="steer",
            event_id=f"{turn_id or 'turn'}:{ordinal}",
        ),
        source_revision=1,
        role="user",
        content=content,
        origin=context.actor_origin,
        recorded_at=recorded_at,
        occurred_at=occurred_at or recorded_at,
        capture_state="complete",
    )


def tool_call_source_event(
    ledger: SourceObservationLedger,
    context: TrustedContext,
    *,
    session_id: str,
    tool_call_id: str,
    tool_name: str,
    result: object,
    recorded_at: str,
    outcome: OutcomeKind,
    origin: Origin = "tool_observation",
) -> tuple[SourceEvent | None, tuple[str, ...], SourceIdentity | None]:
    gaps: list[str] = []
    if outcome != "success":
        gaps.append(f"outcome_gap:{outcome}")
    content = "" if result is None else str(result)
    if not content.strip():
        # Nothing to keep.  A call that failed, was cancelled or was cut off without a word keeps only its outcome.
        if outcome == "success":
            gaps.append("outcome_gap:missing_tool_result")
        return None, tuple(gaps), None
    capture_state = "partial" if gaps else "complete"
    return ledger.observe(
        source_event_key=host_source_key(
            installation_id=context.binding.installation_id,
            entry_id=context.entry_id,
            session_id=session_id,
            event_kind="tool",
            event_id=tool_call_id or tool_name or "tool",
        ),
        source_revision=1,
        role="tool",
        content=content,
        origin=origin,
        recorded_at=recorded_at,
        occurred_at=recorded_at,
        capture_state=capture_state,
        gaps=tuple(gaps),
    )
