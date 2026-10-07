"""A prompt's automatic recall, for a hook: its own (``PromptRecall.own``), or the answer of the entry's resident
server when that ran its vector search (``PromptRecall.answer``); and the recall a resident server runs for a hook that
stored the prompt itself (``PromptRecall.for_resident``).  The hook's state (its diagnostics, budget, runtime and
identity) stays the handler's, reached through ``self._hook``."""

from __future__ import annotations

import sqlite3
import threading
from typing import TYPE_CHECKING, Any

from scope_recall.contracts import ContractError, RecallRequest
from scope_recall.core.retrieval import AUTOMATIC_PACKET_BUDGET_UNITS
from scope_recall.core.secret_patterns import contains_secret_like_text

from ..runtime_wiring import render_host_recall_context
from .boundary import (
    TURN_FIELD,
    is_codex_suggestions_prompt,
    is_task_notification,
    is_workbuddy_agent_run,
    is_workbuddy_notice,
    turn_id_from_payload,
    workbuddy_person_text,
)
from .config import SharedClientConfig
from .hook_answer import (
    HookDiagnostics,
    error_detail,
    recall_incomplete,
    recall_without_vectors,
    server_own_vector_fault,
)
from .session_marks import derived_turn, kept_turn

if TYPE_CHECKING:
    from .handler import CodexHookHandler

#: The longest query a recall request carries (``contracts/recall_request.schema.json``).  A longer prompt is
#: searched by its first part, as Hermes does: whole, it failed the request and the turn had no recall at all.
_RECALL_QUERY_CHARS = 8192
#: When a prompt hook that asked the entry's server (``resident_recall``) recalls as well, if the server has not
#: answered: with this much left.  The helper the hook started at its own start is ready by then.
_LOCAL_RECALL_RESERVE_S = 1.5
#: The least a server is asked with: in less it could not be found, prove itself and answer.
_RESIDENT_MIN_S = 1.0
#: What a server's recall may report of how it ended, besides its vector gap and its error.
_RESIDENT_REASONS = frozenset({"deadline_exceeded", "recall_exception", "recall_incomplete"})


class PromptRecall:
    """A hook's prompt recall; one per handler."""

    def __init__(self, hook: CodexHookHandler) -> None:
        self._hook = hook

    def answer(
        self,
        payload: dict[str, Any],
        context,
        prompt: str,
        request_id: str,
        current_refs: tuple[str, ...],
        deadline: float,
        gaps: tuple[str, ...],
    ) -> dict[str, Any]:
        """This prompt's recall from the entry's MCP server, with its vector search warm, or the hook's own.

        The server is asked from a thread and given all of the hook's time but its answer's way back.  One that has
        not answered when ``_LOCAL_RECALL_RESERVE_S`` are left is recalled alongside, with the helper this hook
        started at its own start: given only what the hook did not keep back, a recall that needed most of the time
        had none (review of rc11).  The hook then uses the answer that ran its vector search, the server's when both
        or neither did; one whose own went without it waits for the server until its own time is up.  An answer that
        failed, ran out of time or came back empty because its read did not finish (``_RESIDENT_REASONS``), or none,
        leaves the hook's own.  One without its vector search is used as it is unless what failed was the server's
        own (``server_own_vector_fault``) and this hook has a vector search: the hook then recalls as well (a server
        that lost its key recalled every prompt by words alone).  The server writes nothing: the prompt was stored
        here, so an answer that comes after the hook is done costs the turn nothing but its warm vectors."""
        remaining = self._hook.remaining(deadline)
        if remaining < _RESIDENT_MIN_S:
            return self.own(context, prompt, request_id, current_refs, deadline, gaps)
        answers: list[Any] = []
        answered = threading.Event()

        def ask() -> None:
            try:
                answers.append(self._hook.resident_recall(payload, current_refs, gaps, remaining))
            except Exception:  # noqa: BLE001 - the hook's own recall is always there to fall back on
                answers.append(None)
            finally:
                answered.set()

        threading.Thread(target=ask, name="scope-recall-resident", daemon=True).start()
        answered.wait(max(0.0, self._hook.remaining(deadline) - _LOCAL_RECALL_RESERVE_S))
        read = answered.is_set()
        server = self._taken(answers[0]) if read else None
        if server is not None and (
            server[1].get("recall_vectors") is True
            or not self._hook.has_vectors
            or not server_own_vector_fault(server[1].get("recall_vector_gap"))
        ):
            return self._used(server)
        reason = self._hook.diagnostics.last_reason
        own = self.own(context, prompt, request_id, current_refs, deadline, gaps)
        own_vectors = self._hook.diagnostics.recall_vectors is True
        if not read and not own_vectors:
            # The hook's own went without its vector search: the server's, warm, is worth what time is left.
            answered.wait(self._hook.remaining(deadline))
        if not read and answered.is_set():
            server, read = self._taken(answers[0]), True
        if server is not None and (server[1].get("recall_vectors") is True or not own_vectors):
            self._hook.diagnostics.last_reason = reason  # what the hook's own recall said is not what answered
            return self._used(server)
        if server is not None:
            gap = error_detail(server[1].get("recall_vector_gap"))
            self._hook.resident_outcome = "without_vectors" + (f":{gap}" if gap else "")
        elif not read:
            self._hook.resident_outcome = "slow" if own_vectors else "late"
        return own

    def _taken(
        self, answered: tuple[dict[str, Any], dict[str, Any]] | None
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """The server's answer and its diagnostics, or None when there is none to take (it ran out of time or
        failed, which the hook's stderr then says)."""
        if answered is None:
            return None
        result, fields = answered
        reason = fields.get("last_reason")
        if reason in _RESIDENT_REASONS:
            # Said on the hook's stderr: a server whose recalls kept failing looked healthy there (review of rc11).
            detail = error_detail(fields.get("recall_error_detail"))
            self._hook.resident_outcome = f"failed:{reason}" + (f":{detail}" if detail else "")
            return None
        return answered

    def _used(self, answered: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
        """The server's answer as this hook's, its diagnostics taken with it."""
        result, fields = answered
        for name in ("recall_vector_gap", "recall_error_detail"):
            value = fields.get(name)
            if value is None or type(value) is str:
                setattr(self._hook.diagnostics, name, error_detail(value) if value else None)
        return result if isinstance(result, dict) else {}

    def for_resident(
        self, payload: dict[str, Any], current_refs: tuple[str, ...], gaps: tuple[str, ...], remaining: float
    ) -> dict[str, Any]:
        """A prompt's automatic recall and nothing else, for the hook that stored the prompt itself
        (``local_endpoint``): the identity, audience and recall ``CodexHookHandler._user_prompt_submit`` gives it,
        in ``remaining`` seconds.  It writes nothing."""
        self._hook.diagnostics = HookDiagnostics(capability_gaps=self._hook.diagnostics.capability_gaps)
        self._hook.diagnostics.last_event = "UserPromptSubmit"
        if payload.get("hook_event_name") != "UserPromptSubmit":
            return {}
        session_id = self._hook.session_of(payload)
        audience = self._hook.audience_of(payload) if session_id is not None else None
        if audience is None:
            return {}
        turn_id, _gaps = turn_id_from_payload(payload, required=True, field=TURN_FIELD[self._hook.host])
        prompt = payload.get("prompt")
        if self._hook.host == "workbuddy":
            if type(prompt) is not str or is_workbuddy_agent_run(payload):
                return {}
            # The turn the hook kept for these words moments ago (``open_turn``), or one of this recall's own.
            prompt = workbuddy_person_text(prompt)
            turn_id = kept_turn(self._hook.config, session_id, prompt) or derived_turn(
                session_id, prompt, self._hook.clock.utc_now()
            )
        # What the hook would have recalled nothing for, or recalled without the vector channel, is not asked here;
        # the server checks again rather than take the hook's word for it.
        if (
            turn_id is None
            or type(prompt) is not str
            or not prompt.strip()
            or contains_secret_like_text(prompt)
            or (self._hook.host == "claude-code" and is_task_notification(prompt))
            or (self._hook.host == "workbuddy" and is_workbuddy_notice(prompt))
            or (self._hook.host == "codex" and is_codex_suggestions_prompt(prompt))
        ):
            return {}
        deadline = self._hook.clock.monotonic() + max(0.0, remaining)
        self._hook.ensure_runtime(audience)
        context = self._hook.context(audience, session_id, "human_direct")
        return self.own(context, prompt, f"{self._hook.host}-auto:{session_id}:{turn_id}", current_refs, deadline, gaps)

    def own(
        self,
        context,
        prompt: str,
        request_id: str,
        current_refs: tuple[str, ...],
        deadline: float,
        gaps: tuple[str, ...],
    ) -> dict[str, Any]:
        """Render this turn's automatic recall context, or nothing once the budget is gone."""
        remaining = self._hook.remaining(deadline)
        self._hook.diagnostics.recall_vectors = None
        if remaining <= 0:
            self._hook.note("deadline_exceeded")
            return {}
        request: RecallRequest = {
            "protocol_version": "1.1",
            "request_id": request_id[:100],
            "query": prompt.strip()[:_RECALL_QUERY_CHARS],
            "mode": "auto",
            "max_items": 6,
            "budget_tokens": AUTOMATIC_PACKET_BUDGET_UNITS,
        }
        try:
            packet = self._hook.core.recall_packet(
                context, request, current_source_refs=current_refs, deadline_seconds=remaining
            )
            preparation = self._hook.core.prepare_recall_render(context, packet)
        except (ContractError, OSError, RuntimeError, sqlite3.Error) as exc:
            code = getattr(exc, "code", None)
            self._hook.diagnostics.recall_error_detail = error_detail(
                f"{type(exc).__name__}:{code}" if isinstance(code, str) else type(exc).__name__
            )
            self._hook.note("recall_exception", gaps=gaps)
            return {}
        without = recall_without_vectors(packet.get("gaps") or ())
        self._hook.diagnostics.recall_vectors = without is None
        if without is not None:
            self._hook.diagnostics.recall_vector_gap = error_detail(without)
        incomplete = recall_incomplete(packet)
        if incomplete is not None:
            # Its vector search may have run, but nothing of it reached the answer: ranked as without it, a hook's own
            # empty answer beat the entry's server's finished one (review of rc11).
            self._hook.diagnostics.recall_vectors = False
            self._hook.diagnostics.recall_error_detail = error_detail(incomplete)
            self._hook.note("recall_incomplete", gaps=gaps)
        if self._hook.remaining(deadline) <= 0:
            # Nothing of its vector search reached an answer: ranked as with it, this empty answer beat a server's
            # (review of rc11).
            self._hook.diagnostics.recall_vectors = False
            self._hook.note("deadline_exceeded", gaps=gaps)
            return {}
        # In a shared store the model is told which agent it is, so another entry's items read as theirs.
        entry = (
            (self._hook.config.entry_id, self._hook.config.entry_name)
            if isinstance(self._hook.config, SharedClientConfig)
            else None
        )
        text = render_host_recall_context(preparation.canonical_text, context=preparation.context, entry=entry)
        if not text:
            return {}
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}
