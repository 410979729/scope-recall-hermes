"""Host calls this session could not take within their bound: which call holds the adapter lock and since when,
and how many calls of each kind were not taken."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .provider import ScopeRecallHermesAdapter

_log = logging.getLogger("scope_recall.adapters.hermes.provider")


class HostBackpressure:
    """The adapter's busy-call bookkeeping (``adapter._calls``); the state it keeps stays on the adapter."""

    def __init__(self, adapter: ScopeRecallHermesAdapter) -> None:
        self._adapter = adapter

    @contextmanager
    def holding(self, name: str):
        previous, self._adapter._holder = self._adapter._holder, (name, time.monotonic(), threading.get_ident())
        try:
            yield
        finally:
            # An outer call of the same thread still holds the lock; anything else is over.
            self._adapter._holder = previous if previous is not None and previous[2] == threading.get_ident() else None

    def count(self, kind: str) -> None:
        with self._adapter._said_lock:
            self._adapter._backpressure[kind] = self._adapter._backpressure.get(kind, 0) + 1

    def busy(self, kind: str, kwargs: dict[str, Any] | None = None) -> None:
        """A host call this session was too busy to take within its bound: counted and said, never waited out.

        Waited out past Hermes' hook timeout, the call was abandoned and Hermes skipped that hook for every session
        of the gateway for a minute (Hermes 0.21.5): Scope Recall registers one callback per hook.  A skipped
        ``pre_llm_call`` leaves its turn id for the turn's start: post_llm_call names the turn's interim messages
        and steers by it, and without it they were dropped.
        """
        holder = self._adapter._holder
        if kind == "pre_llm_call":
            self._adapter._skipped_turn_id = str((kwargs or {}).get("turn_id") or "").strip() or None
            if self._adapter._skipped_turn_id:
                self._adapter._turns.note_opener(
                    self._adapter._skipped_turn_id,
                    (kwargs or {}).get("conversation_history"),
                    (kwargs or {}).get("user_message"),
                )
        self.count(kind)
        _log.warning(
            "scope-recall: %s not taken: this session has been busy in %s for %.1f s",
            kind,
            holder[0] if holder else "another call",
            time.monotonic() - holder[1] if holder else 0.0,
        )

    def counts(self) -> dict[str, int]:
        with self._adapter._said_lock:
            return dict(self._adapter._backpressure)
