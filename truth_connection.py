"""Fail-closed SQLite truth-database connection boundary.

SQLite foreign-key enforcement is connection-local and defaults to disabled.
Every Scope Recall connection that can read or mutate the authoritative memory
SQLite database must cross this module before schema, transactions, migrations,
or activation authorizers are installed. Vector companion databases are not
truth stores and intentionally use their own connection boundary.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Literal


TruthDatabaseMode = Literal["ro", "rw", "rwc"]
_DEFAULT_ISOLATION_LEVEL = object()


class TruthDatabaseConnectionError(RuntimeError):
    """A SQLite truth connection could not satisfy mandatory invariants."""


def require_foreign_keys(conn: sqlite3.Connection) -> None:
    """Enable and verify connection-local foreign-key enforcement.

    ``PRAGMA foreign_keys`` is a no-op inside an active transaction, so treating
    such a call as successful would create a false integrity guarantee.
    """

    if conn.in_transaction:
        raise TruthDatabaseConnectionError(
            "foreign-key enforcement must be configured before any transaction"
        )
    conn.execute("PRAGMA foreign_keys=ON")
    row = conn.execute("PRAGMA foreign_keys").fetchone()
    enabled = int(row[0]) if row is not None else 0
    if enabled != 1:
        raise TruthDatabaseConnectionError(
            f"SQLite truth connection refused foreign-key enforcement: {enabled}"
        )


def require_query_only(conn: sqlite3.Connection) -> None:
    """Enable and verify SQLite's defence-in-depth read-only guard."""

    if conn.in_transaction:
        raise TruthDatabaseConnectionError(
            "query-only mode must be configured before any transaction"
        )
    conn.execute("PRAGMA query_only=ON")
    row = conn.execute("PRAGMA query_only").fetchone()
    enabled = int(row[0]) if row is not None else 0
    if enabled != 1:
        raise TruthDatabaseConnectionError(
            f"SQLite truth connection refused query-only enforcement: {enabled}"
        )


def connect_truth_database(
    path: str | Path,
    *,
    mode: TruthDatabaseMode = "rwc",
    timeout: float = 30.0,
    check_same_thread: bool = True,
    isolation_level: str | None | object = _DEFAULT_ISOLATION_LEVEL,
    row_factory: Any = sqlite3.Row,
) -> sqlite3.Connection:
    """Open one authoritative SQLite truth connection and verify FK=ON.

    ``mode='ro'`` and ``mode='rw'`` require an existing database; ``rwc`` may
    create it. The returned connection is ready for callers to install the
    activation authorizer and configure WAL/synchronous policy.
    """

    normalized_mode = str(mode)
    if normalized_mode not in {"ro", "rw", "rwc"}:
        raise ValueError(f"unsupported truth database mode: {mode!r}")
    kwargs: dict[str, Any] = {
        "uri": True,
        "timeout": float(timeout),
        "check_same_thread": bool(check_same_thread),
    }
    if isolation_level is not _DEFAULT_ISOLATION_LEVEL:
        kwargs["isolation_level"] = isolation_level
    uri = f"file:{Path(path).expanduser()}?mode={normalized_mode}"
    conn = sqlite3.connect(uri, **kwargs)
    try:
        require_foreign_keys(conn)
        if normalized_mode == "ro":
            require_query_only(conn)
        conn.row_factory = row_factory
        return conn
    except BaseException:
        conn.close()
        raise


__all__ = [
    "TruthDatabaseConnectionError",
    "TruthDatabaseMode",
    "connect_truth_database",
    "require_foreign_keys",
    "require_query_only",
]
