"""Daily snapshots and periodic mirror repair in the existing worker wake plan."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..core.deletion_mirror import MIRROR_ERRORS, catchup_deletion_mirror, configured_backup_dir
from .backup_bundle import create_backup_bundle
from .backup_primitives import BackupError, atomic_json, backup_deadline
from .backup_rotation import rotate_backup_bundles

RESERVE_SECONDS = 2.0
BACKUP_STATE_FILE = "backup-state.json"
MIRROR_INTERVAL_SECONDS = 300
BACKUP_RETRY_SECONDS = 900


def _read_state(directory: Path) -> dict:
    try:
        value = json.loads((directory / BACKUP_STATE_FILE).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _stamp(state: dict, key: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(state[key].replace("Z", "+00:00"))
        return value.astimezone(UTC) if value.tzinfo else None
    except (KeyError, AttributeError, TypeError, ValueError):
        return None


def _due_times(state: dict, now: datetime) -> tuple[datetime, datetime]:
    last_backup = _stamp(state, "last_daily_backup_utc")
    daily = (
        now
        if last_backup is None
        else (last_backup + timedelta(days=1)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    )
    failed = _stamp(state, "last_error_utc")
    if failed is not None:
        daily = max(daily, failed + timedelta(seconds=BACKUP_RETRY_SECONDS))
    mirrored = _stamp(state, "last_mirror_attempt_utc")
    mirror = now if mirrored is None else mirrored + timedelta(seconds=MIRROR_INTERVAL_SECONDS)
    return daily, mirror


def next_backup_wake(directory: Path, now: datetime) -> tuple[datetime, str] | None:
    """Wake an idle worker without consuming model or queue-item budgets."""
    try:
        if configured_backup_dir(directory) is None:
            return None
    except MIRROR_ERRORS:
        return None
    daily, mirror = _due_times(_read_state(directory), now)
    return min((daily, "daily_backup"), (mirror, "deletion_mirror"))


def _save_state(directory: Path, state: dict) -> None:
    try:
        atomic_json(directory / BACKUP_STATE_FILE, state)
    except OSError:
        # Failed persistence cannot claim success on a later pass; its due time
        # remains visible from the old state and SQLite mirror inventory.
        pass


def backup_if_due(storage, config, *, available_seconds: float, now: datetime | None = None) -> dict | None:
    """Run content-free catch-up and a daily snapshot, recording bounded retry times."""
    if available_seconds < RESERVE_SECONDS:
        return None
    deadline = time.monotonic() + available_seconds
    directory = storage.binding.data_directory
    now = now or datetime.now(UTC)
    stamp = now.isoformat().replace("+00:00", "Z")
    state = _read_state(directory)
    daily, mirror_due = _due_times(state, now)
    mirror = None
    try:
        backup_dir = configured_backup_dir(directory)
        if backup_dir is None:
            return None
        if now >= mirror_due:
            mirror = catchup_deletion_mirror(storage, backup_dir)
            state["last_mirror_attempt_utc"] = stamp
        if now < daily:
            _save_state(directory, state)
            return {"outcome": "skipped_not_due", "mirror": mirror}
        tag = hashlib.sha256(storage.binding.installation_id.encode()).hexdigest()[:12]
        filename = f"scope-recall-backup-{now.strftime('%Y%m%dT%H%M%SZ')}-{tag}.zip"
        output = backup_dir / filename
        with backup_deadline(deadline):
            receipt = create_backup_bundle(binding=storage.binding, output=output, now=now)
            rotation = rotate_backup_bundles(backup_dir, installation_id=storage.binding.installation_id)
        state.update(last_daily_backup_utc=stamp, last_bundle=str(output), last_error=None, last_error_utc=None)
        _save_state(directory, state)
        return {
            "outcome": "backed_up",
            "bundle": str(output),
            "mirror": mirror,
            "manifest_sha256": receipt["manifest_sha256"],
            "retained_bundles": rotation.retained_total,
        }
    except (*MIRROR_ERRORS, BackupError) as exc:
        state.update(last_error=str(exc), last_error_utc=stamp, last_mirror_attempt_utc=stamp)
        _save_state(directory, state)
        return {"outcome": "failed", "error": str(exc), "mirror": mirror}
