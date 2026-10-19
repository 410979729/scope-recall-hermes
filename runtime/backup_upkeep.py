"""Automatic daily backup creation, rotation and deletion mirror catchup during worker upkeep.

Scheduled during worker drain passes, running before queue processing to ensure durable
recovery points without starving on busy instances.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.deletion_mirror import catchup_deletion_mirror, configured_backup_dir
from ..core.storage import SQLiteStorage
from .backup_bundle import create_backup_bundle
from .backup_rotation import rotate_backup_bundles

RESERVE_SECONDS = 2.0
BACKUP_STATE_FILE = "backup-state.json"


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_backup_state(data_directory: Path) -> dict[str, Any]:
    state_path = data_directory / BACKUP_STATE_FILE
    if not state_path.is_file():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_backup_state(data_directory: Path, state: dict[str, Any]) -> None:
    state_path = data_directory / BACKUP_STATE_FILE
    tmp_path = data_directory / f".{BACKUP_STATE_FILE}.tmp"
    try:
        tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(state_path)
    except OSError:
        pass


def backup_if_due(
    storage: SQLiteStorage,
    config: Any,
    *,
    available_seconds: float,
) -> dict[str, Any] | None:
    """Run daily backup bundle creation, rotation and mirror catch-up when due.

    Never raises: any failure is recorded in backup-state.json and returned as an outcome
    to prevent worker drain degradation.
    """
    if available_seconds < RESERVE_SECONDS:
        return None

    data_directory = storage.binding.data_directory
    backup_dir = configured_backup_dir(data_directory)
    if backup_dir is None:
        return None

    mirror_status: dict[str, Any] | None = None
    try:
        mirror_status = catchup_deletion_mirror(storage, backup_dir)
    except Exception as exc:
        mirror_status = {"outcome": "error", "error": str(exc)}

    now_stamp = _now_utc()
    today_date = now_stamp[:10]

    state = _read_backup_state(data_directory)
    last_backup = str(state.get("last_daily_backup_utc", ""))
    if last_backup.startswith(today_date):
        return {
            "outcome": "skipped_not_due",
            "last_daily_backup_utc": last_backup,
            "mirror": mirror_status,
        }

    ts_slug = now_stamp.replace("-", "").replace(":", "")
    bundle_filename = f"scope-recall-backup-{ts_slug}.zip"
    output_bundle = backup_dir / bundle_filename

    try:
        bundle_receipt = create_backup_bundle(
            binding=storage.binding,
            output=output_bundle,
            with_vectors=False,
        )
        rotation_receipt = rotate_backup_bundles(backup_dir)

        state.update(
            last_daily_backup_utc=now_stamp,
            last_bundle=str(output_bundle),
            manifest_sha256=bundle_receipt.get("manifest_sha256"),
            retained_bundles=rotation_receipt.retained_total,
            last_error=None,
        )
        _write_backup_state(data_directory, state)

        return {
            "outcome": "backed_up",
            "bundle": str(output_bundle),
            "manifest_sha256": bundle_receipt.get("manifest_sha256"),
            "retained_bundles": rotation_receipt.retained_total,
            "mirror": mirror_status,
        }
    except Exception as exc:
        state.update(
            last_error=str(exc),
            last_error_utc=now_stamp,
        )
        _write_backup_state(data_directory, state)
        return {
            "outcome": "failed",
            "error": str(exc),
            "mirror": mirror_status,
        }


__all__ = ["backup_if_due"]
