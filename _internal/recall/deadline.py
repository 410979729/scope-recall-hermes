"""One monotonic request deadline shared by the recall wait points.

Foreground prefetch/search binds an absolute deadline at the door. Companion
I/O reads the remaining budget; bulk workers leave the context empty and keep
their own longer timers. Remaining time caps lock waits, request-scoped SQLite
busy waits (applied by the outer sqlite budget adapter), helper RPC waits,
and Experience start. This is not hard real-time: an already-started SQLite
statement or helper send can overshoot, and helper cleanup after a
request-budget failure may finish on an owned reaper after the caller returns.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
import math
import time
from typing import Any, Iterator, Mapping

# Same default and range as vector.embedder.query_timeout_seconds.
DEFAULT_FOREGROUND_BUDGET_SECONDS = 8.0
_MIN_FOREGROUND_BUDGET_SECONDS = 0.05
_MAX_FOREGROUND_BUDGET_SECONDS = 300.0
EXPERIENCE_MIN_REMAINING_SECONDS = 0.05

_CURRENT: ContextVar[RequestDeadline | None] = ContextVar(
    "scope_recall_request_deadline",
    default=None,
)


@dataclass(frozen=True)
class RequestDeadline:
    """Absolute monotonic deadline for one ordinary recall request."""

    started_monotonic: float
    deadline_monotonic: float
    budget_seconds: float

    @classmethod
    def from_budget(
        cls,
        budget_seconds: float,
        *,
        now: float | None = None,
    ) -> RequestDeadline:
        started = time.monotonic() if now is None else float(now)
        budget = float(budget_seconds)
        return cls(
            started_monotonic=started,
            deadline_monotonic=started + budget,
            budget_seconds=budget,
        )

    @classmethod
    def from_absolute(
        cls,
        deadline_monotonic: float,
        *,
        now: float | None = None,
    ) -> RequestDeadline:
        started = time.monotonic() if now is None else float(now)
        absolute = float(deadline_monotonic)
        return cls(
            started_monotonic=started,
            deadline_monotonic=absolute,
            budget_seconds=max(0.0, absolute - started),
        )

    def remaining(self, now: float | None = None) -> float:
        current = time.monotonic() if now is None else float(now)
        leftover = self.deadline_monotonic - current
        return min(self.budget_seconds, leftover)

    def exhausted(self, now: float | None = None) -> bool:
        return self.remaining(now) <= 0.0


def current_request_deadline() -> RequestDeadline | None:
    return _CURRENT.get()


def bind_request_deadline(deadline: RequestDeadline) -> Token[RequestDeadline | None]:
    return _CURRENT.set(deadline)


def reset_request_deadline(token: Token[RequestDeadline | None]) -> None:
    _CURRENT.reset(token)


@contextmanager
def using_request_deadline(deadline: RequestDeadline) -> Iterator[RequestDeadline]:
    token = bind_request_deadline(deadline)
    try:
        yield deadline
    finally:
        reset_request_deadline(token)


def remaining_seconds(now: float | None = None) -> float | None:
    deadline = current_request_deadline()
    if deadline is None:
        return None
    return deadline.remaining(now)


def acquire_until(lock: Any, deadline: RequestDeadline | None) -> bool:
    """Acquire ``lock`` using only the remaining request budget.

    A missing deadline keeps the historical unbounded acquire. Exhausted
    budget returns False without waiting and without touching the lock
    unless this thread already owns a reentrant lock.
    """

    acquire = getattr(lock, "acquire", None)
    if not callable(acquire):
        return False
    if deadline is None:
        return bool(acquire())
    remaining = deadline.remaining()
    if remaining <= 0.0:
        return bool(acquire(blocking=False))
    return bool(acquire(timeout=remaining))


def resolve_foreground_budget_seconds(config: Mapping[str, Any] | None) -> float:
    """Reuse the existing query-embedding timer as the foreground request budget."""

    raw_vector = config.get("vector") if isinstance(config, Mapping) else None
    raw_embedder = raw_vector.get("embedder") if isinstance(raw_vector, Mapping) else None
    raw = (
        raw_embedder.get("query_timeout_seconds")
        if isinstance(raw_embedder, Mapping)
        else None
    )
    if raw is None:
        return DEFAULT_FOREGROUND_BUDGET_SECONDS
    if isinstance(raw, bool):
        raise ValueError(
            "vector.embedder.query_timeout_seconds must be a finite number "
            f"between {_MIN_FOREGROUND_BUDGET_SECONDS:g} and {_MAX_FOREGROUND_BUDGET_SECONDS:g}"
        )
    try:
        parsed = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "vector.embedder.query_timeout_seconds must be a finite number "
            f"between {_MIN_FOREGROUND_BUDGET_SECONDS:g} and {_MAX_FOREGROUND_BUDGET_SECONDS:g}"
        ) from exc
    if (
        not math.isfinite(parsed)
        or not _MIN_FOREGROUND_BUDGET_SECONDS <= parsed <= _MAX_FOREGROUND_BUDGET_SECONDS
    ):
        raise ValueError(
            "vector.embedder.query_timeout_seconds must be a finite number "
            f"between {_MIN_FOREGROUND_BUDGET_SECONDS:g} and {_MAX_FOREGROUND_BUDGET_SECONDS:g}"
        )
    return parsed


def is_request_budget_failure(exc: BaseException) -> bool:
    """Return whether ``exc`` is a recoverable request-budget exhaustion."""

    if isinstance(exc, TimeoutError):
        return True
    text = str(exc).casefold()
    return "deadline exhausted" in text or "request deadline" in text
