"""Hermes MemoryProvider adapter that delegates recall/capture to the core boundary."""
from __future__ import annotations

from dataclasses import dataclass, replace
import copy
import inspect
import json
from functools import wraps
import sqlite3
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
    pre_llm_source_event,
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
)
from .installation import assert_binding_matches_manifest, assert_core_binding_matches, load_binding_for_home
from .outcomes import TurnOutcomeTracker
from .protocol import PublicMemoryProvider
from .runtime_wiring import GAP_WORKER_LAUNCH_FAILED, HermesHostRuntime, TrustedHostRuntime, attach_trusted_host_runtime
from .worker import AdapterWorker
from .tool_surface import HermesToolSurface, _TOOL_NAMES, display_zone

_CAPTURE_TIMEOUT_S = 1.0
_BOUNDED_MESSAGE_SCAN = 8
GAP_CURRENT_SOURCE_REFS_LIMIT = "degraded:current_source_refs_limit"

def _serialized_host_event(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._lock:
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


@dataclass(frozen=True)
class _RetryCapture:
    context: TrustedContext
    event: dict
    gaps: tuple[str, ...]
    scope_id: str
    host_scope: HermesRuntimeScope


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
        self._session_watermark = 0
        self._current_source_refs: list[str] = []
        #: This turn captured more sources than the fence holds; recall stays
        #: off until the refs reset rather than run with an incomplete fence.
        self._current_source_refs_overflow = False
        self._current_task_message = ""
        self._retry_captures: dict[SourceIdentity, _RetryCapture] = {}
        self._diagnostics = AdapterDiagnostics()
        #: What the last worker launch attempt added to capability_gaps.
        self._worker_launch_gaps: tuple[str, ...] = ()
        self._initialized = False

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
            self._session_watermark = 0
            self._reset_current_source_refs()
            self._current_task_message = ""
            self._retry_captures.clear()
            self._diagnostics.capability_gaps = tuple(
                dict.fromkeys(
                    (*fresh.runtime_audience.capability_gaps, *self._host_runtime.capability_gaps)
                )
            )
            self._worker_launch_gaps = ()
            self._initialized = True
            from .hooks import update_adapter_binding

            update_adapter_binding(self)

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

    def _record_capture_failure(self, identity: SourceIdentity | None, reason: str) -> None:
        if identity is not None:
            self._ledger.rollback(identity)
            label = f"{identity[0]}@{identity[1]}"
        else:
            label = "unknown"
        self._diagnostics.capture_failures = tuple(
            dict.fromkeys((*self._diagnostics.capture_failures, f"capture_failure:{label}:{reason}"))
        )[-64:]
        if identity in self._retry_captures:
            self._merge_gaps(("capture_gap:retry_memory_only", "capability_gap:durable_capture_ingress_unavailable"))

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
    ):
        if event is None:
            if gaps:
                self._merge_gaps(gaps)
            return None
        event = dict(event)
        if not replay:
            source_context = trusted_source_context(self._require_identity().scope)
            if source_context is not None:
                event["source_context"] = source_context
            else:
                event.pop("source_context", None)
        if not scope_id:
            self._record_capture_failure(identity, "capability_gap")
            self._merge_gaps(gaps, ("capability_gap:no_capture_scope",))
            return None
        started = time.monotonic()
        if identity is not None and identity not in self._retry_captures:
            if len(self._retry_captures) < 16 and len(json.dumps(event, ensure_ascii=False).encode("utf-8")) <= 262144:
                self._retry_captures[identity] = _RetryCapture(
                    context, copy.deepcopy(event), gaps, scope_id, self._require_identity().scope,
                )
            else:
                self._merge_gaps(("capture_gap:retry_buffer_full",))
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
                    host_scope=host_scope_payload(self._retry_captures[identity].host_scope if replay and identity in self._retry_captures else self._require_identity().scope),
                    remaining_seconds=max(.001, remaining_seconds - (time.monotonic() - started)))
            else:
                receipt = core.record_event(context, event, scope_id=scope_id,
                    remaining_seconds=max(.001, remaining_seconds - (time.monotonic() - started)))
        except (ContractError, OSError, RuntimeError, sqlite3.Error) as exc:
            if isinstance(exc, ContractError) and exc.code not in {"DEADLINE_EXCEEDED", "STORAGE_UNAVAILABLE"}:
                self._retry_captures.pop(identity, None)
            self._record_capture_failure(identity, "exception")
            self._merge_gaps(gaps, ("capture_gap:write_exception",))
            return None
        if receipt.durability != "persisted":
            if receipt.durability == "queued":
                self._retry_captures.pop(identity, None)
                self._merge_gaps(gaps, ("capture_gap:durable_ingress_pending",))
                self._wake_background_worker(context=context)
                return receipt
            if receipt.disposition in {"rejected", "conflict", "cancelled"}:
                self._retry_captures.pop(identity, None)
            self._record_capture_failure(identity, receipt.error_code or receipt.disposition)
            self._merge_gaps(gaps, (f"capture_gap:{receipt.disposition}",))
            return receipt
        if identity is not None:
            self._ledger.confirm(identity)
            self._retry_captures.pop(identity, None)
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
                from ...core.capture_inbox import replay_inbox
                replay_inbox(core.storage, core.clock, identity.trusted_context(),
                    authorize=build_ingress_authorizer(identity.binding), admission_policy=core.config.admission_policy,
                    remaining_seconds=_CAPTURE_TIMEOUT_S)
            except (ContractError, OSError, RuntimeError, sqlite3.Error, ValueError):
                self._merge_gaps(("capture_gap:durable_ingress_pending",))
        if not self._retry_captures:
            return
        deadline = time.monotonic() + _CAPTURE_TIMEOUT_S
        try:
            manifest = load_binding_for_home(identity.hermes_home)
            assert_binding_matches_manifest(identity.binding, manifest)
            current_audience = resolve_runtime_audience(manifest, identity.scope)
        except (HermesIdentityError, ContractError, OSError, ValueError, TypeError):
            self._merge_gaps(("capture_gap:retry_authorization_unverified",))
            return
        for key, pending in tuple(self._retry_captures.items())[:8]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            original_audience = resolve_runtime_audience(manifest, pending.host_scope)
            allowed_scopes = (pending.context.allowed_scope_ids
                              & original_audience.writable_scope_ids
                              & current_audience.writable_scope_ids)
            if pending.context.binding != identity.binding or pending.scope_id not in allowed_scopes:
                self._retry_captures.pop(key, None)
                self._ledger.rollback(key)
                self._merge_gaps(("capture_gap:retry_authorization_revoked",))
                continue
            if identity.read_only:
                continue
            # Narrow current authorization only. Original actor, session,
            # project, branch, occurrence time and DTO identity stay intact.
            context = replace(pending.context, allowed_scope_ids=frozenset(allowed_scopes))
            self._capture_event(context, pending.event, identity=key, gaps=pending.gaps,
                                scope_id=pending.scope_id, remaining_seconds=remaining, replay=True)

    @_serialized_host_event
    def prefetch(self, query: str, *, session_id: str = "") -> str:
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
        packet = self._require_core().recall_packet(
            context,
            self._recall_request(query, effective_session),
            current_source_refs=current_refs,
        )
        preparation = self._require_core().prepare_recall_render(context, packet)
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
        self._capture_event(
            context,
            event,
            identity=ledger_identity,
            gaps=gaps,
            scope_id=identity.local_scope_id,
        )

    @_serialized_host_event
    def observe_post_tool_call(self, **kwargs) -> None:
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
        is_memory_tool = _is_scope_recall_tool_name(tool_name)
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

    @_serialized_host_event
    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        identity = self._require_identity()
        if identity.read_only:
            return
        effective_session = self._effective_session_id(session_id)
        turn_id = self._active_turn_id or str(self._turn_counter or "turn")
        if not identity.runtime_audience.allowed_scope_ids:
            self._diagnostics.capability_gaps = identity.runtime_audience.capability_gaps
            return
        context = identity.trusted_context(session_id=effective_session, mutation=True)
        outcome = "success"
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
            recorded_at=self._utc_now(),
            outcome=outcome,
        )
        for event, ledger_identity in event_pairs:
            event_context = context
            if event["role"] == "assistant":
                event_context = identity.trusted_context(
                    session_id=effective_session,
                    actor_origin="assistant_visible",
                    mutation=True,
                )
            self._capture_event(
                event_context,
                event,
                identity=ledger_identity,
                gaps=gaps,
                scope_id=identity.local_scope_id,
            )
        self._pre_llm_pending = False
        self._diagnostics.pending_outcome_gaps = self._outcomes.pending_gaps()

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
        self._current_task_message = ""
        self._active_turn_id = ""
        self._pre_llm_pending = False
        self._identity = fresh
        runtime_audience = fresh.runtime_audience
        self._diagnostics.capability_gaps = tuple(
            dict.fromkeys((*runtime_audience.capability_gaps, *(self._host_runtime.capability_gaps if self._host_runtime else ())))
        )
        self._worker_launch_gaps = ()
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
        with self._lock:
            from .hooks import unregister_adapter

            unregister_adapter(self)
            pending = self._pending_capture_identities()
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
