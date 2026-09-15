"""SQLite schema helpers that preserve the caller's transaction boundary."""

from __future__ import annotations

import re
import sqlite3

_TRAILING_COMMENT_RE = re.compile(r"(?:--[^\n]*(?:\n|$)|/\*.*?\*/|\s)+\Z", re.DOTALL)


def execute_script_transaction_neutral(
    conn: sqlite3.Connection,
    script: str,
) -> None:
    """Execute a SQL script without the implicit commit performed by executescript.

    ``sqlite3.Connection.executescript`` commits a pending transaction before it
    runs. Schema helpers promise not to alter that boundary when ``commit=False``,
    so statements are parsed with ``sqlite3.complete_statement`` and executed one
    at a time on the caller's connection.
    """

    buffer = ""
    for line in str(script or "").splitlines(keepends=True):
        buffer += line
        if not sqlite3.complete_statement(buffer):
            continue
        statement = buffer.strip()
        buffer = ""
        if statement:
            conn.execute(statement)

    remainder = buffer.strip()
    if remainder and _TRAILING_COMMENT_RE.fullmatch(remainder) is None:
        raise ValueError("incomplete SQL schema statement")


__all__ = ["execute_script_transaction_neutral"]
