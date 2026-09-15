"""P15-only consistent SQLite backup helper.

This module is intentionally small and offline.  It uses sqlite3.Connection.backup
instead of copying a live main file, never overwrites a public backup, and emits
only structural metadata (no row content or credentials).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any


class BackupError(RuntimeError):
    pass


def _safe_path(value: str | Path, *, must_exist: bool = False, error_type=BackupError) -> Path:
    """Check the original path chain before resolution can erase a link."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise error_type("symlink or reparse paths are not allowed")
    return path.resolve(strict=must_exist)


def _create_output(path: Path, *, error_type=BackupError) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe_path(path, error_type=error_type)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise error_type("refusing to overwrite existing output") from exc
    os.close(descriptor)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def backup_sqlite(source: str | Path, destination: str | Path, *, manifest: str | Path | None = None) -> dict[str, Any]:
    """Create a consistent, verified backup into a new isolated path."""
    source_path = _safe_path(source, must_exist=True)
    destination_path = _safe_path(destination)
    if source_path == destination_path or source_path.is_symlink() or not source_path.is_file():
        raise BackupError("invalid source database")
    if destination_path.exists() or destination_path.is_symlink():
        raise BackupError("refusing to overwrite existing backup")
    manifest_path = _safe_path(manifest) if manifest is not None else None
    if manifest_path is not None and (manifest_path in {source_path, destination_path} or manifest_path.exists()):
        raise BackupError("refusing to overwrite existing backup manifest")
    _create_output(destination_path)
    reader = writer = None
    try:
        reader = sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True)
        writer = sqlite3.connect(destination_path)
        reader.backup(writer)
        writer.commit()
        # A WAL-mode source can copy its journal mode into the destination.
        # Finalize the standalone snapshot before hashing it; closing a writer
        # must not checkpoint bytes after the manifest digest was recorded.
        writer.execute("PRAGMA journal_mode=DELETE")
        quick = str(writer.execute("PRAGMA quick_check").fetchone()[0])
        if quick.lower() != "ok":
            raise BackupError(f"backup quick_check failed: {quick}")
        structural = {
            "format": "scope-recall-p15-sqlite-backup/1",
            "source_sha256": _sha256(source_path),
            "backup_sha256": _sha256(destination_path),
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
        _safe_path(manifest_path)
        with manifest_path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(structural, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return {"backup_path": str(destination_path), **structural}


__all__ = ["BackupError", "backup_sqlite"]
