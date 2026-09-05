"""Request-scoped SQLite busy-timeout adapter.

The inner deadline module owns only monotonic remaining time. This outer
adapter applies that budget to a connection's busy wait and restores the
exact entry ``execute`` ownership. Semantics must stay identical to the
former in-leaf implementation.
"""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from typing import Any, Iterator

from ._internal.recall.deadline import remaining_seconds


def _busy_timeout_ms(conn: Any) -> int | None:
    execute = getattr(conn, "execute", None)
    if not callable(execute):
        return None
    try:
        cursor = execute("PRAGMA busy_timeout")
    except sqlite3.Error:
        return None
    fetchone = getattr(cursor, "fetchone", None)
    if not callable(fetchone):
        return None
    try:
        row = fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    getter = getattr(row, "__getitem__", None)
    if not callable(getter):
        return None
    try:
        raw = getter(0)
    except (TypeError, IndexError):
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _instance_attribute(obj: Any, name: str) -> tuple[bool, Any]:
    namespace = getattr(obj, "__dict__", None)
    if isinstance(namespace, dict) and name in namespace:
        return True, namespace[name]
    return False, None


@contextmanager
def using_request_busy_timeout(conn: Any) -> Iterator[None]:
    """Cap this connection's SQLite busy wait to the remaining request budget.

    Restores the prior busy_timeout and the exact entry ``execute`` attribute
    state: an instance override is put back, an inherited descriptor is left
    without an instance attribute. Nested contexts unwind to the wrapper they
    replaced, including on exception. Does not install or replace a progress
    handler, does not roll back, and does not swallow integrity or schema
    errors. A read-only ``execute`` falls back to one request-scoped PRAGMA
    write. No bound deadline keeps the connection's existing busy timeout.
    The wait is never increased above the connection's configured timeout.
    """

    remaining = remaining_seconds()
    execute = getattr(conn, "execute", None)
    if remaining is None or not callable(execute):
        yield
        return
    prior_ms = _busy_timeout_ms(conn)
    if prior_ms is None:
        yield
        return

    def execute_with_budget(*args: Any, **kwargs: Any) -> Any:
        current = remaining_seconds()
        if current is not None:
            timeout_ms = min(prior_ms, max(0, int(current * 1000.0)))
            execute(f"PRAGMA busy_timeout={timeout_ms}")
        return execute(*args, **kwargs)

    had_instance_override, prior_instance_execute = _instance_attribute(
        conn, "execute"
    )
    patched = False
    try:
        conn.execute = execute_with_budget
        patched = True
    except (AttributeError, TypeError):
        execute(
            f"PRAGMA busy_timeout={min(prior_ms, max(0, int(remaining * 1000.0)))}"
        )
    try:
        yield
    finally:
        if patched:
            if had_instance_override:
                conn.execute = prior_instance_execute
            else:
                try:
                    del conn.execute
                except AttributeError:
                    conn.execute = execute
        execute(f"PRAGMA busy_timeout={prior_ms}")
