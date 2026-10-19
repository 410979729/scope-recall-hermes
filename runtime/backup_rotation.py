"""Backup rotation policy preserving 7 daily and 4 weekly verified backup bundles.

Bounded strictly to verified owned backup bundles using individual unlinks; never issues
recursive cleanup, bulk rm, or generic directory removal.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .backup_primitives import first_link, safe_path

BUNDLE_FILENAME_PATTERN = re.compile(
    r"^scope-recall-backup-(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})T(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})Z\.zip$"
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


def find_owned_backup_bundles(backup_directory: Path) -> list[BackupCandidate]:
    """Find and validate owned backup bundle files in the backup directory.

    Rejects symlinks, directories, and non-conforming filenames.
    """
    safe_dir = safe_path(backup_directory, must_exist=True)
    if not safe_dir.is_dir():
        return []

    candidates: list[BackupCandidate] = []
    for entry in sorted(safe_dir.iterdir()):
        if first_link(entry) is not None or not entry.is_file():
            continue
        ts = _parse_bundle_timestamp(entry.name)
        if ts is not None:
            candidates.append(BackupCandidate(path=entry, timestamp=ts, filename=entry.name))

    candidates.sort(key=lambda c: c.timestamp, reverse=True)
    return candidates


def rotate_backup_bundles(
    backup_directory: Path,
    *,
    now: datetime | None = None,
) -> RotationResult:
    """Apply 7 daily and 4 weekly retention policy to verified owned bundles.

    Removes obsolete bundles by individual file unlinks only.
    """
    candidates = find_owned_backup_bundles(backup_directory)
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
        if cand.path not in keep_paths:
            if cand.path.is_file() and first_link(cand.path) is None:
                os.unlink(cand.path)
                removed.append(cand.filename)

    remaining = [cand.filename for cand in candidates if cand.path in keep_paths]

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
