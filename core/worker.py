"""Bounded durable-work drain. Model and vector calls stay outside SQLite transactions."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable, Protocol

from ..contracts import ContractError, TrustedContext
from .file_lock import advisory_file_lock
from .consolidate import consolidation_messages
from .candidate_lifecycle import (
    PROCESS_BATCH_LIMIT,
    CandidateEvaluator,
    candidate_evaluation_messages,
)
from .retained_artifacts import RetainedBlob, erase_retained
from .storage import SQLiteStorage, StoredSource
from .worker_outcomes import (
    _Outcome as _Outcome,
    _remaining as _remaining,
    _work_result as _work_result,
    _deadline_result as _deadline_result,
    _model_exception_outcome as _model_exception_outcome,
    _finalize_work as _finalize_work,
)
from .worker_consolidation import (
    ConsolidationModel as ConsolidationModel,
    _fit_prompt_budget as _fit_prompt_budget,
    _episode_batch as _episode_batch,
    _root_only_sources as _root_only_sources,
    _decode_consolidation_result as _decode_consolidation_result,
    _process_consolidate as _process_consolidate,
    build_consolidation_model as build_consolidation_model,
)
from .worker_candidates import (
    _candidate_failure as _candidate_failure,
    _candidate_obsolete as _candidate_obsolete,
    _process_candidate_evaluation as _process_candidate_evaluation,
)


class EmbedPort(Protocol):
    def prepare_source(self, source: StoredSource, *, remaining_seconds: float) -> object: ...
    def publish_source(
        self,
        prepared: object,
        *,
        source: StoredSource,
        lease_token: int,
        lease_owner: str,
        lease_guard: Callable[[], bool],
        remaining_seconds: float,
    ) -> None: ...


class PurgePort(Protocol):
    """Physical active-vector purge boundary.

    Implementations run outside SQLite and must return only after the native
    delete has acknowledged the same operation.  The work lease is checked
    again before the acknowledgement is recorded in SQLite.
    """

    def purge_active(
        self,
        operation_id: str,
        *,
        receipt: dict,
        remaining_seconds: float,
    ) -> bool: ...


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


def _process_embed(
    storage,
    clock,
    context,
    item,
    *,
    embed: EmbedPort | None,
    started: float,
    budget: float,
) -> tuple[str, str | None, str]:
    source_invalid = False
    memory_epoch = None
    with storage.read(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
        memory_epoch = tx.status().memory_epoch
        source = tx.source(item.subject_ref, item.subject_revision)
        if source is not None:
            try:
                tx.claims.require_live_source(source.ref, source.revision)
            except ContractError:
                source_invalid = True
    if source is None:
        return _finalize_work(storage, clock, context, item, "obsolete", "authority_revoked", started=started, budget=budget)
    if source_invalid:
        return _finalize_work(storage, clock, context, item, "obsolete", "authority_revoked", started=started, budget=budget)
    if embed is None:
        return _finalize_work(storage, clock, context, item, "retry", "model_unavailable", started=started, budget=budget)
    try:
        prepared = embed.prepare_source(source, remaining_seconds=_remaining(started, clock, budget))
    except ContractError as exc:
        return _finalize_work(
            storage,
            clock,
            context,
            item,
            "failed" if exc.code in {"DERIVATION_INVALID", "INPUT_INVALID"} else "retry",
            exc.code or "model_unavailable",
            started=started,
            budget=budget,
        )
    except Exception as exc:
        outcome = _model_exception_outcome(exc)
        if outcome is None:
            return _finalize_work(storage, clock, context, item, "retry", "model_unavailable", started=started, budget=budget)
        return _finalize_work(storage, clock, context, item, outcome[0], outcome[1], started=started, budget=budget)
    if _remaining(started, clock, budget) <= 0:
        return _deadline_result(storage, context, item)

    epoch_changed = False

    def lease_guard() -> bool:
        nonlocal epoch_changed
        epoch_changed = False
        remaining = _remaining(started, clock, budget)
        if remaining <= 0:
            return False
        try:
            with storage.read(context, remaining_seconds=remaining) as tx:
                if not tx.work._verify_lease(item.work_id, item.lease_token, item.lease_owner, now=clock.utc_now()):
                    return False
                current = tx.source(item.subject_ref, item.subject_revision)
                if current is None or current.scope_id not in context.allowed_scope_ids:
                    return False
                if (current.project_id, current.branch_id) != (context.project_id, context.branch_id):
                    return False
                tx.claims.require_live_source(current.ref, current.revision)
                if tx.status().memory_epoch != memory_epoch:
                    epoch_changed = True
                    return False
                return True
        except ContractError:
            return False

    # The worker owns the first fence check.  A port must call lease_guard
    # again at its physical publication boundary, but it is never entered
    # after a stale/deleted/epoch-changed source is observed here.
    if not lease_guard():
        if _remaining(started, clock, budget) <= 0:
            return _deadline_result(storage, context, item)
        if epoch_changed:
            return _finalize_work(storage, clock, context, item, "retry", "memory_epoch_changed", started=started, budget=budget)
        return _finalize_work(storage, clock, context, item, "obsolete", "authority_revoked", started=started, budget=budget)

    try:
        embed.publish_source(
            prepared,
            source=source,
            lease_token=item.lease_token,
            lease_owner=item.lease_owner,
            lease_guard=lease_guard,
            remaining_seconds=_remaining(started, clock, budget),
        )
    except ContractError as exc:
        if epoch_changed:
            return _finalize_work(storage, clock, context, item, "retry", "memory_epoch_changed", started=started, budget=budget)
        return _finalize_work(
            storage,
            clock,
            context,
            item,
            "failed" if exc.code in {"DERIVATION_INVALID", "INPUT_INVALID"} else "retry",
            exc.code or "model_unavailable",
            started=started,
            budget=budget,
        )
    except Exception as exc:
        outcome = _model_exception_outcome(exc)
        if outcome is None:
            return _finalize_work(storage, clock, context, item, "retry", "model_unavailable", started=started, budget=budget)
        return _finalize_work(storage, clock, context, item, outcome[0], outcome[1], started=started, budget=budget)
    now = clock.utc_now()
    if _remaining(started, clock, budget) <= 0:
        return _deadline_result(storage, context, item)
    with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
        current = tx.source(item.subject_ref, item.subject_revision)
        if current is None:
            return _work_result(
                tx.work.mark_obsolete(item.work_id, item.lease_token, item.lease_owner, now=now),
                error_code="authority_revoked",
            )
        try:
            tx.claims.require_live_source(current.ref, current.revision)
        except ContractError:
            return _work_result(
                tx.work.mark_obsolete(item.work_id, item.lease_token, item.lease_owner, now=now),
                error_code="authority_revoked",
            )
        if tx.status().memory_epoch != memory_epoch:
            return _work_result(
                tx.work.fail(item.work_id, item.lease_token, item.lease_owner,
                             error_code="memory_epoch_changed", now=now, recoverable=True),
                error_code="memory_epoch_changed",
            )
        return _work_result(tx.work.complete(item.work_id, item.lease_token, item.lease_owner, now=now))


#: Provider answers that mean "send less traffic". Distinct from the recoverable
#: set: those describe whether one item may be retried, this decides whether the
#: rest of the pass should even ask.
#: A provider declining to serve anyone.  Derived from ``work_storage`` rather
#: than listed again: the two said the same thing about 429 and 503 while
#: disagreeing about 502 and 504, which is how a pair of hand-kept sets always
#: ends up.  One name, one definition.
from .work_storage import CAPACITY_REFUSALS

_RATE_LIMITED_ERRORS = CAPACITY_REFUSALS


def _process_embed_claim(
    storage,
    clock,
    context,
    item,
    *,
    embed: EmbedPort | None,
    started: float,
    budget: float,
) -> tuple[str, str | None, str]:
    """Embed one claim version so the derived layer is reachable by search.

    A sibling of ``_process_embed`` rather than a branch inside it: that path is
    densely fenced for at-most-once publication against a live source, and the
    liveness question for a claim is a different one (``require_target`` on a
    version, not ``require_live_source`` on an event). Keeping them apart leaves
    the source fence exactly as it was.

    A port without claim support is a capability gap, not a failure: the work
    item waits instead of burning its attempts.
    """
    prepare = getattr(embed, "prepare_claim", None) if embed is not None else None
    publish = getattr(embed, "publish_claim", None) if embed is not None else None
    if embed is None or prepare is None or publish is None:
        return _finalize_work(storage, clock, context, item, "retry", "model_unavailable",
                              started=started, budget=budget)
    with storage.read(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
        memory_epoch = tx.status().memory_epoch
        versions = tx.claims.versions(item.subject_ref)
        version = next((value for value in versions if value.revision == item.subject_revision), None)
        if version is not None:
            try:
                tx.claims.require_target(version)
            except ContractError:
                version = None
    if version is None:
        return _finalize_work(storage, clock, context, item, "obsolete", "authority_revoked",
                              started=started, budget=budget)
    try:
        prepared = prepare(version, remaining_seconds=_remaining(started, clock, budget))
    except ContractError as exc:
        return _finalize_work(storage, clock, context, item,
                              "failed" if exc.code in {"DERIVATION_INVALID", "INPUT_INVALID"} else "retry",
                              exc.code or "model_unavailable", started=started, budget=budget)
    except Exception as exc:
        outcome = _model_exception_outcome(exc)
        if outcome is None:
            return _finalize_work(storage, clock, context, item, "retry", "model_unavailable",
                                  started=started, budget=budget)
        return _finalize_work(storage, clock, context, item, outcome[0], outcome[1],
                              started=started, budget=budget)
    if _remaining(started, clock, budget) <= 0:
        return _deadline_result(storage, context, item)

    epoch_changed = False

    def lease_guard() -> bool:
        nonlocal epoch_changed
        epoch_changed = False
        remaining = _remaining(started, clock, budget)
        if remaining <= 0:
            return False
        try:
            with storage.read(context, remaining_seconds=remaining) as tx:
                if not tx.work._verify_lease(item.work_id, item.lease_token, item.lease_owner, now=clock.utc_now()):
                    return False
                live = tx.claims.versions(item.subject_ref)
                current = next((value for value in live if value.revision == item.subject_revision), None)
                if current is None or current.scope_id not in context.allowed_scope_ids:
                    return False
                if (current.project_id, current.branch_id) != (context.project_id, context.branch_id):
                    return False
                tx.claims.require_target(current)
                if tx.status().memory_epoch != memory_epoch:
                    epoch_changed = True
                    return False
                return True
        except ContractError:
            return False

    if not lease_guard():
        if _remaining(started, clock, budget) <= 0:
            return _deadline_result(storage, context, item)
        if epoch_changed:
            return _finalize_work(storage, clock, context, item, "retry", "memory_epoch_changed",
                                  started=started, budget=budget)
        return _finalize_work(storage, clock, context, item, "obsolete", "authority_revoked",
                              started=started, budget=budget)
    try:
        publish(
            prepared,
            claim=version,
            lease_token=item.lease_token,
            lease_owner=item.lease_owner,
            lease_guard=lease_guard,
            remaining_seconds=_remaining(started, clock, budget),
        )
    except ContractError as exc:
        if epoch_changed:
            return _finalize_work(storage, clock, context, item, "retry", "memory_epoch_changed",
                                  started=started, budget=budget)
        return _finalize_work(storage, clock, context, item,
                              "failed" if exc.code in {"DERIVATION_INVALID", "INPUT_INVALID"} else "retry",
                              exc.code or "model_unavailable", started=started, budget=budget)
    except Exception as exc:
        outcome = _model_exception_outcome(exc)
        if outcome is None:
            return _finalize_work(storage, clock, context, item, "retry", "model_unavailable",
                                  started=started, budget=budget)
        return _finalize_work(storage, clock, context, item, outcome[0], outcome[1],
                              started=started, budget=budget)
    now = clock.utc_now()
    if _remaining(started, clock, budget) <= 0:
        return _deadline_result(storage, context, item)
    with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
        live = tx.claims.versions(item.subject_ref)
        current = next((value for value in live if value.revision == item.subject_revision), None)
        if current is None:
            return _work_result(
                tx.work.mark_obsolete(item.work_id, item.lease_token, item.lease_owner, now=now),
                error_code="authority_revoked",
            )
        try:
            tx.claims.require_target(current)
        except ContractError:
            return _work_result(
                tx.work.mark_obsolete(item.work_id, item.lease_token, item.lease_owner, now=now),
                error_code="authority_revoked",
            )
        if tx.status().memory_epoch != memory_epoch:
            return _work_result(
                tx.work.fail(item.work_id, item.lease_token, item.lease_owner,
                             error_code="memory_epoch_changed", now=now, recoverable=True),
                error_code="memory_epoch_changed",
            )
        return _work_result(tx.work.complete(item.work_id, item.lease_token, item.lease_owner, now=now))


def _process_rebuild_projection(storage, clock, context, item, *, started: float, budget: float) -> tuple[str, str | None, str]:
    now = clock.utc_now()
    if _remaining(started, clock, budget) <= 0:
        return _deadline_result(storage, context, item)
    with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
        if not tx.work._verify_lease(item.work_id, item.lease_token, item.lease_owner, now=now):
            return "stale", None, tx.work.read_state(item.work_id) or "stale"
        source = tx.source(item.subject_ref, item.subject_revision)
        if source is not None:
            tx.index_source(source.ref, source.revision)
        else:
            # Claim revisions also enqueue this type.  Claims are queried from
            # their versioned SQLite tables in the current core, so completing
            # this durable item is the explicit projection boundary; a missing
            # or inaccessible claim revision is obsolete rather than a source
            # indexing failure.
            versions = tx.claims.versions(item.subject_ref)
            version = next((value for value in versions if value.revision == item.subject_revision), None)
            if version is None:
                return _work_result(
                    tx.work.mark_obsolete(item.work_id, item.lease_token, item.lease_owner, now=now),
                    error_code="authority_revoked",
                )
            try:
                tx.claims.require_target(version)
            except ContractError:
                return _work_result(
                    tx.work.mark_obsolete(item.work_id, item.lease_token, item.lease_owner, now=now),
                    error_code="authority_revoked",
                )
        return _work_result(tx.work.complete(item.work_id, item.lease_token, item.lease_owner, now=now))


def _process_purge(
    storage,
    clock,
    context,
    item,
    *,
    purge: PurgePort | None,
    started: float,
    budget: float,
) -> tuple[str, str | None, str]:
    operation_id = item.subject_ref.rsplit(":", 1)[0]
    now = clock.utc_now()
    if _remaining(started, clock, budget) <= 0:
        return _deadline_result(storage, context, item)
    lock_path = context.binding.data_directory / "scope-recall-retained.lock"
    # The same resource lock is also taken by register_artifact in the core
    # composition.  It spans the attachment inventory snapshot, physical I/O,
    # and final metadata CAS, while no SQLite transaction is held during I/O.
    with advisory_file_lock(lock_path, timeout_seconds=_remaining(started, clock, budget)):
        with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
            if not tx.work._verify_lease(item.work_id, item.lease_token, item.lease_owner, now=now):
                return "stale", None, tx.work.read_state(item.work_id) or "stale"
            receipt = tx.deletions.receipt(operation_id)
            if receipt is None:
                return _work_result(
                    tx.work.mark_obsolete(item.work_id, item.lease_token, item.lease_owner, now=now),
                    error_code="authority_revoked",
                )
            physical_members = tx.deletions.physical_members(operation_id)
            attachment_plan = tx.deletions.attachment_plan(operation_id)
            if receipt["layers"].get("sqlite_active") != "removed":
                receipt = tx.deletions.purge_sqlite(operation_id)
            receipt = dict(receipt, physical_members=physical_members)

        if receipt["layers"].get("vector_active") != "removed":
            if purge is None:
                return _finalize_work(storage, clock, context, item, "retry", "storage_unavailable", started=started, budget=budget)
            remaining = _remaining(started, clock, budget)
            if remaining <= 0:
                return _deadline_result(storage, context, item)
            try:
                acknowledged = bool(purge.purge_active(operation_id, receipt=receipt, remaining_seconds=remaining))
            except ContractError as exc:
                return _finalize_work(storage, clock, context, item, "retry", exc.code or "storage_unavailable", started=started, budget=budget)
            except Exception:
                return _finalize_work(storage, clock, context, item, "retry", "storage_unavailable", started=started, budget=budget)
            if not acknowledged:
                return _finalize_work(storage, clock, context, item, "retry", "storage_unavailable", started=started, budget=budget)

        # Attachment erasure is intentionally outside SQLite.  An empty plan
        # is still an inventory result and can be acknowledged as removed.
        for entry in attachment_plan.get("entries", ()):
            if _remaining(started, clock, budget) <= 0:
                return _deadline_result(storage, context, item)
            if entry.get("shared"):
                continue
            try:
                erase_retained(context.binding, RetainedBlob(**entry["blob"]))
            except ContractError as exc:
                return _finalize_work(storage, clock, context, item, "retry", exc.code or "storage_unavailable", started=started, budget=budget)
            except Exception:
                return _finalize_work(storage, clock, context, item, "retry", "storage_unavailable", started=started, budget=budget)

        remaining = _remaining(started, clock, budget)
        if remaining <= 0:
            return _deadline_result(storage, context, item)
        now = clock.utc_now()
        try:
            with storage.write(context, remaining_seconds=remaining) as tx:
                if not tx.work._verify_lease(item.work_id, item.lease_token, item.lease_owner, now=now):
                    return "stale", None, tx.work.read_state(item.work_id) or "stale"
                receipt = tx.deletions.receipt(operation_id)
                if receipt is None:
                    return _work_result(
                        tx.work.mark_obsolete(item.work_id, item.lease_token, item.lease_owner, now=now),
                        error_code="authority_revoked",
                    )
                if receipt["layers"].get("vector_active") != "removed":
                    receipt = tx.deletions.mark_vector_active_removed(operation_id)
                receipt = tx.deletions.finalize_attachments(operation_id, attachment_plan, erased=True)
                if not receipt["active_content_removed"]:
                    return _work_result(
                        tx.work.fail(item.work_id, item.lease_token, item.lease_owner,
                                     error_code="storage_unavailable", now=now, recoverable=True)
                    )
                return _work_result(tx.work.complete(item.work_id, item.lease_token, item.lease_owner, now=now))
        except Exception:
            # Physical layers may already be gone when the final CAS fails.
            # Keep the one work item retryable; the next pass inventories and
            # idempotently acknowledges both native and attachment layers.
            return _finalize_work(storage, clock, context, item, "retry", "storage_unavailable", started=started, budget=budget)


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
    receipts: list[WorkerItemReceipt] = []
    completed = failed = retried = skipped = stale = obsolete = deferred = 0
    processed = 0
    # Missing optional ports are a capability state, not attempted model work.
    # They remain pending and become eligible at the next configured wakeup.
    kinds = {"purge", "rebuild_projection"}
    unavailable = []
    candidate_evaluator = candidate
    if candidate_evaluator is None and callable(getattr(consolidation, "evaluate_candidate", None)):
        candidate_evaluator = consolidation  # type: ignore[assignment]
    for kind, port in (("consolidate", consolidation), ("embed", embed),
                       ("evaluate_candidate", candidate_evaluator)):
        if port is not None and not config.purge_only:
            kinds.add(kind)
        elif not config.purge_only:
            unavailable.append(kind)
    if config.purge_only:
        kinds = {"purge"}
    allowed = frozenset(kinds)
    if not config.purge_only:
        from .admission import resume_deferred
        resume_deferred(storage, clock, context, config.admission_policy, limit=min(16, config.max_items),
                        remaining_seconds=min(1.0, _remaining(started, clock, remaining_seconds)))
        with storage.write(context, remaining_seconds=min(1.0, _remaining(started, clock, remaining_seconds))) as tx:
            tx.candidates.resume_source_pages(now=clock.utc_now())
            tx.candidates.backfill(now=clock.utc_now(), limit=min(config.candidate_batch_limit, config.max_items))
            tx.candidates.archive_dormant(now=clock.utc_now(), limit=min(config.candidate_batch_limit, config.max_items))
            if candidate_evaluator is None:
                tx.candidates.mark_capability_unavailable()
    with storage.read(context, remaining_seconds=_remaining(started, clock, remaining_seconds)) as tx:
        scopes = sorted(context.allowed_scope_ids)
        marks = ",".join("?" for _ in scopes)
        context_filter, context_params = tx.work._context_filter()
        pending_types = {row[0] for row in tx._check().execute(
            f"""SELECT DISTINCT work_type FROM work_items WHERE state IN ('pending','failed','leased')
                AND scope_id IN ({marks}) {context_filter}""", (*scopes, *context_params))}
        unavailable = [kind for kind in unavailable if kind in pending_types]
    with storage.write(context, remaining_seconds=_remaining(started, clock, remaining_seconds)) as tx:
        recovered = tx.work.recover_invalid_derivations(
            now=clock.utc_now(), allowed_work_types=allowed, limit=config.max_items)
        if "consolidate" in allowed:
            recovered += tx.work.recover_oversized_consolidations(
                now=clock.utc_now(),formatter=consolidation_messages,limit=min(8,config.max_items))
        if "evaluate_candidate" in allowed:
            # Same repair for the candidate path. Its failures never reach the
            # sibling above: that one filters work_type='consolidate' and
            # reformats a single source, and the candidate worker lowercases its
            # error codes past that one's exact-case filter.
            # The evidence bound changed, so a candidate whose set was too large
            # yields a different, smaller one now. Re-scheduling gives it a new
            # fingerprint and a genuinely new evaluation; an unchanged set
            # collides on that fingerprint and inserts nothing, so this cannot
            # loop.
            # Evidence stops arriving silently, so something has to notice.
            # observe_source no longer schedules while a candidate is still
            # collecting; this is where a settled one finally gets its one
            # evaluation.  See core/candidate_debounce.py.
            tx.candidates.schedule_settled_candidates(
                now=clock.utc_now(), limit=min(16, max(1, config.max_items)))
            recovered += tx.candidates.reschedule_budget_blocked_candidates(
                now=clock.utc_now(), limit=min(8, config.max_items))
            recovered += tx.candidates.recover_oversized_evaluations(
                now=clock.utc_now(),formatter=candidate_evaluation_messages,limit=min(8,config.max_items))
        recovered += tx.work.recover_transient_failures(
            now=clock.utc_now(), allowed_work_types=allowed,
            cooldown_seconds=config.auto_retry_cooldown_seconds,
            max_recoveries=config.max_auto_recoveries, limit=config.max_items)
    candidate_processed = 0
    while processed < config.max_items and _remaining(started, clock, remaining_seconds) > 0:
        with storage.write(context, remaining_seconds=_remaining(started, clock, remaining_seconds)) as tx:
            claimed = tx.work.claim_next(
                config.owner_id,
                clock.utc_now(),
                lease_seconds=config.lease_seconds,
                limit=min(config.max_items - processed, 1),
                allowed_work_types=allowed,
            )
        if not claimed:
            break
        item = claimed[0]
        processed += 1
        error_detail = None
        if item.work_type == "consolidate":
            outcome = _process_consolidate(
                storage,
                clock,
                context,
                item,
                model=consolidation,
                started=started,
                budget=remaining_seconds,
            )
            disposition, error_code, state = outcome
            error_detail = getattr(outcome, "detail", None)
        elif item.work_type == "embed":
            # One work type, two subjects. The ref prefix is the discriminator
            # the rest of the core already uses for object identity.
            processor = _process_embed_claim if item.subject_ref.startswith("claim-") else _process_embed
            disposition, error_code, state = processor(
                storage,
                clock,
                context,
                item,
                embed=embed,
                started=started,
                budget=remaining_seconds,
            )
        elif item.work_type == "evaluate_candidate":
            if candidate_evaluator is None:
                raise AssertionError("candidate work claimed without evaluator")
            disposition, error_code, state = _process_candidate_evaluation(
                storage,
                clock,
                context,
                item,
                evaluator=candidate_evaluator,
                started=started,
                budget=remaining_seconds,
            )
            candidate_processed += 1
            if candidate_processed >= config.candidate_batch_limit:
                allowed = allowed - {"evaluate_candidate"}
        elif item.work_type == "rebuild_projection":
            disposition, error_code, state = _process_rebuild_projection(
                storage,
                clock,
                context,
                item,
                started=started,
                budget=remaining_seconds,
            )
        elif item.work_type == "purge":
            try:
                disposition, error_code, state = _process_purge(
                    storage,
                    clock,
                    context,
                    item,
                    purge=purge,
                    started=started,
                    budget=remaining_seconds,
                )
            except TimeoutError:
                disposition, error_code, state = _finalize_work(
                    storage, clock, context, item, "retry", "storage_unavailable",
                    started=started, budget=remaining_seconds,
                )
        else:
            now = clock.utc_now()
            with storage.write(context, remaining_seconds=_remaining(started, clock, remaining_seconds)) as tx:
                mutation = tx.work.fail(
                    item.work_id,
                    item.lease_token,
                    item.lease_owner,
                    error_code="INPUT_INVALID",
                    now=now,
                    recoverable=False,
                )
                disposition, error_code, state = mutation.disposition, "INPUT_INVALID", mutation.state
        if disposition == "completed":
            completed += 1
        elif disposition == "failed":
            failed += 1
        elif disposition == "retry":
            retried += 1
        elif disposition == "skipped":
            skipped += 1
        elif disposition == "stale":
            stale += 1
        elif disposition == "obsolete":
            obsolete += 1
        elif disposition == "deferred":
            deferred += 1
            allowed = allowed - {item.work_type}
        if str(error_code or "").lower() in _RATE_LIMITED_ERRORS:
            # A provider that just answered 429 will answer 429 to the next item
            # too. Each item backing off on its own is no backoff at all: seven
            # different work items burned an attempt each inside nine seconds
            # against a provider plainly asking for less traffic. Stand this work
            # type down for the rest of the pass — the per-item backoff still
            # decides when each one returns.
            allowed = allowed - {item.work_type}
        receipts.append(WorkerItemReceipt(item.work_id, item.work_type, disposition, state, error_code, error_detail))
        if _remaining(started, clock, remaining_seconds) <= 0:
            break
    idle = processed == 0
    return WorkerReceipt(processed, completed, failed, retried, skipped, stale, obsolete, idle,
                         tuple(receipts), deferred, recovered, tuple(unavailable))
