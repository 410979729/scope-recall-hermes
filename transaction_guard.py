"""Release leftover snapshot transactions before network I/O on truth connections.

Issue #47's lock-holder class is a SQLite write transaction left open across
LLM, embedding, or other network callbacks. Snapshot and plan reads must
finish and release before those callbacks; apply uses a later short
transaction. This module does not own digest orchestration, schema, or
candidate apply.

Release rolls back only leftover, contractually disposable snapshot state.
Callers must commit known legitimate DML before invoking the boundary helper.
"""

from __future__ import annotations

import sqlite3

from .sqlite_recovery import rollback_if_active


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


__all__ = [
    "OpenTransactionAtNetworkBoundaryError",
    "assert_no_open_transaction",
    "prepare_network_boundary",
    "release_snapshot_transaction",
]
