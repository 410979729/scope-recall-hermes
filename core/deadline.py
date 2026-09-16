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
import time
from typing import Iterator

# Same default and range as vector.embedder.query_timeout_seconds.
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

