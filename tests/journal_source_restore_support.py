"""Synthetic fixtures for official journal source-restore tests.

This module is not a collected test module. It builds temporary SQLite
databases and computes the documented canonical set/epoch digests so tests
can bind expected values without reading operator data. Fixture bodies stay
synthetic and must not be printed by callers.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from scope_recall.journal_store import ensure_journal_schema
from scope_recall.sql_store import SCHEMA_VERSION, ensure_schema
from scope_recall.sqlite_backup import inspect_sqlite_health


JOURNAL_WINDOW_START = "2026-03-01T00:00:00+00:00"
JOURNAL_WINDOW_END = "2026-03-02T00:00:00+00:00"
DIGEST_WINDOW_START = "2026-03-01T00:00:00+00:00"
DIGEST_WINDOW_END = "2026-03-02T00:00:00+00:00"
JOURNAL_EXCLUDED_START = "2026-03-02T00:00:00+00:00"
JOURNAL_EXCLUDED_END = "2026-03-02T12:00:00+00:00"
DIGEST_EXCLUDED_START = "2026-03-02T00:00:00+00:00"
DIGEST_EXCLUDED_END = "2026-03-02T12:00:00+00:00"

SHARED_CONTENT_HASH = hashlib.sha256(b"synthetic-shared-hash-body").hexdigest()


def journal_content_hash(content: str) -> str:
    """Independent copy of the repository journal content-hash contract."""

    return hashlib.sha256(str(content).encode("utf-8")).hexdigest()


REFERENCED_DIGEST_ID = "jsr-run-ok"
UNREFERENCED_DIGEST_ID = "jsr-run-err"

JOURNAL_SET_DIGEST_FIELDS = (
    "scope_id",
    "session_id",
    "turn_number",
    "role",
    "content_hash",
    "created_at",
)
JOURNAL_SEMANTIC_FIELDS = (
    "scope_id",
    "shared_scope_id",
    "platform",
    "user_id",
    "chat_id",
    "thread_id",
    "gateway_session_key",
    "agent_identity",
    "agent_workspace",
    "session_id",
    "turn_number",
    "role",
    "content",
    "content_hash",
    "created_at",
    "processed_run_id",
    "processed_at",
    "metadata",
    "extraction_attempts",
    "deferred_run_id",
    "deferred_at",
    "defer_count",
    "retryable_failures",
)
DIGEST_RUN_LOGICAL_FIELDS = (
    "id",
    "started_at",
    "finished_at",
    "status",
    "extractor",
    "interval_label",
    "processed_entries",
    "inserted",
    "updated",
    "skipped",
    "error",
    "metadata",
)
NONTARGET_TABLES = (
    "memories",
    "memories_fts",
    "procedural_playbooks",
    "memory_journal_sources",
    "journal_rejections",
)

_JOURNAL_INSERT_SQL = """
    INSERT INTO journal_entries(
        id, scope_id, shared_scope_id, platform, user_id, chat_id, thread_id,
        gateway_session_key, agent_identity, agent_workspace, session_id,
        turn_number, role, content, content_hash, created_at, processed_run_id,
        processed_at, metadata, extraction_attempts, deferred_run_id, deferred_at,
        defer_count, retryable_failures
    ) VALUES (
        :id, :scope_id, :shared_scope_id, :platform, :user_id, :chat_id, :thread_id,
        :gateway_session_key, :agent_identity, :agent_workspace, :session_id,
        :turn_number, :role, :content, :content_hash, :created_at, :processed_run_id,
        :processed_at, :metadata, :extraction_attempts, :deferred_run_id, :deferred_at,
        :defer_count, :retryable_failures
    )
"""
_DIGEST_INSERT_SQL = """
    INSERT INTO journal_digest_runs(
        id, started_at, finished_at, status, extractor, interval_label,
        processed_entries, inserted, updated, skipped, error, metadata
    ) VALUES (
        :id, :started_at, :finished_at, :status, :extractor, :interval_label,
        :processed_entries, :inserted, :updated, :skipped, :error, :metadata
    )
"""


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


def sqlite_sidecars(path: Path) -> tuple[Path, Path, Path]:
    return (
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{path}-journal"),
    )


def file_identity(path: Path) -> dict[str, int | str]:
    """Capture Windows-usable regular-file identity without following links."""

    stat_result = path.stat()
    return {
        "st_dev": int(stat_result.st_dev),
        "st_ino": int(stat_result.st_ino),
        "st_size": int(stat_result.st_size),
        "st_mtime_ns": int(stat_result.st_mtime_ns),
        "sha256": sha256_file(path),
    }


def convert_to_wal_header_main_only(path: Path) -> None:
    """Leave a checkpoint-complete WAL-header main with no sibling files.

    The main file keeps the WAL header (bytes 18-19 == 2,2). Ordinary
    ``mode=ro`` may then materialize sidecars; immutable-ro must not.
    """

    checkpoint_sqlite_file(path)
    conn = sqlite3.connect(path)
    try:
        mode = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).strip().lower()
        if mode != "wal":
            raise RuntimeError(f"fixture could not enter WAL ({mode})")
        conn.execute("UPDATE journal_entries SET metadata = metadata WHERE id = 7")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    for sidecar in sqlite_sidecars(path):
        if sidecar.exists() or sidecar.is_symlink():
            sidecar.unlink()
    header = Path(path).read_bytes()[:20]
    if len(header) < 20 or header[18] != 2 or header[19] != 2:
        raise RuntimeError("fixture main file is not a WAL-header SQLite database")
    if any(sidecar.exists() or sidecar.is_symlink() for sidecar in sqlite_sidecars(path)):
        raise RuntimeError("WAL-header fixture still has sidecars")


def ordinary_readonly_open(path: Path) -> sqlite3.Connection:
    """Show the dest-open sidecar hazard: plain file: URI with mode=ro only."""

    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)


def checkpoint_sqlite_file(path: Path) -> None:
    """Force a DELETE-journal checkpoint so the main file is self-contained."""

    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        row = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
        mode = "" if row is None else str(row[0]).strip().lower()
        if mode != "delete":
            raise RuntimeError(f"fixture checkpoint refused DELETE journal ({mode})")
        conn.commit()
    finally:
        conn.close()


def open_fixture_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize_truth_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_fixture_connection(path)
    try:
        ensure_schema(conn)
        ensure_journal_schema(conn)
        conn.commit()
    finally:
        conn.close()
    checkpoint_sqlite_file(path)


def compute_schema_digest(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT type, name, tbl_name, COALESCE(sql, '') AS sql "
        "FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name, tbl_name"
    ).fetchall()
    payload = [
        {
            "name": str(row["name"]),
            "sql": str(row["sql"]),
            "tbl_name": str(row["tbl_name"]),
            "type": str(row["type"]),
        }
        for row in rows
    ]
    return sha256_text(canonical_json(payload))


def _normalized_field(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def journal_identity_record(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: _normalized_field(row[field])
        for field in JOURNAL_SET_DIGEST_FIELDS
    }


def journal_semantic_record(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: _normalized_field(row[field])
        for field in JOURNAL_SEMANTIC_FIELDS
    }


def digest_run_logical_record(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: _normalized_field(row[field])
        for field in DIGEST_RUN_LOGICAL_FIELDS
    }


def compute_journal_set_digest(rows: list[Mapping[str, Any]]) -> str:
    records = [journal_identity_record(row) for row in rows]
    records.sort(
        key=lambda item: (
            str(item["created_at"] or ""),
            str(item["scope_id"] or ""),
            str(item["session_id"] or ""),
            int(item["turn_number"] or 0),
            str(item["role"] or ""),
            str(item["content_hash"] or ""),
        )
    )
    return sha256_text("\n".join(canonical_json(item) for item in records))


def compute_digest_run_set_digest(rows: list[Mapping[str, Any]]) -> str:
    records = [digest_run_logical_record(row) for row in rows]
    records.sort(
        key=lambda item: (str(item["started_at"] or ""), str(item["id"] or ""))
    )
    return sha256_text("\n".join(canonical_json(item) for item in records))


def select_journal_window(
    conn: sqlite3.Connection,
    *,
    start: str,
    end: str,
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM journal_entries "
            "WHERE created_at >= ? AND created_at < ? "
            "ORDER BY created_at ASC, scope_id ASC, session_id ASC, "
            "turn_number ASC, role ASC, content_hash ASC",
            (start, end),
        ).fetchall()
    )


def select_digest_window(
    conn: sqlite3.Connection,
    *,
    start: str,
    end: str,
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM journal_digest_runs "
            "WHERE started_at >= ? AND started_at < ? "
            "ORDER BY started_at ASC, id ASC",
            (start, end),
        ).fetchall()
    )


def compute_table_logical_digest(conn: sqlite3.Connection, table: str) -> str:
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        return sha256_text("[]")
    columns = [str(item[1]) for item in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    records = []
    for row in rows:
        mapping = {column: _normalized_field(row[column]) for column in columns}
        records.append(mapping)
    records.sort(key=lambda item: canonical_json(item))
    return sha256_text("\n".join(canonical_json(item) for item in records))


def compute_target_epoch(path: Path) -> dict[str, Any]:
    """Fixture binder for the official target epoch. Not imported from production."""

    conn = open_fixture_connection(path)
    try:
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        schema_digest = compute_schema_digest(conn)
        named = (
            "journal_digest_runs",
            "journal_entries",
            "journal_rejections",
            "memories",
            "memories_fts",
            "memory_journal_sources",
            "operator_operations",
            "procedural_playbooks",
        )
        tables = {}
        for name in named:
            count = conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()
            tables[name] = {
                "count": int(count["n"] if count is not None else 0),
                "digest": compute_table_logical_digest(conn, name),
            }
        sequence = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'journal_entries'"
        ).fetchone()
        sqlite_sequence = {"journal_entries": 0 if sequence is None else int(sequence["seq"])}
    finally:
        conn.close()
    payload = {
        "file_sha256": sha256_file(path),
        "schema_digest": schema_digest,
        "sqlite_sequence": sqlite_sequence,
        "tables": tables,
        "user_version": user_version,
    }
    return {
        **payload,
        "epoch_digest": sha256_text(canonical_json(payload)),
    }


def nontarget_snapshot(path: Path) -> dict[str, Any]:
    conn = open_fixture_connection(path)
    try:
        snapshot = {}
        for table in NONTARGET_TABLES:
            snapshot[table] = {
                "count": int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]),
                "digest": compute_table_logical_digest(conn, table),
            }
        return snapshot
    finally:
        conn.close()


def _journal_row(
    *,
    entry_id: int,
    scope_id: str,
    session_id: str,
    turn_number: int,
    role: str,
    content: str,
    content_hash: str,
    created_at: str,
    processed_run_id: str = "",
    processed_at: str | None = None,
    metadata: str = "{}",
    extraction_attempts: int = 0,
    deferred_run_id: str = "",
    deferred_at: str | None = None,
    defer_count: int = 0,
    retryable_failures: int = 0,
    platform: str = "cli",
    user_id: str = "synthetic-user",
    chat_id: str = "synthetic-chat",
) -> dict[str, Any]:
    return {
        "id": entry_id,
        "scope_id": scope_id,
        "shared_scope_id": f"shared-{scope_id}",
        "platform": platform,
        "user_id": user_id,
        "chat_id": chat_id,
        "thread_id": "",
        "gateway_session_key": "gw-synthetic",
        "agent_identity": "synthetic-agent",
        "agent_workspace": "hermes",
        "session_id": session_id,
        "turn_number": turn_number,
        "role": role,
        "content": content,
        "content_hash": content_hash,
        "created_at": created_at,
        "processed_run_id": processed_run_id,
        "processed_at": processed_at,
        "metadata": metadata,
        "extraction_attempts": extraction_attempts,
        "deferred_run_id": deferred_run_id,
        "deferred_at": deferred_at,
        "defer_count": defer_count,
        "retryable_failures": retryable_failures,
    }


def _digest_row(
    *,
    digest_id: str,
    started_at: str,
    status: str,
    extractor: str = "heuristic",
    finished_at: str | None = None,
    interval_label: str = "synthetic",
    processed_entries: int = 0,
    inserted: int = 0,
    updated: int = 0,
    skipped: int = 0,
    error: str | None = None,
    metadata: str = "{}",
) -> dict[str, Any]:
    return {
        "id": digest_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "extractor": extractor,
        "interval_label": interval_label,
        "processed_entries": processed_entries,
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "error": error,
        "metadata": metadata,
    }


def approved_journal_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(1, 17):
        created = f"2026-03-01T01:{index:02d}:00+00:00"
        processed = index <= 8
        rows.append(
            _journal_row(
                entry_id=1000 + index,
                scope_id="scope-approved",
                session_id=f"session-{index:02d}",
                turn_number=index,
                role="user" if index % 2 else "assistant",
                content=f"synthetic-approved-{index:02d}",
                content_hash=journal_content_hash(f"synthetic-approved-{index:02d}"),
                created_at=created,
                processed_run_id=REFERENCED_DIGEST_ID if processed else "",
                processed_at="2026-03-01T02:05:00+00:00" if processed else None,
                metadata=canonical_json({"fixture": True, "index": index}),
                extraction_attempts=index % 3,
                deferred_run_id="defer-1" if index == 16 else "",
                deferred_at="2026-03-01T01:40:00+00:00" if index == 16 else None,
            )
        )
    for offset, (session_id, turn_number, role, created_at) in enumerate(
        (
            ("shared-hash-a", 70, "user", "2026-03-01T01:50:00+00:00"),
            ("shared-hash-b", 71, "assistant", "2026-03-01T01:51:00+00:00"),
            ("shared-hash-c", 72, "tool", "2026-03-01T01:52:00+00:00"),
        ),
        start=17,
    ):
        rows.append(
            _journal_row(
                entry_id=1000 + offset,
                scope_id=f"scope-shared-{offset}",
                session_id=session_id,
                turn_number=turn_number,
                role=role,
                content="synthetic-shared-hash-body",
                content_hash=SHARED_CONTENT_HASH,
                created_at=created_at,
                processed_run_id="",
                metadata=canonical_json({"shared_hash": True, "slot": offset}),
            )
        )
    return rows


def approved_digest_rows() -> list[dict[str, Any]]:
    return [
        _digest_row(
            digest_id=REFERENCED_DIGEST_ID,
            started_at="2026-03-01T02:00:00+00:00",
            finished_at="2026-03-01T02:06:00+00:00",
            status="ok",
            processed_entries=8,
            inserted=2,
            metadata=canonical_json({"kind": "referenced"}),
        ),
        _digest_row(
            digest_id=UNREFERENCED_DIGEST_ID,
            started_at="2026-03-01T03:00:00+00:00",
            finished_at="2026-03-01T03:01:00+00:00",
            status="error",
            error="synthetic-timeout",
            skipped=1,
            metadata=canonical_json({"kind": "unreferenced-error-receipt"}),
        ),
    ]


def outside_journal_rows() -> list[dict[str, Any]]:
    return [
        _journal_row(
            entry_id=10,
            scope_id="scope-outside",
            session_id="before",
            turn_number=1,
            role="user",
            content="synthetic-before-window",
            content_hash=journal_content_hash("synthetic-before-window"),
            created_at="2026-02-28T23:59:59+00:00",
        ),
        _journal_row(
            entry_id=11,
            scope_id="scope-outside",
            session_id="after",
            turn_number=1,
            role="user",
            content="synthetic-after-window",
            content_hash=journal_content_hash("synthetic-after-window"),
            created_at="2026-03-02T00:00:00+00:00",
        ),
    ]


def outside_digest_rows() -> list[dict[str, Any]]:
    return [
        _digest_row(
            digest_id="jsr-run-before",
            started_at="2026-02-28T12:00:00+00:00",
            finished_at="2026-02-28T12:01:00+00:00",
            status="ok",
        ),
        _digest_row(
            digest_id="jsr-run-after",
            started_at="2026-03-02T00:00:00+00:00",
            finished_at="2026-03-02T00:01:00+00:00",
            status="ok",
        ),
    ]


def seed_nontarget_rows(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
            agent_identity, agent_workspace, session_id, source, target, content, summary,
            created_at, updated_at, last_recalled_turn, metadata
        ) VALUES (
            'mem-nontarget', 'scope-approved', 'cli', 'synthetic-user', 'synthetic-chat',
            '', '', 'synthetic-agent', 'hermes', 'session-nontarget', 'manual', 'ops',
            'synthetic nontarget memory', 'nontarget', '2026-02-01T00:00:00+00:00',
            '2026-02-01T00:00:00+00:00', 0, '{}'
        )
        """
    )
    conn.execute(
        "INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)",
        ("mem-nontarget", "synthetic nontarget memory", "nontarget"),
    )
    conn.execute(
        """
        INSERT INTO procedural_playbooks(
            id, scope_id, shared_scope_id, task_class, title, trigger, goal,
            created_at, updated_at
        ) VALUES (
            'pb-nontarget', 'scope-approved', 'shared-scope-approved', 'ops',
            'synthetic playbook', 'never print fixture bodies', 'keep nontarget stable',
            '2026-02-01T00:00:00+00:00', '2026-02-01T00:00:00+00:00'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO journal_entries(
            id, scope_id, shared_scope_id, platform, user_id, chat_id, thread_id,
            gateway_session_key, agent_identity, agent_workspace, session_id,
            turn_number, role, content, content_hash, created_at, processed_run_id,
            metadata
        ) VALUES (
            7, 'scope-target-only', 'shared-target', 'cli', 'synthetic-user',
            'synthetic-chat', '', '', 'synthetic-agent', 'hermes', 'target-only',
            1, 'user', 'target-only sentinel', ?, '2026-01-15T00:00:00+00:00',
            '', '{}'
        )
        """,
        (journal_content_hash("target-only sentinel"),),
    )
    conn.execute(
        """
        INSERT INTO memory_journal_sources(memory_id, journal_entry_id, run_id, created_at)
        VALUES ('mem-nontarget', 7, 'preexisting-run', '2026-02-01T00:00:01+00:00')
        """
    )
    conn.execute(
        """
        INSERT INTO journal_rejections(journal_entry_id, run_id, reason, candidate, created_at)
        VALUES (7, 'preexisting-run', 'low-value:synthetic', '', '2026-02-01T00:00:02+00:00')
        """
    )


@dataclass(frozen=True)
class SourceRestorePair:
    source_path: Path
    target_path: Path
    backup_path: Path
    journal_created_at_start: str
    journal_created_at_end: str
    digest_started_at_start: str
    digest_started_at_end: str
    expected_journal_count: int
    expected_digest_run_count: int
    expected_journal_set_digest: str
    expected_digest_run_set_digest: str
    expected_source_sha256: str
    expected_schema_digest: str
    expected_user_version: int
    expected_target_epoch_digest: str
    source_journal_ids: tuple[int, ...]
    source_digest_ids: tuple[str, ...]


def _write_rows(path: Path, *, journals: list[dict[str, Any]], digests: list[dict[str, Any]], seed_nontarget: bool) -> None:
    initialize_truth_file(path)
    conn = open_fixture_connection(path)
    try:
        if seed_nontarget:
            seed_nontarget_rows(conn)
        for row in journals:
            conn.execute(_JOURNAL_INSERT_SQL, row)
        for row in digests:
            conn.execute(_DIGEST_INSERT_SQL, row)
        conn.commit()
    finally:
        conn.close()
    checkpoint_sqlite_file(path)


def build_source_restore_pair(root: Path, *, occupy_source_ids: bool = False) -> SourceRestorePair:
    """Create a checkpointed source snapshot and an offline target database."""

    source_path = root / "source" / "snapshot.sqlite3"
    target_path = root / "target" / "memory.sqlite3"
    backup_path = root / "backups" / "prewrite.sqlite3"
    approved_journals = approved_journal_rows()
    approved_digests = approved_digest_rows()
    _write_rows(
        source_path,
        journals=[*outside_journal_rows(), *approved_journals],
        digests=[*outside_digest_rows(), *approved_digests],
        seed_nontarget=True,
    )
    occupiers: list[dict[str, Any]] = []
    if occupy_source_ids:
        for row in approved_journals:
            occupiers.append(
                _journal_row(
                    entry_id=int(row["id"]),
                    scope_id="scope-occupied",
                    session_id=f"occupied-{row['id']}",
                    turn_number=9000 + int(row["id"]),
                    role="user",
                    content=f"occupied-placeholder-{row['id']}",
                    content_hash=f"e{int(row['id']):063x}",
                    created_at="2026-01-01T00:00:00+00:00",
                )
            )
    _write_rows(
        target_path,
        journals=occupiers,
        digests=[],
        seed_nontarget=True,
    )
    source_conn = open_fixture_connection(source_path)
    try:
        selected_journals = select_journal_window(
            source_conn, start=JOURNAL_WINDOW_START, end=JOURNAL_WINDOW_END
        )
        selected_digests = select_digest_window(
            source_conn, start=DIGEST_WINDOW_START, end=DIGEST_WINDOW_END
        )
        schema_digest = compute_schema_digest(source_conn)
        user_version = int(source_conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        source_conn.close()
    health = inspect_sqlite_health(source_path)
    if not health["ok"]:
        raise RuntimeError("synthetic source fixture is unhealthy")
    if user_version != SCHEMA_VERSION:
        raise RuntimeError("synthetic source fixture user_version drifted")
    return SourceRestorePair(
        source_path=source_path,
        target_path=target_path,
        backup_path=backup_path,
        journal_created_at_start=JOURNAL_WINDOW_START,
        journal_created_at_end=JOURNAL_WINDOW_END,
        digest_started_at_start=DIGEST_WINDOW_START,
        digest_started_at_end=DIGEST_WINDOW_END,
        expected_journal_count=len(selected_journals),
        expected_digest_run_count=len(selected_digests),
        expected_journal_set_digest=compute_journal_set_digest(selected_journals),
        expected_digest_run_set_digest=compute_digest_run_set_digest(selected_digests),
        expected_source_sha256=sha256_file(source_path),
        expected_schema_digest=schema_digest,
        expected_user_version=user_version,
        expected_target_epoch_digest=compute_target_epoch(target_path)["epoch_digest"],
        source_journal_ids=tuple(int(row["id"]) for row in selected_journals),
        source_digest_ids=tuple(str(row["id"]) for row in selected_digests),
    )


def plan_kwargs(pair: SourceRestorePair) -> dict[str, Any]:
    return {
        "source_path": pair.source_path,
        "target_path": pair.target_path,
        "journal_created_at_start": pair.journal_created_at_start,
        "journal_created_at_end": pair.journal_created_at_end,
        "digest_started_at_start": pair.digest_started_at_start,
        "digest_started_at_end": pair.digest_started_at_end,
        "expected_journal_count": pair.expected_journal_count,
        "expected_digest_run_count": pair.expected_digest_run_count,
        "expected_journal_set_digest": pair.expected_journal_set_digest,
        "expected_digest_run_set_digest": pair.expected_digest_run_set_digest,
        "expected_source_sha256": pair.expected_source_sha256,
        "expected_schema_digest": pair.expected_schema_digest,
        "expected_user_version": pair.expected_user_version,
        "dry_run": True,
        "maintenance_confirmed": False,
    }


def apply_kwargs(pair: SourceRestorePair) -> dict[str, Any]:
    payload = plan_kwargs(pair)
    payload.update(
        {
            "dry_run": False,
            "maintenance_confirmed": True,
            "expected_target_epoch_digest": pair.expected_target_epoch_digest,
            "prewrite_backup_path": pair.backup_path,
            "operation_id": f"op_jsr_{uuid.uuid4().hex}",
        }
    )
    return payload


def rebind_source_expectations(pair: SourceRestorePair) -> SourceRestorePair:
    """Recompute approved source bindings after an in-place snapshot mutation."""

    source_conn = open_fixture_connection(pair.source_path)
    try:
        selected_journals = select_journal_window(
            source_conn, start=pair.journal_created_at_start, end=pair.journal_created_at_end
        )
        selected_digests = select_digest_window(
            source_conn, start=pair.digest_started_at_start, end=pair.digest_started_at_end
        )
        schema_digest = compute_schema_digest(source_conn)
        user_version = int(source_conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        source_conn.close()
    return SourceRestorePair(
        source_path=pair.source_path,
        target_path=pair.target_path,
        backup_path=pair.backup_path,
        journal_created_at_start=pair.journal_created_at_start,
        journal_created_at_end=pair.journal_created_at_end,
        digest_started_at_start=pair.digest_started_at_start,
        digest_started_at_end=pair.digest_started_at_end,
        expected_journal_count=len(selected_journals),
        expected_digest_run_count=len(selected_digests),
        expected_journal_set_digest=compute_journal_set_digest(selected_journals),
        expected_digest_run_set_digest=compute_digest_run_set_digest(selected_digests),
        expected_source_sha256=sha256_file(pair.source_path),
        expected_schema_digest=schema_digest,
        expected_user_version=user_version,
        expected_target_epoch_digest=compute_target_epoch(pair.target_path)["epoch_digest"],
        source_journal_ids=tuple(int(row["id"]) for row in selected_journals),
        source_digest_ids=tuple(str(row["id"]) for row in selected_digests),
    )


def dangling_processed_run_count(path: Path) -> int:
    """Count journal rows whose processed_run_id has no digest-run row."""

    conn = open_fixture_connection(path)
    try:
        return int(
            conn.execute(
                """
                SELECT COUNT(*) FROM journal_entries
                WHERE processed_run_id != ''
                  AND NOT EXISTS (
                    SELECT 1 FROM journal_digest_runs
                    WHERE journal_digest_runs.id = journal_entries.processed_run_id
                  )
                """
            ).fetchone()[0]
        )
    finally:
        conn.close()


def cli_argv(kwargs: Mapping[str, Any], *, apply: bool = False) -> list[str]:
    """Build the official journal source-restore CLI argument vector."""

    args = [
        "--source",
        str(kwargs["source_path"]),
        "--target",
        str(kwargs["target_path"]),
        "--journal-created-at-start",
        str(kwargs["journal_created_at_start"]),
        "--journal-created-at-end",
        str(kwargs["journal_created_at_end"]),
        "--digest-started-at-start",
        str(kwargs["digest_started_at_start"]),
        "--digest-started-at-end",
        str(kwargs["digest_started_at_end"]),
        "--expected-journal-count",
        str(kwargs["expected_journal_count"]),
        "--expected-digest-run-count",
        str(kwargs["expected_digest_run_count"]),
        "--expected-journal-set-digest",
        str(kwargs["expected_journal_set_digest"]),
        "--expected-digest-run-set-digest",
        str(kwargs["expected_digest_run_set_digest"]),
        "--expected-source-sha256",
        str(kwargs["expected_source_sha256"]),
        "--expected-schema-digest",
        str(kwargs["expected_schema_digest"]),
        "--expected-user-version",
        str(kwargs["expected_user_version"]),
    ]
    if apply:
        args.extend(
            [
                "--apply",
                "--maintenance-confirmed",
                "--expected-target-epoch-digest",
                str(kwargs["expected_target_epoch_digest"]),
                "--prewrite-backup-path",
                str(kwargs["prewrite_backup_path"]),
                "--operation-id",
                str(kwargs.get("operation_id") or f"op_jsr_{uuid.uuid4().hex}"),
            ]
        )
    return args


def count_rows(path: Path, table: str) -> int:
    conn = open_fixture_connection(path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def journal_row_by_identity(
    path: Path,
    *,
    scope_id: str,
    session_id: str,
    turn_number: int,
    role: str,
    content_hash: str,
) -> sqlite3.Row | None:
    conn = open_fixture_connection(path)
    try:
        return conn.execute(
            """
            SELECT * FROM journal_entries
            WHERE scope_id = ? AND session_id = ? AND turn_number = ?
              AND role = ? AND content_hash = ?
            """,
            (scope_id, session_id, turn_number, role, content_hash),
        ).fetchone()
    finally:
        conn.close()


def sqlite_sequence_value(path: Path, table: str) -> int:
    conn = open_fixture_connection(path)
    try:
        row = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = ?",
            (table,),
        ).fetchone()
        return 0 if row is None else int(row[0])
    finally:
        conn.close()


def make_exploding_connection_factory(*, fail_on_insert: int) -> Callable[[Path], sqlite3.Connection]:
    """Return a Connection subclass factory that fails on a numbered INSERT."""

    state = {"inserts": 0}

    class ExplodingRestoreConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:  # type: ignore[override]
            text = str(sql).lstrip().upper()
            if text.startswith("INSERT"):
                state["inserts"] += 1
                if state["inserts"] == fail_on_insert:
                    raise sqlite3.OperationalError("injected_source_restore_failure")
            return super().execute(sql, parameters)

    def factory(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(os.fspath(path), factory=ExplodingRestoreConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    return factory
