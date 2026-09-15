"""One more attempt at the query embedding when the connection, not the request, failed.

The query embedding is a single network call with no second chance, and losing
it costs the entire semantic channel for that recall: ``_vector_candidates``
catches the failure, records ``vector_unavailable`` and ``vector_error:<type>``,
and returns nothing, so the answer is assembled from the lexical and recent
channels alone.  That degradation is honest -- it is reported, never silent --
but it is avoidable when the cause was a connection that failed in
milliseconds.  A live instance recorded exactly that twice in one day.

Only *connection* failures are retried.  A request the provider rejected
(``input_invalid``, ``endpoint_invalid``, ``credential_missing``) or a vector it
returned malformed will fail the same way a second time, and retrying it would
spend the recall's deadline to arrive at the same answer.  ``timeout`` is
deliberately excluded for the same reason: a call that used its whole budget
has none left to use again.

The retry is also refused unless a real share of the embedding budget survives,
because a second attempt that cannot finish is worse than the first failure --
it turns a degraded packet into a late one, and the deadline belongs to the
caller.

Not responsible for: retrying anything the worker does.  Paid consolidation
calls already have their own durable, bounded recovery in
``AUTO_RECOVERABLE_ERRORS``; this is only the read path's one free retry.
"""
from __future__ import annotations

from typing import Any, Callable, Sequence

#: ``AuxiliaryModelError.error_type`` values that mean "the call never reached
#: a provider that could answer it". Everything else is the provider's answer.
TRANSIENT_EMBEDDING_ERRORS = frozenset({
    "transport_unavailable",
    "transport_worker",
    "transport_worker_protocol",
    "network_error",
})

#: A retry must be able to use at least this share of the original budget.
MINIMUM_RETRY_FRACTION = 0.5


def transient(error: BaseException) -> bool:
    """Whether this failure was the connection rather than the request."""
    return getattr(error, "error_type", None) in TRANSIENT_EMBEDDING_ERRORS


def retry_budget(budget_seconds: float, remaining_seconds: float) -> float:
    """Seconds a second attempt may use, or ``0.0`` when it must not run."""
    if type(budget_seconds) not in (int, float) or type(remaining_seconds) not in (int, float):
        # Exact types, as everywhere else a bound is read: a string that happens
        # to parse is a caller bug, and silently coercing it hides one.
        return 0.0
    budget, remaining = float(budget_seconds), float(remaining_seconds)
    if not budget > 0 or not remaining > 0 or budget != budget or remaining != remaining:
        return 0.0
    if remaining < budget * MINIMUM_RETRY_FRACTION:
        return 0.0
    return min(budget, remaining)


def embed_with_one_retry(embed: Callable[[float], Sequence[float]], *, budget_seconds: float,
                         remaining: Callable[[], float]) -> Any:
    """Run ``embed(seconds)``, retrying once if the connection was what failed.

    ``remaining`` is read again after the failure rather than passed in, so the
    second attempt is budgeted from the time the first one actually left.
    """
    try:
        return embed(budget_seconds)
    except Exception as error:
        if not transient(error):
            raise
        seconds = retry_budget(budget_seconds, remaining())
        if seconds <= 0:
            raise
        return embed(seconds)


__all__ = [
    "MINIMUM_RETRY_FRACTION",
    "TRANSIENT_EMBEDDING_ERRORS",
    "embed_with_one_retry",
    "retry_budget",
    "transient",
]
