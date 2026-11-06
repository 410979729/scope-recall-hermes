"""The worker's bookkeeping around one evaluation attempt, and fencing on deletion.

An evaluation is a queued question with a work item behind it.  The worker
reads it, marks the single model attempt, and then reports one of four
outcomes; each moves the evaluation, its lifecycle row and its work item
together, because ``begin_model_attempt`` will only accept the three in step.
"""

from __future__ import annotations

from ..contracts import ContractError
from .candidate_lifecycle import CandidateEvaluationSnapshot
from .candidate_tables import (
    HEAD_COLUMNS,
    HEAD_JOINS,
    UNRESOLVED_SUBJECT_REASON,
    CandidateTables,
    is_live_head,
    parse_refs,
    parse_time,
    snapshot,
)
from .evidence_question import (
    AUTOMATIC_VERDICTS,
    PERSON_ABSENT_REASON,
    REPEAT_WITHOUT_RESTATEMENT_REASON,
    evidence_text,
    needs_absent_person,
    rootless,
    unanswerable_reason,
)
from .failure_policy import PROVIDER_RECHECK, retryable
from .worker_outcomes import record_error_detail

#: Where deleting or suppressing an object reaches candidate work.
_FENCED_BY = {
    "claim": "SELECT candidate_ref,candidate_revision FROM candidate_lifecycle WHERE candidate_ref=?",
    "event": "SELECT candidate_ref,candidate_revision FROM candidate_evidence WHERE source_ref=?",
}


class CandidateEvaluations(CandidateTables):
    """Read, claim and settle one candidate evaluation at a time."""

    def retire_unresolved_evaluation(self, evaluation_id: int, *, now: str) -> bool:
        """Apply the stored-subject fence even when this evaluation has no judgeable snapshot."""
        key = self._candidate_of(evaluation_id)
        return key is not None and self.retire_unresolved_subjects(now=now, refs=(key,)) > 0

    def evaluation(self, evaluation_id: int) -> CandidateEvaluationSnapshot | None:
        """The queued evaluation as the worker may act on it, or ``None`` if it may not."""
        if type(evaluation_id) is not int or evaluation_id < 1:
            raise ContractError("INPUT_INVALID", "candidate_evaluation")
        context, params = self._context("l.")
        row = (
            self._read()
            .execute(
                f"""SELECT e.evaluation_id,e.state,e.evidence_refs_json,e.evidence_fingerprint,e.memory_epoch,
                       e.model_attempted_at,{HEAD_COLUMNS}
                FROM candidate_evaluations e
                JOIN candidate_lifecycle l ON l.candidate_ref=e.candidate_ref
                  AND l.candidate_revision=e.candidate_revision
                {HEAD_JOINS}
                WHERE e.evaluation_id=? AND {context}""",
                (evaluation_id, *params),
            )
            .fetchone()
        )
        if row is None or row["state"] != "queued" or row["processing_state"] != "pending_evaluation":
            return None
        if not is_live_head(row):
            return None
        try:
            refs = parse_refs(row["evidence_refs_json"])
        except (ValueError, TypeError, AttributeError) as exc:
            raise ContractError("STORAGE_UNAVAILABLE", "candidate_evidence_shape") from exc
        return CandidateEvaluationSnapshot(
            row["evaluation_id"],
            snapshot(row),
            refs,
            row["evidence_fingerprint"],
            row["memory_epoch"],
            row["state"],
            row["model_attempted_at"],
        )

    def settle_without_model(self, evaluation: CandidateEvaluationSnapshot, work, sources, *, now: str):
        """Answer a rule-decided question or defer a queued question still inside its daily interval.

        Returns the work mutation, or ``None`` when the model has to be asked.
        Nothing here begins a model attempt, so the at-most-once fence is
        untouched and a candidate whose question later changes is asked anew.
        """
        candidate = evaluation.candidate
        answer = None
        cited = self._cited_origins(candidate.ref, candidate.revision)
        if needs_absent_person(candidate.payload, cited):
            answer = ("archived", PERSON_ABSENT_REASON)
        else:
            evidence = tuple(evidence_text(source) for source in sources)
            reason = unanswerable_reason(candidate.payload, evidence) or rootless(cited, evidence)
            if reason is not None:
                answer = ("waiting_evidence", reason)
            else:
                verdicts, judged = self._model_verdicts(
                    candidate.ref, candidate.revision, excluding=evaluation.evaluation_id
                )
                if verdicts >= AUTOMATIC_VERDICTS and not self._restated_in_unjudged(
                    candidate.payload, evaluation.evidence_refs, judged
                ):
                    answer = ("waiting_evidence", REPEAT_WITHOUT_RESTATEMENT_REASON)
        if answer is None:
            # Repairs and explicit retries can queue work without the intake/sweep timing gates.
            # Reuse their daily limit here before any model attempt, retaining the queued question.
            until = self._slowed_until(candidate.ref, candidate.revision)
            if evaluation.model_attempted_at is None and until is not None:
                delay = (until - parse_time(now)).total_seconds()
                if delay > 0:
                    return self._tx.work.defer_without_attempt(
                        *work.lease, now=now, error_code="candidate_slowed", seconds=delay
                    )
            return None
        return self.complete(
            evaluation.evaluation_id, work, now=now, state=answer[0], reason=answer[1], result_digest=None
        )

    def begin_model_attempt(self, evaluation_id: int, work_id: int, lease_token: int, owner: str, *, now: str) -> bool:
        """Claim the single model attempt; False when the lease or the evaluation has moved."""
        conn = self._write()
        if not self._tx.work._verify_lease(work_id, lease_token, owner, now=now):
            return False
        row = conn.execute(
            "SELECT state,model_attempted_at,work_id FROM candidate_evaluations WHERE evaluation_id=?",
            (evaluation_id,),
        ).fetchone()
        if (
            row is None
            or row["state"] != "queued"
            or row["work_id"] != work_id
            or row["model_attempted_at"] is not None
        ):
            return False
        return (
            conn.execute(
                """UPDATE candidate_evaluations SET model_attempted_at=?,
               memory_epoch=(SELECT memory_epoch FROM instance_meta WHERE singleton=1)
               WHERE evaluation_id=? AND model_attempted_at IS NULL""",
                (now, evaluation_id),
            ).rowcount
            == 1
        )

    def _record_error(self, work_id: int, lease_token: int, code: str, now: str, field: str | None = None) -> None:
        record_error_detail(
            self._write(), work_id, lease_token, now=now, stage="candidate_evaluation", code=code, field=field
        )

    def defer_entry(self, evaluation_id: int, work, *, now: str):
        """Release an uncalled attempt while one of its source entries is paused."""
        self._release_attempt(evaluation_id, "entry_paused", "entry_paused")
        return self._tx.work.defer_without_attempt(*work.lease, now=now, error_code="entry_paused", seconds=60)

    def defer_budget(self, evaluation_id: int, work, *, now: str, code: str):
        """No verdict was produced: hand the attempt back and park the work for an hour.

        Either no request left the process, or the provider refused the account
        itself (``ACCOUNT_REFUSALS``), which answers before judging anything.
        """
        self._record_error(work.work_id, work.lease_token, code, now)
        self._release_attempt(evaluation_id, "budget_paused", code)
        self._move_for(evaluation_id, "pending_evaluation", "budget_paused")
        return self._tx.work.defer_without_attempt(
            work.work_id,
            work.lease_token,
            work.lease_owner,
            now=now,
            error_code=code,
            seconds=PROVIDER_RECHECK.delay(),
        )

    def fail(
        self,
        evaluation_id: int,
        work,
        *,
        now: str,
        code: str,
        field: str | None = None,
        validation_code: str | None = None,
    ):
        """An explicit rejection has no usable result to replay."""
        self._record_error(work.work_id, work.lease_token, validation_code or code, now, field)
        mutation = self._tx.work.fail(
            work.work_id,
            work.lease_token,
            work.lease_owner,
            error_code=code,
            now=now,
            recoverable=retryable(code, candidate_attempted=True),
        )
        if mutation.disposition == "retry":
            self._release_attempt(evaluation_id, "retry_scheduled", code)
            self._move_for(evaluation_id, "pending_evaluation", "retry_scheduled", now=now)
        else:
            self._close_evaluation(evaluation_id, "failed", "evaluation_failed", now, failure_code=code)
            self._move_for(evaluation_id, "waiting_evidence", "evaluation_failed", now=now, evaluated_at=now)
        return mutation

    def complete(self, evaluation_id: int, work, *, now: str, state: str, reason: str, result_digest: str):
        if state not in {"resolved", "waiting_evidence", "archived"}:
            raise ContractError("INPUT_INVALID", "candidate_completion")
        mutation = self._tx.work.complete(work.work_id, work.lease_token, work.lease_owner, now=now)
        self._close_evaluation(evaluation_id, state, reason, now, result_digest=result_digest)
        self._move_for(
            evaluation_id, state, reason, now=now, evaluated_at=now, dormant_at=now if state == "archived" else None
        )
        return mutation

    def inherit_answer(self, evaluation: CandidateEvaluationSnapshot, revision: int, *, now: str) -> None:
        """Carry an answered question and collected evidence into the version its verdict produced.

        Evidence arriving during the call is retained too. Its different question
        fingerprint can schedule a later evaluation; the answered one cannot.
        """
        ref, old_revision = evaluation.candidate.ref, evaluation.candidate.revision
        candidate = self._tx.claims.version(ref, revision)
        if candidate is None or candidate.current_revision != revision:
            raise ContractError("VERSION_CONFLICT", "candidate_revision")
        self._tx.claims.require_target(candidate)
        conn = self._write()
        conn.execute(
            """INSERT INTO candidate_evidence(candidate_ref,candidate_revision,source_ref,source_revision,observed_at)
            SELECT candidate_ref,?,source_ref,source_revision,observed_at FROM candidate_evidence
            WHERE candidate_ref=? AND candidate_revision=? ON CONFLICT DO NOTHING""",
            (revision, ref, old_revision),
        )
        conn.execute(
            """INSERT INTO candidate_evaluations(candidate_ref,candidate_revision,evidence_fingerprint,
                evidence_refs_json,rule_version,memory_epoch,state,reason,created_at,completed_at,
                model_attempted_at,result_digest)
            SELECT candidate_ref,?,evidence_fingerprint,evidence_refs_json,rule_version,memory_epoch,
                state,'inherited_evaluation',?,completed_at,model_attempted_at,result_digest
            FROM candidate_evaluations WHERE evaluation_id=? AND state IN ('resolved','waiting_evidence','archived')
            ON CONFLICT(candidate_ref,candidate_revision,evidence_fingerprint,rule_version) DO NOTHING""",
            (revision, now, evaluation.evaluation_id),
        )
        conn.execute(
            """UPDATE candidate_lifecycle SET evidence_fingerprint=?,last_evaluated_at=?,
                last_evidence_at=(SELECT max(observed_at) FROM candidate_evidence
                    WHERE candidate_ref=? AND candidate_revision=?)
            WHERE candidate_ref=? AND candidate_revision=?""",
            (evaluation.evidence_fingerprint, now, ref, revision, ref, revision),
        )

    def obsolete(self, evaluation_id: int, work, *, now: str, reason: str):
        mutation = self._tx.work.mark_obsolete(work.work_id, work.lease_token, work.lease_owner, now=now)
        self._close_evaluation(evaluation_id, "obsolete", reason, now, failure_code=reason)
        self._move_for(evaluation_id, "waiting_evidence", reason, now=now)
        return mutation

    def _retry_row(self, work_id: int, *, include_unfinished: bool):
        """Read an explicit grant's unfinished question only when its work already stopped."""
        return (
            self._read()
            .execute(
                "SELECT evaluation_id,candidate_ref,candidate_revision,state, "
                "(SELECT last_error_code FROM work_items w WHERE w.work_id=e.work_id) AS work_error "
                "FROM candidate_evaluations e "
                "WHERE work_id=? AND (state='failed' OR (? AND state='queued' AND EXISTS "
                "(SELECT 1 FROM work_items w WHERE w.work_id=e.work_id AND w.state='failed')))",
                (work_id, include_unfinished),
            )
            .fetchone()
        )

    def evaluation_retry_rejection(self, work_id: int, *, include_unfinished: bool = False) -> str | None:
        """Read a grant's refusal without writes; automatic callers require an already failed evaluation."""
        row = self._retry_row(work_id, include_unfinished=include_unfinished)
        if row is None:
            return "candidate_evaluation_not_failed"
        if self._unresolved_subject_row(row["candidate_ref"], row["candidate_revision"]) is not None:
            return UNRESOLVED_SUBJECT_REASON
        return None

    def reconcile_failed_evaluation(self, work_id: int, *, now: str, dry_run: bool) -> int:
        """Close unfinished bookkeeping for stopped work without clearing its request mark or granting a retry."""
        row = self._retry_row(work_id, include_unfinished=True)
        if row is None or row["state"] != "queued":
            return 0
        if not dry_run:
            self._close_evaluation(row["evaluation_id"], "failed", "work_failed", now, failure_code=row["work_error"])
            self._move(
                row["candidate_ref"], row["candidate_revision"], "waiting_evidence", "evaluation_failed", now=now
            )
        return 1

    def reopen_evaluation(self, work_id: int, *, now: str, reason: str = "operator_retry") -> bool:
        """Reopen a failed question under its caller's bounded grant; automatic callers cannot close queued work."""
        if self.evaluation_retry_rejection(work_id) is not None:
            return False
        row = self._retry_row(work_id, include_unfinished=False)
        if row is None:
            return False
        if self._requeue_failed(row["evaluation_id"], reason) != 1:
            return False
        self._move(row["candidate_ref"], row["candidate_revision"], "pending_evaluation", reason, now=now)
        return True

    def block_objects(self, targets, *, operation_id: str, delete: bool, now: str) -> int:
        """Fence candidate work for deleted or suppressed claims and sources."""
        conn = self._write()
        pairs: set[tuple[str, int]] = set()
        for target in targets:
            lookup = _FENCED_BY.get(target.kind)
            if lookup:
                pairs.update((row[0], row[1]) for row in conn.execute(lookup, (target.ref,)))
        reason = ("deleted" if delete else "suppressed") + ":" + operation_id
        changed = 0
        for ref, revision in sorted(pairs):
            changed += self._move(ref, revision, "blocked", reason, now=now)
            self._retire_evaluations(ref, revision, "authority_revoked", now)
        return changed


__all__ = ["CandidateEvaluations"]
