"""Approved/excluded selection, classification, reference closure, and remaps.

This module owns journal/digest window selection, excluded-tail comparison,
semantic classify-then-plain-INSERT, processed_run_id reference closure,
content-hash verification, insert order, and hashed remap evidence. It does
not open databases, take leases, or commit. Callers supply connections and
persist remap evidence only as salted hashes, never raw integer IDs.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

import sqlite3

from .journal_source_restore_snapshot import (
    JournalSourceRestoreError,
    canonical_json,
    sha256_text,
)

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
_JOURNAL_OPTIONAL_SEMANTIC_DEFAULTS = {
    "extraction_attempts": 0,
    "deferred_run_id": "",
    "deferred_at": None,
    "defer_count": 0,
    "retryable_failures": 0,
}
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
_JOURNAL_INSERT_FIELDS = JOURNAL_SEMANTIC_FIELDS
_DIGEST_INSERT_SQL = """
    INSERT INTO journal_digest_runs(
        id, started_at, finished_at, status, extractor, interval_label,
        processed_entries, inserted, updated, skipped, error, metadata
    ) VALUES (
        :id, :started_at, :finished_at, :status, :extractor, :interval_label,
        :processed_entries, :inserted, :updated, :skipped, :error, :metadata
    )
"""


def _normalized_field(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def _row_mapping(row: sqlite3.Row | Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {str(key): row[key] for key in row.keys()}


def _require_row_mapping(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    mapped = _row_mapping(row)
    if mapped is None:
        raise JournalSourceRestoreError("source_row_unreadable")
    return mapped


def _record(row: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: _normalized_field(row[field]) for field in fields}


def journal_identity_record(row: Mapping[str, Any]) -> dict[str, Any]:
    return _record(row, JOURNAL_SET_DIGEST_FIELDS)


def journal_semantic_record(row: Mapping[str, Any]) -> dict[str, Any]:
    mapped = _require_row_mapping(row)
    for field, default in _JOURNAL_OPTIONAL_SEMANTIC_DEFAULTS.items():
        if field not in mapped:
            mapped[field] = default
    return _record(mapped, JOURNAL_SEMANTIC_FIELDS)


def digest_run_logical_record(row: Mapping[str, Any]) -> dict[str, Any]:
    return _record(row, DIGEST_RUN_LOGICAL_FIELDS)


def journal_content_hash(content: str) -> str:
    """Match ``journal_store`` SHA-256 of UTF-8 content bytes."""

    return hashlib.sha256(str(content).encode("utf-8")).hexdigest()


def parse_aware_iso_timestamp(value: str, *, code: str) -> datetime:
    """Parse one aware ISO/RFC3339 timestamp; naive or malformed values refuse."""

    text = str(value or "").strip()
    if not text:
        raise JournalSourceRestoreError(code)
    if text.endswith("Z") and not text.endswith("+00:00"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise JournalSourceRestoreError(code) from exc
    if parsed.tzinfo is None:
        raise JournalSourceRestoreError(code)
    return parsed


def require_half_open_window(start: str, end: str, *, code: str) -> tuple[str, str]:
    """Validate a half-open aware window and return the exact stored strings."""

    start_text = str(start or "")
    end_text = str(end or "")
    start_at = parse_aware_iso_timestamp(start_text, code=code)
    end_at = parse_aware_iso_timestamp(end_text, code=code)
    if start_at >= end_at:
        raise JournalSourceRestoreError(code)
    return start_text, end_text


def compute_journal_set_digest(rows: Sequence[Mapping[str, Any]]) -> str:
    """Hash approved journal identities plus created_at in documented order."""

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


def compute_digest_run_set_digest(rows: Sequence[Mapping[str, Any]]) -> str:
    """Hash every stored digest-run field in started_at/id order."""

    records = [digest_run_logical_record(row) for row in rows]
    records.sort(key=lambda item: (str(item["started_at"] or ""), str(item["id"] or "")))
    return sha256_text("\n".join(canonical_json(item) for item in records))


def verify_journal_content_hashes(rows: Sequence[Mapping[str, Any]]) -> None:
    """Refuse when stored content_hash is not the repository hash of content."""

    for row in rows:
        stored = str(row.get("content_hash") or "")
        expected = journal_content_hash(str(row.get("content") or ""))
        if stored != expected:
            raise JournalSourceRestoreError("journal_content_hash_mismatch")


def select_journal_window(
    conn: sqlite3.Connection, *, start: str, end: str
) -> list[dict[str, Any]]:
    return [
        _require_row_mapping(row)
        for row in conn.execute(
            "SELECT * FROM journal_entries "
            "WHERE created_at >= ? AND created_at < ? "
            "ORDER BY created_at ASC, scope_id ASC, session_id ASC, "
            "turn_number ASC, role ASC, content_hash ASC",
            (start, end),
        ).fetchall()
    ]


def select_digest_window(
    conn: sqlite3.Connection, *, start: str, end: str
) -> list[dict[str, Any]]:
    return [
        _require_row_mapping(row)
        for row in conn.execute(
            "SELECT * FROM journal_digest_runs "
            "WHERE started_at >= ? AND started_at < ? "
            "ORDER BY started_at ASC, id ASC",
            (start, end),
        ).fetchall()
    ]


def lookup_target_journal(
    conn: sqlite3.Connection, row: Mapping[str, Any]
) -> dict[str, Any] | None:
    return _row_mapping(
        conn.execute(
            """
            SELECT * FROM journal_entries
            WHERE scope_id = ? AND session_id = ? AND turn_number = ?
              AND role = ? AND content_hash = ?
            """,
            (
                row["scope_id"],
                row["session_id"],
                int(row["turn_number"] or 0),
                row["role"],
                row["content_hash"],
            ),
        ).fetchone()
    )


def lookup_target_digest(conn: sqlite3.Connection, digest_id: str) -> dict[str, Any] | None:
    return _row_mapping(
        conn.execute(
            "SELECT * FROM journal_digest_runs WHERE id = ?",
            (digest_id,),
        ).fetchone()
    )


def lookup_source_digest(conn: sqlite3.Connection, digest_id: str) -> dict[str, Any] | None:
    return _row_mapping(
        conn.execute(
            "SELECT * FROM journal_digest_runs WHERE id = ?",
            (digest_id,),
        ).fetchone()
    )


def classify_rows(
    source_rows: Sequence[Mapping[str, Any]],
    target_lookup: Callable[[Mapping[str, Any]], Any],
    semantic: Callable[[Mapping[str, Any]], dict[str, Any]],
) -> tuple[list[Mapping[str, Any]], int, int]:
    missing: list[Mapping[str, Any]] = []
    already = 0
    conflicts = 0
    for row in source_rows:
        existing = target_lookup(row)
        if existing is None:
            missing.append(row)
            continue
        if semantic(existing) == semantic(row):
            already += 1
        else:
            conflicts += 1
    return missing, already, conflicts


def require_excluded_tail(
    source_rows: Sequence[Mapping[str, Any]],
    target_lookup: Callable[[Mapping[str, Any]], Any],
    semantic: Callable[[Mapping[str, Any]], dict[str, Any]],
    *,
    missing_code: str,
    conflict_code: str,
) -> None:
    """Refuse when an excluded-tail source row is missing or conflicts on target."""

    for row in source_rows:
        existing = target_lookup(row)
        if existing is None:
            raise JournalSourceRestoreError(missing_code)
        if semantic(existing) != semantic(row):
            raise JournalSourceRestoreError(conflict_code)


def require_digest_references(
    journals: Sequence[Mapping[str, Any]],
    selected_digests: Sequence[Mapping[str, Any]],
    *,
    source_lookup: Callable[[str], Mapping[str, Any] | None],
    target_lookup: Callable[[str], Mapping[str, Any] | None],
) -> None:
    """Close each non-empty processed_run_id via selected source or target control.

    A selected source digest row is sufficient. Otherwise the same TEXT id must
    already exist on target and match the source digest when the source still
    has that row. Missing or conflicting control refuses before backup.
    """

    selected_ids = {str(row["id"] or "") for row in selected_digests}
    for row in journals:
        run_id = str(row["processed_run_id"] or "")
        if not run_id or run_id in selected_ids:
            continue
        target = target_lookup(run_id)
        if target is None:
            raise JournalSourceRestoreError("dangling_digest_reference")
        source = source_lookup(run_id)
        if source is None:
            raise JournalSourceRestoreError("dangling_digest_reference")
        if digest_run_logical_record(source) != digest_run_logical_record(target):
            raise JournalSourceRestoreError("dangling_digest_reference")


def bind_selection(
    journals: Sequence[Mapping[str, Any]],
    digests: Sequence[Mapping[str, Any]],
    *,
    expected_journal_count: int,
    expected_digest_run_count: int,
    expected_journal_set_digest: str,
    expected_digest_run_set_digest: str,
) -> tuple[str, str]:
    journal_digest = compute_journal_set_digest(journals)
    digest_digest = compute_digest_run_set_digest(digests)
    if len(journals) != int(expected_journal_count) or len(digests) != int(
        expected_digest_run_count
    ):
        raise JournalSourceRestoreError("selection_count_mismatch")
    if journal_digest != str(expected_journal_set_digest or ""):
        raise JournalSourceRestoreError("journal_set_digest_mismatch")
    if digest_digest != str(expected_digest_run_set_digest or ""):
        raise JournalSourceRestoreError("digest_run_set_digest_mismatch")
    return journal_digest, digest_digest


def partition_insert_order(
    journals: Sequence[Mapping[str, Any]],
    digests: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Referenced digests, then journals, then unreferenced digest receipts."""

    referenced_ids = {
        str(row["processed_run_id"] or "")
        for row in journals
        if str(row["processed_run_id"] or "")
    }
    referenced = [row for row in digests if str(row["id"]) in referenced_ids]
    unreferenced = [row for row in digests if str(row["id"]) not in referenced_ids]
    referenced.sort(key=lambda row: (str(row["started_at"] or ""), str(row["id"] or "")))
    unreferenced.sort(key=lambda row: (str(row["started_at"] or ""), str(row["id"] or "")))
    journals_sorted = sorted(
        journals,
        key=lambda row: (
            str(row["created_at"] or ""),
            str(row["scope_id"] or ""),
            str(row["session_id"] or ""),
            int(row["turn_number"] or 0),
            str(row["role"] or ""),
            str(row["content_hash"] or ""),
        ),
    )
    return referenced, journals_sorted, unreferenced


def remap_pair_pseudonyms(
    *,
    operation_id: str,
    request_fingerprint: str,
    source_id: int,
    target_id: int,
) -> dict[str, str]:
    """Return salted hashes for one remap. Raw integers never leave this function."""

    material = f"{operation_id}\n{request_fingerprint}".encode("utf-8")
    source_mac = hmac.new(material, f"src:{source_id}".encode("utf-8"), hashlib.sha256).hexdigest()
    target_mac = hmac.new(material, f"tgt:{target_id}".encode("utf-8"), hashlib.sha256).hexdigest()
    return {"source": source_mac, "target": target_mac}


def mapping_evidence(pairs: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    records = [dict(item) for item in pairs]
    records.sort(key=canonical_json)
    return {
        "mapping_count": len(records),
        "mapping_digest": sha256_text("\n".join(canonical_json(item) for item in records)),
        "pairs": records,
    }


def _journal_table_columns(conn: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in conn.execute("PRAGMA table_info(journal_entries)")}


def _journal_insert_sql_and_bind(
    record: Mapping[str, Any],
    columns: set[str],
) -> tuple[str, dict[str, Any]]:
    """Bind only columns the target table actually has.

    Old targets omit later backlog columns. Mixed-schema apply is refused
    earlier; this keeps same-generation old→old inserts honest.
    """

    fields = [name for name in _JOURNAL_INSERT_FIELDS if name in columns]
    if not fields:
        raise JournalSourceRestoreError("target_journal_schema_unsupported")
    sql = (
        "INSERT INTO journal_entries("
        + ", ".join(fields)
        + ") VALUES ("
        + ", ".join(":" + name for name in fields)
        + ")"
    )
    return sql, {name: record[name] for name in fields}


def _session_resume_after_id(
    conn: sqlite3.Connection, *, scope_id: str, session_id: str
) -> int | None:
    """Read one target cursor without migrating journal_entries."""

    try:
        row = conn.execute(
            """
            SELECT resume_after_id FROM journal_session_digest_state
            WHERE scope_id = ? AND session_id = ?
            """,
            (scope_id, session_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return int(row[0] or 0)


def _session_cursor_is_unsafe(
    *,
    resume_after_id: int,
    unprocessed_ids: Sequence[int],
    remapped: bool,
) -> bool:
    if resume_after_id <= 0 or not unprocessed_ids:
        return False
    if remapped:
        return True
    return any(int(entry_id or 0) <= resume_after_id for entry_id in unprocessed_ids)


def _cursor_reset_evidence(sessions: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """Secret-free count plus a digest of hashed composite identities."""

    identities = sorted(
        sha256_text(canonical_json({"scope_id": scope_id, "session_id": session_id}))
        for scope_id, session_id in sessions
    )
    return {
        "cursor_reset_count": len(identities),
        "cursor_reset_digest": sha256_text("\n".join(identities)) if identities else "",
    }


def insert_missing_rows(
    conn: sqlite3.Connection,
    *,
    journals: Sequence[Mapping[str, Any]],
    digests: Sequence[Mapping[str, Any]],
    operation_id: str = "",
    request_fingerprint: str = "",
) -> tuple[int, int, bool, dict[str, Any]]:
    """Classify then plain-INSERT missing journal/digest rows.

    Journal and digest rows are never IGNORE/REPLACE/UPDATE/DELETE. Session
    digest cursors may be DELETE-reset in the same transaction only when
    restored unprocessed rows or ID remaps make a prior high cursor unsafe.
    """

    referenced, ordered_journals, unreferenced = partition_insert_order(journals, digests)
    journal_inserted = 0
    digest_inserted = 0
    remapping = False
    pairs: list[dict[str, str]] = []
    session_stats: dict[tuple[str, str], dict[str, Any]] = {}
    journal_columns = _journal_table_columns(conn)

    def insert_digest(row: Mapping[str, Any]) -> None:
        nonlocal digest_inserted
        existing = lookup_target_digest(conn, str(row["id"]))
        if existing is not None:
            if digest_run_logical_record(existing) != digest_run_logical_record(row):
                raise JournalSourceRestoreError("digest_logical_conflict")
            return
        conn.execute(_DIGEST_INSERT_SQL, digest_run_logical_record(row))
        digest_inserted += 1

    for row in referenced:
        insert_digest(row)
    for row in ordered_journals:
        existing = lookup_target_journal(conn, row)
        if existing is not None:
            if journal_semantic_record(existing) != journal_semantic_record(row):
                raise JournalSourceRestoreError("journal_logical_conflict")
            continue
        cursor = conn.execute(
            *_journal_insert_sql_and_bind(journal_semantic_record(row), journal_columns)
        )
        journal_inserted += 1
        new_id = int(cursor.lastrowid or 0)
        source_id = int(row["id"] or 0) if "id" in row else 0
        remapped_this = bool(new_id and source_id and new_id != source_id) or (
            bool(new_id) and source_id == 0
        )
        if remapped_this:
            remapping = True
        key = (str(row["scope_id"] or ""), str(row["session_id"] or ""))
        stats = session_stats.setdefault(
            key, {"unprocessed_ids": [], "remapped": False}
        )
        if remapped_this:
            stats["remapped"] = True
        if new_id and not str(row.get("processed_run_id") or ""):
            stats["unprocessed_ids"].append(new_id)
        if new_id:
            pairs.append(
                remap_pair_pseudonyms(
                    operation_id=operation_id,
                    request_fingerprint=request_fingerprint,
                    source_id=source_id,
                    target_id=new_id,
                )
            )
    for row in unreferenced:
        insert_digest(row)
    reset_sessions: list[tuple[str, str]] = []
    for (scope_id, session_id), stats in session_stats.items():
        resume = _session_resume_after_id(
            conn, scope_id=scope_id, session_id=session_id
        )
        if resume is None:
            continue
        if _session_cursor_is_unsafe(
            resume_after_id=resume,
            unprocessed_ids=stats["unprocessed_ids"],
            remapped=bool(stats["remapped"]),
        ):
            reset_sessions.append((scope_id, session_id))
    if reset_sessions:
        from .journal_store import reset_session_digest_cursors

        reset_session_digest_cursors(conn, sessions=reset_sessions, commit=False)
    evidence = mapping_evidence(pairs)
    evidence.update(_cursor_reset_evidence(reset_sessions))
    return journal_inserted, digest_inserted, remapping, evidence


__all__ = [
    "DIGEST_RUN_LOGICAL_FIELDS",
    "JOURNAL_SEMANTIC_FIELDS",
    "JOURNAL_SET_DIGEST_FIELDS",
    "bind_selection",
    "classify_rows",
    "compute_digest_run_set_digest",
    "compute_journal_set_digest",
    "digest_run_logical_record",
    "insert_missing_rows",
    "journal_content_hash",
    "journal_identity_record",
    "journal_semantic_record",
    "lookup_source_digest",
    "lookup_target_digest",
    "lookup_target_journal",
    "mapping_evidence",
    "parse_aware_iso_timestamp",
    "partition_insert_order",
    "remap_pair_pseudonyms",
    "require_digest_references",
    "require_excluded_tail",
    "require_half_open_window",
    "select_digest_window",
    "select_journal_window",
    "verify_journal_content_hashes",
]
