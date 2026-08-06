"""SQLite host-parameter budgeting for bounded batched statements.

Callers keep transaction ownership.  This module only divides one ordered
parameter sequence into chunks that fit the live connection limit, including
reserved parameters and repeated placeholders per logical item.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from typing import Any, TypeVar

T = TypeVar("T")
_DEFAULT_SQLITE_VARIABLE_LIMIT = 999


def sqlite_variable_limit(conn: Any) -> int:
    """Return the live host-parameter limit or a conservative fallback."""

    getlimit = getattr(conn, "getlimit", None)
    if callable(getlimit):
        try:
            value = getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
            if isinstance(value, int) and value > 0:
                return value
        except (AttributeError, TypeError, ValueError, sqlite3.Error):
            pass
    return _DEFAULT_SQLITE_VARIABLE_LIMIT


def chunked_sql_parameters(
    conn: Any,
    items: Sequence[T],
    *,
    reserved: int = 0,
    variables_per_item: int = 1,
) -> Iterator[list[T]]:
    """Yield ordered chunks that fit one SQLite statement's variable budget.

    ``reserved`` accounts for parameters unrelated to ``items``.  Use
    ``variables_per_item=2`` when the same logical IDs occur in two clauses of
    one statement, such as a relation-source/target UNION.
    """

    reserved_count = int(reserved)
    per_item = int(variables_per_item)
    if reserved_count < 0:
        raise ValueError("reserved SQLite parameters must be non-negative")
    if per_item < 1:
        raise ValueError("variables_per_item must be at least 1")
    available = sqlite_variable_limit(conn) - reserved_count
    chunk_size = available // per_item
    if chunk_size < 1:
        raise ValueError(
            "SQLite variable limit leaves no room for batched parameters: "
            f"limit={sqlite_variable_limit(conn)}, reserved={reserved_count}, "
            f"variables_per_item={per_item}"
        )
    for offset in range(0, len(items), chunk_size):
        yield list(items[offset : offset + chunk_size])


__all__ = ["chunked_sql_parameters", "sqlite_variable_limit"]
