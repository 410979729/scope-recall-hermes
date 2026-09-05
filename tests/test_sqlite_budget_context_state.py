"""Request SQLite busy-timeout context must restore exact execute ownership."""

from __future__ import annotations

import sqlite3

import pytest

from scope_recall._internal.recall.deadline import (
    RequestDeadline,
    using_request_deadline,
)
from scope_recall.recall_sqlite_budget import using_request_busy_timeout


class _Conn(sqlite3.Connection):
    """Subclass so an instance can own a temporary execute override."""


def _connect_subclass() -> _Conn:
    conn = sqlite3.connect(":memory:", factory=_Conn)
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def _busy_timeout_ms(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA busy_timeout").fetchone()[0])


def _inject_execute(conn: _Conn) -> tuple[object, list[str]]:
    original = conn.execute
    seen: list[str] = []

    def injected(*args: object, **kwargs: object):
        if args:
            seen.append(str(args[0]))
        return original(*args, **kwargs)

    conn.execute = injected
    return injected, seen


def test_single_instance_execute_override_is_restored() -> None:
    conn = _connect_subclass()
    try:
        injected, seen = _inject_execute(conn)
        with using_request_deadline(RequestDeadline.from_budget(1)):
            with using_request_busy_timeout(conn):
                assert conn.execute is not injected
                conn.execute("SELECT 1")
            assert conn.execute is injected
        assert conn.execute is injected
        assert _busy_timeout_ms(conn) == 2000
        seen.clear()
        conn.execute("SELECT 2")
        assert any("SELECT 2" in sql for sql in seen)
    finally:
        conn.close()


def test_nested_busy_timeout_restores_outer_wrapper() -> None:
    conn = _connect_subclass()
    try:
        injected, _seen = _inject_execute(conn)
        with using_request_deadline(RequestDeadline.from_budget(1)):
            with using_request_busy_timeout(conn):
                outer = conn.execute
                with using_request_busy_timeout(conn):
                    conn.execute("SELECT 1")
                    assert conn.execute is not outer
                assert conn.execute is outer
            assert conn.execute is injected
        assert conn.execute is injected
        assert _busy_timeout_ms(conn) == 2000
    finally:
        conn.close()


def test_exception_unwinds_instance_execute_override() -> None:
    conn = _connect_subclass()
    try:
        injected, _seen = _inject_execute(conn)
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO items VALUES (1)")
        with using_request_deadline(RequestDeadline.from_budget(1)):
            with using_request_busy_timeout(conn):
                outer = conn.execute
                with pytest.raises(RuntimeError, match="boom"):
                    with using_request_busy_timeout(conn):
                        raise RuntimeError("boom")
                assert conn.execute is outer
                with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
                    with using_request_busy_timeout(conn):
                        conn.execute("INSERT INTO items VALUES (1)")
                assert conn.execute is outer
            assert conn.execute is injected
        assert conn.execute is injected
        assert _busy_timeout_ms(conn) == 2000
    finally:
        conn.close()


def test_inherited_execute_absence_is_restored() -> None:
    conn = _connect_subclass()
    try:
        assert "execute" not in conn.__dict__
        with using_request_deadline(RequestDeadline.from_budget(1)):
            with using_request_busy_timeout(conn):
                assert "execute" in conn.__dict__
                assert conn.execute("SELECT 1").fetchone()[0] == 1
            assert "execute" not in conn.__dict__
        assert "execute" not in conn.__dict__
        assert _busy_timeout_ms(conn) == 2000
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        conn.close()


def test_ordinary_connection_uses_readonly_execute_fallback() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA busy_timeout=2000")
        with pytest.raises(AttributeError, match="read-only"):
            conn.execute = lambda *_a, **_k: None  # type: ignore[method-assign]
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO items VALUES (1)")
        with using_request_deadline(RequestDeadline.from_budget(1)):
            with using_request_busy_timeout(conn):
                assert type(conn.execute).__name__ == "builtin_function_or_method"
                assert conn.execute("SELECT 1").fetchone()[0] == 1
                with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
                    conn.execute("INSERT INTO items VALUES (1)")
        assert type(conn.execute).__name__ == "builtin_function_or_method"
        assert _busy_timeout_ms(conn) == 2000
        with pytest.raises(AttributeError, match="read-only"):
            conn.execute = lambda *_a, **_k: None  # type: ignore[method-assign]
    finally:
        conn.close()
