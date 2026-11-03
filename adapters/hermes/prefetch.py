"""The Hermes adapter's automatic recall for a turn (Hermes' ``prefetch``): the turn's state read under the adapter's
lock, the recall run without it, its context rendered for the model."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from scope_recall.contracts import ContractError, RecallRequest, TrustedContext
from scope_recall.core.capture_filters import without_private
from scope_recall.core.private import notice_for_mode, parse_recording_command
from scope_recall.core.recall_trace import record_refusal, record_unbound_attempt
from scope_recall.core.retrieval import AUTOMATIC_PACKET_BUDGET_UNITS

from ..host_memory import read_host_memory
from ..runtime_wiring import render_host_recall_context
from .tool_surface import display_zone
from .turn_capture import direct_owner_cli

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
        self._adapter._diagnostics.injection_tokens = 0
        self._adapter._diagnostics.injection_refs = ()
        command = parse_recording_command(query)
        if type(query) is str:
            query = without_private(query)[0]
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
            recent = (
                (without_private(self._adapter._current_task_message)[0],)
                if self._adapter._current_task_message
                else ()
            )
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
            removed = self._adapter._private_span_receipts.get((context.session_id, turn), 0)
        finally:
            self._adapter._lock.release()
        started = core.clock.monotonic()
        read_mode = getattr(core, "private_mode", None)
        if direct_owner_cli(identity) and command:
            mode = read_mode(context) if callable(read_mode) else None
            return f"Scope Recall recording mode: {mode or 'none'}."
        # An entry state that cannot be read is treated like a pause: nothing is recalled or injected, and the
        # reason stays in the diagnostics (e.g. SCHEMA_UNSUPPORTED while another process upgrades the store).
        try:
            paused = core.entry_state(context)[0] == "paused"
        except ContractError as exc:
            paused, reason = True, f"entry_state_unavailable:{exc.code}"
        except (sqlite3.Error, OSError, ValueError, RuntimeError):
            paused, reason = True, "entry_state_unavailable"
        else:
            reason = "entry_state:paused"
        if paused:
            self._adapter._diagnostics.capability_gaps = (*self._adapter._diagnostics.capability_gaps, reason)
            return ""
        mode = read_mode(context) if callable(read_mode) else None
        notice = notice_for_mode(mode) or ""
        if removed:
            notice = "\n".join(part for part in (notice, f"private_spans_removed:{removed}") if part)
        if mode == "full":
            return self._confirm(
                core, context, effective_session, turn, {}, notice, started, request_id=request["request_id"]
            )
        return self._deliver(
            query, (core, context, generation, effective_session, turn, notice, request, current_refs, identity)
        )

    def _deliver(self, query, state):
        """Prepare and confirm the ordinary turn after its source and privacy checks."""
        core, context, generation, effective_session, turn, notice, request, current_refs, identity = state
        started = core.clock.monotonic()
        briefing = self._brief(core, context, generation)
        if briefing.get("delivery_reason") == "entry_state:paused":
            return ""
        brief = briefing["text"]
        if type(query) is not str or not query.strip():
            record_refusal(core, context, query, "blank_prompt")
            text = "\n\n".join(part for part in (brief, notice) if part)
            return self._confirm(
                core, context, effective_session, turn, briefing, text, started, request_id=request["request_id"]
            )
        try:
            entries = read_host_memory(
                "hermes",
                identity.hermes_home,
                project=identity.hermes_home,
                session_started=None,
                session_id=effective_session,
                deadline=time.monotonic() + 0.2,
            )
        except Exception:  # noqa: BLE001 - optional host reading must not prevent prefetch
            entries = frozenset()
        host_context: dict[str, Any] = {"host_memory_entries": entries} if entries else {}
        try:
            packet = core.recall_packet(
                context,
                request,
                current_source_refs=current_refs,
                **host_context,
                # A day the message names is read in the zone this profile tells its model, as its memories' times are.
                zone=display_zone(),
            )
        except (ContractError, sqlite3.Error, OSError, RuntimeError, ValueError):
            if not brief:
                raise
            text = "\n\n".join(part for part in (brief, notice) if part)
            return self._confirm(
                core, context, effective_session, turn, briefing, text, started, request_id=request["request_id"]
            )
        preparation = core.prepare_recall_render(context, packet)
        current = getattr(core, "recall_packet_current", None)
        if packet.get("items") and callable(current) and not current(context, packet):
            packet = core.recall_packet(
                context, request, current_source_refs=current_refs, zone=display_zone(), **host_context
            )
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
        if packet.get("items") and callable(current) and not current(context, packet):
            return self._confirm(
                core, context, effective_session, turn, {}, notice, started, request_id=request["request_id"]
            )

        if brief:
            core.remove_question_overlaps(briefing, rendered)
            brief = briefing["text"]
        if brief:
            rendered = f"{brief}\n\n{rendered}" if rendered else brief
        if notice:
            rendered = f"{rendered}\n\n{notice}" if rendered else notice
        return self._confirm(
            core, context, effective_session, turn, briefing, rendered, started, request_id=request["request_id"]
        )

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
            if brief.get("delivery_reason") == "entry_state:paused":
                self._adapter._diagnostics.capability_gaps = (
                    *self._adapter._diagnostics.capability_gaps,
                    "entry_state:paused",
                )
            brief["_generation"] = generation
            return brief
        finally:
            self._adapter._brief_lock.release()

    def _confirm(self, core, context, session, turn, briefing, text, started, *, request_id=None):
        """Mark the delivered response after rendering, retaining a pending compaction on failure."""
        with self._adapter._lock:
            if self._adapter._active_turn_id != turn:
                return ""
            try:
                briefing = core.prepare_session_injection(
                    context, text, brief=briefing, request_id=request_id, remaining_seconds=0.2
                )
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
        diagnostics.injection_format = usage.get("format", "json")
        diagnostics.injection_refs = usage.get("references", ())
        if usage.get("reason"):
            diagnostics.last_prefetch_skip_reason = usage["reason"]
        text = usage.get("text", "")
        if reason == "entry_state:paused":
            diagnostics.injection_tokens = 0
            diagnostics.injection_refs = ()
            self._adapter._diagnostics.capability_gaps = (*self._adapter._diagnostics.capability_gaps, reason)
            return ""
        if reason in {"brief_invalidated", "brief_safety_unconfirmed"}:
            diagnostics.injection_tokens = 0
            diagnostics.injection_refs = ()
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
