"""Explicit dependencies; host adapters remain outside the core."""

from __future__ import annotations

import math
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo
from typing import TYPE_CHECKING, Any, Literal, Protocol, overload

from ..contracts import (
    ContractError,
    InstanceBinding,
    SourceEvent,
    TrustedContext,
    decode_payload,
    utc_timestamp,
    validate_model_request,
    validate_payload,
)
from . import private
from .admission import AdmissionPolicy
from .capture import CaptureReceipt, record_event
from .capture_filters import without_private
from .delete_storage import retraction_after
from .model_content import withhold_secret_items
from .operations import Operations
from .recall_diagnostics import RECALL_DIAGNOSTIC_PREFIX, RecallDiagnostics
from .recall_log_storage import LogPolicy
from .recall_trace import CURRENT_TRACE, logged_recall
from .records import Records
from .restore import InstallationMaintenance, maintenance_context
from .retrieval import CandidateRef, RetrievalResult, SearchContext, SearchLimits
from .source_records import StoredSource
from .storage import SQLiteStorage, StoreStatus


class Clock(Protocol):
    def utc_now(self) -> str: ...
    def monotonic(self) -> float: ...


class SystemClock:
    def utc_now(self) -> str:
        return datetime.now(UTC).isoformat()

    def monotonic(self) -> float:
        return time.monotonic()


class Identifiers(Protocol):
    def next_id(self) -> str: ...


class RandomIdentifiers:
    def next_id(self) -> str:
        return uuid.uuid4().hex


class VectorPort(Protocol):
    def search(self, context: SearchContext, *, limit: int, remaining_seconds: float) -> tuple[CandidateRef, ...]: ...


class ConsolidationPort(Protocol):
    def propose(self, messages: list[dict], *, remaining_seconds: float) -> str: ...


if TYPE_CHECKING:
    from .capture_inbox import PendingCapture


@dataclass(frozen=True)
class CoreConfig:
    binding: InstanceBinding
    write_timeout_seconds: float = 1.0
    auto_recall_seconds: float = 5.0
    admission_policy: AdmissionPolicy = field(default_factory=AdmissionPolicy)
    session_brief_enabled: bool = True
    session_brief_tokens: int = 1024
    recall_log_policy: LogPolicy = field(default_factory=LogPolicy)

    def __post_init__(self) -> None:
        if not isinstance(self.binding, InstanceBinding):
            raise ValueError("binding must be an InstanceBinding")
        if not isinstance(self.admission_policy, AdmissionPolicy):
            raise ValueError("admission_policy must be an AdmissionPolicy")
        if type(self.session_brief_enabled) is not bool or type(self.session_brief_tokens) is not int:
            raise ValueError("invalid session briefing configuration")
        if not 1 <= self.session_brief_tokens <= 8192:
            raise ValueError("session briefing token limit must be between 1 and 8192")
        if not isinstance(self.recall_log_policy, LogPolicy):
            raise ValueError("recall_log_policy must be a LogPolicy")
        value = self.write_timeout_seconds
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 30:
            raise ValueError("write timeout must be finite and between zero and 30 seconds")
        auto = self.auto_recall_seconds
        if type(auto) not in (int, float) or not math.isfinite(auto) or not 0 < auto <= 5:
            raise ValueError("auto recall timeout must be finite and between zero and five seconds")


# The services below import their modules when first used, not when this module loads: every entry, the installers
# and the Hermes registration import ``scope_recall.core``, and most of them use few of these services.
class MemoryCore:
    """Application composition. Construction neither opens storage nor calls a model."""

    def __init__(
        self,
        config: CoreConfig,
        *,
        storage: SQLiteStorage | None = None,
        clock: Clock | None = None,
        identifiers: Identifiers | None = None,
        vectors: VectorPort | None = None,
        consolidation: ConsolidationPort | None = None,
        retrieval_policy=None,
    ) -> None:
        self.config = config
        self.clock = clock if clock is not None else SystemClock()
        self.identifiers = identifiers if identifiers is not None else RandomIdentifiers()
        self.storage = (
            storage
            if storage is not None
            else SQLiteStorage(config.binding, timeout_seconds=config.write_timeout_seconds, clock=self.clock.monotonic)
        )
        if self.storage.binding != config.binding:
            raise ValueError("storage binding differs from core binding")
        self.vectors = vectors
        self.storage.entity_vectors = lambda: self.vectors
        self.consolidation = consolidation
        from .recall import RetrievalPipeline
        from .recall_packet import RecallPacketCompiler, RecallPacketRenderer

        self.recall_pipeline = RetrievalPipeline(
            self.storage, vector_port=vectors, policy=retrieval_policy, clock=self.clock
        )
        self.recall_diagnostics = RecallDiagnostics(
            self.storage.path.with_name("recall.sqlite3"), config.recall_log_policy
        )
        self.recall_packet_renderer = RecallPacketRenderer()
        self._packet_fences = {}
        self._brief_fences = {}
        self._packet_fences_lock = threading.RLock()
        self.recall_packet_compiler = RecallPacketCompiler(
            self.recall_pipeline.storage_reader,
            diagnostics=self.recall_diagnostics,
            clock=self.clock,
        )
        self.operations = Operations(self)
        self.records = Records(self)

    def initialize(self) -> StoreStatus:
        """Explicit initialization or an identity-checked supported schema upgrade."""
        return self.storage.initialize(created_at=self.clock.utc_now())

    def session_brief(
        self,
        context: TrustedContext,
        *,
        token_limit: int | None = None,
        remaining_seconds=None,
        deliver: bool = False,
        compact: bool = False,
        defer_confirmation: bool = False,
    ) -> dict:
        """Read a bounded, source-checked briefing for the bound session."""
        from .session_brief import read

        limit = self.config.session_brief_tokens if token_limit is None else token_limit
        if not self.config.session_brief_enabled:
            if not compact:
                return {"text": "", "items": [], "tokens": 0, "elapsed_ms": 0}
            limit = 0
            deliver = False
        brief = read(
            self.storage,
            self.clock,
            context,
            token_limit=limit,
            remaining_seconds=remaining_seconds,
            deliver=deliver,
            compact=compact,
            defer_confirmation=True,
        )
        if "_entry_fence" in brief:
            fence = brief["_entry_fence"].copy()
            if self._entry_blocked(context, fence):
                return {"text": "", "items": [], "tokens": 0, "elapsed_ms": 0, "delivery_reason": "entry_state:paused"}
            with self._packet_fences_lock:
                self._brief_fences[id(brief)] = (brief, context.binding.installation_id, fence)
                while len(self._brief_fences) > 128:
                    self._brief_fences.pop(next(iter(self._brief_fences)))
        if not defer_confirmation and (deliver or compact) and "_epoch" in brief:
            brief["delivery_reason"] = self.confirm_session_brief(context, brief, remaining_seconds=remaining_seconds)
            if brief["delivery_reason"] in {"brief_invalidated", "brief_safety_unconfirmed", "entry_state:paused"}:
                brief.update(text="", items=[], tokens=0, elapsed_ms=0)
        return brief

    def confirm_session_brief(self, context: TrustedContext, brief: dict, *, remaining_seconds=None) -> str:
        """Confirm a prepared briefing once the host has built its response."""
        from .session_brief import confirm

        with self._packet_fences_lock:
            saved = self._brief_fences.get(id(brief))
        if saved is None or saved[0] is not brief or saved[1] != context.binding.installation_id:
            return "entry_state:paused"
        fence = saved[2]
        reason = confirm(
            self.storage, self.clock, context, brief, entry_fence=fence, remaining_seconds=remaining_seconds
        )
        if reason != "entry_state:paused" and self._entry_blocked(context, fence):
            reason = "entry_state:paused"
        if brief.get("_deliver") and brief.get("items") and reason in {"brief_delivered", "brief_delivery_unconfirmed"}:
            from .recall_trace import record_brief

            record_brief(self, context, brief, reason)
        return reason

    def sync_session_compaction(self, context: TrustedContext, *, remaining_seconds: float = 0.03) -> bool:
        """Persist an observed compaction without waiting indefinitely for the writer."""
        from .session_brief import sync_compaction

        return sync_compaction(self.storage, context, remaining_seconds=remaining_seconds)

    @staticmethod
    def remove_question_overlaps(brief: dict, question_text: str) -> None:
        """Remove facts already present in the same host response's question recall."""
        from .session_brief import remove_question_overlaps

        remove_question_overlaps(brief, question_text)

    def session_compaction_watermark(self, context: TrustedContext) -> int:
        """Read the last committed compaction sequence for this entry and session."""
        from .session_brief import compaction_watermark

        return compaction_watermark(self.storage, context)

    def reset_session_injections(self, context: TrustedContext, *, remaining_seconds=None) -> None:
        """Forget the current context's delivered references after host compaction."""
        from .session_brief import reset_session

        reset_session(self.storage, context, remaining_seconds=remaining_seconds)

    def record_session_recall(self, context: TrustedContext, *, tokens: int, elapsed_ms: int, remaining_seconds: float):
        """Record content-free automatic recall usage after the host chooses its answer."""
        from .session_brief import record_recall

        record_recall(
            self.storage, self.clock, context, tokens=tokens, elapsed_ms=elapsed_ms, remaining_seconds=remaining_seconds
        )

    def session_injection_stats(self, context: TrustedContext) -> dict:
        """Return opening and question recall usage for this session."""
        from .session_brief import session_stats

        return session_stats(self.storage, context)

    def status(
        self,
        context: TrustedContext,
        *,
        include_admission: bool = True,
        include_queue_age: bool = True,
        remaining_seconds: float | None = None,
    ) -> StoreStatus:
        """Queue and store standing.  The admission counts scan every source's JSON and the
        queue age walks every queued row, so a caller that only needs the queue's depth says
        so and skips both.  ``remaining_seconds`` bounds the read's wait for the store's lock,
        which is otherwise the core's write timeout."""
        with self.storage.read(context, remaining_seconds=remaining_seconds) as tx:
            return tx.status(include_admission=include_admission, include_queue_age=include_queue_age)

    def memory_epoch(self, context: TrustedContext) -> int:
        """Fresh identity-checked fence; diagnostics are a separate operation."""
        with self.storage.read(context) as tx:
            return tx.memory_epoch()

    def entry_state(self, context: TrustedContext):
        """Read the current entry control without caching across requests."""
        from .entry_control import state_for

        with self.storage.read(context) as tx:
            return state_for(tx, context)

    def _entry_fence(self, context):
        """Capture the request entry's current authority."""
        from .entry_control import context_fence

        with self.storage.read(context) as tx:
            return context_fence(tx, context)

    def _entry_blocked(self, context, fence):
        """Recheck a request against the shared entry-state fence."""
        from .entry_control import entry_changed

        with self.storage.read(context) as tx:
            return entry_changed(tx, fence)

    def memory_retracted_since(self, context: TrustedContext, epoch: int) -> bool:
        """Whether a deletion or suppression in this context's scopes was recorded after ``epoch``.

        A host fences what it delivers against this rather than against any epoch move: the epoch
        moves with every capture, and on a store several entries write to, most views were
        compiled one capture ago.
        """
        with self.storage.read(context) as tx:
            return retraction_after(tx._check(), context.allowed_scope_ids, epoch)

    def source_by_event_key(
        self,
        context: TrustedContext,
        source_event_key: str,
        revision: int = 1,
        *,
        remaining_seconds: float | None = None,
    ) -> StoredSource | None:
        """Read a scoped source by its host event key while the entry is authorized."""
        fence = self._entry_fence(context)
        if self._entry_blocked(context, fence):
            return None
        with self.storage.read(context, remaining_seconds=remaining_seconds) as tx:
            result = tx.sources.source_by_event_key(source_event_key, revision)
        return None if self._entry_blocked(context, fence) else result

    def _source_replay_metadata(
        self, context, source_event_key, revision=1, *, remaining_seconds=None
    ) -> dict[str, Any] | None:
        """Read only identity, timestamps and a digest needed to recognize a replay."""
        with self.storage.read(context, remaining_seconds=remaining_seconds) as tx:
            source = tx.sources.source_by_event_key(source_event_key, revision)
            if source is None:
                return None
            return dict(
                scope_id=source.scope_id,
                session_id=source.session_id,
                project_id=source.project_id,
                branch_id=source.branch_id,
                content_sha256=source.content_sha256,
                event={
                    key: source.event[key]
                    for key in ("occurred_at", "recorded_at", "time_precision")
                    if key in source.event
                },
            )

    def source(self, context: TrustedContext, ref: str, revision: int) -> StoredSource | None:
        fence = self._entry_fence(context)
        if self._entry_blocked(context, fence):
            return None
        with self.storage.read(context) as tx:
            result = tx.source(ref, revision)
        return None if self._entry_blocked(context, fence) else result

    def inspect_object(
        self,
        context: TrustedContext,
        ref: str,
        revision: int | None = None,
        *,
        limit: int = 24,
        owner_authority: InstallationMaintenance | None = None,
    ):
        """Inspect within scope and deletion fences; only a local owner may read during privacy."""
        from .inspect_service import InspectedObject, inspect_object
        from .recall_diagnostics import diagnostic
        from .recall_log_view import REQUEST_PREFIX, visible_record

        entry = self._entry_fence(context) if owner_authority is None else None
        if entry is not None and self._entry_blocked(context, entry):
            return InspectedObject(
                "unknown", ref, revision or 1, None, self.memory_epoch(context), ("entry_state:paused",)
            )
        if owner_authority is not None:
            owner = maintenance_context(self.storage, owner_authority)
            if replace(context, project_id=owner.project_id, branch_id=owner.branch_id) != owner:
                raise ContractError("ACCESS_DENIED", "owner_authority")
        marker = self._private_read_refusal(context) if owner_authority is None else None
        if type(ref) is str and ref.startswith((RECALL_DIAGNOSTIC_PREFIX, REQUEST_PREFIX)):
            if revision not in (None, 1):
                raise ContractError("INPUT_INVALID", "revision")
            self.recall_diagnostics.expire(self.clock.utc_now())
            self.recall_diagnostics.flush()
            selector = {"request_id": ref[len(REQUEST_PREFIX) :]} if ref.startswith(REQUEST_PREFIX) else {"ref": ref}
            rows = self.recall_diagnostics.store.list(
                installation_id=context.binding.installation_id,
                session_id=context.session_id,
                limit=1,
                now=self.clock.utc_now(),
                **selector,
            )
            view = visible_record(self, context, rows[0]) if rows else None
            if view is None:
                raise ContractError("SOURCE_MISSING", "recall_diagnostic")
            record = diagnostic(view)
            result = InspectedObject("recall_diagnostic", record.ref, 1, record.to_public(), record.memory_epoch or 0)
        else:
            result = inspect_object(self.storage, self.clock, context, ref, revision, limit=limit)
        if owner_authority is None:
            if self._entry_blocked(context, entry):
                return InspectedObject(
                    "unknown", ref, revision or 1, None, self.memory_epoch(context), ("entry_state:paused",)
                )
            self._private_read_refusal(context, after=marker)
            gaps: list[str] = []
            if not withhold_secret_items([result.value], gaps):
                result = replace(result, value=None, gaps=tuple(gaps))
        return result

    def private_mode(self, context: TrustedContext) -> str | None:
        """The active private mode, None, or ``unavailable`` during a format upgrade.

        This is the adapter-facing read used for the injected notice; capture and
        recall gates use the same core helper and never a copy of this policy.
        """
        now = utc_timestamp(self.clock.utc_now())
        try:
            with self.storage.read(context, remaining_seconds=self.config.write_timeout_seconds) as tx:
                return private.mode_at(private.matching_intervals(tx, context, now=now), now)
        except ContractError as exc:
            if exc.code == "SCHEMA_UNSUPPORTED" and exc.field in {"upgrade_in_progress", "upgrade_pending"}:
                # Capture and recall retain their explicit schema gap. The hook's
                # notice read must not turn that bounded refusal into a host error.
                return "unavailable"
            raise

    def _recall_private_state(self, context: TrustedContext, *, after: int | None = None, logging: bool = False):
        """Read a fresh decision and rule marker at recall entry or release."""
        now = utc_timestamp(self.clock.utc_now())
        policy_context = (
            context if context.allowed_scope_ids else replace(context, allowed_scope_ids=context.binding.scope_ids)
        )
        with self.storage.read(policy_context, remaining_seconds=self.config.write_timeout_seconds) as tx:
            state = private.recall_state(tx, context, now, after=after, logging=logging)
            trace = CURRENT_TRACE.get()
            # The ordinary release read also fences diagnostic publication.
            # With no newly opened rule, its marker avoids a second lookup.
            if not logging and trace is not None and trace.enabled and after is not None and state[1] > after:
                if private.rules_changed(tx, context, after=after, modes=private.MODES)[0]:
                    trace.enabled = False
            return state

    def _recall_entry(self, search_context):
        """Share one entry snapshot between private policy and query identity."""
        from .entity_queries import resolve_query_context

        context = search_context.trusted_context
        now = utc_timestamp(self.clock.utc_now())
        policy_context = (
            context if context.allowed_scope_ids else replace(context, allowed_scope_ids=context.binding.scope_ids)
        )
        with self.storage.read(policy_context, remaining_seconds=self.config.write_timeout_seconds) as tx:
            mode, marker = private.recall_state(tx, context, now)
            trace = CURRENT_TRACE.get()
            if trace is not None:
                trace.enabled = mode is None
                trace.private_marker = marker
            if mode != "full" and context.allowed_scope_ids:
                search_context = resolve_query_context(tx, search_context, self.clock)
        return search_context, mode, marker

    def private_capture_blocked(
        self,
        context: TrustedContext,
        value: SourceEvent | dict | str | bytes,
        *,
        remaining_seconds: float = 1.0,
        guard: private.CaptureGuard | None = None,
    ) -> bool:
        """Whether this event's own occurrence time falls in any private interval.

        The caller uses this before keeping a failed capture on disk.  The same
        check runs again at the authoritative write, so a rule opened between the
        two cannot be bypassed. A batch guard reads only occurrence metadata from
        the caller's already validated capture; it does not mask or split its text again.
        """
        if guard is not None:
            event = decode_payload(dict(value) if isinstance(value, dict) else value)
            return guard.blocked(context, (event,))
        from .events import prepare_capture

        prepared = prepare_capture(value, context)
        if prepared.rejection:
            return False
        with self.storage.capture_privacy(context, remaining_seconds=remaining_seconds) as tx:
            return (
                private.capture_mode(private.matching_intervals(tx, context, events=prepared.events), prepared.events)
                is not None
            )

    def private_capture_guard(self, context: TrustedContext, *, observations=(), remaining_seconds: float = 1.0):
        """Read one metadata snapshot for a hook's bounded retry or deferral batch."""
        with self.storage.capture_privacy(context, remaining_seconds=remaining_seconds) as tx:
            return private.CaptureGuard.read(tx, observations)

    def private_rules_exist(self, context: TrustedContext) -> bool:
        """Return only whether the entry's store has active or closed private intervals."""
        with self.storage.capture_privacy(context, remaining_seconds=self.config.write_timeout_seconds) as tx:
            return private.has_rules(tx)

    def private_check(
        self, context: TrustedContext, times, *, rule_marker: int | None = None, project_unverified: bool = False
    ) -> dict:
        """Metadata-only answer for a remote client: whether these moments or a recall are private.

        ``times`` are event instants the client is about to send; an empty list
        asks about now.  No prompt, attachment or record line travels here.
        """
        if times is None:
            moments = []
        elif isinstance(times, (list, tuple)) and len(times) <= 64:
            moments = []
            for value in times:
                if isinstance(value, dict):
                    first, last = utc_timestamp(value.get("earliest")), utc_timestamp(value.get("latest"))
                    if first > last:
                        raise ContractError("INPUT_INVALID", "times")
                    moments.append(dict(occurred_at=first, occurred_until=last))
                else:
                    moments.append(dict(occurred_at=utc_timestamp(value) if value is not None else None))
        else:
            raise ContractError("INPUT_INVALID", "times")
        now = utc_timestamp(self.clock.utc_now())
        with self.storage.capture_privacy(context, remaining_seconds=self.config.write_timeout_seconds) as tx:
            private_rules = private.has_rules(tx)
            events = [dict(occurred_at=now, recorded_at=now), *[dict(moment, recorded_at=now) for moment in moments]]
            intervals = private.matching_intervals(tx, context, events=events)
            if project_unverified:
                intervals.extend(private.unverified_project_rule(tx))
            mode = private.mode_at(intervals, now)
            event_modes = [private.capture_mode(intervals, [event]) for event in events[1:]]
            decisions = [mode is not None for mode in event_modes]
            invalidated, marker = private.rules_changed(tx, context, after=rule_marker)
            if invalidated:
                mode = "full"
        return {
            "private_rules": private_rules,
            "mode": mode or private.MODE_NONE,
            "capture_blocked": any(decisions) or mode is not None,
            "decisions": decisions,
            "full_decisions": [mode == "full" for mode in event_modes],
            "rule_marker": marker,
            "recall_blocked": mode == "full",
            "notice": private.notice_for_mode(mode),
        }

    def _private_read_refusal(self, context: TrustedContext, *, after: int | None = None) -> int:
        """Fence read entry and release, including a full interval closed during the read."""
        if self._entry_blocked(context, self._entry_fence(context)):
            raise ContractError("ACCESS_DENIED", "entry_state:paused")
        mode, marker = self._recall_private_state(context, after=after)
        if mode == "full":
            raise ContractError("ACCESS_DENIED", "private_mode")
        return marker

    def profile(self, context: TrustedContext, request):
        """Read-only categorized current-fact profile for one explicit subject."""
        from .read_views import paused_view, read_profile

        entry = self._entry_fence(context)
        if self._entry_blocked(context, entry):
            return paused_view(context, request)
        marker = self._private_read_refusal(context)
        result = read_profile(self.storage, self.clock, context, request)
        if self._entry_blocked(context, entry):
            return paused_view(context, request)
        self._private_read_refusal(context, after=marker)
        return result

    def trace(self, context: TrustedContext, request, *, deadline_seconds=5.0):
        from .trace import paused_trace, read_trace

        entry = self._entry_fence(context)
        if self._entry_blocked(context, entry):
            return paused_trace(self, context, request)
        marker = self._private_read_refusal(context)
        result = read_trace(self.storage, self.clock, context, request, seconds=deadline_seconds)
        if self._entry_blocked(context, entry):
            return paused_trace(self, context, request)
        self._private_read_refusal(context, after=marker)
        return result

    def entity(self, context: TrustedContext, request):
        """Read-only exact one-hop entity probe or related-statement view."""
        from .read_views import paused_view, read_entity

        entry = self._entry_fence(context)
        if self._entry_blocked(context, entry):
            return paused_view(context, request, entity=True)
        marker = self._private_read_refusal(context)
        result = read_entity(self.storage, self.clock, context, request)
        if self._entry_blocked(context, entry):
            return paused_view(context, request, entity=True)
        self._private_read_refusal(context, after=marker)
        return result

    def record_event(
        self,
        context: TrustedContext,
        value: SourceEvent | dict | str | bytes,
        *,
        scope_id: str,
        remaining_seconds: float | None = None,
        _prepared=None,
    ) -> CaptureReceipt:
        return record_event(
            self.storage,
            self.clock,
            context,
            value,
            scope_id=scope_id,
            admission_policy=self.config.admission_policy,
            _prepared=_prepared,
            remaining_seconds=self.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds,
        )

    @overload
    def record_host_event(
        self,
        context,
        value,
        *,
        scope_id,
        host_scope,
        remaining_seconds=1.0,
        _prepared=None,
        background: Literal[False] = False,
    ) -> CaptureReceipt: ...

    @overload
    def record_host_event(
        self,
        context,
        value,
        *,
        scope_id,
        host_scope,
        remaining_seconds=1.0,
        _prepared=None,
        background: Literal[True],
    ) -> CaptureReceipt | PendingCapture: ...

    @overload
    def record_host_event(
        self,
        context,
        value,
        *,
        scope_id,
        host_scope,
        remaining_seconds=1.0,
        _prepared=None,
        background: bool,
    ) -> CaptureReceipt | PendingCapture: ...

    def record_host_event(
        self, context, value, *, scope_id, host_scope, remaining_seconds=1.0, _prepared=None, background=False
    ):
        """Authenticated host ingress with durable recovery before derivation."""
        from .capture_inbox import durable_record_event, start_record_event

        record = start_record_event if background else durable_record_event
        return record(
            self.storage,
            self.clock,
            context,
            value if _prepared is None else _prepared,
            scope_id=scope_id,
            host_scope=host_scope,
            admission_policy=self.config.admission_policy,
            remaining_seconds=remaining_seconds,
        )

    def search_sources(
        self, context: TrustedContext, query: str, *, limit: int = 20, history: bool = False, automatic: bool = False
    ) -> tuple[StoredSource, ...]:
        fence = self._entry_fence(context)
        if self._entry_blocked(context, fence):
            return ()
        with self.storage.read(context) as tx:
            result = tx.sources.search_sources(query, limit=limit, history=history, automatic=automatic)
        return () if self._entry_blocked(context, fence) else result

    def said_in_session(
        self,
        context: TrustedContext,
        scope_id: str,
        items,
        *,
        window_seconds: float = 120.0,
        remaining_seconds: float | None = None,
        across_sessions: bool = False,
    ) -> tuple[bool, ...]:
        """Whether each (role, content, occurred_at, host_key) is already held in this session, or with
        ``across_sessions`` a named one in any session of the scope; see ``Sources.said_in_session``."""
        with self.storage.read(context, remaining_seconds=remaining_seconds) as tx:
            return tx.sources.said_in_session(
                scope_id, tuple(items), window_seconds=window_seconds, across_sessions=across_sessions
            )

    @staticmethod
    def _private_recall_packet(search_context) -> dict:
        """The empty packet a full interval returns before any embedding, network or model call."""
        packet = dict(
            protocol_version="1.1",
            request_id=search_context.request_id,
            status="no_match",
            memory_epoch=None,
            items=[],
            gaps=["private_mode:full"],
            diagnostic_ref=None,
            answerability="unknown",
            coverage="unknown",
            unmet_needs=[],
        )
        validate_payload("recall_packet", packet)
        return packet

    @staticmethod
    def _paused_recall_packet(search_context) -> dict:
        """Return an empty public packet carrying the entry-state reason."""
        packet = MemoryCore._private_recall_packet(search_context)
        packet["gaps"] = ["entry_state:paused"]
        return packet

    def _search_context(
        self,
        context: TrustedContext,
        request,
        current_source_refs: tuple[str, ...],
        deadline_seconds: float | None,
        background_without_evidence: bool,
        zone: tzinfo | None,
        in_context_since: str | None = None,
    ) -> tuple[SearchContext, float]:
        """A recall request's search context, and the seconds it was given: the caller's, else the core's for an
        automatic recall and 2 for any other; an automatic recall never gets more than the core's."""
        if not context.allowed_scope_ids and (trace := CURRENT_TRACE.get()) is not None:
            # Validate only the diagnostic shape; the original unbound request
            # still fails its ordinary authorization check below.
            diagnostic_context = SearchContext.from_request(
                request,
                replace(context, allowed_scope_ids=context.binding.scope_ids),
                now=self.clock.utc_now(),
                deadline=self.clock.monotonic() + 1,
            )
            trace.context = replace(diagnostic_context, trusted_context=context)
            privacy_state = self._recall_private_state(context)
            trace.enabled = privacy_state[0] is None
            trace.private_marker = privacy_state[1]
        payload = validate_model_request("recall_request", request, context)
        payload["query"] = without_private(payload["query"])[0]
        context = replace(context, recent_messages=tuple(without_private(text)[0] for text in context.recent_messages))
        if deadline_seconds is None:
            effective_deadline = self.config.auto_recall_seconds if payload.get("mode") == "auto" else 2.0
        else:
            if (
                type(deadline_seconds) not in (int, float)
                or not math.isfinite(deadline_seconds)
                or deadline_seconds <= 0
            ):
                raise ContractError("INPUT_INVALID", "deadline_seconds")
            effective_deadline = float(deadline_seconds)
            if payload.get("mode") == "auto":
                effective_deadline = min(effective_deadline, self.config.auto_recall_seconds)
        search_context = SearchContext.from_request(
            payload,
            context,
            now=self.clock.utc_now(),
            deadline=self.clock.monotonic() + effective_deadline,
            current_source_refs=tuple(current_source_refs),
            background_without_evidence=background_without_evidence,
            zone=zone,
            in_context_since=in_context_since,
        )
        trace = CURRENT_TRACE.get()
        if trace is not None:
            trace.context = search_context
        return search_context, effective_deadline

    @logged_recall
    def recall(
        self,
        context: TrustedContext,
        request,
        *,
        current_source_refs: tuple[str, ...] = (),
        deadline_seconds: float | None = None,
        background_without_evidence: bool = True,
        zone: tzinfo | None = None,
    ):
        """Run the sole read-only P08 pipeline for auto and tool callers.

        ``background_without_evidence`` is a trusted caller choice, never a
        request field; see :class:`SearchContext`.
        """
        search_context, _deadline = self._search_context(
            context, request, current_source_refs, deadline_seconds, background_without_evidence, zone
        )
        fence = self._entry_fence(context)
        if self._entry_blocked(context, fence):
            return RetrievalResult((), (), None, ("entry_state:paused",), request_id=search_context.request_id)
        search_context, mode, private_marker = self._recall_entry(search_context)
        if mode == "full":
            return RetrievalResult(
                (), (), None, ("private_mode:full",), "unknown", "unknown", request_id=search_context.request_id
            )
        if self._entry_blocked(context, fence):
            return RetrievalResult((), (), None, ("entry_state:paused",), request_id=search_context.request_id)
        result = self.recall_pipeline.search(search_context)
        try:
            blocked = self._entry_blocked(context, fence)
        except ContractError as exc:
            if exc.code != "DEADLINE_EXCEEDED":
                raise
            return RetrievalResult(
                (), (), None, ("deadline_exceeded_release_fence",), request_id=search_context.request_id
            )
        if blocked:
            return RetrievalResult((), (), None, ("entry_state:paused",), request_id=search_context.request_id)
        final_private = self._recall_private_state(context, after=private_marker)[0]
        if final_private is not None and (trace := CURRENT_TRACE.get()) is not None:
            trace.enabled = False
        if final_private == "full":
            return RetrievalResult(
                (), (), None, ("private_mode:full",), "unknown", "unknown", request_id=search_context.request_id
            )
        return result

    @logged_recall
    def recall_packet(
        self,
        context: TrustedContext,
        request,
        *,
        current_source_refs: tuple[str, ...] = (),
        deadline_seconds: float | None = None,
        background_without_evidence: bool = True,
        zone: tzinfo | None = None,
        in_context_since: str | None = None,
    ):
        """Retrieve once, compile once, and return the public RecallPacket contract.

        Explicit tool lookups pass ``background_without_evidence=False``: a
        query that finds nothing then compiles to ``no_match`` rather than to
        ambient preferences a caller could read as the answer.  A host whose
        model still holds this session's turns says since when
        (``in_context_since``, see :class:`SearchContext`).
        Explicit lookup intent also preserves channel admission and bypasses
        conversational prompt skipping when the requested mode is auto.
        """
        search_context, _effective_deadline = self._search_context(
            context, request, current_source_refs, deadline_seconds, background_without_evidence, zone, in_context_since
        )
        fence = self._entry_fence(context)
        if self._entry_blocked(context, fence):
            return self._paused_recall_packet(search_context)
        search_context, mode, private_marker = self._recall_entry(search_context)
        if mode == "full":
            # Before the vector channel, so a full interval makes no query embedding
            # request and reaches no provider.
            return self._private_recall_packet(search_context)
        if self._entry_blocked(context, fence):
            return self._paused_recall_packet(search_context)
        search_context = replace(search_context, packet_selection=True)
        result = self.recall_pipeline.search(search_context)
        compiler = self.recall_packet_compiler
        # Keep the compiler on the pipeline's current read boundary.  Tests
        # and controlled deployments may replace the injected reader with a
        # barrier/failure port; compiling through the stale constructor-time
        # reader would bypass that port's release fence.
        if compiler.storage_reader is not self.recall_pipeline.storage_reader:
            from .recall_packet import RecallPacketCompiler

            compiler = RecallPacketCompiler(
                self.recall_pipeline.storage_reader,
                diagnostics=self.recall_diagnostics,
                clock=self.clock,
            )
        packet = compiler.compile(
            result.release_context(search_context),
            result,
            self.storage,
        )
        if packet["items"]:
            try:
                blocked = self._entry_blocked(context, fence)
            except ContractError as exc:
                if exc.code != "DEADLINE_EXCEEDED":
                    raise
                packet = self._paused_recall_packet(search_context)
                packet["gaps"] = ["deadline_exceeded_release_fence"]
                return packet
            if blocked:
                return self._paused_recall_packet(search_context)
        validate_payload("recall_packet", dict(packet))
        final_private = self._recall_private_state(context, after=private_marker)[0]
        if final_private is not None and (trace := CURRENT_TRACE.get()) is not None:
            trace.enabled = False
        if final_private == "full":
            return self._private_recall_packet(search_context)
        # A packet remains bound to the entry snapshot that produced it.  The
        # bounded sidecar keeps the public packet schema unchanged.
        with self._packet_fences_lock:
            self._packet_fences[id(packet)] = (packet, context.binding.installation_id, fence)
            while len(self._packet_fences) > 128:
                self._packet_fences.pop(next(iter(self._packet_fences)))
        return packet

    def recall_packet_current(self, context: TrustedContext, packet) -> bool:
        """Whether a packet still belongs to this entry's unchanged recall authority."""
        with self._packet_fences_lock:
            saved = self._packet_fences.get(id(packet))
        return bool(
            saved is not None
            and saved[0] is packet
            and saved[1] == context.binding.installation_id
            and saved[2] == self._entry_fence(context)
            and not self._entry_blocked(context, saved[2])
        )

    def release_recall_packet(self, context: TrustedContext, packet, *, original=None):
        """Fence a packet at its final delivery boundary, including after intermediate transformations."""
        if not packet.get("items") or self.recall_packet_current(context, original if original is not None else packet):
            return packet
        return dict(
            protocol_version="1.1",
            request_id=packet["request_id"],
            status="no_match",
            memory_epoch=None,
            items=[],
            gaps=["entry_state:paused"],
            diagnostic_ref=None,
            answerability="unknown",
            coverage="unknown",
            unmet_needs=[],
        )

    def prepare_recall_render(self, context: TrustedContext, packet):
        """Prepare an injection-ready render context; does not assert host adoption."""
        from .entry_control import entry_key
        from .recall_packet import RecallRenderPreparation, render_recall_packet_context

        if not self.recall_packet_current(context, packet):
            return RecallRenderPreparation(None, None, None)
        with self._packet_fences_lock:
            saved = self._packet_fences.get(id(packet))
        if saved is None:
            return RecallRenderPreparation(None, None, None)
        fence = saved[2]
        version = next(iter(fence.values()))[1] if fence else 0

        result = render_recall_packet_context(
            packet,
            installation_id=context.binding.installation_id,
            session_id=context.session_id,
            renderer=self.recall_packet_renderer,
            entry_id=entry_key(context),
            entry_version=version,
        )
        if self._entry_blocked(context, fence):
            return RecallRenderPreparation(None, None, None)
        return result

    def collection(self, context: TrustedContext, query, *, cursor=None, deadline_seconds: float = 2.0):
        if type(deadline_seconds) not in (int, float) or deadline_seconds <= 0:
            raise ContractError("INPUT_INVALID", "deadline_seconds")
        from .retrieval_storage import CollectionPage

        entry = self._entry_fence(context)
        if self._entry_blocked(context, entry):
            return CollectionPage((), None, "unknown", self.memory_epoch(context), ("entry_state:paused",))
        marker = self._private_read_refusal(context)
        search_context = SearchContext(
            query="collection",
            mode=query.mode,
            as_of=query.as_of,
            focus_refs=(),
            # Collection page size is a storage enumeration bound (1..100),
            # separate from recall delivery's max_items bound (1..30).
            limits=SearchLimits(
                max_items=30,
                budget_tokens=8000,
                candidate_pool=200,
                recent_items=24,
                relation_hops=0,
                relation_objects=0,
                vector_limit=0,
                followups=0,
            ),
            deadline=self.clock.monotonic() + float(deadline_seconds),
            now=self.clock.utc_now(),
            trusted_context=context,
        )
        result = self.recall_pipeline.collection(search_context, query, cursor)
        if self._entry_blocked(context, entry):
            return CollectionPage((), None, "unknown", self.memory_epoch(context), ("entry_state:paused",))
        self._private_read_refusal(context, after=marker)
        return result

    def revise(self, context: TrustedContext, value, *, remaining_seconds: float | None = None):
        from .mutate import revise

        return revise(
            self.storage,
            self.clock,
            context,
            value,
            remaining_seconds=self.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds,
        )

    def current_claim(
        self, context: TrustedContext, ref: str, *, as_of: str | None = None, known_at: str | None = None
    ):
        from .mutate import current_claim

        fence = self._entry_fence(context)
        if self._entry_blocked(context, fence):
            return None
        result = current_claim(self.storage, self.clock, context, ref, as_of=as_of, known_at=known_at)
        return None if self._entry_blocked(context, fence) else result

    def claim_history(self, context: TrustedContext, ref: str):
        fence = self._entry_fence(context)
        if self._entry_blocked(context, fence):
            return ()
        with self.storage.read(context) as tx:
            result = tx.claims.versions(ref)
        return () if self._entry_blocked(context, fence) else result

    def forget(self, context: TrustedContext, value, *, remaining_seconds: float | None = None):
        from .deletion import forget

        return forget(
            self.storage,
            self.clock,
            context,
            value,
            remaining_seconds=self.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds,
        )

    def episodes(self, context, *, limit=200):
        fence = self._entry_fence(context)
        if self._entry_blocked(context, fence):
            return ()
        with self.storage.read(context) as tx:
            result = tx.episodes.list(limit=limit)
        return () if self._entry_blocked(context, fence) else result

    def drain_worker(
        self,
        context: TrustedContext,
        *,
        owner_id: str | None = None,
        max_items: int = 32,
        lease_seconds: float = 60.0,
        remaining_seconds: float | None = None,
        consolidation=None,
        embed=None,
        purge=None,
    ):
        """Explicit bounded drain for the sole work_items queue. Not a background daemon."""
        from .worker import WorkerConfig, build_consolidation_model, drain_worker

        budget = self.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds
        model = consolidation if consolidation is not None else build_consolidation_model(self.consolidation)
        return drain_worker(
            self.storage,
            self.clock,
            context,
            config=WorkerConfig(
                owner_id or self.identifiers.next_id(),
                lease_seconds=lease_seconds,
                max_items=max_items,
                admission_policy=self.config.admission_policy,
            ),
            consolidation=model,
            embed=embed,
            purge=purge,
            remaining_seconds=budget,
        )
