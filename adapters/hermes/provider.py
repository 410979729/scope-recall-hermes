"""Hermes MemoryProvider adapter that delegates recall/capture to the core boundary."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import copy
import inspect
import json
from functools import wraps
import logging
import sqlite3
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from scope_recall.contracts import ContractError, RecallRequest, TrustedContext
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.capture_filters import sanitize_source_capture_text
from scope_recall.core.retrieval import AUTOMATIC_PACKET_BUDGET_UNITS, MAX_CURRENT_SOURCE_REFS
from ..runtime_wiring import render_host_recall_context

from .boundary import (
    SourceIdentity,
    SourceObservationLedger,
    interim_messages,
    interim_source_event,
    pre_llm_source_event,
    steer_messages,
    steer_source_event,
    sync_turn_source_events,
    tool_call_source_event,
)
from .authorization import build_ingress_authorizer
from .gating import is_trivial_prompt
from .identity import (
    HermesIdentity,
    HermesIdentityError,
    HermesRuntimeScope,
    assert_same_installation,
    bind_hermes_identity,
    host_scope_payload,
    resolve_runtime_audience,
    switch_hermes_identity,
    trusted_source_context,
    unbound_session_hint,
)
from .audiences import LOCAL_PLATFORMS
from .installation import assert_binding_matches_manifest, assert_core_binding_matches, load_binding_for_home
from .outcomes import TurnOutcomeTracker
from .protocol import PublicMemoryProvider
from .runtime_wiring import GAP_WORKER_LAUNCH_FAILED, HermesHostRuntime, TrustedHostRuntime, attach_trusted_host_runtime
from .worker import AdapterWorker
from .tool_surface import HermesToolSurface, _TOOL_NAMES, display_zone

_log = logging.getLogger(__name__)

_CAPTURE_TIMEOUT_S = 1.0
#: What a shutdown waits for captures whose store I/O runs without the adapter lock.  A tool result's write took
#: 1.4-4.4 s on the shared store (2026-10-03), past its own ``_CAPTURE_TIMEOUT_S`` budget; 10 s covers that.
_CAPTURE_DRAIN_WAIT_S = 10.0
#: What the retry thread's pass may spend, off any hook's time and off Hermes' single memory worker.  At a capture's
#: own ``_CAPTURE_TIMEOUT_S`` a pass wrote about one of up to 16 buffered tool results, each write 1-4 s on the busy
#: shared store (2026-10-04).  A turn's end keeps that 1 s: it runs on Hermes' memory worker, which the next turn's
#: writes queue behind (review of 3.6.1).
_RETRY_PASS_SECONDS = 5.0
#: What a shutdown's last pass may spend: the thread wrote again within the last ``_RETRY_EVERY_S``, and on a busy store
#: a longer pass seldom changes the outcome while it holds up a gateway's planned stop (review of 3.6.1).
_SHUTDOWN_RETRY_SECONDS = 2.0
#: How long a capture is kept to retry: one that cannot be written by then is dropped and logged as lost.  Kept for
#: good, a capture of a full inbox or of an installation whose scopes changed under a running gateway was retried every
#: ``_RETRY_EVERY_S`` for the life of the process, and its thread held an evicted agent's adapter (review of 3.6.1).
_RETRY_GIVE_UP_S = 1800.0
#: How often the retry thread writes again what the buffer holds, for as long as it holds anything.  Hermes runs
#: ``sync_turn`` only after a turn with a message and a reply: a turn it injected (a watch notification), one it
#: interrupted, or one with no reply ran no retry.  An idle agent evicted from Hermes' cache keeps its adapter without
#: a shutdown, so nothing wrote the buffer again until a gateway restart dropped it.  tianji lost 10 tool results so on
#: 2026-10-04 (``capture_failure`` logged once, never in the store).
_RETRY_EVERY_S = 30.0
_BOUNDED_MESSAGE_SCAN = 8
#: Turns whose opening message ``pre_llm_call`` stored, remembered across a compression's session switch.
_USER_CAPTURED_TURNS = 64
#: What one turn showed between tool calls, written message by message after the reply: the first ones are
#: kept, the answer is always written, and a turn past this says so (``capture_gap:interim_limit``).  With no
#: bound a turn of three hundred tool steps held the adapter lock for minutes while each waited for the store.
_INTERIM_PER_TURN = 64
GAP_CURRENT_SOURCE_REFS_LIMIT = "degraded:current_source_refs_limit"
#: How long a prefetch waits for its session's state.  Hermes gives the whole prefetch 8 s and goes on without it,
#: and an automatic recall takes up to 5.
_PREFETCH_STATE_WAIT_S = 2.0

def _serialized_host_event(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._lock, self._holding(method.__name__):
            return method(self, *args, **kwargs)
    return guarded


def _is_scope_recall_tool_name(tool_name: object) -> bool:
    """Recognize only names routed by Hermes' registered memory provider.

    Hermes' frozen memory manager builds ``_tool_to_provider`` from each
    provider's returned schemas, rejects duplicate names, and dispatches an
    exact name to that provider.  The post-tool hook supplies no provider
    object, so this exact frozen registry surface is the strongest available
    host identity.  The result body is deliberately never inspected.
    """

    return type(tool_name) is str and tool_name in _TOOL_NAMES


#: Hermes' own tools that hand back what was already said or remembered: its search over past
#: sessions and its built-in memory notes.  Their output is recall, not a new observation.  Captured
#: as one, a session search on the pilot came back as a page of old conversation, and consolidation
#: turned it into six new facts that then filled the next automatic recall.  Like Scope Recall's own
#: output it is kept as a source only.
_HOST_MEMORY_TOOL_NAMES = frozenset({"session_search", "memory"})


def _is_memory_tool_name(tool_name: object) -> bool:
    return _is_scope_recall_tool_name(tool_name) or (type(tool_name) is str and tool_name in _HOST_MEMORY_TOOL_NAMES)


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


def _same_stored_content(stored_event: dict, content: object) -> bool:
    """Whether a capture repeats what is already stored under its key.

    Storage keeps the admitted text, not the host's raw text, and splits a long
    one into segments whose first holds the prefix, so the comparison runs on
    the same admitted form.
    """
    if type(content) is not str:
        return False
    admitted = sanitize_source_capture_text(content)
    stored = stored_event.get("content")
    if type(stored) is not str:
        return False
    if "segment" in stored_event:
        return admitted[:len(stored)] == stored
    return admitted == stored


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


def _label(identity: SourceIdentity) -> str:
    """A capture's key and revision for a log line: never its content."""
    return f"{identity[0]}@{identity[1]}"[:200]


@dataclass(frozen=True)
class _RetryCapture:
    context: TrustedContext
    event: dict
    gaps: tuple[str, ...]
    scope_id: str
    host_scope: HermesRuntimeScope
    #: When it was kept (the monotonic clock), for ``_RETRY_GIVE_UP_S``.
    kept_at: float


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
        self._session_watermark = 0
        self._current_source_refs: list[str] = []
        #: This turn captured more sources than the fence holds; recall stays
        #: off until the refs reset rather than run with an incomplete fence.
        self._current_source_refs_overflow = False
        self._current_task_message = ""
        self._retry_captures: dict[SourceIdentity, _RetryCapture] = {}
        #: The retry thread (``_keep_retrying``), running while the buffer holds anything; None otherwise.  Started and
        #: cleared under ``_lock``.
        self._retry_thread: threading.Thread | None = None
        #: Set while a retry pass runs, under ``_lock``: one pass at a time writes the buffer (``_retry_buffered_captures``).
        self._retrying = False
        #: Set by ``shutdown`` to end the retry thread's wait at once.
        self._retry_wake = threading.Event()
        #: Buffered captures a pass is writing without ``_lock``: a shutdown's pass, which may overlap it, skips them.
        self._retry_in_flight: set[SourceIdentity] = set()
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
        #: Captures whose store I/O runs without ``_lock`` (``_capture_event(release=True)``), and the condition a
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
        self._count_backpressure(kind)
        _log.warning("scope-recall: %s not taken: this session has been busy in %s for %.1f s", kind,
                     holder[0] if holder else "another call", time.monotonic() - holder[1] if holder else 0.0)

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
        self._diagnostics.pending_capture_identities = tuple(
            f"{key}@{revision}" for key, revision in pending
        )
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
            with self._core.storage.read(context, remaining_seconds=.1) as tx:
                return tx._check().execute(f"SELECT count(*) FROM capture_inbox WHERE scope_id IN ({','.join('?' for _ in scopes)}) AND project_id IS ? AND branch_id IS ?",
                                          (*scopes, context.project_id, context.branch_id)).fetchone()[0]
        except (ContractError, OSError, RuntimeError, sqlite3.Error):
            return None

    def _pending_capture_identities(self) -> tuple[SourceIdentity, ...]:
        with self._lock:
            # Failed writes roll back the observation ledger, but their DTO
            # may still occupy the bounded memory retry buffer.
            return tuple(sorted(set(self._ledger.pending_identities()) | set(self._retry_captures)))

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
            self._user_captured_turns.clear()
            self._session_watermark = 0
            self._reset_current_source_refs()
            self._current_task_message = ""
            # The buffer stays: each capture keeps the session, actor and scope it was said in, and is written under
            # its own scope's grant (``_retry_pass``).  Cleared here, a session started again in this adapter dropped
            # tool results that had only met a busy store.
            self._diagnostics.capability_gaps = tuple(
                dict.fromkeys(
                    (*fresh.runtime_audience.capability_gaps, *self._host_runtime.capability_gaps)
                )
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
        if (scope.platform not in LOCAL_PLATFORMS or scope.agent_context != "primary"
                or identity.runtime_audience.allowed_scope_ids):
            self._unbound_route = None
            return
        route = (scope.platform, scope.user_id, scope.chat_type, scope.chat_id, scope.thread_id, scope.agent_workspace)
        if route == self._unbound_route:
            return
        self._unbound_route = route
        said = ("scope-recall: session bound to no memory scope: a %s session for %s (%s); nothing in it is "
                "captured or recalled; %s") % (scope.platform, scope.user_id[:120],
                                               ", ".join(identity.runtime_audience.capability_gaps),
                                               unbound_session_hint(scope))
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
        self._diagnostics.capability_gaps = tuple(dict.fromkeys((
            *(gap for gap in self._diagnostics.capability_gaps if gap not in previous),
            *self._worker_launch_gaps,
        )))

    def _record_capture_failure(self, identity: SourceIdentity | None, reason: str, *, replay: bool = False) -> None:
        if identity is not None:
            self._ledger.rollback(identity)
            label = f"{identity[0]}@{identity[1]}"
        else:
            label = "unknown"
        self._diagnostics.capture_failures = tuple(
            dict.fromkeys((*self._diagnostics.capture_failures, f"capture_failure:{label}:{reason}"))
        )[-64:]
        retried = identity in self._retry_captures
        if retried:
            self._merge_gaps(("capture_gap:retry_memory_only", "capability_gap:durable_capture_ingress_unavailable"))
        # The source's key and the failure's code only, never its content: a capture that failed used to leave
        # no trace outside this process's memory.  A retry that fails again, and is still kept, is not said again:
        # driven by the retry thread, that was a line per capture every 30 s; its end is said (stored, dropped, lost).
        (_log.debug if replay and retried else _log.warning)(
            "scope-recall: not stored (%s)%s: %s", reason, ", kept to retry" if retried else "", label[:200])

    def _capture_event(
        self,
        context,
        event,
        *,
        identity: SourceIdentity | None,
        gaps: tuple[str, ...],
        scope_id: str | None,
        remaining_seconds: float = _CAPTURE_TIMEOUT_S,
        replay: bool = False,
        bound: HermesIdentity | None = None,
        release: bool = False,
    ):
        if event is None:
            if gaps:
                self._merge_gaps(gaps)
            return None
        event = dict(event)
        # The binding the capture was said under: a turn written after its reply keeps it whatever session switch
        # came in meanwhile (``sync_turn`` passes it).
        bound = self._require_identity() if bound is None else bound
        if not replay:
            source_context = trusted_source_context(bound.scope)
            if source_context is not None:
                event["source_context"] = source_context
            else:
                event.pop("source_context", None)
        if not scope_id:
            self._record_capture_failure(identity, "capability_gap")
            self._merge_gaps(gaps, ("capability_gap:no_capture_scope",))
            return None
        started = time.monotonic()
        # What a failed write would keep to try again, made now and kept only once a write failed for a reason that
        # may pass.  Kept before the write, a capture whose store I/O runs without the lock sat in the buffer while it
        # wrote, and a retry pass of the same session wrote it a second time (review of 3.5.1).
        retry: _RetryCapture | None = None
        if identity is not None and identity not in self._retry_captures:
            if len(json.dumps(event, ensure_ascii=False).encode("utf-8")) <= 262144:
                retry = _RetryCapture(context, copy.deepcopy(event), gaps, scope_id, bound.scope, time.monotonic())

        def keep_to_retry() -> None:
            if identity is None or identity in self._retry_captures:
                return
            if retry is not None and len(self._retry_captures) < 16:
                self._retry_captures[identity] = retry
                self._start_retrying()
            else:
                self._merge_gaps(("capture_gap:retry_buffer_full",))

        host_scope = self._retry_captures[identity].host_scope if replay and identity in self._retry_captures else bound.scope
        failure, holder = None, self._holder
        if release:
            # The store I/O without the lock, which the caller (``sync_turn``, a tool hook) holds exactly once.  Held
            # across the write, it was taken straight back by this thread as it released it: the next turn's start
            # got in after 2 of 14 such captures (measured on 3.4.9).  Only the store is touched until it is taken
            # again.
            self._captures_in_flight += 1
            self._holder = None
            self._lock.release()
        try:
            # The bounded host cache is only an optimization. SQLite retains
            # first-witnessed time after an old identity leaves that cache.
            # Only a replay of the same message inherits it: a restarted gateway
            # numbers turns from 1 again, so a different message can arrive under
            # an old key.  Storage re-keys that one, and it must keep its own time.
            if identity is not None:
                previous = self._require_core().source_by_event_key(context, identity[0], identity[1],
                            remaining_seconds=max(.001, remaining_seconds - (time.monotonic() - started)))
                if (previous is not None and previous.scope_id == scope_id
                        and previous.session_id == context.session_id
                        and previous.project_id == context.project_id and previous.branch_id == context.branch_id
                        and _same_stored_content(previous.event, event.get("content"))):
                    for field in ("occurred_at", "recorded_at", "time_precision"):
                        if field in previous.event:
                            event[field] = previous.event[field]
            core = self._require_core()
            if isinstance(core, MemoryCore):
                receipt = core.record_host_event(context, event, scope_id=scope_id,
                    host_scope=host_scope_payload(host_scope),
                    remaining_seconds=max(.001, remaining_seconds - (time.monotonic() - started)))
            else:
                receipt = core.record_event(context, event, scope_id=scope_id,
                    remaining_seconds=max(.001, remaining_seconds - (time.monotonic() - started)))
        except (ContractError, OSError, RuntimeError, sqlite3.Error) as exc:
            failure = exc
        finally:
            if release:
                self._lock.acquire()
                self._holder = holder and (holder[0], time.monotonic(), holder[2])
                self._captures_in_flight -= 1
                self._captures_done.notify_all()
        if release and not self._initialized:
            # A shutdown stopped waiting for this write and closed the session meanwhile.  The write itself may well
            # have landed (the store is not closed with the session); its bookkeeping belongs to a closed session,
            # and raised into the host's hook runner (review of 3.5.1).
            if failure is None and identity is not None and receipt.durability in ("persisted", "queued"):
                self._ledger.confirm(identity)
                self._retry_captures.pop(identity, None)
            if replay and identity is not None:
                # Said "still being written" at shutdown: its end is said here (review of 3.6.1).
                if failure is None and receipt.durability in ("persisted", "queued"):
                    _log.info("scope-recall: %s on retry: %s",
                              "stored" if receipt.durability == "persisted" else "queued", _label(identity))
                else:
                    _log.warning("scope-recall: not stored (still failing at shutdown), lost: %s", _label(identity))
            return None if failure is not None else receipt
        if failure is not None:
            if isinstance(failure, ContractError) and failure.code not in {"DEADLINE_EXCEEDED", "STORAGE_UNAVAILABLE"}:
                self._retry_captures.pop(identity, None)
            else:
                keep_to_retry()
            self._record_capture_failure(identity, "exception", replay=replay)
            self._merge_gaps(gaps, ("capture_gap:write_exception",))
            return None
        if receipt.durability != "persisted":
            if receipt.durability == "queued":
                self._retry_captures.pop(identity, None)
                if identity is not None:
                    # Durably queued: the worker stores it from the inbox.  Left pending, it held one of the
                    # ledger's 64 slots until the session ended, and a full ledger refused every capture.
                    self._ledger.confirm(identity)
                    if replay:
                        _log.info("scope-recall: queued on retry: %s", _label(identity))
                self._merge_gaps(gaps, ("capture_gap:durable_ingress_pending",))
                self._wake_background_worker(context=context)
                return receipt
            if receipt.disposition in {"rejected", "conflict", "cancelled"}:
                self._retry_captures.pop(identity, None)
            else:
                keep_to_retry()
            self._record_capture_failure(identity, receipt.error_code or receipt.disposition, replay=replay)
            self._merge_gaps(gaps, (f"capture_gap:{receipt.disposition}",))
            return receipt
        if identity is not None:
            self._ledger.confirm(identity)
            self._retry_captures.pop(identity, None)
            if replay:
                # Said once, as the capture's being kept was: the pair shows what the retries saved.
                _log.info("scope-recall: stored on retry: %s", _label(identity))
        for write in receipt.event_refs if context.session_id == self._require_identity().stored_session_id() else ():
            ref = f"{write.ref}@{write.revision}"
            if ref in self._current_source_refs:
                continue
            if len(self._current_source_refs) < MAX_CURRENT_SOURCE_REFS:
                self._current_source_refs.append(ref)
            elif not self._current_source_refs_overflow:
                # A ref the fence cannot hold would let this turn's own source
                # come back as memory, so recall stays off until the refs
                # reset.  The gap goes where tool replies read it.
                self._current_source_refs_overflow = True
                self._diagnostics.capability_gaps = (*self._diagnostics.capability_gaps, GAP_CURRENT_SOURCE_REFS_LIMIT)
        if gaps:
            self._merge_gaps(gaps)
        self._wake_background_worker(context=context)
        return receipt

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

    def _retry_observed_captures(self) -> None:
        """Retry only previously observed DTOs with their original identities.

        Raw history without event IDs is deliberately not promoted to new user
        evidence.  This also avoids re-saving compacted summaries as originals.
        """
        identity = self._require_identity()
        core = self._require_core()
        if isinstance(core, MemoryCore) and not identity.read_only:
            try:
                from ...core.capture_inbox import INGRESS_PENDING_GAP, replay_inbox
                receipts = replay_inbox(core.storage, core.clock, identity.trusted_context(),
                    authorize=build_ingress_authorizer(identity.binding), admission_policy=core.config.admission_policy,
                    remaining_seconds=_CAPTURE_TIMEOUT_S)
            except (ContractError, OSError, RuntimeError, sqlite3.Error, ValueError):
                self._merge_gaps(("capture_gap:durable_ingress_pending",))
            else:
                # A busy store stops the replay's page with a receipt that says so, where it used to raise.
                if any(INGRESS_PENDING_GAP in receipt.gaps for receipt in receipts):
                    self._merge_gaps((INGRESS_PENDING_GAP,))
        self._retry_buffered_captures()

    def _retry_buffered_captures(self, *, release: bool = False, seconds: float = _CAPTURE_TIMEOUT_S,
                                 force: bool = False) -> None:
        """Write again what a busy store kept in memory; nothing to do, and nothing opened, when it holds none.

        Run at every ``sync_turn``, by the retry thread while the buffer holds anything (``_keep_retrying``), at the
        session's end, before a compression and at shutdown: waiting for the turns left a capture that timed out on
        the writer lease in memory for hours, and an eviction or a restart lost it.  One pass at a time; ``force`` is
        shutdown's, which may run while another pass waits for the lock between two captures.  One retry per hold of
        the lock; with ``release`` (``sync_turn`` and the retry thread, which do not hold it) each writes without.
        """
        with self._lock:
            if not self._retry_captures or (self._retrying and not force):
                return
            identity = self._require_identity()
            pending_items = tuple(self._retry_captures.items())
            self._retrying = True
        try:
            self._retry_pass(identity, pending_items, deadline=time.monotonic() + seconds, release=release,
                             force=force)
        finally:
            with self._lock:
                self._retrying = False

    def _retry_pass(self, identity: HermesIdentity, pending_items: tuple, *, deadline: float, release: bool,
                    force: bool) -> None:
        try:
            manifest = load_binding_for_home(identity.hermes_home)
            assert_binding_matches_manifest(identity.binding, manifest)
        except (HermesIdentityError, ContractError, OSError, ValueError, TypeError):
            with self._lock:
                self._merge_gaps(("capture_gap:retry_authorization_unverified",))
                expired = self._give_up_expired(pending_items)
            if len(expired) < len(pending_items):
                _log.warning("scope-recall: %d buffered capture(s) not written again now: their authorization could "
                             "not be read", len(pending_items) - len(expired))
            return
        for key, pending in pending_items:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            with self._lock, self._holding("retry_buffered_captures"):
                if self._identity is not identity or not (self._initialized or force):
                    # A session switch or a shutdown came in between: what is left stays for the next pass.
                    break
                # A capture another pass is writing (a shutdown's pass may overlap the thread's) is left to it.
                if self._retry_captures.get(key) is not pending or key in self._retry_in_flight:
                    continue
                if self._give_up_expired(((key, pending),)):
                    continue
                # The capture's own audience as the manifest grants it now: a scope taken away since is not written
                # to.  Not the current session's: kept across a session switch, a direct message's tool result written
                # again while the agent served a group would have been dropped as revoked.
                try:
                    audience = resolve_runtime_audience(manifest, pending.host_scope)
                except (HermesIdentityError, ContractError, ValueError, TypeError):
                    audience = None
                allowed_scopes = (pending.context.allowed_scope_ids & audience.writable_scope_ids
                                  if audience is not None else frozenset())
                if pending.context.binding != identity.binding or pending.scope_id not in allowed_scopes:
                    self._retry_captures.pop(key, None)
                    self._ledger.rollback(key)
                    self._merge_gaps(("capture_gap:retry_authorization_revoked",))
                    _log.warning("scope-recall: not stored (authorization revoked), dropped: %s", _label(key))
                    continue
                # Not the current session's read-only state either: a capture is buffered only after a write its own
                # session was allowed, and one kept while a read-only session (an unknown user, a delegated agent)
                # was current waited and was dropped at shutdown.
                # Narrow current authorization only. Original actor, session,
                # project, branch, occurrence time and DTO identity stay intact.
                context = replace(pending.context, allowed_scope_ids=frozenset(allowed_scopes))
                self._retry_in_flight.add(key)
                try:
                    self._capture_event(context, pending.event, identity=key, gaps=pending.gaps,
                                        scope_id=pending.scope_id, remaining_seconds=remaining, replay=True,
                                        bound=identity, release=release)
                finally:
                    self._retry_in_flight.discard(key)

    def _give_up_expired(self, items) -> list:
        """Drop the buffered captures kept longer than ``_RETRY_GIVE_UP_S``, each logged as lost; the caller holds
        ``_lock``.  Returns their keys."""
        expired = [key for key, pending in items
                   if self._retry_captures.get(key) is pending and key not in self._retry_in_flight
                   and time.monotonic() - pending.kept_at > _RETRY_GIVE_UP_S]
        for key in expired:
            self._retry_captures.pop(key, None)
            self._ledger.rollback(key)
            self._merge_gaps(("capture_gap:retry_gave_up",))
            _log.warning("scope-recall: not stored (still failing after %d minutes), lost: %s",
                         int(_RETRY_GIVE_UP_S // 60), _label(key))
        return expired

    def _start_retrying(self) -> None:
        """Start the retry thread when none runs; the caller holds ``_lock``."""
        if self._retry_thread is not None:
            return
        self._retry_wake.clear()
        thread = threading.Thread(target=self._keep_retrying, name="scope-recall-capture-retry", daemon=True)
        self._retry_thread = thread
        thread.start()

    def _keep_retrying(self) -> None:
        """Write the buffer again every ``_RETRY_EVERY_S``, off any hook's time, until it is empty or the adapter is
        shut down: Hermes runs no ``sync_turn`` after a turn it injected, interrupted or got no reply for."""
        while True:
            if self._retry_wake.wait(_RETRY_EVERY_S):
                self._retry_wake.clear()
            with self._lock:
                if not self._initialized or not self._retry_captures:
                    self._retry_thread = None
                    return
            try:
                self._retry_buffered_captures(release=True, seconds=_RETRY_PASS_SECONDS)
            except Exception as exc:  # noqa: BLE001 - the next pass, a turn's end or the shutdown writes it
                _log.warning("scope-recall: a retry of buffered captures failed (%s)", type(exc).__name__)
                # A pass that raised before its captures' own check still gives up the expired ones: it would hold
                # them, and an evicted agent's adapter, for good (review of 3.6.1).
                with self._lock:
                    self._give_up_expired(tuple(self._retry_captures.items()))

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
            preparation.canonical_text, context=preparation.context,
            entry=(identity.entry_id, identity.manifest.entry_name) if identity.entry_id is not None else None,
            zone=display_zone(),
        )

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        return None

    @_serialized_host_event
    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        self._turn_counter = int(turn_number)
        ordinal_turn_id = str(kwargs.get("turn_id") or turn_number)
        skipped, self._skipped_turn_id = self._skipped_turn_id, None
        if skipped and not kwargs.get("turn_id") and not self._pre_llm_pending:
            # This turn's pre_llm_call was skipped (``_session_busy``): its turn id is the one the turn is known by.
            ordinal_turn_id = skipped
        # Hermes calls this after pre_llm_call. Preserve that UUID and its
        # current-source fence until prefetch/sync consume this turn. If no
        # UUID arrived, the ordinal is the bounded fallback.
        if not self._pre_llm_pending:
            if ordinal_turn_id != self._active_turn_id:
                self._reset_current_source_refs()
            self._active_turn_id = ordinal_turn_id
        session_id = self._effective_session_id(str(kwargs.get("session_id") or ""))
        if type(message) is str and message:
            self._current_task_message = message[:8192]
        self._outcomes.open_turn(session_id, self._active_turn_id)

    @_serialized_host_event
    def observe_pre_llm(self, **kwargs) -> None:
        """Capture raw current input only; never inject a second recall context."""

        identity = self._require_identity()
        if identity.read_only or not identity.runtime_audience.allowed_scope_ids:
            self._diagnostics.capability_gaps = identity.runtime_audience.capability_gaps
            return
        session_id = self._effective_session_id(str(kwargs.get("session_id") or ""))
        supplied_turn_id = str(kwargs.get("turn_id") or "").strip()
        turn_id = supplied_turn_id or self._active_turn_id or str(self._turn_counter or "turn")
        if supplied_turn_id and supplied_turn_id != self._active_turn_id:
            self._reset_current_source_refs()
            self._active_turn_id = supplied_turn_id
        self._pre_llm_pending = bool(supplied_turn_id)
        self._outcomes.open_turn(session_id, turn_id)
        current_message = kwargs.get("user_message")
        if type(current_message) is str and current_message:
            self._current_task_message = current_message[:8192]
        context = identity.trusted_context(session_id=session_id, mutation=True)
        event, gaps, ledger_identity = pre_llm_source_event(
            self._ledger,
            context,
            session_id=session_id,
            turn_id=turn_id,
            user_message=kwargs.get("user_message"),
            recorded_at=self._utc_now(),
            attachments=kwargs.get("attachments") if isinstance(kwargs.get("attachments"), list) else None,
        )
        if event is None and not gaps:
            return
        receipt = self._capture_event(
            context,
            event,
            identity=ledger_identity,
            gaps=gaps,
            scope_id=identity.local_scope_id,
        )
        if receipt is not None and receipt.durability in ("persisted", "queued"):
            self._user_captured_turns.pop(turn_id, None)
            self._user_captured_turns[turn_id] = None
            while len(self._user_captured_turns) > _USER_CAPTURED_TURNS:
                self._user_captured_turns.pop(next(iter(self._user_captured_turns)))

    @_serialized_host_event
    def observe_post_tool_call(self, **kwargs) -> None:
        self._observe_post_tool_call(**kwargs)

    def _observe_post_tool_call(self, **kwargs) -> None:
        """Capture one tool result; the caller holds ``_lock`` exactly once, and the store I/O runs without it.

        Hermes calls the hook for each of a step's parallel tool calls at once.  Held across its write (1.4-4.4 s on
        the shared store), one capture kept the others waiting, and those past the hook's bound were not taken:
        yuheng 6 and tianji 2 tool results on 2026-10-03.
        """
        identity = self._require_identity()
        if identity.read_only or not identity.runtime_audience.allowed_scope_ids:
            self._diagnostics.capability_gaps = identity.runtime_audience.capability_gaps
            return
        session_id = self._effective_session_id(str(kwargs.get("session_id") or ""))
        turn_id = str(kwargs.get("turn_id") or self._active_turn_id or "turn")
        tool_call_id = str(kwargs.get("tool_call_id") or kwargs.get("id") or turn_id)
        tool_name = str(kwargs.get("tool_name") or kwargs.get("name") or "tool")
        result = kwargs.get("result") if "result" in kwargs else kwargs.get("content")
        status = str(kwargs.get("status") or kwargs.get("outcome") or "success").lower()
        outcome = "success"
        if status in {"error", "failed", "failure"}:
            outcome = "failure"
            self._outcomes.mark_failure(session_id, turn_id, reason=status)
        elif status in {"cancelled", "canceled"}:
            outcome = "cancelled"
            self._outcomes.mark_cancelled(session_id, turn_id)
        elif status in {"interrupted"}:
            outcome = "interrupted"
            self._outcomes.mark_interrupted(session_id, turn_id)
        elif result is None and "result" not in kwargs and "content" not in kwargs:
            outcome = "truncated"
            self._outcomes.mark_truncated(session_id, turn_id)
        is_memory_tool = _is_memory_tool_name(tool_name)
        captured_origin = "memory_reinjection" if is_memory_tool else "tool_observation"
        context = identity.trusted_context(session_id=session_id, actor_origin=captured_origin, mutation=True)
        event, gaps, ledger_identity = tool_call_source_event(
            self._ledger,
            context,
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            result=result,
            recorded_at=self._utc_now(),
            outcome=outcome,
            origin=captured_origin,
        )
        self._capture_event(
            context,
            event,
            identity=ledger_identity,
            gaps=gaps,
            scope_id=identity.local_scope_id if outcome == "success" else None,
            bound=identity,
            release=True,
        )
        self._diagnostics.pending_outcome_gaps = self._outcomes.pending_gaps()

    @_serialized_host_event
    def observe_api_request_error(self, **kwargs) -> None:
        self._require_identity()
        session_id = self._effective_session_id(str(kwargs.get("session_id") or ""))
        turn_id = str(kwargs.get("turn_id") or self._active_turn_id or "turn")
        status = str(kwargs.get("status") or kwargs.get("status_code") or "error")
        self._outcomes.mark_failure(session_id, turn_id, reason=status)
        self._diagnostics.pending_outcome_gaps = self._outcomes.pending_gaps()

    def observe_post_llm_call(self, **kwargs) -> None:
        """Keep what the assistant showed on the way through this turn for ``sync_turn`` to record.

        Hermes calls this once, when a turn that has an answer ends, with a copy of the conversation, and
        before it sends the reply.  It reads that copy and writes nothing, so it takes only ``_said_lock``: under
        the adapter lock the reply waited behind whatever held it, a capture on a busy store or a recall, and
        a callback Hermes gave up on was then skipped for a minute, for every session (Hermes 0.21.5).
        """
        identity = self._require_identity()
        if identity.read_only or not identity.runtime_audience.allowed_scope_ids:
            return
        turn_id = str(kwargs.get("turn_id") or "").strip()
        if not turn_id:
            return
        answer = kwargs.get("assistant_response")
        history = kwargs.get("conversation_history")
        said = interim_messages(history, answer=answer if isinstance(answer, str) else "")
        steered = steer_messages(history)
        with self._said_lock:
            if turn_id != self._active_turn_id:
                return
            if said:
                self._interim_said[turn_id] = said
            if steered:
                self._steer_said[turn_id] = steered

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Write the finished turn one capture per hold of the lock, each one's store I/O without it.

        Hermes runs this on its memory worker after the reply, with no time limit, while the next turn may already
        start.  Held for the whole turn (up to 64 interim messages, the steers, the reply and the retries, each
        waiting up to 1 s for a busy store) it kept the next turn's hooks, its start and its prefetch waiting.
        The captures keep the binding the turn was said under (``bound``).  One turn is written at a time, and a
        shutdown waits for it (``_sync_lock``): one that came between two captures closed the runtime under the
        rest of the turn, the reply included (review of 3.4.10).
        """
        with self._sync_lock:
            self._sync_turn(user_content, assistant_content, session_id=session_id)

    def _sync_turn(self, user_content: str, assistant_content: str, *, session_id: str) -> None:
        with self._lock:
            identity = self._require_identity()
            if identity.read_only:
                return
            # The turn's message and reply are dated when its writing begins: dated as each was reached, the reply
            # came after the next turn's message, written between this turn's captures (review of 3.4.10).
            said_at = self._utc_now()
            effective_session = self._effective_session_id(session_id)
            active_turn = self._active_turn_id
            turn_id = active_turn or str(self._turn_counter or "turn")
            if not identity.runtime_audience.allowed_scope_ids:
                self._diagnostics.capability_gaps = identity.runtime_audience.capability_gaps
                return
        # Hermes runs this on its memory worker, after the reply: the place to write again what a busy store
        # kept in memory, before the turn's own sources.
        self._retry_buffered_captures(release=True)
        context = identity.trusted_context(session_id=effective_session, mutation=True)
        shown = identity.trusted_context(session_id=effective_session, actor_origin="assistant_visible", mutation=True)
        with self._said_lock:
            interim, self._interim_said = self._interim_said, {}
            steers, self._steer_said = self._steer_said, {}
        limited = False
        for said_turn, said in interim.items():
            if len(said) > _INTERIM_PER_TURN:
                _log.warning("scope-recall: turn %s showed %d messages between tool calls; the first %d are kept",
                             said_turn, len(said), _INTERIM_PER_TURN)
                limited, said = True, said[:_INTERIM_PER_TURN]
            for ordinal, (text, occurred_at) in enumerate(said, 1):
                with self._lock, self._holding("sync_turn"):
                    event, gaps, ledger_identity = interim_source_event(
                        self._ledger, shown, session_id=effective_session, turn_id=said_turn, ordinal=ordinal,
                        content=text, recorded_at=self._utc_now(), occurred_at=occurred_at)
                    if event is not None or gaps:
                        self._capture_event(shown, event, identity=ledger_identity, gaps=gaps,
                                            scope_id=identity.local_scope_id, bound=identity, release=True)
        for said_turn, said in steers.items():
            for ordinal, (text, occurred_at) in enumerate(said, 1):
                with self._lock, self._holding("sync_turn"):
                    event, gaps, ledger_identity = steer_source_event(
                        self._ledger, context, session_id=effective_session, turn_id=said_turn, ordinal=ordinal,
                        content=text, recorded_at=self._utc_now(), occurred_at=occurred_at)
                    if event is not None or gaps:
                        self._capture_event(context, event, identity=ledger_identity, gaps=gaps,
                                            scope_id=identity.local_scope_id, bound=identity, release=True)
        outcome = "success"
        with self._lock:
            if not assistant_content.strip():
                outcome = "truncated"
                self._outcomes.mark_truncated(effective_session, turn_id)
            else:
                self._outcomes.mark_success(effective_session, turn_id)
            event_pairs, gaps = sync_turn_source_events(
                self._ledger,
                context,
                session_id=effective_session,
                turn_id=turn_id,
                user_content=user_content,
                assistant_content=assistant_content,
                recorded_at=said_at,
                outcome=outcome,
                include_user=turn_id not in self._user_captured_turns,
            )
        for event, ledger_identity in event_pairs:
            event_context = shown if event["role"] == "assistant" else context
            with self._lock, self._holding("sync_turn"):
                self._capture_event(event_context, event, identity=ledger_identity, gaps=gaps,
                                    scope_id=identity.local_scope_id, bound=identity, release=True)
        with self._lock:
            if self._active_turn_id == active_turn:
                # The next turn may have begun between these writes; its pre_llm marker is its own.
                self._pre_llm_pending = False
            self._diagnostics.pending_outcome_gaps = self._outcomes.pending_gaps()
            if limited:
                self._merge_gaps(("capture_gap:interim_limit",))

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        # Serialize the short process launch with shutdown, never the drain.
        with self._lock:
            self._end_session(messages)

    def _end_session(self, messages: List[Dict[str, Any]]) -> None:
        identity = self._require_identity()
        self._session_watermark += 1
        self._retry_observed_captures()
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
                    core.drain_worker(context, max_items=8, remaining_seconds=_CAPTURE_TIMEOUT_S)
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
            self._user_captured_turns.clear()
        self._identity = fresh
        runtime_audience = fresh.runtime_audience
        self._diagnostics.capability_gaps = tuple(
            dict.fromkeys((*runtime_audience.capability_gaps, *(self._host_runtime.capability_gaps if self._host_runtime else ())))
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
        self._retry_observed_captures()
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
            self._retry_wake.set()
            try:
                self._retry_buffered_captures(
                    seconds=min(_SHUTDOWN_RETRY_SECONDS, max(_CAPTURE_TIMEOUT_S, deadline - time.monotonic())),
                    force=True)
            except Exception as exc:  # noqa: BLE001 - the shutdown goes on; what is left is said below
                _log.warning("scope-recall: a retry of buffered captures failed at shutdown (%s)", type(exc).__name__)
            pending = self._pending_capture_identities()
            for key in tuple(self._retry_captures)[:16]:
                if key in self._retry_in_flight:
                    _log.warning("scope-recall: not stored yet (still being written at shutdown): %s", _label(key))
                else:
                    _log.warning("scope-recall: not stored (still failing at shutdown), lost: %s", _label(key))
            self._retry_captures.clear()
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
