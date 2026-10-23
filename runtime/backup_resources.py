"""Store resources a full backup can name from the instance's own runtime config.

Kept captures are the retry files beside the store. The auxiliary budget ledger
is the SQLite file named by ``runtime-config.json``, or the file beside the
store when that config has no auxiliary section. Restore mappings name only
those budget fields. Other config fields are left untouched.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..core import private
from ..core.file_lock import advisory_file_lock
from .auxiliary import DEFAULT_LEDGER_NAME
from .backup_archive import ArchiveLayoutError, canonical_member_name
from .backup_primitives import (
    BackupError,
    backup_sqlite,
    check_backup_deadline,
    copy_stream,
    first_link,
    safe_path,
    sha256,
)
from .remote_clock import private_metadata, stored_session_id
from .validation import validate_worker_day

#: Directory of Hermes kept captures beside the store (``kept_captures.DIRECTORY``).
KEPT_CAPTURES_DIRECTORY = "kept-captures"
#: One restored ledger for both the default and an explicit source path.
BUDGET_DIRECTORY = "budgets"
BUDGET_ARCHIVE_PATH = "budgets/budget1.sqlite3"
RUNTIME_CONFIG_NAME = "runtime-config.json"
LEDGER_FIELD = "auxiliary.ledger_path"
INSTALLATION_DIR_FIELD = "auxiliary.installation_dir"

_BUDGET_KEYS = {
    LEDGER_FIELD: "ledger_path",
    INSTALLATION_DIR_FIELD: "installation_dir",
}


class BackupPrivacy:
    """Check deferred bodies against the bundle's interval snapshot without changing originals."""

    def __init__(self, database: Path, *, entry_ids: tuple[str, ...] = ()):
        self.connection = sqlite3.connect(database)
        self.connection.row_factory = sqlite3.Row
        self.has_rules = self.connection.execute("SELECT 1 FROM private_intervals LIMIT 1").fetchone() is not None
        self.entry_ids = entry_ids

    def close(self):
        """Release the staged database connection before hashing and archiving it."""
        self.connection.close()

    def permits(
        self, session, project, events, *, project_identity: str = "local", project_unverified: bool = False
    ) -> bool:
        """An unknown event time is ordinary only when no applicable history could cover it."""
        if not self.has_rules:
            return True
        if not isinstance(session, str) or not session or not events:
            return False
        context = SimpleNamespace(session_id=session, project_root=project, project_identity=project_identity)
        intervals = private.matching_connection_intervals(
            self.connection, context, events=events, project_unverified=project_unverified
        )
        return private.capture_mode(intervals, events) is None

    def permits_kept(self, value) -> bool:
        """A kept capture or inbox entry uses its original context and event time."""
        if not self.has_rules:
            return True
        if not isinstance(value, dict) or not isinstance(value.get("context"), dict):
            return False
        events = value.get("events") if "events" in value else [value.get("event")]
        if not isinstance(events, (list, tuple)) or not events or any(not isinstance(event, dict) for event in events):
            return False
        context = value["context"]
        return self.permits(
            context.get("session_id"),
            context.get("project_root"),
            events,
            project_identity=context.get("project_identity", "local"),
        )

    def permits_remote(self, value) -> bool:
        """A remote spool preserves per-record calibration; upload clocks never date history."""
        if not self.has_rules:
            return True
        if not isinstance(value, dict) or not isinstance(value.get("payload"), dict):
            return False
        metadata = private_metadata(value)
        events = []
        for moment, clock in zip(metadata["times"], metadata["clocks"], strict=True):
            span = private.calibrated_event_time(moment, clock)
            events.append(
                dict(occurred_at=span["earliest"], occurred_until=span["latest"]) if span else dict(occurred_at=None)
            )
        # Client configs need not identify one entry. Refuse if any possible
        # entry makes the record private; unrelated raw sessions stay ordinary.
        sessions = [metadata["session_id"]]
        if isinstance(metadata["session_id"], str):
            sessions.extend(stored_session_id(metadata["session_id"], entry_id=entry) for entry in self.entry_ids)
        return all(
            self.permits(
                session,
                metadata["project"],
                events or [dict(occurred_at=None)],
                project_identity="remote",
                project_unverified=metadata["project_unverified"],
            )
            for session in sessions
        )

    def filter_inbox(self):
        """Erase refused queued bodies only in the snapshot, including freed SQLite pages."""
        if not self.has_rules:
            return
        refused = []
        for token, encoded in self.connection.execute("SELECT token,payload_json FROM capture_inbox"):
            try:
                allowed = self.permits_kept(json.loads(encoded))
            except (ValueError, TypeError):
                allowed = False
            if not allowed:
                refused.append((token,))
        if refused:
            self.connection.execute("PRAGMA secure_delete=ON")
            self.connection.executemany("DELETE FROM capture_inbox WHERE token=?", refused)
            self.connection.commit()
            self.connection.execute("VACUUM")


def _absolute_path(value: object, field: str) -> Path:
    if type(value) is not str or not value.strip():
        raise BackupError(f"{field} must be an absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise BackupError(f"{field} must be an absolute path")
    return path


def _read_runtime_config(data_directory: Path) -> dict[str, Any] | None:
    """The store's own runtime config, or None when that file is absent.

    A symlink or junction is refused. A config in a parent directory is not
    this store's auxiliary ledger.
    """
    config_path = data_directory / RUNTIME_CONFIG_NAME
    if not config_path.exists():
        return None
    checked = safe_path(config_path, must_exist=True)
    if not checked.is_file():
        return None
    try:
        raw = json.loads(checked.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"failed reading runtime-config.json: {exc}") from exc
    if type(raw) is not dict:
        raise BackupError("runtime-config.json must be an object")
    return raw


def _auxiliary_ledger(data_directory: Path) -> tuple[bool, Path | None]:
    """Whether auxiliary config names a ledger, and the path it names.

    ``ledger_path`` wins. Without it, the ledger is
    ``installation_dir / auxiliary-budget.sqlite3``. Auxiliary config with
    neither field does not fall back to a file beside the store.
    """
    raw = _read_runtime_config(data_directory)
    if raw is None:
        return False, None
    auxiliary = raw.get("auxiliary")
    if type(auxiliary) is not dict:
        return False, None
    ledger = auxiliary.get("ledger_path")
    if ledger not in (None, ""):
        return True, _absolute_path(ledger, LEDGER_FIELD)
    installation = auxiliary.get("installation_dir")
    if installation not in (None, ""):
        directory = _absolute_path(installation, INSTALLATION_DIR_FIELD)
        return True, directory / DEFAULT_LEDGER_NAME
    return True, None


def _existing_ledger(path: Path) -> Path:
    """Refuse a missing ledger, and a symlink or junction anywhere on its path."""
    if not path.exists():
        raise BackupError("budget ledger is missing")
    checked = safe_path(path, must_exist=True)
    if not checked.is_file():
        raise BackupError("budget ledger is missing")
    return checked


def budget_ledger_source(data_directory: Path) -> tuple[Path | None, bool]:
    """The ledger file to snapshot, and whether runtime config names its fields.

    The second value is true only when auxiliary config names a location, so
    restore can rewrite those fields. With no auxiliary config, a regular file
    at ``data_directory / auxiliary-budget.sqlite3`` is included and no field
    is rewritten.
    """
    configured, path = _auxiliary_ledger(data_directory)
    if configured:
        if path is None:
            return None, False
        return _existing_ledger(path), True
    fallback = data_directory / DEFAULT_LEDGER_NAME
    if not fallback.exists():
        return None, False
    return _existing_ledger(fallback), False


def _file_entry(archive_path: str, staged: Path) -> dict[str, Any]:
    return {"path": archive_path, "sha256": sha256(staged), "size_bytes": staged.stat().st_size}


def _resource(
    role: str,
    archive_path: str,
    target_relative: str,
    *,
    field: str | None,
) -> dict[str, Any]:
    canonical_member_name(archive_path)
    canonical_member_name(target_relative)
    return {
        "role": role,
        "archive_path": archive_path,
        "target_relative": target_relative,
        "config_file": RUNTIME_CONFIG_NAME if field else None,
        "field": field,
    }


def _copy_regular(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as incoming, destination.open("wb") as outgoing:
        copy_stream(incoming, outgoing)


def real_files(root: Path) -> list[Path]:
    """Files under ``root`` whose own path is not a symlink or junction.

    Linked children are skipped. The walk does not descend through them.
    """
    found: list[Path] = []
    pending = [root]
    while pending:
        check_backup_deadline()
        current = pending.pop()
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise BackupError(f"failed reading kept captures: {exc}") from exc
        for child in children:
            if child.is_symlink() or first_link(child) is not None:
                continue
            if child.is_dir():
                pending.append(child)
            elif child.is_file():
                found.append(child)
    return sorted(found)


def _stage_kept_captures(
    data_directory: Path,
    stage_dir: Path,
    files: list[dict[str, Any]],
    resources: list[dict[str, Any]],
    privacy: BackupPrivacy | None = None,
) -> None:
    root = data_directory / KEPT_CAPTURES_DIRECTORY
    if not root.exists():
        return
    checked = safe_path(root, must_exist=True)
    if not checked.is_dir():
        return
    for item in real_files(checked):
        if privacy is not None and privacy.has_rules:
            try:
                if not privacy.permits_kept(json.loads(item.read_bytes())):
                    continue
            except (OSError, ValueError, TypeError):
                continue
        relative = f"{KEPT_CAPTURES_DIRECTORY}/{item.relative_to(checked).as_posix()}"
        try:
            canonical_member_name(relative)
        except ArchiveLayoutError as exc:
            raise BackupError(str(exc)) from exc
        destination = stage_dir / relative
        _copy_regular(item, destination)
        files.append(_file_entry(relative, destination))
        resources.append(_resource("kept_captures", relative, relative, field=None))


def _stage_budget(
    data_directory: Path,
    stage_dir: Path,
    files: list[dict[str, Any]],
    resources: list[dict[str, Any]],
) -> None:
    source, rewrite_config = budget_ledger_source(data_directory)
    if source is None:
        return
    destination = stage_dir / BUDGET_ARCHIVE_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup_sqlite(source, destination)
    files.append(_file_entry(BUDGET_ARCHIVE_PATH, destination))
    if rewrite_config:
        resources.append(_resource("budget_ledger", BUDGET_ARCHIVE_PATH, BUDGET_ARCHIVE_PATH, field=LEDGER_FIELD))
        resources.append(
            _resource(
                "budget_installation_dir",
                BUDGET_ARCHIVE_PATH,
                BUDGET_DIRECTORY,
                field=INSTALLATION_DIR_FIELD,
            )
        )
        return
    resources.append(_resource("budget_ledger", BUDGET_ARCHIVE_PATH, BUDGET_ARCHIVE_PATH, field=None))


def stage_instance_resources(
    data_directory: Path, stage_dir: Path, *, privacy: BackupPrivacy | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Stage kept captures and one consistent budget snapshot.

    The budget file is written with the SQLite backup API so a live WAL is
    part of the snapshot. The source ledger is only read.
    """
    files: list[dict[str, Any]] = []
    resources: list[dict[str, Any]] = []
    with advisory_file_lock(data_directory / "runtime-worker.lock", timeout_seconds=0):
        _stage_kept_captures(data_directory, stage_dir, files, resources, privacy)
        _stage_budget(data_directory, stage_dir, files, resources)
        _stage_worker_day(data_directory, stage_dir, files, resources)
    resources.sort(key=lambda item: (item["role"], item["archive_path"], item["field"] or ""))
    return files, resources


def _stage_worker_day(data: Path, stage: Path, files: list[dict], resources: list[dict]) -> None:
    """Save the reserved queue allowance while its worker cannot alter it."""
    source = data / "runtime-worker-day.json"
    if not source.exists():
        return
    source = safe_path(source, must_exist=True)
    config = _read_runtime_config(data) or {}
    installation = config.get("binding", {}).get("installation_id")
    if not installation:
        with closing(sqlite3.connect(f"{(data / 'memory.sqlite3').as_uri()}?mode=ro", uri=True)) as conn:
            installation = conn.execute("SELECT installation_id FROM instance_meta WHERE singleton=1").fetchone()[0]
    try:
        if source.stat().st_size > 4096:
            raise ValueError("worker_budget_size")
        validate_worker_day(json.loads(source.read_text(encoding="utf-8")), installation)
    except ValueError as exc:
        raise BackupError(f"invalid worker budget: {exc}") from exc
    destination = stage / source.name
    _copy_regular(source, destination)
    files.append(_file_entry(source.name, destination))
    resources.append(_resource("worker_day", source.name, source.name, field=None))


def restored_resource_path(dest: Path, relative: str) -> Path:
    """Path of one resource mapping inside the restore root."""
    try:
        name = canonical_member_name(relative)
    except ArchiveLayoutError as exc:
        raise BackupError(str(exc)) from exc
    return dest.joinpath(*name.split("/"))


def rewrite_budget_fields(
    config: dict[str, Any],
    dest: Path,
    resources: object,
    *,
    config_file: str = RUNTIME_CONFIG_NAME,
) -> None:
    """Point auxiliary ledger fields at the packed budget file.

    Only ``auxiliary.ledger_path`` and ``auxiliary.installation_dir`` are
    written, and only when the resource list maps that field and the restored
    config already has an auxiliary object. No other field is changed.
    """
    if type(resources) is not list:
        return
    auxiliary = config.get("auxiliary")
    if type(auxiliary) is not dict:
        return
    for resource in resources:
        if type(resource) is not dict or resource.get("config_file") != config_file:
            continue
        field = resource.get("field")
        key = _BUDGET_KEYS.get(field) if isinstance(field, str) else None
        relative = resource.get("target_relative")
        if key is None or type(relative) is not str:
            continue
        auxiliary[key] = str(restored_resource_path(dest, relative))


__all__ = [
    "BUDGET_ARCHIVE_PATH",
    "BUDGET_DIRECTORY",
    "INSTALLATION_DIR_FIELD",
    "KEPT_CAPTURES_DIRECTORY",
    "LEDGER_FIELD",
    "RUNTIME_CONFIG_NAME",
    "budget_ledger_source",
    "restored_resource_path",
    "rewrite_budget_fields",
    "stage_instance_resources",
]
