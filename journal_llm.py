"""LLM call/retry/quarantine helpers for journal digest extraction.

Failures are classified for dead-letter or retry handling instead of being hidden behind empty digest output."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import Any, cast

from .capture_filters import sanitize_report_text
from .http_utils import UnsafeEndpointError
from .nightly_llm import call_llm, classify_llm_error as _classify_llm_digest_error

__all__ = [
    "JournalDigestLLMError",
    "_call_llm_with_retries",
    "_classify_llm_digest_error",
    "_quarantine_classification",
]

LLMCall = Callable[..., str]


class JournalDigestLLMError(RuntimeError):
    def __init__(self, message: str, *, attempts: int, error_kind: str, retryable: bool) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.error_kind = error_kind
        self.retryable = retryable


def _active_call_llm() -> LLMCall:
    """Return the journal LLM call hook, preserving legacy monkeypatch behavior.

    Historically tests and operator probes patched ``scope_recall.journal.call_llm``
    before calling ``journal._call_llm_with_retries``.  H3 moves the retry helper
    here, but the compatibility hook still checks the loaded journal module first.
    This keeps the old journal module as a stable monkeypatch surface without a
    static import cycle from ``journal_llm`` back to ``journal``.
    """

    journal_module = sys.modules.get("scope_recall.journal")
    journal_call = getattr(journal_module, "call_llm", None) if journal_module is not None else None
    return cast(LLMCall, journal_call) if callable(journal_call) else call_llm


def _quarantine_classification(error: Exception) -> tuple[str, dict[str, Any]]:
    if isinstance(error, JournalDigestLLMError):
        classification = "retry_exhausted" if error.retryable else "dead_letter"
        reason_prefix = "retry-exhausted" if error.retryable else "dead-letter"
        sanitized = sanitize_report_text(str(error)[:400])
        return f"{reason_prefix}:{error.error_kind}", {
            "classification": classification,
            "kind": error.error_kind,
            "retryable": bool(error.retryable),
            "attempts": int(error.attempts),
            "message": sanitized,
        }
    kind, retryable = _classify_llm_digest_error(error)
    classification = "retry_exhausted" if retryable else "dead_letter"
    reason_prefix = "retry-exhausted" if retryable else "dead-letter"
    return f"{reason_prefix}:{kind}", {
        "classification": classification,
        "kind": kind,
        "retryable": retryable,
        "attempts": 1,
        "message": sanitize_report_text(f"{type(error).__name__}: {str(error)[:400]}"),
    }


def _call_llm_with_retries(
    prompt: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    timeout: float,
    api_mode: str,
    max_attempts: int,
    retry_delay: float,
    endpoint: str = "",
    append_v1: bool = True,
    allow_insecure_endpoint: bool = False,
    thinking: dict[str, Any] | None = None,
    system_prompt: str | None = None,
) -> str:
    last_error: Exception | None = None
    last_kind = "unknown"
    last_retryable = True
    active_call_llm = _active_call_llm()
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            if not isinstance(allow_insecure_endpoint, bool):
                raise UnsafeEndpointError("allow_insecure must be a boolean")
            call_kwargs = {
                "model": model,
                "base_url": base_url,
                "api_key": api_key,
                "timeout": timeout,
                "api_mode": api_mode,
                "endpoint": endpoint,
                "append_v1": append_v1,
            }
            # Preserve the legacy monkeypatch/custom-hook call contract when
            # the new opt-ins are not in use.  Explicit insecure HTTP support
            # and a configured thinking dict are the only cases that require
            # the new keywords.
            if allow_insecure_endpoint:
                call_kwargs["allow_insecure_endpoint"] = True
            if thinking is not None:
                call_kwargs["thinking"] = thinking
            if system_prompt is not None:
                call_kwargs["system_prompt"] = system_prompt
            return active_call_llm(prompt, **call_kwargs)
        except Exception as exc:
            if isinstance(exc, JournalDigestLLMError):
                raise
            last_error = exc
            last_kind, last_retryable = _classify_llm_digest_error(exc)
            if (not last_retryable) or attempt >= max_attempts:
                raise JournalDigestLLMError(
                    f"{last_kind} after {attempt} attempt(s): {type(exc).__name__}: {sanitize_report_text(str(exc)[:400])}",
                    attempts=attempt,
                    error_kind=last_kind,
                    retryable=last_retryable,
                ) from exc
            if retry_delay > 0:
                time.sleep(retry_delay)
    assert last_error is not None
    raise JournalDigestLLMError(
        f"{last_kind} after {max_attempts} attempt(s): {type(last_error).__name__}: {sanitize_report_text(str(last_error)[:400])}",
        attempts=max_attempts,
        error_kind=last_kind,
        retryable=last_retryable,
    ) from last_error
