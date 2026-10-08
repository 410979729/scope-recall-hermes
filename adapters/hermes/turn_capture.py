"""What the Hermes adapter does with a turn: its start, the person's message before the model call (``pre_llm``),
each tool's result, a failed request, what the model showed on the way (``post_llm``), and the finished turn
(``sync``).  It works on the adapter's state under the adapter's locks (``self._adapter``) and writes through the
adapter's ``CaptureWriter``."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .boundary import (
    extract_user_text,
    host_notice,
    interim_messages,
    interim_source_event,
    pre_llm_source_event,
    steer_messages,
    steer_source_event,
    sync_turn_source_events,
    tool_call_source_event,
)
from .tool_surface import TOOL_NAMES

if TYPE_CHECKING:
    from .provider import ScopeRecallHermesAdapter

#: The adapter's log name, which these lines carried before the adapter was split: a host writes it into each line,
#: and a logging configuration may name it.
_log = logging.getLogger("scope_recall.adapters.hermes.provider")

#: Turns whose opening message ``pre_llm_call`` stored, remembered across a compression's session switch.
_USER_CAPTURED_TURNS = 64
#: What one turn showed between tool calls, written message by message after the reply: the first ones are
#: kept, the answer is always written, and a turn past this says so (``capture_gap:interim_limit``).  With no
#: bound a turn of three hundred tool steps held the adapter lock for minutes while each waited for the store.
_INTERIM_PER_TURN = 64


def _is_scope_recall_tool_name(tool_name: object) -> bool:
    """Recognize only names routed by Hermes' registered memory provider.

    Hermes' frozen memory manager builds ``_tool_to_provider`` from each
    provider's returned schemas, rejects duplicate names, and dispatches an
    exact name to that provider.  The post-tool hook supplies no provider
    object, so this exact frozen registry surface is the strongest available
    host identity.  The result body is deliberately never inspected.
    """

    return type(tool_name) is str and tool_name in TOOL_NAMES


#: Hermes' own tools that hand back what was already said or remembered: its search over past
#: sessions and its built-in memory notes.  Their output is recall, not a new observation.  Captured
#: as one, a session search on the pilot came back as a page of old conversation, and consolidation
#: turned it into six new facts that then filled the next automatic recall.  Like Scope Recall's own
#: output it is kept as a source only.
_HOST_MEMORY_TOOL_NAMES = frozenset({"session_search", "memory"})


def _is_memory_tool_name(tool_name: object) -> bool:
    return _is_scope_recall_tool_name(tool_name) or (type(tool_name) is str and tool_name in _HOST_MEMORY_TOOL_NAMES)


class TurnCapture:
    """A Hermes adapter's turns; one per adapter."""

    def __init__(self, adapter: ScopeRecallHermesAdapter) -> None:
        self._adapter = adapter

    def note_opener(self, turn_id: str, history: object, user_message: object) -> bool:
        """Remember whether Hermes opened ``turn_id`` itself (``host_notice``), and with which text; True if it did."""
        notice = host_notice(history, user_message)
        with self._adapter._said_lock:
            self._adapter._notice_turns.pop(turn_id, None)
            if notice:
                self._adapter._notice_turns[turn_id] = extract_user_text(user_message).strip()
                while len(self._adapter._notice_turns) > _USER_CAPTURED_TURNS:
                    self._adapter._notice_turns.pop(next(iter(self._adapter._notice_turns)))
        return notice

    def opened_by_host(self, turn_id: str, user_content: str) -> bool:
        """Whether ``user_content`` is the message Hermes opened ``turn_id`` with."""
        with self._adapter._said_lock:
            opener = self._adapter._notice_turns.get(turn_id)
        return opener is not None and opener == user_content.strip()

    def start(self, turn_number: int, message: str, **kwargs) -> None:
        self._adapter._turn_counter = int(turn_number)
        ordinal_turn_id = str(kwargs.get("turn_id") or turn_number)
        skipped, self._adapter._skipped_turn_id = self._adapter._skipped_turn_id, None
        if skipped and not kwargs.get("turn_id") and not self._adapter._pre_llm_pending:
            # This turn's pre_llm_call was skipped (``_session_busy``): its turn id is the one the turn is known by.
            ordinal_turn_id = skipped
        # Hermes calls this after pre_llm_call. Preserve that UUID and its
        # current-source fence until prefetch/sync consume this turn. If no
        # UUID arrived, the ordinal is the bounded fallback.
        if not self._adapter._pre_llm_pending:
            if ordinal_turn_id != self._adapter._active_turn_id:
                self._adapter._reset_current_source_refs()
            self._adapter._active_turn_id = ordinal_turn_id
        session_id = self._adapter._effective_session_id(str(kwargs.get("session_id") or ""))
        if type(message) is str and message:
            self._adapter._current_task_message = message[:8192]
        self._adapter._outcomes.open_turn(session_id, self._adapter._active_turn_id)

    def pre_llm(self, **kwargs) -> None:
        """Capture raw current input only; never inject a second recall context."""

        identity = self._adapter._require_identity()
        if identity.read_only or not identity.runtime_audience.allowed_scope_ids:
            self._adapter._diagnostics.capability_gaps = identity.runtime_audience.capability_gaps
            return
        session_id = self._adapter._effective_session_id(str(kwargs.get("session_id") or ""))
        supplied_turn_id = str(kwargs.get("turn_id") or "").strip()
        turn_id = supplied_turn_id or self._adapter._active_turn_id or str(self._adapter._turn_counter or "turn")
        if supplied_turn_id and supplied_turn_id != self._adapter._active_turn_id:
            self._adapter._reset_current_source_refs()
            self._adapter._active_turn_id = supplied_turn_id
        self._adapter._pre_llm_pending = bool(supplied_turn_id)
        self._adapter._outcomes.open_turn(session_id, turn_id)
        current_message = kwargs.get("user_message")
        if type(current_message) is str and current_message:
            self._adapter._current_task_message = current_message[:8192]
        notice = self.note_opener(turn_id, kwargs.get("conversation_history"), current_message)
        context = identity.trusted_context(
            session_id=session_id, actor_origin="host_generated" if notice else None, mutation=True
        )
        event, gaps, ledger_identity = pre_llm_source_event(
            self._adapter._ledger,
            context,
            session_id=session_id,
            turn_id=turn_id,
            user_message=kwargs.get("user_message"),
            recorded_at=self._adapter._utc_now(),
            attachments=kwargs.get("attachments") if isinstance(kwargs.get("attachments"), list) else None,
        )
        if event is None and not gaps:
            return
        receipt = self._adapter._writer.write(
            context,
            event,
            identity=ledger_identity,
            gaps=gaps,
            scope_id=identity.local_scope_id,
        )
        if receipt is not None and receipt.durability in ("persisted", "queued"):
            self._adapter._user_captured_turns.pop(turn_id, None)
            self._adapter._user_captured_turns[turn_id] = None
            while len(self._adapter._user_captured_turns) > _USER_CAPTURED_TURNS:
                self._adapter._user_captured_turns.pop(next(iter(self._adapter._user_captured_turns)))

    def tool_result(self, **kwargs) -> None:
        """Capture one tool result; the caller holds ``_lock`` exactly once, and the store I/O runs without it.

        Hermes calls the hook for each of a step's parallel tool calls at once.  Held across its write (1.4-4.4 s on
        the shared store), one capture kept the others waiting, and those past the hook's bound were not taken:
        yuheng 6 and tianji 2 tool results on 2026-10-03.

        A call that failed is kept as well, as Codex's are.  Hermes calls a result failed for a non-zero exit code or
        an error field, and what such a call printed (a traceback, a failing test) is what the agent saw and acted
        on; dropped as having no scope, it was about 6% of the five instances' tool results, each logged as a
        failed capture.  It is stored ``partial``, which also keeps it from ending its task (``core/episodes.py``).
        """
        identity = self._adapter._require_identity()
        if identity.read_only or not identity.runtime_audience.allowed_scope_ids:
            self._adapter._diagnostics.capability_gaps = identity.runtime_audience.capability_gaps
            return
        session_id = self._adapter._effective_session_id(str(kwargs.get("session_id") or ""))
        turn_id = str(kwargs.get("turn_id") or self._adapter._active_turn_id or "turn")
        tool_call_id = str(kwargs.get("tool_call_id") or kwargs.get("id") or turn_id)
        tool_name = str(kwargs.get("tool_name") or kwargs.get("name") or "tool")
        result = kwargs.get("result") if "result" in kwargs else kwargs.get("content")
        status = str(kwargs.get("status") or kwargs.get("outcome") or "success").lower()
        outcome = "success"
        if status in {"error", "failed", "failure"}:
            outcome = "failure"
            self._adapter._outcomes.mark_failure(session_id, turn_id, reason=status)
        elif status in {"cancelled", "canceled"}:
            outcome = "cancelled"
            self._adapter._outcomes.mark_cancelled(session_id, turn_id)
        elif status in {"interrupted"}:
            outcome = "interrupted"
            self._adapter._outcomes.mark_interrupted(session_id, turn_id)
        elif result is None and "result" not in kwargs and "content" not in kwargs:
            outcome = "truncated"
            self._adapter._outcomes.mark_truncated(session_id, turn_id)
        is_memory_tool = _is_memory_tool_name(tool_name)
        captured_origin = "memory_reinjection" if is_memory_tool else "tool_observation"
        context = identity.trusted_context(session_id=session_id, actor_origin=captured_origin, mutation=True)
        event, gaps, ledger_identity = tool_call_source_event(
            self._adapter._ledger,
            context,
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            result=result,
            recorded_at=self._adapter._utc_now(),
            outcome=outcome,
            origin=captured_origin,
        )
        self._adapter._writer.write(
            context,
            event,
            identity=ledger_identity,
            gaps=gaps,
            scope_id=identity.local_scope_id,
            bound=identity,
            release=True,
        )
        self._adapter._diagnostics.pending_outcome_gaps = self._adapter._outcomes.pending_gaps()

    def request_error(self, **kwargs) -> None:
        self._adapter._require_identity()
        session_id = self._adapter._effective_session_id(str(kwargs.get("session_id") or ""))
        turn_id = str(kwargs.get("turn_id") or self._adapter._active_turn_id or "turn")
        status = str(kwargs.get("status") or kwargs.get("status_code") or "error")
        self._adapter._outcomes.mark_failure(session_id, turn_id, reason=status)
        self._adapter._diagnostics.pending_outcome_gaps = self._adapter._outcomes.pending_gaps()

    def post_llm(self, **kwargs) -> None:
        """Keep what the assistant showed on the way through this turn for ``sync_turn`` to record.

        Hermes calls this once, when a turn that has an answer ends, with a copy of the conversation, and
        before it sends the reply.  It reads that copy and writes nothing, so it takes only ``_said_lock``: under
        the adapter lock the reply waited behind whatever held it, a capture on a busy store or a recall, and
        a callback Hermes gave up on was then skipped for a minute, for every session (Hermes 0.21.5).
        """
        identity = self._adapter._require_identity()
        if identity.read_only or not identity.runtime_audience.allowed_scope_ids:
            return
        turn_id = str(kwargs.get("turn_id") or "").strip()
        if not turn_id:
            return
        answer = kwargs.get("assistant_response")
        history = kwargs.get("conversation_history")
        said = interim_messages(history, answer=answer if isinstance(answer, str) else "")
        steered = steer_messages(history)
        with self._adapter._said_lock:
            if turn_id != self._adapter._active_turn_id:
                return
            if said:
                self._adapter._interim_said[turn_id] = said
            if steered:
                self._adapter._steer_said[turn_id] = steered

    def sync(self, user_content: str, assistant_content: str, *, session_id: str) -> None:
        """Write the finished turn one capture per hold of the lock, each one's store I/O without it.

        Hermes runs this on its memory worker after the reply, with no time limit, while the next turn may already
        start.  Held for the whole turn (up to 64 interim messages, the steers, the reply and the retries, each
        waiting up to 1 s for a busy store) it kept the next turn's hooks, its start and its prefetch waiting.
        The captures keep the binding the turn was said under (``bound``).  One turn is written at a time, and a
        shutdown waits for it (the adapter's ``_sync_lock``): one that came between two captures closed the runtime
        under the rest of the turn, the reply included (review of 3.4.10).
        """
        adapter = self._adapter
        with adapter._lock:
            identity = adapter._require_identity()
            if identity.read_only:
                return
            # The turn's message and reply are dated when its writing begins: dated as each was reached, the reply
            # came after the next turn's message, written between this turn's captures (review of 3.4.10).
            said_at = adapter._utc_now()
            effective_session = adapter._effective_session_id(session_id)
            active_turn = adapter._active_turn_id
            turn_id = active_turn or str(adapter._turn_counter or "turn")
            if not identity.runtime_audience.allowed_scope_ids:
                adapter._diagnostics.capability_gaps = identity.runtime_audience.capability_gaps
                return
        # Hermes runs this on its memory worker, after the reply: the place to write again what a busy store
        # kept in memory, before the turn's own sources.
        adapter._retry.write_buffered(release=True)
        context = identity.trusted_context(session_id=effective_session, mutation=True)
        shown = identity.trusted_context(session_id=effective_session, actor_origin="assistant_visible", mutation=True)
        opened = (
            identity.trusted_context(session_id=effective_session, actor_origin="host_generated", mutation=True)
            if self.opened_by_host(turn_id, user_content)
            else context
        )
        with adapter._said_lock:
            interim, adapter._interim_said = adapter._interim_said, {}
            steers, adapter._steer_said = adapter._steer_said, {}
        limited = self._write_messages(
            interim_source_event, interim, identity, shown, effective_session, limit=_INTERIM_PER_TURN
        )
        self._write_messages(steer_source_event, steers, identity, context, effective_session)
        self._write_turn(identity, opened, shown, effective_session, turn_id, user_content, assistant_content, said_at)
        with adapter._lock:
            if adapter._active_turn_id == active_turn:
                # The next turn may have begun between these writes; its pre_llm marker is its own.
                adapter._pre_llm_pending = False
            adapter._diagnostics.pending_outcome_gaps = adapter._outcomes.pending_gaps()
            if limited:
                adapter._merge_gaps(("capture_gap:interim_limit",))

    def _write_messages(self, make_event, messages: dict, identity, context, session_id: str, *, limit=None) -> bool:
        """Each message of each turn (what the model showed between tool calls, or what the person sent while the turn
        ran), one capture per hold of the lock; with ``limit``, a turn's first messages only.  Whether a turn had
        more than ``limit``."""
        adapter = self._adapter
        limited = False
        for said_turn, said in messages.items():
            if limit is not None and len(said) > limit:
                _log.warning(
                    "scope-recall: turn %s showed %d messages between tool calls; the first %d are kept",
                    said_turn,
                    len(said),
                    limit,
                )
                limited, said = True, said[:limit]
            for ordinal, (text, occurred_at) in enumerate(said, 1):
                with adapter._lock, adapter._holding("sync_turn"):
                    event, gaps, ledger_identity = make_event(
                        adapter._ledger,
                        context,
                        session_id=session_id,
                        turn_id=said_turn,
                        ordinal=ordinal,
                        content=text,
                        recorded_at=adapter._utc_now(),
                        occurred_at=occurred_at,
                    )
                    if event is not None or gaps:
                        adapter._writer.write(
                            context,
                            event,
                            identity=ledger_identity,
                            gaps=gaps,
                            scope_id=identity.local_scope_id,
                            bound=identity,
                            release=True,
                        )
        return limited

    def _write_turn(
        self, identity, opened, shown, session_id: str, turn_id: str, user_content: str, assistant_content: str, said_at
    ) -> None:
        """The turn's message (unless ``pre_llm`` stored it) and its reply, with the turn's outcome."""
        adapter = self._adapter
        outcome = "success"
        with adapter._lock:
            if not assistant_content.strip():
                outcome = "truncated"
                adapter._outcomes.mark_truncated(session_id, turn_id)
            else:
                adapter._outcomes.mark_success(session_id, turn_id)
            event_pairs, gaps = sync_turn_source_events(
                adapter._ledger,
                opened,
                session_id=session_id,
                turn_id=turn_id,
                user_content=user_content,
                assistant_content=assistant_content,
                recorded_at=said_at,
                outcome=outcome,
                include_user=turn_id not in adapter._user_captured_turns,
            )
        for event, ledger_identity in event_pairs:
            event_context = shown if event["role"] == "assistant" else opened
            with adapter._lock, adapter._holding("sync_turn"):
                adapter._writer.write(
                    event_context,
                    event,
                    identity=ledger_identity,
                    gaps=gaps,
                    scope_id=identity.local_scope_id,
                    bound=identity,
                    release=True,
                )
