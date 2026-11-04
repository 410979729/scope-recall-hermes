"""Bounded operator recovery grants and sanitized validation feedback for failed work.

Unknown failures remain actionable; by-design refusals require explicit inclusion. A per-release marker prevents
repeated grants, independently of schema changes and automatic infrastructure recovery.
"""

from __future__ import annotations

import re

from .._version import __version__
from ..contracts import ContractError
from .failure_policy import (
    TERMINAL_FAILURES,
    failure_kind,
)
from .secret_patterns import contains_secret_like_text

DERIVATION_RETRY_MARKER = "derivation_retry:1"
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
    "already_retried",
    "marked",
    "retry_class",
    "selects",
    "validate_page",
    "wanted",
]
