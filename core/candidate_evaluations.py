"""The worker's bookkeeping around one evaluation attempt, and fencing on deletion.

An evaluation is a queued question with a work item behind it.  The worker
reads it, marks the single model attempt, and then reports one of four
outcomes; each moves the evaluation, its lifecycle row and its work item
together, because ``begin_model_attempt`` will only accept the three in step.
"""
from __future__ import annotations

from ..contracts import ContractError
from .candidate_lifecycle import CandidateEvaluationSnapshot
from .candidate_tables import HEAD_COLUMNS, HEAD_JOINS, CandidateTables, is_live_head, parse_refs, snapshot

#: Provider failures that keep the ordinary retry limit.  Invalid output and an
#: uncertain timeout or crash do not: the at-most-once fence must not be
#: cleared by a failure whose effect is unknown.
RETRYABLE_CODES = frozenset({"http_429", "rate_limited", "http_500", "http_502", "http_503", "http_504"})

#: Where deleting or suppressing an object reaches candidate work.
_FENCED_BY = {
    "claim": "SELECT candidate_ref,candidate_revision FROM candidate_lifecycle WHERE candidate_ref=?",
    "event": "SELECT candidate_ref,candidate_revision FROM candidate_evidence WHERE source_ref=?",
}


class CandidateEvaluations(CandidateTables):
    """Read, claim and settle one candidate evaluation at a time."""

    def evaluation(self, evaluation_id: int) -> CandidateEvaluationSnapshot | None:
        """The queued evaluation as the worker may act on it, or ``None`` if it may not."""
        if type(evaluation_id) is not int or evaluation_id < 1:
            raise ContractError("INPUT_INVALID", "candidate_evaluation")
        context, params = self._context("l.")
        row = self._read().execute(
            f"""SELECT e.evaluation_id,e.state,e.evidence_refs_json,e.evidence_fingerprint,e.memory_epoch,
                       e.model_attempted_at,{HEAD_COLUMNS}
                FROM candidate_evaluations e
                JOIN candidate_lifecycle l ON l.candidate_ref=e.candidate_ref AND l.candidate_revision=e.candidate_revision
                {HEAD_JOINS}
                WHERE e.evaluation_id=? AND {context}""",
            (evaluation_id, *params),
        ).fetchone()
        if row is None or row["state"] != "queued" or row["processing_state"] != "pending_evaluation":
            return None
        if not is_live_head(row):
            return None
        try:
            refs = parse_refs(row["evidence_refs_json"])
        except (ValueError, TypeError, AttributeError) as exc:
            raise ContractError("STORAGE_UNAVAILABLE", "candidate_evidence_shape") from exc
        return CandidateEvaluationSnapshot(
            row["evaluation_id"], snapshot(row), refs, row["evidence_fingerprint"], row["memory_epoch"],
            row["state"], row["model_attempted_at"],
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
        if row is None or row["state"] != "queued" or row["work_id"] != work_id or row["model_attempted_at"] is not None:
            return False
        return conn.execute(
            """UPDATE candidate_evaluations SET model_attempted_at=?,
               memory_epoch=(SELECT memory_epoch FROM instance_meta WHERE singleton=1)
               WHERE evaluation_id=? AND model_attempted_at IS NULL""",
            (now, evaluation_id),
        ).rowcount == 1

    def _record_error(self, work_id: int, lease_token: int, code: str, now: str, field: str | None = None) -> None:
        self._write().execute(
            """INSERT INTO work_error_details(work_id,lease_token,stage,error_code,error_field,recorded_at)
               VALUES (?,?,'candidate_evaluation',?,?,?)""",
            (work_id, lease_token, code, field, now),
        )

    def defer_budget(self, evaluation_id: int, work, *, now: str, code: str):
        """No verdict was produced: hand the attempt back and park the work for an hour.

        Either no request left the process, or the provider refused the account
        itself (``ACCOUNT_REFUSALS``), which answers before judging anything.
        """
        self._record_error(work.work_id, work.lease_token, code, now)
        self._release_attempt(evaluation_id, "budget_paused", code)
        self._move_for(evaluation_id, "pending_evaluation", "budget_paused")
        return self._tx.work.defer_without_attempt(
            work.work_id, work.lease_token, work.lease_owner, now=now, error_code=code, seconds=3600,
        )

    def fail(self, evaluation_id: int, work, *, now: str, code: str, field: str | None = None,
             validation_code: str | None = None):
        """An explicit rejection has no usable result to replay."""
        self._record_error(work.work_id, work.lease_token, validation_code or code, now, field)
        mutation = self._tx.work.fail(
            work.work_id, work.lease_token, work.lease_owner, error_code=code, now=now,
            recoverable=code in RETRYABLE_CODES,
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
        self._move_for(evaluation_id, state, reason, now=now, evaluated_at=now,
                       dormant_at=now if state == "archived" else None)
        return mutation

    def obsolete(self, evaluation_id: int, work, *, now: str, reason: str):
        mutation = self._tx.work.mark_obsolete(work.work_id, work.lease_token, work.lease_owner, now=now)
        self._close_evaluation(evaluation_id, "obsolete", reason, now, failure_code=reason)
        self._move_for(evaluation_id, "waiting_evidence", reason, now=now)
        return mutation

    def reopen_evaluation(self, work_id: int, *, now: str, reason: str = "operator_retry") -> bool:
        """Put a failed evaluation and its lifecycle back where a retry can run.

        Returns False when there is nothing to move, so the caller leaves the
        work item alone rather than creating the mismatch it is avoiding.
        """
        row = self._read().execute(
            "SELECT evaluation_id,candidate_ref,candidate_revision FROM candidate_evaluations WHERE work_id=? AND state='failed'",
            (work_id,),
        ).fetchone()
        if row is None or self._requeue_failed(row["evaluation_id"], reason) != 1:
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


__all__ = ["RETRYABLE_CODES", "CandidateEvaluations"]
