"""The Hermes adapter's automatic recall for a turn (Hermes' ``prefetch``): the turn's state read under the adapter's
lock, the recall run without it, its context rendered for the model."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import TYPE_CHECKING

from scope_recall.contracts import ContractError, RecallRequest, TrustedContext
from scope_recall.core.private import notice_for_mode
from scope_recall.core.recall_trace import record_refusal, record_unbound_attempt
from scope_recall.core.retrieval import AUTOMATIC_PACKET_BUDGET_UNITS

from ..runtime_wiring import render_host_recall_context
from .tool_surface import display_zone

if TYPE_CHECKING:
    from .provider import ScopeRecallHermesAdapter

#: How long a prefetch waits for its session's state.  Hermes gives the whole prefetch 8 s and goes on without it,
#: and an automatic recall takes up to 5.
_PREFETCH_STATE_WAIT_S = 2.0


class Prefetch:
    """A Hermes adapter's turn recall; one per adapter."""

    def __init__(self, adapter: ScopeRecallHermesAdapter) -> None:
        self._adapter = adapter

    def recall(self, query: str, *, session_id: str = "") -> str:
        """Read the turn's state under the lock, recall without it.

        Hermes gives a prefetch 8 s and goes on with the turn while the call keeps running.  Held through the recall,
        the lock would keep the turn's tool hooks waiting behind it past Hermes' 30 s hook timeout (a prefetch that
        timed out can run 30-50 s).  Nothing of the turn is written meanwhile: its message was stored before, its
        tools run after.
        """
        if not self._adapter._lock.acquire(timeout=_PREFETCH_STATE_WAIT_S):
            self._adapter._calls.busy("prefetch")
            return ""
        try:
            identity = self._adapter._require_identity()
            effective_session = self._adapter._effective_session_id(session_id)
            if not identity.runtime_audience.allowed_scope_ids:
                self._adapter._diagnostics.capability_gaps = identity.runtime_audience.capability_gaps
                context = TrustedContext(
                    identity.binding,
                    identity.stored_session_id(effective_session),
                    frozenset(),
                    "human_direct",
                    entry_id=identity.entry_id,
                )
                record_unbound_attempt(self._adapter._require_core(), context, query)
                return ""
            if self._adapter._current_source_refs_overflow:
                # The overflow already reported its gap; an unfenced recall could
                # inject this turn's own sources back as memory.
                record_refusal(
                    self._adapter._require_core(),
                    identity.trusted_context(session_id=effective_session),
                    query,
                    "source_ref_limit",
                )
                return ""
            recent = (self._adapter._current_task_message,) if self._adapter._current_task_message else ()
            context = identity.trusted_context(session_id=effective_session, recent_messages=recent)
            generation = self._adapter._brief_compactions.get(effective_session, 0)
            compacted_at = self._adapter._brief_compaction_times.get(effective_session)
            if compacted_at is not None:
                context = replace(
                    context,
                    compacted_at=compacted_at,
                    compaction_seq=generation,
                    compaction_epoch=self._adapter._brief_epoch,
                )
            current_refs = tuple(self._adapter._current_source_refs)
            request = self._request(query, effective_session)
            core = self._adapter._require_core()
            turn = self._adapter._active_turn_id
        finally:
            self._adapter._lock.release()
        started = core.clock.monotonic()
        read_mode = getattr(core, "private_mode", None)
        mode = read_mode(context) if callable(read_mode) else None
        notice = notice_for_mode(mode) or ""
        if mode == "full":
            return self._confirm(core, context, effective_session, turn, {}, notice, started)
        briefing = self._brief(core, context, generation)
        brief = briefing["text"]
        if type(query) is not str or not query.strip():
            record_refusal(core, context, query, "blank_prompt")
            text = "\n\n".join(part for part in (brief, notice) if part)
            return self._confirm(core, context, effective_session, turn, briefing, text, started)
        try:
            packet = core.recall_packet(
                context,
                request,
                current_source_refs=current_refs,
                # A day the message names is read in the zone this profile tells its model, as its memories' times are.
                zone=display_zone(),
            )
        except (ContractError, sqlite3.Error, OSError, RuntimeError, ValueError):
            if not brief:
                raise
            text = "\n\n".join(part for part in (brief, notice) if part)
            return self._confirm(core, context, effective_session, turn, briefing, text, started)
        preparation = core.prepare_recall_render(context, packet)
        with self._adapter._lock:
            if self._adapter._active_turn_id == turn:
                # A turn begun meanwhile, after Hermes gave up on this call, keeps its own state.
                self._adapter._diagnostics.last_prefetch_request_id = packet["request_id"]
                self._adapter._diagnostics.last_render_ref = preparation.render_ref
                self._adapter._diagnostics.last_prefetch_skip_reason = next(
                    (gap.split(":", 1)[1] for gap in packet["gaps"] if gap.startswith("auto_recall_skipped:")), None
                )
                self._adapter._pre_llm_pending = False
        rendered = render_host_recall_context(
            preparation.canonical_text,
            context=preparation.context,
            entry=(identity.entry_id, identity.manifest.entry_name) if identity.entry_id is not None else None,
            zone=display_zone(),
        )
        if brief:
            core.remove_question_overlaps(briefing, rendered)
            brief = briefing["text"]
        if brief:
            rendered = f"{brief}\n\n{rendered}" if rendered else brief
        if notice:
            rendered = f"{rendered}\n\n{notice}" if rendered else notice
        return self._confirm(core, context, effective_session, turn, briefing, rendered, started)

    def _brief(self, core, context, generation):
        """Prepare the brief while preserving a pending compaction until confirmation."""
        empty = {"text": "", "tokens": 0, "elapsed_ms": 0}
        if not self._adapter._brief_lock.acquire(timeout=0.2):
            return empty
        try:
            try:
                brief = core.session_brief(
                    context, deliver=True, compact=bool(generation), remaining_seconds=0.2, defer_confirmation=True
                )
            except (ContractError, sqlite3.Error, OSError, ValueError):
                return empty
            brief["_generation"] = generation
            return brief
        finally:
            self._adapter._brief_lock.release()

    def _confirm(self, core, context, session, turn, briefing, text, started):
        """Mark the delivered response after rendering, retaining a pending compaction on failure."""
        with self._adapter._lock:
            if self._adapter._active_turn_id != turn:
                return ""
            try:
                briefing = core.prepare_session_injection(context, text, brief=briefing, remaining_seconds=0.2)
                briefing["_injection"]["elapsed_ms"] = max(
                    0, round((core.clock.monotonic() - started) * 1000) - briefing["elapsed_ms"]
                )
                reason = core.confirm_session_brief(context, briefing, remaining_seconds=0.1)
            except Exception:  # noqa: BLE001 - an unfinished safety check cannot release prepared text.
                reason = "brief_safety_unconfirmed"
        usage = briefing.get("_injection", {})
        diagnostics = self._adapter._diagnostics
        for name in ("injection_tokens", "session_tokens", "injection_token_cap", "session_token_cap"):
            key = "tokens" if name == "injection_tokens" else name
            if key in usage:
                setattr(diagnostics, name, usage[key])
        diagnostics.delivery_reason = reason
        if usage.get("reason"):
            diagnostics.last_prefetch_skip_reason = usage["reason"]
        text = usage.get("text", "")
        if reason in {"brief_invalidated", "brief_safety_unconfirmed"}:
            diagnostics.injection_tokens = 0
            text = ""
        if reason == "brief_delivered" and briefing.get("_generation"):
            with self._adapter._lock:
                if self._adapter._brief_compactions.get(session) == briefing["_generation"]:
                    del self._adapter._brief_compactions[session]
                    self._adapter._brief_compaction_times.pop(session, None)
        if reason in {"brief_delivery_unconfirmed", "injection_delivery_unconfirmed", "brief_safety_unconfirmed"}:
            self._adapter._diagnostics.capability_gaps = (*self._adapter._diagnostics.capability_gaps, reason)
        return text

    def _request(self, query: str, session_id: str) -> RecallRequest:
        self._adapter._require_identity()
        request_id = f"hermes-prefetch:{session_id}:{self._adapter._turn_counter}"
        payload: RecallRequest = {
            "protocol_version": "1.1",
            "request_id": request_id[:100],
            "query": query,
            "mode": "auto",
            "max_items": 6,
            "budget_tokens": AUTOMATIC_PACKET_BUDGET_UNITS,
        }
        return payload
