"""Read-only inspection and guarded repair for memory FTS membership drift.

SQLite ``memories`` is the truth source. The FTS table is rebuildable, but an
apply operation still requires an explicit maintenance acknowledgement and a
verified online backup before the companion is reconciled.
"""
from __future__ import annotations

import os
import re
import sqlite3
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .maintenance_ops import connect_memory_db, memory_db_path
from .sql_store import fts_integrity_report, reconcile_fts_index


def backup_permission_model() -> str:
    """Describe the permission primitive recorded in operator receipts."""

    return "windows_acl_inherited" if os.name == "nt" else "posix_owner_only"


def secure_online_backup(
    conn: sqlite3.Connection,
    db_path: Path,
    *,
    purpose: str = "fts-reconcile",
    backup_source: sqlite3.Connection | None = None,
) -> Path:
    """Create and quick-check an owner-only SQLite online backup.

    ``backup_source`` may supply a separate reader connection when the caller
    holds a write fence (``BEGIN IMMEDIATE``) on ``conn``: SQLite cannot run an
    online backup from a connection with an open write transaction, but a
    reader connection may copy the committed snapshot while the fence blocks
    concurrent writers.
    """

    normalized_purpose = str(purpose or "").strip().lower()
    if re.fullmatch(r"[a-z0-9-]{1,24}", normalized_purpose) is None:
        raise ValueError("backup purpose must match [a-z0-9-]{1,24}")

    backup_dir = db_path.parent / "backups"
    if backup_dir.is_symlink():
        raise RuntimeError("FTS backup directory must not be a symlink")
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup_dir.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_dir / (
        f"memory.pre-{normalized_purpose}.{stamp}.{uuid.uuid4().hex[:8]}.sqlite3"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(destination, flags, 0o600)
        try:
            mode = os.fstat(descriptor).st_mode
            if not stat.S_ISREG(mode):
                raise RuntimeError("FTS backup destination must be a regular file")
            descriptor_chmod = getattr(os, "fchmod", None)
            if descriptor_chmod is not None:
                descriptor_chmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
    except Exception:
        destination.unlink(missing_ok=True)
        raise

    target: sqlite3.Connection | None = None
    try:
        target = sqlite3.connect(destination)
        source = backup_source if backup_source is not None else conn
        source.backup(target)
        quick_check = str(target.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check.lower() != "ok":
            raise RuntimeError(f"FTS backup quick_check failed: {quick_check}")
    except Exception:
        if target is not None:
            target.close()
            target = None
        destination.unlink(missing_ok=True)
        raise
    finally:
        if target is not None:
            target.close()
    destination.chmod(0o600)
    return destination


def _read_report(db_path: Path) -> dict[str, int | bool]:
    conn = connect_memory_db(db_path, apply=False)
    try:
        return fts_integrity_report(conn)
    finally:
        conn.close()


def repair_fts_index(
    hermes_home: Path,
    *,
    apply: bool = False,
    maintenance_confirmed: bool = False,
) -> dict[str, Any]:
    """Inspect or transactionally reconcile memory FTS lifecycle membership.

    Dry-run is the default and opens the truth database read-only. Apply mode
    refuses to open a mutable connection until the caller confirms that normal
    writers are stopped for the maintenance window.
    """

    db_path = memory_db_path(hermes_home)
    if not db_path.is_file():
        return {
            "ok": False,
            "status": "missing",
            "dry_run": not apply,
            "path": str(db_path),
            "backup_path": "",
            "error": "SQLite truth DB not found",
        }

    try:
        before = _read_report(db_path)
    except Exception as exc:
        return {
            "ok": False,
            "status": "error",
            "dry_run": not apply,
            "path": str(db_path),
            "backup_path": "",
            "error": str(exc),
        }

    if not apply:
        return {
            "ok": bool(before.get("healthy")),
            "status": "ready" if bool(before.get("healthy")) else "needs_repair",
            "dry_run": True,
            "path": str(db_path),
            "backup_path": "",
            "before": before,
            "after": dict(before),
        }

    if not maintenance_confirmed:
        return {
            "ok": False,
            "status": "confirmation_required",
            "dry_run": False,
            "path": str(db_path),
            "backup_path": "",
            "before": before,
            "after": dict(before),
            "error": "Stop normal Scope Recall writers and pass maintenance_confirmed before apply",
        }

    if bool(before.get("healthy")):
        return {
            "ok": True,
            "status": "ready",
            "dry_run": False,
            "path": str(db_path),
            "backup_path": "",
            "before": before,
            "after": dict(before),
        }

    try:
        conn = connect_memory_db(db_path, apply=True)
    except Exception as exc:
        return {
            "ok": False,
            "status": "error",
            "dry_run": False,
            "path": str(db_path),
            "backup_path": "",
            "before": before,
            "after": dict(before),
            "error": str(exc),
        }
    backup_path: Path | None = None
    try:
        backup_path = secure_online_backup(
            conn,
            db_path,
            purpose="fts-reconcile",
        )
        repair_receipt = reconcile_fts_index(conn, commit=True)
        raw_after = repair_receipt.get("after")
        after = dict(raw_after) if isinstance(raw_after, dict) else fts_integrity_report(conn)
    except Exception as exc:
        if conn.in_transaction:
            conn.rollback()
        try:
            current_after = _read_report(db_path)
        except Exception:
            current_after = dict(before)
        return {
            "ok": False,
            "status": "error",
            "dry_run": False,
            "path": str(db_path),
            "backup_path": str(backup_path) if backup_path else "",
            "before": before,
            "after": current_after,
            "error": str(exc),
        }
    finally:
        conn.close()

    healthy = bool(after.get("healthy"))
    return {
        "ok": healthy,
        "status": "ready" if healthy else "needs_repair",
        "dry_run": False,
        "path": str(db_path),
        "backup_path": str(backup_path) if backup_path else "",
        "backup_permission_model": backup_permission_model(),
        "before": before,
        "after": after,
        "repair": repair_receipt,
    }
