"""Fail-closed SQLite truth-database connection boundary.

SQLite foreign-key enforcement is connection-local and defaults to disabled.
Every Scope Recall connection that can read or mutate the authoritative memory
SQLite database must cross this module before schema, transactions, migrations,
or activation authorizers are installed. Vector companion databases are not
truth stores and intentionally use their own connection boundary.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from typing import Any, Literal


TruthDatabaseMode = Literal["ro", "rw", "rwc"]
_DEFAULT_ISOLATION_LEVEL = object()


class TruthDatabaseConnectionError(RuntimeError):
    """A SQLite truth connection could not satisfy mandatory invariants."""


TRUTH_DIRECTORY_MODE = 0o700
TRUTH_DATABASE_MODE = 0o600


def _descriptor_permissions_supported() -> bool:
    """Return whether POSIX descriptor chmod is meaningful on this platform."""

    return os.name != "nt" and callable(getattr(os, "fchmod", None))


def _harden_mutable_truth_path(path: Path, *, create: bool) -> None:
    """Create or harden mutable truth storage without following final symlinks.

    Windows uses the containing profile's inherited ACL boundary. POSIX systems
    harden both the directory and database through opened descriptors so a path
    replacement cannot redirect the chmod operation.
    """

    parent = path.parent
    if path.is_symlink() or parent.is_symlink():
        raise TruthDatabaseConnectionError(
            "SQLite truth storage cannot use symlink paths"
        )
    if create:
        parent.mkdir(parents=True, exist_ok=True, mode=TRUTH_DIRECTORY_MODE)
    if path.is_symlink() or parent.is_symlink():
        raise TruthDatabaseConnectionError(
            "SQLite truth storage cannot use symlink paths"
        )
    if not _descriptor_permissions_supported():
        return

    fchmod = getattr(os, "fchmod")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = (
        os.O_RDWR
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    if create:
        file_flags |= os.O_CREAT

    try:
        directory_fd = os.open(parent, directory_flags)
        try:
            if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
                raise TruthDatabaseConnectionError(
                    "SQLite truth storage parent is not a directory"
                )
            fchmod(directory_fd, TRUTH_DIRECTORY_MODE)
        finally:
            os.close(directory_fd)

        database_fd = os.open(path, file_flags, TRUTH_DATABASE_MODE)
        try:
            if not stat.S_ISREG(os.fstat(database_fd).st_mode):
                raise TruthDatabaseConnectionError(
                    "SQLite truth storage is not a regular file"
                )
            fchmod(database_fd, TRUTH_DATABASE_MODE)
        finally:
            os.close(database_fd)
    except TruthDatabaseConnectionError:
        raise
    except OSError as exc:
        raise TruthDatabaseConnectionError(
            "SQLite truth storage is unsafe or inaccessible"
        ) from exc


def truth_storage_permissions(path: str | Path) -> dict[str, Any]:
    """Return a read-only permission report for the truth DB and its directory."""

    expanded_path = Path(path).expanduser()
    db_path = Path(os.path.abspath(os.fspath(expanded_path)))
    symlink = db_path.is_symlink() or db_path.parent.is_symlink()
    if symlink:
        return {
            "status": "unsafe",
            "ok": False,
            "platform": os.name,
            "policy": "owner_only_posix_modes",
            "path": str(db_path),
            "directory": str(db_path.parent),
            "symlink": True,
        }
    if not db_path.exists():
        return {
            "status": "missing",
            "ok": False,
            "platform_policy": (
                "windows-inherited-acl" if os.name == "nt" else "posix-owner-only"
            ),
            "directory_mode": "",
            "database_mode": "",
        }
    if not _descriptor_permissions_supported():
        return {
            "status": "acl_managed",
            "ok": True,
            "platform_policy": "windows-inherited-acl",
            "directory_mode": "",
            "database_mode": "",
        }

    directory_mode = stat.S_IMODE(db_path.parent.stat().st_mode)
    database_mode = stat.S_IMODE(db_path.stat().st_mode)
    ok = (
        directory_mode == TRUTH_DIRECTORY_MODE
        and database_mode == TRUTH_DATABASE_MODE
    )
    return {
        "status": "ready" if ok else "unsafe",
        "ok": ok,
        "platform_policy": "posix-owner-only",
        "directory_mode": f"{directory_mode:04o}",
        "database_mode": f"{database_mode:04o}",
    }


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
    raw_path = os.fspath(path)
    if raw_path == ":memory:":
        if normalized_mode != "rwc":
            raise ValueError("SQLite :memory: truth databases require mode='rwc'")
        database: str | Path = "file::memory:?mode=rwc"
    else:
        expanded_path = Path(path).expanduser()
        db_path = Path(os.path.abspath(os.fspath(expanded_path)))
        if normalized_mode in {"rw", "rwc"}:
            _harden_mutable_truth_path(
                db_path,
                create=normalized_mode == "rwc",
            )
        elif not db_path.is_file():
            raise sqlite3.OperationalError(
                f"SQLite truth database does not exist: {db_path}"
            )

        # SQLite's Windows URI parser rejects otherwise-valid filenames containing
        # URI metacharacters even when they are percent-encoded. A plain filesystem
        # path has no query-string surface; mode semantics are already enforced by
        # the existence/hardening checks above and query_only below.
        if os.name == "nt" and any(
            character in str(db_path) for character in "?#%"
        ):
            kwargs["uri"] = False
            database = db_path
        else:
            database = f"{db_path.as_uri()}?mode={normalized_mode}"
    conn = sqlite3.connect(database, **kwargs)
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
    "TRUTH_DATABASE_MODE",
    "TRUTH_DIRECTORY_MODE",
    "TruthDatabaseConnectionError",
    "TruthDatabaseMode",
    "connect_truth_database",
    "require_foreign_keys",
    "require_query_only",
    "truth_storage_permissions",
]
