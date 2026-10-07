"""The Hermes adapter's automatic recall for a turn (Hermes' ``prefetch``): the turn's state read under the adapter's
lock, the recall run without it, its context rendered for the model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scope_recall.contracts import RecallRequest
from scope_recall.core.retrieval import AUTOMATIC_PACKET_BUDGET_UNITS

from ..runtime_wiring import render_host_recall_context
from .gating import is_trivial_prompt
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
        the lock kept the turn's tool hooks waiting behind it past Hermes' 30 s hook timeout (tianji 2026-09-26,
        tianxuan 2026-09-30: the same session's prefetch timed out 48 s and 33 s before).  Nothing of the turn is
        written meanwhile: its message was stored before, its tools run after.
        """
        if not self._adapter._lock.acquire(timeout=_PREFETCH_STATE_WAIT_S):
            self._adapter._session_busy("prefetch")
            return ""
        try:
            identity = self._adapter._require_identity()
            effective_session = self._adapter._effective_session_id(session_id)
            if is_trivial_prompt(query):
                return ""
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
                self._adapter._pre_llm_pending = False
        return render_host_recall_context(
            preparation.canonical_text,
            context=preparation.context,
            entry=(identity.entry_id, identity.manifest.entry_name) if identity.entry_id is not None else None,
            zone=display_zone(),
        )

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
