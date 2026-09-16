"""Lease-fenced work outcomes and sanitized failure persistence.

Owned by the worker drain; model calls stay outside SQLite transactions.
Every processor returns ``(disposition, error_code, state)`` and records its
verdict only through the helpers here.
"""
from __future__ import annotations

from ..contracts import ContractError
from .failure_retry import validation_feedback

_NON_RETRYABLE_MODEL_ERRORS = frozenset({
    "budget_exhausted",
    "meter_breach",
    "input_invalid",
    "budget_unavailable",
})

#: Refusals raised before any network attempt.  The item is parked for an hour
#: without spending an attempt, and its work type stands down for the pass.
BUDGET_PAUSE_ERRORS = frozenset({
    "budget_exhausted",
    "budget_unavailable",
    "credential_missing",
    "credential_shape_invalid",
})

#: Port rejections that describe this payload rather than the provider;
#: sending the same input again would fail the same way.
_INVALID_INPUT_CODES = frozenset({"DERIVATION_INVALID", "INPUT_INVALID"})


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


def _stale(tx, item, error_code: str | None = None) -> tuple[str, str | None, str]:
    """The lease is gone; report the row's current state without touching it."""
    return "stale", error_code, tx.work.read_state(item.work_id) or "stale"


def _deadline_result(storage, context, item) -> tuple[str, str, str]:
    with storage.read(context) as tx:
        return "skipped", "DEADLINE_EXCEEDED", tx.work.read_state(item.work_id) or "stale"


def _mark_obsolete(tx, item, now: str) -> tuple[str, str | None, str]:
    return _work_result(tx.work.mark_obsolete(*item.lease, now=now), error_code="authority_revoked")


def _epoch_changed(tx, item, now: str) -> tuple[str, str | None, str]:
    """An unrelated write advanced the instance epoch under this item; retry it."""
    return _work_result(
        tx.work.fail(*item.lease, error_code="memory_epoch_changed", now=now, recoverable=True),
        error_code="memory_epoch_changed",
    )


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


def _model_failure(exc: BaseException) -> tuple[str, str]:
    """Disposition and code for an arbitrary exception out of a model call."""
    return _model_exception_outcome(exc) or ("retry", "model_unavailable")


def _port_failure(exc: BaseException) -> tuple[str, str]:
    """Disposition and code for an exception out of an embedding port."""
    if isinstance(exc, ContractError):
        disposition = "failed" if exc.code in _INVALID_INPUT_CODES else "retry"
        return disposition, exc.code or "model_unavailable"
    return _model_failure(exc)


def _record_error_detail(conn, item, *, now: str, stage: str, code: str, detail: str | None) -> None:
    # Store metadata only, never raw model output or exception bodies.
    field = validation_feedback(code, detail)["field"] if detail else None
    conn.execute(
        "INSERT INTO work_error_details(work_id,lease_token,stage,error_code,error_field,recorded_at) VALUES (?,?,?,?,?,?)",
        (item.work_id, item.lease_token, stage, code, field, now),
    )
    conn.execute(
        "DELETE FROM work_error_details WHERE work_id=? AND detail_id NOT IN "
        "(SELECT detail_id FROM work_error_details WHERE work_id=? ORDER BY detail_id DESC LIMIT 16)",
        (item.work_id, item.work_id),
    )


def _finalize_work(storage, clock, context, item, disposition: str, error_code: str | None, *, started: float, budget: float,
                   error_detail: str | None = None, stage: str = "process",
                   validation_code: str | None = None) -> tuple[str, str | None, str]:
    """Record a retry, failed or obsolete verdict under the lease.

    A spent deadline or a lost lease reports the row instead of mutating it.
    """
    now = clock.utc_now()
    if _remaining(started, clock, budget) <= 0:
        return _deadline_result(storage, context, item)
    with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
        conn = tx._check(write=True)
        if not tx.work._verify_lease(*item.lease, now=now):
            return _stale(tx, item, error_code)
        if error_code:
            _record_error_detail(conn, item, now=now, stage=stage, code=validation_code or error_code, detail=error_detail)
        if error_code in BUDGET_PAUSE_ERRORS:
            return _work_result(
                tx.work.defer_without_attempt(*item.lease, now=now, error_code=error_code, seconds=3600),
                error_code=error_code,
            )
        if disposition == "obsolete":
            return _work_result(tx.work.mark_obsolete(*item.lease, now=now), error_code=error_code)
        recoverable = disposition != "failed"
        return _work_result(
            tx.work.fail(
                *item.lease,
                error_code=error_code or ("model_unavailable" if recoverable else "derivation_invalid"),
                now=now,
                recoverable=recoverable,
            ),
            error_code=error_code,
        )
