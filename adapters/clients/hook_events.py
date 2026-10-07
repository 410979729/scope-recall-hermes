"""What each hook event does once the handler has admitted it (its session, audience and budget): a session's start
and end, a prompt (stored, then recalled for), a turn's end (the reply stored), an interruption and a tool's output.
The hook's state stays the handler's, reached through ``self._hook``; reading the session record after a turn is
``record_reader``'s, recalling ``prompt_recall``'s."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from scope_recall.contracts import ContractError, Origin
from scope_recall.core.secret_patterns import contains_secret_like_text

from . import transcript
from .boundary import (
    TURN_FIELD,
    assistant_stop_source_event,
    authorized_attachment_refs,
    is_codex_suggestions_prompt,
    is_codex_suggestions_reply,
    is_task_notification,
    is_workbuddy_notice,
    lifecycle_source_event,
    tool_use_source_event,
    turn_id_from_payload,
    user_prompt_source_event,
    workbuddy_person_text,
)
from .session_marks import (
    close_turn,
    in_suggestions_thread,
    mark_suggestions_thread,
    note_error_reply,
    open_turn,
    words_of,
)

if TYPE_CHECKING:
    from .handler import CodexHookHandler

#: How long a capture waits for the writer lease.
CAPTURE_TIMEOUT_S = 1.0
#: The owner's own message may wait longer for the writer lease: another agent's long reply can hold it 1-2 s
#: while it is matched against the candidates, and in the work computer's first day 12 of its 55 prompts
#: waited their one second and were not stored.  It takes at most half of what the hook has left, and never
#: less than the one second every other capture waits, so a 6 s budget waits 2 s and a 2 s one still 1 s.
_PROMPT_CAPTURE_TIMEOUT_S = 2.0
#: Attaching the trusted runtime after a capture needs this much budget left.
RUNTIME_ATTACH_MIN_S = 0.3


class HookEvents:
    """A hook's handling of each event; one per handler."""

    def __init__(self, hook: CodexHookHandler) -> None:
        self._hook = hook

    def session_start(self, session_id: str, audience, deadline: float) -> bool:
        context = self._hook.context(audience, session_id, "host_generated")
        try:
            self._hook.core.status(context)
        except ContractError:
            self._hook.note("binding_unavailable")
            return False
        return True

    def session_end(self, session_id: str, audience, payload: dict[str, Any], deadline: float) -> dict[str, Any]:
        reason = payload.get("reason")
        label = reason if type(reason) is str and reason.strip() else "unknown"
        event = lifecycle_source_event(
            installation_id=self._hook.config.installation_id,
            host=self._hook.host,
            session_id=session_id,
            event_kind="session_end",
            event_id=session_id,
            content=f"session_end:{label}",
            recorded_at=self._hook.clock.utc_now(),
        )
        self._hook.capture(
            self._hook.context(audience, session_id, "host_generated"), audience, event, deadline=deadline
        )
        return {}

    def interrupt(self, session_id: str, audience, payload: dict[str, Any], deadline: float) -> dict[str, Any]:
        turn_id, gaps = turn_id_from_payload(payload, required=True, field=TURN_FIELD[self._hook.host])
        if turn_id is None:
            gaps = (*gaps, "outcome_gap:interrupt_without_turn")
        event = lifecycle_source_event(
            installation_id=self._hook.config.installation_id,
            host=self._hook.host,
            session_id=session_id,
            event_kind="interrupt",
            event_id=turn_id or session_id,
            content="interrupt:turn_stopped",
            recorded_at=self._hook.clock.utc_now(),
            gaps=gaps,
        )
        self._hook.capture(
            self._hook.context(audience, session_id, "host_generated"), audience, event, deadline=deadline, gaps=gaps
        )
        return {}

    def prompt(self, session_id: str, audience, payload: dict[str, Any], deadline: float) -> dict[str, Any]:
        taken = self._prompt_turn(session_id, payload)
        if taken is None:
            return {}
        turn_id, prompt, gaps = taken
        attachment_refs, attachment_gaps = authorized_attachment_refs(payload)
        gaps = (*gaps, *attachment_gaps)
        if attachment_gaps:
            self._hook.note("attachment_gap", gaps=attachment_gaps)
        event = user_prompt_source_event(
            installation_id=self._hook.config.installation_id,
            host=self._hook.host,
            session_id=session_id,
            turn_id=turn_id,
            prompt=prompt,
            recorded_at=self._hook.clock.utc_now(),
            gaps=gaps,
        )
        if event is not None and attachment_refs:
            event["artifact_refs"] = attachment_refs
        context = self._hook.context(audience, session_id, "human_direct")
        wait = min(_PROMPT_CAPTURE_TIMEOUT_S, max(CAPTURE_TIMEOUT_S, self._hook.remaining(deadline) / 2))
        current_refs, capture_gaps = self._hook.capture(
            context, audience, event, deadline=deadline, gaps=gaps, wait=wait
        )
        # The vector search comes with the runtime.  A prompt the store was too busy to take is recalled by
        # meaning as well: six on the work computer's two entries in one night were recalled by words alone.  One
        # the capture refused, or that holds a credential however the capture ended, goes without it, so that
        # nothing of it reaches an embedding provider.
        vectors = (
            self._hook.call.captured
            or (
                event is not None
                and not self._hook.diagnostics.capture_refused
                and not contains_secret_like_text(prompt)
            )
        ) and self._hook.remaining(deadline) >= RUNTIME_ATTACH_MIN_S
        if vectors:
            self._hook.ensure_runtime(audience)
            if self._hook.call.queued:
                self._hook.launch_worker(session_id, audience)
        if not prompt.strip():
            return {}
        if event is not None and not current_refs and not self._hook.call.queued:
            self._hook.note("capture_failed", gaps=capture_gaps)
        # The turn is recalled whether or not its message was stored; skipping here left it without memory
        # whenever the store was busy, which is when a writer holds the lease.  A message that failed or still
        # waits in the inbox is not among the sources a recall reads, so there is nothing of this turn to fence
        # out.  One refused (a credential) attached no runtime above, so its recall has no vector channel and
        # nothing of it goes to an embedding provider.
        request_id = f"{self._hook.host}-auto:{session_id}:{turn_id}"
        if vectors and self._hook.resident_recall is not None:
            return self._hook.prompt_recall.answer(
                payload, context, prompt, request_id, current_refs, deadline, capture_gaps
            )
        return self._hook.prompt_recall.own(context, prompt, request_id, current_refs, deadline, capture_gaps)

    def _prompt_turn(self, session_id: str, payload: dict[str, Any]) -> tuple[str, str, tuple[str, ...]] | None:
        """The turn and the words of a prompt this hook stores and recalls for, with its gaps; or None, said why: a
        prompt without its turn or words, a client's own notice, or Codex asking the model for suggestions."""
        if self._hook.host == "workbuddy":
            prompt = payload.get("prompt")
            if type(prompt) is not str:
                self._hook.note("missing_prompt", gaps=("capability_gap:missing_prompt",))
                return None
            # Only the person's words are stored and recalled for, never what WorkBuddy wraps around them.
            prompt = workbuddy_person_text(prompt)
            notice = is_workbuddy_notice(prompt)
            # A turn is kept for a notice too, so that the reply to it is stored under a turn of its own.
            turn_id, gaps = (
                open_turn(
                    self._hook.config, session_id, payload, None if notice else prompt, self._hook.clock.utc_now()
                ),
                (),
            )
        else:
            turn_id, gaps = turn_id_from_payload(payload, required=True, field=TURN_FIELD[self._hook.host])
            if turn_id is None:
                self._hook.note("missing_turn_id", gaps=gaps)
                return None
            prompt = payload.get("prompt")
            if type(prompt) is not str:
                self._hook.note("missing_prompt", gaps=(*gaps, "capability_gap:missing_prompt"))
                return None
            notice = self._hook.host == "claude-code" and is_task_notification(prompt)
        if notice:
            # Claude Code's own notice that a background task finished: recorded as the owner's words it
            # became a message they never wrote, and a recall on it answers nothing they asked.  WorkBuddy
            # hands its model the same notice (its ``BackgroundTaskNotifier``), and a Stop hook's or a goal's
            # request to go on.
            self._hook.note("task_notification")
            return None
        if self._hook.host == "codex" and is_codex_suggestions_prompt(prompt):
            # Codex asking the model what the owner might do next, through the hook a message comes by: not their
            # words, and nothing for a recall to answer.  The thread is marked, so that its later hooks, each a
            # process of its own, keep the rest of it out too.
            mark_suggestions_thread(self._hook.config, session_id)
            self._hook.note("host_generated_prompt")
            return None
        if self._hook.host == "codex":
            # The owner speaking in a marked thread makes the rest of it theirs.
            in_suggestions_thread(self._hook.config, session_id, ended=True)
        return turn_id, prompt, gaps

    def stop(self, session_id: str, audience, payload: dict[str, Any], deadline: float) -> dict[str, Any]:
        if self._hook.host == "dsh" and type(payload.get("last_assistant_message")) is not str:
            # A turn that ended without a reply (aborted, failed), or dsh's plugin sending what it kept of earlier
            # turns: the record lines carry whatever was said.
            self._hook.note("no_reply")
            return {}
        if self._hook.host == "workbuddy":
            reply = payload.get("last_assistant_message")
            if type(reply) is str and self._workbuddy_error_reply(session_id, payload, reply):
                # WorkBuddy hands the Stop the error it showed in place of a reply (not signed in, a model or network
                # failure) as the reply; the model said nothing, and the record marks that message with the error.
                note_error_reply(self._hook.config, session_id, reply)
                self._hook.note("client_error_reply")
                return {}
            turn_id, repeated = close_turn(
                self._hook.config, session_id, payload, reply if type(reply) is str else "", self._hook.clock.utc_now()
            )
            self._hook.call.closed_reply = (turn_id, words_of(reply)) if type(reply) is str and reply.strip() else None
            gaps = ()
            if repeated:
                # A turn that failed or was stopped before it said anything hands the Stop the reply before it.
                self._hook.note("repeated_reply")
                return {}
        else:
            turn_id, gaps = turn_id_from_payload(payload, required=True, field=TURN_FIELD[self._hook.host])
            if turn_id is None:
                self._hook.note("missing_turn_id", gaps=gaps)
                return {}
        message = payload.get("last_assistant_message")
        if type(message) is not str:
            gaps = (*gaps, "outcome_gap:missing_assistant_body")
            message = ""
        if self._hook.host == "codex" and is_codex_suggestions_reply(message):
            # The model's answer to Codex's request for suggestions (``is_codex_suggestions_prompt``).
            self._hook.note("host_generated_reply")
            return {}
        event, outcome_gaps = assistant_stop_source_event(
            installation_id=self._hook.config.installation_id,
            host=self._hook.host,
            session_id=session_id,
            turn_id=turn_id,
            message=message,
            recorded_at=self._hook.clock.utc_now(),
        )
        context = self._hook.context(audience, session_id, "assistant_visible")
        self._hook.capture(context, audience, event, deadline=deadline, gaps=(*gaps, *outcome_gaps))
        return {}

    def _workbuddy_error_reply(self, session_id: str, payload: dict[str, Any], reply: str) -> bool:
        """Whether a WorkBuddy Stop's reply is an error its record marks (``transcript.workbuddy_error_reply``).  A client
        on another machine judges it from its own record and says so (this side never opens a record for a request); a
        record that cannot be found or read says no, and the reply is stored as before."""
        if not self._hook.call.local_record:
            return self._hook.call.client_error_reply
        try:
            record = transcript.workbuddy_record_path(
                payload.get("transcript_path"), session_id, record_id=payload.get("agent_id")
            )
        except OSError:
            return False
        return record is not None and transcript.workbuddy_error_reply(record, reply)

    def post_tool_use(self, session_id: str, audience, payload: dict[str, Any], deadline: float) -> dict[str, Any]:
        turn_id, gaps = turn_id_from_payload(payload, required=True, field=TURN_FIELD[self._hook.host])
        if turn_id is None:
            self._hook.note("missing_turn_id", gaps=gaps)
            return {}
        tool_use_id = payload.get("tool_use_id")
        if type(tool_use_id) is not str or not tool_use_id.strip() or len(tool_use_id) > 240:
            self._hook.note("missing_tool_use_id", gaps=(*gaps, "capability_gap:missing_tool_use_id"))
            return {}
        tool_name = payload.get("tool_name")
        if type(tool_name) is not str or not tool_name.strip():
            self._hook.note("missing_tool_name", gaps=(*gaps, "capability_gap:missing_tool_name"))
            return {}
        event, tool_gaps, origin = tool_use_source_event(
            installation_id=self._hook.config.installation_id,
            host=self._hook.host,
            session_id=session_id,
            turn_id=turn_id,
            tool_use_id=tool_use_id.strip(),
            tool_name=tool_name.strip(),
            tool_input=payload.get("tool_input"),
            tool_response=payload.get("tool_response"),
            recorded_at=self._hook.clock.utc_now(),
        )
        if tool_gaps:
            self._hook.note("tool_payload_gap", gaps=tool_gaps)
        context = self._hook.context(audience, session_id, cast(Origin, origin))
        self._hook.capture(context, audience, event, deadline=deadline, gaps=(*gaps, *tool_gaps))
        return {}
