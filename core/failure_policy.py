"""Failure meaning and retry pacing shared by durable work and host replay.

Classification does not grant authority or clear a model-attempt fence. Unknown codes remain actionable for
operator recovery; an uncertain candidate request is never repeated merely because its failure is unknown.
"""

from __future__ import annotations

from dataclasses import dataclass


def failure_kind(error_code: object) -> str:
    """Read the final, case-insensitive kind behind a stored retry history."""
    return str(error_code or "").strip().lower().rsplit("|", 1)[-1]


#: The provider declining the account rather than this request: payment required, key rejected, access forbidden.
#: No payload changes that answer, so the worker parks the item without an attempt (``BUDGET_PAUSE_ERRORS``) until
#: someone fixes the account.  A balance that runs out answers 402 to every request: treated as a failure, fifteen
#: minutes of it would fail a hundred candidate evaluations outright.
ACCOUNT_REFUSALS = frozenset({"http_401", "http_402", "http_403"})
#: Failures that are never about the work item.  A provider declining to serve anyone says nothing about this
#: payload -- unlike a timeout, which a large item can genuinely cause -- so the lease never got an attempt at all and
#: the attempt is refunded.  Without the refund one four-hour provider outage pushes hundreds of items into ``failed``
#: at ``attempt=3`` apiece, each needing an operator.  529 is a provider saying it is overloaded (MiniMax, Anthropic):
#: without it here one such answer fails a candidate evaluation for good.
CAPACITY_REFUSALS = frozenset({"http_429", "rate_limited", "http_502", "http_503", "http_504", "http_529"})
#: Provider answers that mean "send less traffic".  The recoverable set says whether an item may be retried; this one
#: decides whether the rest of the pass should even ask.  Every capacity refusal is one, taken from
#: ``CAPACITY_REFUSALS`` rather than copied.  So is a generation the provider interrupted, for lack of capacity or at
#: its own end: the next item meets it too.  That one was a billed answer, so unlike a refusal it is not refunded; it
#: costs its item an attempt. An empty upstream envelope also ends the pass: it gives no usable answer, but proves
#: neither a capacity refusal nor that the call was unbilled. Its attempts and candidate fence stay intact.
RATE_LIMITED_ERRORS = CAPACITY_REFUSALS | {"model_output_interrupted", "upstream_empty_response"}
#: By-design outcomes, as ``failure_kind`` reads them.  Re-running them asserts that something upstream changed.
#: ``sensitive_request`` is the request guard refusing to send secret-like text to a model; the payload is what it is,
#: so a retry can only refuse again.  ``model_refused`` is the provider's policy refusing or stopping the answer (a
#: content filter, a refusal): the request is not asked again, and a run re-opens it once the model, the route or the
#: provider's policy has changed.  Every failure not named here is actionable.
TERMINAL_FAILURES = frozenset({"derivation_invalid", "input_invalid", "model_refused", "sensitive_request"})
BUDGET_PAUSE_ERRORS = ACCOUNT_REFUSALS | {
    "budget_exhausted",
    "budget_unavailable",
    "credential_missing",
    "credential_shape_invalid",
    "provider_hold",
    "unsupported_model",
}
CONNECTION_FAILURES = frozenset(
    {"transport_unavailable", "transport_worker", "transport_worker_protocol", "network_error"}
)
_CAPABILITY_FAILURES = BUDGET_PAUSE_ERRORS | {"model_unavailable", "embedding_unavailable"}
_CONFIGURATION_FAILURES = frozenset({"meter_breach", "request_too_large", "input_invalid", "derivation_invalid"})
_REFUSALS = frozenset({"model_refused", "sensitive_request", "access_denied", "authority_revoked"})
# Only infrastructure failures may be recovered without a new source revision; invalid derivations and rejected
# authority remain terminal and inspectable.  ``http_protocol`` is the transport failing mid-reply (a connection closed
# or a malformed answer) and is treated as ``network_error`` is: it says nothing about the payload.  Missing here,
# consolidation and embedding work failing with it would never be recovered.  ``model_output_interrupted`` is the
# provider ending an answer for lack of capacity or by interrupting it, and ``response_status_failed`` is a Responses
# route's generation failing at the provider; neither says anything about the payload either.  An answer the route
# cannot use (a vector of another width, say) is not here: it comes back until someone changes the model or the
# configuration, and a recovery gated by time alone cannot tell when they have, so ``retry-failures`` re-opens it.
# An empty upstream envelope likewise says nothing about the payload. It remains attempt-charged and keeps an
# uncertain candidate's at-most-once fence; only ordinary work gets bounded automatic recovery.
# Recovery queries compare stored codes exactly, independently of case-insensitive history display.
AUTO_RECOVERABLE_ERRORS = CAPACITY_REFUSALS | frozenset(
    {
        "model_unavailable",
        "model_timeout",
        "model_output_interrupted",
        "response_status_failed",
        "upstream_empty_response",
        "timeout",
        "network_error",
        "http_protocol",
        "http_500",
        "storage_unavailable",
        "STORAGE_UNAVAILABLE",
        "DEADLINE_EXCEEDED",
        "memory_epoch_changed",
        "lease_exhausted",
        "embedding_unavailable",
    }
)
_MODEL_REJECTIONS = frozenset(
    {
        "budget_exhausted",
        "meter_breach",
        "input_invalid",
        "request_too_large",
        "budget_unavailable",
        "sensitive_request",
        "model_refused",
    }
)
_PORT_REJECTIONS = frozenset({"DERIVATION_INVALID", "INPUT_INVALID"})
#: Provider failures that keep the ordinary retry limit for a candidate evaluation already asked.  Invalid output and
#: an uncertain timeout or crash do not: the at-most-once fence must not be cleared by a failure whose effect is
#: unknown.
_CANDIDATE_REJECTIONS = CAPACITY_REFUSALS | {"http_500"}
#: Inbox rows a replay still stores, besides those never tried: a passing failure (``INGRESS_PASSING_ERRORS``), a row
#: from before the missing source was named, or a key another message already took (``resolve_conflicted_ingress``
#: stores it under a new key).  Any other code is terminal; the row stays for inspection only.
INGRESS_REPLAY_ERRORS = frozenset({"STORAGE_UNAVAILABLE", "DEADLINE_EXCEEDED", "SOURCE_MISSING", "VERSION_CONFLICT"})
#: A capture commit's failures that leave its inbox row for the next pass.
INGRESS_PASSING_ERRORS = frozenset({"STORAGE_UNAVAILABLE", "DEADLINE_EXCEEDED"})
_CAPTURE_REFUSALS = frozenset({"ACCESS_DENIED", "INPUT_INVALID", "VERSION_CONFLICT", "ENTRY_STATE"})


def failure_category(error_code: object) -> str:
    """Return transient, capability, configuration or refused without retaining provider text."""
    kind = failure_kind(error_code)
    if kind in _CAPABILITY_FAILURES:
        return "capability"
    if kind in _REFUSALS:
        return "refused"
    if kind in _CONFIGURATION_FAILURES:
        return "configuration"
    if kind.startswith("http_") and len(kind) == 8 and kind[5:].isdigit():
        return "transient" if kind == "http_429" or 500 <= int(kind[5:]) <= 599 else "configuration"
    return "transient"


def retryable(error_code: object, *, candidate_attempted: bool = False) -> bool:
    """Match a live model rejection exactly; only explicit provider rejections clear a candidate request fence."""
    if candidate_attempted:
        return error_code in _CANDIDATE_REJECTIONS
    return error_code not in _MODEL_REJECTIONS


def port_retryable(error_code: str) -> bool:
    """Whether a live contract rejection gets a bounded retry; contract codes are case-sensitive."""
    return error_code not in _PORT_REJECTIONS


def http_retryable(status: int) -> bool:
    """Whether an explicit HTTP response rejects a request for temporary provider reasons."""
    return status == 429 or 500 <= status <= 599


def capture_retryable(error_code: object) -> bool:
    """Whether a failed capture may be replayed under its original, revalidated authority."""
    return error_code not in _CAPTURE_REFUSALS


@dataclass(frozen=True)
class RetrySchedule:
    """A bounded delay per consecutive failure; attempts=None means delay without abandonment."""

    first: float
    ceiling: float
    multiplier: float = 1.0
    attempts: int | None = None

    def delay(self, attempt: int = 1) -> float:
        """Return a finite wait, saturating before a corrupt counter can overflow."""
        count = attempt if type(attempt) is int else 1
        return min(self.ceiling, self.first * self.multiplier ** min(32, max(0, count - 1)))


WORK_RETRY = RetrySchedule(2, 60, 2, 3)
#: The first wait after a provider refuses for capacity, doubling with repeated refusals up to the ceiling.  There is
#: no retry-count limit: an item is delayed rather than abandoned.
CAPACITY_RETRY = RetrySchedule(1800, 14400, 2)
PROVIDER_RECHECK = RetrySchedule(3600, 3600)
PROVIDER_HOLD = RetrySchedule(60, 1800, 2)
INGRESS_RETRY = RetrySchedule(60, 3600, 2, 24)
HERMES_REPLAY = RetrySchedule(30, 30)
HERMES_DISK_RETRY = RetrySchedule(3600, 3600)
HERMES_MEMORY_SECONDS = 1800.0
BACKUP_RETRY = RetrySchedule(900, 900)
MIRROR_RETRY = RetrySchedule(300, 300)
SUPERVISOR_RETRY = RetrySchedule(30, 30, attempts=3)
SETTLE_RETRY = RetrySchedule(900, 900)
CAPABILITY_RECHECK = RetrySchedule(300, 300)


def capacity_backoff_seconds(refusals: int) -> float:
    """Delay before asking a provider that refused for capacity again."""
    return CAPACITY_RETRY.delay(refusals)
