"""What version each live process actually loaded, as opposed to what is on disk.

A long-running host imports ``scope_recall`` once; after a reinstall it keeps
running the old code while every fresh worker runs the new one, and neither the
installer nor a disk-only doctor can see it.  So every process that opens a
runtime instance leaves one small record, and ``maintenance/doctor.py`` compares
the live ones against the package on disk.

Two facts are recorded because two different things go wrong:

* ``version`` -- the in-memory ``__version__``, bound at import; a mismatch
  against disk is proof of a stale process.
* ``first_record_at`` -- when this process first wrote its record, necessarily
  after it imported the package.  A package modified later than that is not
  what this process loaded, even when the version string is unchanged, which
  is the ordinary reinstall-the-same-candidate case.

Both tests are exact; neither can fire for a process that is genuinely current.
A diagnostic that cries wolf gets ignored, and the real signal with it.

Records are written at instance construction, not at import: a host adapter
registers when a conversation starts, before it recalls or captures anything.
A host with no conversation yet has no record, and "no records" means "nothing
has registered yet", not "everything is current".

Not responsible for: acting on a stale process (doctor reports, the operator
restarts), or any form of process control.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any, Iterator

from .._version import __version__
from .process_probe import is_same_process, probe_process

#: Directory under the instance data directory holding one file per process.
RUNNING_CODE_DIRNAME = "running-code"
#: Bumped only when the record shape changes in a way readers must notice.
RECORD_SCHEMA = "scope-recall.running-code.v1"
#: Upper bound on records examined in one pass.  An instance has a handful of
#: core-holding processes; anything beyond this is leftover, and an unbounded
#: scan inside a health check is its own outage.
MAX_RECORDS = 64

#: A modification stamp further ahead than this is not believed. Small skew is
#: ordinary; the failure this guards against was four hours.
FUTURE_TOLERANCE_SECONDS = 60


@dataclass(frozen=True)
class RunningCodeRecord:
    schema: str
    pid: int
    version: str
    first_record_at: str
    recorded_at: str
    package_path: str
    start_token: str | None = None
    host_adapter: str | None = None
    installation_id: str | None = None
    python_executable: str | None = None

    @classmethod
    def from_mapping(cls, raw: Any) -> "RunningCodeRecord | None":
        """Return ``None`` for anything unreadable rather than raising.

        These files are diagnostic breadcrumbs.  A truncated one -- a process
        killed mid-write, a half-synced directory -- must not be able to take
        down the health check that reads it.
        """
        if not isinstance(raw, dict) or raw.get("schema") != RECORD_SCHEMA:
            return None
        try:
            return cls(
                schema=RECORD_SCHEMA,
                pid=int(raw["pid"]),
                version=str(raw["version"]),
                first_record_at=str(raw["first_record_at"]),
                recorded_at=str(raw["recorded_at"]),
                package_path=str(raw["package_path"]),
                start_token=None if raw.get("start_token") is None else str(raw["start_token"]),
                host_adapter=None if raw.get("host_adapter") is None else str(raw["host_adapter"]),
                installation_id=None if raw.get("installation_id") is None else str(raw["installation_id"]),
                python_executable=(
                    None if raw.get("python_executable") is None else str(raw["python_executable"])
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def running_code_dir(data_directory: Path) -> Path:
    return Path(data_directory) / RUNNING_CODE_DIRNAME


#: Set once this process has written its record, so repeated instance opens do
#: not repeatedly touch the filesystem.  The recorded facts cannot change within
#: a process, so there is nothing to refresh.
_written: Path | None = None
_first_record_at: str | None = None


def record_running_code(
    data_directory: Path,
    *,
    host_adapter: str | None = None,
    installation_id: str | None = None,
) -> Path | None:
    """Leave this process's record, and retire the records of dead processes.

    Returns the record path, or ``None`` when it could not be written.  Never
    raises: an instance must still open on a read-only or full disk, because a
    diagnostic aid that can deny service is worse than no diagnostic aid.
    """
    global _written, _first_record_at
    directory = running_code_dir(data_directory)
    target = directory / f"{os.getpid()}.json"
    if _written == target and target.exists():
        return target
    if _first_record_at is None:
        _first_record_at = _now()
    record = RunningCodeRecord(
        schema=RECORD_SCHEMA,
        pid=os.getpid(),
        version=__version__,
        first_record_at=_first_record_at,
        recorded_at=_now(),
        package_path=str(_package_root()),
        start_token=probe_process(os.getpid()).start_token,
        host_adapter=host_adapter,
        installation_id=installation_id,
        python_executable=sys.executable or None,
    )
    try:
        directory.mkdir(parents=True, exist_ok=True)
        partial = directory / f"{os.getpid()}.json.partial"
        partial.write_text(json.dumps(asdict(record), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(partial, target)
    except OSError:
        return None
    _written = target
    _retire_dead_records(directory, keep=target)
    return target


def _retire_dead_records(directory: Path, *, keep: Path) -> None:
    """Delete records whose process is gone.  Best effort, bounded, never raises."""
    for path, record in _iter_records(directory):
        if path == keep:
            continue
        if record is not None and is_same_process(record.pid, record.start_token):
            continue
        try:
            path.unlink()
        except OSError:
            pass


def _iter_records(directory: Path) -> Iterator[tuple[Path, RunningCodeRecord | None]]:
    try:
        paths = sorted(directory.glob("*.json"))[:MAX_RECORDS]
    except OSError:
        return
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            yield path, None
            continue
        yield path, RunningCodeRecord.from_mapping(raw)


def live_records(data_directory: Path) -> list[RunningCodeRecord]:
    """Records whose process is still the one that wrote them.  Read-only."""
    live: list[RunningCodeRecord] = []
    for _path, record in _iter_records(running_code_dir(data_directory)):
        if record is not None and is_same_process(record.pid, record.start_token):
            live.append(record)
    return live


def package_modified_at(package_path: Path, *, now: float | None = None) -> str | None:
    """Newest modification time across the package's Python sources.

    Compared against ``first_record_at`` to catch a reinstall that did not
    change the version string.  Bounded to ``*.py`` because those are exactly
    what an already-running interpreter cannot pick up.

    A stamp meaningfully in the future is ignored rather than believed: it is
    a broken clock or an archive extracted with mishandled local-time entries,
    and believing it would make every process look stale forever.  The
    tolerance exists because a file written a moment ago can legitimately
    carry a stamp a hair past ``time.time()``.
    """
    ceiling = (time.time() if now is None else now) + FUTURE_TOLERANCE_SECONDS
    newest: float | None = None
    try:
        for path in Path(package_path).rglob("*.py"):
            try:
                stamp = path.stat().st_mtime
            except OSError:
                continue
            if stamp > ceiling:
                continue
            if newest is None or stamp > newest:
                newest = stamp
    except OSError:
        return None
    if newest is None:
        return None
    return datetime.fromtimestamp(newest, timezone.utc).isoformat()


_VERSION_LINE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.MULTILINE)


def _version_on_disk(package_path: Path) -> str | None:
    """The version a restart would load from ``package_path``; ``None`` when it cannot be read."""
    try:
        match = _VERSION_LINE.search((package_path / "_version.py").read_text(encoding="utf-8"))
    except OSError:
        return None
    return match.group(1) if match else None


def _same_folder(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(os.path.normpath(str(right)))


def stale_records(
    data_directory: Path,
    *,
    disk_version: str,
    package_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Live processes provably not running the code now on disk.

    ``disk_version`` and ``package_path`` describe the caller's own package.  A
    shared store is written by every entry's host and by its worker, each from
    its own environment, so a record that names another package folder is
    judged against that folder: upgrading one entry says nothing about the
    code another entry's process loaded.  When that folder's version cannot be
    read, the record is compared with ``disk_version`` alone, as before.

    Each entry names its reason so a reader does not have to re-derive it.
    """
    modified: dict[str, str | None] = {}

    def modified_at(folder: Path) -> str | None:
        if str(folder) not in modified:
            modified[str(folder)] = package_modified_at(folder)
        return modified[str(folder)]

    stale: list[dict[str, Any]] = []
    for record in live_records(data_directory):
        folder: Path | None = package_path
        version = disk_version
        if package_path is not None and not _same_folder(record.package_path, package_path):
            foreign = _version_on_disk(Path(record.package_path))
            folder = Path(record.package_path) if foreign is not None else None
            version = foreign or disk_version
        package_modified = modified_at(folder) if folder is not None else None
        if record.version != version:
            reason = "version_mismatch"
        elif package_modified is not None and package_modified > record.first_record_at:
            reason = "package_rewritten_after_load"
        else:
            continue
        stale.append(
            {
                "pid": record.pid,
                "reason": reason,
                "loaded_version": record.version,
                "disk_version": version,
                "first_record_at": record.first_record_at,
                "package_modified_at": package_modified,
                "host_adapter": record.host_adapter,
                "package_path": record.package_path,
            }
        )
    return stale


__all__ = [
    "MAX_RECORDS",
    "RECORD_SCHEMA",
    "RUNNING_CODE_DIRNAME",
    "RunningCodeRecord",
    "live_records",
    "package_modified_at",
    "record_running_code",
    "running_code_dir",
    "stale_records",
]
