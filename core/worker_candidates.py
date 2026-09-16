"""Candidate evaluation attempts, provider errors and fenced verdicts.

Owned by the worker drain; model calls stay outside SQLite transactions.
"""
from __future__ import annotations

import json
import hashlib
from dataclasses import replace
from ..contracts import ContractError
from .candidate_lifecycle import CandidateEvaluator, candidate_subject_matches
from .worker_consolidation import _decode_consolidation_result
from .worker_outcomes import _remaining, _work_result, _deadline_result, _model_exception_outcome



def _candidate_failure(storage, clock, context, item, *, evaluation_id: int,
                       code: str, started: float, budget: float,
                       field: str | None = None, budget_pause: bool = False,
                       validation_code: str | None = None):
    if _remaining(started, clock, budget) <= 0:
        return _deadline_result(storage, context, item)
    with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
        now = clock.utc_now()
        if not tx.work._verify_lease(item.work_id, item.lease_token, item.lease_owner, now=now):
            return "stale", code, tx.work.read_state(item.work_id) or "stale"
        if budget_pause:
            mutation = tx.candidates.defer_budget(evaluation_id, item, now=now, code=code)
        else:
            from .failure_retry import validation_feedback
            safe_field = validation_feedback(validation_code or code, field)["field"] if field else None
            mutation = tx.candidates.fail(evaluation_id, item, now=now, code=code, field=safe_field,
                                          validation_code=validation_code)
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
        feedback = tx.work.derivation_feedback(item.work_id)
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
        repair = {"validation_feedback": feedback} if feedback is not None else {}
        raw = evaluator.evaluate_candidate(
            evaluation.candidate,
            evidence_sources,
            remaining_seconds=_remaining(started, clock, budget),
            **repair,
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
            validation_code=exc.code if isinstance(exc, ContractError) else "INPUT_INVALID",
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

