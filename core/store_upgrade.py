"""Snapshot and atomically migrate one truth store under its writer lease."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import closing, contextmanager, suppress
from pathlib import Path
from typing import Any

from ..contracts import ContractError
from .backup_primitives import (
    BackupError,
    atomic_json,
    backup_deadline,
    backup_sqlite,
    check_backup_deadline,
    safe_path,
    seal_backup_manifest,
    sha256,
    sqlite_busy,
)
from .file_lock import advisory_file_lock

UPGRADE_STATE = "store-upgrade.json"
UPGRADE_LOCK = "store-upgrade.lock"


def upgrade_in_progress(directory: Path) -> bool:
    """Check the coordinator lock without waiting or changing the truth database."""
    if not (directory / UPGRADE_LOCK).exists():
        return False
    try:
        with advisory_file_lock(directory / UPGRADE_LOCK, timeout_seconds=0):
            return False
    except TimeoutError:
        # Initialization also uses this lock for current stores. Only a running
        # migration record makes its contention a schema capability gap.
        try:
            state = json.loads((directory / UPGRADE_STATE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(state, dict) and state.get("status") == "running"


def read_upgrade_state(directory: Path) -> dict[str, Any]:
    """Read content-free progress; an abandoned running record means interruption."""
    try:
        value = json.loads((directory / UPGRADE_STATE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {"status": "unreadable", "reason": "upgrade_state_unreadable"}
    if not isinstance(value, dict):
        return {"status": "unreadable", "reason": "upgrade_state_unreadable"}
    if value.get("status") == "running" and not upgrade_in_progress(directory):
        value.update(status="interrupted", reason="process_interrupted")
    return value


def _failure_reason(exc: BaseException) -> str:
    if isinstance(exc, ContractError):
        return exc.field or exc.code
    if isinstance(exc, sqlite3.Error):
        return str(getattr(exc, "sqlite_errorname", None) or type(exc).__name__)
    if isinstance(exc, OSError):
        return f"{type(exc).__name__}:errno={exc.errno}"
    return type(exc).__name__


def _snapshot_markers(path: Path) -> tuple[int, int | None]:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        header = int(db.execute("PRAGMA user_version").fetchone()[0])
        row = db.execute("SELECT schema_version FROM instance_meta WHERE singleton=1").fetchone()
        return header, int(row[0]) if row else None


def _automatic_snapshots(directory: Path, role: str | None = None) -> list[Path]:
    """Select only coordinator-owned outputs, never explicit backup directories."""
    snapshots = []
    for manifest in directory.glob("memory-*.json"):
        parts = manifest.stem.split("-")
        if len(parts) != 3 or not parts[1].isdigit() or len(parts[2]) != 32:
            continue
        try:
            uuid.UUID(hex=parts[2])
            value = json.loads(safe_path(manifest).read_text(encoding="utf-8"))
        except (OSError, ValueError, BackupError):
            continue
        if (
            isinstance(value, dict)
            and value.get("automatic_upgrade") is True
            and (role is None or value.get("upgrade_role", "rollback") == role)
        ):
            snapshots.append(manifest.with_suffix(".sqlite3"))
    return snapshots


def _verified_snapshot(path: Path) -> bool:
    try:
        manifest = json.loads(safe_path(path.with_suffix(".json")).read_text(encoding="utf-8"))
        return (
            isinstance(manifest, dict)
            and manifest.get("quick_check") == "ok"
            and sha256(safe_path(path)) == manifest.get("backup_sha256")
        )
    except (FileNotFoundError, ValueError):
        return False


def _remove_snapshot(path: Path) -> None:
    """Unlink only the named attempt's outputs after all its SQLite handles close."""
    for candidate in (path, path.with_suffix(".json"), Path(str(path) + "-journal")):
        safe_path(candidate).unlink(missing_ok=True)


def _rotate_snapshots(directory: Path, role: str, keep: Path) -> None:
    """Rotate within one role; an older rollback never replaces fresh pre-upgrade truth."""
    for path in _automatic_snapshots(directory, role):
        if path != keep:
            _remove_snapshot(path)


def _promote_snapshot(path: Path) -> None:
    """Publish the committed upgrade's rollback role before retiring either older role."""
    manifest = path.with_suffix(".json")
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise BackupError("invalid snapshot manifest")
    atomic_json(manifest, seal_backup_manifest({**metadata, "upgrade_role": "rollback"}))
    _rotate_snapshots(path.parent, "rollback", path)
    _rotate_snapshots(path.parent, "attempt", path)


def recover_upgrade_snapshot(directory: Path, *, current_schema: int | None = None) -> None:
    """Clean an interrupted automatic attempt under exclusive upgrade admission."""
    state = read_upgrade_state(directory)
    if state.get("automatic_snapshot") is not True:
        return
    snapshots = directory / "upgrade-snapshots"
    path = Path(str(state.get("backup", "")))
    if path.parent != snapshots or not path.name.startswith("memory-") or path.suffix != ".sqlite3":
        return
    token = path.stem.rpartition("-")[2]
    if len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
        return
    committed = state.get("status") == "complete" or (
        current_schema is not None and current_schema == state.get("schema_target")
    )
    if committed:
        if path in _automatic_snapshots(snapshots, "attempt") and _verified_snapshot(path):
            _promote_snapshot(path)
        return
    # A completed attempt is the freshest pre-upgrade truth, even if a much
    # older successful upgrade left a rollback snapshot of its own.
    complete = state.get("snapshot_verified") or _verified_snapshot(path)
    if not complete:
        _remove_snapshot(path)
    elif path in _automatic_snapshots(snapshots, "attempt"):
        _rotate_snapshots(snapshots, "attempt", path)


def _lock_budget(conn: sqlite3.Connection, deadline: float | None, *, spill_wait: bool = False) -> None:
    if deadline is not None:
        milliseconds = max(0, int((deadline - time.monotonic()) * 1000))
        conn.execute(f"PRAGMA busy_timeout={min(milliseconds, 50) if spill_wait else milliseconds}")


@contextmanager
def _migration_budget(conn: sqlite3.Connection, deadline: float | None, journal: str):
    """Bound repeated cache-spill waits without changing the connection in a progress callback."""
    refreshing = False

    def before_statement(_statement):
        nonlocal refreshing
        if refreshing:
            return
        refreshing = True
        try:
            _lock_budget(conn, deadline, spill_wait=True)
            if deadline is not None and time.monotonic() >= deadline:
                conn.interrupt()
        finally:
            refreshing = False

    try:
        conn.set_progress_handler(lambda: check_backup_deadline() or 0, 1000)
        if deadline is not None and journal == "delete":
            # A spill can retry a busy reader for each row of executemany.
            # Trace refreshes each statement; the guard excludes its own PRAGMA.
            conn.set_trace_callback(before_statement)
        yield
    finally:
        conn.set_trace_callback(None)
        conn.set_progress_handler(None, 0)


def _blocked_reader(conn: sqlite3.Connection, exc: BaseException, journal: str) -> bool:
    """After rollback, distinguish a still-held rollback-journal read lock from CPU timeout."""
    if journal != "delete" or not isinstance(exc, sqlite3.OperationalError):
        return False
    if sqlite_busy(exc):
        return True
    if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_INTERRUPT:
        return False
    conn.execute("PRAGMA busy_timeout=0")
    try:
        conn.execute("BEGIN EXCLUSIVE")
        conn.rollback()
    except sqlite3.OperationalError as blocked:
        return sqlite_busy(blocked)
    return False


def _save_state(directory: Path, state: dict[str, Any]) -> None:
    """Diagnostic I/O after commit must not turn a committed migration into failure."""
    with suppress(OSError, BackupError):
        atomic_json(directory / UPGRADE_STATE, state)


def upgrade_store(
    conn: sqlite3.Connection,
    path: Path,
    *,
    target: int,
    verify: Callable[..., object],
    migrate: Callable[[sqlite3.Connection], None],
    backup_dir: Path | None = None,
    recorded_schema: int | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Snapshot committed truth, then migrate and commit the caller's live transaction.

    The caller holds the coordinator lock, writer lease and BEGIN IMMEDIATE.
    A separate reader can back up committed pages while that transaction fences
    other writers in both rollback and WAL modes. No live write precedes the
    verified snapshot; rollback restores logical truth after any migration error.
    """
    before = int(conn.execute("PRAGMA user_version").fetchone()[0])
    expected = recorded_schema if recorded_schema is not None else before
    if recorded_schema is None:
        verify(conn, expected_schema=expected)
    with backup_deadline(deadline):
        recover_upgrade_snapshot(path.parent, current_schema=before)
    directory = safe_path(backup_dir or path.parent / "upgrade-snapshots")
    snapshot = directory / f"memory-{before}-{uuid.uuid4().hex}.sqlite3"
    automatic = backup_dir is None
    state: dict[str, Any] = {
        "status": "running",
        "phase": "snapshot",
        "schema_before": before,
        "schema_target": target,
        "backup": str(snapshot),
        "snapshot_verified": False,
        "automatic_snapshot": automatic,
    }
    started = time.monotonic()
    journal = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    try:
        with backup_deadline(deadline):
            atomic_json(path.parent / UPGRADE_STATE, state)
            backup_sqlite(path, snapshot, manifest=snapshot.with_suffix(".json"), automatic_upgrade=automatic)
            header, metadata = _snapshot_markers(snapshot)
            state.update(backup_schema=header, backup_recorded_schema=metadata)
            if (header, metadata) != (before, expected):
                raise ContractError("SCHEMA_UNSUPPORTED", "backup_schema_mismatch")
            state.update(phase="migration", snapshot_verified=True)
            atomic_json(path.parent / UPGRADE_STATE, state)
            with _migration_budget(conn, deadline, journal):
                _lock_budget(conn, deadline)
                conn.execute(f"PRAGMA user_version={int(expected)}")
                verify(conn, expected_schema=expected)
                migrate(conn)
                verify(conn, expected_schema=target)
                check_backup_deadline()
                _lock_budget(conn, deadline)
                conn.commit()
    except BaseException as exc:
        if conn.in_transaction:
            conn.rollback()
        reason = (
            "upgrade_deadline_exceeded"
            if deadline is not None and time.monotonic() >= deadline
            else _failure_reason(exc)
        )
        if state["phase"] == "migration" and _blocked_reader(conn, exc, journal):
            reason = "upgrade_blocked_by_reader"
        state.update(status="failed", reason=reason)
        if automatic:
            with suppress(OSError, BackupError):
                if state["snapshot_verified"]:
                    _rotate_snapshots(directory, "attempt", snapshot)
                else:
                    _remove_snapshot(snapshot)
        _save_state(path.parent, state)
        raise
    state.update(status="complete", phase="complete", schema_after=target, seconds=round(time.monotonic() - started, 1))
    if automatic:
        with suppress(OSError, BackupError, ValueError):
            _promote_snapshot(snapshot)
    _save_state(path.parent, state)
    return state
