"""Small SQLite contention and transaction-recovery primitives.

Callers own retry policy because a safe retry boundary differs between truth
writes, journal digests, and derived-index maintenance.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def is_sqlite_lock_contention(exc: BaseException) -> bool:
    """Return whether an exception represents SQLite BUSY/LOCKED contention."""

    if not isinstance(exc, sqlite3.OperationalError):
        return False
    error_code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(error_code, int) and (error_code & 0xFF) in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }:
        return True
    message = str(exc).strip().lower()
    return "database is locked" in message or "database table is locked" in message


def rollback_if_active(conn: Any) -> bool:
    """Rollback one active transaction and report whether work was performed.

    The function intentionally lets rollback failures propagate. A caller must
    not claim recovery when SQLite could not release a failed transaction.
    """

    if not bool(getattr(conn, "in_transaction", False)):
        return False
    conn.rollback()
    return True
