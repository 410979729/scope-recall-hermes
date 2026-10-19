"""Locked, content-free disaster tombstones copied from authoritative SQLite.

An installation and record identity form the checkpoint. Epochs are not unique
after imports. Committed deletions survive failed I/O and are retried by upkeep.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, BinaryIO

from ..contracts import ContractError
from .delete_storage import canonical
from .file_lock import advisory_file_lock
from .restore import deletion_operation_record
from .retained_artifacts import extended_path

DELETION_MIRROR_FILENAME = "deletion-mirror.jsonl"
DELETION_MIRROR_LAG_FILENAME = "deletion-mirror-lag.json"
DELETION_MIRROR_FORMAT = "scope-recall-deletion-mirror/1"
MIRROR_ERRORS = (OSError, ValueError, sqlite3.Error, ContractError)


def configured_backup_dir(data_directory: Path) -> Path | None:
    """Read the operator's backup location without consulting host credentials."""
    for name in ("backup-config.json", "runtime-config.json"):
        path = extended_path(data_directory / name)
        if not path.is_file():
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("backup configuration must be an object")
        value = raw.get("backup_dir")
        if value:
            target = Path(value).expanduser()
            if not target.is_absolute():
                raise ValueError("backup_dir must be absolute")
            return target
    return None


def mirror_file_path(backup_dir: Path) -> Path:
    """The content-free journal shared safely by installation-qualified records."""
    return extended_path(backup_dir / DELETION_MIRROR_FILENAME)


def mirror_lag_path(data_directory: Path) -> Path:
    """Local diagnostic marker; authoritative catch-up remains in SQLite."""
    return extended_path(data_directory / DELETION_MIRROR_LAG_FILENAME)


def record_mirror_lag(data_directory: Path, *, error: str) -> None:
    """Best-effort diagnostic when the independent mirror is unavailable."""
    try:
        mirror_lag_path(data_directory).write_text(canonical({"failed": True, "error": error}), encoding="utf-8")
    except OSError:
        # Even a full local disk must not turn a committed delete into a retry.
        pass


def read_mirror_lag(data_directory: Path) -> dict[str, Any] | None:
    """Read lag without treating a damaged marker as a successful checkpoint."""
    path = mirror_lag_path(data_directory)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"failed": True, "error": "unreadable lag marker"}


def _safe_mirror(path: Path) -> Path:
    for part in (path, *path.parents):
        try:
            info = extended_path(part).lstat()
        except FileNotFoundError:
            continue
        if part.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("mirror paths cannot use links or reparse points")
    return extended_path(path)


def _key(item: dict) -> tuple[str, str, str]:
    return item["installation_id"], item["kind"], item["record_id"]


def _read_records(stream: BinaryIO, *, repair_tail: bool = False) -> list[dict]:
    records: dict[tuple[str, str, str], dict] = {}
    while True:
        offset = stream.tell()
        line = stream.readline()
        if not line:
            break
        if not line.endswith(b"\n"):
            if not repair_tail:
                raise ValueError("incomplete deletion mirror tail; catch up from the original store")
            # A newline is the commit boundary. Only an incomplete last record
            # can be discarded; SQLite supplies it again before success.
            stream.seek(offset)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
            break
        item = json.loads(line)
        if (
            not isinstance(item, dict)
            or item.get("format") != DELETION_MIRROR_FORMAT
            or item.get("kind") not in {"identity", "operation", "source_group", "absence"}
            or not all(isinstance(item.get(k), str) and item[k] for k in ("installation_id", "record_id"))
        ):
            raise ValueError("invalid deletion mirror record")
        key = _key(item)
        if key in records and records[key] != item:
            raise ValueError("conflicting deletion mirror record")
        records[key] = item
    return list(records.values())


def load_mirror_records(path: Path) -> list[dict]:
    """Read a complete locked journal; missing or damaged protection is an error."""
    target = _safe_mirror(path)
    with advisory_file_lock(target.with_suffix(".lock"), timeout_seconds=2), target.open("rb") as stream:
        return _read_records(stream)


def load_mirror_operations(path: Path) -> list[dict]:
    """Return only operation records for operator diagnostics."""
    return [item for item in load_mirror_records(path) if item["kind"] == "operation"]


def installation_mirror(path: Path, installation_id: str) -> list[dict]:
    """Require the independently kept journal for this exact installation."""
    records = [item for item in load_mirror_records(path) if item["installation_id"] == installation_id]
    if not any(item["kind"] == "identity" and item["record_id"] == installation_id for item in records):
        raise ValueError("deletion mirror does not identify this installation")
    return records


def _truth_records(conn):
    installation_id = conn.execute("SELECT installation_id FROM instance_meta WHERE singleton=1").fetchone()[0]
    common = {"format": DELETION_MIRROR_FORMAT, "installation_id": installation_id}
    yield {**common, "kind": "identity", "record_id": installation_id}
    for row in conn.execute("SELECT * FROM deletion_operations WHERE mode='delete' ORDER BY rowid"):
        # Purge progress is mutable; the mirror carries deletion intent only.
        record = deletion_operation_record(conn, row)
        record["operation"].update(active_content_removed=0, layers_json="{}")
        yield {**common, "kind": "operation", "record_id": row["operation_id"], **record}
    for row in conn.execute("SELECT * FROM source_group_blocks WHERE read_blocked=1 ORDER BY group_sha256"):
        data = dict(row)
        data["suppressed"] = 1
        yield {**common, "kind": "source_group", "record_id": data["group_sha256"], "data": data}
    for row in conn.execute("SELECT * FROM restored_absence_blocks ORDER BY object_kind,object_ref"):
        data = dict(row)
        digest = hashlib.sha256(canonical(data).encode()).hexdigest()
        yield {**common, "kind": "absence", "record_id": digest, "data": data}


def _catchup(data_directory: Path, db_path: Path, backup_dir: Path) -> dict:
    target = _safe_mirror(mirror_file_path(backup_dir))
    target.parent.mkdir(parents=True, exist_ok=True)
    with (
        advisory_file_lock(target.with_suffix(".lock"), timeout_seconds=2),
        target.open("a+b") as stream,
        closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as conn,
    ):
        stream.seek(0)
        known = {_key(item) for item in _read_records(stream, repair_tail=True)}
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        count = 0
        for item in _truth_records(conn):
            if _key(item) in known:
                continue
            stream.write((canonical(item) + "\n").encode("utf-8"))
            count += 1
        stream.flush()
        os.fsync(stream.fileno())
        # Never clear lag before every record has been persisted.
        mirror_lag_path(data_directory).unlink(missing_ok=True)
    return {"status": "caught_up" if count else "up_to_date", "caught_up": count}


def sync_deletion_mirror(data_directory: Path, backup_dir: Path) -> dict:
    """Synchronize all current tombstones, raising when backup protection cannot be established."""
    return _catchup(data_directory, data_directory / "memory.sqlite3", backup_dir)


def mirror_committed_deletions(data_directory: Path, db_path: Path) -> bool:
    """Attempt post-commit mirroring without changing the deletion's outcome."""
    try:
        backup_dir = configured_backup_dir(data_directory)
        if backup_dir is not None:
            _catchup(data_directory, db_path, backup_dir)
        return True
    except MIRROR_ERRORS as exc:
        record_mirror_lag(data_directory, error=f"{type(exc).__name__}: {exc}")
        return False


def catchup_deletion_mirror(storage, backup_dir: Path) -> dict:
    """Use the same locked writer for manual, scheduled and post-commit catch-up."""
    directory = storage.binding.data_directory
    try:
        return _catchup(directory, storage.path, backup_dir)
    except MIRROR_ERRORS as exc:
        record_mirror_lag(directory, error=f"{type(exc).__name__}: {exc}")
        return {"status": "error", "caught_up": 0, "error": str(exc)}


def mirror_pending(data_directory: Path, backup_dir: Path) -> int:
    """Compare durable record identities against SQLite, surfacing invalid journals."""
    path = mirror_file_path(backup_dir)
    known = {_key(item) for item in load_mirror_records(path)} if path.exists() else set()
    db_path = data_directory / "memory.sqlite3"
    with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        return sum(_key(item) not in known for item in _truth_records(conn))
