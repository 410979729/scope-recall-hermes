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
from .work_storage import CAPACITY_REFUSALS, MAX_RECOVERY_PAGE
from .worker_candidates import _process_candidate_evaluation
from .worker_consolidation import (
    ConsolidationModel,
    _decode_consolidation_result,
    _process_consolidate,
    build_consolidation_model,
)
from .worker_outcomes import BUDGET_PAUSE_ERRORS, _model_exception_outcome, _remaining
from .worker_projection import (
    EmbedPort,
    PurgePort,
    _process_embed,
    publish_embed_group,
    _process_purge,
    _process_rebuild_projection,
    prepare_embed_group,
)

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
#: Truncated source-trigger pages a pass continues before it claims work.  At
#: one page a pass, a source matching a thousand candidates took sixty passes,
#: started back to back, each paying a whole pass to link sixteen of them.
SOURCE_PAGES_PER_PASS = 16
#: Source embeddings one group may carry.  The adapter sends them as consecutive
#: full provider requests (``adapters/models.py``: a hundred each), so this is
#: how many items share one read, one group commit and one set of per-pass costs
#: -- the numbers a drain is actually paying.  A pass still claims no more than
#: its own ``max_items``.
EMBED_BATCH_LIMIT = 1000


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
    #: Source embeddings a pass may ask for in one request.  One request per
    #: source is what made a vector store of a hundred thousand sources days of
    #: wall clock to rebuild, for minutes of tokens.
    embed_batch_limit: int = EMBED_BATCH_LIMIT
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
        if type(self.max_items) is not int or not 1 <= self.max_items <= 1000:
            raise ValueError("max_items must be between 1 and 1000")
        if type(self.auto_retry_cooldown_seconds) not in (int, float) or not math.isfinite(self.auto_retry_cooldown_seconds) or not 60 <= self.auto_retry_cooldown_seconds <= 86400:
            raise ValueError("auto_retry_cooldown_seconds")
        if type(self.max_auto_recoveries) is not int or not 0 <= self.max_auto_recoveries <= 4:
            raise ValueError("max_auto_recoveries")
        if type(self.purge_only) is not bool:
            raise ValueError("purge_only")
        if type(self.candidate_batch_limit) is not int or not 1 <= self.candidate_batch_limit <= PROCESS_BATCH_LIMIT:
            raise ValueError("candidate_batch_limit")
        if type(self.embed_batch_limit) is not int or not 1 <= self.embed_batch_limit <= EMBED_BATCH_LIMIT:
            raise ValueError("embed_batch_limit")
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
    # One write per page, so a capture never waits behind more than one; the
    # pages may take at most half of the pass.
    for _ in range(SOURCE_PAGES_PER_PASS):
        if _remaining(started, clock, budget) <= budget / 2:
            break
        with storage.write(context, remaining_seconds=min(1.0, _remaining(started, clock, budget))) as tx:
            if not tx.candidates.pending_source_pages():
                break
            tx.candidates.resume_source_pages(now=clock.utc_now())
    page = min(config.candidate_batch_limit, config.max_items)
    with storage.write(context, remaining_seconds=min(1.0, _remaining(started, clock, budget))) as tx:
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


#: Candidate evaluations allowed to wait before a pass stops queueing more.
#:
#: Scheduling settled candidates is how a candidate whose evidence stopped
#: arriving is finally judged, and it ran every pass however deep the queue
#: already was.  A pass evaluates at most ``candidate_batch_limit`` of them, so
#: on another instance the queue grew by eight a pass -- 941 waiting, the oldest ten hours
#: old -- while every one of them cost a model call to get there.  Above this
#: depth the work is already recorded and waiting; adding more only ages it.
#: Twelve passes' worth, an hour at the default wake, so an idle instance clears
#: the ceiling before the next sweep.
CANDIDATE_QUEUE_CEILING = PROCESS_BATCH_LIMIT * 12


#: Seconds a pass may still spend handing untouched leases back once its budget
#: is gone.  The usual reason a group is cut short is that the pass ran out of
#: time, and that was the one case in which nothing was handed back: every item
#: claimed and never looked at kept its spent attempt, and after three such
#: passes it was failed as ``lease_exhausted`` without having been tried once.
#: Seen on an instance whose ``max_items`` had been left at 1000: 888 embeddings
#: in an hour, through all four automatic recoveries.  This is one page of
#: UPDATEs; the runtime keeps a margin of the handed deadline for what follows
#: the drain and the watchdog waits ``KILL_GRACE_SECONDS`` beyond it.
RELEASE_SECONDS = 1.0


def _release_group(storage, clock, context, members, error_code, started: float, budget: float) -> None:
    """Return leased work nobody will look at this pass, without spending its attempt."""
    if not members:
        return
    try:
        with storage.write(context, remaining_seconds=max(_remaining(started, clock, budget), RELEASE_SECONDS)) as tx:
            now = clock.utc_now()
            for member in members:
                tx.work.defer_without_attempt(*member.lease, now=now,
                                              error_code=str(error_code or "pass_ended"), seconds=0)
    except ContractError:
        # The lease expires on its own; a failure to hand work back early is
        # never worth failing a pass over.
        return


def _other_work_ready(storage, clock, context, started: float, budget: float, kinds: frozenset[str]) -> bool:
    """Whether work this pass can still do, other than candidate evaluation, is ready now."""
    if not kinds:
        return False
    with storage.read(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
        return tx.work.other_work_ready(now=clock.utc_now(), kinds=kinds)


def _recover_failed_work(storage, clock, context, config: WorkerConfig, allowed: frozenset[str],
                         started: float, budget: float) -> int:
    """Grant bounded fresh attempts to failures a later fix or budget may have cured."""
    with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
        recovery_page = min(MAX_RECOVERY_PAGE, config.max_items)
        recovered = tx.work.recover_invalid_derivations(
            now=clock.utc_now(), allowed_work_types=allowed, limit=recovery_page)
        if "consolidate" in allowed:
            recovered += tx.work.recover_oversized_consolidations(
                now=clock.utc_now(), formatter=consolidation_messages, limit=min(8, config.max_items))
        if "evaluate_candidate" in allowed:
            # Evidence stops arriving silently, so something has to notice.
            # observe_source no longer schedules while a candidate is still
            # collecting; this is where a settled one finally gets its one
            # evaluation.  See core/candidate_debounce.py.  A pass queues at
            # most what it can also evaluate, and stops queueing entirely once
            # the queue is deeper than passes can reach, so a backlog cannot
            # grow on its own: what is not queued now waits and is queued later.
            if tx.work.pending_depth("evaluate_candidate") < CANDIDATE_QUEUE_CEILING:
                tx.candidates.schedule_settled_candidates(
                    now=clock.utc_now(), limit=min(config.candidate_batch_limit, config.max_items))
            recovered += tx.candidates.reschedule_budget_blocked_candidates(
                now=clock.utc_now(), limit=min(8, config.max_items))
            # The candidate twin of recover_oversized_consolidations, which
            # filters work_type='consolidate' and reformats a single source, so
            # it never reaches these rows.
            recovered += tx.candidates.recover_oversized_evaluations(
                now=clock.utc_now(), formatter=candidate_evaluation_messages, limit=min(8, config.max_items))
            # An attempt begun and never finished leaves nothing recorded; the
            # candidate would wait for evidence it already has.
            recovered += tx.work.recover_interrupted_attempts(
                now=clock.utc_now(), allowed_work_types=allowed, limit=min(8, config.max_items))
        recovered += tx.work.recover_transient_failures(
            now=clock.utc_now(), allowed_work_types=allowed,
            cooldown_seconds=config.auto_retry_cooldown_seconds,
            max_recoveries=config.max_auto_recoveries, limit=recovery_page)
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
    candidate_ceiling = min(config.candidate_batch_limit, config.max_items)
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
        group: tuple = (item,)
        prepared_group: dict = {}
        published_group: frozenset = frozenset()
        if item.work_type == "embed" and embed is not None:
            # Embedding is one request per source; asking for the pass's other
            # ready sources in the same one is the difference between a rebuild
            # measured in hours and one measured in days.  The extra items are
            # leased exactly as this one was and are processed right here, so
            # none is left leased on a pass that ends early.
            room = min(config.embed_batch_limit, config.max_items - len(receipts)) - 1
            if room > 0:
                with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
                    group = (item, *tx.work.claim_next(
                        config.owner_id, clock.utc_now(), lease_seconds=config.lease_seconds,
                        limit=room, allowed_work_types=frozenset({"embed"})))
                prepared_group = prepare_embed_group(storage, clock, context, group,
                                                     embed=embed, started=started, budget=budget)
                # One commit for the group's vectors, for the same reason as one
                # request for its texts: the per-item cost was the store's lock,
                # not the work.
                published_group = publish_embed_group(storage, clock, context, group, embed=embed,
                                                      prepared_group=prepared_group,
                                                      started=started, budget=budget)
        for index, member in enumerate(group):
            claimed_types[member.work_type] += 1
            run = (partial(_process_embed, embed=embed, prepared_group=prepared_group,
                           published_group=published_group)
                   if member.work_type == "embed" else processors[member.work_type])
            outcome = run(storage, clock, context, member, started=started, budget=budget)
            disposition, error_code, state = outcome
            dispositions[disposition] += 1
            receipts.append(WorkerItemReceipt(member.work_id, member.work_type, disposition, state, error_code,
                                              getattr(outcome, "detail", None)))
            item = member
            refused = (disposition == "deferred" and error_code in BUDGET_PAUSE_ERRORS) or (
                str(error_code or "").lower() in _RATE_LIMITED_ERRORS)
            if refused or _remaining(started, clock, budget) <= 0:
                # The rest of this group was leased for a request that is not
                # going to be made. Hand it back unspent rather than holding it
                # until the lease expires.
                _release_group(storage, clock, context, group[index + 1:], error_code, started, budget)
                break
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
            # a fourth instance, with no embedding credential, parked one of 2,548 items
            # every seven seconds.
            paused.append(item.work_type)
        if str(error_code or "").lower() in _RATE_LIMITED_ERRORS:
            # A provider that just answered 429 will answer 429 to the next item
            # too.  Each item backing off on its own is no backoff at all.
            allowed = allowed - {item.work_type}
        if "evaluate_candidate" in allowed and claimed_types["evaluate_candidate"] >= candidate_ceiling:
            # The batch limit is there so a busy candidate queue never holds
            # captured conversation back.  With nothing else ready there is
            # nothing to hold back, and standing down anyway is what let a
            # backlog outlive the passes meant to drain it: another instance evaluated
            # eight a pass while its schedulers queued up to sixteen, so the
            # queue grew by eight a pass with no new conversation at all.
            if candidate_ceiling < config.max_items and not _other_work_ready(
                    storage, clock, context, started, budget, allowed - {"evaluate_candidate"}):
                candidate_ceiling = config.max_items
            else:
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
