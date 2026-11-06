"""Consistent SQLite backup and maintenance re-exports.

Delegates canonical implementation of backup primitives to core.backup_primitives.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from scope_recall.core.backup_primitives import (
    BackupError,
    atomic_json,
    atomic_write,
    backup_sqlite,
    create_output,
    exclusive_store,
    first_link,
    safe_path,
    sha256,
    sqlite_busy,
)
from scope_recall.core.file_access import preserve_file_access

__all__ = [
    "BackupError",
    "adjacent_backup",
    "write_private_configuration",
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


def adjacent_backup(path: Path) -> Path:
    """Keep exact configuration bytes beside their source, under the same directory's access boundary.

    Exclusive creation inherits the parent ACL on Windows, then preserves any
    narrower source DACL before writing. POSIX backups are private from their first byte.
    """
    backup = path.with_name(path.name + ".scope-recall-" + uuid.uuid4().hex + ".bak")
    with path.open("rb") as source:
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as target:
                preserve_file_access(path, backup)
                target.write(source.read())
                target.flush()
                os.fsync(target.fileno())
        except BaseException:
            backup.unlink()
            raise
    return backup


def write_private_configuration(path: Path, content: str | bytes) -> None:
    """Publish host configuration under its prior access boundary, with private POSIX permissions."""
    atomic_write(path, content, mode=0o600, access_source=path if path.is_file() else None)
