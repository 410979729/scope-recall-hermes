"""Explicit dependencies; host adapters remain outside the core."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import time
from typing import Protocol
import uuid

from ..contracts import InstanceBinding, SourceEvent, TrustedContext
from .storage import SQLiteStorage, StoreStatus, StoredSource
from .capture import CaptureReceipt, record_event


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


from .retrieval import CandidateRef, SearchContext


class VectorPort(Protocol):
    def search(self, context: SearchContext, *, limit: int, remaining_seconds: float) -> tuple[CandidateRef, ...]: ...


class ConsolidationPort(Protocol):
    def propose(self, sources: tuple[SourceEvent, ...], *, remaining_seconds: float) -> str: ...


@dataclass(frozen=True)
class CoreConfig:
    binding: InstanceBinding
    write_timeout_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.binding, InstanceBinding):
            raise ValueError("binding must be an InstanceBinding")
        value = self.write_timeout_seconds
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 30:
            raise ValueError("write timeout must be finite and between zero and 30 seconds")


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
        self.recall_pipeline = RetrievalPipeline(self.storage, vector_port=vectors, policy=retrieval_policy, clock=self.clock)

    def initialize(self) -> StoreStatus:
        """Explicit initialization for a new target; never upgrades an existing schema."""
        return self.storage.initialize()

    def status(self, context: TrustedContext) -> StoreStatus:
        with self.storage.read(context) as tx:
            return tx.status()

    def source(self, context: TrustedContext, ref: str, revision: int) -> StoredSource | None:
        with self.storage.read(context) as tx:
            return tx.source(ref, revision)

    def record_event(self, context: TrustedContext, value: SourceEvent | dict | str | bytes,
                     *, scope_id: str, remaining_seconds: float | None = None) -> CaptureReceipt:
        return record_event(self.storage, self.clock, context, value, scope_id=scope_id,
                            remaining_seconds=self.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds)

    def search_sources(self, context: TrustedContext, query: str, *, limit: int = 20,
                       history: bool = False, automatic: bool = False) -> tuple[StoredSource, ...]:
        with self.storage.read(context) as tx:
            return tx.search_sources(query, limit=limit, history=history, automatic=automatic)

    def recall(self, context: TrustedContext, request, *, current_source_refs: tuple[str, ...] = (), deadline_seconds: float = 2.0):
        """Run the sole read-only P08 pipeline for auto and tool callers."""
        from ..contracts import ContractError, validate_model_request
        from .retrieval import SearchContext
        if type(deadline_seconds) not in (int, float) or deadline_seconds <= 0:
            raise ContractError("INPUT_INVALID", "deadline_seconds")
        payload = validate_model_request("recall_request", request, context)
        effective_deadline = float(deadline_seconds)
        if payload.get("mode") == "auto":
            effective_deadline = min(effective_deadline, 1.5)
        search_context = SearchContext.from_request(
            request,
            context,
            now=self.clock.utc_now(),
            deadline=self.clock.monotonic() + effective_deadline,
            current_source_refs=tuple(current_source_refs),
        )
        return self.recall_pipeline.search(search_context)

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
        with self.storage.read(context) as tx:return tx.episodes.list(limit=limit)

    def episode_sources(self,context,ref,*,after_sequence=0,limit=32):
        with self.storage.read(context) as tx:return tx.episodes.sources(ref,after_sequence=after_sequence,limit=limit)

    def accept_consolidation(self,context,value,*,scope_id,remaining_seconds=None):
        from .consolidate import accept_consolidation
        return accept_consolidation(self.storage,self.clock,context,value,scope_id=scope_id,
                                   remaining_seconds=self.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds)

    def register_artifact(self,context,*,remaining_seconds=None,**registration):
        with self.storage.write(context,remaining_seconds=self.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds) as tx:
            return tx.artifacts.register(**registration,now=self.clock.utc_now())

    def artifact(self,context,ref,revision):
        with self.storage.read(context) as tx:return tx.artifacts.get(ref,revision)

    def open_artifact(self,context,ref,revision):
        with self.storage.read(context) as tx:return tx.artifacts.open(ref,revision)

    def purge_attachments(self,context,operation_id,*,remaining_seconds=None):
        with self.storage.write(context,remaining_seconds=self.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds) as tx:
            return tx.deletions.purge_attachments(operation_id)

    def reference(self,context,ref,revision=None):
        with self.storage.read(context) as tx:return tx.references.get(ref,revision)
