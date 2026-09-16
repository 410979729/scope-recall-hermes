"""Lease-fenced work outcomes and sanitized failure persistence.

Owned by the worker drain; model calls stay outside SQLite transactions.
"""
from __future__ import annotations


_NON_RETRYABLE_MODEL_ERRORS = frozenset({
    "budget_exhausted",
    "meter_breach",
    "input_invalid",
    "budget_unavailable",
})




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



def _remaining(started: float, clock, budget: float) -> float:
    return max(0.0, budget - (clock.monotonic() - started))



def _work_result(mutation, *, error_code: str | None = None) -> tuple[str, str | None, str]:
    return mutation.disposition, error_code, mutation.state



def _deadline_result(storage, context, item) -> tuple[str, str, str]:
    with storage.read(context) as tx:
        return "skipped", "DEADLINE_EXCEEDED", tx.work.read_state(item.work_id) or "stale"



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
                   error_detail: str | None = None, stage: str = "process",
                   validation_code: str | None = None) -> tuple[str, str | None, str]:
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
            from .failure_retry import validation_feedback
            field = validation_feedback(validation_code or error_code, error_detail)["field"] if error_detail else None
            conn.execute("INSERT INTO work_error_details(work_id,lease_token,stage,error_code,error_field,recorded_at) VALUES (?,?,?,?,?,?)",
                         (item.work_id, item.lease_token, stage, validation_code or error_code, field, now))
            conn.execute("DELETE FROM work_error_details WHERE work_id=? AND detail_id NOT IN (SELECT detail_id FROM work_error_details WHERE work_id=? ORDER BY detail_id DESC LIMIT 16)", (item.work_id, item.work_id))
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

