"""Fail-closed SQLite truth-database connection boundary.

SQLite foreign-key enforcement is connection-local and defaults to disabled.
Every Scope Recall connection that can read or mutate the authoritative memory
SQLite database must cross this module before schema, transactions, migrations,
or activation authorizers are installed. Writable FILE-backed connections
classified as live truth acquire a connection-level truth writer lease on the
storage directory before the SQLite pager opens. POSIX writable opens
raw-open and fchmod-harden each database identity at most once per process
so a later descriptor close cannot cancel same-process SQLite advisory
locks. Identity replacement or permission drift after that cached event
fails closed instead of raw-opening while this process may hold locks.
An existing process-wide hardening marker with an unknown version or
schema also fails closed and requires a process restart; it is not
repaired or replaced while this process may already hold SQLite locks.

Live truth is the
ASCII-case-insensitive ``memory.sqlite3`` basename, plus any existing
same-directory filesystem alias or hardlink that ``os.path.samefile``
identifies with sibling ``memory.sqlite3``. ``:memory:``, read-only
connections, and backup/staging/vector filenames that are not same-file
aliases do not take that lease. Vector companion databases are not truth
stores and intentionally use their own connection boundary.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
import threading
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

if __package__:
    from .writer_lease import TruthWriterBusyError, TruthWriterLease
else:
    from writer_lease import TruthWriterBusyError, TruthWriterLease


TruthDatabaseMode = Literal["ro", "rw", "rwc"]
_DEFAULT_ISOLATION_LEVEL = object()


class TruthDatabaseConnectionError(RuntimeError):
    """A SQLite truth connection could not satisfy mandatory invariants."""


class TruthDatabaseCleanupError(TruthDatabaseConnectionError):
    """Setup failed and the leased connection/lease still needs cleanup.

    The public message, ``str``, and ``repr`` stay generic so a path or live
    connection cannot leak through diagnostics. The bound cleanup callable is
    private. ``retry_cleanup`` is serialized so two callers cannot
    double-release; a successful retry is idempotent, and a failed retry
    keeps the callable for another attempt.
    """

    def __init__(self, *, cleanup: Callable[[], None]) -> None:
        super().__init__(
            "truth database cleanup is pending after a connection setup failure"
        )
        self._cleanup: Callable[[], None] | None = cleanup
        self._cleanup_lock = threading.Lock()
        self._cleanup_pending = True

    def __repr__(self) -> str:
        return f"{type(self).__name__}(cleanup_pending={self.cleanup_pending!r})"

    @property
    def cleanup_pending(self) -> bool:
        return self._cleanup_pending

    def retry_cleanup(self) -> None:
        """Retry the retained close/release. Success clears the owner."""

        with self._cleanup_lock:
            if not self._cleanup_pending:
                return
            cleanup = self._cleanup
            if cleanup is None:
                self._cleanup_pending = False
                return
            cleanup()
            self._cleanup = None
            self._cleanup_pending = False


TRUTH_DIRECTORY_MODE = 0o700
TRUTH_DATABASE_MODE = 0o600
CANONICAL_LIVE_TRUTH_FILENAME = "memory.sqlite3"


def is_live_truth_database_path(path: str | Path) -> bool:
    """Return whether *path* names the live SQLite truth database.

    ``connect_truth_database`` uses this classifier before acquiring a
    connection-level writer lease. The rule is conservative: extra leasing
    of a distinct case-sensitive ``MEMORY.SQLITE3`` file is acceptable;
    missing authority on a case or filesystem alias is not.

    A path is live truth when:

    - it is not the SQLite ``:memory:`` URI
    - its basename is ASCII-case-insensitive ``memory.sqlite3`` (via
      ``str.casefold``), even if the file is missing
    - or the existing file is ``os.path.samefile`` with the sibling
      canonical ``memory.sqlite3``

    ``OSError`` from ``samefile`` (missing path, permission, or an
    unsupported comparison) fails safe to "not an alias" and never
    overrides a canonical basename match. This function does not resolve
    symlinks and does not replace symlink/path hardening in
    ``connect_truth_database``.
    """

    raw_path = os.fspath(path)
    if raw_path == ":memory:":
        return False
    candidate = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
    if candidate.name.casefold() == CANONICAL_LIVE_TRUTH_FILENAME.casefold():
        return True
    sibling = candidate.parent / CANONICAL_LIVE_TRUTH_FILENAME
    try:
        return bool(os.path.samefile(os.fspath(candidate), os.fspath(sibling)))
    except OSError:
        return False


def _descriptor_permissions_supported() -> bool:
    """Return whether POSIX descriptor chmod is meaningful on this platform."""

    return os.name != "nt" and callable(getattr(os, "fchmod", None))


_POSIX_HARDENING_STATE_NAME = "_scope_recall_posix_truth_hardening"
_POSIX_HARDENING_STATE_VERSION = 1
_THREAD_LOCK_TYPE = type(threading.Lock())


def _new_posix_hardening_holder() -> types.ModuleType:
    """Build a versioned process-wide holder using only alias-neutral builtins."""

    holder = types.ModuleType(_POSIX_HARDENING_STATE_NAME)
    setattr(holder, "version", _POSIX_HARDENING_STATE_VERSION)
    setattr(holder, "lock", threading.Lock())
    setattr(holder, "pid", os.getpid())
    setattr(holder, "by_path", {})
    setattr(holder, "by_identity", {})
    return holder


def _posix_hardening_holder_usable(holder: object) -> bool:
    """Return whether *holder* has the shared schema without class identity."""

    return (
        getattr(holder, "version", None) == _POSIX_HARDENING_STATE_VERSION
        and isinstance(getattr(holder, "lock", None), _THREAD_LOCK_TYPE)
        and isinstance(getattr(holder, "by_path", None), dict)
        and isinstance(getattr(holder, "by_identity", None), dict)
        and isinstance(getattr(holder, "pid", None), int)
    )


def _posix_hardening_state() -> types.ModuleType:
    """Return the process-wide POSIX hardening holder shared across import aliases.

    A missing marker is created with ``sys.modules.setdefault`` so concurrent
    import aliases still share one lock. A valid current-schema marker is
    reused. An existing marker with an unknown version or schema fails
    closed: this process may already hold SQLite locks, so the holder is
    not repaired, cleared, or replaced.
    """

    holder = sys.modules.get(_POSIX_HARDENING_STATE_NAME)
    if _posix_hardening_holder_usable(holder) and isinstance(holder, types.ModuleType):
        return holder
    if _POSIX_HARDENING_STATE_NAME not in sys.modules:
        holder = sys.modules.setdefault(
            _POSIX_HARDENING_STATE_NAME, _new_posix_hardening_holder()
        )
    else:
        # Re-read after a missed first get so a racing alias that published a
        # valid holder is reused instead of being treated as incompatible.
        holder = sys.modules.get(_POSIX_HARDENING_STATE_NAME)
    if _posix_hardening_holder_usable(holder) and isinstance(holder, types.ModuleType):
        return holder
    raise TruthDatabaseConnectionError(
        "SQLite truth storage hardening state is incompatible; restart the process"
    )


def _reset_posix_hardening_cache_for_tests() -> None:
    """Clear process-local hardening records so tests cannot leak identity."""

    holder = sys.modules.get(_POSIX_HARDENING_STATE_NAME)
    lock = getattr(holder, "lock", None)
    by_path = getattr(holder, "by_path", None)
    by_identity = getattr(holder, "by_identity", None)
    if not isinstance(lock, _THREAD_LOCK_TYPE):
        return
    if not isinstance(by_path, dict) or not isinstance(by_identity, dict):
        return
    with lock:
        by_path.clear()
        by_identity.clear()
        setattr(holder, "pid", os.getpid())


def _after_fork_reset_hardening() -> None:
    """Drop inherited records and the inherited lock after ``fork``."""

    holder = sys.modules.get(_POSIX_HARDENING_STATE_NAME)
    if holder is None:
        return
    setattr(holder, "lock", threading.Lock())
    setattr(holder, "pid", os.getpid())
    setattr(holder, "by_path", {})
    setattr(holder, "by_identity", {})
    setattr(holder, "version", _POSIX_HARDENING_STATE_VERSION)


def _invalidate_inherited_hardening_state(state: types.ModuleType) -> None:
    """Forget a parent's records when this process is a fork child.

    Caller must hold ``state.lock``. ``register_at_fork`` already replaces the
    inherited lock; this PID check covers import aliases and platforms without
    that hook.
    """

    if getattr(state, "pid", None) == os.getpid():
        return
    by_path = getattr(state, "by_path", None)
    by_identity = getattr(state, "by_identity", None)
    if isinstance(by_path, dict):
        by_path.clear()
    if isinstance(by_identity, dict):
        by_identity.clear()
    setattr(state, "pid", os.getpid())


def _path_hardening_key(path: Path) -> str:
    return os.path.abspath(os.fspath(path))


def _as_hardening_record(value: object) -> tuple[int, int, int] | None:
    """Accept only an immutable ``(dev, ino, mode)`` tuple."""

    if not isinstance(value, tuple) or len(value) != 3:
        return None
    dev, ino, mode = value
    if not isinstance(dev, int) or not isinstance(ino, int) or not isinstance(mode, int):
        return None
    return (dev, ino, mode)


def _lstat_hardening_record(path: Path) -> tuple[int, int, int] | None:
    try:
        st = os.lstat(os.fspath(path))
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        raise TruthDatabaseConnectionError(
            "SQLite truth storage cannot use symlink paths"
        )
    return (int(st.st_dev), int(st.st_ino), int(stat.S_IMODE(st.st_mode)))


def _harden_truth_directory_descriptor(
    parent: Path, directory_flags: int, fchmod: Callable[[int, int], None]
) -> None:
    """Re-apply owner-only directory mode through a no-follow descriptor.

    A directory descriptor is a different inode than the live SQLite file, so
    closing it cannot cancel database advisory locks. Repeat this on every
    writable open so directory permission drift is still corrected.
    """

    directory_fd = os.open(parent, directory_flags)
    try:
        if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
            raise TruthDatabaseConnectionError(
                "SQLite truth storage parent is not a directory"
            )
        fchmod(directory_fd, TRUTH_DIRECTORY_MODE)
    finally:
        os.close(directory_fd)


def _apply_database_descriptor_hardening(
    path: Path,
    file_flags: int,
    fchmod: Callable[[int, int], None],
    state: types.ModuleType,
    path_key: str,
) -> None:
    """Raw-open, fchmod, and record one database identity. Caller holds the lock."""

    database_fd = os.open(path, file_flags, TRUTH_DATABASE_MODE)
    try:
        st = os.fstat(database_fd)
        if not stat.S_ISREG(st.st_mode):
            raise TruthDatabaseConnectionError(
                "SQLite truth storage is not a regular file"
            )
        fchmod(database_fd, TRUTH_DATABASE_MODE)
        st = os.fstat(database_fd)
        record = (
            int(st.st_dev),
            int(st.st_ino),
            int(stat.S_IMODE(st.st_mode)),
        )
        state.by_path[path_key] = record
        state.by_identity[record[:2]] = record
    finally:
        os.close(database_fd)


def _harden_truth_database_descriptor_once(
    path: Path, file_flags: int, fchmod: Callable[[int, int], None]
) -> None:
    """Harden the live DB at most once per process identity, or fail closed."""

    path_key = _path_hardening_key(path)
    state = _posix_hardening_state()
    with state.lock:
        _invalidate_inherited_hardening_state(state)
        current = _lstat_hardening_record(path)
        if path_key in state.by_path:
            cached_path = _as_hardening_record(state.by_path[path_key])
            if cached_path is None or current is None or cached_path != current:
                raise TruthDatabaseConnectionError(
                    "SQLite truth storage identity or permissions changed after hardening"
                )
            return
        if current is not None:
            identity_key = current[:2]
            if identity_key in state.by_identity:
                cached_identity = _as_hardening_record(state.by_identity[identity_key])
                if cached_identity is None or cached_identity != current:
                    raise TruthDatabaseConnectionError(
                        "SQLite truth storage identity or permissions changed after hardening"
                    )
                state.by_path[path_key] = cached_identity
                return
        _apply_database_descriptor_hardening(
            path, file_flags, fchmod, state, path_key
        )


_register_at_fork = getattr(os, "register_at_fork", None)
if callable(_register_at_fork):
    _register_at_fork(after_in_child=_after_fork_reset_hardening)


def _harden_mutable_truth_path(path: Path, *, create: bool) -> None:
    """Create or harden mutable truth storage without following final symlinks.

    Windows uses the containing profile's inherited ACL boundary. POSIX systems
    harden both the directory and database through opened descriptors so a path
    replacement cannot redirect the chmod operation.

    Closing a raw descriptor for a live SQLite file can cancel advisory locks
    held by this process. The database file is therefore raw-opened and
    fchmod-hardened at most once per process identity/path epoch. A later
    writable open of the same inode skips ``os.open``. If the path now names a
    different file or the recorded mode drifted, this function fails closed
    rather than raw-opening while SQLite locks may exist. An incompatible
    process-wide hardening marker fails closed the same way and requires a
    process restart.
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
        _harden_truth_directory_descriptor(parent, directory_flags, fchmod)
        _harden_truth_database_descriptor_once(path, file_flags, fchmod)
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


class _LeasedTruthConnection(sqlite3.Connection):
    """SQLite connection that owns a connection-level truth writer lease.

    The lease is acquired before this pager opens. ``close()`` runs SQLite
    close first and releases the lease only after that succeeds, so a leaked
    helper-local connection still holds process authority. If SQLite close
    raises, the lease stays attached. If lease release raises while still
    acquired, the lease stays attached for a close retry; if authority was
    actually released, the lease is detached and the error still surfaces.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._truth_writer_lease: TruthWriterLease | None = None

    def close(self) -> None:
        try:
            super().close()
        except BaseException:
            raise
        lease = self._truth_writer_lease
        if lease is None:
            return
        try:
            lease.release()
        except BaseException:
            if lease.acquired:
                raise
            self._truth_writer_lease = None
            raise
        self._truth_writer_lease = None


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
    create it. A FILE-backed writable open classified as live truth by
    :func:`is_live_truth_database_path` acquires a connection-level
    ``TruthWriterLease`` on the parent directory before ``sqlite3.connect``
    opens the pager. ``:memory:``, read-only mode, and backup/staging/vector
    names that are not same-file aliases do not take that lease. The
    returned connection remains a ``sqlite3.Connection`` ready for callers
    to install the activation authorizer and configure WAL/synchronous
    policy.
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
    lease: TruthWriterLease | None = None
    conn: sqlite3.Connection | None = None
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
        if normalized_mode in {"rw", "rwc"} and is_live_truth_database_path(db_path):
            lease = TruthWriterLease(db_path.parent, role="truth_connection")
            result = lease.acquire()
            if result.get("status") != "acquired":
                owner = result.get("owner")
                raise TruthWriterBusyError(
                    role="truth_connection",
                    scope=str(result.get("scope") or ""),
                    owner=owner if isinstance(owner, dict) else {},
                )
    try:
        if lease is not None:
            kwargs["factory"] = _LeasedTruthConnection
        conn = sqlite3.connect(database, **kwargs)
        if lease is not None:
            if not isinstance(conn, _LeasedTruthConnection):
                raise TruthDatabaseConnectionError(
                    "SQLite truth connection factory did not bind the writer lease"
                )
            conn._truth_writer_lease = lease
            lease = None
        require_foreign_keys(conn)
        if normalized_mode == "ro":
            require_query_only(conn)
        conn.row_factory = row_factory
        return conn
    except BaseException as original:
        if conn is not None:
            pending_conn = conn
            try:
                pending_conn.close()
            except BaseException:
                # Look up close at retry time so a later injection removal can
                # succeed. Never treat a failed close as a released lease.
                def _retry_close() -> None:
                    pending_conn.close()

                raise TruthDatabaseCleanupError(cleanup=_retry_close) from original
        elif lease is not None:
            pending_lease = lease
            try:
                pending_lease.release()
            except BaseException:
                def _retry_release() -> None:
                    pending_lease.release()

                raise TruthDatabaseCleanupError(cleanup=_retry_release) from original
        raise


SQLITE_HEADER_PREFIX = b"SQLite format 3\x00"


def probe_truth_database_connection(conn: sqlite3.Connection) -> dict[str, Any]:
    """Probe the live SQLite pager without raw-opening its database file.

    On POSIX, closing any raw file descriptor for a SQLite database can cancel
    advisory locks held by the same process. Live health checks therefore stay
    on the provider-owned pager connection. ``PRAGMA schema_version`` is a cheap
    page-1 read that detects an unreadable or non-SQLite truth store without
    disturbing lock ownership.
    """

    try:
        row = conn.execute("PRAGMA schema_version").fetchone()
    except sqlite3.DatabaseError:
        return {
            "ok": False,
            "status": "corrupt_or_unreadable",
            "error": "SQLite truth database is corrupt or unreadable",
        }
    if row is None:
        return {
            "ok": False,
            "status": "unreadable",
            "error": "SQLite truth database schema header is unreadable",
        }
    return {"ok": True, "status": "ok"}


def probe_truth_database_header(
    path: str | Path,
    *,
    connections_quiesced: bool = False,
) -> dict[str, Any]:
    """Read an offline SQLite header only after all connections are quiesced.

    Raw open/close probes are unsafe while the same process owns SQLite pager
    locks on POSIX. Callers handling a live provider must use
    :func:`probe_truth_database_connection` instead. The explicit gate prevents
    future maintenance code from accidentally reintroducing that lock-canceling
    pattern.
    """

    if not connections_quiesced:
        return {
            "ok": False,
            "status": "unsafe_live_probe_refused",
            "error": "raw SQLite header probes require quiesced connections",
        }
    raw_path = os.fspath(path)
    if raw_path == ":memory:":
        return {"ok": True, "status": "memory"}
    expanded = Path(path).expanduser()
    db_path = Path(os.path.abspath(os.fspath(expanded)))
    if not db_path.is_file():
        return {
            "ok": False,
            "status": "missing",
            "error": "SQLite truth database file is missing",
        }
    try:
        with db_path.open("rb") as handle:
            header = handle.read(len(SQLITE_HEADER_PREFIX))
    except OSError:
        return {
            "ok": False,
            "status": "unreadable",
            "error": "SQLite truth database header is unreadable",
        }
    if header != SQLITE_HEADER_PREFIX:
        return {
            "ok": False,
            "status": "corrupt_header",
            "error": "SQLite truth database header is corrupt",
        }
    return {"ok": True, "status": "ok"}


__all__ = [
    "CANONICAL_LIVE_TRUTH_FILENAME",
    "SQLITE_HEADER_PREFIX",
    "TRUTH_DATABASE_MODE",
    "TRUTH_DIRECTORY_MODE",
    "TruthDatabaseCleanupError",
    "TruthDatabaseConnectionError",
    "TruthDatabaseMode",
    "connect_truth_database",
    "is_live_truth_database_path",
    "probe_truth_database_connection",
    "probe_truth_database_header",
    "require_foreign_keys",
    "require_query_only",
    "truth_storage_permissions",
]
