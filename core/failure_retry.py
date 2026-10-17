"""Give a failed work item one more attempt, once, when a fix has shipped.

A failure that nothing clears is not automatically a failure nobody should look
at again.  In one store's 249 failed work items, for example:

    derivation_invalid            213   model returned an invalid payload
    timeout                        15   transient; the automatic budget ran out
    candidate_attempt_interrupted   8   the at-most-once fence fired after a crash
    budget_checked|input_invalid    7   evidence genuinely too large, already marked
    http_400 (embed)                6   oversized body; there was no input bound

Most of those failed under rules or configuration that have since changed --
the JSON-escape quote ladder, ``response_format`` on the consolidation route,
the embedding input bound -- and 157 of the 213 are of exactly the two kinds
those two fixes addressed.  None of them will ever be retried on their own,
because retrying them automatically would be wrong: the same request would fail
the same way until somebody changed something.

So this is an operator command, and a deliberately blunt one: it re-opens a
bounded page of failures and stamps each row with a marker naming the release
that granted the re-look.  A row already carrying this release's marker is
skipped, so running it twice does nothing and it cannot loop.  The grant is the
release's, not the schema's: most fixes that can cure a failure (a validation,
a route, a bound) ship without a schema change, and keyed to the schema, a row
re-opened once before such a fix could not be re-opened after it.

Two classes, because they answer different questions:

* **terminal** -- ``TERMINAL_FAILURES``.  These are by-design outcomes that
  never clear; the store's status counts the failed rows among them
  (``StoreStatus.terminal_failed_work``) for the worker and the doctor alike.
  Re-running them is a judgement that something upstream changed, so it takes
  an explicit flag rather than happening by default.
* **actionable** -- every other failure, whatever its code.  These are faults.
  They drive "degraded" and clearing them is how an instance gets back to
  healthy.

Only the terminal class is a list.  The worker records whatever code a provider
adapter or a port raises (``worker_outcomes.model_exception_outcome``), so a
list of the failures that may pass cannot be complete, while the status counts
every failed row outside the terminal list as a fault: a fault missing from
such a list would hold an installation at ``degraded`` with nothing anyone
could do.  A by-design outcome missing from the terminal list costs one re-look
per release instead, which its next attempt answers.

Not responsible for: deciding whether the upstream fix actually works -- the
next attempt decides that, and a row that fails again is failed again with its
marker intact.
"""

from __future__ import annotations

import re

from .._version import __version__
from ..contracts import ContractError
from .secret_patterns import contains_secret_like_text

DERIVATION_RETRY_MARKER = "derivation_retry:1"
# Only infrastructure failures may be recovered without a new source revision.
# Invalid derivations and rejected authority remain terminal and inspectable.
# ``http_protocol`` is the transport failing mid-reply (a connection closed or a malformed answer), and is treated as
# ``network_error`` is: it says nothing about the payload.  Missing here, consolidation and embedding work failing
# with it would never be recovered.  ``model_output_interrupted`` is the provider ending an answer for lack of capacity
# or by interrupting it, and ``response_status_failed`` is a Responses route's generation failing at the provider;
# neither says anything about the payload either.  An answer the route cannot use (a vector of another width, say) is
# not here: it comes back until someone changes the model or the configuration, and a recovery gated by time alone
# cannot tell when they have, so ``retry-failures`` re-opens it.
AUTO_RECOVERABLE_ERRORS = frozenset(
    {
        "model_unavailable",
        "model_timeout",
        "model_output_interrupted",
        "response_status_failed",
        "timeout",
        "network_error",
        "http_protocol",
        "http_429",
        "http_500",
        "http_502",
        "http_503",
        "http_504",
        "http_529",
        "rate_limited",
        "storage_unavailable",
        "STORAGE_UNAVAILABLE",
        "DEADLINE_EXCEEDED",
        "memory_epoch_changed",
        "lease_exhausted",
        "embedding_unavailable",
    }
)

#: The provider declining the account rather than this request: payment
#: required, key rejected, access forbidden.  No payload changes that answer, so
#: the worker parks the item without an attempt (``BUDGET_PAUSE_ERRORS``) until
#: someone fixes the account.  A balance that runs out answers 402 to every
#: request: treated as a failure, fifteen minutes of it would fail a hundred
#: candidate evaluations outright.
ACCOUNT_REFUSALS = frozenset({"http_401", "http_402", "http_403"})

#: By-design outcomes, as ``failure_kind`` reads them.  Re-running them asserts
#: that something upstream changed.  ``sensitive_request`` is the request guard
#: refusing to send secret-like text to a model; the payload is what it is, so a
#: retry can only refuse again.  ``model_refused`` is the provider's policy
#: refusing or stopping the answer (a content filter, a refusal): the request
#: is not asked again, and a run re-opens it once the model, the route or the
#: provider's policy has changed.  Every failure not named here is actionable.
TERMINAL_FAILURES = frozenset(
    {
        "derivation_invalid",
        "input_invalid",
        "model_refused",
        "sensitive_request",
    }
)

#: Stamped on every row this grants a re-look, with the release that granted
#: it, so the grant is visible and cannot be repeated within one release.
RETRY_MARKER = "retried"
#: The release this code is, the generation whose re-look ``retry-failures`` grants.
RETRY_GENERATION = __version__
# Per work item, not per release; explicit operator retries remain separate.
NEEDS_REVIEW_COUNT = f"""
    SELECT count(*) FROM work_items WHERE state='failed'
    AND last_error_code LIKE '%{DERIVATION_RETRY_MARKER}|%'
    AND lower(last_error_code) LIKE '%|derivation_invalid'
"""

# A stamp naming a schema generation (``retried:1110|``, as older releases wrote it) names no release, so it never
# blocks one.  An explicit retry of one item keeps the code it found behind ``prior:``, stamp and all.
_MARKER_RE = re.compile(rf"(?:^|\|(?:prior:)?){RETRY_MARKER}:([^|]+)\|")
_STAMP_RE = re.compile(rf"(?:^|(?<=\|)){RETRY_MARKER}:[^|]+\|")


def validation_feedback(code: object, field: object) -> dict[str, str]:
    """Return bounded validation symbols, never exception/output text.

    Legacy rows may lack a field or carry a decorated work code. In that case
    the only honest repair hint is the generic derivation/payload failure.
    """
    safe_code = code.upper() if isinstance(code, str) else ""
    if safe_code not in {"INPUT_INVALID", "DERIVATION_INVALID"}:
        safe_code = "DERIVATION_INVALID"
    safe_field = (
        field
        if (
            isinstance(field, str)
            and re.fullmatch(r"[A-Za-z0-9_./\[\]-]{1,240}", field)
            and not contains_secret_like_text(field)
        )
        else "payload"
    )
    return {"code": safe_code, "field": safe_field}


def failure_kind(error_code: object) -> str:
    """The bare failure kind inside a possibly decorated error code.

    Codes accumulate history (``auto_retry:1|timeout``,
    ``budget_checked:1108|input_invalid``), so the kind is the last segment.
    """
    text = str(error_code or "").strip().lower()
    return text.rsplit("|", 1)[-1] if text else ""


def already_retried(error_code: object, *, generation: str) -> bool:
    """Whether this row already had its re-look in this release."""
    return generation in _MARKER_RE.findall(str(error_code or ""))


def retry_class(error_code: object) -> str:
    """``"terminal"`` for a by-design outcome (``TERMINAL_FAILURES``), and ``"actionable"`` for every other failure,
    an empty code and one no release has written yet included."""
    return "terminal" if failure_kind(error_code) in TERMINAL_FAILURES else "actionable"


def marked(error_code: object, *, generation: str) -> str:
    """The error code to store on a row being re-opened, before its writer bounds it.

    The stamp replaces an earlier release's: only this release's decides (``already_retried``), and a stamp per
    release that re-opened a row would only lengthen the code.
    """
    history = _STAMP_RE.sub("", str(error_code or "").strip())
    return f"{RETRY_MARKER}:{generation}|{history}"


def wanted(error_code: object, *, include_terminal: bool) -> bool:
    """Whether a request with or without ``include_terminal`` covers this failure, whatever its stamp."""
    return include_terminal or retry_class(error_code) == "actionable"


def selects(error_code: object, *, include_terminal: bool, generation: str) -> bool:
    """Whether this failure should be granted a re-look now."""
    return wanted(error_code, include_terminal=include_terminal) and not already_retried(
        error_code, generation=generation
    )


def validate_page(limit: object) -> int:
    if type(limit) is not int or type(limit) is bool or not 1 <= limit <= 256:
        raise ContractError("INPUT_INVALID", "retry_limit")
    return limit


__all__ = [
    "RETRY_GENERATION",
    "RETRY_MARKER",
    "TERMINAL_FAILURES",
    "already_retried",
    "failure_kind",
    "marked",
    "retry_class",
    "selects",
    "validate_page",
    "wanted",
]
