"""Verified SQLite online backups for explicit operator mutations."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any


_BACKUP_LABEL_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,79}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_verified_sqlite_backup(
    source_conn: sqlite3.Connection,
    database_path: str | Path,
    *,
    label: str,
) -> dict[str, Any]:
    """Create and verify one owner-only SQLite online backup.

    The caller owns ``source_conn``. This helper owns only the temporary backup
    connection and removes an invalid partial artifact before raising.
    """

    clean_label = str(label or "").strip().lower()
    if not _BACKUP_LABEL_RE.fullmatch(clean_label):
        raise ValueError("backup label must match [a-z0-9][a-z0-9_.-]{0,79}")
    db_path = Path(os.path.abspath(os.fspath(Path(database_path).expanduser())))
    backup_dir = db_path.parent / "backups"
    if backup_dir.is_symlink():
        raise ValueError("backup directory cannot be a symlink")
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if backup_dir.is_symlink():
        raise ValueError("backup directory cannot be a symlink")
    if os.name != "nt":
        os.chmod(backup_dir, 0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_path = backup_dir / f"{clean_label}.{timestamp}.sqlite3"
    destination = sqlite3.connect(backup_path)
    try:
        source_conn.backup(destination)
        quick_check = str(destination.execute("PRAGMA quick_check").fetchone()[0])
    except Exception:
        destination.close()
        backup_path.unlink(missing_ok=True)
        raise
    finally:
        try:
            destination.close()
        except Exception:
            pass
    if quick_check.lower() != "ok":
        backup_path.unlink(missing_ok=True)
        raise RuntimeError(f"backup quick_check failed: {quick_check}")
    if os.name != "nt":
        os.chmod(backup_path, 0o600)
    return {
        "path": str(backup_path),
        "size_bytes": int(backup_path.stat().st_size),
        "sha256": _sha256_file(backup_path),
        "quick_check": quick_check,
    }


__all__ = ["create_verified_sqlite_backup"]
