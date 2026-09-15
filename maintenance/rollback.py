"""Conservative P15 rollback boundary.

Rollback is a data-preservation decision, not a blind file replacement.  Once
the new store contains a source or deletion event that the old format cannot
express, this module records durable stop-write protection and leaves both
stores untouched.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from .backup import _create_output, _safe_path


class RollbackError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result: dict[str, int] = {}
        for table in ("source_events", "deletion_operations", "claims", "episodes", "capture_inbox"):
            if table in tables:
                result[table] = int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        return result
    finally:
        conn.close()


def _verified_snapshot_copy(source: Path, destination: Path) -> str:
    _create_output(destination, error_type=RollbackError)
    reader = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
    writer = sqlite3.connect(destination)
    try:
        reader.backup(writer)
        writer.commit()
        quick = str(writer.execute("PRAGMA quick_check").fetchone()[0])
        if quick.lower() != "ok":
            raise RollbackError(f"old snapshot quick_check failed: {quick}")
        return quick
    finally:
        reader.close()
        writer.close()


def _install_core_restore_fence(current: Path) -> dict[str, str]:
    """Install the restore-required marker consumed by every normal Core open."""
    from scope_recall.contracts import InstanceBinding, TrustedContext
    from scope_recall.core.restore import InstallationMaintenance, begin_restore, export_deletion_ledger, ledger_digest
    from scope_recall.core.storage import SQLiteStorage

    conn = sqlite3.connect(f"{current.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        meta = conn.execute("SELECT agent_id,installation_id,data_directory,test_mode FROM instance_meta WHERE singleton=1").fetchone()
        scopes = frozenset(str(row[0]) for row in conn.execute("SELECT scope_id FROM instance_scopes"))
    finally:
        conn.close()
    if meta is None or not scopes:
        raise RollbackError("new database lacks verified Core identity")
    expected_dir = os.path.normcase(os.path.abspath(os.fspath(current.parent)))
    if os.path.normcase(str(meta["data_directory"])) != expected_dir:
        raise RollbackError("Core identity data directory does not match rollback database")
    binding = InstanceBinding(str(meta["agent_id"]), str(meta["installation_id"]), current.parent, scopes, bool(meta["test_mode"]))
    storage = SQLiteStorage(binding, timeout_seconds=3.0)
    context = TrustedContext(binding, "p15-rollback", scopes, "host_generated")
    authority = InstallationMaintenance(context)
    ledger = export_deletion_ledger(storage, authority)
    digest = ledger_digest(ledger)
    restore_marker = current.parent / "restore-required.json"
    if not restore_marker.exists() and not restore_marker.is_symlink():
        begin_restore(storage, authority, expected_ledger_sha256=digest)
    elif restore_marker.is_symlink() or not restore_marker.is_file():
        raise RollbackError("invalid existing Core restore fence")
    else:
        try:
            marker_payload = json.loads(restore_marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RollbackError("unreadable existing Core restore fence") from exc
        if marker_payload.get("expected_ledger_sha256") != digest or marker_payload.get("agent_id") != binding.agent_id or marker_payload.get("installation_id") != binding.installation_id:
            raise RollbackError("existing Core restore fence does not match latest deletion ledger")
    return {"restore_required_marker": str(restore_marker), "deletion_ledger_sha256": digest}


def plan_rollback(current_db: str | Path, old_snapshot: str | Path, *, destination: str | Path | None = None) -> dict[str, Any]:
    """Inspect a rollback without installing a stop-write fence or copying data."""
    current = _safe_path(current_db, must_exist=True, error_type=RollbackError)
    snapshot = _safe_path(old_snapshot, must_exist=True, error_type=RollbackError)
    if current == snapshot or not current.is_file() or not snapshot.is_file():
        raise RollbackError("current and snapshot must be distinct regular files")
    counts = _counts(current)
    protects_new_data = any(counts.get(key, 0) for key in ("source_events", "deletion_operations", "capture_inbox"))
    if destination is not None:
        target = _safe_path(destination, error_type=RollbackError)
        if target.exists() or target in {current, snapshot}:
            raise RollbackError("rollback output must be a new path")
    return dict(status="planned", action="stop_write_for_reconciliation" if protects_new_data else "copy_verified_snapshot",
                current_db=str(current), old_snapshot=str(snapshot), current_counts=counts,
                old_snapshot_preserved=True, restored=False, requires_apply=True)


def rollback_to_verified_snapshot(current_db: str | Path, old_snapshot: str | Path, *, destination: str | Path | None = None) -> dict[str, Any]:
    """Return a safe rollback result without overwriting either input.

    An old-format snapshot is usable only when the new database has no source
    or deletion rows.  Otherwise a stop-write marker is created beside the new
    database, preserving the new database, its deletion ledger, and the old
    snapshot for an authorized replay/cutover decision.
    """
    current = _safe_path(current_db, must_exist=True, error_type=RollbackError)
    snapshot = _safe_path(old_snapshot, must_exist=True, error_type=RollbackError)
    if current == snapshot or not current.is_file() or not snapshot.is_file():
        raise RollbackError("current and snapshot must be distinct regular files")
    current_counts = _counts(current)
    new_data = any(current_counts.get(key, 0) for key in ("source_events", "deletion_operations", "capture_inbox"))
    receipt: dict[str, Any] = {
        "format": "scope-recall-p15-rollback/1",
        "current_sha256": _sha256(current),
        "old_snapshot_sha256": _sha256(snapshot),
        "current_counts": current_counts,
        "old_snapshot_preserved": True,
        "restored": False,
    }
    if new_data:
        fence = _install_core_restore_fence(current)
        marker = current.with_name(current.name + ".stop-write.json")
        if marker.exists() or marker.is_symlink():
            raise RollbackError("stop-write marker already exists")
        receipt.update(status="stop_write_protection_required", reason="old format cannot losslessly represent new sources/deletions/permissions", **fence)
        _safe_path(marker, error_type=RollbackError)
        with marker.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        receipt["stop_write_marker"] = str(marker)
        return receipt
    target = _safe_path(destination, error_type=RollbackError) if destination is not None else current.with_name(current.stem + ".rolledback.sqlite3")
    if target.exists() or target.is_symlink():
        raise RollbackError("refusing to overwrite rollback output")
    target.parent.mkdir(parents=True, exist_ok=True)
    quick = _verified_snapshot_copy(snapshot, target)
    receipt.update(status="verified_old_snapshot_available", restored=True, snapshot_quick_check=quick, rollback_output=str(target), rollback_output_sha256=_sha256(target))
    return receipt


__all__ = ["RollbackError", "rollback_to_verified_snapshot"]
