"""Candidate evaluation attempts, provider errors and fenced verdicts.

Owned by the worker drain; model calls stay outside SQLite transactions.
"""
from __future__ import annotations

import json
import hashlib
from dataclasses import replace
from functools import partial

from ..contracts import ContractError
from .candidate_lifecycle import CandidateEvaluator, candidate_identity_restored
from .failure_retry import validation_feedback
from .worker_consolidation import _decode_consolidation_result
from .worker_outcomes import (
    BUDGET_PAUSE_ERRORS,
    _deadline_result,
    _model_failure,
    _remaining,
    _stale,
    _work_result,
    claim_versions_mark,
    claims_changed,
    derivation_changed,
    read_derivation_fence,
)


def _candidate_verdict(storage, clock, context, item, *, code: str, started: float, budget: float, mutate):
    """Record one fenced candidate outcome; ``mutate(tx, now)`` picks the transition."""
    if _remaining(started, clock, budget) <= 0:
        return _deadline_result(storage, context, item)
    with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
        now = clock.utc_now()
        if not tx.work._verify_lease(*item.lease, now=now):
            return _stale(tx, item, code)
        return _work_result(mutate(tx, now), error_code=code)


def _candidate_failure(storage, clock, context, item, *, evaluation_id: int,
                       code: str, started: float, budget: float,
                       field: str | None = None, budget_pause: bool = False,
                       validation_code: str | None = None):
    def mutate(tx, now):
        if budget_pause:
            return tx.candidates.defer_budget(evaluation_id, item, now=now, code=code)
        safe_field = validation_feedback(validation_code or code, field)["field"] if field else None
        return tx.candidates.fail(evaluation_id, item, now=now, code=code, field=safe_field,
                                  validation_code=validation_code)

    return _candidate_verdict(storage, clock, context, item, code=code, started=started, budget=budget, mutate=mutate)


def _candidate_obsolete(storage, clock, context, item, *, evaluation_id: int,
                        reason: str, started: float, budget: float):
    return _candidate_verdict(
        storage, clock, context, item, code=reason, started=started, budget=budget,
        mutate=lambda tx, now: tx.candidates.obsolete(evaluation_id, item, now=now, reason=reason),
    )


def _apply_verdict(tx, item, current, value, live_sources, now):
    """Validate one decoded verdict and apply its proposal; None when it has none."""
    expected_refs = tuple(f"{ref}@{revision}" for ref, revision in current.evidence_refs)
    if tuple(sorted(value["source_refs"])) != tuple(sorted(expected_refs)):
        raise ContractError("DERIVATION_INVALID", "candidate_source_refs")
    if len(value["claim_proposals"]) > 1:
        raise ContractError("DERIVATION_INVALID", "candidate_proposal_count")
    from .mutate import apply_claim, evidence_refs, validate_claims
    validated = validate_claims(tx, value, item.scope_id)
    if not validated["claim_proposals"]:
        return None
    proposal = validated["claim_proposals"][0]
    from .claim_normalization import normalize_frame
    proposal = normalize_frame(proposal, tx.claims.roots(evidence_refs(proposal)))
    expected_candidate = replace(current.candidate, payload=normalize_frame(
        current.candidate.payload, tx.claims.roots(evidence_refs(current.candidate.payload))))
    authorized_sources = tuple(source for source in live_sources if source is not None)
    # What the candidate is was recorded before the call; the verdict decides
    # whether the evidence supports it, with what value and on which quote.
    proposal = candidate_identity_restored(expected_candidate, authorized_sources, proposal)
    # A version the evaluator writes rests on a source a claim may be derived from, as a
    # consolidation's does, and says nothing only a tool output states: it quotes a root, and its
    # value from something other than tool output.  A person confirming an agent's proposal is
    # still confirmed; a tool output's value quoted beside any fragment of a person's message was
    # written as that person's report.
    from .evidence_question import DERIVATION_ROOT_ORIGINS, evidence_text, value_beyond_tool_output
    origin = {(source.ref, source.revision): evidence_text(source).origin for source in authorized_sources}
    quoted = [(origin.get((span["source_ref"], span["source_revision"]), "origin_unknown"), span["quote"])
              for span in proposal["evidence_spans"]]
    if (not any(source_origin in DERIVATION_ROOT_ORIGINS for source_origin, _quote in quoted)
            or not value_beyond_tool_output(proposal, quoted)):
        return None
    applied = apply_claim(tx, proposal, item.scope_id, now)
    applied_version = tx.claims.version(applied.ref, applied.revision)
    if (
        applied_version is None
        or applied_version.payload.get("subject") != expected_candidate.payload.get("subject")
    ):
        raise ContractError("DERIVATION_INVALID", "candidate_subject_binding")
    return applied


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
    # The evaluation id is the subject revision of the work item.
    fail = partial(_candidate_failure, storage, clock, context, item,
                   evaluation_id=item.subject_revision, started=started, budget=budget)
    obsolete = partial(_candidate_obsolete, storage, clock, context, item,
                       evaluation_id=item.subject_revision, started=started, budget=budget)
    invalid_reason = None
    evidence_sources = ()
    dependencies = None
    began = False
    # Bind the attempt to the current, revalidated snapshot, not the epoch at
    # enqueue time. Unrelated writes while queued must not waste a model call.
    # Validation, the dependency record and the at-most-once fence share one
    # transaction.
    with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
        if not tx.work._verify_lease(*item.lease, now=clock.utc_now()):
            return _stale(tx, item)
        feedback = tx.work.derivation_feedback(item.work_id)
        evaluation = tx.candidates.evaluation(item.subject_revision)
        if evaluation is None:
            invalid_reason = "authority_revoked"
        else:
            sources = tuple(tx.source(ref, revision) for ref, revision in evaluation.evidence_refs)
            if any(source is None or source.suppressed for source in sources):
                invalid_reason = "authority_revoked"
            else:
                evidence_sources = tuple(source for source in sources if source is not None)
                for source in evidence_sources:
                    try:
                        tx.claims.require_live_source(source.ref, source.revision)
                    except ContractError:
                        invalid_reason = "authority_revoked"
                        break
                if invalid_reason is None:
                    dependencies = read_derivation_fence(tx, scope_id=item.scope_id, sources=evidence_sources)
                    # A question a rule already answers needs no model call; see
                    # core/evidence_question.py.  Recorded like a verdict, with
                    # no attempt spent, so the at-most-once fence is untouched.
                    settled = tx.candidates.settle_without_model(
                        evaluation, item, evidence_sources, now=clock.utc_now(),
                    )
                    if settled is not None:
                        return _work_result(settled)
                if invalid_reason is None and evaluation.model_attempted_at is None:
                    began = tx.candidates.begin_model_attempt(
                        evaluation.evaluation_id, *item.lease, now=clock.utc_now(),
                    )
    if invalid_reason is not None:
        return obsolete(reason=invalid_reason)
    # Commit the at-most-once fence before the optional model call. A crash
    # after this point fails closed and waits for genuinely new evidence; an
    # attempt already begun by an earlier lease is that crash observed.
    if not began:
        return fail(code="candidate_attempt_interrupted")
    try:
        repair = {"validation_feedback": feedback} if feedback is not None else {}
        raw = evaluator.evaluate_candidate(
            evaluation.candidate,
            evidence_sources,
            remaining_seconds=_remaining(started, clock, budget),
            **repair,
        )
    except ContractError as exc:
        code = (exc.code or "candidate_evaluation_failed").lower()
        return fail(code=code, field=exc.field, budget_pause=code in BUDGET_PAUSE_ERRORS)
    except Exception as exc:
        code = _model_failure(exc)[1]
        return fail(code=code, budget_pause=code in BUDGET_PAUSE_ERRORS)
    try:
        value = _decode_consolidation_result(raw, evidence_sources)
    except (ContractError, ValueError, TypeError, json.JSONDecodeError) as exc:
        contract = isinstance(exc, ContractError)
        return fail(code="derivation_invalid", field=exc.field if contract else "json_envelope",
                    validation_code=exc.code if contract else "INPUT_INVALID")
    result_digest = hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    try:
        with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
            now = clock.utc_now()
            if not tx.work._verify_lease(*item.lease, now=now):
                return _stale(tx, item)
            current = tx.candidates.evaluation(evaluation.evaluation_id)
            if current is None:
                mutation = tx.candidates.obsolete(evaluation.evaluation_id, item, now=now, reason="authority_revoked")
                return _work_result(mutation, error_code="authority_revoked")
            # The row's stamp identifies this model attempt; the recorded
            # dependencies say whether anything the verdict was judged on
            # changed.  A capture or a write to another object changes neither.
            if current.memory_epoch != dependencies.memory_epoch or derivation_changed(tx, dependencies) is not None:
                mutation = tx.candidates.obsolete(evaluation.evaluation_id, item, now=now, reason="memory_epoch_changed")
                return _work_result(mutation, error_code="memory_epoch_changed")
            live_sources = tuple(tx.source(ref, revision) for ref, revision in current.evidence_refs)
            if any(source is None or source.suppressed for source in live_sources):
                mutation = tx.candidates.obsolete(evaluation.evaluation_id, item, now=now, reason="authority_revoked")
                return _work_result(mutation, error_code="authority_revoked")
            mark = claim_versions_mark(tx)
            try:
                applied = _apply_verdict(tx, item, current, value, live_sources, now)
            except ContractError as exc:
                # A rejection caused by a claim written during the call stays
                # the conflict the epoch comparison used to report first.
                if claims_changed(tx, dependencies, until=mark):
                    raise ContractError("VERSION_CONFLICT", "memory_epoch") from exc
                raise
            if applied is None:
                mutation = tx.candidates.complete(
                    evaluation.evaluation_id, item, now=now, state="waiting_evidence",
                    reason="insufficient_evidence", result_digest=result_digest,
                )
                return _work_result(mutation)
            # Another writer's version in the slot the verdict just wrote to
            # would have the stale verdict ordered against it; discard instead.
            if claims_changed(tx, dependencies, until=mark, claim_refs={applied.ref, current.candidate.ref}):
                raise ContractError("VERSION_CONFLICT", "memory_epoch")
            lifecycle_state = "resolved" if applied.state == "active" else "waiting_evidence"
            reason = "fact_active" if lifecycle_state == "resolved" else "evaluated_waiting_evidence"
            mutation = tx.candidates.complete(
                evaluation.evaluation_id, item, now=now, state=lifecycle_state,
                reason=reason, result_digest=result_digest,
            )
            if (applied.ref, applied.revision) != (current.candidate.ref, current.candidate.revision):
                tx.candidates.register(
                    applied.ref, applied.revision, observed_at=now,
                    rule_version=current.candidate.rule_version, schedule_initial=False,
                )
            return _work_result(mutation)
    except ContractError as exc:
        code = exc.code or "DERIVATION_INVALID"
        if code in {"SOURCE_MISSING", "VERSION_CONFLICT", "ACCESS_DENIED"}:
            return obsolete(reason="memory_epoch_changed" if exc.field == "memory_epoch" else "authority_revoked")
        return fail(code=code.lower(), field=exc.field)
