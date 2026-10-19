"""Durable deletion ledger mirror in the configured backup directory.

Maintains an append-only sequence of deletion operations outside the database
file so that disaster recovery replaying a historical snapshot can re-apply every
deletion committed since that snapshot was taken.

Mirror writes only execute after SQLite transaction commit. Mirror write failures
never block or roll back the deletion; durable lag state is recorded for worker
catch-up and doctor reporting.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from ..contracts import ContractError
from .delete_storage import canonical
from .restore import InstallationMaintenance, replay_deletion_ledger
from .schema import SCHEMA_VERSION

DELETION_MIRROR_FILENAME = "deletion-mirror.jsonl"
DELETION_MIRROR_LAG_FILENAME = "deletion-mirror-lag.json"
DELETION_MIRROR_FORMAT = "scope-recall-deletion-mirror/1"


def _extended(path: Path) -> Path:
    if os.name != "nt":
        return path
    value = os.path.abspath(os.fspath(path))
    if value.startswith("\\\\?\\"):
        return Path(value)
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def configured_backup_dir(data_directory: Path) -> Path | None:
    """Read the configured backup directory for an instance, or None if unconfigured.

    Looks first in backup-config.json and falls back to runtime-config.json. Symlinks
    and non-absolute paths are rejected.
    """
    for config_name in ("backup-config.json", "runtime-config.json"):
        config_path = _extended(data_directory / config_name)
        if not config_path.is_file() or config_path.is_symlink():
            continue
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                continue
            backup_dir_val = payload.get("backup_dir")
            if not backup_dir_val or not isinstance(backup_dir_val, str):
                continue
            target = Path(backup_dir_val).expanduser()
            if not target.is_absolute() or target.is_symlink():
                continue
            return target
        except (OSError, ValueError):
            continue
    return None


def mirror_lag_path(data_directory: Path) -> Path:
    return _extended(data_directory / DELETION_MIRROR_LAG_FILENAME)


def read_mirror_lag(data_directory: Path) -> dict[str, Any] | None:
    """Return recorded mirror lag status, or None when mirror is up to date."""
    path = mirror_lag_path(data_directory)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return {"status": "lagging", "corrupt_lag_file": True}


def record_mirror_lag(data_directory: Path, operation_id: str, memory_epoch: int, *, error: str) -> None:
    """Record that a committed deletion could not be mirrored immediately."""
    path = mirror_lag_path(data_directory)
    temp = path.with_name(f".{path.name}.tmp")
    payload = {
        "status": "lagging",
        "failed": True,
        "operation_id": operation_id,
        "memory_epoch": memory_epoch,
        "error": error,
    }
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        temp.unlink(missing_ok=True)


def clear_mirror_lag(data_directory: Path) -> None:
    """Clear lag marker after successful catch-up."""
    try:
        mirror_lag_path(data_directory).unlink(missing_ok=True)
    except OSError:
        pass


def mirror_file_path(backup_dir: Path) -> Path:
    return _extended(backup_dir / DELETION_MIRROR_FILENAME)


def read_mirrored_epochs(mirror_path: Path) -> set[int]:
    """Read the set of all memory epochs already present in the mirror file."""
    if not mirror_path.is_file() or mirror_path.is_symlink():
        return set()
    epochs: set[int] = set()
    try:
        with mirror_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if isinstance(entry, dict) and "memory_epoch" in entry:
                        epochs.add(int(entry["memory_epoch"]))
                except (ValueError, TypeError):
                    continue
    except OSError:
        pass
    return epochs


def append_deletion_mirror(
    data_directory: Path,
    backup_dir: Path | None,
    *,
    operation_id: str,
    row: dict[str, Any],
    members: list[dict[str, Any]],
    memory_epoch: int,
) -> bool:
    """Append one committed deletion to the mirror file in the configured backup directory.

    Returns True if written or if mirror is not configured; False if writing failed
    and durable lag was recorded.
    """
    if backup_dir is None:
        return True
    target = mirror_file_path(backup_dir)
    line = (
        canonical(
            {
                "format": DELETION_MIRROR_FORMAT,
                "memory_epoch": memory_epoch,
                "operation_id": operation_id,
                "operation": row,
                "members": members,
            }
        )
        + "\n"
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        clear_mirror_lag(data_directory)
        return True
    except OSError as exc:
        record_mirror_lag(data_directory, operation_id, memory_epoch, error=f"{type(exc).__name__}: {exc}")
        return False


def mirror_committed_deletions(data_directory: Path, db_path: Path, operation_ids: list[str]) -> bool:
    """Safely append committed deletions to mirror file after transaction commit.

    Uses SQLite deletion_operations as persistent checkpoint to ensure reliable replay.
    """
    backup_dir = configured_backup_dir(data_directory)
    if backup_dir is None:
        return True
    target = mirror_file_path(backup_dir)
    mirrored_epochs = read_mirrored_epochs(target)
    with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        ops = conn.execute("SELECT * FROM deletion_operations ORDER BY memory_epoch, operation_id").fetchall()
        unmirrored = [op for op in ops if int(op["memory_epoch"]) not in mirrored_epochs]
        if not unmirrored:
            clear_mirror_lag(data_directory)
            return True
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as stream:
                for op in unmirrored:
                    op_dict = dict(op)
                    members = [
                        dict(m)
                        for m in conn.execute(
                            """SELECT b.object_kind, b.object_ref, b.scope_id, b.project_id, b.branch_id
                            FROM deletion_members m JOIN object_blocks b USING(object_kind, object_ref)
                            WHERE m.operation_id=? ORDER BY b.object_kind, b.object_ref""",
                            (op["operation_id"],),
                        ).fetchall()
                    ]
                    line = (
                        canonical(
                            {
                                "format": DELETION_MIRROR_FORMAT,
                                "memory_epoch": int(op["memory_epoch"]),
                                "operation_id": op["operation_id"],
                                "operation": op_dict,
                                "members": members,
                            }
                        )
                        + "\n"
                    )
                    stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
            clear_mirror_lag(data_directory)
            return True
        except OSError as exc:
            record_mirror_lag(
                data_directory,
                unmirrored[0]["operation_id"],
                int(unmirrored[0]["memory_epoch"]),
                error=f"{type(exc).__name__}: {exc}",
            )
            return False


def catchup_deletion_mirror(storage, backup_dir: Path) -> dict[str, Any]:
    """Catch up any unmirrored deletions from SQLite to the mirror file.

    Called by background worker upkeep or maintenance commands.
    """
    data_directory = storage.binding.data_directory
    target = mirror_file_path(backup_dir)
    mirrored_epochs = read_mirrored_epochs(target)
    caught_up = 0
    with closing(sqlite3.connect(f"{storage.path.as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        ops = conn.execute("SELECT * FROM deletion_operations ORDER BY memory_epoch, operation_id").fetchall()
        unmirrored = [op for op in ops if int(op["memory_epoch"]) not in mirrored_epochs]
        if not unmirrored:
            clear_mirror_lag(data_directory)
            return {"status": "up_to_date", "caught_up": 0}
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("a", encoding="utf-8") as stream:
                for op in unmirrored:
                    op_dict = dict(op)
                    members = [
                        dict(m)
                        for m in conn.execute(
                            """SELECT b.object_kind, b.object_ref, b.scope_id, b.project_id, b.branch_id
                            FROM deletion_members m JOIN object_blocks b USING(object_kind, object_ref)
                            WHERE m.operation_id=? ORDER BY b.object_kind, b.object_ref""",
                            (op["operation_id"],),
                        ).fetchall()
                    ]
                    line = (
                        canonical(
                            {
                                "format": DELETION_MIRROR_FORMAT,
                                "memory_epoch": int(op["memory_epoch"]),
                                "operation_id": op["operation_id"],
                                "operation": op_dict,
                                "members": members,
                            }
                        )
                        + "\n"
                    )
                    stream.write(line)
                    caught_up += 1
                stream.flush()
                os.fsync(stream.fileno())
            clear_mirror_lag(data_directory)
            return {"status": "caught_up", "caught_up": caught_up}
        except OSError as exc:
            record_mirror_lag(
                data_directory,
                unmirrored[0]["operation_id"],
                int(unmirrored[0]["memory_epoch"]),
                error=f"{type(exc).__name__}: {exc}",
            )
            return {"status": "error", "caught_up": caught_up, "error": str(exc)}


def load_mirror_operations(mirror_path: Path) -> list[dict[str, Any]]:
    """Parse and validate deletion operations from a mirror file.

    Rejects corrupted lines, out-of-order sequences, or incompatible formats.
    """
    if not mirror_path.is_file() or mirror_path.is_symlink():
        return []
    entries: list[dict[str, Any]] = []
    seen_ops: set[str] = set()
    last_epoch = -1
    with mirror_path.open("r", encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError as exc:
                raise ContractError("RESTORE_UNVERIFIED", f"mirror_syntax_error:line_{line_no}") from exc
            if not isinstance(item, dict) or item.get("format") != DELETION_MIRROR_FORMAT:
                raise ContractError("RESTORE_UNVERIFIED", f"mirror_format_invalid:line_{line_no}")
            op_id = item.get("operation_id")
            epoch = item.get("memory_epoch")
            if not isinstance(op_id, str) or not isinstance(epoch, int):
                raise ContractError("RESTORE_UNVERIFIED", f"mirror_shape_invalid:line_{line_no}")
            if op_id in seen_ops:
                continue
            if epoch < last_epoch:
                raise ContractError("RESTORE_UNVERIFIED", f"mirror_reordered:line_{line_no}")
            seen_ops.add(op_id)
            last_epoch = epoch
            entries.append(item)
    return entries


def replay_deletion_mirror(
    storage: Any,
    mirror_path: Path,
    *,
    authority: InstallationMaintenance | None = None,
    remaining_seconds: float = 10.0,
) -> dict[str, Any]:
    """Replay operations from a deletion mirror against a restored database.

    Operations already recorded in the restored database are skipped safely.
    """
    items = load_mirror_operations(mirror_path)
    if not items:
        return {"status": "replayed", "replayed_operations": 0, "purges_queued": 0, "latest_mirrored_at": None}

    db_path = storage if isinstance(storage, Path) else storage.path
    with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as conn:
        applied_ops = {row[0] for row in conn.execute("SELECT operation_id FROM deletion_operations")}
        row = conn.execute(
            "SELECT agent_id, installation_id, data_directory, test_mode, installation_kind FROM instance_meta WHERE singleton=1"
        ).fetchone()
        scopes = frozenset(r[0] for r in conn.execute("SELECT scope_id FROM instance_scopes"))

    pending = [item for item in items if item["operation_id"] not in applied_ops]
    if not pending:
        latest = items[-1].get("mirrored_at") or items[-1].get("operation", {}).get("created_at")
        return {"status": "replayed", "replayed_operations": 0, "purges_queued": 0, "latest_mirrored_at": latest}

    actual_storage = storage
    kind = row[4] if len(row) > 4 and row[4] else "local"
    if (
        not hasattr(storage, "binding")
        or actual_storage.binding.agent_id != row[0]
        or actual_storage.binding.installation_id != row[1]
        or actual_storage.binding.data_directory.resolve() != Path(row[2]).resolve()
    ):
        from ..contracts import InstanceBinding
        from .storage import SQLiteStorage

        binding = InstanceBinding(
            agent_id=row[0],
            installation_id=row[1],
            data_directory=Path(row[2]),
            scope_ids=scopes,
            test_mode=bool(row[3]),
            installation_kind=kind,
        )
        actual_storage = SQLiteStorage(binding)

    actual_authority = authority
    if actual_authority is None:
        from ..contracts import TrustedContext

        context = TrustedContext(
            binding=actual_storage.binding,
            session_id="disaster_restore",
            allowed_scope_ids=actual_storage.binding.scope_ids,
            actor_origin="human_direct",
        )
        actual_authority = InstallationMaintenance(context=context)

    operations_to_replay = [{"operation": item["operation"], "members": item["members"]} for item in pending]
    synthetic_ledger = {
        "format": "scope-recall-deletion-ledger/1",
        "schema_version": SCHEMA_VERSION,
        "memory_epoch": pending[-1]["memory_epoch"],
        "installation_id": actual_storage.binding.installation_id,
        "agent_id": actual_storage.binding.agent_id,
        "scope_ids": sorted(actual_storage.binding.scope_ids),
        "operations": operations_to_replay,
        "withdrawals": [],
        "source_groups": [],
        "absent_objects": [],
    }

    from .restore import (
        LEDGER_FILENAME,
        MARKER_FILENAME,
        ledger_digest,
        _write_whole,
    )

    data_dir = actual_storage.binding.data_directory
    digest = ledger_digest(synthetic_ledger)
    marker_payload = dict(
        installation_id=actual_storage.binding.installation_id,
        agent_id=actual_storage.binding.agent_id,
        data_directory=str(actual_storage.binding.data_directory),
        expected_ledger_sha256=digest,
    )
    _write_whole(data_dir / LEDGER_FILENAME, canonical(synthetic_ledger) + "\n")
    _write_whole(data_dir / MARKER_FILENAME, canonical(marker_payload) + "\n")

    result = replay_deletion_ledger(
        actual_storage, actual_authority, synthetic_ledger, remaining_seconds=remaining_seconds
    )

    purges = sum(item["operation"].get("mode") == "delete" for item in pending)
    latest_at = pending[-1].get("mirrored_at") or pending[-1].get("operation", {}).get("created_at")
    return {
        "status": "replayed",
        "replayed_operations": len(pending),
        "purges_queued": purges,
        "affected_objects": result.get("affected_objects", 0),
        "latest_mirrored_at": latest_at,
    }


__all__ = [
    "DELETION_MIRROR_FILENAME",
    "DELETION_MIRROR_LAG_FILENAME",
    "DELETION_MIRROR_FORMAT",
    "append_deletion_mirror",
    "catchup_deletion_mirror",
    "clear_mirror_lag",
    "configured_backup_dir",
    "load_mirror_operations",
    "mirror_committed_deletions",
    "mirror_file_path",
    "mirror_lag_path",
    "read_mirror_lag",
    "read_mirrored_epochs",
    "record_mirror_lag",
    "replay_deletion_mirror",
]
