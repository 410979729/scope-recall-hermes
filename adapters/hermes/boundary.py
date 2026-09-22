"""Convert Hermes host callbacks into core SourceEvent DTOs at the boundary."""
from __future__ import annotations

from dataclasses import dataclass, field
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
) -> tuple[tuple[tuple[SourceEvent, SourceIdentity | None], ...], tuple[str, ...]]:
    gaps: list[str] = []
    if outcome != "success":
        gaps.append(f"outcome_gap:{outcome}")
    events: list[tuple[SourceEvent, SourceIdentity | None]] = []
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
    if not content.strip() and outcome == "success":
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


