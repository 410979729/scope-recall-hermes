"""What version each live process actually loaded, as opposed to what is on disk.

The gap this closes was measured, not imagined: on 2026-09-13 the TianShu
gateway started at 04:14 and the package was reinstalled at 01:34 the next day,
but the gateway is a long-running process that imports ``scope_recall`` once at
startup.  For 21 hours the same function behaved one way inside the gateway
(old code) and another way inside each worker drain (new code, fresh process
every time).  Nothing reported it: the installer does not look at processes and
doctor only looked at disk.  It was found by bisecting evidence byte counts.

So every process that opens a runtime instance leaves one small record saying
which version it is holding.  ``maintenance/doctor.py`` reads those records,
keeps the ones whose process is still alive, and compares them against the
package on disk.

Two facts are recorded because two different things go wrong:

* ``version`` -- the in-memory ``__version__``.  Bound at import, so it cannot
  drift; a mismatch against disk is proof of a stale process.
* ``first_record_at`` -- when this process first wrote its record, which is
  necessarily *after* it imported the package.  If the package on disk was
  modified later than that, this process demonstrably loaded something else,
  even when the version string is unchanged.  Reinstalling the same release
  candidate is the ordinary development loop, so this is the common case.

Both tests are exact.  Neither can fire for a process that is genuinely current,
which matters more than catching every exotic case: a diagnostic that cries wolf
gets ignored, and then the real signal is lost with it.

When a process appears here, stated rather than left to be rediscovered: at
instance construction, not at import.  For a host adapter that is earlier than
it sounds -- ``initialize(session_id)`` binds an identity and
``attach_trusted_host_runtime`` builds the instance right there, so a gateway
registers when a *conversation starts*, before it recalls or captures anything.

The window that remains is a host that has started and had no conversation yet.
It is also doing no memory work in that window, so nothing is missed; but "no
records" means "nothing has registered yet", not "everything is current", and
the doctor says so in those words rather than reporting ok.

Not responsible for: deciding what to do about a stale process (doctor reports,
the operator restarts), or for any form of process control.
"""
from __future__ import annotations

import json
import os
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

    A stamp meaningfully in the future is ignored rather than believed.
    Nothing can have been modified later than now, so such a stamp is a broken
    clock or an unpacking tool that mishandled the archive's local-time entries
    -- and believing it would make *every* process look stale forever, which is
    the permanently-degraded health check this whole module exists to avoid.
    The real TianShu case was an extraction that shifted 31 of 135 files four
    hours ahead.

    "Meaningfully" is the whole point of the tolerance: a file written a
    moment ago can carry a stamp a hair past ``time.time()`` -- filesystem
    timestamp granularity, or a clock that ticked between the write and the
    read -- and treating that as evidence of a broken clock would discard the
    ordinary rebuild-and-reinstall case this check exists to catch.
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


def stale_records(
    data_directory: Path,
    *,
    disk_version: str,
    package_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Live processes provably not running the code now on disk.

    Each entry names its reason so a reader does not have to re-derive it.
    """
    package_modified = package_modified_at(package_path) if package_path is not None else None
    stale: list[dict[str, Any]] = []
    for record in live_records(data_directory):
        if record.version != disk_version:
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
                "disk_version": disk_version,
                "first_record_at": record.first_record_at,
                "package_modified_at": package_modified,
                "host_adapter": record.host_adapter,
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
