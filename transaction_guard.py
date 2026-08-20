"""Release leftover snapshot transactions before network I/O on truth connections.

Issue #47's lock-holder class is a SQLite write transaction left open across
LLM, embedding, or other network callbacks. Snapshot and plan reads must
finish and release before those callbacks; apply uses a later short
transaction. This module does not own digest orchestration, schema, or
candidate apply.

Release rolls back only leftover, contractually disposable snapshot state.
Callers must commit known legitimate DML before invoking the boundary helper.

Write transactions are also measured so long holders are visible in receipts
and logs instead of being discovered through another process's
``database is locked`` noise.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .sqlite_recovery import rollback_if_active

logger = logging.getLogger(__name__)

SLOW_TRUTH_TRANSACTION_SECONDS = 5.0

_LAST_TRANSACTION_STATS: dict[str, Any] = {
    "max_ms": 0.0,
    "max_context": "",
    "slow_count": 0,
}


class OpenTransactionAtNetworkBoundaryError(RuntimeError):
    """A network callback still saw an open truth transaction after release."""


def release_snapshot_transaction(conn: sqlite3.Connection | None) -> bool:
    """End any leftover snapshot transaction before network I/O.

    Returns whether a transaction was rolled back. Safe to call when the
    connection is idle or missing. Known durable DML must already be committed
    by the caller; this helper must not be used to discard audit writes.
    """

    if conn is None:
        return False
    return rollback_if_active(conn)


def assert_no_open_transaction(conn: sqlite3.Connection | None, context: str) -> None:
    """Fail closed if a network-bound path still holds a truth transaction."""

    if conn is None:
        return
    if getattr(conn, "in_transaction", False):
        raise OpenTransactionAtNetworkBoundaryError(
            f"{context} must not run inside an open SQLite truth transaction"
        )


def prepare_network_boundary(conn: sqlite3.Connection | None, context: str) -> None:
    """Release a leftover snapshot transaction, then confirm the connection is idle."""

    release_snapshot_transaction(conn)
    assert_no_open_transaction(conn, context)


def _record_transaction_duration(context: str, started_monotonic: float) -> None:
    elapsed_ms = (time.perf_counter() - started_monotonic) * 1000.0
    if elapsed_ms > _LAST_TRANSACTION_STATS["max_ms"]:
        _LAST_TRANSACTION_STATS["max_ms"] = round(elapsed_ms, 2)
        _LAST_TRANSACTION_STATS["max_context"] = context
    if elapsed_ms >= SLOW_TRUTH_TRANSACTION_SECONDS * 1000.0:
        _LAST_TRANSACTION_STATS["slow_count"] = (
            int(_LAST_TRANSACTION_STATS["slow_count"]) + 1
        )
        logger.warning(
            "Scope Recall truth write transaction held for %.1fms (%s); "
            "budget is %.0fs",
            elapsed_ms,
            context,
            SLOW_TRUTH_TRANSACTION_SECONDS,
        )


class TruthTransactionTimer:
    """Idempotent duration probe for one truth write transaction."""

    def __init__(self, context: str) -> None:
        self._context = context
        self._started = time.perf_counter()
        self._stopped = False

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        _record_transaction_duration(self._context, self._started)


@contextmanager
def timed_truth_transaction(context: str) -> Iterator[None]:
    """Measure one truth write transaction and surface slow holders."""

    timer = TruthTransactionTimer(context)
    try:
        yield
    finally:
        timer.stop()


def transaction_duration_stats() -> dict[str, Any]:
    """Expose process-lifetime write-transaction duration highlights."""

    return dict(_LAST_TRANSACTION_STATS)


def reset_transaction_duration_stats() -> None:
    _LAST_TRANSACTION_STATS.update({"max_ms": 0.0, "max_context": "", "slow_count": 0})


__all__ = [
    "OpenTransactionAtNetworkBoundaryError",
    "SLOW_TRUTH_TRANSACTION_SECONDS",
    "TruthTransactionTimer",
    "assert_no_open_transaction",
    "prepare_network_boundary",
    "release_snapshot_transaction",
    "reset_transaction_duration_stats",
    "timed_truth_transaction",
    "transaction_duration_stats",
]
