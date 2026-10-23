"""Consistent SQLite backup and path/holding primitives.

Provides atomic write, safe path resolution, store-holding context manager,
and verified SQLite online snapshotting.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import IO, Any

from ..core.file_lock import advisory_file_lock
from ..core.writer_lease import holding_truth_writer_lease, truth_writer_process_snapshot


class BackupError(RuntimeError):
    pass


_BACKUP_DEADLINE: ContextVar[float | None] = ContextVar("backup_deadline", default=None)
_BACKUP_CANCELLED: ContextVar[Callable[[], bool] | None] = ContextVar("backup_cancelled", default=None)


@contextmanager
def backup_deadline(deadline: float | None, *, cancelled: Callable[[], bool] | None = None) -> Iterator[None]:
    """Bound this backup's chunked I/O without changing other threads or manual snapshots."""
    token = _BACKUP_DEADLINE.set(deadline if deadline is not None else _BACKUP_DEADLINE.get())
    cancellation = _BACKUP_CANCELLED.set(cancelled if cancelled is not None else _BACKUP_CANCELLED.get())
    try:
        check_backup_deadline()
        yield
    finally:
        _BACKUP_DEADLINE.reset(token)
        _BACKUP_CANCELLED.reset(cancellation)


def check_backup_deadline() -> None:
    """Stop between I/O chunks when the worker's pass has no time left."""
    deadline = _BACKUP_DEADLINE.get()
    cancelled = _BACKUP_CANCELLED.get()
    if cancelled is not None and cancelled():
        raise BackupError("backup cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise BackupError("backup deadline exceeded")


def copy_stream(source: IO[bytes], target: IO[bytes]) -> None:
    """Copy bounded chunks, respecting the active backup deadline."""
    while True:
        check_backup_deadline()
        chunk = source.read(1024 * 1024)
        if not chunk:
            return
        target.write(chunk)


def stream_digest(stream: IO[bytes]) -> str:
    """Hash bounded chunks under the same deadline as the snapshot."""
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        check_backup_deadline()
        digest.update(chunk)
    return digest.hexdigest()


def sqlite_busy(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).casefold()
    return "busy" in text or "locked" in text


@contextmanager
def exclusive_store(data_directory: Path, *, busy: Callable[[str], Exception]) -> Iterator[None]:
    """Hold writer lease, retained lock, and worker lock during execution."""
    try:
        snapshot = truth_writer_process_snapshot(data_directory)
        if snapshot.get("same_process_holder_count", 0) or snapshot.get("connection_pin_count", 0):
            raise busy("truth_writer")
        with (
            holding_truth_writer_lease(data_directory, role="save_config"),
            advisory_file_lock(data_directory / "scope-recall-retained.lock", timeout_seconds=0),
            advisory_file_lock(data_directory / "runtime-worker.lock", timeout_seconds=0),
        ):
            yield
    except TimeoutError as exc:
        raise busy("owned_lock") from exc
    except sqlite3.OperationalError as exc:
        if sqlite_busy(exc):
            raise busy("truth_database") from exc
        raise
    except RuntimeError as exc:
        if "truth_writer_busy" in str(exc):
            raise busy("truth_writer") from exc
        raise


def first_link(path: Path) -> Path | None:
    """The path itself or nearest ancestor that is a symlink or reparse point."""
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            return part
    return None


def safe_path(value: str | Path, *, must_exist: bool = False, error_type=BackupError) -> Path:
    """Check the original path chain before resolution can erase a link."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if first_link(path) is not None:
        raise error_type("symlink or reparse paths are not allowed")
    return path.resolve(strict=must_exist)


def create_output(path: Path, *, error_type=BackupError) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_path(path, error_type=error_type)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise error_type("refusing to overwrite existing output") from exc
    os.close(descriptor)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return stream_digest(stream)


def atomic_write(path: Path, content: str | bytes) -> None:
    """Flush a complete checkpoint before atomically publishing it beside its target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    owned = None
    try:
        with temporary.open("xb") as stream:
            owned = os.fstat(stream.fileno())
            # Installation receipts rely on the platform's text newline convention.
            stream.write(content if isinstance(content, bytes) else content.replace("\n", os.linesep).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if not os.path.samestat(owned, temporary.lstat()):
            raise BackupError("checkpoint temporary file identity changed")
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if owned is not None and temporary.exists() and os.path.samestat(owned, temporary.lstat()):
            temporary.unlink()


def atomic_json(path: Path, value: object, *, sort_keys: bool = False) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, sort_keys=sort_keys, indent=2) + "\n")


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def backup_sqlite(source: str | Path, destination: str | Path, *, manifest: str | Path | None = None) -> dict[str, Any]:
    """Create a consistent, verified backup into a new isolated path."""
    source_path = safe_path(source, must_exist=True)
    destination_path = safe_path(destination)
    if source_path == destination_path or source_path.is_symlink() or not source_path.is_file():
        raise BackupError("invalid source database")
    if destination_path.exists() or destination_path.is_symlink():
        raise BackupError("refusing to overwrite existing backup")
    manifest_path = safe_path(manifest) if manifest is not None else None
    if manifest_path is not None and (manifest_path in {source_path, destination_path} or manifest_path.exists()):
        raise BackupError("refusing to overwrite existing backup manifest")
    create_output(destination_path)
    reader = writer = None
    try:
        reader = sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True)
        writer = sqlite3.connect(destination_path)
        reader.backup(writer, pages=256, progress=lambda _status, _remaining, _total: check_backup_deadline())
        writer.commit()
        writer.execute("PRAGMA journal_mode=DELETE")
        quick = str(writer.execute("PRAGMA quick_check").fetchone()[0])
        if quick.lower() != "ok":
            raise BackupError(f"backup quick_check failed: {quick}")
        structural = {
            "format": "scope-recall-p15-sqlite-backup/1",
            "source_sha256": sha256(source_path),
            "backup_sha256": sha256(destination_path),
            "size_bytes": destination_path.stat().st_size,
            "user_version": int(writer.execute("PRAGMA user_version").fetchone()[0]),
            "application_id": int(writer.execute("PRAGMA application_id").fetchone()[0]),
            "quick_check": quick,
        }
    finally:
        if reader is not None:
            reader.close()
        if writer is not None:
            writer.close()
    structural["manifest_sha256"] = hashlib.sha256(_canonical(structural)).hexdigest()
    if manifest_path is not None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        safe_path(manifest_path)
        with manifest_path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(structural, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return {"backup_path": str(destination_path), **structural}


__all__ = [
    "BackupError",
    "atomic_json",
    "atomic_write",
    "backup_sqlite",
    "create_output",
    "exclusive_store",
    "first_link",
    "safe_path",
    "sha256",
    "sqlite_busy",
]
