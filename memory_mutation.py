"""Single-owner transaction boundary for public durable memory mutations."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class MemoryMutationTransactionError(RuntimeError):
    """Raised when a public mutation cannot own a clean SQLite transaction."""


class MemoryMutationService:
    """Own ``BEGIN IMMEDIATE`` through commit/rollback for one provider mutation.

    Repository and companion helpers called inside this scope must remain
    transaction-neutral.  Acquiring the SQLite write reservation before reads
    makes ownership/lifecycle checks serializable across provider objects,
    processes, and independent SQLite connections.
    """

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._provider._lock:
            conn = self._provider._require_conn()
            if conn.in_transaction:
                raise MemoryMutationTransactionError(
                    "durable mutation requires a clean SQLite transaction boundary"
                )
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise


__all__ = ["MemoryMutationService", "MemoryMutationTransactionError"]
