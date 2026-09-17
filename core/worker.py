"""Bounded durable-work drain. Model and vector calls stay outside SQLite transactions."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from functools import partial
import math

from ..contracts import ContractError, TrustedContext
from .admission import resume_deferred
from .candidate_lifecycle import PROCESS_BATCH_LIMIT, CandidateEvaluator, candidate_evaluation_messages
from .consolidate import consolidation_messages
from .storage import SQLiteStorage
from .work_storage import CAPACITY_REFUSALS
from .worker_candidates import _process_candidate_evaluation
from .worker_consolidation import (
    ConsolidationModel,
    _decode_consolidation_result,
    _process_consolidate,
    build_consolidation_model,
)
from .worker_outcomes import BUDGET_PAUSE_ERRORS, _model_exception_outcome, _remaining
from .worker_projection import EmbedPort, PurgePort, _process_embed, _process_purge, _process_rebuild_projection

__all__ = [
    "FINALIZE_MARGIN_SECONDS",
    "ConsolidationModel",
    "EmbedPort",
    "PurgePort",
    "WorkerConfig",
    "WorkerItemReceipt",
    "WorkerReceipt",
    "build_consolidation_model",
    "drain_worker",
    # Reached through this module by the contract tests.
    "_decode_consolidation_result",
    "_model_exception_outcome",
    "_process_consolidate",
    "_process_embed",
]

#: Provider answers that mean "send less traffic".  Distinct from the
#: recoverable set: that one says whether an item may be retried, this one
#: decides whether the rest of the pass should even ask.  One name, one
#: definition -- two hand-kept copies of this set drifted apart once already.
_RATE_LIMITED_ERRORS = CAPACITY_REFUSALS

#: Seconds a model-bound item keeps back, beyond its one bounded request, to
#: record the verdict under its lease: a few fenced SQLite writes and, for
#: embed, the vector publish.  Finalization refuses to write once the pass is
#: spent, so an item claimed without this stays leased until its lease expires,
#: and a candidate evaluation, whose attempt marker is already committed, is
#: then failed as interrupted for good.
FINALIZE_MARGIN_SECONDS = 5.0
#: Work that waits on an auxiliary provider; purge and projection rebuilds never do.
_HOLDABLE_WORK_TYPES = frozenset({"consolidate", "embed", "evaluate_candidate"})


@dataclass(frozen=True)
class WorkerConfig:
    owner_id: str
    lease_seconds: float = 60.0
    max_items: int = 32
    auto_retry_cooldown_seconds: float = 3600.0
    max_auto_recoveries: int = 2
    purge_only: bool = False
    admission_policy: object | None = None
    candidate_batch_limit: int = PROCESS_BATCH_LIMIT
    #: The bound the caller clamps every model and embedding request to (the
    #: runtime's ``request_seconds``).  None means the ports are unbounded and
    #: each call gets whatever the pass has left, so no reserve can be known.
    request_seconds: float | None = None
    #: Work types whose provider is on hold (runtime/model_budget.py): not
    #: claimed at all this pass, so no item is leased only to be parked.
    held_work_types: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if type(self.owner_id) is not str or not self.owner_id:
            raise ValueError("owner_id is required")
        if type(self.lease_seconds) not in (int, float) or not math.isfinite(self.lease_seconds) or self.lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if type(self.max_items) is not int or not 1 <= self.max_items <= 200:
            raise ValueError("max_items must be between 1 and 200")
        if type(self.auto_retry_cooldown_seconds) not in (int, float) or not math.isfinite(self.auto_retry_cooldown_seconds) or not 60 <= self.auto_retry_cooldown_seconds <= 86400:
            raise ValueError("auto_retry_cooldown_seconds")
        if type(self.max_auto_recoveries) is not int or not 0 <= self.max_auto_recoveries <= 4:
            raise ValueError("max_auto_recoveries")
        if type(self.purge_only) is not bool:
            raise ValueError("purge_only")
        if type(self.candidate_batch_limit) is not int or not 1 <= self.candidate_batch_limit <= PROCESS_BATCH_LIMIT:
            raise ValueError("candidate_batch_limit")
        if self.request_seconds is not None and (
                type(self.request_seconds) not in (int, float) or not math.isfinite(self.request_seconds)
                or self.request_seconds <= 0):
            raise ValueError("request_seconds must be positive")
        if type(self.held_work_types) is not frozenset or not self.held_work_types <= _HOLDABLE_WORK_TYPES:
            raise ValueError("held_work_types")


@dataclass(frozen=True)
class WorkerItemReceipt:
    work_id: int
    work_type: str
    disposition: str
    state: str
    error_code: str | None = None
    error_detail: str | None = None


@dataclass(frozen=True)
class WorkerReceipt:
    processed: int
    completed: int
    failed: int
    retried: int
    skipped: int
    stale: int
    obsolete: int
    idle: bool
    items: tuple[WorkerItemReceipt, ...]
    deferred: int = 0
    recovered: int = 0
    unavailable_work_types: tuple[str, ...] = ()


def _resume_admission(storage, clock, context, config: WorkerConfig, started: float, budget: float,
                      *, candidate_available: bool) -> None:
    """Wake deferred captures and settle candidate pages before any claim."""
    resume_deferred(storage, clock, context, config.admission_policy, limit=min(16, config.max_items),
                    remaining_seconds=min(1.0, _remaining(started, clock, budget)))
    page = min(config.candidate_batch_limit, config.max_items)
    with storage.write(context, remaining_seconds=min(1.0, _remaining(started, clock, budget))) as tx:
        tx.candidates.resume_source_pages(now=clock.utc_now())
        tx.candidates.backfill(now=clock.utc_now(), limit=page)
        tx.candidates.archive_dormant(now=clock.utc_now(), limit=page)
        if not candidate_available:
            tx.candidates.mark_capability_unavailable()


def _queued_work_types(storage, clock, context, started: float, budget: float, kinds: list[str]) -> tuple[str, ...]:
    """Which of ``kinds`` have queued rows, so the receipt reports only real gaps."""
    if not kinds:
        return ()
    with storage.read(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
        visible, params = tx.work._visible_filter()
        queued = {row[0] for row in tx._check().execute(
            f"SELECT DISTINCT work_type FROM work_items WHERE state IN ('pending','failed','leased') AND {visible}",
            params)}
    return tuple(kind for kind in kinds if kind in queued)


def _recover_failed_work(storage, clock, context, config: WorkerConfig, allowed: frozenset[str],
                         started: float, budget: float) -> int:
    """Grant bounded fresh attempts to failures a later fix or budget may have cured."""
    with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
        recovered = tx.work.recover_invalid_derivations(
            now=clock.utc_now(), allowed_work_types=allowed, limit=config.max_items)
        if "consolidate" in allowed:
            recovered += tx.work.recover_oversized_consolidations(
                now=clock.utc_now(), formatter=consolidation_messages, limit=min(8, config.max_items))
        if "evaluate_candidate" in allowed:
            # Evidence stops arriving silently, so something has to notice.
            # observe_source no longer schedules while a candidate is still
            # collecting; this is where a settled one finally gets its one
            # evaluation.  See core/candidate_debounce.py.
            tx.candidates.schedule_settled_candidates(now=clock.utc_now(), limit=min(16, config.max_items))
            recovered += tx.candidates.reschedule_budget_blocked_candidates(
                now=clock.utc_now(), limit=min(8, config.max_items))
            # The candidate twin of recover_oversized_consolidations, which
            # filters work_type='consolidate' and reformats a single source, so
            # it never reaches these rows.
            recovered += tx.candidates.recover_oversized_evaluations(
                now=clock.utc_now(), formatter=candidate_evaluation_messages, limit=min(8, config.max_items))
        recovered += tx.work.recover_transient_failures(
            now=clock.utc_now(), allowed_work_types=allowed,
            cooldown_seconds=config.auto_retry_cooldown_seconds,
            max_recoveries=config.max_auto_recoveries, limit=config.max_items)
    return recovered


def drain_worker(
    storage: SQLiteStorage,
    clock,
    context: TrustedContext,
    *,
    config: WorkerConfig,
    consolidation: ConsolidationModel | None = None,
    candidate: CandidateEvaluator | None = None,
    embed: EmbedPort | None = None,
    purge: PurgePort | None = None,
    remaining_seconds: float = 30.0,
) -> WorkerReceipt:
    """Process ready work until idle or the bounded limit is reached."""
    if type(remaining_seconds) not in (int, float) or not math.isfinite(remaining_seconds) or remaining_seconds <= 0:
        raise ContractError("DEADLINE_EXCEEDED")
    started = clock.monotonic()
    budget = remaining_seconds
    if candidate is None and callable(getattr(consolidation, "evaluate_candidate", None)):
        candidate = consolidation  # type: ignore[assignment]
    # Missing optional ports are a capability state, not attempted model work.
    # Their items remain pending and become eligible at the next configured wakeup.
    ports = {"consolidate": consolidation, "embed": embed, "evaluate_candidate": candidate}
    if config.purge_only:
        allowed = frozenset({"purge"})
        unavailable: tuple[str, ...] = ()
    else:
        allowed = frozenset({"purge", "rebuild_projection",
                             *(kind for kind, port in ports.items() if port is not None)}) - config.held_work_types
        _resume_admission(storage, clock, context, config, started, budget, candidate_available=candidate is not None)
        unavailable = _queued_work_types(storage, clock, context, started, budget,
                                         [kind for kind, port in ports.items() if port is None])
    recovered = _recover_failed_work(storage, clock, context, config, allowed, started, budget)
    processors = {
        "consolidate": partial(_process_consolidate, model=consolidation),
        "embed": partial(_process_embed, embed=embed),
        "evaluate_candidate": partial(_process_candidate_evaluation, evaluator=candidate),
        "rebuild_projection": _process_rebuild_projection,
        "purge": partial(_process_purge, purge=purge),
    }
    receipts: list[WorkerItemReceipt] = []
    dispositions: Counter[str] = Counter()
    claimed_types: Counter[str] = Counter()
    # Every optional port is one bounded model or embedding request.  Its work
    # is claimed only while the pass still covers that request and the margin
    # to record the verdict; purge and rebuild_projection are local work and
    # may use the tail of the pass.
    model_bound = frozenset(ports)
    reserve = 0.0 if config.request_seconds is None else config.request_seconds + FINALIZE_MARGIN_SECONDS
    paused: list[str] = []
    while len(receipts) < config.max_items and _remaining(started, clock, budget) > 0:
        if _remaining(started, clock, budget) < reserve:
            allowed = allowed - model_bound
        # Every second claim prefers fresh conversation over the backlog.  The
        # first claim of a pass stays FIFO, so the lane holds at most half of
        # any pass, a one-claim pass included, and the oldest work keeps moving
        # however busy the chat is.  A default runtime pass (120 s, 45 s
        # requests) fits two model requests, so each such pass reaches the lane.
        with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
            claimed = tx.work.claim_next(config.owner_id, clock.utc_now(), lease_seconds=config.lease_seconds,
                                         limit=1, allowed_work_types=allowed, fresh_lane=len(receipts) % 2 == 1)
        if not claimed:
            break
        item = claimed[0]
        claimed_types[item.work_type] += 1
        outcome = processors[item.work_type](storage, clock, context, item, started=started, budget=budget)
        disposition, error_code, state = outcome
        dispositions[disposition] += 1
        receipts.append(WorkerItemReceipt(item.work_id, item.work_type, disposition, state, error_code,
                                          getattr(outcome, "detail", None)))
        # Standing a work type down for the rest of the pass.  The per-item
        # backoff still decides when each item returns; this only decides how
        # many of one type are tried in one pass.
        if disposition == "deferred" and error_code in BUDGET_PAUSE_ERRORS:
            # The port refused before any attempt (budget, credentials) and
            # would refuse the next item the same way.  A long source that
            # checkpointed a page is deferred too, but without a refusal code:
            # it is pending again and says nothing about the port.
            allowed = allowed - {item.work_type}
            # Reported like a missing port, so the supervisor sleeps the type
            # instead of starting the next pass at once for the next item:
            # delta, with no embedding credential, parked one of 2,548 items
            # every seven seconds.
            paused.append(item.work_type)
        if str(error_code or "").lower() in _RATE_LIMITED_ERRORS:
            # A provider that just answered 429 will answer 429 to the next item
            # too.  Each item backing off on its own is no backoff at all.
            allowed = allowed - {item.work_type}
        if claimed_types["evaluate_candidate"] >= config.candidate_batch_limit:
            allowed = allowed - {"evaluate_candidate"}
        if _remaining(started, clock, budget) <= 0:
            break
    return WorkerReceipt(
        processed=len(receipts),
        completed=dispositions["completed"],
        failed=dispositions["failed"],
        retried=dispositions["retry"],
        skipped=dispositions["skipped"],
        stale=dispositions["stale"],
        obsolete=dispositions["obsolete"],
        idle=not receipts,
        items=tuple(receipts),
        deferred=dispositions["deferred"],
        recovered=recovered,
        unavailable_work_types=tuple(dict.fromkeys((*unavailable, *paused))),
    )
