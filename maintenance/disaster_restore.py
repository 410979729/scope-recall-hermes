"""Disaster recovery restore to a fresh destination location.

Validates the full backup bundle, ensures target destination is fresh, restores SQLite
database, attachments, configuration and queue buffers, rewrites paths to the new location,
restores vectors or queues worker rebuild, replays the deletion ledger mirror, runs doctor,
and reports the recovery point and unrecoverable later interval.

Does not execute live registration, host installation, or autostart restarts.
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..contracts import InstanceBinding, TrustedContext
from ..core.deletion_mirror import DELETION_MIRROR_FILENAME, replay_deletion_mirror
from ..core.storage import SQLiteStorage, _directory
from ..runtime.backup_archive import write_verified_members
from ..runtime.backup_bundle import inspect_backup_archive, verify_backup_bundle
from ..runtime.backup_primitives import first_link, safe_path
from .doctor_report import DoctorReport
from .doctor_store import (
    check_backlog,
    check_candidates,
    check_deletion_mirror,
    check_footprint,
    check_index,
    check_ledger,
    check_model_output,
    check_runtime_config_present,
    check_schema,
    check_storage,
)


class DisasterRestoreError(RuntimeError):
    """Raised when disaster restore validation or application fails."""


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _find_mirror_path(bundle_path: Path, explicit_mirror: Path | None) -> Path | None:
    if explicit_mirror is not None:
        p = safe_path(explicit_mirror, must_exist=True)
        if p.is_file():
            return p
        raise DisasterRestoreError(f"specified mirror file does not exist: {p}")
    sibling = bundle_path.parent / DELETION_MIRROR_FILENAME
    if sibling.is_file() and first_link(sibling) is None:
        return sibling
    return None


def _validate_destination(to: Path) -> Path:
    dest = safe_path(to)
    if dest.exists():
        if not dest.is_dir():
            raise DisasterRestoreError(f"destination must be a directory, not a file: {dest}")
        if any(dest.iterdir()):
            raise DisasterRestoreError(f"destination directory is not empty: {dest}")
    return dest


def plan_disaster_restore(
    bundle: Path,
    to: Path,
    *,
    mirror: Path | None = None,
) -> dict[str, Any]:
    """Validate a backup bundle and target destination for disaster recovery restore."""
    safe_bundle = safe_path(bundle, must_exist=True)
    dest = _validate_destination(to)

    if safe_bundle == dest or safe_bundle in dest.parents or dest in safe_bundle.parents:
        raise DisasterRestoreError("bundle cannot be inside target destination or vice-versa")

    verify_receipt = verify_backup_bundle(safe_bundle, temp_restore_check=False)
    mirror_path = _find_mirror_path(safe_bundle, mirror)

    with zipfile.ZipFile(safe_bundle, "r") as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))

    return {
        "status": "planned",
        "bundle": str(safe_bundle),
        "destination": str(dest),
        "recovery_point_utc": manifest.get("created_at_utc"),
        "with_vectors": manifest.get("with_vectors", False),
        "entries": manifest.get("entries", []),
        "files_count": len(manifest.get("files", [])),
        "mirror_path": str(mirror_path) if mirror_path else None,
        "verification": verify_receipt,
    }


def _rewrite_extracted_configs(dest: Path) -> None:
    """Rewrite absolute data directory and child paths in restored configuration files."""
    runtime_config_path = dest / "runtime-config.json"
    if runtime_config_path.is_file():
        try:
            data = json.loads(runtime_config_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if "binding" in data and isinstance(data["binding"], dict):
                    data["binding"]["data_directory"] = str(dest)
                if "vector" in data and isinstance(data["vector"], dict) and "storage_dir" in data["vector"]:
                    old_vdir = Path(data["vector"]["storage_dir"])
                    data["vector"]["storage_dir"] = str(dest / "vectors" / old_vdir.name)
                if "auxiliary" in data and isinstance(data["auxiliary"], dict) and data["auxiliary"].get("ledger_path"):
                    old_ledger = Path(data["auxiliary"]["ledger_path"])
                    data["auxiliary"]["ledger_path"] = str(dest / "budget" / old_ledger.name)
                runtime_config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (json.JSONDecodeError, OSError) as exc:
            raise DisasterRestoreError(f"failed rewriting runtime-config.json: {exc}") from exc

    for inst_name in ("installation.json", "codex-installation.json"):
        inst_path = dest / inst_name
        if inst_path.is_file():
            try:
                data = json.loads(inst_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data["data_directory"] = str(dest)
                    inst_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except (json.JSONDecodeError, OSError):
                pass

    shared_manifest_path = dest / "shared-manifest.json"
    if shared_manifest_path.is_file():
        try:
            data = json.loads(shared_manifest_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data["root"] = str(dest)
                shared_manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (json.JSONDecodeError, OSError):
            pass


def _generate_reregistration_commands(dest: Path, entries: list[dict[str, Any]]) -> list[str]:
    """Generate exact CLI commands for operators to re-register clients against the new store."""
    commands: list[str] = []
    seen: set[str] = set()

    for entry in entries:
        host = entry.get("host")
        entry_id = entry.get("entry_id")
        if host and host not in seen:
            seen.add(host)
            if host in ("hermes", "codex", "claude-code", "workbuddy", "dsh"):
                commands.append(
                    f"scope-recall attach --root {dest} --host {host} --instance-root <path-to-{host}-home>"
                )

    if not commands:
        commands.append(f"scope-recall doctor --host <host> --instance-root {dest.parent}")

    return sorted(commands)


def _restored_binding(dest: Path) -> InstanceBinding:
    """Read identity from restored truth, including the local/shared store mode."""
    with closing(sqlite3.connect(f"{(dest / 'memory.sqlite3').as_uri()}?mode=ro", uri=True)) as conn:
        row = conn.execute(
            "SELECT agent_id,installation_id,data_directory,test_mode,installation_kind "
            "FROM instance_meta WHERE singleton=1"
        ).fetchone()
        scopes = frozenset(r[0] for r in conn.execute("SELECT scope_id FROM instance_scopes"))
    if row is None:
        raise DisasterRestoreError("restored store has no installation identity")
    return InstanceBinding(row[0], row[1], Path(row[2]), scopes, bool(row[3]), row[4])


def _run_doctor_checks(dest: Path) -> dict[str, Any]:
    """Run store diagnostics on the newly restored directory."""
    report = DoctorReport(host="hermes", status="ok")
    try:
        check_runtime_config_present(report, dest)
        db_path = dest / "memory.sqlite3"
        readable = False
        if db_path.is_file():
            readable = check_storage(report, _restored_binding(dest), dest)
        check_footprint(report, dest, None)
        if readable:
            check_schema(report)
            check_backlog(report, None)
            check_candidates(report)
            check_model_output(report)
            check_ledger(report)
            check_deletion_mirror(report, dest)
        check_index(report, dest, store_readable=readable, config=None)
        if report.capability_gaps:
            report.status = "degraded"
    except Exception as exc:  # noqa: BLE001
        report.capability_gaps.append(f"doctor_check_error:{exc}")
        report.status = "degraded"

    return {
        "status": report.status,
        "capability_gaps": sorted(set(report.capability_gaps)),
        "checks_count": len(report.checks),
    }


def apply_disaster_restore(
    bundle: Path,
    to: Path,
    *,
    mirror: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Restore a full backup bundle to a fresh destination directory."""
    safe_bundle = safe_path(bundle, must_exist=True)
    dest = _validate_destination(to)

    if safe_bundle == dest or safe_bundle in dest.parents or dest in safe_bundle.parents:
        raise DisasterRestoreError("bundle cannot be inside target destination or vice-versa")

    mirror_path = _find_mirror_path(safe_bundle, mirror)
    if not zipfile.is_zipfile(safe_bundle):
        raise DisasterRestoreError("bundle is not a valid zip archive")

    with zipfile.ZipFile(safe_bundle, "r") as archive:
        inspection = inspect_backup_archive(archive)
        write_verified_members(archive, dest, inspection.members)
    manifest = inspection.manifest

    _rewrite_extracted_configs(dest)

    db_path = dest / "memory.sqlite3"
    if not db_path.is_file():
        raise DisasterRestoreError("restored memory.sqlite3 is missing")

    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("UPDATE instance_meta SET data_directory = ? WHERE singleton = 1", (_directory(dest),))
        conn.commit()

    with_vectors = manifest.get("with_vectors", False)
    vector_rebuild_queued = 0
    if not with_vectors:
        moment = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
        binding = _restored_binding(dest)
        context = TrustedContext(binding, "disaster-restore", binding.scope_ids, "host_generated")
        with SQLiteStorage(binding).write(context, remaining_seconds=30) as tx:
            vector_rebuild_queued = tx.work.rebuild_lost_vectors(now=moment)

    mirror_result: dict[str, Any] = {"status": "none", "replayed_operations": 0, "latest_mirrored_at": None}
    if mirror_path is not None:
        try:
            mirror_result = replay_deletion_mirror(db_path, mirror_path)
        except Exception as exc:
            raise DisasterRestoreError(f"failed replaying deletion mirror: {exc}") from exc

    entries = manifest.get("entries", [])
    reregistration_commands = _generate_reregistration_commands(dest, entries)
    doctor_summary = _run_doctor_checks(dest)

    recovery_point_utc = manifest.get("created_at_utc", "")
    latest_mirrored_at = mirror_result.get("latest_mirrored_at")
    restore_moment = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")

    interval_start = latest_mirrored_at or recovery_point_utc
    try:
        t0 = datetime.fromisoformat(interval_start.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(restore_moment.replace("Z", "+00:00"))
        unrecoverable_seconds = max(0.0, (t1 - t0).total_seconds())
    except (ValueError, TypeError):
        unrecoverable_seconds = 0.0

    return {
        "status": "restored",
        "bundle": str(safe_bundle),
        "destination": str(dest),
        "recovery_point_utc": recovery_point_utc,
        "latest_mirrored_at": latest_mirrored_at,
        "mirror_replayed_operations": mirror_result.get("replayed_operations", 0),
        "with_vectors": with_vectors,
        "vector_rebuild_queued": vector_rebuild_queued,
        "unrecoverable_later_interval": {
            "start_utc": interval_start,
            "end_utc": restore_moment,
            "duration_seconds": round(unrecoverable_seconds, 1),
            "note": "Regular captures and updates after recovery point are unrecoverable; post-backup deletions were replayed.",
        },
        "reregistration_commands": reregistration_commands,
        "doctor": doctor_summary,
    }


__all__ = [
    "DisasterRestoreError",
    "apply_disaster_restore",
    "plan_disaster_restore",
]
