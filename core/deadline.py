"""One monotonic request deadline shared by bounded request wait points.

Foreground work binds a deadline in its caller's clock domain. Remaining time
caps locks, SQLite busy waits, numerical scan blocks and Experience startup.
Already running native statements or NumPy blocks can finish after the limit;
scan tasks are joined before their read mappings are released.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Iterator

# Same default and range as vector.embedder.query_timeout_seconds.
_CURRENT: ContextVar[RequestDeadline | None] = ContextVar(
    "scope_recall_request_deadline",
    default=None,
)


@dataclass(frozen=True)
class RequestDeadline:
    """Absolute deadline and remaining budget in one monotonic clock domain."""

    started_monotonic: float
    deadline_monotonic: float
    budget_seconds: float
    clock: Callable[[], float] | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_budget(
        cls,
        budget_seconds: float,
        *,
        now: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> RequestDeadline:
        """Create a relative budget using the selected monotonic clock."""
        started = (time.monotonic() if clock is None else clock()) if now is None else float(now)
        budget = float(budget_seconds)
        return cls(
            started_monotonic=started,
            deadline_monotonic=started + budget,
            budget_seconds=budget,
            clock=clock,
        )

    @classmethod
    def from_absolute(
        cls,
        deadline_monotonic: float,
        *,
        now: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> RequestDeadline:
        """Bind an absolute limit and its starting tick to one clock."""
        started = (time.monotonic() if clock is None else clock()) if now is None else float(now)
        absolute = float(deadline_monotonic)
        return cls(
            started_monotonic=started,
            deadline_monotonic=absolute,
            budget_seconds=max(0.0, absolute - started),
            clock=clock,
        )

    @classmethod
    def within(
        cls,
        deadline_monotonic: float,
        *,
        budget_seconds: float,
        clock: Callable[[], float] | None = None,
    ) -> RequestDeadline:
        """Narrow a caller's absolute deadline without changing its clock domain."""
        selected = request_clock(clock)
        now = selected()
        left = min(float(budget_seconds), float(deadline_monotonic) - now)
        parent = current_request_deadline()
        if parent is not None:
            left = min(left, parent.remaining(now))
        return cls.from_absolute(now + left, now=now, clock=selected)

    def remaining(self, now: float | None = None) -> float:
        """Return the capped budget at a supplied tick or at the bound clock's current tick."""
        current = (time.monotonic() if self.clock is None else self.clock()) if now is None else float(now)
        leftover = self.deadline_monotonic - current
        return min(self.budget_seconds, leftover)

    def exhausted(self, now: float | None = None) -> bool:
        return self.remaining(now) <= 0.0


def current_request_deadline() -> RequestDeadline | None:
    return _CURRENT.get()


def request_clock(fallback: Callable[[], float] | None = None) -> Callable[[], float]:
    """Use the bound caller's clock, or the supplied clock outside a request."""
    parent = current_request_deadline()
    selected = fallback if parent is None else parent.clock
    return time.monotonic if selected is None else selected


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


def fits(deadline_monotonic: float, took_seconds: float, *, now: float | None = None) -> bool:
    """Whether one more store call fits before ``deadline_monotonic``, reckoned by the call before it: something is
    left, and at least the ``took_seconds`` that call took.  The store refuses a call given nothing (``storage._open``),
    but one given a little runs as long as the store takes; on a store that slow, a capture's second write or a hook's
    next capture started with less than its predecessor took would run past the deadline."""
    left = deadline_monotonic - (time.monotonic() if now is None else float(now))
    return left > 0 and left >= took_seconds
