"""Backup rotation policy preserving 7 daily and 4 weekly verified backup bundles.

Bounded strictly to verified owned backup bundles using individual unlinks; never issues
recursive cleanup, bulk rm, or generic directory removal.
"""

from __future__ import annotations

import json
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .backup_bundle import BackupBundleError, verify_backup_bundle
from .backup_primitives import atomic_json, check_backup_deadline, first_link, safe_path

BUNDLE_FILENAME_PATTERN = re.compile(
    r"^scope-recall-backup-(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})T"
    r"(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})Z(?:-[0-9a-f]{12})?\.zip$"
)
BUNDLE_ALT_PATTERN = re.compile(
    r"^scope-recall-backup-(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})T(?P<hour>\d{2})-(?P<minute>\d{2})-(?P<second>\d{2})Z\.zip$"
)

DAILY_LIMIT = 7
WEEKLY_LIMIT = 4


@dataclass(frozen=True)
class BackupCandidate:
    path: Path
    timestamp: datetime
    filename: str
    signature: tuple[int, int, int, int]

    @property
    def date_key(self) -> str:
        return self.timestamp.strftime("%Y-%m-%d")

    @property
    def week_key(self) -> str:
        year, week, _ = self.timestamp.isocalendar()
        return f"{year}-W{week:02d}"


@dataclass(frozen=True)
class RotationResult:
    kept_daily: list[str]
    kept_weekly: list[str]
    removed: list[str]
    retained_total: int


def _parse_bundle_timestamp(filename: str) -> datetime | None:
    match = BUNDLE_FILENAME_PATTERN.match(filename) or BUNDLE_ALT_PATTERN.match(filename)
    if not match:
        return None
    d = match.groupdict()
    try:
        return datetime(
            year=int(d["year"]),
            month=int(d["month"]),
            day=int(d["day"]),
            hour=int(d["hour"]),
            minute=int(d["minute"]),
            second=int(d["second"]),
            tzinfo=UTC,
        )
    except ValueError:
        return None


def find_owned_backup_bundles(
    backup_directory: Path, installation_id: str, copy_id: str, *, progress_path: Path | None = None
) -> list[BackupCandidate]:
    """Find and validate owned backup bundle files in the backup directory.

    Rejects symlinks, directories, and non-conforming filenames.
    """
    safe_dir = safe_path(backup_directory, must_exist=True)
    if not safe_dir.is_dir():
        return []

    candidates: list[BackupCandidate] = []
    cache = _verification_progress(progress_path)
    for entry in safe_dir.iterdir():
        check_backup_deadline()
        if first_link(entry) is not None or not entry.is_file():
            continue
        ts = _parse_bundle_timestamp(entry.name)
        if ts is None:
            continue
        before = _signature(entry)
        try:
            saved = cache.get(str(entry), {})
            receipt = saved["receipt"] if saved.get("signature") == list(before) else verify_backup_bundle(entry)
            cache[str(entry)] = {"signature": list(before), "receipt": receipt}
            if progress_path is not None:
                atomic_json(progress_path, cache)
            recorded = datetime.fromisoformat(receipt["created_at_utc"].replace("Z", "+00:00"))
        except (BackupBundleError, OSError, ValueError, zipfile.BadZipFile):
            continue
        if (
            receipt.get("installation_id") == installation_id
            and receipt.get("copy_id") == copy_id
            and recorded == ts
            and before == _signature(entry)
        ):
            candidates.append(BackupCandidate(entry, ts, entry.name, before))

    candidates.sort(key=lambda c: c.timestamp, reverse=True)
    return candidates


def _verification_progress(path: Path | None) -> dict:
    """Resume archive verification only while its filesystem identity is unchanged."""
    if path is None or not path.exists():
        return {}
    try:
        value = json.loads(safe_path(path, must_exist=True).read_text(encoding="utf-8"))
        return value if type(value) is dict else {}
    except (OSError, ValueError):
        return {}


def _signature(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def rotate_backup_bundles(
    backup_directory: Path,
    *,
    installation_id: str,
    copy_id: str,
    now: datetime | None = None,
    progress_path: Path | None = None,
) -> RotationResult:
    """Apply 7 daily and 4 weekly retention policy to verified owned bundles.

    Removes obsolete bundles by individual file unlinks only.
    """
    candidates = find_owned_backup_bundles(backup_directory, installation_id, copy_id, progress_path=progress_path)
    if not candidates:
        return RotationResult(kept_daily=[], kept_weekly=[], removed=[], retained_total=0)

    daily_selected: dict[str, BackupCandidate] = {}
    weekly_selected: dict[str, BackupCandidate] = {}

    for cand in candidates:
        if cand.date_key not in daily_selected and len(daily_selected) < DAILY_LIMIT:
            daily_selected[cand.date_key] = cand

    for cand in candidates:
        if cand.week_key not in weekly_selected and len(weekly_selected) < WEEKLY_LIMIT:
            weekly_selected[cand.week_key] = cand

    keep_paths = {cand.path for cand in daily_selected.values()} | {cand.path for cand in weekly_selected.values()}

    removed: list[str] = []
    for cand in candidates:
        check_backup_deadline()
        if cand.path not in keep_paths:
            if cand.path.is_file() and first_link(cand.path) is None and _signature(cand.path) == cand.signature:
                os.unlink(cand.path)
                removed.append(cand.filename)

    remaining = [cand.filename for cand in candidates if cand.path in keep_paths]
    if progress_path is not None:
        cache = _verification_progress(progress_path)
        atomic_json(progress_path, {str(path): cache[str(path)] for path in keep_paths if str(path) in cache})

    return RotationResult(
        kept_daily=[cand.filename for cand in daily_selected.values()],
        kept_weekly=[cand.filename for cand in weekly_selected.values()],
        removed=removed,
        retained_total=len(remaining),
    )


__all__ = [
    "DAILY_LIMIT",
    "WEEKLY_LIMIT",
    "BackupCandidate",
    "RotationResult",
    "find_owned_backup_bundles",
    "rotate_backup_bundles",
]
