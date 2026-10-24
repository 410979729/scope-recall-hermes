"""Daily snapshots and periodic mirror repair in the existing worker wake plan."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..core.deletion_mirror import (
    MIRROR_ERRORS,
    catchup_deletion_mirror,
    configured_backup_dir,
    scheduled_snapshots_enabled,
)
from ..core.file_lock import advisory_file_lock
from ..core.installation_copy import installation_copy_id
from .backup_bundle import create_backup_bundle
from .backup_primitives import BackupError, atomic_json, backup_deadline, check_backup_deadline
from .backup_rotation import rotate_backup_bundles

RESERVE_SECONDS = 2.0
BACKUP_STATE_FILE = "backup-state.json"
MIRROR_INTERVAL_SECONDS = 300
BACKUP_RETRY_SECONDS = 900
BACKUP_JOB_SECONDS = 1800.0


def read_backup_state(directory: Path) -> dict:
    """Read scheduler status without changing it; absent or unreadable state is empty."""
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
    if state.get("backup_blocked"):
        daily = datetime.max.replace(tzinfo=UTC)
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
    state = read_backup_state(directory)
    daily, mirror = _due_times(state, now)
    candidates = [(mirror, "deletion_mirror")]
    try:
        snapshots = scheduled_snapshots_enabled(directory)
    except MIRROR_ERRORS:
        # The planner never fails on this file; the scheduled job records the error.
        snapshots = True
    if snapshots and not state.get("backup_blocked"):
        candidates.append((daily, "daily_backup"))
    if state.get("retention_pending"):
        failed = _stamp(state, "last_retention_error_utc")
        candidates.append((failed + timedelta(seconds=BACKUP_RETRY_SECONDS) if failed else now, "backup_retention"))
    if due := [item for item in candidates if item[1] != "deletion_mirror" and item[0] <= now]:
        return min(due)
    return min(candidates)


def _save_state(directory: Path, state: dict) -> None:
    """Merge metadata under a short lock so mirror upkeep cannot erase publication."""
    with advisory_file_lock(directory / "backup-state.lock", timeout_seconds=2):
        current = read_backup_state(directory)
        current.update(state)
        atomic_json(directory / BACKUP_STATE_FILE, current)


def record_backup_job_failure(directory: Path, error: str) -> None:
    """Persist a stopped owned job without losing its publication checkpoint."""
    state = read_backup_state(directory)
    stamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    if state.get("job_phase") == "retention":
        changes = dict(last_retention_error=error, last_retention_error_utc=stamp)
    else:
        changes = dict(last_error=error, last_error_utc=stamp, backup_blocked="deadline" in error)
    _save_state(directory, changes)


def backup_job_problem(directory: Path) -> str | None:
    """Explain a scheduled backup that requires operator action or retention repair."""
    state = read_backup_state(directory)
    if state.get("backup_blocked"):
        return f"scheduled backup exceeded its {BACKUP_JOB_SECONDS:g} second budget; automatic snapshots are paused"
    if state.get("last_retention_error"):
        return f"archive published; retention pending: {state['last_retention_error']}"
    return None


def mirror_if_due(storage, config, *, available_seconds: float, now: datetime | None = None) -> dict | None:
    """Repair deletion protection within a short portion of ordinary queue work."""
    if available_seconds < RESERVE_SECONDS:
        return None
    directory = storage.binding.data_directory
    now = now or datetime.now(UTC)
    state = read_backup_state(directory)
    try:
        backup_dir = configured_backup_dir(directory)
    except MIRROR_ERRORS as exc:
        # A hand-edited configuration error is reported with each pass instead of stopping queue work.
        return {"outcome": "failed", "error": f"backup configuration unreadable: {type(exc).__name__}"}
    if backup_dir is None or now < _due_times(state, now)[1]:
        return None
    try:
        deadline = time.monotonic() + min(available_seconds / 2, 5.0)
        with backup_deadline(deadline):
            result = catchup_deletion_mirror(storage, backup_dir, check=check_backup_deadline)
        return result
    except (*MIRROR_ERRORS, BackupError) as exc:
        return {"outcome": "failed", "error": str(exc)}
    finally:
        _save_state(directory, {"last_mirror_attempt_utc": now.isoformat().replace("+00:00", "Z")})


def _retention(storage, backup_dir: Path, state: dict) -> dict:
    directory = storage.binding.data_directory
    rotation = rotate_backup_bundles(
        backup_dir,
        installation_id=storage.binding.installation_id,
        copy_id=installation_copy_id(directory, storage.binding.installation_id),
        progress_path=directory / "backup-retention.json",
    )
    state.update(retention_pending=False, job_phase=None, last_retention_error=None, last_retention_error_utc=None)
    _save_state(directory, state)
    return {"retained_bundles": rotation.retained_total}


def _publish(storage, backup_dir: Path, state: dict, now: datetime) -> dict:
    directory = storage.binding.data_directory
    copy_id = installation_copy_id(directory, storage.binding.installation_id)
    tag = hashlib.sha256(copy_id.encode()).hexdigest()[:12]
    output = backup_dir / f"scope-recall-backup-{now.strftime('%Y%m%dT%H%M%SZ')}-{tag}.zip"
    state["job_phase"] = "publication"
    _save_state(directory, state)
    receipt = create_backup_bundle(binding=storage.binding, output=output, now=now)
    state.update(
        last_daily_backup_utc=now.isoformat().replace("+00:00", "Z"),
        last_bundle=str(output),
        last_error=None,
        last_error_utc=None,
        backup_blocked=False,
        retention_pending=True,
        job_phase="retention",
    )
    _save_state(directory, state)
    return {"bundle": str(output), "manifest_sha256": receipt["manifest_sha256"]}


def backup_if_due(storage, config, *, available_seconds: float, now: datetime | None = None) -> dict | None:
    """Run one independent snapshot job; publication and retention checkpoint separately."""
    if available_seconds < RESERVE_SECONDS:
        return None
    deadline = time.monotonic() + available_seconds - RESERVE_SECONDS
    directory = storage.binding.data_directory
    now = now or datetime.now(UTC)
    stamp = now.isoformat().replace("+00:00", "Z")
    state = read_backup_state(directory)
    daily, mirror_due = _due_times(state, now)
    mirror = None
    try:
        backup_dir = configured_backup_dir(directory)
        if backup_dir is None:
            return None
        if now >= mirror_due:
            with backup_deadline(deadline):
                mirror = catchup_deletion_mirror(storage, backup_dir, check=check_backup_deadline)
            state["last_mirror_attempt_utc"] = stamp
        if not scheduled_snapshots_enabled(directory):
            _save_state(directory, state)
            return {"outcome": "snapshots_disabled", "mirror": mirror}
        if now < daily and not state.get("retention_pending"):
            _save_state(directory, state)
            return {"outcome": "skipped_not_due", "mirror": mirror}
        with backup_deadline(deadline):
            receipt = _publish(storage, backup_dir, state, now) if now >= daily else {}
            rotation = _retention(storage, backup_dir, state)
        return {"outcome": "backed_up" if receipt else "retained", "mirror": mirror, **receipt, **rotation}
    except (*MIRROR_ERRORS, BackupError) as exc:
        if state.get("job_phase") == "retention":
            state.update(last_retention_error=str(exc), last_retention_error_utc=stamp)
        else:
            state.update(last_error=str(exc), last_error_utc=stamp, backup_blocked="deadline" in str(exc))
        state["last_mirror_attempt_utc"] = stamp
        _save_state(directory, state)
        return {"outcome": "failed", "error": str(exc), "mirror": mirror}
