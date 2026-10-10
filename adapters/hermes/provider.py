"""Hermes MemoryProvider adapter that delegates recall/capture to the core boundary."""

from __future__ import annotations

import inspect
import logging
import threading
import time
from dataclasses import dataclass
from functools import wraps
from typing import Any, Dict, List, Optional

from scope_recall.core import MemoryCore

from .backpressure import HostBackpressure
from .boundary import (
    SourceObservationLedger,
)
from .capture import CAPTURE_TIMEOUT_S, CaptureWriter, label
from .capture_retry import SHUTDOWN_RETRY_SECONDS, CaptureRetry
from .identity import HermesIdentity, HermesIdentityError
from .outcomes import TurnOutcomeTracker
from .prefetch import Prefetch
from .protocol import PublicMemoryProvider
from .runtime_wiring import TrustedHostRuntime
from .session_binding import SessionBinding
from .tool_surface import HermesToolSurface
from .turn_capture import TurnCapture, steer_deadline
from .worker import AdapterWorker

_log = logging.getLogger(__name__)

#: What a shutdown waits for captures whose store I/O runs without the adapter lock.  A tool result's write took
#: 1.4-4.4 s on a busy shared store, past its own ``CAPTURE_TIMEOUT_S`` budget; 10 s covers that.
_CAPTURE_DRAIN_WAIT_S = 10.0


def _serialized_host_event(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._lock, self._calls.holding(method.__name__):
            return method(self, *args, **kwargs)

    return guarded


def _memory_provider_base():
    try:
        from agent.memory_provider import MemoryProvider  # pyright: ignore[reportMissingImports]
    except ImportError:
        return PublicMemoryProvider
    return MemoryProvider


_MemoryProviderBase = _memory_provider_base()


@dataclass
class AdapterDiagnostics:
    last_prefetch_request_id: str | None = None
    last_render_ref: str | None = None
    unsupported_fields: dict[str, str] | None = None
    pending_outcome_gaps: tuple[str, ...] = ()
    capability_gaps: tuple[str, ...] = ()
    capture_failures: tuple[str, ...] = ()
    pending_capture_identities: tuple[str, ...] = ()
    durable_pending_captures: int | None = None
    current_source_refs: tuple[str, ...] = ()
    shutdown_state: dict[str, int | str] | None = None
    #: Host calls this session did not take or took too long for, by kind: a hook or a prefetch that would have
    #: waited past its bound (``post_tool_call``, ``prefetch``), a hook that ran past the host's timeout
    #: (``post_tool_call_overran``).
    host_backpressure: dict[str, int] | None = None


class ScopeRecallHermesAdapter(HermesToolSurface, _MemoryProviderBase):  # pyright: ignore[reportGeneralTypeIssues]
    """Bounded public adapter: one prefetch recall path, capture at the DTO boundary."""

    PROVIDER_NAME = "scope-recall"

    def __init__(
        self,
        *,
        core: MemoryCore | None = None,
        host_runtime: TrustedHostRuntime | None = None,
        clock: Any | None = None,
    ) -> None:
        self._lock = threading.RLock()
        #: Guards only ``_interim_said``/``_steer_said`` and the turn they belong to, never across I/O:
        #: ``post_llm_call`` runs before Hermes sends the reply and must not wait behind a capture.
        self._said_lock = threading.Lock()
        self._identity: HermesIdentity | None = None
        self._host_runtime = host_runtime
        self._core = host_runtime.core if host_runtime is not None else core
        self._clock = clock
        self._worker = AdapterWorker()
        self._ledger = SourceObservationLedger()
        self._outcomes = TurnOutcomeTracker()
        self._turn_counter = 0
        self._active_turn_id = ""
        self._pre_llm_pending = False
        #: What the assistant showed between tool calls, and when, by turn: read at ``post_llm_call`` on the
        #: host's thread and written by ``sync_turn`` on its memory worker, where a write may wait.
        self._interim_said: dict[str, tuple[tuple[str, str | None], ...]] = {}
        #: What the person sent while a turn ran (Hermes' steers), and when, by turn; kept like ``_interim_said``.
        self._steer_said: dict[str, tuple[Any, ...]] = {}
        #: The names of the steers this process wrote (``TurnCapture._write_steers``), oldest first.
        self._steers_written: dict[str, None] = {}
        #: Turns whose opening message ``pre_llm_call`` stored: after a compression switches the session id
        #: mid-turn, ``sync_turn`` would store it again under the new session's key.
        self._user_captured_turns: dict[str, None] = {}
        #: Turns Hermes opened itself (``host_notice``), with the text of the message that opened each, kept like
        #: ``_user_captured_turns`` under ``_said_lock``: that message is stored as the host's wherever it is stored, by
        #: ``pre_llm_call`` or by ``sync_turn``.  ``sync_turn`` names its turn by the one active when it runs, which can
        #: be the next turn already, so the text decides, never the turn id alone.
        self._notice_turns: dict[str, str] = {}
        self._session_watermark = 0
        self._current_source_refs: list[str] = []
        #: This turn captured more sources than the fence holds; recall stays
        #: off until the refs reset rather than run with an incomplete fence.
        self._current_source_refs_overflow = False
        self._current_task_message = ""
        self._retry = CaptureRetry(self)
        self._writer = CaptureWriter(self)
        self._turns = TurnCapture(self)
        self._prefetch = Prefetch(self)
        self._binding = SessionBinding(self)
        self._calls = HostBackpressure(self)
        self._diagnostics = AdapterDiagnostics()
        #: What the last worker launch attempt added to capability_gaps.
        self._worker_launch_gaps: tuple[str, ...] = ()
        self._initialized = False
        #: Which call holds ``_lock``, since when, on which thread: read without the lock, to say what a call that
        #: could not wait was waiting for.
        self._holder: tuple[str, float, int] | None = None
        #: ``host_backpressure``, counted under ``_said_lock``: never across I/O.
        self._backpressure: dict[str, int] = {}
        #: Held by ``sync_turn`` for the whole turn, which takes ``_lock`` only around each capture: a shutdown waits
        #: for it (``shutdown``).
        self._sync_lock = threading.RLock()
        #: Captures whose store I/O runs without ``_lock`` (``CaptureWriter.write(release=True)``), and the condition a
        #: shutdown waits on until none is left: a tool hook's capture is not covered by ``_sync_lock``.
        self._captures_in_flight = 0
        self._captures_done = threading.Condition(self._lock)
        #: The turn id of a ``pre_llm_call`` this session was too busy to take, for the turn's start
        #: (``on_turn_start``); written without ``_lock`` by the skipped hook.
        self._skipped_turn_id: str | None = None

    @property
    def name(self) -> str:
        return self.PROVIDER_NAME

    @property
    def installation_token(self) -> str:
        if self._identity is None:
            return ""
        return self._identity.binding.installation_id

    @property
    def diagnostics(self) -> AdapterDiagnostics:
        pending = self._retry.pending_identities()
        self._diagnostics.pending_capture_identities = tuple(f"{key}@{revision}" for key, revision in pending)
        self._diagnostics.current_source_refs = tuple(self._current_source_refs)
        self._diagnostics.durable_pending_captures = self._retry.durable_pending_count()
        self._diagnostics.host_backpressure = self._calls.counts() or None
        return self._diagnostics

    def is_available(self) -> bool:
        if self._identity is None:
            return True
        manifest_path = self._identity.manifest.data_directory / "installation.json"
        db_path = self._identity.manifest.data_directory / "memory.sqlite3"
        return manifest_path.is_file() and db_path.is_file()

    def unavailable_reason(self) -> str:
        if self.is_available():
            return ""
        return "scope-recall installation manifest or database is unavailable"

    def initialize(self, session_id: str, **kwargs) -> None:
        """Bind the session Hermes opens (``SessionBinding.bind``)."""
        self._binding.bind(session_id, **kwargs)

    def _require_identity(self) -> HermesIdentity:
        if self._identity is None or not self._initialized:
            raise HermesIdentityError("adapter is not initialized")
        return self._identity

    def _require_core(self) -> MemoryCore:
        if self._core is None:
            raise HermesIdentityError("adapter core is unavailable")
        return self._core

    def _utc_now(self) -> str:
        if self._clock is not None and hasattr(self._clock, "utc_now"):
            return self._clock.utc_now()
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _effective_session_id(self, session_id: str) -> str:
        identity = self._require_identity()
        return session_id or identity.session_id

    def _merge_gaps(self, *groups: tuple[str, ...]) -> None:
        values = list(self._diagnostics.pending_outcome_gaps)
        for group in groups:
            values.extend(group)
        merged = tuple(dict.fromkeys(values))[-128:]
        self._diagnostics.pending_outcome_gaps = merged

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """The turn's automatic recall (``Prefetch.recall``)."""
        return self._prefetch.recall(query, session_id=session_id)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        return None

    @_serialized_host_event
    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        self._turns.start(turn_number, message, **kwargs)

    @_serialized_host_event
    def observe_pre_llm(self, **kwargs) -> None:
        """Capture raw current input only; never inject a second recall context."""
        self._turns.pre_llm(**kwargs)

    @_serialized_host_event
    def observe_post_tool_call(self, **kwargs) -> None:
        self._turns.tool_result(**kwargs)

    def _observe_post_tool_call(self, **kwargs) -> None:
        """A tool result, for ``hooks``, which holds ``_lock`` exactly once (``TurnCapture.tool_result``)."""
        self._turns.tool_result(**kwargs)

    @_serialized_host_event
    def observe_api_request_error(self, **kwargs) -> None:
        self._turns.request_error(**kwargs)

    def observe_post_llm_call(self, **kwargs) -> None:
        self._turns.post_llm(**kwargs)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Write the finished turn (``TurnCapture.sync``); one turn at a time, and a shutdown waits for it."""
        with self._sync_lock:
            self._turns.sync(user_content, assistant_content, session_id=session_id, messages=messages)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        # The steers first, their store I/O without the lock; then the short process launch, serialized with
        # shutdown, never the drain.
        self._turns.steers(messages, hook="on_session_end")
        with self._lock:
            self._binding.end(messages)

    @_serialized_host_event
    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        """Speak for the session Hermes switched to (``SessionBinding.switch``)."""
        self._binding.switch(
            new_session_id, parent_session_id=parent_session_id, reset=reset, rewound=rewound, **kwargs
        )

    def on_pre_compress(self, messages: List[Dict[str, Any]], **kwargs) -> str:
        # One budget for the hook's captures: what the retry pass leaves of it is the steers'.
        deadline = steer_deadline()
        with self._lock, self._calls.holding("on_pre_compress"):
            if kwargs:
                self._diagnostics.unsupported_fields = {
                    **(self._diagnostics.unsupported_fields or {}),
                    "on_pre_compress_kwargs": "ignored_in_bounded_slice",
                }
            self._retry.write_observed(deadline=deadline)
            self._binding.bounded_message_gaps(messages, hook="on_pre_compress")
        # The compression takes the steers it summarizes out of the conversation; the turn's end would not find them.
        # Written outside the hook's hold of the lock, so their store I/O runs without it.
        self._turns.steers(
            messages, session_id=str(kwargs.get("session_id") or ""), hook="on_pre_compress", deadline=deadline
        )
        with self._lock, self._calls.holding("on_pre_compress"):
            self._binding.wake_worker()
        return ""

    def shutdown(self) -> None:
        # A turn being written is finished first (``sync_turn``), as when it held the adapter lock throughout, and so
        # is a tool hook's capture whose store I/O runs without the lock, for at most ``_CAPTURE_DRAIN_WAIT_S``: its
        # bookkeeping needs the session it was said in.  One still writing after that is counted, not waited out.
        with self._sync_lock, self._lock:
            from .hooks import unregister_adapter

            unregister_adapter(self)
            deadline = time.monotonic() + _CAPTURE_DRAIN_WAIT_S
            while self._captures_in_flight and time.monotonic() < deadline:
                self._captures_done.wait(max(0.0, deadline - time.monotonic()))
            still_writing = self._captures_in_flight
            # What the buffer still holds is written once more, in the time the drain left and at least a capture's:
            # dropped here, it would be lost at every gateway restart.  An agent Hermes evicts keeps its
            # adapter without a shutdown; the retry thread writes its buffer.
            self._retry.wake.set()
            try:
                self._retry.write_buffered(
                    seconds=min(SHUTDOWN_RETRY_SECONDS, max(CAPTURE_TIMEOUT_S, deadline - time.monotonic())),
                    force=True,
                )
            except Exception as exc:  # noqa: BLE001 - the shutdown goes on; what is left is said below
                _log.warning("scope-recall: a retry of buffered captures failed at shutdown (%s)", type(exc).__name__)
            pending = self._retry.pending_identities()
            for key in tuple(self._retry.captures)[:16]:
                if key in self._retry.in_flight:
                    _log.warning("scope-recall: not stored yet (still being written at shutdown): %s", label(key))
                else:
                    _log.warning("scope-recall: not stored (still failing at shutdown), lost: %s", label(key))
            self._retry.captures.clear()
            durable_pending = self._retry.durable_pending_count()
            state = self._worker.shutdown()
            if self._host_runtime is not None:
                self._host_runtime.close()
                self._host_runtime = None
            if pending:
                state = {
                    **state,
                    "pending_captures": len(pending),
                    "pending_capture_status": "unpersisted",
                    "pending_capture_durability": "memory_only",
                }
                self._merge_gaps(("capability_gap:durable_capture_ingress_unavailable",))
            if self._diagnostics.capture_failures:
                state = {
                    **state,
                    "capture_failures": len(self._diagnostics.capture_failures),
                }
            if durable_pending:
                state.update(durable_pending_captures=durable_pending, durable_capture_status="queued_in_sqlite")
            elif durable_pending is None:
                state["durable_capture_status"] = "unknown"
            for kind, count in self._calls.counts().items():
                state[f"host_backpressure:{kind}"] = count
            if still_writing:
                state["captures_still_writing"] = still_writing
            self._diagnostics.shutdown_state = state
            self._initialized = False


def public_signatures_match(provider: PublicMemoryProvider) -> bool:
    """Return whether a fixture provider exposes the documented public surface."""

    required = {
        "name": property,
        "is_available": callable,
        "initialize": callable,
        "prefetch": callable,
        "queue_prefetch": callable,
        "sync_turn": callable,
        "on_session_end": callable,
        "on_session_switch": callable,
        "on_pre_compress": callable,
        "shutdown": callable,
        "get_tool_schemas": callable,
        "handle_tool_call": callable,
    }
    for attr, kind in required.items():
        value = getattr(provider, attr, None)
        if kind is property and not isinstance(getattr(type(provider), attr, None), property):
            return False
        if kind is callable and not callable(value):
            return False
    initialize_params = inspect.signature(provider.initialize).parameters
    if "session_id" not in initialize_params:
        return False
    if "kwargs" not in initialize_params and not any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in initialize_params.values()
    ):
        return False
    return True


def register_adapter(ctx: Any) -> ScopeRecallHermesAdapter:
    from .hooks import register_capture_hooks, unsupported_host_fields

    adapter = ScopeRecallHermesAdapter()
    adapter._diagnostics.unsupported_fields = unsupported_host_fields()
    ctx.register_memory_provider(adapter)
    register_capture_hooks(ctx, adapter)
    return adapter
