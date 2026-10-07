"""Hermes MemoryProvider adapter that delegates recall/capture to the core boundary."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import inspect
from functools import wraps
import logging
import sqlite3
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from scope_recall.contracts import ContractError, RecallRequest
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.retrieval import AUTOMATIC_PACKET_BUDGET_UNITS
from ..runtime_wiring import render_host_recall_context

from .boundary import (
    SourceIdentity,
    SourceObservationLedger,
)
from .gating import is_trivial_prompt
from .identity import (
    HermesIdentity,
    HermesIdentityError,
    assert_same_installation,
    bind_hermes_identity,
    switch_hermes_identity,
    unbound_session_hint,
)
from .audiences import LOCAL_PLATFORMS
from .installation import assert_core_binding_matches
from .outcomes import TurnOutcomeTracker
from .protocol import PublicMemoryProvider
from .runtime_wiring import GAP_WORKER_LAUNCH_FAILED, HermesHostRuntime, TrustedHostRuntime, attach_trusted_host_runtime
from .worker import AdapterWorker
from .capture import CAPTURE_TIMEOUT_S, GAP_CURRENT_SOURCE_REFS_LIMIT, CaptureWriter, label
from .capture_retry import SHUTDOWN_RETRY_SECONDS, CaptureRetry
from .turn_capture import TurnCapture
from .tool_surface import HermesToolSurface, display_zone

_log = logging.getLogger(__name__)

#: What a shutdown waits for captures whose store I/O runs without the adapter lock.  A tool result's write took
#: 1.4-4.4 s on the shared store (2026-10-03), past its own ``CAPTURE_TIMEOUT_S`` budget; 10 s covers that.
_CAPTURE_DRAIN_WAIT_S = 10.0
_BOUNDED_MESSAGE_SCAN = 8
#: How long a prefetch waits for its session's state.  Hermes gives the whole prefetch 8 s and goes on without it,
#: and an automatic recall takes up to 5.
_PREFETCH_STATE_WAIT_S = 2.0


def _serialized_host_event(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._lock, self._holding(method.__name__):
            return method(self, *args, **kwargs)

    return guarded


def _start_vector_helper(host_runtime) -> None:
    """Start the vector search's helper when a gateway first binds (``vector.process_store.prestart``).

    The first search opened it then, and its LanceDB import (about 2 s) could outrun that recall's budget: a probe
    run as a gateway's first turn after a start came back without its vector search (``helper_open_deadline``).
    """
    runtime = getattr(host_runtime, "runtime", None)
    if sys.platform != "win32" or runtime is None or runtime.config.vector is None:
        return
    try:
        from ...vector.process_store import prestart

        prestart()
    except OSError as exc:
        # The first search starts its own helper, as before: slower, never a reason not to bind.
        _log.warning("could not start a vector helper ahead: %s", type(exc).__name__)


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
        self._steer_said: dict[str, tuple[tuple[str, str | None], ...]] = {}
        #: Turns whose opening message ``pre_llm_call`` stored: after a compression switches the session id
        #: mid-turn, ``sync_turn`` would store it again under the new session's key.
        self._user_captured_turns: dict[str, None] = {}
        #: Turns Hermes opened itself (``host_notice``), with the text of the message that opened each, kept like
        #: ``_user_captured_turns`` under ``_said_lock``: that message is stored as the host's wherever it is stored, by
        #: ``pre_llm_call`` or by ``sync_turn``.  ``sync_turn`` names its turn by the one active when it runs, which can
        #: be the next turn already, so the text decides, never the turn id alone (review of 3.7.2).
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
        self._diagnostics = AdapterDiagnostics()
        #: What the last worker launch attempt added to capability_gaps.
        self._worker_launch_gaps: tuple[str, ...] = ()
        #: The route this session last reported as bound to no scope; a session switch on it is not reported again.
        self._unbound_route: tuple[str, ...] | None = None
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

    @contextmanager
    def _holding(self, name: str):
        previous, self._holder = self._holder, (name, time.monotonic(), threading.get_ident())
        try:
            yield
        finally:
            # An outer call of the same thread still holds the lock; anything else is over.
            self._holder = previous if previous is not None and previous[2] == threading.get_ident() else None

    def _count_backpressure(self, kind: str) -> None:
        with self._said_lock:
            self._backpressure[kind] = self._backpressure.get(kind, 0) + 1

    def _session_busy(self, kind: str, kwargs: dict[str, Any] | None = None) -> None:
        """A host call this session was too busy to take within its bound: counted and said, never waited out.

        Waited out past Hermes' hook timeout, the call was abandoned and Hermes skipped that hook for every session
        of the gateway for a minute (Hermes 0.21.5): Scope Recall registers one callback per hook.  A skipped
        ``pre_llm_call`` leaves its turn id for the turn's start: post_llm_call names the turn's interim messages
        and steers by it, and without it they were dropped (review of 3.4.10).
        """
        holder = self._holder
        if kind == "pre_llm_call":
            self._skipped_turn_id = str((kwargs or {}).get("turn_id") or "").strip() or None
            if self._skipped_turn_id:
                self._turns.note_opener(
                    self._skipped_turn_id,
                    (kwargs or {}).get("conversation_history"),
                    (kwargs or {}).get("user_message"),
                )
        self._count_backpressure(kind)
        _log.warning(
            "scope-recall: %s not taken: this session has been busy in %s for %.1f s",
            kind,
            holder[0] if holder else "another call",
            time.monotonic() - holder[1] if holder else 0.0,
        )

    def _backpressure_counts(self) -> dict[str, int]:
        with self._said_lock:
            return dict(self._backpressure)

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
        pending = self._pending_capture_identities()
        self._diagnostics.pending_capture_identities = tuple(f"{key}@{revision}" for key, revision in pending)
        self._diagnostics.current_source_refs = tuple(self._current_source_refs)
        self._diagnostics.durable_pending_captures = self._durable_pending_count()
        self._diagnostics.host_backpressure = self._backpressure_counts() or None
        return self._diagnostics

    def _durable_pending_count(self):
        if not isinstance(self._core, MemoryCore) or self._identity is None:
            return None
        try:
            context = self._identity.trusted_context()
            scopes = sorted(context.allowed_scope_ids)
            with self._core.storage.read(context, remaining_seconds=0.1) as tx:
                return (
                    tx._check()
                    .execute(
                        f"SELECT count(*) FROM capture_inbox WHERE scope_id IN ({','.join('?' for _ in scopes)}) AND project_id IS ? AND branch_id IS ?",
                        (*scopes, context.project_id, context.branch_id),
                    )
                    .fetchone()[0]
                )
        except (ContractError, OSError, RuntimeError, sqlite3.Error):
            return None

    def _pending_capture_identities(self) -> tuple[SourceIdentity, ...]:
        with self._lock:
            # Failed writes roll back the observation ledger, but their DTO
            # may still occupy the bounded memory retry buffer.
            return tuple(sorted(set(self._ledger.pending_identities()) | set(self._retry.captures)))

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
        fresh = bind_hermes_identity(session_id, **kwargs)
        with self._lock:
            assert_same_installation(self._identity, fresh)
            runtime_path = kwargs.get("trusted_runtime_config_path") or fresh.runtime_config_path
            if self._host_runtime is None:
                self._host_runtime = attach_trusted_host_runtime(
                    config_path=runtime_path,
                    expected_binding=fresh.binding,
                    session_id=fresh.session_id,
                    allowed_scope_ids=fresh.writable_scope_ids,
                    core=self._core,
                    clock=self._clock,
                )
                _start_vector_helper(self._host_runtime)
            else:
                self._host_runtime.rebind_session(
                    fresh.session_id,
                    fresh.writable_scope_ids,
                )
            self._core = self._host_runtime.core
            if self._core is None:
                self._core = MemoryCore(CoreConfig(fresh.binding), clock=self._clock)
            else:
                assert_core_binding_matches(self._core, fresh.binding)
            # An unknown or unconfigured audience is a valid fail-closed
            # capability state: initialize succeeds so the host can report
            # the gap, while no Core read/write is attempted.
            if fresh.runtime_audience.allowed_scope_ids:
                self._core.status(fresh.trusted_context())
            self._identity = fresh
            self._ledger.reset()
            self._turn_counter = 0
            self._active_turn_id = ""
            self._pre_llm_pending = False
            with self._said_lock:
                self._interim_said.clear()
                self._steer_said.clear()
                self._notice_turns.clear()
            self._user_captured_turns.clear()
            self._session_watermark = 0
            self._reset_current_source_refs()
            self._current_task_message = ""
            # The buffer stays: each capture keeps the session, actor and scope it was said in, and is written under
            # its own scope's grant (``_retry_pass``).  Cleared here, a session started again in this adapter dropped
            # tool results that had only met a busy store.
            self._diagnostics.capability_gaps = tuple(
                dict.fromkeys((*fresh.runtime_audience.capability_gaps, *self._host_runtime.capability_gaps))
            )
            self._worker_launch_gaps = ()
            self._initialized = True
            self._unbound_route = None
            self._say_if_unbound(fresh)
            from .hooks import update_adapter_binding

            update_adapter_binding(self)

    def _say_if_unbound(self, identity: HermesIdentity) -> None:
        """Say once per session, in the host's log, that a desktop or tui session binds no scope (#175).

        Such a session fails closed: nothing in it is captured or recalled.  Hermes reads none of this
        adapter's diagnostics, so without this line a Desktop login's sessions wrote nothing for days and
        nothing said so.  A gateway chat left unmapped is the owner's choice and says nothing, as before: a line
        for each would name its users, some by phone number (review of 3.4.10).  The platform, the login and the
        gap codes only, never what was said, and nothing a login could make into a line of its own.
        """
        scope = identity.scope
        if (
            scope.platform not in LOCAL_PLATFORMS
            or scope.agent_context != "primary"
            or identity.runtime_audience.allowed_scope_ids
        ):
            self._unbound_route = None
            return
        route = (scope.platform, scope.user_id, scope.chat_type, scope.chat_id, scope.thread_id, scope.agent_workspace)
        if route == self._unbound_route:
            return
        self._unbound_route = route
        said = (
            "scope-recall: session bound to no memory scope: a %s session for %s (%s); nothing in it is "
            "captured or recalled; %s"
        ) % (
            scope.platform,
            scope.user_id[:120],
            ", ".join(identity.runtime_audience.capability_gaps),
            unbound_session_hint(scope),
        )
        _log.warning("%s", "".join(character for character in said if character.isprintable()))

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

    def _recall_request(self, query: str, session_id: str) -> RecallRequest:
        self._require_identity()
        request_id = f"hermes-prefetch:{session_id}:{self._turn_counter}"
        payload: RecallRequest = {
            "protocol_version": "1.1",
            "request_id": request_id[:100],
            "query": query,
            "mode": "auto",
            "max_items": 6,
            "budget_tokens": AUTOMATIC_PACKET_BUDGET_UNITS,
        }
        return payload

    def _merge_gaps(self, *groups: tuple[str, ...]) -> None:
        values = list(self._diagnostics.pending_outcome_gaps)
        for group in groups:
            values.extend(group)
        merged = tuple(dict.fromkeys(values))[-128:]
        self._diagnostics.pending_outcome_gaps = merged

    def _reset_current_source_refs(self) -> None:
        """Open a new current-turn fence: no refs, no overflow, no overflow gap."""
        self._current_source_refs.clear()
        self._current_source_refs_overflow = False
        self._diagnostics.capability_gaps = tuple(
            gap for gap in self._diagnostics.capability_gaps if gap != GAP_CURRENT_SOURCE_REFS_LIMIT
        )

    def _replace_worker_launch_gaps(self, gaps: tuple[str, ...]) -> None:
        """This launch attempt's gaps replace the previous attempt's.

        Busy or failed describes one attempt, not the session, so it must not
        outlive a later attempt that was neither.  Identity, audience, runtime
        and turn gaps are not launch results and stay.
        """
        previous = self._worker_launch_gaps
        self._worker_launch_gaps = tuple(gaps)
        self._diagnostics.capability_gaps = tuple(
            dict.fromkeys(
                (
                    *(gap for gap in self._diagnostics.capability_gaps if gap not in previous),
                    *self._worker_launch_gaps,
                )
            )
        )

    def _wake_background_worker(self, *, context=None) -> None:
        """Use the host runtime's coalesced launcher, never drain in a hook."""
        identity = self._require_identity()
        if identity.read_only or not identity.writable_scope_ids:
            return
        runtime = self._host_runtime
        if isinstance(runtime, HermesHostRuntime) and runtime.configured:
            try:
                gaps = runtime.maybe_launch_bounded_worker(
                    session_id=identity.session_id if context is None else context.session_id,
                    allowed_scope_ids=identity.writable_scope_ids if context is None else context.allowed_scope_ids,
                    project_id=(identity.trusted_context().project_id if context is None else context.project_id),
                    branch_id=(identity.trusted_context().branch_id if context is None else context.branch_id),
                )
            except Exception:
                gaps = (GAP_WORKER_LAUNCH_FAILED,)
            self._replace_worker_launch_gaps(gaps)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Read the turn's state under the lock, recall without it.

        Hermes gives a prefetch 8 s and goes on with the turn while the call keeps running.  Held through the recall,
        the lock kept the turn's tool hooks waiting behind it past Hermes' 30 s hook timeout (tianji 2026-09-26,
        tianxuan 2026-09-30: the same session's prefetch timed out 48 s and 33 s before).  Nothing of the turn is
        written meanwhile: its message was stored before, its tools run after.
        """
        if not self._lock.acquire(timeout=_PREFETCH_STATE_WAIT_S):
            self._session_busy("prefetch")
            return ""
        try:
            identity = self._require_identity()
            effective_session = self._effective_session_id(session_id)
            if is_trivial_prompt(query):
                return ""
            if not identity.runtime_audience.allowed_scope_ids:
                self._diagnostics.capability_gaps = identity.runtime_audience.capability_gaps
                return ""
            if self._current_source_refs_overflow:
                # The overflow already reported its gap; an unfenced recall could
                # inject this turn's own sources back as memory.
                return ""
            recent = (self._current_task_message,) if self._current_task_message else ()
            context = identity.trusted_context(session_id=effective_session, recent_messages=recent)
            current_refs = tuple(self._current_source_refs)
            request = self._recall_request(query, effective_session)
            core = self._require_core()
            turn = self._active_turn_id
        finally:
            self._lock.release()
        packet = core.recall_packet(
            context,
            request,
            current_source_refs=current_refs,
            # A day the message names is read in the zone this profile tells its model, as its memories' times are.
            zone=display_zone(),
        )
        preparation = core.prepare_recall_render(context, packet)
        with self._lock:
            if self._active_turn_id == turn:
                # A turn begun meanwhile, after Hermes gave up on this call, keeps its own state.
                self._diagnostics.last_prefetch_request_id = packet["request_id"]
                self._diagnostics.last_render_ref = preparation.render_ref
                self._pre_llm_pending = False
        return render_host_recall_context(
            preparation.canonical_text,
            context=preparation.context,
            entry=(identity.entry_id, identity.manifest.entry_name) if identity.entry_id is not None else None,
            zone=display_zone(),
        )

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
            self._turns.sync(user_content, assistant_content, session_id=session_id)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        # Serialize the short process launch with shutdown, never the drain.
        with self._lock:
            self._end_session(messages)

    def _end_session(self, messages: List[Dict[str, Any]]) -> None:
        identity = self._require_identity()
        self._session_watermark += 1
        self._retry.write_observed()
        self._bounded_message_gaps(messages, hook="on_session_end")
        if identity.read_only:
            return
        if identity.writable_scope_ids:
            host_runtime = self._host_runtime
            if host_runtime is not None and host_runtime.configured:
                gaps = (GAP_WORKER_LAUNCH_FAILED,)
                try:
                    if isinstance(host_runtime, HermesHostRuntime):
                        gaps = host_runtime.maybe_launch_bounded_worker(
                            session_id=identity.session_id,
                            allowed_scope_ids=identity.writable_scope_ids,
                            project_id=identity.trusted_context().project_id,
                            branch_id=identity.trusted_context().branch_id,
                        )
                except Exception:
                    # Persisted work remains recoverable on the next wakeup.
                    pass
                self._replace_worker_launch_gaps(gaps)
                return
            elif identity.entry_id is not None:
                # A shared store is drained by its own worker, never by an entry.
                return
            else:
                # Basic mode retains the original bounded Core worker.  It is
                # still an owned wakeup; the host callback never drains in
                # the foreground lifecycle hook.
                core = self._require_core()
                context = identity.trusted_context(mutation=True)

                def drain() -> None:
                    core.drain_worker(context, max_items=8, remaining_seconds=CAPTURE_TIMEOUT_S)

            self._worker.submit(drain, kind="drain")

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
        identity = self._require_identity()
        fresh = switch_hermes_identity(identity, new_session_id, parent_session_id=parent_session_id, **kwargs)
        self._outcomes.reset_session(identity.session_id)
        self._ledger.reset()
        self._reset_current_source_refs()
        # A compression gives the conversation a new session id in the middle of a turn, and the turn goes on:
        # its id and what it said on the way stay, or its post_llm_call no longer matched the turn and what it
        # said between tool calls, and what the person sent meanwhile, was never recorded.
        if reset or kwargs.get("reason") != "compression":
            self._current_task_message = ""
            self._pre_llm_pending = False
            with self._said_lock:
                # With the turn id cleared under the same lock, a post_llm_call of the old session that arrives
                # late finds no turn to keep its copy for.
                self._active_turn_id = ""
                self._interim_said.clear()
                self._steer_said.clear()
                self._notice_turns.clear()
            self._user_captured_turns.clear()
        self._identity = fresh
        runtime_audience = fresh.runtime_audience
        self._diagnostics.capability_gaps = tuple(
            dict.fromkeys(
                (*runtime_audience.capability_gaps, *(self._host_runtime.capability_gaps if self._host_runtime else ()))
            )
        )
        self._worker_launch_gaps = ()
        self._say_if_unbound(fresh)
        if self._host_runtime is not None:
            self._host_runtime.rebind_session(new_session_id, fresh.writable_scope_ids)
        from .hooks import update_adapter_binding

        update_adapter_binding(self)
        if reset:
            self._turn_counter = 0
        self._session_watermark += 1

    @_serialized_host_event
    def on_pre_compress(self, messages: List[Dict[str, Any]], **kwargs) -> str:
        if kwargs:
            self._diagnostics.unsupported_fields = {
                **(self._diagnostics.unsupported_fields or {}),
                "on_pre_compress_kwargs": "ignored_in_bounded_slice",
            }
        self._retry.write_observed()
        self._bounded_message_gaps(messages, hook="on_pre_compress")
        self._wake_background_worker()
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
            # dropped here, it was lost at every gateway restart (2026-10-04).  An agent Hermes evicts keeps its
            # adapter without a shutdown; the retry thread writes its buffer.
            self._retry.wake.set()
            try:
                self._retry.write_buffered(
                    seconds=min(SHUTDOWN_RETRY_SECONDS, max(CAPTURE_TIMEOUT_S, deadline - time.monotonic())),
                    force=True,
                )
            except Exception as exc:  # noqa: BLE001 - the shutdown goes on; what is left is said below
                _log.warning("scope-recall: a retry of buffered captures failed at shutdown (%s)", type(exc).__name__)
            pending = self._pending_capture_identities()
            for key in tuple(self._retry.captures)[:16]:
                if key in self._retry.in_flight:
                    _log.warning("scope-recall: not stored yet (still being written at shutdown): %s", label(key))
                else:
                    _log.warning("scope-recall: not stored (still failing at shutdown), lost: %s", label(key))
            self._retry.captures.clear()
            durable_pending = self._durable_pending_count()
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
            for kind, count in self._backpressure_counts().items():
                state[f"host_backpressure:{kind}"] = count
            if still_writing:
                state["captures_still_writing"] = still_writing
            self._diagnostics.shutdown_state = state
            self._initialized = False

    def _bounded_message_gaps(self, messages: List[Dict[str, Any]], *, hook: str) -> None:
        gaps: list[str] = []
        for message in (messages or [])[-_BOUNDED_MESSAGE_SCAN:]:
            if not isinstance(message, dict):
                gaps.append(f"{hook}_gap:unsupported_message_shape")
                continue
            role = str(message.get("role") or "unknown")
            if role == "tool" and not str(message.get("content") or message.get("tool_call_id") or "").strip():
                gaps.append(f"{hook}_gap:tool_result_missing")
            if role == "assistant" and message.get("tool_calls") and not message.get("content"):
                gaps.append(f"{hook}_gap:assistant_tool_calls_without_body")
        if gaps:
            self._merge_gaps(tuple(dict.fromkeys(gaps)))


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
