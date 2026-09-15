"""Bounded durable-work drain. Model and vector calls stay outside SQLite transactions."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
from typing import Callable, Protocol

from ..contracts import ContractError, TrustedContext, decode_payload, validate_payload
from ..file_lock import advisory_file_lock
from .consolidate import ConsolidationWorkFence, accept_consolidation, consolidation_messages
from .consolidation_chunks import source_chunk
from .candidate_lifecycle import (
    PROCESS_BATCH_LIMIT,
    CandidateEvaluator,
    candidate_evaluation_messages,
    candidate_subject_matches,
)
from .episodes import source_origin
from .retained_artifacts import RetainedBlob, erase_retained
from .storage import SQLiteStorage, StoredSource


class ConsolidationModel(Protocol):
    def propose(self, sources: tuple[StoredSource, ...], *, episode_ref: str | None, remaining_seconds: float) -> str: ...


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


class _Outcome(tuple):
    """A (disposition, error_code, state) result that can carry the contract field.

    Acceptance raises ContractError with both a code and a field, but only the
    code reaches ``work_items.last_error_code`` -- and that column is pinned to
    an exact value by contract tests, so the field cannot be appended to it.
    Subclassing tuple lets the field ride along to the receipt while every
    existing three-way unpack keeps working untouched.
    """

    def __new__(cls, disposition, error_code, state, detail=None):
        value = super().__new__(cls, (disposition, error_code, state))
        value.detail = detail
        return value


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


_ROOT_ORIGINS = frozenset({"human_direct", "tool_observation", "external_document", "imported"})


def _remaining(started: float, clock, budget: float) -> float:
    return max(0.0, budget - (clock.monotonic() - started))


def _work_result(mutation, *, error_code: str | None = None) -> tuple[str, str | None, str]:
    return mutation.disposition, error_code, mutation.state


def _deadline_result(storage, context, item) -> tuple[str, str, str]:
    with storage.read(context) as tx:
        return "skipped", "DEADLINE_EXCEEDED", tx.work.read_state(item.work_id) or "stale"


def _fit_prompt_budget(
    episode_ref: str | None,
    batch: list[StoredSource],
    subject: tuple[str, int],
) -> tuple[list[StoredSource], list[StoredSource]]:
    """Keep the consolidation batch inside the serialized model-input budget.

    The subject source is always kept because the acceptance fence requires
    its coverage.  Other sources are added in episode order only while the
    exact prompt built by ``consolidation_messages`` still fits; deferred
    sources stay pending for a later bounded pass.  Without this the whole
    episode stalls: an over-budget batch raises INPUT_INVALID on every
    attempt and no work item ever completes.
    """
    kept = [stored for stored in batch if (stored.ref, stored.revision) == subject]
    if not kept:
        return list(batch), []
    deferred: list[StoredSource] = []
    for stored in batch:
        if (stored.ref, stored.revision) == subject:
            continue
        candidate = kept + [stored]
        try:
            consolidation_messages(tuple(candidate), episode_ref=episode_ref)
        except ContractError as exc:
            if exc.code == "INPUT_INVALID" and exc.field == "consolidation_input_budget":
                deferred.append(stored)
                continue
            raise
        kept.append(stored)
    order = {id(stored): index for index, stored in enumerate(batch)}
    kept.sort(key=lambda stored: order[id(stored)])
    return kept, deferred


def _episode_batch(tx, source: StoredSource, item, *, now: str):
    episode = tx.episodes.source_episode(source.ref, source.revision)
    if episode is None:
        return None, (source,), ()
    prior = tx._check().execute(
        """SELECT v.processed_sequence,v.resume_json,ee.sequence FROM episode_versions v
           JOIN episode_events ee ON ee.episode_id=v.episode_id
           WHERE v.episode_id=? AND v.revision=? AND ee.source_ref=? AND ee.source_revision=?""",
        (episode.ref, episode.revision, source.ref, source.revision),
    ).fetchone()
    processed_sequence = prior["processed_sequence"] if prior and prior["resume_json"] else 0
    if prior and prior["sequence"] <= processed_sequence:
        return episode.ref, (), ()
    batch_rows = tx._check().execute(
        """SELECT ee.sequence,ee.source_ref,ee.source_revision,w.lease_token,w.work_id
           FROM episode_events ee JOIN work_items w ON w.work_type='consolidate'
             AND w.subject_ref=ee.source_ref AND w.subject_revision=ee.source_revision
           WHERE ee.episode_id=? AND ee.sequence>? AND w.scope_id=?
             AND w.project_id IS ? AND w.branch_id IS ?
             AND (w.work_id=? OR (w.state='pending' AND w.available_at<=? AND w.consolidation_offset=0))
           ORDER BY (w.work_id=?) DESC,ee.sequence LIMIT 32""",
        (episode.ref, processed_sequence, source.scope_id, source.project_id, source.branch_id,
         item.work_id, now, item.work_id),
    ).fetchall()
    batch, pending = [], []
    lease_tokens: dict[tuple[str, int], int] = {}
    for row in sorted(batch_rows, key=lambda row: row["sequence"]):
        stored = tx.source(row["source_ref"], row["source_revision"])
        if stored is None:
            continue
        try:
            tx.claims.require_live_source(stored.ref, stored.revision)
        except ContractError:
            continue
        batch.append(stored)
        lease_tokens[(stored.ref, stored.revision)] = row["lease_token"]
        if row["work_id"] != item.work_id:
            pending.append((stored.ref, stored.revision, row["lease_token"]))
    batch, deferred = _fit_prompt_budget(episode.ref, batch, (source.ref, source.revision))
    if deferred:
        pending_keys = {(ref, revision) for ref, revision, _token in pending}
        for stored in deferred:
            key = (stored.ref, stored.revision)
            if key not in pending_keys:
                pending.append((stored.ref, stored.revision, lease_tokens[key]))
    return episode.ref, tuple(batch), tuple(pending)


def _root_only_sources(tx, sources: tuple[StoredSource, ...]) -> tuple[StoredSource, ...]:
    roots: list[StoredSource] = []
    seen: set[tuple[str, int]] = set()
    for source in sources:
        origin = source_origin(source)
        if origin not in _ROOT_ORIGINS:
            continue
        key = (source.ref, source.revision)
        if key in seen:
            continue
        # A partial/interrupted capture remains durable evidence with gaps,
        # but it is not a root from which the model may derive durable claims.
        if source.capture_gaps or source.event.get("capture_state") != "complete":
            continue
        seen.add(key)
        try:
            tx.claims.require_live_source(source.ref, source.revision)
        except ContractError:
            continue
        roots.append(source)
    return tuple(roots)


_NON_RETRYABLE_MODEL_ERRORS = frozenset({
    "budget_exhausted",
    "meter_breach",
    "input_invalid",
    "budget_unavailable",
})


def _model_exception_outcome(exc: BaseException) -> tuple[str, str] | None:
    """Keep auxiliary error types; do not relabel budget failures as missing model."""
    error_type = getattr(exc, "error_type", None)
    if type(error_type) is not str or not error_type or len(error_type) > 80:
        return None
    if any(character.isspace() for character in error_type):
        return None
    if error_type == "http_status":
        status = getattr(exc, "detail", "")
        if type(status) is str and status.isdigit() and len(status) == 3:
            code = int(status)
            return ("retry" if code == 429 or 500 <= code <= 599 else "failed", f"http_{code}")
    disposition = "failed" if error_type in _NON_RETRYABLE_MODEL_ERRORS else "retry"
    return disposition, error_type


def _finalize_work(storage, clock, context, item, disposition: str, error_code: str | None, *, started: float, budget: float,
                   error_detail: str | None = None, stage: str = "process") -> tuple[str, str | None, str]:
    now = clock.utc_now()
    if _remaining(started, clock, budget) <= 0:
        with storage.read(context) as tx:
            return "skipped", "DEADLINE_EXCEEDED", tx.work.read_state(item.work_id) or "stale"
    if disposition in {"completed", "stale"}:
        with storage.read(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
            state = tx.work.read_state(item.work_id) or "stale"
        return disposition, error_code, state
    with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
        conn = tx._check(write=True)
        if not tx.work._verify_lease(item.work_id, item.lease_token, item.lease_owner, now=now):
            return "stale", error_code, tx.work.read_state(item.work_id) or "stale"
        if error_code:
            # Store metadata only, never raw model output or exception bodies.
            import re
            field = error_detail if isinstance(error_detail, str) and re.fullmatch(r"[A-Za-z0-9_./\[\]-]{1,240}", error_detail) else None
            conn.execute("INSERT INTO work_error_details(work_id,lease_token,stage,error_code,error_field,recorded_at) VALUES (?,?,?,?,?,?)",
                         (item.work_id, item.lease_token, stage, error_code, field, now))
            conn.execute("DELETE FROM work_error_details WHERE work_id=? AND detail_id NOT IN (SELECT detail_id FROM work_error_details WHERE work_id=? ORDER BY detail_id DESC LIMIT 16)", (item.work_id, item.work_id))
            attempt = conn.execute("SELECT attempt FROM work_items WHERE work_id=?", (item.work_id,)).fetchone()[0]
            if item.work_type == "consolidate" and error_code.lower() == "derivation_invalid" and attempt >= 3:
                # Extraction has exhausted its bounded attempts. Raw evidence
                # remains searchable; no failed proposal is promoted to fact.
                tx.claims.require_live_source(item.subject_ref, item.subject_revision)
                tx.work.complete(item.work_id, item.lease_token, item.lease_owner, now=now)
                conn.execute("UPDATE work_items SET last_error_code=? WHERE work_id=?", (error_code, item.work_id))
                conn.execute("INSERT OR REPLACE INTO consolidation_outcomes VALUES (?,?,?,?)",
                             (item.work_id, "source_only", "structured_extraction_failed", now))
                conn.execute("DELETE FROM consolidation_fragments WHERE work_id=?", (item.work_id,))
                return _Outcome("source_only", error_code, "done", detail=field)
        if error_code in {"budget_exhausted", "budget_unavailable", "credential_missing", "credential_shape_invalid"}:
            return _work_result(tx.work.defer_without_attempt(
                item.work_id, item.lease_token, item.lease_owner, now=now,
                error_code=error_code, seconds=3600), error_code=error_code)
        if disposition == "obsolete":
            return _work_result(tx.work.mark_obsolete(item.work_id, item.lease_token, item.lease_owner, now=now), error_code=error_code)
        if disposition == "failed":
            return _work_result(
                tx.work.fail(
                    item.work_id,
                    item.lease_token,
                    item.lease_owner,
                    error_code=error_code or "derivation_invalid",
                    now=now,
                    recoverable=False,
                ),
                error_code=error_code,
            )
        return _work_result(
            tx.work.fail(
                item.work_id,
                item.lease_token,
                item.lease_owner,
                error_code=error_code or "model_unavailable",
                now=now,
                recoverable=True,
            ),
            error_code=error_code,
        )


def _decode_consolidation_result(raw: str, sources=()) -> dict:
    """Decode the model envelope without relaxing the consolidation contract.

    Some OpenAI-compatible gateways/models still wrap a JSON-object response in
    a single ``json`` code fence despite ``response_format=json_object``.  The
    fence is transport decoration, so accepting it is safe only when it is the
    complete outer envelope.  Likewise, ``location`` is an optional evidence
    field; a model's ``null`` for it has exactly the same meaning as omission.
    All other shape, source, quote, and authority checks remain in the existing
    validators below.
    """
    if type(raw) is not str:
        raise ValueError("consolidation_result_not_text")
    candidate = raw.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) < 3 or lines[0].strip() not in {"```", "```json"} or lines[-1].strip() != "```":
            raise ValueError("consolidation_result_fence")
        candidate = "\n".join(lines[1:-1]).strip()
    value = decode_payload(candidate)
    source_text = {(s.ref, s.revision): s.event["content"] for s in sources}
    claims = value.get("claim_proposals")
    if isinstance(claims, list):
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            for kind_field in ("procedure", "intention", "alias"):
                if claim.get("kind") != kind_field and claim.get(kind_field) in (None, {}):
                    claim.pop(kind_field, None)
            spans = claim.get("evidence_spans")
            if not isinstance(spans, list):
                continue
            for span in spans:
                if isinstance(span, dict) and span.get("location") is None and "location" in span:
                    del span["location"]
                if not isinstance(span, dict) or not isinstance(span.get("quote"), str):
                    continue
                content = source_text.get((span.get("source_ref"), span.get("source_revision")), "")
                quote = span["quote"]
                if quote not in content:
                    # The model may quote the decoded value of serialized JSON.
                    # Re-escape once only when that exact, unique stored span
                    # exists. No fuzzy matching, truncation or text generation.
                    encoded = json.dumps(quote, ensure_ascii=False)[1:-1]
                    if encoded != quote and content.count(encoded) == 1 and len(encoded) <= 4096:
                        span["quote"] = encoded
                        for field in ("subject", "predicate", "value_text"):
                            fragment = claim.get(field)
                            if isinstance(fragment, str) and fragment in quote and fragment not in encoded:
                                claim[field] = json.dumps(fragment, ensure_ascii=False)[1:-1]
    return validate_payload("consolidation_result", value)


def _process_consolidate(
    storage,
    clock,
    context,
    item,
    *,
    model: ConsolidationModel | None,
    started: float,
    budget: float,
) -> tuple[str, str | None, str]:
    memory_epoch = None
    batch, pending_sources = (), ()
    chunk, offset = None, 0
    seed = ()
    with storage.read(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
        memory_epoch = tx.status().memory_epoch
        if not tx.work._verify_lease(item.work_id, item.lease_token, item.lease_owner, now=clock.utc_now()):
            state = tx.work.read_state(item.work_id) or "stale"
            return "stale", "authority_revoked" if state == "obsolete" else None, state
        source = tx.source(item.subject_ref, item.subject_revision)
        if source is not None:
            try:
                tx.claims.require_live_source(source.ref, source.revision)
            except ContractError:
                source = None
        if source is None:
            episode_ref, roots = None, ()
        else:
            offset = tx.work.consolidation_offset(item.work_id, item.lease_token, item.lease_owner,
                                                 now=clock.utc_now())
            if offset:
                from .consolidation_summary import resume_seed
                seed = resume_seed(tx, item.work_id)
                episode = tx.episodes.source_episode(source.ref, source.revision)
                episode_ref, batch, pending_sources = episode.ref if episode else None, (source,), ()
            else:
                episode_ref, batch, pending_sources = _episode_batch(tx, source, item, now=clock.utc_now())
            roots = _root_only_sources(tx, batch)
    # Do not open a write transaction while the read transaction above is
    # still active.  SQLite's reader lock otherwise turns an obsolete source
    # into a spurious "database is locked" failure.
    now = clock.utc_now()
    if source is None:
        return _finalize_work(storage, clock, context, item, "obsolete", "authority_revoked", started=started, budget=budget)
    if not roots:
        if _remaining(started, clock, budget) <= 0:
            return _deadline_result(storage, context, item)
        with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
            now = clock.utc_now()
            if not tx.work._verify_lease(item.work_id, item.lease_token, item.lease_owner, now=now):
                return _work_result(tx.work.complete(item.work_id, item.lease_token, item.lease_owner, now=now))
            try:
                tx.claims.require_live_source(item.subject_ref, item.subject_revision)
                for stored in batch:
                    tx.claims.require_live_source(stored.ref, stored.revision)
                if tx.status().memory_epoch != memory_epoch:
                    raise ContractError("VERSION_CONFLICT", "memory_epoch")
            except ContractError as exc:
                if exc.code == "VERSION_CONFLICT" and exc.field == "memory_epoch":
                    return _work_result(
                        tx.work.fail(item.work_id, item.lease_token, item.lease_owner,
                                     error_code="memory_epoch_changed", now=now, recoverable=True),
                        error_code="memory_epoch_changed",
                    )
                return _work_result(
                    tx.work.mark_obsolete(item.work_id, item.lease_token, item.lease_owner, now=now),
                    error_code="authority_revoked",
                )
            return _work_result(tx.work.complete_consolidation(
                item.work_id, item.lease_token, item.lease_owner, now=now,
                covered_source_refs=frozenset(f"{s.ref}@{s.revision}" for s in batch) | {f"{source.ref}@{source.revision}"},
                pending_sources=pending_sources,
            ))
    if model is None:
        return _finalize_work(storage, clock, context, item, "retry", "model_unavailable", started=started, budget=budget)
    try:
        needs_chunk = bool(offset)
        if not needs_chunk:
            try:
                consolidation_messages(roots, episode_ref=episode_ref)
            except ContractError as exc:
                if exc.code != "INPUT_INVALID" or exc.field != "consolidation_input_budget":
                    raise
                needs_chunk = True
        if needs_chunk:
            page, chunk = source_chunk(source, offset, formatter=consolidation_messages,
                                       episode_ref=episode_ref, resume_seed=seed)
            roots, batch, pending_sources = (page,), (source,), ()
        allowed_refs = frozenset(f"{stored.ref}@{stored.revision}" for stored in roots)
        raw = model.propose(roots, episode_ref=episode_ref, remaining_seconds=_remaining(started, clock, budget))
    except ContractError as exc:
        # A probabilistic model occasionally emits an invalid derivation; give
        # the work item a bounded fresh attempt instead of killing it on the
        # first bad generation (attempts stay capped by MAX_RECOVERABLE_ATTEMPTS).
        return _finalize_work(
            storage,
            clock,
            context,
            item,
            "retry",
            exc.code or "model_unavailable",
            started=started,
            budget=budget,
            error_detail=exc.field, stage="prepare_or_model",
        )
    except Exception as exc:
        outcome = _model_exception_outcome(exc)
        if outcome is None:
            return _finalize_work(storage, clock, context, item, "retry", "model_unavailable", started=started, budget=budget)
        return _finalize_work(storage, clock, context, item, outcome[0], outcome[1], started=started, budget=budget)
    try:
        value = _decode_consolidation_result(raw, roots)
    except (ContractError, ValueError, TypeError, json.JSONDecodeError) as exc:
        detail = exc.field if isinstance(exc, ContractError) else "json_envelope"
        return _Outcome(*_finalize_work(storage, clock, context, item, "retry", "derivation_invalid", started=started, budget=budget,
                                      error_detail=detail, stage="decode"), detail=detail)
    fence = ConsolidationWorkFence(
        item.work_id,
        item.lease_token,
        item.lease_owner,
        item.subject_ref,
        item.subject_revision,
        memory_epoch,
        allowed_refs,
        skipped_source_refs=frozenset(f"{s.ref}@{s.revision}" for s in batch) - allowed_refs,
        pending_sources=pending_sources,
        chunk=chunk,
    )
    try:
        accept_consolidation(
            storage,
            clock,
            # A durable work item runs outside the originating host session.
            # Use the session from its freshly read source, never from model
            # output, so current_human can validate that session's actual
            # latest evidence. Scope, project and the work/epoch fence remain
            # unchanged; stale or unrelated human evidence still fails.
            replace(context, session_id=source.session_id, recent_messages=()),
            value,
            scope_id=item.scope_id,
            remaining_seconds=_remaining(started, clock, budget),
            work_fence=fence,
        )
    except ContractError as exc:
        code = exc.code or "derivation_invalid"
        # Reject this result, but retain live work for a fresh bounded attempt.
        # An unrelated capture also advances the instance-wide epoch.
        if code == "VERSION_CONFLICT" and exc.field == "memory_epoch":
            return _finalize_work(storage, clock, context, item, "retry", "memory_epoch_changed", started=started, budget=budget)
        if code in {"SOURCE_MISSING", "VERSION_CONFLICT", "ACCESS_DENIED"}:
            return _finalize_work(storage, clock, context, item, "obsolete", "authority_revoked", started=started, budget=budget)
        recoverable = code in {"STORAGE_UNAVAILABLE", "DEADLINE_EXCEEDED", "DERIVATION_INVALID"}
        disposition = "retry" if recoverable else "failed"
        # Keep the clause that rejected the result. Without it a terminal
        # DERIVATION_INVALID cannot be told apart from any other, and diagnosing
        # one costs a full reproduction against live work.
        return _Outcome(
            *_finalize_work(storage, clock, context, item, disposition, code, started=started, budget=budget,
                            error_detail=exc.field, stage="accept"),
            detail=exc.field,
        )
    if chunk is not None and not chunk.final:
        return "deferred", None, "pending"
    return "completed", None, "done"


def _candidate_failure(storage, clock, context, item, *, evaluation_id: int,
                       code: str, started: float, budget: float,
                       field: str | None = None, budget_pause: bool = False):
    if _remaining(started, clock, budget) <= 0:
        return _deadline_result(storage, context, item)
    with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
        now = clock.utc_now()
        if not tx.work._verify_lease(item.work_id, item.lease_token, item.lease_owner, now=now):
            return "stale", code, tx.work.read_state(item.work_id) or "stale"
        if budget_pause:
            mutation = tx.candidates.defer_budget(evaluation_id, item, now=now, code=code)
        else:
            safe_field = field if isinstance(field, str) and len(field) <= 240 else None
            mutation = tx.candidates.fail(evaluation_id, item, now=now, code=code, field=safe_field)
        return _work_result(mutation, error_code=code)


def _candidate_obsolete(storage, clock, context, item, *, evaluation_id: int,
                        reason: str, started: float, budget: float):
    if _remaining(started, clock, budget) <= 0:
        return _deadline_result(storage, context, item)
    with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
        now = clock.utc_now()
        if not tx.work._verify_lease(item.work_id, item.lease_token, item.lease_owner, now=now):
            return "stale", reason, tx.work.read_state(item.work_id) or "stale"
        mutation = tx.candidates.obsolete(evaluation_id, item, now=now, reason=reason)
        return _work_result(mutation, error_code=reason)


def _process_candidate_evaluation(
    storage,
    clock,
    context,
    item,
    *,
    evaluator: CandidateEvaluator,
    started: float,
    budget: float,
) -> tuple[str, str | None, str]:
    """Evaluate one snapshot; only explicit provider rejection can retry."""
    invalid_reason = None
    interrupted = False
    evaluation = None
    evidence_sources = ()
    memory_epoch = None
    began = False
    # Bind the attempt to the current, revalidated snapshot, not the epoch at
    # enqueue time. Unrelated writes while queued must not waste a model call.
    # Validation and the at-most-once fence share one transaction.
    with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
        if not tx.work._verify_lease(item.work_id, item.lease_token, item.lease_owner, now=clock.utc_now()):
            return "stale", None, tx.work.read_state(item.work_id) or "stale"
        evaluation = tx.candidates.evaluation(item.subject_revision)
        if evaluation is None:
            invalid_reason = "authority_revoked"
        else:
            interrupted = evaluation.model_attempted_at is not None
            sources = tuple(tx.source(ref, revision) for ref, revision in evaluation.evidence_refs)
            if any(source is None or source.suppressed for source in sources):
                invalid_reason = "authority_revoked"
            else:
                evidence_sources = tuple(source for source in sources if source is not None)
                memory_epoch = tx.status().memory_epoch
                for source in evidence_sources:
                    try:
                        tx.claims.require_live_source(source.ref, source.revision)
                    except ContractError:
                        invalid_reason = "authority_revoked"
                        break
                if invalid_reason is None and not interrupted:
                    began = tx.candidates.begin_model_attempt(
                        evaluation.evaluation_id, item.work_id, item.lease_token, item.lease_owner,
                        now=clock.utc_now(),
                    )
    if invalid_reason is not None:
        return _candidate_obsolete(
            storage, clock, context, item, evaluation_id=item.subject_revision,
            reason=invalid_reason, started=started, budget=budget,
        )
    if evaluation is None:
        raise AssertionError("candidate evaluation vanished")
    if interrupted:
        return _candidate_failure(
            storage, clock, context, item, evaluation_id=evaluation.evaluation_id,
            code="candidate_attempt_interrupted", started=started, budget=budget,
        )
    # Commit the at-most-once fence before the optional model call. A crash
    # after this point fails closed and waits for genuinely new evidence.
    if not began:
        return _candidate_failure(
            storage, clock, context, item, evaluation_id=evaluation.evaluation_id,
            code="candidate_attempt_interrupted", started=started, budget=budget,
        )
    try:
        raw = evaluator.evaluate_candidate(
            evaluation.candidate,
            evidence_sources,
            remaining_seconds=_remaining(started, clock, budget),
        )
    except ContractError as exc:
        code = (exc.code or "candidate_evaluation_failed").lower()
        pause = code in {"budget_exhausted", "budget_unavailable", "credential_missing", "credential_shape_invalid"}
        return _candidate_failure(
            storage, clock, context, item, evaluation_id=evaluation.evaluation_id,
            code=code, field=exc.field, budget_pause=pause, started=started, budget=budget,
        )
    except Exception as exc:
        outcome = _model_exception_outcome(exc)
        code = outcome[1] if outcome is not None else "model_unavailable"
        pause = code in {"budget_exhausted", "budget_unavailable", "credential_missing", "credential_shape_invalid"}
        return _candidate_failure(
            storage, clock, context, item, evaluation_id=evaluation.evaluation_id,
            code=code, budget_pause=pause, started=started, budget=budget,
        )
    try:
        value = _decode_consolidation_result(raw, evidence_sources)
    except (ContractError, ValueError, TypeError, json.JSONDecodeError) as exc:
        field = exc.field if isinstance(exc, ContractError) else "json_envelope"
        return _candidate_failure(
            storage, clock, context, item, evaluation_id=evaluation.evaluation_id,
            code="derivation_invalid", field=field, started=started, budget=budget,
        )
    result_digest = hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    try:
        with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
            now = clock.utc_now()
            if not tx.work._verify_lease(item.work_id, item.lease_token, item.lease_owner, now=now):
                return "stale", None, tx.work.read_state(item.work_id) or "stale"
            current = tx.candidates.evaluation(evaluation.evaluation_id)
            if current is None:
                mutation = tx.candidates.obsolete(
                    evaluation.evaluation_id, item, now=now, reason="authority_revoked",
                )
                return _work_result(mutation, error_code="authority_revoked")
            if current.memory_epoch != memory_epoch or tx.status().memory_epoch != memory_epoch:
                mutation = tx.candidates.obsolete(
                    evaluation.evaluation_id, item, now=now, reason="memory_epoch_changed",
                )
                return _work_result(mutation, error_code="memory_epoch_changed")
            live_sources = tuple(tx.source(ref, revision) for ref, revision in current.evidence_refs)
            if any(source is None or source.suppressed for source in live_sources):
                mutation = tx.candidates.obsolete(
                    evaluation.evaluation_id, item, now=now, reason="authority_revoked",
                )
                return _work_result(mutation, error_code="authority_revoked")
            expected_refs = tuple(f"{ref}@{revision}" for ref, revision in current.evidence_refs)
            if tuple(sorted(value["source_refs"])) != tuple(sorted(expected_refs)):
                raise ContractError("DERIVATION_INVALID", "candidate_source_refs")
            if len(value["claim_proposals"]) > 1:
                raise ContractError("DERIVATION_INVALID", "candidate_proposal_count")
            from .mutate import apply_claim, validate_claims
            validated = validate_claims(tx, value, item.scope_id)
            if not validated["claim_proposals"]:
                mutation = tx.candidates.complete(
                    evaluation.evaluation_id, item, now=now, state="waiting_evidence",
                    reason="insufficient_evidence", result_digest=result_digest,
                )
                return _work_result(mutation)
            proposal = validated["claim_proposals"][0]
            from .claim_normalization import normalize_frame
            from .mutate import evidence_refs
            proposal = normalize_frame(proposal, tx.claims.roots(evidence_refs(proposal)))
            expected_candidate = replace(current.candidate, payload=normalize_frame(
                current.candidate.payload, tx.claims.roots(evidence_refs(current.candidate.payload))))
            for field in ("kind", "predicate"):
                if proposal.get(field) != expected_candidate.payload.get(field):
                    raise ContractError("DERIVATION_INVALID", f"candidate_{field}")
            authorized_sources = tuple(source for source in live_sources if source is not None)
            if not candidate_subject_matches(
                expected_candidate, authorized_sources, proposal.get("subject"),
            ):
                raise ContractError("DERIVATION_INVALID", "candidate_subject")
            applied = apply_claim(tx, proposal, item.scope_id, now)
            applied_version = next(
                (version for version in tx.claims.versions(applied.ref)
                 if version.revision == applied.revision),
                None,
            )
            if (
                applied_version is None
                or applied_version.payload.get("subject")
                != expected_candidate.payload.get("subject")
            ):
                raise ContractError("DERIVATION_INVALID", "candidate_subject_binding")
            lifecycle_state = "resolved" if applied.state == "active" else "waiting_evidence"
            reason = "fact_active" if lifecycle_state == "resolved" else "evaluated_waiting_evidence"
            mutation = tx.candidates.complete(
                evaluation.evaluation_id, item, now=now, state=lifecycle_state,
                reason=reason, result_digest=result_digest,
            )
            if (applied.ref, applied.revision) != (
                current.candidate.ref, current.candidate.revision,
            ):
                tx.candidates.register(
                    applied.ref, applied.revision, observed_at=now,
                    rule_version=current.candidate.rule_version, schedule_initial=False,
                )
            return _work_result(mutation)
    except ContractError as exc:
        code = exc.code or "DERIVATION_INVALID"
        if code in {"SOURCE_MISSING", "VERSION_CONFLICT", "ACCESS_DENIED"}:
            reason = "memory_epoch_changed" if exc.field == "memory_epoch" else "authority_revoked"
            return _candidate_obsolete(
                storage, clock, context, item, evaluation_id=evaluation.evaluation_id,
                reason=reason, started=started, budget=budget,
            )
        return _candidate_failure(
            storage, clock, context, item, evaluation_id=evaluation.evaluation_id,
            code=code.lower(), field=exc.field, started=started, budget=budget,
        )


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
        recovered = 0
        if "consolidate" in allowed:
            recovered = tx.work.recover_oversized_consolidations(
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


def build_consolidation_model(port) -> ConsolidationModel | None:
    if port is None:
        return None

    class Adapter:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
            messages = consolidation_messages(sources, episode_ref=episode_ref)
            return port.propose(messages, remaining_seconds=remaining_seconds)

        def evaluate_candidate(self, candidate, sources, *, remaining_seconds=1.0):
            messages = candidate_evaluation_messages(candidate, sources)
            return port.propose(messages, remaining_seconds=remaining_seconds)

    return Adapter()
