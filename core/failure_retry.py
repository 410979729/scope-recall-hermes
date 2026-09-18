"""Give a failed work item one more attempt, once, when a fix has shipped.

A failure that nothing clears is not automatically a failure nobody should look
at again.  Measured on one instance, the 249 failed work items were:

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
bounded page of failures and stamps each row with a marker naming the schema
generation that granted the re-look.  A row already carrying this generation's
marker is skipped, so running it twice does nothing and it cannot loop.

Two classes, because they answer different questions:

* **actionable** -- every code the worker already treats as transient
  (``AUTO_RECOVERABLE_ERRORS``), plus two that are not auto-recoverable but are
  still an operator's to clear.  These are faults.  They drive "degraded" and
  clearing them is how an instance gets back to healthy.
* **terminal** -- ``derivation_invalid`` and ``budget_checked``.  These are
  by-design outcomes that never clear (see ``doctor.TERMINAL_FAILURE_COUNT``);
  re-running them is a judgement that something upstream changed, so it takes
  an explicit flag rather than happening by default.

The actionable set is *derived* from the worker's own transient set rather than
listed again here.  Keeping two hand-written lists of "failures that may pass"
is how they drift: they did.  ``model_unavailable`` is auto-recoverable, so the
worker retried it until its automatic budget ran out and then failed the row --
but it was missing from this list, so no operator could clear it either, and
four rows left over from one outage pinned a live instance at ``degraded``
with nothing anyone could do about them.

Not responsible for: deciding whether the upstream fix actually works -- the
next attempt decides that, and a row that fails again is failed again with its
marker intact.
"""
from __future__ import annotations

import re

from ..contracts import ContractError
from .secret_patterns import contains_secret_like_text
from .work_storage import ACCOUNT_REFUSALS, AUTO_RECOVERABLE_ERRORS, DERIVATION_RETRY_MARKER

#: Faults an operator may clear even though the worker's automatic budget is
#: spent.  None is auto-recoverable, and each is here for a stated reason:
#: ``http_400`` came from unbounded embedding input, which now has a bound;
#: ``candidate_attempt_interrupted`` is the at-most-once fence firing after a
#: crash, which the three-table reopen undoes; and an account refusal failed
#: its item outright before the worker learned to park it, so the rows it left
#: can only come back once someone has fixed the account.
_OPERATOR_ONLY_FAILURES = frozenset({
    "http_400",
    "candidate_attempt_interrupted",
}) | ACCOUNT_REFUSALS

#: Faults.  Clearing these is what moves an instance from degraded to healthy.
#: Derived from the worker's transient set so the two cannot drift apart again,
#: and lower-cased to match what ``failure_kind`` produces -- the worker's set
#: is matched against raw codes by SQL and carries a few in upper case.
ACTIONABLE_FAILURES = frozenset(
    code.lower() for code in AUTO_RECOVERABLE_ERRORS
) | _OPERATOR_ONLY_FAILURES

#: By-design outcomes.  Re-running them asserts that something upstream changed.
TERMINAL_FAILURES = frozenset({
    "derivation_invalid",
    "input_invalid",
})

#: Stamped on every row this grants a re-look, so the grant is visible and
#: cannot be repeated within one schema generation.
RETRY_MARKER = "retried"
# Per work item, not per schema generation; explicit operator retries remain separate.
NEEDS_REVIEW_COUNT = f"""
    SELECT count(*) FROM work_items WHERE state='failed'
    AND last_error_code LIKE '%{DERIVATION_RETRY_MARKER}|%'
    AND lower(last_error_code) LIKE '%|derivation_invalid'
"""

_MARKER_RE = re.compile(rf"(?:^|\|){RETRY_MARKER}:(\d+)\|")


def validation_feedback(code: object, field: object) -> dict[str, str]:
    """Return bounded validation symbols, never exception/output text.

    Legacy rows may lack a field or carry a decorated work code. In that case
    the only honest repair hint is the generic derivation/payload failure.
    """
    safe_code = code.upper() if isinstance(code, str) else ""
    if safe_code not in {"INPUT_INVALID", "DERIVATION_INVALID"}:
        safe_code = "DERIVATION_INVALID"
    safe_field = field if (isinstance(field, str)
                           and re.fullmatch(r"[A-Za-z0-9_./\[\]-]{1,240}", field)
                           and not contains_secret_like_text(field)) else "payload"
    return {"code": safe_code, "field": safe_field}


def failure_kind(error_code: object) -> str:
    """The bare failure kind inside a possibly decorated error code.

    Codes accumulate history (``auto_retry:1|timeout``,
    ``budget_checked:1108|input_invalid``), so the kind is the last segment.
    """
    text = str(error_code or "").strip().lower()
    return text.rsplit("|", 1)[-1] if text else ""


def already_retried(error_code: object, *, generation: int) -> bool:
    """Whether this row already had its re-look in this schema generation."""
    return any(int(found) == generation for found in _MARKER_RE.findall(str(error_code or "")))


def retry_class(error_code: object) -> str | None:
    """``"actionable"``, ``"terminal"``, or ``None`` for anything else."""
    kind = failure_kind(error_code)
    if kind in ACTIONABLE_FAILURES:
        return "actionable"
    if kind in TERMINAL_FAILURES:
        return "terminal"
    return None


def marked(error_code: object, *, generation: int) -> str:
    """The error code to store on a row being re-opened."""
    return f"{RETRY_MARKER}:{generation}|{str(error_code or '').strip()}"[:1024]


def selects(error_code: object, *, include_terminal: bool, generation: int) -> bool:
    """Whether this failure should be granted a re-look now."""
    if already_retried(error_code, generation=generation):
        return False
    found = retry_class(error_code)
    if found == "actionable":
        return True
    return found == "terminal" and include_terminal


def validate_page(limit: object) -> int:
    if type(limit) is not int or type(limit) is bool or not 1 <= limit <= 256:
        raise ContractError("INPUT_INVALID", "retry_limit")
    return limit


__all__ = [
    "ACTIONABLE_FAILURES",
    "RETRY_MARKER",
    "TERMINAL_FAILURES",
    "already_retried",
    "failure_kind",
    "marked",
    "retry_class",
    "selects",
    "validate_page",
]
