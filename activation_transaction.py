"""Failure-atomic snapshots and compensation for ``install --activate``.

The installer replaces three independent state surfaces during activation:
the plugin copy, Hermes ``config.yaml``, and Scope Recall storage.  This module
captures their pre-state before any replacement and can restore that state
after config, migration, provider-load, or runtime-verification failures.
"""

from __future__ import annotations

import gc
import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .maintenance_lease import (
    ACTIVATION_LEASE_FILENAME,
    activation_lease_path,
    ensure_activation_guard_triggers,
    read_activation_lease,
    remove_activation_guard_triggers,
)
from .recovery_commands import (
    quote_argument,
    restore_file_command,
    restore_symlink_command,
    restore_tree_command,
)
from .truth_connection import connect_truth_database


class ActivationSnapshotError(RuntimeError):
    """Raised when activation pre-state cannot be captured safely."""


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d.%H%M%S.%f")


def _acquire_activation_lease(database_path: Path) -> dict[str, Any]:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    path = activation_lease_path(database_path)
    token = uuid.uuid4().hex
    payload = {
        "kind": "scope-recall-activation-maintenance",
        "token": token,
        "pid": os.getpid(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database_path": str(database_path),
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise ActivationSnapshotError(
            "an activation maintenance lease already exists; verify no activation "
            f"is running before manual cleanup: {path}"
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return {
        "path": path,
        "database_path": database_path,
        "token": token,
        "payload": payload,
        "acquired": True,
        "released": False,
        "filename": ACTIVATION_LEASE_FILENAME,
    }


def _release_activation_lease(lease: dict[str, Any]) -> bool:
    path = Path(lease["path"])
    if not path.exists():
        lease["released"] = True
        return True
    try:
        payload = read_activation_lease(
            Path(str(lease.get("database_path") or path.parent / "memory.sqlite3"))
        )
    except Exception:
        return False
    if payload is None or str(payload.get("token") or "") != str(lease.get("token") or ""):
        return False
    path.unlink()
    lease["released"] = True
    return True


def _ensure_activation_lease_retained(lease: dict[str, Any]) -> bool:
    """Keep a failed-compensation lease physically present with the same token."""

    path = Path(lease["path"])
    database_path = Path(
        str(lease.get("database_path") or path.parent / "memory.sqlite3")
    )
    if path.exists():
        try:
            payload = read_activation_lease(database_path)
        except Exception:
            return False
        return bool(payload) and str(payload.get("token") or "") == str(
            lease.get("token") or ""
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    raw_payload = lease.get("payload")
    payload = dict(raw_payload) if isinstance(raw_payload, dict) else {
        "kind": "scope-recall-activation-maintenance",
        "token": str(lease.get("token") or ""),
        "pid": os.getpid(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database_path": str(database_path),
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        return _ensure_activation_lease_retained(lease)
    except Exception:
        path.unlink(missing_ok=True)
        return False
    lease["released"] = False
    return True


def _remove_path(path: Path) -> None:
    for attempt in range(4):
        try:
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
            elif path.exists():
                shutil.rmtree(path)
            return
        except OSError as exc:
            sharing_violation = os.name == "nt" and getattr(exc, "winerror", None) in {
                32,
                33,
            }
            if not sharing_violation or attempt == 3:
                raise
            gc.collect()
            time.sleep(0.05 * (2**attempt))


def _generation_state(path: Path) -> dict[str, Any]:
    """Return a cheap generation fingerprint without copying rebuildable vectors."""

    if path.is_symlink():
        return {
            "kind": "symlink",
            "target": os.readlink(path),
        }
    if not path.exists():
        return {"kind": "absent"}
    if path.is_file():
        stat = path.stat()
        return {
            "kind": "file",
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "inode": stat.st_ino,
        }
    if not path.is_dir():
        return {"kind": "unsupported"}

    digest = hashlib.sha256()
    entry_count = 0
    total_bytes = 0
    for child in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
        relative = child.relative_to(path).as_posix()
        stat = child.lstat()
        if child.is_symlink():
            record = f"L\0{relative}\0{os.readlink(child)}\0{stat.st_mtime_ns}"
        elif child.is_file():
            entry_count += 1
            total_bytes += stat.st_size
            record = (
                f"F\0{relative}\0{stat.st_size}\0{stat.st_mtime_ns}\0{stat.st_ino}"
            )
        else:
            record = f"D\0{relative}\0{stat.st_mtime_ns}\0{stat.st_ino}"
        digest.update(record.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\n")
    root_stat = path.stat()
    return {
        "kind": "directory",
        "digest": digest.hexdigest(),
        "entry_count": entry_count,
        "total_bytes": total_bytes,
        "mtime_ns": root_stat.st_mtime_ns,
        "inode": root_stat.st_ino,
    }


def _capture_vector_companions(storage_dir: Path) -> list[dict[str, Any]]:
    specs = (
        (
            "sqlite-bruteforce",
            (
                storage_dir / "vector.sqlite3",
                storage_dir / "vector.sqlite3-wal",
                storage_dir / "vector.sqlite3-shm",
            ),
        ),
        ("lancedb", (storage_dir / "lancedb",)),
    )
    return [
        {
            "name": name,
            "paths": [
                {"path": path, "state": _generation_state(path)} for path in paths
            ],
        }
        for name, paths in specs
    ]


def _compensate_vector_companions(
    snapshots: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    receipts: list[dict[str, Any]] = []
    failures: list[str] = []
    for companion in snapshots:
        paths = list(companion.get("paths") or [])
        current = [
            {
                "path": item["path"],
                "state": _generation_state(Path(item["path"])),
            }
            for item in paths
        ]
        changed = current != paths
        discarded = False
        if changed:
            try:
                for item in current:
                    _remove_path(Path(item["path"]))
                discarded = True
            except Exception as exc:  # pragma: no cover - filesystem dependent
                failures.append(
                    f"vector companion discard failed ({companion.get('name')}): "
                    f"{type(exc).__name__}: {exc}"
                )
        receipts.append(
            {
                "name": str(companion.get("name") or ""),
                "changed": changed,
                "discarded": discarded,
                "rebuild_required": changed,
                "status": (
                    "discarded_rebuild_required"
                    if discarded
                    else "discard_failed"
                    if changed
                    else "unchanged"
                ),
                "paths": [str(item["path"]) for item in paths],
            }
        )
    return receipts, failures


def _committed_vector_receipts(
    snapshots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for companion in snapshots:
        paths = list(companion.get("paths") or [])
        current_states = [
            _generation_state(Path(item["path"])) for item in paths
        ]
        previous_states = [dict(item.get("state") or {}) for item in paths]
        receipts.append(
            {
                "name": str(companion.get("name") or ""),
                "changed": current_states != previous_states,
                "discarded": False,
                "rebuild_required": False,
                "status": "committed_retained",
                "paths": [str(item["path"]) for item in paths],
            }
        )
    return receipts


def _copy_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    elif source.is_file():
        shutil.copy2(source, destination)
    else:
        shutil.copytree(source, destination, symlinks=True)


def _capture_file(path: Path, backup_path: Path) -> dict[str, Any]:
    if path.is_symlink():
        try:
            target_path = path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ActivationSnapshotError(
                f"unable to resolve activation config symlink: {path}"
            ) from exc
        if target_path.exists() and not target_path.is_file():
            raise ActivationSnapshotError(
                f"activation config symlink target is not a file: {target_path}"
            )
        target_preexisting = target_path.is_file()
        target_mode = (
            target_path.stat().st_mode & 0o777 if target_preexisting else None
        )
        target_backup_path: Path | None = None
        if target_preexisting:
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target_path, backup_path)
            backup_path.chmod(0o600)
            target_backup_path = backup_path
        return {
            "path": path,
            "preexisting": True,
            "kind": "symlink",
            "link_target": os.readlink(path),
            "backup_path": target_backup_path,
            "mode": None,
            "target_path": target_path,
            "target_preexisting": target_preexisting,
            "target_backup_path": target_backup_path,
            "target_mode": target_mode,
        }
    if path.exists() and not path.is_file():
        raise ActivationSnapshotError(f"activation state path is not a file: {path}")
    if not path.is_file():
        return {
            "path": path,
            "preexisting": False,
            "kind": "absent",
            "link_target": "",
            "backup_path": None,
            "mode": None,
        }
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_path)
    backup_path.chmod(0o600)
    return {
        "path": path,
        "preexisting": True,
        "kind": "file",
        "link_target": "",
        "backup_path": backup_path,
        "mode": path.stat().st_mode & 0o777,
    }


def _restore_file(snapshot: dict[str, Any]) -> None:
    path = Path(snapshot["path"])
    kind = str(snapshot.get("kind") or "absent")
    if kind == "symlink":
        target_path = Path(snapshot["target_path"])
        _remove_path(target_path)
        if bool(snapshot.get("target_preexisting")):
            target_backup = Path(snapshot["target_backup_path"])
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target_backup, target_path)
            target_mode = snapshot.get("target_mode")
            if isinstance(target_mode, int):
                target_path.chmod(target_mode)
        _remove_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(str(snapshot.get("link_target") or ""))
        return

    _remove_path(path)
    if kind == "absent":
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = Path(snapshot["backup_path"])
    shutil.copy2(backup_path, path)
    mode = snapshot.get("mode")
    if isinstance(mode, int):
        path.chmod(mode)


def _file_state_matches(snapshot: dict[str, Any]) -> bool:
    """Verify both link identity and dereferenced target/file pre-state."""

    path = Path(snapshot["path"])
    kind = str(snapshot.get("kind") or "absent")
    if kind == "absent":
        return not path.exists() and not path.is_symlink()
    if kind == "file":
        backup_path = snapshot.get("backup_path")
        if path.is_symlink() or not path.is_file() or not backup_path:
            return False
        if path.read_bytes() != Path(backup_path).read_bytes():
            return False
        mode = snapshot.get("mode")
        return not isinstance(mode, int) or (path.stat().st_mode & 0o777) == mode
    if kind != "symlink" or not path.is_symlink():
        return False
    if os.readlink(path) != str(snapshot.get("link_target") or ""):
        return False
    target_path = Path(snapshot["target_path"])
    if not bool(snapshot.get("target_preexisting")):
        return not target_path.exists() and not target_path.is_symlink()
    target_backup = snapshot.get("target_backup_path")
    if not target_backup or target_path.is_symlink() or not target_path.is_file():
        return False
    if target_path.read_bytes() != Path(target_backup).read_bytes():
        return False
    target_mode = snapshot.get("target_mode")
    return (
        not isinstance(target_mode, int)
        or (target_path.stat().st_mode & 0o777) == target_mode
    )


def _sqlite_online_backup(source_path: Path, backup_path: Path) -> None:
    if source_path.is_symlink():
        raise ActivationSnapshotError(
            f"refusing activation against symlinked SQLite truth DB: {source_path}"
        )
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source = connect_truth_database(source_path, mode="ro", timeout=30)
    destination = connect_truth_database(backup_path, mode="rwc")
    try:
        source.backup(destination)
        destination.commit()
        check = destination.execute("PRAGMA quick_check").fetchone()
        if check is None or str(check[0]).lower() != "ok":
            raise ActivationSnapshotError(
                f"SQLite activation backup quick_check failed: {backup_path}"
            )
    finally:
        destination.close()
        source.close()
    backup_path.chmod(0o600)


def _install_sqlite_activation_guards(path: Path, *, lease_token: str) -> list[str]:
    connection = connect_truth_database(path, mode="rw", timeout=5.0)
    try:
        connection.execute("BEGIN IMMEDIATE")
        names = ensure_activation_guard_triggers(
            connection,
            path,
            lease_token=lease_token,
        )
        connection.commit()
        return names
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _remove_sqlite_activation_guards(path: Path) -> list[str]:
    if not path.is_file():
        return []
    connection = connect_truth_database(path, mode="rw", timeout=5.0)
    try:
        connection.execute("BEGIN IMMEDIATE")
        names = remove_activation_guard_triggers(connection)
        connection.commit()
        return names
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _sqlite_state_fingerprint(path: Path) -> str:
    """Hash one logical SQLite snapshot, including WAL-visible committed state."""

    if not path.is_file():
        return "absent"
    connection = connect_truth_database(path, mode="ro", timeout=30)
    try:
        connection.execute("BEGIN")
        payload = connection.serialize()
        connection.execute("ROLLBACK")
    finally:
        connection.close()
    return hashlib.sha256(payload).hexdigest()


def _sqlite_logical_fingerprint(path: Path) -> str:
    """Hash schema and rows without depending on SQLite page layout."""

    connection = connect_truth_database(path, mode="ro", timeout=30)
    try:
        digest = hashlib.sha256()
        for line in connection.iterdump():
            digest.update(line.encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()
    finally:
        connection.close()


def refresh_activation_sqlite_epoch(snapshot: dict[str, Any]) -> str:
    """Register activation-owned SQLite writes before any later failure point."""

    sqlite_snapshot = snapshot["sqlite"]
    path = Path(sqlite_snapshot["path"])
    lease = dict(snapshot.get("maintenance_lease") or {})
    if path.is_file() and str(lease.get("token") or ""):
        guard_names = _install_sqlite_activation_guards(
            path,
            lease_token=str(lease["token"]),
        )
        sqlite_snapshot["guard_trigger_names"] = guard_names
        sqlite_snapshot["guard_count"] = len(guard_names)
        sqlite_snapshot["guards_installed"] = True
    fingerprint = _sqlite_state_fingerprint(path)
    sqlite_snapshot["expected_fingerprint"] = fingerprint
    sqlite_snapshot["write_epoch"] = int(sqlite_snapshot.get("write_epoch") or 0) + 1
    return fingerprint


def _verify_sqlite_writer_quiescence(path: Path) -> None:
    """Acquire the writer lock and invalidate cached statements under the lease."""

    connection = connect_truth_database(
        path,
        mode="rw",
        timeout=0.1,
        isolation_level=None,
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
        connection.execute("COMMIT")
    except sqlite3.OperationalError as exc:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise ActivationSnapshotError(
            "SQLite truth DB has an active writer or the maintenance barrier could "
            f"not be established; stop every Scope Recall writer and retry: {path}"
        ) from exc
    finally:
        connection.close()

def _remove_sqlite_files(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        candidate.unlink(missing_ok=True)


def _sqlite_restore_drift_reason(snapshot: dict[str, Any]) -> str:
    path = Path(snapshot["path"])
    expected = str(snapshot.get("expected_fingerprint") or "")
    actual = _sqlite_state_fingerprint(path)
    snapshot["actual_fingerprint"] = actual
    if not expected:
        snapshot["drift_detected"] = True
        return "activation snapshot has no expected SQLite fingerprint"
    drifted = actual != expected
    snapshot["drift_detected"] = drifted
    if not drifted:
        return ""
    return (
        "SQLite truth changed after activation snapshot "
        f"(expected={expected[:16]}, actual={actual[:16]}); "
        "automatic database restore refused to preserve newer writes"
    )


def _restore_sqlite(snapshot: dict[str, Any]) -> None:
    path = Path(snapshot["path"])
    if not bool(snapshot.get("preexisting")):
        _remove_sqlite_files(path)
        return
    backup_path = Path(snapshot["backup_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    source = connect_truth_database(backup_path, mode="ro", timeout=30)
    destination = connect_truth_database(path, mode="rwc")
    try:
        source.backup(destination)
        destination.commit()
        check = destination.execute("PRAGMA quick_check").fetchone()
        if check is None or str(check[0]).lower() != "ok":
            raise ActivationSnapshotError(
                f"restored SQLite truth DB quick_check failed: {path}"
            )
    finally:
        destination.close()
        source.close()
    expected = _sqlite_logical_fingerprint(backup_path)
    actual = _sqlite_logical_fingerprint(path)
    if actual != expected:
        raise ActivationSnapshotError(
            "restored SQLite truth DB fingerprint does not match the activation snapshot"
        )
    mode = snapshot.get("mode")
    if isinstance(mode, int):
        path.chmod(mode)


def capture_activation_state(
    home: Path,
    *,
    writer_quiesced: bool = False,
) -> dict[str, Any]:
    """Capture activation pre-state with an explicit writer-quiescence contract."""

    home = home.expanduser().resolve()
    backup_root = (
        home
        / "backups"
        / "scope-recall-activation"
        / f"{_stamp()}.{uuid.uuid4().hex[:8]}"
    )
    backup_root.mkdir(parents=True, exist_ok=False)
    backup_root.chmod(0o700)
    storage_dir = home / "scope-recall"
    storage_dir_preexisting = storage_dir.is_dir()
    db_path = storage_dir / "memory.sqlite3"
    config = _capture_file(home / "config.yaml", backup_root / "config.yaml")
    storage_config = _capture_file(
        storage_dir / "config.json",
        backup_root / "storage-config.json",
    )
    sqlite_snapshot: dict[str, Any] = {
        "path": db_path,
        "preexisting": db_path.is_file(),
        "backup_path": None,
        "mode": (db_path.stat().st_mode & 0o777) if db_path.is_file() else None,
        "writer_quiesced": bool(writer_quiesced),
        "writer_lock_verified": False,
        "snapshot_fingerprint": "absent",
        "expected_fingerprint": "absent",
        "write_epoch": 0,
        "guard_trigger_names": [],
        "guard_count": 0,
        "guards_installed": False,
        "backup_guards_removed": False,
    }
    if db_path.exists() and not db_path.is_file():
        raise ActivationSnapshotError(f"SQLite truth path is not a file: {db_path}")
    vector_companions: list[dict[str, Any]] = []
    lease = _acquire_activation_lease(db_path)
    try:
        vector_companions = _capture_vector_companions(storage_dir)
        if db_path.is_file():
            if writer_quiesced:
                _verify_sqlite_writer_quiescence(db_path)
                sqlite_snapshot["writer_lock_verified"] = True
            guard_names = _install_sqlite_activation_guards(
                db_path,
                lease_token=str(lease["token"]),
            )
            sqlite_snapshot["guard_trigger_names"] = guard_names
            sqlite_snapshot["guard_count"] = len(guard_names)
            sqlite_snapshot["guards_installed"] = True
            sqlite_snapshot["expected_fingerprint"] = _sqlite_state_fingerprint(
                db_path
            )
            sqlite_snapshot["write_epoch"] = 1
            sqlite_backup = backup_root / "memory.sqlite3"
            _sqlite_online_backup(db_path, sqlite_backup)
            _remove_sqlite_activation_guards(sqlite_backup)
            sqlite_snapshot["backup_guards_removed"] = True
            sqlite_snapshot["backup_path"] = sqlite_backup
            sqlite_snapshot["snapshot_fingerprint"] = _sqlite_state_fingerprint(
                sqlite_backup
            )
    except Exception:
        guards_removed = True
        if bool(sqlite_snapshot.get("guards_installed")):
            try:
                _remove_sqlite_activation_guards(db_path)
            except Exception:
                guards_removed = False
        if guards_removed:
            _release_activation_lease(lease)
        if not storage_dir_preexisting and guards_removed:
            try:
                storage_dir.rmdir()
            except OSError:
                pass
        raise
    return {
        "snapshot_root": backup_root,
        "storage_dir": storage_dir,
        "storage_dir_preexisting": storage_dir_preexisting,
        "maintenance_lease": lease,
        "config": config,
        "storage_config": storage_config,
        "sqlite": sqlite_snapshot,
        "vector_companions": vector_companions,
    }


def _maintenance_lease_receipt(lease: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(lease.get("path") or "")) if lease else Path()
    present = bool(lease) and path.is_file()
    token_matches = False
    if present:
        try:
            payload = read_activation_lease(
                Path(
                    str(
                        lease.get("database_path")
                        or path.parent / "memory.sqlite3"
                    )
                )
            )
            token_matches = bool(payload) and str(payload.get("token") or "") == str(
                lease.get("token") or ""
            )
        except Exception:
            token_matches = False
    released = bool(lease.get("released")) and not present
    return {
        "path": str(lease.get("path") or ""),
        "acquired": bool(lease.get("acquired")),
        "released": released,
        "retained": bool(lease.get("acquired")) and token_matches and not released,
        "present": present,
        "token_matches": token_matches,
    }


def _surface_receipt(snapshot: dict[str, Any], *, restored: bool) -> dict[str, Any]:
    backup = snapshot.get("backup_path")
    kind = str(snapshot.get("kind") or "absent")
    is_symlink = kind == "symlink"
    target_backup = snapshot.get("target_backup_path")
    return {
        "path": str(snapshot["path"]),
        "preexisting": bool(snapshot.get("preexisting")),
        "kind": kind,
        "backup_path": str(backup) if backup else "",
        "restored": restored,
        "link_target": str(snapshot.get("link_target") or "") if is_symlink else "",
        "link_restored": restored if is_symlink else None,
        "target_path": str(snapshot.get("target_path") or "") if is_symlink else "",
        "target_preexisting": (
            bool(snapshot.get("target_preexisting")) if is_symlink else None
        ),
        "target_backup_path": (
            str(target_backup) if is_symlink and target_backup else ""
        ),
        "target_restored": restored if is_symlink else None,
        "writer_quiesced": snapshot.get("writer_quiesced"),
        "writer_lock_verified": snapshot.get("writer_lock_verified"),
        "snapshot_fingerprint": str(snapshot.get("snapshot_fingerprint") or ""),
        "expected_fingerprint": str(snapshot.get("expected_fingerprint") or ""),
        "actual_fingerprint": str(snapshot.get("actual_fingerprint") or ""),
        "write_epoch": int(snapshot.get("write_epoch") or 0),
        "guard_count": int(snapshot.get("guard_count") or 0),
        "guards_installed": bool(snapshot.get("guards_installed")),
        "guards_removed": bool(snapshot.get("guards_removed")),
        "backup_guards_removed": bool(snapshot.get("backup_guards_removed")),
        "drift_detected": snapshot.get("drift_detected"),
        "manual_recovery_required": bool(snapshot.get("drift_detected")) and not restored,
    }


def _restore_command(snapshot: dict[str, Any]) -> str:
    path = Path(snapshot["path"])
    kind = str(snapshot.get("kind") or "absent")
    if kind == "symlink":
        target_backup = snapshot.get("target_backup_path")
        return restore_symlink_command(
            path,
            link_target=str(snapshot.get("link_target") or ""),
            target_path=Path(snapshot["target_path"]),
            target_backup_path=Path(target_backup) if target_backup else None,
        )
    backup = snapshot.get("backup_path")
    return restore_file_command(
        path,
        backup_path=Path(backup) if backup else None,
        preexisting=bool(snapshot.get("preexisting")),
    )


def _plugin_restore_command(
    plugin_dir: Path,
    *,
    previous_plugin_existed: bool,
    plugin_backup_path: str,
) -> str:
    return restore_tree_command(
        plugin_dir,
        backup_path=Path(plugin_backup_path) if plugin_backup_path else None,
        preexisting=previous_plugin_existed,
    )


def committed_activation_receipt(
    snapshot: dict[str, Any],
    *,
    plugin_dir: Path,
    previous_plugin_existed: bool,
    plugin_backup_path: str,
    plugin_replaced: bool,
) -> dict[str, Any]:
    """Return rollback evidence for a successful activation commit."""

    commands = [
        _plugin_restore_command(
            plugin_dir,
            previous_plugin_existed=previous_plugin_existed,
            plugin_backup_path=plugin_backup_path,
        ),
        _restore_command(snapshot["config"]),
        _restore_command(snapshot["storage_config"]),
        _restore_command(snapshot["sqlite"]),
    ]
    commands = [command for command in commands if command]
    lease = dict(snapshot.get("maintenance_lease") or {})
    failures: list[str] = []
    sqlite_snapshot = snapshot["sqlite"]
    if bool(sqlite_snapshot.get("guards_installed")):
        try:
            _remove_sqlite_activation_guards(Path(sqlite_snapshot["path"]))
            sqlite_snapshot["guards_removed"] = True
            sqlite_snapshot["guards_installed"] = False
        except Exception as exc:
            failures.append(f"activation SQLite guard cleanup failed: {exc}")
    if lease and not failures and not _release_activation_lease(lease):
        failures.append("activation maintenance lease release failed")
        try:
            guard_names = _install_sqlite_activation_guards(
                Path(sqlite_snapshot["path"]),
                lease_token=str(lease.get("token") or ""),
            )
            sqlite_snapshot["guard_trigger_names"] = guard_names
            sqlite_snapshot["guard_count"] = len(guard_names)
            sqlite_snapshot["guards_installed"] = True
            sqlite_snapshot["guards_removed"] = False
        except Exception as exc:
            failures.append(f"activation SQLite guard reinstall failed: {exc}")
    if lease:
        snapshot["maintenance_lease"] = lease
    return {
        "status": "committed" if not failures else "commit_cleanup_failed",
        "automatic_rollback": False,
        "snapshot_root": str(snapshot["snapshot_root"]),
        "failures": failures,
        "maintenance_lease": _maintenance_lease_receipt(lease),
        "restore_commands": commands,
        "plugin": {
            "path": str(plugin_dir),
            "preexisting": previous_plugin_existed,
            "backup_path": plugin_backup_path,
            "replaced": plugin_replaced,
            "restored": False,
        },
        "config": _surface_receipt(snapshot["config"], restored=False),
        "storage_config": _surface_receipt(
            snapshot["storage_config"],
            restored=False,
        ),
        "sqlite": _surface_receipt(snapshot["sqlite"], restored=False),
        "vector_companions": _committed_vector_receipts(
            list(snapshot.get("vector_companions") or [])
        ),
    }


def compensate_activation_failure(
    snapshot: dict[str, Any],
    *,
    plugin_dir: Path,
    previous_plugin_existed: bool,
    previous_version: str,
    plugin_backup_path: str,
    plugin_replaced: bool,
) -> dict[str, Any]:
    """Best-effort restoration of every activation state surface."""

    sqlite_snapshot = snapshot["sqlite"]
    preflight_reason = ""
    if bool(sqlite_snapshot.get("preexisting")) and not bool(
        sqlite_snapshot.get("writer_quiesced")
    ):
        preflight_reason = (
            "writer quiescence was not confirmed before the activation snapshot"
        )
    else:
        preflight_reason = _sqlite_restore_drift_reason(sqlite_snapshot)
    if preflight_reason:
        storage_dir = Path(snapshot["storage_dir"])
        commands = [
            _plugin_restore_command(
                plugin_dir,
                previous_plugin_existed=previous_plugin_existed,
                plugin_backup_path=plugin_backup_path,
            ),
            _restore_command(snapshot["config"]),
            _restore_command(snapshot["storage_config"]),
            _restore_command(sqlite_snapshot),
        ]
        commands = [command for command in commands if command]
        vector_receipts = [
            {
                **item,
                "status": "compensation_skipped_preflight",
                "compensation_skipped": True,
            }
            for item in _committed_vector_receipts(
                list(snapshot.get("vector_companions") or [])
            )
        ]
        if any(bool(item.get("changed")) for item in vector_receipts):
            commands.append(
                "hermes-scope-recall vector repair apply --hermes-home "
                f"{quote_argument(storage_dir.parent)}"
            )
        lease = dict(snapshot.get("maintenance_lease") or {})
        return {
            "status": "rollback_failed",
            "automatic_rollback": False,
            "compensation_started": False,
            "snapshot_root": str(snapshot["snapshot_root"]),
            "failures": [f"SQLite compensation preflight refused: {preflight_reason}"],
            "maintenance_lease": _maintenance_lease_receipt(lease),
            "restore_commands": commands,
            "plugin": {
                "path": str(plugin_dir),
                "preexisting": previous_plugin_existed,
                "backup_path": plugin_backup_path,
                "replaced": plugin_replaced,
                "restored": False,
            },
            "config": _surface_receipt(snapshot["config"], restored=False),
            "storage_config": _surface_receipt(
                snapshot["storage_config"],
                restored=False,
            ),
            "sqlite": _surface_receipt(sqlite_snapshot, restored=False),
            "vector_companions": vector_receipts,
        }

    failures: list[str] = []
    plugin_restored = False
    config_restored = False
    storage_config_restored = False
    sqlite_restored = False
    vector_receipts, vector_failures = _compensate_vector_companions(
        list(snapshot.get("vector_companions") or [])
    )
    failures.extend(vector_failures)

    try:
        if plugin_replaced:
            _remove_path(plugin_dir)
            if previous_plugin_existed:
                if not plugin_backup_path:
                    raise ActivationSnapshotError(
                        "previous plugin existed but no plugin backup was captured"
                    )
                _copy_path(Path(plugin_backup_path), plugin_dir)
        plugin_restored = (
            plugin_dir.exists() == previous_plugin_existed
            and (
                not previous_plugin_existed
                or not previous_version
                or _manifest_version(plugin_dir) == previous_version
            )
        )
        if not plugin_restored:
            raise ActivationSnapshotError("plugin pre-state verification failed")
    except Exception as exc:  # pragma: no cover - exercised by fault-injection tests
        failures.append(f"plugin restore failed: {type(exc).__name__}: {exc}")

    try:
        _restore_file(snapshot["config"])
        config_restored = _file_state_matches(snapshot["config"])
        if not config_restored:
            raise ActivationSnapshotError("config pre-state verification failed")
    except Exception as exc:  # pragma: no cover - platform/filesystem dependent
        failures.append(f"config restore failed: {type(exc).__name__}: {exc}")

    storage_dir = Path(snapshot["storage_dir"])
    if not bool(snapshot.get("storage_dir_preexisting")):
        drift_reason = _sqlite_restore_drift_reason(snapshot["sqlite"])
        if drift_reason:
            failures.append(f"fresh storage cleanup refused: {drift_reason}")
        else:
            try:
                if storage_dir.exists() or storage_dir.is_symlink():
                    _remove_path(storage_dir)
                storage_config_restored = True
                sqlite_restored = True
                snapshot["sqlite"]["guards_removed"] = True
                snapshot["sqlite"]["guards_installed"] = False
            except Exception as exc:  # pragma: no cover - platform/filesystem dependent
                failures.append(
                    f"fresh storage cleanup failed: {type(exc).__name__}: {exc}"
                )
    else:
        try:
            _restore_file(snapshot["storage_config"])
            storage_config_restored = _file_state_matches(
                snapshot["storage_config"]
            )
            if not storage_config_restored:
                raise ActivationSnapshotError(
                    "provider config pre-state verification failed"
                )
        except Exception as exc:  # pragma: no cover - platform/filesystem dependent
            failures.append(
                f"provider config restore failed: {type(exc).__name__}: {exc}"
            )
        if bool(snapshot["sqlite"].get("preexisting")) and not bool(
            snapshot["sqlite"].get("writer_quiesced")
        ):
            failures.append(
                "SQLite restore refused: writer quiescence was not confirmed before snapshot"
            )
        else:
            drift_reason = _sqlite_restore_drift_reason(snapshot["sqlite"])
            if drift_reason:
                failures.append(f"SQLite restore refused: {drift_reason}")
            else:
                try:
                    _restore_sqlite(snapshot["sqlite"])
                    snapshot["sqlite"]["guards_removed"] = True
                    snapshot["sqlite"]["guards_installed"] = False
                    sqlite_restored = True
                except Exception as exc:  # pragma: no cover - platform/filesystem dependent
                    failures.append(
                        f"SQLite restore failed: {type(exc).__name__}: {exc}"
                    )

    commands = [
        _plugin_restore_command(
            plugin_dir,
            previous_plugin_existed=previous_plugin_existed,
            plugin_backup_path=plugin_backup_path,
        ),
        _restore_command(snapshot["config"]),
        _restore_command(snapshot["storage_config"]),
        _restore_command(snapshot["sqlite"]),
    ]
    commands = [command for command in commands if command]
    if any(bool(item.get("rebuild_required")) for item in vector_receipts):
        commands.append(
            "hermes-scope-recall vector repair apply --hermes-home "
            f"{quote_argument(storage_dir.parent)}"
        )
    lease = dict(snapshot.get("maintenance_lease") or {})
    if failures and lease and not _ensure_activation_lease_retained(lease):
        failures.append(
            "activation maintenance lease could not be retained after rollback failure"
        )
    if not failures and lease and not _release_activation_lease(lease):
        failures.append("activation maintenance lease release failed")
    if lease:
        snapshot["maintenance_lease"] = lease
    return {
        "status": "rolled_back" if not failures else "rollback_failed",
        "automatic_rollback": not failures,
        "compensation_started": True,
        "snapshot_root": str(snapshot["snapshot_root"]),
        "failures": failures,
        "maintenance_lease": _maintenance_lease_receipt(lease),
        "restore_commands": commands,
        "plugin": {
            "path": str(plugin_dir),
            "preexisting": previous_plugin_existed,
            "backup_path": plugin_backup_path,
            "replaced": plugin_replaced,
            "restored": plugin_restored,
        },
        "config": _surface_receipt(snapshot["config"], restored=config_restored),
        "storage_config": _surface_receipt(
            snapshot["storage_config"],
            restored=storage_config_restored,
        ),
        "sqlite": _surface_receipt(
            snapshot["sqlite"],
            restored=sqlite_restored,
        ),
        "vector_companions": vector_receipts,
    }


def _manifest_version(plugin_dir: Path) -> str:
    manifest = plugin_dir / "plugin.yaml"
    if not manifest.is_file():
        return ""
    for line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().startswith("version:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    return ""
