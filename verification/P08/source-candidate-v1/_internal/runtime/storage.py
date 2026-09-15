"""SQLite bootstrap helpers. Provider must not inline PRAGMA/schema SQL."""

from __future__ import annotations

import sqlite3
from typing import Any

from ...journal_store import _journal_unprocessed_count, ensure_journal_schema
from ...sql_store import ensure_schema
from ...truth_connection import connect_truth_database


def _published_session(host: Any) -> Any:
    """Resolve the published connection owner. Never assign ``provider._conn``."""

    session = getattr(host, "_truth", None)
    if session is not None:
        return session
    return host


def _host_db_path(host: Any, session: Any) -> Any:
    path = getattr(host, "_db_path", None)
    if path is not None:
        return path
    return getattr(session, "db_path", None)


def apply_sqlite_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")


def prepare_truth_schema(
    conn: sqlite3.Connection,
    *,
    commit: bool = False,
    schema_fn: Any | None = None,
    journal_fn: Any | None = None,
) -> None:
    (schema_fn or ensure_schema)(conn, commit=False)
    (journal_fn or ensure_journal_schema)(conn, commit=False)
    if commit:
        conn.commit()


def probe_sqlite_write(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
        return True
    except Exception:
        try:
            if conn.in_transaction:
                conn.rollback()
        except Exception:
            return False
        return False


def finish_writer_schema_setup(
    provider: Any,
    conn: sqlite3.Connection,
    *,
    schema_fn: Any,
    journal_fn: Any,
    ensure_triggers_fn: Any,
) -> None:
    prepare_truth_schema(conn, schema_fn=schema_fn, journal_fn=journal_fn)
    ensure_triggers_fn(conn, provider._db_path)
    conn.commit()


def open_configured_truth_connection(
    db_path: Any,
    *,
    timeout: float,
    connect_fn: Any | None = None,
    schema_fn: Any | None = None,
    journal_fn: Any | None = None,
    install_authorizer_fn: Any | None = None,
    ensure_triggers_fn: Any | None = None,
    lease_token: Any | None = None,
    row_factory: Any | None = None,
) -> sqlite3.Connection:
    """Open one truth connection and apply schema/lease setup.

    Callers pass monkeypatchable functions from the provider module so
    existing tests keep patching names on ``provider.py``.
    """

    opener = connect_fn or connect_truth_database
    conn = opener(
        db_path,
        mode="rwc",
        check_same_thread=False,
        timeout=timeout,
    )
    try:
        if install_authorizer_fn is not None:
            if lease_token is None:
                install_authorizer_fn(conn, db_path)
            else:
                install_authorizer_fn(conn, db_path, lease_token=lease_token)
        if row_factory is not None:
            conn.row_factory = row_factory
        apply_sqlite_pragmas(conn)
        prepare_truth_schema(conn, schema_fn=schema_fn, journal_fn=journal_fn)
        if ensure_triggers_fn is not None:
            if lease_token is None:
                ensure_triggers_fn(conn, db_path)
            else:
                ensure_triggers_fn(conn, db_path, lease_token=lease_token)
        conn.commit()
        return conn
    except BaseException:
        try:
            if bool(getattr(conn, "in_transaction", False)):
                conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        raise


def truth_has_unprocessed_journal(provider: Any) -> bool:
    try:
        with provider._lock:
            return _journal_unprocessed_count(provider._require_conn()) > 0
    except Exception:
        return False


def configure_published_writer_connection(
    provider: Any,
    *,
    timeout: float,
    connect_fn: Any,
    authorizer_fn: Any,
    schema_fn: Any,
    journal_fn: Any,
    ensure_triggers_fn: Any,
) -> sqlite3.Connection:
    """Configure one already-published writer connection.

    The connection is assigned to the published TruthSession before
    authorizer/schema work. If close fails after a setup error, the
    published connection is retained so lease tests can still observe
    the handle.
    """

    session = _published_session(provider)
    db_path = _host_db_path(provider, session)
    conn = connect_fn(
        db_path,
        mode="rwc",
        check_same_thread=False,
        timeout=timeout,
    )
    session._conn = conn
    try:
        authorizer_fn(conn, db_path)
        apply_sqlite_pragmas(conn)
        prepare_truth_schema(conn, schema_fn=schema_fn, journal_fn=journal_fn)
        ensure_triggers_fn(conn, db_path)
        conn.commit()
    except BaseException as setup_error:
        try:
            if bool(getattr(conn, "in_transaction", False)):
                conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except BaseException as close_error:
            raise close_error from setup_error
        if session._conn is conn:
            session._conn = None
        raise
    return conn


def open_readonly_truth_connection(
    db_path: Any,
    *,
    timeout: float,
    connect_fn: Any | None = None,
) -> sqlite3.Connection:
    opener = connect_fn or connect_truth_database
    return opener(
        db_path,
        mode="ro",
        check_same_thread=False,
        timeout=timeout,
    )


def store_provider_event_candidates(provider: Any, **kwargs: Any) -> dict[str, Any]:
    from ...candidate_store import store_event_candidates

    with provider._lock:
        return store_event_candidates(provider._require_conn(), **kwargs)
