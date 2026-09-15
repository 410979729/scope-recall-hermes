"""Immutable source artifact, file identity, and epoch snapshots.

This module owns regular-file identity, refusal of every ``-wal``/``-shm``/
``-journal`` sibling (including 0-byte files), the dedicated ``file:`` URI
opener with ``mode=ro&immutable=1``, source health/schema reads on that
connection, and source/target epoch payloads.

Source is always opened immutable-main-only. Checkpointed main-only target
inspection uses the same sidecar-safe URI only after proving there are no
siblings; a dirty or non-checkpointed target is refused first and is never
treated as immutable. Ordinary ``connect_truth_database`` / ``inspect_sqlite_health``
opens are not used here: they can materialize sidecars on a WAL-header main.

Apply-time logical epoch is computed on a caller-supplied writer connection.
This module never opens a second ordinary read-only target connection.

Ownership: snapshot and identity only. Row selection and apply
orchestration stay in sibling modules. Failure behavior is fail-closed
via ``JournalSourceRestoreError`` codes; this module does not write.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Mapping


class JournalSourceRestoreError(RuntimeError):
    """Fail-closed restore refusal with a redacted error code only."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "source_restore_refused")
        super().__init__(self.code)


_EPOCH_TABLES = (
    "journal_digest_runs",
    "journal_entries",
    "journal_rejections",
    "memories",
    "memories_fts",
    "memory_journal_sources",
    "operator_operations",
    "procedural_playbooks",
)


def canonical_json(value: Any) -> str:
    """Serialize one value with sorted keys and compact separators."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def as_absolute_path(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def sidecar_paths(path: Path) -> tuple[Path, Path, Path]:
    return Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")


def any_sqlite_sidecar_present(path: Path) -> bool:
    """True when any WAL/SHM/rollback-journal sibling exists, including 0 bytes.

    W115R dest-open class: a zero-byte sidecar is still a present sidecar.
    Symlinks and unreadable siblings are also refuse.
    """

    for sidecar in sidecar_paths(path):
        try:
            if sidecar.is_symlink() or sidecar.exists():
                return True
        except OSError:
            return True
    return False


def capture_regular_file_identity(path: Path) -> dict[str, int | str]:
    """Capture Windows-usable regular-file identity before an immutable open.

    Immutable mode disables locking and change detection, so callers must bind
    ``st_dev``, ``st_ino``, size, mtime, and SHA-256 before opening and recheck
    after close. Replacement or drift is a bounded refusal.
    """

    stat_result = path.stat()
    return {
        "st_dev": int(stat_result.st_dev),
        "st_ino": int(stat_result.st_ino),
        "st_size": int(stat_result.st_size),
        "st_mtime_ns": int(stat_result.st_mtime_ns),
        "sha256": sha256_file(path),
    }


def require_main_only_sqlite_file(path: str | Path, *, wal_code: str) -> Path:
    """Refuse symlinks, non-files, symlink parents, and any sidecar sibling."""

    db_path = as_absolute_path(path)
    try:
        if db_path.is_symlink() or db_path.parent.is_symlink() or not db_path.is_file():
            raise JournalSourceRestoreError("source_unhealthy")
    except OSError as exc:
        raise JournalSourceRestoreError("source_unhealthy") from exc
    if any_sqlite_sidecar_present(db_path):
        raise JournalSourceRestoreError(wal_code)
    return db_path


def immutable_source_uri(path: Path) -> str:
    """Build a correctly encoded ``file:`` URI with ``mode=ro&immutable=1``."""

    db_path = as_absolute_path(path)
    return f"{db_path.as_uri()}?mode=ro&immutable=1"


def open_main_only_immutable_reader(path: str | Path, *, wal_code: str) -> sqlite3.Connection:
    """Open one sidecar-safe reader after proving the file is main-only.

    Dirty files are refused before the URI is built. The read itself must
    not create ``-wal``/``-shm``/``-journal`` siblings.
    """

    db_path = require_main_only_sqlite_file(path, wal_code=wal_code)
    conn = sqlite3.connect(immutable_source_uri(db_path), uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def open_immutable_source_connection(path: str | Path) -> sqlite3.Connection:
    """Open one source-only immutable reader.

    The source must already be a regular main-only file. The connection is
    query-capable for health, schema, window selection, and reference checks.
    Callers must close it and recheck file identity plus sidecar absence.
    """

    return open_main_only_immutable_reader(path, wal_code="source_wal_present")


def open_checkpointed_target_reader(path: str | Path) -> sqlite3.Connection:
    """Open a sidecar-safe reader for a proven checkpointed main-only target.

    Sidecars, including zero-byte files and symlinks, are refused first.
    Only then is the ``mode=ro&immutable=1`` URI used. A dirty or
    non-checkpointed target never takes this path.
    """

    return open_main_only_immutable_reader(path, wal_code="target_wal_incoherent")


def inspect_immutable_source_health(conn: sqlite3.Connection) -> None:
    """Run quick_check, bounded integrity_check(1), and foreign_key_check."""

    quick = conn.execute("PRAGMA quick_check").fetchone()
    if quick is None or str(quick[0]).strip().lower() != "ok":
        raise JournalSourceRestoreError("source_unhealthy")
    integrity = conn.execute("PRAGMA integrity_check(1)").fetchone()
    if integrity is None or str(integrity[0]).strip().lower() != "ok":
        raise JournalSourceRestoreError("source_unhealthy")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise JournalSourceRestoreError("source_unhealthy")


def _normalized_field(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def compute_schema_digest(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT type, name, tbl_name, COALESCE(sql, '') AS sql "
        "FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name, tbl_name"
    ).fetchall()
    payload = [
        {
            "name": str(row["name"] if isinstance(row, sqlite3.Row) else row[1]),
            "sql": str(row["sql"] if isinstance(row, sqlite3.Row) else row[3]),
            "tbl_name": str(row["tbl_name"] if isinstance(row, sqlite3.Row) else row[2]),
            "type": str(row["type"] if isinstance(row, sqlite3.Row) else row[0]),
        }
        for row in rows
    ]
    return sha256_text(canonical_json(payload))


def compute_table_logical_digest(conn: sqlite3.Connection, table: str) -> str:
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        return sha256_text("[]")
    columns = [str(item[1]) for item in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    records = []
    for row in rows:
        if isinstance(row, sqlite3.Row):
            mapping = {column: _normalized_field(row[column]) for column in columns}
        else:
            mapping = {column: _normalized_field(row[index]) for index, column in enumerate(columns)}
        records.append(mapping)
    records.sort(key=canonical_json)
    return sha256_text("\n".join(canonical_json(item) for item in records))


def inspect_source_artifact(source: Path) -> dict[str, Any]:
    """Bind source identity, schema, and user_version on one immutable open.

    Side effects: none on the source bytes when the opener is used. Failure
    closes the connection and raises a bounded source error.
    """

    db_path = require_main_only_sqlite_file(source, wal_code="source_wal_present")
    identity = capture_regular_file_identity(db_path)
    conn = open_immutable_source_connection(db_path)
    try:
        inspect_immutable_source_health(conn)
        schema_digest = compute_schema_digest(conn)
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()
    require_main_only_sqlite_file(db_path, wal_code="source_wal_present")
    after = capture_regular_file_identity(db_path)
    if after != identity:
        raise JournalSourceRestoreError("source_snapshot_changed")
    return {
        "file_sha256": str(identity["sha256"]),
        "schema_digest": schema_digest,
        "user_version": user_version,
        "identity": identity,
        "source_epoch_digest": sha256_text(
            canonical_json(
                {
                    "schema_digest": schema_digest,
                    "sha256": identity["sha256"],
                    "user_version": user_version,
                }
            )
        ),
    }


def compute_target_epoch_from_connection(
    conn: sqlite3.Connection,
    *,
    file_sha256: str,
) -> dict[str, Any]:
    """Bind schema, user_version, and table snapshots from an open connection.

    Apply-time callers pass the already-authorized writer after
    ``BEGIN IMMEDIATE`` plus the checkpointed main-file hash bound before
    that writer was opened. This helper never opens a second connection.
    """

    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    schema_digest = compute_schema_digest(conn)
    tables: dict[str, Any] = {}
    for table in _EPOCH_TABLES:
        tables[table] = {
            "count": int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]),
            "digest": compute_table_logical_digest(conn, table),
        }
    sequence_row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'journal_entries'"
    ).fetchone()
    sqlite_sequence = {
        "journal_entries": 0 if sequence_row is None else int(sequence_row[0]),
    }
    payload = {
        "file_sha256": str(file_sha256),
        "schema_digest": schema_digest,
        "sqlite_sequence": sqlite_sequence,
        "tables": tables,
        "user_version": user_version,
    }
    return {**payload, "epoch_digest": sha256_text(canonical_json(payload))}


def compute_target_epoch(path: Path) -> dict[str, Any]:
    """Bind identity and logical epoch from a checkpointed main-only target.

    Dirty or sidecar-bearing targets are refused before any open. The reader
    uses the sidecar-safe URI only after that proof, so a WAL-header main
    does not grow ``-wal``/``-shm``. This is not treating a live dirty
    target as immutable.
    """

    db_path = require_main_only_sqlite_file(path, wal_code="target_wal_incoherent")
    identity = capture_regular_file_identity(db_path)
    conn = open_checkpointed_target_reader(db_path)
    try:
        epoch = compute_target_epoch_from_connection(
            conn, file_sha256=str(identity["sha256"])
        )
    finally:
        conn.close()
    require_main_only_sqlite_file(db_path, wal_code="target_wal_incoherent")
    after = capture_regular_file_identity(db_path)
    if after != identity:
        raise JournalSourceRestoreError("target_snapshot_changed")
    return epoch


def identities_match(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return dict(left) == dict(right)


__all__ = [
    "JournalSourceRestoreError",
    "any_sqlite_sidecar_present",
    "as_absolute_path",
    "canonical_json",
    "capture_regular_file_identity",
    "compute_schema_digest",
    "compute_table_logical_digest",
    "compute_target_epoch",
    "compute_target_epoch_from_connection",
    "identities_match",
    "immutable_source_uri",
    "inspect_immutable_source_health",
    "inspect_source_artifact",
    "open_checkpointed_target_reader",
    "open_immutable_source_connection",
    "open_main_only_immutable_reader",
    "require_main_only_sqlite_file",
    "sha256_file",
    "sha256_text",
    "sidecar_paths",
]
