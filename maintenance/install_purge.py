"""Purge inventory and guard: exactly which receipt-bound files an explicit purge may delete."""
from __future__ import annotations

from contextlib import contextmanager, suppress
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from types import ModuleType
from typing import Any, Iterator

from .install_common import BACKUP_DIRNAME, InstallError, UninstallPlan, _norm, _reject_symlink_chain


@dataclass(frozen=True)
class _PurgeInventory:
    """Exact, receipt-bound files that an explicit purge may remove."""

    data_directory: Path
    installation_id: str
    agent_id: str
    files: tuple[Path, ...]
    retained_files: tuple[Path, ...]
    vector_files: tuple[Path, ...]
    retained_backups: tuple[Path, ...]


def _busy(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).casefold()
    return "busy" in text or "locked" in text


@contextmanager
def _purge_guard(data_directory: Path) -> Iterator[None]:
    """Hold the cooperative writer and physical-retained locks for purge."""
    from scope_recall.core.file_lock import advisory_file_lock
    from scope_recall.core.writer_lease import holding_truth_writer_lease, truth_writer_process_snapshot

    try:
        snapshot = truth_writer_process_snapshot(data_directory)
        if snapshot.get("same_process_holder_count", 0) or snapshot.get("connection_pin_count", 0):
            raise InstallError("purge_busy:truth_writer")
        # ``save_config`` is the existing exclusive maintenance role.  It uses
        # the same OS lease as all Core writers and therefore makes a purge
        # fail closed when a provider process still owns the store.
        with (
            holding_truth_writer_lease(data_directory, role="save_config"),
            advisory_file_lock(data_directory / "scope-recall-retained.lock", timeout_seconds=0),
            advisory_file_lock(data_directory / "runtime-worker.lock", timeout_seconds=0),
        ):
            yield
    except TimeoutError as exc:
        raise InstallError("purge_busy:owned_lock") from exc
    except sqlite3.OperationalError as exc:
        if _busy(exc):
            raise InstallError("purge_busy:truth_database") from exc
        raise
    except RuntimeError as exc:
        if "truth_writer_busy" in str(exc):
            raise InstallError("purge_busy:truth_writer") from exc
        raise


def _purge_identity(host: ModuleType, plan: UninstallPlan, receipt: dict[str, Any]) -> tuple[Path, str, str, Path]:
    """Resolve the data directory only through the signed install identity."""
    data_directory, installation_id, agent_id, config_path = host.purge_identity(plan.instance_root)
    if _norm(data_directory) != _norm(host.data_dir(plan.instance_root).resolve()):
        raise InstallError("purge_refused:data_directory_binding")
    if str(receipt.get("installation_id") or "") != installation_id:
        raise InstallError("purge_refused:installation_binding")
    if str(receipt.get("agent_id") or "") != agent_id:
        raise InstallError("purge_refused:agent_binding")
    _reject_symlink_chain(plan.instance_root)
    _reject_symlink_chain(data_directory)
    if config_path.is_symlink() or not config_path.is_file():
        raise InstallError("purge_refused:installation_manifest")
    return data_directory, installation_id, agent_id, config_path


def _safe_owned_files(root: Path, *, label: str) -> tuple[Path, ...]:
    """Return regular files below an owned directory, rejecting links."""
    _reject_symlink_chain(root)
    if not root.exists():
        return ()
    if not root.is_dir():
        raise InstallError(f"purge_refused:{label}_not_directory")
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        _reject_symlink_chain(path)
        if path.is_dir():
            continue
        if not path.is_file():
            raise InstallError(f"purge_refused:{label}_special_file")
        files.append(path.resolve())
    return tuple(files)


def _expected_retained(db_path: Path, data_directory: Path, *, installation_id: str, agent_id: str) -> set[Path]:
    """Retained blobs the truth database claims, after proving the database is this install's."""
    expected: set[Path] = set()
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        meta = conn.execute(
            "SELECT agent_id,installation_id,data_directory FROM instance_meta WHERE singleton=1"
        ).fetchone()
        if meta is None or (
            str(meta["agent_id"]) != agent_id
            or str(meta["installation_id"]) != installation_id
            or _norm(Path(str(meta["data_directory"]))) != _norm(data_directory)
        ):
            raise InstallError("purge_refused:database_identity")
        rows = conn.execute("SELECT blob_json FROM artifact_versions WHERE blob_json IS NOT NULL").fetchall()
        for row in rows:
            try:
                blob = json.loads(str(row["blob_json"]))
            except (TypeError, ValueError) as exc:
                raise InstallError("purge_refused:attachment_metadata") from exc
            if not isinstance(blob, dict):
                raise InstallError("purge_refused:attachment_metadata")
            if blob.get("installation_id") != installation_id or blob.get("agent_id") != agent_id:
                raise InstallError("purge_refused:attachment_identity")
            relative = Path(str(blob.get("relative_path") or ""))
            sha = str(blob.get("sha256") or "")
            if (
                len(sha) != 64
                or any(char not in "0123456789abcdef" for char in sha)
                or relative.parts != ("retained", sha)
            ):
                raise InstallError("purge_refused:attachment_path")
            expected.add((data_directory / relative).resolve())
    except sqlite3.OperationalError as exc:
        if _busy(exc):
            raise InstallError("purge_busy:truth_database") from exc
        raise InstallError("purge_refused:database_read") from exc
    finally:
        if conn is not None:
            with suppress(Exception):
                conn.rollback()
                conn.close()
    return expected


def _purge_inventory(host: ModuleType, plan: UninstallPlan, receipt: dict[str, Any]) -> _PurgeInventory:
    data_directory, installation_id, agent_id, config_path = _purge_identity(host, plan, receipt)
    db_path = data_directory / "memory.sqlite3"
    if db_path.is_symlink() or not db_path.is_file():
        raise InstallError("purge_refused:database_missing")
    _reject_symlink_chain(db_path)
    if (data_directory / "restore-required.json").exists():
        raise InstallError("purge_refused:restore_pending")

    retained_files = _safe_owned_files(data_directory / "retained", label="retained")
    vector_files = _safe_owned_files(data_directory / "vectors", label="vectors")
    expected_retained = _expected_retained(db_path, data_directory, installation_id=installation_id, agent_id=agent_id)
    # A retained blob the database does not list is foreign; one the database
    # lists but the disk lacks is already gone and safe to purge past.
    if set(retained_files) - expected_retained:
        raise InstallError("purge_refused:unknown_retained_file")

    files = [config_path.resolve(), db_path.resolve()]
    for sidecar in (db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")):
        if sidecar.exists():
            _reject_symlink_chain(sidecar)
            if not sidecar.is_file():
                raise InstallError("purge_refused:database_sidecar")
            files.append(sidecar.resolve())
    files.extend(retained_files)
    files.extend(vector_files)
    backups: tuple[Path, ...] = ()
    backup_root = plan.instance_root / BACKUP_DIRNAME
    if backup_root.exists():
        _reject_symlink_chain(backup_root)
        backups = (backup_root.resolve(),)
    return _PurgeInventory(
        data_directory=data_directory,
        installation_id=installation_id,
        agent_id=agent_id,
        files=tuple(dict.fromkeys(files)),
        retained_files=retained_files,
        vector_files=vector_files,
        retained_backups=backups,
    )


def _purge_owned_data(host: ModuleType, plan: UninstallPlan, receipt: dict[str, Any]) -> tuple[list[str], list[str]]:
    data_directory, _installation_id, _agent_id, _config_path = _purge_identity(host, plan, receipt)
    with _purge_guard(data_directory):
        inventory = _purge_inventory(host, plan, receipt)
        removed: list[str] = []
        # Delete only the inventory under the verified Core data directory;
        # instance_root siblings (host sessions/config/backups) are untouched.
        for path in inventory.files:
            _reject_symlink_chain(path)
            if path.is_file():
                path.unlink()
                removed.append(str(path))
        for directory in (data_directory / "retained", data_directory / "vectors"):
            _reject_symlink_chain(directory)
            if directory.is_dir() and not directory.is_symlink():
                for child in sorted(directory.rglob("*"), reverse=True):
                    _reject_symlink_chain(child)
                    if child.is_dir() and not child.is_symlink():
                        with suppress(OSError):
                            child.rmdir()
                with suppress(OSError):
                    directory.rmdir()
        return removed, [str(path) for path in inventory.retained_backups]
