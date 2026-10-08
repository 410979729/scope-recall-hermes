"""Consolidation batching, model envelopes and acceptance fences.

Owned by the worker drain; model calls stay outside SQLite transactions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from functools import partial
from typing import Any, Protocol

from ..contracts import ContractError, decode_payload, utc_instant, validate_payload
from .consolidate import (
    ConsolidationWorkFence,
    accept_consolidation,
    candidate_evaluation_messages,
    consolidation_messages,
)
from .consolidation_chunks import source_chunk
from .consolidation_summary import resume_seed
from .episodes import source_origin
from .evidence_question import DERIVATION_ROOT_ORIGINS
from .source_records import StoredSource
from .worker_outcomes import (
    Outcome,
    budget_left,
    deadline_result,
    derivation_changed,
    epoch_changed,
    finalize_work,
    mark_obsolete,
    model_failure,
    read_derivation_fence,
    work_result,
)


class ConsolidationModel(Protocol):
    def propose(
        self,
        sources: tuple[StoredSource, ...],
        *,
        episode_ref: str | None,
        remaining_seconds: float,
        validation_feedback: dict[str, str] | None = None,
    ) -> str: ...


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
    prior = (
        tx._check()
        .execute(
            """SELECT v.processed_sequence,v.resume_json,ee.sequence FROM episode_versions v
           JOIN episode_events ee ON ee.episode_id=v.episode_id
           WHERE v.episode_id=? AND v.revision=? AND ee.source_ref=? AND ee.source_revision=?""",
            (episode.ref, episode.revision, source.ref, source.revision),
        )
        .fetchone()
    )
    processed_sequence = prior["processed_sequence"] if prior and prior["resume_json"] else 0
    if prior and prior["sequence"] <= processed_sequence:
        return episode.ref, (), ()
    batch_rows = (
        tx._check()
        .execute(
            """SELECT ee.sequence,ee.source_ref,ee.source_revision,w.lease_token,w.work_id
           FROM episode_events ee JOIN work_items w ON w.work_type='consolidate'
             AND w.subject_ref=ee.source_ref AND w.subject_revision=ee.source_revision
           WHERE ee.episode_id=? AND ee.sequence>? AND w.scope_id=?
             AND w.project_id IS ? AND w.branch_id IS ?
             AND (w.work_id=? OR (w.state='pending' AND w.available_at<=? AND w.consolidation_offset=0))
           ORDER BY (w.work_id=?) DESC,ee.sequence LIMIT 32""",
            (
                episode.ref,
                processed_sequence,
                source.scope_id,
                source.project_id,
                source.branch_id,
                item.work_id,
                now,
                item.work_id,
            ),
        )
        .fetchall()
    )
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
        if origin not in DERIVATION_ROOT_ORIGINS:
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


def _requote(claim: dict, span: dict, content: str) -> None:
    """A quote of a serialized JSON value, re-escaped once (with the claim's fields that held it) when exactly that
    escaped span is stored."""
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


def _undecorate_claim(claim: dict, source_text: dict) -> None:
    """One claim's transport decoration taken off, in place: empty kind fields the claim is not of, offset times,
    null locations, and quotes of serialized JSON values."""
    for kind_field in ("procedure", "intention", "alias"):
        if claim.get("kind") != kind_field and claim.get(kind_field) in (None, {}):
            claim.pop(kind_field, None)
    for time_field in ("valid_from", "valid_to"):
        if time_field in claim:
            claim[time_field] = utc_instant(claim[time_field])
    spans = claim.get("evidence_spans")
    if not isinstance(spans, list):
        return
    for span in spans:
        if isinstance(span, dict) and span.get("location") is None and "location" in span:
            del span["location"]
        if not isinstance(span, dict) or not isinstance(span.get("quote"), str):
            continue
        content = source_text.get((span.get("source_ref"), span.get("source_revision")), "")
        _requote(claim, span, content)


def decode_consolidation_result(raw: str, sources=()) -> dict:
    """Decode the model envelope without relaxing the consolidation contract.

    Some OpenAI-compatible gateways/models still wrap a JSON-object response in
    a single ``json`` code fence despite ``response_format=json_object``.  The
    fence is transport decoration, so accepting it is safe only when it is the
    complete outer envelope.  Likewise, ``location`` is an optional evidence
    field; a model's ``null`` for it has exactly the same meaning as omission,
    and a ``valid_from``/``valid_to`` written with a numeric offset names the
    same instant as its UTC form (see ``contracts.utc_instant``).  All other shape,
    source, quote, and authority checks remain in the existing validators below.
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
            _undecorate_claim(claim, source_text)
    return validate_payload("consolidation_result", value)


@dataclass(frozen=True)
class _ConsolidationBatch:
    """What one consolidation attempt works on, read in one snapshot: the work item's source (None when it is gone or
    no longer live), its episode, the batch and the sources left for a later one, the root-only sources the model
    reads, a long source's resume point, and the dependencies the result is fenced on."""

    source: Any = None
    episode_ref: str | None = None
    batch: tuple = ()
    pending_sources: tuple = ()
    roots: tuple = ()
    offset: int = 0
    seed: tuple = ()
    dependencies: Any = None


def _read_batch(tx, item, clock) -> _ConsolidationBatch:
    """The batch a consolidation attempt works on, in the read transaction that verified its lease."""
    source = tx.source(item.subject_ref, item.subject_revision)
    if source is not None:
        try:
            tx.claims.require_live_source(source.ref, source.revision)
        except ContractError:
            source = None
    if source is None:
        return _ConsolidationBatch()
    seed = ()
    offset = tx.work.consolidation_offset(*item.lease, now=clock.utc_now())
    if offset:
        seed = resume_seed(tx, item.work_id)
        episode = tx.episodes.source_episode(source.ref, source.revision)
        episode_ref, batch, pending_sources = episode.ref if episode else None, (source,), ()
    else:
        episode_ref, batch, pending_sources = _episode_batch(tx, source, item, now=clock.utc_now())
    roots = _root_only_sources(tx, batch)
    # The dependencies come from the same snapshot as the batch itself.
    dependencies = read_derivation_fence(tx, scope_id=item.scope_id, sources=(source, *batch), episode_ref=episode_ref)
    return _ConsolidationBatch(source, episode_ref, batch, pending_sources, roots, offset, seed, dependencies)


def _complete_without_model(storage, clock, context, item, read: _ConsolidationBatch, started: float, budget: float):
    """Complete a batch with nothing for the model to read (no root-only source) without a model call, while the
    lease, every source and the dependencies still hold."""
    if budget_left(started, clock, budget) <= 0:
        return deadline_result(storage, context, item)
    with storage.write(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
        now = clock.utc_now()
        if not tx.work._verify_lease(*item.lease, now=now):
            return work_result(tx.work.complete(*item.lease, now=now))
        try:
            tx.claims.require_live_source(item.subject_ref, item.subject_revision)
            for stored in read.batch:
                tx.claims.require_live_source(stored.ref, stored.revision)
        except ContractError:
            return mark_obsolete(tx, item, now)
        if derivation_changed(tx, read.dependencies) is not None:
            return epoch_changed(tx, item, now)
        return work_result(
            tx.work.complete_consolidation(
                *item.lease,
                now=now,
                covered_source_refs=frozenset(f"{s.ref}@{s.revision}" for s in read.batch)
                | {f"{read.source.ref}@{read.source.revision}"},
                pending_sources=read.pending_sources,
            )
        )


def _needs_chunk(roots, episode_ref, feedback, offset) -> bool:
    """Whether the source goes to the model a page at a time: it is already part-way through, or its messages exceed
    the input budget."""
    if offset:
        return True
    try:
        consolidation_messages(roots, episode_ref=episode_ref, validation_feedback=feedback)
    except ContractError as exc:
        if exc.code != "INPUT_INVALID" or exc.field != "consolidation_input_budget":
            raise
        return True
    return False


def _decode_refused(finish, exc: Exception):
    """A model answer that does not decode: a bounded fresh attempt, with the clause or envelope that failed."""
    detail = exc.field if isinstance(exc, ContractError) else "json_envelope"
    return Outcome(
        *finish(
            "retry",
            "derivation_invalid",
            error_detail=detail,
            stage="decode",
            validation_code=exc.code if isinstance(exc, ContractError) else "INPUT_INVALID",
        ),
        detail=detail,
    )


def _accept_refused(finish, exc: ContractError):
    """A decoded result the store refused to accept."""
    code = exc.code or "derivation_invalid"
    # Reject this result, but retain live work for a fresh bounded attempt:
    # something it was derived from changed during the call.
    if code == "VERSION_CONFLICT" and exc.field == "memory_epoch":
        return finish("retry", "memory_epoch_changed")
    if code in {"SOURCE_MISSING", "VERSION_CONFLICT", "ACCESS_DENIED"}:
        return finish("obsolete", "authority_revoked")
    recoverable = code in {"STORAGE_UNAVAILABLE", "DEADLINE_EXCEEDED", "DERIVATION_INVALID"}
    # Keep the clause that rejected the result. Without it a terminal
    # DERIVATION_INVALID cannot be told apart from any other, and diagnosing
    # one costs a full reproduction against live work.
    return Outcome(
        *finish("retry" if recoverable else "failed", code, error_detail=exc.field, stage="accept"),
        detail=exc.field,
    )


def process_consolidate(
    storage,
    clock,
    context,
    item,
    *,
    model: ConsolidationModel | None,
    started: float,
    budget: float,
) -> tuple[str, str | None, str]:
    finish = partial(finalize_work, storage, clock, context, item, started=started, budget=budget)
    with storage.read(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
        feedback = tx.work.derivation_feedback(item.work_id)
        if not tx.work._verify_lease(*item.lease, now=clock.utc_now()):
            state = tx.work.read_state(item.work_id) or "stale"
            return "stale", "authority_revoked" if state == "obsolete" else None, state
        read = _read_batch(tx, item, clock)
    # Do not open a write transaction while the read transaction above is
    # still active.  SQLite's reader lock otherwise turns an obsolete source
    # into a spurious "database is locked" failure.
    source, episode_ref = read.source, read.episode_ref
    if source is None:
        return finish("obsolete", "authority_revoked")
    if not read.roots:
        return _complete_without_model(storage, clock, context, item, read, started, budget)
    if model is None:
        return finish("retry", "model_unavailable")
    roots, batch, pending_sources, chunk = read.roots, read.batch, read.pending_sources, None
    try:
        if _needs_chunk(roots, episode_ref, feedback, read.offset):
            page, chunk = source_chunk(
                source,
                read.offset,
                formatter=partial(consolidation_messages, validation_feedback=feedback),
                episode_ref=episode_ref,
                resume_seed=read.seed,
            )
            roots, batch, pending_sources = (page,), (source,), ()
        allowed_refs = frozenset(f"{stored.ref}@{stored.revision}" for stored in roots)
        repair = {"validation_feedback": feedback} if feedback is not None else {}
        raw = model.propose(
            roots, episode_ref=episode_ref, remaining_seconds=budget_left(started, clock, budget), **repair
        )
    except ContractError as exc:
        # A probabilistic model occasionally emits an invalid derivation; give
        # the work item a bounded fresh attempt instead of killing it on the
        # first bad generation (attempts stay capped by MAX_RECOVERABLE_ATTEMPTS).
        return finish("retry", exc.code or "model_unavailable", error_detail=exc.field, stage="prepare_or_model")
    except Exception as exc:
        return finish(*model_failure(exc))
    try:
        value = decode_consolidation_result(raw, roots)
    except (ContractError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return _decode_refused(finish, exc)
    fence = ConsolidationWorkFence(
        item.work_id,
        item.lease_token,
        item.lease_owner,
        item.subject_ref,
        item.subject_revision,
        read.dependencies.only_sources((source, *batch)),
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
            # latest evidence. Scope, project and the work/dependency fence
            # remain unchanged; stale or unrelated human evidence still fails.
            replace(context, session_id=source.session_id, recent_messages=()),
            value,
            scope_id=item.scope_id,
            remaining_seconds=budget_left(started, clock, budget),
            work_fence=fence,
        )
    except ContractError as exc:
        return _accept_refused(finish, exc)
    if chunk is not None and not chunk.final:
        return "deferred", None, "pending"
    return "completed", None, "done"


def build_consolidation_model(port) -> ConsolidationModel | None:
    if port is None:
        return None

    class Adapter:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0, validation_feedback=None):
            messages = consolidation_messages(sources, episode_ref=episode_ref, validation_feedback=validation_feedback)
            return port.propose(messages, remaining_seconds=remaining_seconds)

        def evaluate_candidate(self, candidate, sources, *, remaining_seconds=1.0, validation_feedback=None):
            messages = candidate_evaluation_messages(candidate, sources, validation_feedback=validation_feedback)
            return port.propose(messages, remaining_seconds=remaining_seconds)

    return Adapter()
