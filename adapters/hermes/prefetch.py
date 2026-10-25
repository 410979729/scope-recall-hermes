"""The Hermes adapter's automatic recall for a turn (Hermes' ``prefetch``): the turn's state read under the adapter's
lock, the recall run without it, its context rendered for the model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scope_recall.contracts import RecallRequest
from scope_recall.core.private import notice_for_mode
from scope_recall.core.retrieval import AUTOMATIC_PACKET_BUDGET_UNITS

from ..runtime_wiring import render_host_recall_context
from .gating import automatic_recall_skip_reason
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
                return ""
            if self._adapter._current_source_refs_overflow:
                # The overflow already reported its gap; an unfenced recall could
                # inject this turn's own sources back as memory.
                return ""
            recent = (self._adapter._current_task_message,) if self._adapter._current_task_message else ()
            context = identity.trusted_context(session_id=effective_session, recent_messages=recent)
            current_refs = tuple(self._adapter._current_source_refs)
            request = self._request(query, effective_session)
            core = self._adapter._require_core()
            turn = self._adapter._active_turn_id
        finally:
            self._adapter._lock.release()
        read_mode = getattr(core, "private_mode", None)
        mode = read_mode(context) if callable(read_mode) else None
        notice = notice_for_mode(mode) or ""
        if mode == "full":
            return notice
        reason = automatic_recall_skip_reason(query)
        if reason is not None:
            with self._adapter._lock:
                if self._adapter._active_turn_id == turn:
                    self._adapter._diagnostics.last_prefetch_skip_reason = reason
            return notice
        packet = core.recall_packet(
            context,
            request,
            current_source_refs=current_refs,
            # A day the message names is read in the zone this profile tells its model, as its memories' times are.
            zone=display_zone(),
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
        if notice:
            return f"{rendered}\n\n{notice}" if rendered else notice
        return rendered

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
