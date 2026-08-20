"""Verified SQLite online backup/health boundary for activation and canary paths.

Ordinary startup and bounded reconciliation must not import or call this module.
Protection stays fail-closed: symlink/non-file truth, unhealthy source/backup,
preexisting destination assets, path-identical source/destination, or logical
mismatch aborts. Only partial artifacts created by the current call may be
removed; cleanup failure is reported explicitly.
"""

from __future__ import annotations

import errno
import hashlib
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, NoReturn

from .maintenance_lease import ACTIVATION_GUARD_TRIGGER_PREFIX
from .truth_connection import connect_truth_database


_FileIdentity = tuple[int, int]
_UNLINK_OPEN_DESCRIPTOR_SAFE = os.name != "nt"


class SqliteBackupError(RuntimeError):
    """Raised when a verified SQLite online backup cannot be produced safely."""


def _as_path(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _sidecar_paths(path: Path) -> tuple[Path, Path, Path]:
    return (
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{path}-journal"),
    )


def _stat_identity(stat_result: os.stat_result) -> _FileIdentity:
    return (int(stat_result.st_dev), int(stat_result.st_ino))


def _path_identity(path: Path) -> _FileIdentity | None:
    try:
        return _stat_identity(path.stat(follow_symlinks=False))
    except FileNotFoundError:
        return None


def _remove_owned_sqlite_artifacts(
    path: Path,
    owned_identity: _FileIdentity | None,
) -> list[str]:
    """Remove only the exact database file created by this attempt.

    SQLite sidecars are never inferred to be ours from their path alone. If the
    base identity changed, the whole namespace is left untouched. Unexpected
    sidecars are preserved and reported so cleanup cannot erase external data.
    """

    failures: list[str] = []
    current_identity = _path_identity(path)
    if current_identity is not None:
        if owned_identity is None or current_identity != owned_identity:
            failures.append(f"{path}: ownership changed; refusing cleanup")
            return failures
        try:
            path.unlink()
        except OSError as exc:
            failures.append(f"{path}: {type(exc).__name__}: {exc}")

    for sidecar in _sidecar_paths(path):
        if sidecar.exists() or sidecar.is_symlink():
            failures.append(f"{sidecar}: unowned sidecar preserved")
    return failures


def _close_descriptor(
    descriptor: int,
    expected_identity: _FileIdentity | None,
) -> tuple[bool, list[str]]:
    """Close one descriptor, retrying only while it is proven to remain open."""

    last_error: OSError | None = None
    for _attempt in range(3):
        try:
            os.close(descriptor)
            return True, []
        except OSError as exc:
            last_error = exc
            try:
                current_identity = _stat_identity(os.fstat(descriptor))
            except OSError as state_error:
                if state_error.errno == errno.EBADF:
                    return True, []
            else:
                if (
                    expected_identity is None
                    or current_identity != expected_identity
                ):
                    return True, [
                        f"descriptor {descriptor} identity changed after close error; "
                        "refusing retry to avoid closing a reused descriptor"
                    ]
    assert last_error is not None
    return False, [
        f"descriptor {descriptor} remained open after close retries: "
        f"{type(last_error).__name__}: {last_error}"
    ]


def _close_sqlite_connection(
    connection: sqlite3.Connection,
    *,
    label: str,
) -> list[str]:
    """Close an SQLite connection without short-circuiting later cleanup."""

    last_error: Exception | None = None
    for _attempt in range(3):
        try:
            connection.close()
            return []
        except Exception as exc:  # noqa: BLE001 - cleanup must continue
            last_error = exc
    assert last_error is not None
    return [
        f"{label} connection close failed after retries: "
        f"{type(last_error).__name__}: {last_error}"
    ]


def _release_owned_sqlite_artifacts(
    path: Path,
    owned_identity: _FileIdentity | None,
    owned_descriptor: int | None,
) -> tuple[bool, list[str]]:
    """Release one reservation without opening an inode-reuse cleanup race.

    POSIX can unlink an open inode, so cleanup keeps the descriptor pinned until
    after the path identity decision. Windows must close the reservation first
    because an open descriptor prevents unlinking the staging file there.
    """

    failures: list[str] = []
    descriptor_released = True
    if owned_descriptor is not None and _UNLINK_OPEN_DESCRIPTOR_SAFE:
        try:
            failures.extend(_remove_owned_sqlite_artifacts(path, owned_identity))
        finally:
            descriptor_released, close_failures = _close_descriptor(
                owned_descriptor,
                owned_identity,
            )
            failures.extend(close_failures)
        return descriptor_released, failures

    if owned_descriptor is not None:
        descriptor_released, close_failures = _close_descriptor(
            owned_descriptor,
            owned_identity,
        )
        failures.extend(close_failures)
        if not descriptor_released:
            return False, failures
    failures.extend(_remove_owned_sqlite_artifacts(path, owned_identity))
    return descriptor_released, failures


def _raise_with_partial_cleanup(
    primary: Exception | str,
    *,
    destination: Path,
    created: bool,
    owned_identity: _FileIdentity | None,
    owned_descriptor: int | None,
    prior_cleanup_failures: list[str] | None = None,
) -> NoReturn:
    """Best-effort cleanup of call-owned artifacts; never hide cleanup failure."""

    cleanup_failures = list(prior_cleanup_failures or [])
    if created:
        _descriptor_released, release_failures = _release_owned_sqlite_artifacts(
            destination,
            owned_identity,
            owned_descriptor,
        )
        cleanup_failures.extend(release_failures)
    primary_message = str(primary)
    if cleanup_failures:
        detail = "; ".join(cleanup_failures)
        raise SqliteBackupError(
            f"{primary_message}; partial cleanup failure: {detail}"
        ) from (primary if isinstance(primary, Exception) else None)
    if isinstance(primary, SqliteBackupError):
        raise primary
    if isinstance(primary, Exception):
        raise SqliteBackupError(primary_message) from primary
    raise SqliteBackupError(primary_message)


def _reserve_staging_path(destination: Path) -> tuple[Path, int, _FileIdentity]:
    """Create and hold a private same-directory staging inode for one attempt."""

    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOINHERIT", 0)
    )
    for _attempt in range(4):
        staging = destination.with_name(
            f"{destination.name}.scope-recall-stage-{uuid.uuid4().hex}.tmp"
        )
        try:
            descriptor = os.open(staging, flags, 0o600)
        except FileExistsError:
            continue
        try:
            identity = _stat_identity(os.fstat(descriptor))
        except OSError:
            os.close(descriptor)
            staging.unlink(missing_ok=True)
            raise
        return staging, descriptor, identity
    raise SqliteBackupError("unable to reserve a unique SQLite backup staging path")


def _publish_staged_backup(
    staging: Path,
    destination: Path,
    staging_identity: _FileIdentity,
    staging_descriptor: int,
) -> None:
    """Publish a verified staging inode without overwriting any public asset."""

    try:
        descriptor_identity = _stat_identity(os.fstat(staging_descriptor))
    except OSError as exc:
        raise SqliteBackupError(
            f"SQLite backup staging reservation handle is unavailable: {exc}"
        ) from exc
    if descriptor_identity != staging_identity:
        raise SqliteBackupError(
            "SQLite backup staging reservation handle identity changed"
        )
    if _path_identity(staging) != staging_identity:
        raise SqliteBackupError(
            "SQLite backup staging ownership changed before publish"
        )
    if destination.exists() or destination.is_symlink():
        raise SqliteBackupError(
            "refusing to overwrite destination SQLite asset created concurrently: "
            f"{destination}"
        )
    for sidecar in _sidecar_paths(destination):
        if sidecar.exists() or sidecar.is_symlink():
            raise SqliteBackupError(
                "refusing SQLite backup publish while a destination sidecar was "
                f"created concurrently: {sidecar}"
            )
    try:
        os.link(staging, destination)
    except FileExistsError as exc:
        raise SqliteBackupError(
            "refusing to overwrite destination SQLite asset created concurrently: "
            f"{destination}"
        ) from exc
    except OSError as exc:
        raise SqliteBackupError(
            f"unable to atomically publish SQLite backup: {exc}"
        ) from exc

    if _path_identity(destination) != staging_identity:
        raise SqliteBackupError(
            "published SQLite backup identity does not match verified staging inode"
        )
    for sidecar in _sidecar_paths(destination):
        if sidecar.exists() or sidecar.is_symlink():
            raise SqliteBackupError(
                "destination sidecar appeared during SQLite backup publish; "
                f"published file retained for manual inspection: {sidecar}"
            )


def inspect_sqlite_health(path: str | Path) -> dict[str, Any]:
    """Return structured SQLite health: quick_check, integrity_check, FK violations.

    ``ok`` is true only when quick_check is healthy, integrity_check(1) is healthy,
    and foreign_key_check reports zero violations.
    """

    db_path = _as_path(path)
    base = {
        "path": str(db_path),
        "ok": False,
        "quick_check": "unknown",
        "integrity_check": "unknown",
        "foreign_key_violation_present": False,
        "error": "",
    }
    if db_path.is_symlink():
        return {
            **base,
            "quick_check": "symlink_refused",
            "integrity_check": "symlink_refused",
            "error": f"refusing symlinked SQLite path: {db_path}",
        }
    if not db_path.is_file():
        return {
            **base,
            "quick_check": "not_a_file",
            "integrity_check": "not_a_file",
            "error": f"SQLite path is not a file: {db_path}",
        }
    try:
        connection = connect_truth_database(db_path, mode="ro", timeout=30)
    except Exception as exc:  # noqa: BLE001 - health must stay structured
        return {
            **base,
            "quick_check": "open_failed",
            "integrity_check": "open_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    try:
        quick_row = connection.execute("PRAGMA quick_check").fetchone()
        quick_check = "unknown" if quick_row is None else str(quick_row[0])
        integrity_row = connection.execute("PRAGMA integrity_check(1)").fetchone()
        integrity_check = (
            "unknown" if integrity_row is None else str(integrity_row[0])
        )
        fk_row = connection.execute("PRAGMA foreign_key_check").fetchone()
        foreign_key_violation_present = fk_row is not None

        quick_ok = quick_check.lower() == "ok"
        integrity_ok = integrity_check.lower() == "ok"
        ok = quick_ok and integrity_ok and not foreign_key_violation_present

        errors: list[str] = []
        if not quick_ok:
            errors.append(f"quick_check failed: {quick_check}")
        if not integrity_ok:
            errors.append(f"integrity_check failed: {integrity_check}")
        if foreign_key_violation_present:
            errors.append("foreign_key_check reported at least one violation")
        return {
            "path": str(db_path),
            "ok": ok,
            "quick_check": quick_check,
            "integrity_check": integrity_check,
            "foreign_key_violation_present": foreign_key_violation_present,
            "error": "" if ok else "; ".join(errors),
        }
    except Exception as exc:  # noqa: BLE001 - health must stay structured
        return {
            **base,
            "quick_check": "check_failed",
            "integrity_check": "check_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        connection.close()


def _activation_guard_trigger_statements(
    connection: sqlite3.Connection,
) -> set[str]:
    """Return exact dump statements for activation-owned guard triggers.

    The reserved namespace applies to trigger names, not arbitrary SQL text.
    Exact statement matching therefore keeps ordinary triggers whose bodies
    merely mention the reserved prefix inside the logical fingerprint.
    """

    rows = connection.execute(
        "SELECT sql FROM sqlite_schema "
        "WHERE type = 'trigger' AND name GLOB ? AND sql IS NOT NULL",
        (f"{ACTIVATION_GUARD_TRIGGER_PREFIX}*",),
    ).fetchall()
    return {f"{str(row[0]).rstrip(';')};" for row in rows}


def logical_fingerprint(path: str | Path) -> str:
    """Hash schema/rows while excluding reserved temporary activation guards."""

    db_path = _as_path(path)
    if db_path.is_symlink():
        raise SqliteBackupError(f"refusing symlinked SQLite path: {db_path}")
    if not db_path.is_file():
        raise SqliteBackupError(f"SQLite path is not a file: {db_path}")
    connection = connect_truth_database(db_path, mode="ro", timeout=30)
    try:
        ignored_trigger_statements = _activation_guard_trigger_statements(connection)
        digest = hashlib.sha256()
        for line in connection.iterdump():
            if line in ignored_trigger_statements:
                continue
            digest.update(line.encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()
    finally:
        connection.close()


def _transfer_online_backup(
    source_conn: sqlite3.Connection,
    destination_conn: sqlite3.Connection,
) -> None:
    """Run SQLite's online backup API (patch seam for fault injection)."""

    source_conn.backup(destination_conn)


def _normalize_standalone_staging_journal(connection: sqlite3.Connection) -> None:
    """Force DELETE journal mode before any read-only staging reopen.

    Online backup of a WAL source copies WAL into the standalone staging file.
    Health and iterdump later reopen that path read-only, which would create
    ``-wal``/``-shm`` beside the reserved inode. Cleanup correctly treats those
    sidecars as unowned, so the published backup would fail closed. Normalize
    here on the still-open writer instead of deleting sidecars later.
    """

    row = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
    mode = "" if row is None else str(row[0]).strip().lower()
    if mode != "delete":
        raise SqliteBackupError(
            "SQLite staging backup refused DELETE journal mode "
            f"(got {mode or 'unknown'})"
        )


def verified_online_backup(
    source_path: str | Path,
    backup_path: str | Path,
) -> dict[str, Any]:
    """Create one verified SQLite online backup and return structured evidence.

    Fail closed on symlink/non-file truth, same-path source/destination,
    preexisting destination or sidecars, unhealthy source or backup, backup API
    failure, journal-mode normalize failure, or logical fingerprint mismatch.
    WAL sources are converted to DELETE on staging before health/fingerprint
    reopen so those reads cannot create unowned sidecars. Only partial
    artifacts created by this call are eligible for cleanup; cleanup failure
    is explicit.
    """

    source = _as_path(source_path)
    destination = _as_path(backup_path)

    if source == destination:
        raise SqliteBackupError(
            f"refusing SQLite backup when source and destination are the same "
            f"absolute path: {source}"
        )
    if source.is_symlink():
        raise SqliteBackupError(
            f"refusing activation against symlinked SQLite truth DB: {source}"
        )
    if not source.is_file():
        raise SqliteBackupError(f"SQLite truth path is not a file: {source}")
    if destination.is_symlink():
        raise SqliteBackupError(f"refusing symlinked SQLite backup path: {destination}")

    if destination.exists():
        raise SqliteBackupError(
            f"refusing to overwrite preexisting destination SQLite asset: {destination}"
        )
    for sidecar in _sidecar_paths(destination):
        if sidecar.exists() or sidecar.is_symlink():
            raise SqliteBackupError(
                f"refusing backup while preexisting destination sidecar exists: {sidecar}"
            )

    source_health = inspect_sqlite_health(source)
    if not source_health["ok"]:
        raise SqliteBackupError(
            f"SQLite source health check failed: {source_health.get('error') or source}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging: Path | None = None
    staging_identity: _FileIdentity | None = None
    staging_descriptor: int | None = None
    staging_cleanup_pending = False
    source_conn: sqlite3.Connection | None = None
    dest_conn: sqlite3.Connection | None = None
    try:
        staging, staging_descriptor, staging_identity = _reserve_staging_path(
            destination
        )
        staging_cleanup_pending = True
        source_conn = connect_truth_database(source, mode="ro", timeout=30)
        dest_conn = connect_truth_database(staging, mode="rw")
        if _stat_identity(os.fstat(staging_descriptor)) != staging_identity:
            raise SqliteBackupError(
                "SQLite backup staging reservation handle identity changed before transfer"
            )
        if _path_identity(staging) != staging_identity:
            raise SqliteBackupError(
                "SQLite backup staging ownership changed before transfer"
            )
        _transfer_online_backup(source_conn, dest_conn)
        dest_conn.commit()
        check = dest_conn.execute("PRAGMA quick_check").fetchone()
        quick_check = "unknown" if check is None else str(check[0])
        if quick_check.lower() != "ok":
            raise SqliteBackupError(
                f"SQLite activation backup quick_check failed: {staging}"
            )
        _normalize_standalone_staging_journal(dest_conn)

        connection_close_failures = _close_sqlite_connection(
            dest_conn,
            label="destination SQLite",
        )
        dest_conn = None
        connection_close_failures.extend(
            _close_sqlite_connection(
                source_conn,
                label="source SQLite",
            )
        )
        source_conn = None
        if connection_close_failures:
            raise SqliteBackupError(
                "SQLite backup connection close failed: "
                + "; ".join(connection_close_failures)
            )

        staging_health = inspect_sqlite_health(staging)
        if not staging_health["ok"]:
            raise SqliteBackupError(
                "SQLite staging backup health check failed: "
                f"{staging_health.get('error') or staging}"
            )
        source_fp = logical_fingerprint(source)
        staging_fp = logical_fingerprint(staging)
        if source_fp != staging_fp:
            raise SqliteBackupError(
                "SQLite staging backup logical fingerprint does not match source "
                f"(source={source_fp[:16]}, staging={staging_fp[:16]})"
            )

        _publish_staged_backup(
            staging,
            destination,
            staging_identity,
            staging_descriptor,
        )
        descriptor_released, cleanup_failures = _release_owned_sqlite_artifacts(
            staging,
            staging_identity,
            staging_descriptor,
        )
        if descriptor_released:
            staging_descriptor = None
        if not cleanup_failures:
            staging_cleanup_pending = False
        if cleanup_failures:
            raise SqliteBackupError(
                "SQLite backup published; partial cleanup failure: "
                + "; ".join(cleanup_failures)
            )

        backup_health = inspect_sqlite_health(destination)
        if not backup_health["ok"]:
            raise SqliteBackupError(
                f"SQLite published backup health check failed: "
                f"{backup_health.get('error') or destination}"
            )
        backup_fp = logical_fingerprint(destination)
        if source_fp != backup_fp:
            raise SqliteBackupError(
                "SQLite published backup logical fingerprint does not match source "
                f"(source={source_fp[:16]}, backup={backup_fp[:16]})"
            )
    except Exception as exc:
        connection_close_failures: list[str] = []
        if dest_conn is not None:
            connection_close_failures.extend(
                _close_sqlite_connection(
                    dest_conn,
                    label="destination SQLite",
                )
            )
            dest_conn = None
        if source_conn is not None:
            connection_close_failures.extend(
                _close_sqlite_connection(
                    source_conn,
                    label="source SQLite",
                )
            )
            source_conn = None
        cleanup_created = (
            staging_cleanup_pending
            and staging is not None
            and staging_identity is not None
        )
        cleanup_descriptor = staging_descriptor
        staging_cleanup_pending = False
        staging_descriptor = None
        primary = (
            exc
            if isinstance(exc, SqliteBackupError)
            else SqliteBackupError(f"SQLite online backup failed: {exc}")
        )
        _raise_with_partial_cleanup(
            primary,
            destination=staging or destination,
            created=cleanup_created,
            owned_identity=staging_identity,
            owned_descriptor=cleanup_descriptor,
            prior_cleanup_failures=connection_close_failures,
        )
    finally:
        if dest_conn is not None:
            _close_sqlite_connection(dest_conn, label="destination SQLite")
        if source_conn is not None:
            _close_sqlite_connection(source_conn, label="source SQLite")
        if (
            staging_cleanup_pending
            and staging is not None
            and staging_identity is not None
        ):
            _release_owned_sqlite_artifacts(
                staging,
                staging_identity,
                staging_descriptor,
            )

    return {
        "source_path": str(source),
        "backup_path": str(destination),
        "source_health": source_health,
        "backup_health": backup_health,
        "source_logical_fingerprint": source_fp,
        "backup_logical_fingerprint": backup_fp,
        "logical_fingerprint": source_fp,
        "logical_equivalent": True,
    }


__all__ = [
    "SqliteBackupError",
    "inspect_sqlite_health",
    "logical_fingerprint",
    "verified_online_backup",
]
