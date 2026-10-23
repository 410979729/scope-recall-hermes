"""Disaster recovery restore to a fresh destination location.

Validates the full backup bundle, ensures target destination is fresh, restores SQLite
database, attachments, configuration and queue buffers, rewrites paths to the new location,
restores vectors or queues worker rebuild, replays the deletion ledger mirror, runs doctor,
and reports the recovery point and unrecoverable later interval.

Does not execute live registration, host installation, or autostart restarts.
"""

from __future__ import annotations

import json
import platform
import sqlite3
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scope_recall.contracts import InstanceBinding, TrustedContext
from scope_recall.core.restore import InstallationMaintenance, fence_disaster_recovery, finish_disaster_recovery
from scope_recall.core.storage import SQLiteStorage, _directory
from scope_recall.maintenance.backup_replay import replay_deletion_mirror

from ..adapters.hermes.shared_entries import read_shared_payload
from ..core.deletion_mirror import DELETION_MIRROR_FILENAME, installation_mirror
from ..core.recall_policy import EMBEDDING_SPACE, embedding_space_id
from ..runtime.auxiliary import AuxiliaryRuntimeConfig
from ..runtime.backup_archive import write_verified_members
from ..runtime.backup_bundle import inspect_backup_archive, verify_backup_bundle
from ..runtime.backup_primitives import BackupError, atomic_json, first_link, safe_path
from ..runtime.backup_resources import rewrite_budget_fields
from .backup_relocation import (
    RestoreLayout,
    command_text,
    relocate_installation,
    relocate_remote_clients,
    reregistration_arguments,
    restore_layout,
)
from .doctor import load_binding
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
from .shared import store_binding


class DisasterRestoreError(RuntimeError):
    """Raised when disaster restore validation or application fails."""


def _find_mirror_path(bundle_path: Path, explicit_mirror: Path | None) -> Path:
    if explicit_mirror is not None:
        p = safe_path(explicit_mirror, must_exist=True)
        if p.name != DELETION_MIRROR_FILENAME:
            raise DisasterRestoreError("mirror must be named deletion-mirror.jsonl; keep that name when copying it")
        if p.is_file():
            return p
        raise DisasterRestoreError(f"specified mirror file does not exist: {p}")
    sibling = bundle_path.parent / DELETION_MIRROR_FILENAME
    if sibling.is_file() and first_link(sibling) is None:
        return sibling
    raise DisasterRestoreError("deletion mirror is required; supply --mirror from the original backup directory")


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

    verify_receipt = verify_backup_bundle(safe_bundle, manifest_check=_require_same_system)
    mirror_path = _find_mirror_path(safe_bundle, mirror)

    manifest = verify_receipt
    installation_mirror(mirror_path, manifest["installation_id"])
    layout = restore_layout(dest, manifest)

    return {
        "status": "planned",
        "bundle": str(safe_bundle),
        "destination": str(dest),
        "data_directory": str(layout.data),
        "recovery_point_utc": manifest.get("created_at_utc"),
        "with_vectors": manifest.get("with_vectors", False),
        "entries": manifest.get("entries", []),
        "files_count": verify_receipt["files_verified"],
        "mirror_path": str(mirror_path) if mirror_path else None,
        "verification": verify_receipt,
    }


def _require_same_system(manifest: dict[str, Any]) -> None:
    """Host configuration paths are supported only within their source OS family."""
    if manifest["source_os"] != platform.system():
        raise DisasterRestoreError("restore on the same kind of system as the backup source")


def _rewrite_extracted_configs(dest: Path, manifest: dict[str, Any], mirror_directory: Path | None = None) -> None:
    """Rewrite restored configuration paths this backup can actually place.

    Auxiliary ledger fields follow the bundle resource list. Binding, vector
    and installation paths below are the store's own restored locations.
    """
    # Keep deletion protection at the supplied mirror; snapshot scheduling
    # stays disabled until the operator chooses a destination.
    backup_config = dest / "backup-config.json"
    backup_config.write_text(
        json.dumps(
            {
                "backup_dir": str(mirror_directory) if mirror_directory else None,
                "snapshots_enabled": False,
                "client_configs": [
                    str(dest / item["config_file"])
                    for item in manifest.get("resources", [])
                    if item["role"] == "remote_client"
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    configs = ["runtime-config.json"] + [
        f"entries/{entry['entry_id']}/runtime-config.json" for entry in manifest.get("entries", [])
    ]
    for name in configs:
        runtime_config_path = dest / name
        if not runtime_config_path.is_file():
            continue
        try:
            data = json.loads(runtime_config_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.pop("backup_dir", None)
                if "binding" in data and isinstance(data["binding"], dict):
                    data["binding"]["data_directory"] = str(dest)
                if "vector" in data and isinstance(data["vector"], dict) and "storage_dir" in data["vector"]:
                    auxiliary = AuxiliaryRuntimeConfig.from_mapping(
                        data.get("auxiliary") or {"external_embedding": False, "external_consolidation": False}
                    )
                    space = auxiliary.embedding.space() if auxiliary.embedding else EMBEDDING_SPACE
                    data["vector"]["storage_dir"] = str(dest / "vectors" / embedding_space_id(space))
                rewrite_budget_fields(data, dest, manifest.get("resources"), config_file=name)
                runtime_config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (json.JSONDecodeError, OSError, BackupError) as exc:
            raise DisasterRestoreError(f"failed rewriting runtime-config.json: {exc}") from exc


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


def _run_doctor_checks(layout: RestoreLayout) -> dict[str, Any]:
    """Run store diagnostics on the newly restored directory."""
    report = DoctorReport(host="hermes", status="ok")
    dest = layout.data
    readable = False
    if layout.host is not None:
        binding, _ = load_binding(layout.host, layout.root)
    else:
        payload = read_shared_payload(dest)
        binding = store_binding(payload, dest, frozenset(payload["scope_ids"]))
    try:
        check_runtime_config_present(report, dest)
        db_path = dest / "memory.sqlite3"
        if db_path.is_file():
            readable = check_storage(report, binding, dest, restoring=True)
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
        "binding_verified": readable,
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
        inspection = inspect_backup_archive(archive, manifest_check=_require_same_system)
        records = installation_mirror(mirror_path, inspection.manifest["installation_id"])
        layout = restore_layout(dest, inspection.manifest)
        fence_disaster_recovery(layout.data)
        write_verified_members(archive, dest, layout.members(inspection.members))
    manifest = inspection.manifest
    relocate_installation(layout)
    dest = layout.data
    _rewrite_extracted_configs(dest, manifest, mirror_path.parent)
    credential_references = relocate_remote_clients(dest, manifest.get("resources", []))

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
        with SQLiteStorage(binding)._transaction(context, writable=True, remaining_seconds=30, restoring=True) as tx:
            vector_rebuild_queued = tx.work.rebuild_lost_vectors(now=moment)

    mirror_result: dict[str, Any] = {"status": "none", "replayed_operations": 0, "latest_mirrored_at": None}
    if mirror_path is not None:
        try:
            binding = _restored_binding(dest)
            context = TrustedContext(binding, "disaster-restore", binding.scope_ids, "human_direct")
            mirror_result = replay_deletion_mirror(SQLiteStorage(binding), InstallationMaintenance(context), records)
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise DisasterRestoreError(f"failed replaying deletion mirror: {exc}") from exc

    entries = manifest.get("entries", [])
    reregistration_argv = reregistration_arguments(layout, entries)
    binding = _restored_binding(dest)
    context = TrustedContext(binding, "disaster-restore", binding.scope_ids, "human_direct")
    doctor_summary = _run_doctor_checks(layout)

    recovery_point_utc = manifest.get("created_at_utc", "")
    latest_mirrored_at = mirror_result.get("latest_mirrored_at")
    restore_moment = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")

    interval_start = recovery_point_utc
    try:
        t0 = datetime.fromisoformat(interval_start.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(restore_moment.replace("Z", "+00:00"))
        unrecoverable_seconds = max(0.0, (t1 - t0).total_seconds())
    except (ValueError, TypeError):
        unrecoverable_seconds = 0.0

    receipt = {
        "status": "restored",
        "bundle": str(safe_bundle),
        "destination": str(layout.root),
        "data_directory": str(dest),
        "recovery_point_utc": recovery_point_utc,
        "latest_mirrored_at": latest_mirrored_at,
        "mirror_replayed_operations": mirror_result.get("replayed_operations", 0),
        "deletion_protection": {
            "status": mirror_result["status"],
            "mirror_path": str(mirror_path),
            "installation_id": manifest["installation_id"],
            "latest_mirrored_at": latest_mirrored_at,
            "note": "All deletions in the supplied mirror were replayed; "
            "deletions absent from that copy cannot be recovered.",
        },
        "with_vectors": with_vectors,
        "vector_rebuild_queued": vector_rebuild_queued,
        "unrecoverable_later_interval": {
            "start_utc": interval_start,
            "end_utc": restore_moment,
            "duration_seconds": round(unrecoverable_seconds, 1),
            "note": "Regular captures and updates after recovery point are unrecoverable; "
            "available mirrored deletions were replayed.",
        },
        "reregistration_commands": [command_text(arguments) for arguments in reregistration_argv],
        "reregistration_argv": reregistration_argv,
        "credential_references": credential_references,
        "doctor": doctor_summary,
    }
    finish_disaster_recovery(
        SQLiteStorage(binding),
        InstallationMaintenance(context),
        before_reopen=lambda: atomic_json(dest / "recovery-receipt.json", receipt),
    )
    return receipt


__all__ = [
    "DisasterRestoreError",
    "apply_disaster_restore",
    "plan_disaster_restore",
]
