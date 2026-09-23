"""Explicit dependencies; host adapters remain outside the core."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import math
import time
from typing import Protocol
import uuid

from ..contracts import ContractError, InstanceBinding, SourceEvent, TrustedContext
from .file_lock import advisory_file_lock
from .storage import SQLiteStorage, StoreStatus, StoredSource
from .capture import CaptureReceipt, record_event
from .admission import AdmissionPolicy
from .retrieval import CandidateRef, SearchContext


class Clock(Protocol):
    def utc_now(self) -> str: ...
    def monotonic(self) -> float: ...


class SystemClock:
    def utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

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


@dataclass(frozen=True)
class CoreConfig:
    binding: InstanceBinding
    write_timeout_seconds: float = 1.0
    auto_recall_seconds: float = 5.0
    admission_policy: AdmissionPolicy = field(default_factory=AdmissionPolicy)

    def __post_init__(self) -> None:
        if not isinstance(self.binding, InstanceBinding):
            raise ValueError("binding must be an InstanceBinding")
        if not isinstance(self.admission_policy, AdmissionPolicy):
            raise ValueError("admission_policy must be an AdmissionPolicy")
        value = self.write_timeout_seconds
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 30:
            raise ValueError("write timeout must be finite and between zero and 30 seconds")
        auto = self.auto_recall_seconds
        if type(auto) not in (int, float) or not math.isfinite(auto) or not 0 < auto <= 5:
            raise ValueError("auto recall timeout must be finite and between zero and five seconds")


class MemoryCore:
    """Application composition. Construction neither opens storage nor calls a model."""

    def __init__(self, config: CoreConfig, *, storage: SQLiteStorage | None = None,
                 clock: Clock | None = None, identifiers: Identifiers | None = None,
                 vectors: VectorPort | None = None,
                 consolidation: ConsolidationPort | None = None,
                 retrieval_policy=None) -> None:
        self.config = config
        self.clock = clock if clock is not None else SystemClock()
        self.identifiers = identifiers if identifiers is not None else RandomIdentifiers()
        self.storage = storage if storage is not None else SQLiteStorage(config.binding, timeout_seconds=config.write_timeout_seconds)
        if self.storage.binding != config.binding:
            raise ValueError("storage binding differs from core binding")
        self.vectors = vectors
        self.consolidation = consolidation
        from .recall import RetrievalPipeline
        from .recall_diagnostics import RecallDiagnostics
        from .recall_packet import RecallPacketCompiler, RecallPacketRenderer

        self.recall_pipeline = RetrievalPipeline(self.storage, vector_port=vectors, policy=retrieval_policy, clock=self.clock)
        self.recall_diagnostics = RecallDiagnostics()
        self.recall_packet_renderer = RecallPacketRenderer()
        self.recall_packet_compiler = RecallPacketCompiler(
            self.recall_pipeline.storage_reader,
            diagnostics=self.recall_diagnostics,
            clock=self.clock,
        )

    def initialize(self) -> StoreStatus:
        """Explicit initialization or an identity-checked supported schema upgrade."""
        return self.storage.initialize()

    def status(self, context: TrustedContext, *, include_admission: bool = True,
               include_queue_age: bool = True) -> StoreStatus:
        """Queue and store standing.  The admission counts scan every source's JSON and the
        queue age walks every queued row, so a caller that only needs the queue's depth says
        so and skips both."""
        with self.storage.read(context) as tx:
            return tx.status(include_admission=include_admission, include_queue_age=include_queue_age)

    def memory_epoch(self, context: TrustedContext) -> int:
        """Fresh identity-checked fence; diagnostics are a separate operation."""
        with self.storage.read(context) as tx:
            return tx.memory_epoch()

    def memory_retracted_since(self, context: TrustedContext, epoch: int) -> bool:
        """Whether a deletion or suppression in this context's scopes was recorded after ``epoch``.

        A host fences what it delivers against this rather than against any epoch move: the epoch
        moves with every capture, and on a store several entries write to, most views were
        compiled one capture ago.
        """
        from .delete_storage import retraction_after

        with self.storage.read(context) as tx:
            return retraction_after(tx._check(), context.allowed_scope_ids, epoch)

    def source_by_event_key(self, context: TrustedContext, source_event_key: str, revision: int = 1, *, remaining_seconds: float | None = None) -> StoredSource | None:
        """Read first-capture timestamps for an authenticated host replay."""
        with self.storage.read(context, remaining_seconds=remaining_seconds) as tx:
            return tx.source_by_event_key(source_event_key, revision)

    def source(self, context: TrustedContext, ref: str, revision: int) -> StoredSource | None:
        with self.storage.read(context) as tx:
            return tx.source(ref, revision)

    def inspect_object(self, context: TrustedContext, ref: str, revision: int | None = None, *, limit: int = 24):
        from .inspect_service import inspect_object
        return inspect_object(self.storage, self.clock, context, ref, revision, limit=limit)

    def profile(self, context: TrustedContext, request):
        """Read-only categorized current-fact profile for one explicit subject."""
        from .read_views import read_profile
        return read_profile(self.storage, self.clock, context, request)

    def trace(self, context: TrustedContext, request, *, deadline_seconds=5.0):
        from .trace import read_trace
        return read_trace(self.storage, self.clock, context, request, seconds=deadline_seconds)

    def entity(self, context: TrustedContext, request):
        """Read-only exact one-hop entity probe or related-statement view."""
        from .read_views import read_entity
        return read_entity(self.storage, self.clock, context, request)


    def record_event(self, context: TrustedContext, value: SourceEvent | dict | str | bytes,
                     *, scope_id: str, remaining_seconds: float | None = None) -> CaptureReceipt:
        return record_event(self.storage, self.clock, context, value, scope_id=scope_id,
                            admission_policy=self.config.admission_policy,
                            remaining_seconds=self.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds)

    def schedule_source(self, context, ref, revision, *, remaining_seconds=None):
        from .admission import schedule_source
        return schedule_source(self.storage, self.clock, context, ref, revision,
                               policy=self.config.admission_policy,
                               remaining_seconds=self.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds)

    def record_host_event(self, context, value, *, scope_id, host_scope, remaining_seconds=1.0):
        """Authenticated host ingress with durable recovery before derivation."""
        from .capture_inbox import durable_record_event
        return durable_record_event(self.storage, self.clock, context, value, scope_id=scope_id,
            host_scope=host_scope, admission_policy=self.config.admission_policy, remaining_seconds=remaining_seconds)

    def resume_deferred(self, context, *, limit=16, remaining_seconds=None):
        from .admission import resume_deferred
        return resume_deferred(self.storage, self.clock, context, self.config.admission_policy,
                               limit=limit,
                               remaining_seconds=self.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds)

    def search_sources(self, context: TrustedContext, query: str, *, limit: int = 20,
                       history: bool = False, automatic: bool = False) -> tuple[StoredSource, ...]:
        with self.storage.read(context) as tx:
            return tx.search_sources(query, limit=limit, history=history, automatic=automatic)

    def recall(self, context: TrustedContext, request, *, current_source_refs: tuple[str, ...] = (), deadline_seconds: float | None = None,
               background_without_evidence: bool = True):
        """Run the sole read-only P08 pipeline for auto and tool callers.

        ``background_without_evidence`` is a trusted caller choice, never a
        request field; see :class:`SearchContext`.
        """
        from ..contracts import ContractError, validate_model_request
        from .retrieval import SearchContext
        payload = validate_model_request("recall_request", request, context)
        if deadline_seconds is None:
            effective_deadline = self.config.auto_recall_seconds if payload.get("mode") == "auto" else 2.0
        else:
            if type(deadline_seconds) not in (int, float) or not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
                raise ContractError("INPUT_INVALID", "deadline_seconds")
            effective_deadline = float(deadline_seconds)
            if payload.get("mode") == "auto":
                effective_deadline = min(effective_deadline, self.config.auto_recall_seconds)
        search_context = SearchContext.from_request(
            request,
            context,
            now=self.clock.utc_now(),
            deadline=self.clock.monotonic() + effective_deadline,
            current_source_refs=tuple(current_source_refs),
            background_without_evidence=background_without_evidence,
        )
        return self.recall_pipeline.search(search_context)

    def recall_packet(self, context: TrustedContext, request, *, current_source_refs: tuple[str, ...] = (), deadline_seconds: float | None = None,
                      background_without_evidence: bool = True):
        """Retrieve once, compile once, and return the public RecallPacket contract.

        Explicit tool lookups pass ``background_without_evidence=False``: a
        query that finds nothing then compiles to ``no_match`` rather than to
        ambient preferences a caller could read as the answer.
        """
        from ..contracts import ContractError, validate_model_request, validate_payload
        from .retrieval import SearchContext

        payload = validate_model_request("recall_request", request, context)
        if deadline_seconds is None:
            effective_deadline = self.config.auto_recall_seconds if payload.get("mode") == "auto" else 2.0
        else:
            if type(deadline_seconds) not in (int, float) or not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
                raise ContractError("INPUT_INVALID", "deadline_seconds")
            effective_deadline = float(deadline_seconds)
            if payload.get("mode") == "auto":
                effective_deadline = min(effective_deadline, self.config.auto_recall_seconds)
        search_context = SearchContext.from_request(
            request,
            context,
            now=self.clock.utc_now(),
            deadline=self.clock.monotonic() + effective_deadline,
            current_source_refs=tuple(current_source_refs),
            background_without_evidence=background_without_evidence,
        )
        # Candidate collection is optional work. Reserve part of the original
        # deadline for the mandatory fresh SQLite release checks and rendering.
        retrieval_context = replace(
            search_context,
            deadline=search_context.deadline - min(1.0, effective_deadline * 0.2),
        )
        result = self.recall_pipeline.search(retrieval_context)
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
            search_context,
            result,
            self.storage,
        )
        validate_payload("recall_packet", dict(packet))
        return packet

    def prepare_recall_render(self, context: TrustedContext, packet):
        """Prepare an injection-ready render context; does not assert host adoption."""
        from .recall_packet import render_recall_packet_context

        return render_recall_packet_context(
            packet,
            installation_id=context.binding.installation_id,
            session_id=context.session_id,
            renderer=self.recall_packet_renderer,
        )

    def collection(self, context: TrustedContext, query, *, cursor=None, deadline_seconds: float = 2.0):
        from .retrieval import SearchContext, SearchLimits
        if type(deadline_seconds) not in (int, float) or deadline_seconds <= 0:
            from ..contracts import ContractError
            raise ContractError("INPUT_INVALID", "deadline_seconds")
        search_context = SearchContext(
            query="collection",
            mode=query.mode,
            as_of=query.as_of,
            focus_refs=(),
            # Collection page size is a storage enumeration bound (1..100),
            # separate from recall delivery's max_items bound (1..30).
            limits=SearchLimits(max_items=30, budget_tokens=8000, candidate_pool=200, recent_items=24, relation_hops=0, relation_objects=0, vector_limit=0, followups=0),
            deadline=self.clock.monotonic() + float(deadline_seconds),
            now=self.clock.utc_now(),
            trusted_context=context,
        )
        return self.recall_pipeline.collection(search_context, query, cursor)

    def accept_claim_proposals(self, context: TrustedContext, value, *, scope_id: str,
                               remaining_seconds: float | None = None):
        from .mutate import accept_claim_proposals
        return accept_claim_proposals(self.storage,self.clock,context,value,scope_id=scope_id,
                                      remaining_seconds=self.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds)

    def revise(self, context: TrustedContext, value, *, remaining_seconds: float | None = None):
        from .mutate import revise
        return revise(self.storage,self.clock,context,value,
                      remaining_seconds=self.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds)

    def current_claim(self, context: TrustedContext, ref: str, *, as_of: str | None = None,
                      known_at: str | None = None):
        from .mutate import current_claim
        return current_claim(self.storage,self.clock,context,ref,as_of=as_of,known_at=known_at)

    def claim_history(self, context: TrustedContext, ref: str):
        with self.storage.read(context) as tx:
            return tx.claims.versions(ref)

    def unresolved_updates(self, context: TrustedContext):
        with self.storage.read(context) as tx:
            return tx.claims.unresolved_updates()

    def repair_claim_frames(self, context: TrustedContext, *, after_ref: str = '',
                            limit: int = 16, remaining_seconds: float | None = None):
        """Revalidate one bounded page of legacy frames without model calls."""
        from .claim_normalization import repair_frames
        with self.storage.write(context, remaining_seconds=self.config.write_timeout_seconds
                                if remaining_seconds is None else remaining_seconds) as tx:
            return repair_frames(tx, now=self.clock.utc_now(), after_ref=after_ref, limit=limit)

    def requalify_claims(self, context: TrustedContext, *, after_ref: str = "",
                         limit: int = 16, dry_run: bool = True,
                         remaining_seconds: float | None = None):
        """Re-judge one bounded page of stored claims after a rule change.

        The preview runs in a read transaction, so it cannot write even by
        mistake; only ``dry_run=False`` opens the write path.
        """
        from .requalify import requalify_claims as _requalify

        seconds = (self.config.write_timeout_seconds if remaining_seconds is None
                   else remaining_seconds)
        opener = self.storage.read if dry_run else self.storage.write
        with opener(context, remaining_seconds=seconds) as tx:
            return _requalify(tx, now=self.clock.utc_now(), after_ref=after_ref,
                              limit=limit, dry_run=dry_run).to_dict()

    def retry_failed_work(self, context: TrustedContext, *, include_terminal: bool = False,
                          limit: int = 64, dry_run: bool = True,
                          remaining_seconds: float | None = None):
        """Grant one bounded re-look to failures a shipped fix may have cured.

        The preview runs in a read transaction, so it cannot write even by
        mistake; only ``dry_run=False`` opens the write path.
        """
        seconds = (self.config.write_timeout_seconds if remaining_seconds is None
                   else remaining_seconds)
        opener = self.storage.read if dry_run else self.storage.write
        with opener(context, remaining_seconds=seconds) as tx:
            return tx.work.retry_failed(now=self.clock.utc_now(), include_terminal=include_terminal,
                                        limit=limit, dry_run=dry_run)

    def forget(self, context: TrustedContext, value, *, remaining_seconds: float | None = None):
        from .deletion import forget
        return forget(self.storage,self.clock,context,value,
                      remaining_seconds=self.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds)

    def purge_sqlite(self, context: TrustedContext, operation_id: str, *, remaining_seconds: float | None = None):
        from .deletion import purge_sqlite
        return purge_sqlite(self.storage,context,operation_id,
                            remaining_seconds=self.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds)

    def release_objects(self, context: TrustedContext, refs, *, expected_epoch: int, automatic: bool = True, history: bool = False):
        from .visibility import release_objects
        return release_objects(self.storage,self.clock,context,refs,expected_epoch=expected_epoch,automatic=automatic,history=history)

    def episodes(self,context,*,limit=200):
        with self.storage.read(context) as tx:
            return tx.episodes.list(limit=limit)

    def episode_sources(self,context,ref,*,after_sequence=0,limit=32):
        with self.storage.read(context) as tx:
            return tx.episodes.sources(ref,after_sequence=after_sequence,limit=limit)

    def accept_consolidation(self,context,value,*,scope_id,remaining_seconds=None):
        from .consolidate import accept_consolidation
        return accept_consolidation(self.storage,self.clock,context,value,scope_id=scope_id,
                                   remaining_seconds=self.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds)

    def register_artifact(self,context,*,remaining_seconds=None,**registration):
        budget = self.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds
        deadline = time.monotonic() + float(budget)
        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())
        try:
            with advisory_file_lock(context.binding.data_directory / "scope-recall-retained.lock", timeout_seconds=remaining()):
                with self.storage.write(context,remaining_seconds=remaining()) as tx:
                    return tx.artifacts.register(**registration,now=self.clock.utc_now())
        except TimeoutError as exc:
            raise ContractError("DEADLINE_EXCEEDED", "retained_lock") from exc

    def artifact(self,context,ref,revision):
        with self.storage.read(context) as tx:
            return tx.artifacts.get(ref,revision)

    def open_artifact(self,context,ref,revision):
        with self.storage.read(context) as tx:
            return tx.artifacts.open(ref,revision)

    def purge_attachments(self,context,operation_id,*,remaining_seconds=None):
        from .retained_artifacts import RetainedBlob, erase_retained
        budget = self.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds
        deadline = time.monotonic() + float(budget)
        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())
        try:
            with advisory_file_lock(context.binding.data_directory / "scope-recall-retained.lock", timeout_seconds=remaining()):
                with self.storage.read(context, remaining_seconds=remaining()) as tx:
                    plan = tx.deletions.attachment_plan(operation_id)
                if plan.get("already_done"):
                    with self.storage.read(context, remaining_seconds=remaining()) as tx:
                        return tx.deletions.receipt(operation_id)
                for entry in plan["entries"]:
                    if remaining() <= 0:
                        raise ContractError("DEADLINE_EXCEEDED")
                    if entry.get("shared"):
                        continue
                    erase_retained(context.binding, RetainedBlob(**entry["blob"]))
                if remaining() <= 0:
                    raise ContractError("DEADLINE_EXCEEDED")
                with self.storage.write(context, remaining_seconds=remaining()) as tx:
                    return tx.deletions.finalize_attachments(operation_id, plan, erased=True)
        except TimeoutError as exc:
            raise ContractError("DEADLINE_EXCEEDED", "retained_lock") from exc

    def reference(self,context,ref,revision=None):
        with self.storage.read(context) as tx:
            return tx.references.get(ref,revision)

    def drain_worker(self, context: TrustedContext, *, owner_id: str | None = None, max_items: int = 32,
                      lease_seconds: float = 60.0, remaining_seconds: float | None = None,
                      consolidation=None, embed=None, purge=None):
        """Explicit bounded drain for the sole work_items queue. Not a background daemon."""
        from .worker import WorkerConfig, build_consolidation_model, drain_worker
        budget = self.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds
        model = consolidation if consolidation is not None else build_consolidation_model(self.consolidation)
        return drain_worker(
            self.storage,
            self.clock,
            context,
            config=WorkerConfig(owner_id or self.identifiers.next_id(), lease_seconds=lease_seconds, max_items=max_items,
                                admission_policy=self.config.admission_policy),
            consolidation=model,
            embed=embed,
            purge=purge,
            remaining_seconds=budget,
        )
