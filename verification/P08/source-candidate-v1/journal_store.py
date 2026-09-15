"""SQLite journal storage primitives for capture, chunking, processed flags, and backlog loading.

Journal rows are operational evidence; schema helpers must be idempotent and safe to call from runtime startup."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .capture_filters import sanitize_capture_text, should_capture_text
from .digest_state import next_session_resume_after_id
from .gating import clean_text
from .models import RuntimeScope
from .sql_store import now_iso

__all__ = [
    "BASE64ISH_RE",
    "DATA_URL_PREFIX_RE",
    "JournalEntry",
    "_chunk_journal_text",
    "_insert_journal_entry",
    "_journal_capture_allowed",
    "_journal_entry_for_digest",
    "_journal_unprocessed_count",
    "_looks_like_base64_blob",
    "_metadata_json",
    "_prune_processed_journal",
    "_row_to_entry",
    "_strip_inline_data_urls",
    "advance_session_digest_cursors",
    "append_journal_entry",
    "clear_current_deferral",
    "ensure_journal_schema",
    "increment_extraction_attempts",
    "increment_retryable_failures",
    "journal_entry_group_identity",
    "load_session_digest_state",
    "load_unprocessed_journal_entries",
    "mark_entries_deferred",
    "mark_entries_processed",
    "reset_retryable_failures",
    "reset_session_digest_cursors",
    "upsert_session_digest_state",
]

DATA_URL_PREFIX_RE = re.compile(r"data:[a-z0-9.+-]+/[a-z0-9.+-]+;base64,", re.IGNORECASE)
INLINE_DATA_URL_RE = re.compile(
    r"data:[a-z0-9.+-]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/]*={0,2}",
    re.IGNORECASE,
)
BASE64_CONTINUATION_RE = re.compile(r"^[ \t]*[A-Za-z0-9+/]{64,}={0,2}(?=$|[ \t])")
BASE64ISH_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")


@dataclass
class JournalEntry:
    id: int
    scope_id: str
    shared_scope_id: str
    session_id: str
    turn_number: int
    role: str
    content: str
    created_at: str
    processed_run_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    extraction_attempts: int = 0


def journal_entry_group_identity(entry: Any) -> tuple[str, str]:
    """Return the raw stored ``(scope_id, session_id)`` pair used for grouping.

    Empty or missing parts stay empty strings. Callers must not remap an empty
    session_id to the display label ``unknown``, and must not concatenate the
    pair into a single string key. This is grouping identity, not the fail-closed
    provenance helper that returns ``None`` when either part is missing.
    """

    return (
        str(getattr(entry, "scope_id", "") or ""),
        str(getattr(entry, "session_id", "") or ""),
    )


def _strip_inline_data_urls(text: str) -> str:
    """Compatibility wrapper for the unified capture/storage sanitizer."""

    return sanitize_capture_text(text)


def _looks_like_base64_blob(text: str) -> bool:
    raw = str(text or "").strip()
    compact = re.sub(r"\s+", "", raw)
    if len(compact) < 500:
        return False
    if not BASE64ISH_RE.fullmatch(compact):
        return False
    # Avoid treating ordinary long English/ASCII prose as binary just because
    # it happens to use only base64 alphabet characters plus spaces. Real base64
    # payload chunks are usually one long run or line-wrapped into long rows;
    # prose is word-wrapped into many short tokens.
    tokens = re.split(r"\s+", raw)
    if len(tokens) > 1 and max((len(token) for token in tokens), default=0) < 64:
        return False
    return True


def _journal_entry_for_digest(entry: JournalEntry) -> JournalEntry | None:
    stripped = _strip_inline_data_urls(entry.content)
    if stripped != entry.content:
        metadata = dict(entry.metadata)
        metadata["inline_data_redacted"] = True
        return JournalEntry(
            id=entry.id,
            scope_id=entry.scope_id,
            shared_scope_id=entry.shared_scope_id,
            session_id=entry.session_id,
            turn_number=entry.turn_number,
            role=entry.role,
            content=stripped,
            created_at=entry.created_at,
            processed_run_id=entry.processed_run_id,
            metadata=metadata,
        )
    if _looks_like_base64_blob(entry.content):
        return None
    return entry


def ensure_journal_schema(
    conn: sqlite3.Connection,
    *,
    commit: bool = True,
) -> None:
    """Create or migrate journal capture tables.

    This helper is safe to call from startup and tests; it should only establish schema, not process backlog."""
    schema_script = """
        CREATE TABLE IF NOT EXISTS journal_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_id TEXT NOT NULL,
            shared_scope_id TEXT NOT NULL,
            platform TEXT,
            user_id TEXT,
            chat_id TEXT,
            thread_id TEXT,
            gateway_session_key TEXT,
            agent_identity TEXT,
            agent_workspace TEXT,
            session_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL DEFAULT 0,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            processed_run_id TEXT NOT NULL DEFAULT '',
            processed_at TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            UNIQUE(scope_id, session_id, turn_number, role, content_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_scope_recall_journal_unprocessed
            ON journal_entries(scope_id, processed_run_id, created_at, id);
        CREATE INDEX IF NOT EXISTS idx_scope_recall_journal_session
            ON journal_entries(session_id, turn_number, id);

        CREATE TABLE IF NOT EXISTS journal_digest_runs (
            id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            extractor TEXT NOT NULL,
            interval_label TEXT NOT NULL DEFAULT '',
            processed_entries INTEGER NOT NULL DEFAULT 0,
            inserted INTEGER NOT NULL DEFAULT 0,
            updated INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            metadata TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_scope_recall_journal_digest_started
            ON journal_digest_runs(started_at DESC);

        CREATE TABLE IF NOT EXISTS memory_journal_sources (
            memory_id TEXT NOT NULL,
            journal_entry_id INTEGER NOT NULL,
            run_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(memory_id, journal_entry_id)
        );
        CREATE INDEX IF NOT EXISTS idx_scope_recall_memory_journal_memory
            ON memory_journal_sources(memory_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_scope_recall_memory_journal_entry
            ON memory_journal_sources(journal_entry_id);

        CREATE TABLE IF NOT EXISTS journal_rejections (
            journal_entry_id INTEGER NOT NULL,
            run_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            candidate TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY(journal_entry_id, run_id)
        );
        CREATE INDEX IF NOT EXISTS idx_scope_recall_journal_rejection_entry
            ON journal_rejections(journal_entry_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS journal_session_digest_state (
            scope_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            resume_after_id INTEGER NOT NULL DEFAULT 0,
            last_run_id TEXT NOT NULL DEFAULT '',
            defer_rounds INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (scope_id, session_id)
        );
        CREATE INDEX IF NOT EXISTS idx_scope_recall_journal_session_digest_state
            ON journal_session_digest_state(scope_id, resume_after_id);
        """
    for statement in schema_script.split(";"):
        if statement.strip():
            conn.execute(statement)
    _ensure_journal_backlog_columns(conn)
    if commit:
        conn.commit()


_JOURNAL_BACKLOG_COLUMNS = (
    # issue #45: bounded retry accounting for entries the LLM extractor keeps
    # classifying as unresolved; issue #46: visible deferral state for entries
    # loaded but pushed past the per-session chunk budget.
    ("extraction_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("deferred_run_id", "TEXT NOT NULL DEFAULT ''"),
    ("deferred_at", "TEXT"),
    ("defer_count", "INTEGER NOT NULL DEFAULT 0"),
    ("retryable_failures", "INTEGER NOT NULL DEFAULT 0"),
)


def _ensure_journal_backlog_columns(conn: sqlite3.Connection) -> None:
    for name, declaration in _JOURNAL_BACKLOG_COLUMNS:
        try:
            conn.execute(
                f"ALTER TABLE journal_entries ADD COLUMN {name} {declaration}"
            )
        except sqlite3.OperationalError as exc:
            # Concurrent first-boot initializes can race PRAGMA/ADD COLUMN the
            # same way sql_store's memory columns do; already-present columns
            # are success, everything else stays fail-closed.
            if "duplicate column name" not in str(exc).lower():
                raise


def _metadata_json(metadata: dict[str, Any] | None) -> str:
    return json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)


def _journal_capture_allowed(text: str) -> bool:
    # Journal storage must not drop valuable long task instructions.  Re-run the
    # normal safety filter with the length gate disabled, then chunk below.
    return should_capture_text(text, {"capture_hard_max_chars": -1}).allowed


def _chunk_journal_text(text: str, *, chunk_chars: int = 2000) -> list[str]:
    if len(text) <= chunk_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_chars)
        if end < len(text):
            # Prefer a nearby natural boundary, but never make tiny chunks.
            boundary = max(text.rfind("\n", start + chunk_chars // 2, end), text.rfind("。", start + chunk_chars // 2, end))
            if boundary > start:
                end = boundary + 1
        chunks.append(text[start:end])
        start = end
    return [chunk for chunk in chunks if chunk]


def _insert_journal_entry(
    conn: sqlite3.Connection,
    *,
    scope: RuntimeScope,
    scope_id: str,
    shared_scope_id: str,
    session_id: str,
    turn_number: int,
    role: str,
    text: str,
    metadata: dict[str, Any] | None = None,
) -> int:
    text = sanitize_capture_text(text)
    if not text:
        return 0
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    created_at = now_iso()
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO journal_entries(
            scope_id, shared_scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
            agent_identity, agent_workspace, session_id, turn_number, role, content, content_hash,
            created_at, processed_run_id, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)
        """,
        (
            scope_id,
            shared_scope_id,
            scope.platform,
            scope.user_id,
            scope.chat_id,
            scope.thread_id,
            scope.gateway_session_key,
            scope.agent_identity,
            scope.agent_workspace,
            session_id,
            int(turn_number or 0),
            role,
            text,
            content_hash,
            created_at,
            _metadata_json(metadata),
        ),
    )
    if cur.rowcount == 0:
        row = conn.execute(
            """
            SELECT id FROM journal_entries
            WHERE scope_id = ? AND session_id = ? AND turn_number = ? AND role = ? AND content_hash = ?
            """,
            (scope_id, session_id, int(turn_number or 0), role, content_hash),
        ).fetchone()
        return int(row["id"] if row else 0)
    return int(cur.lastrowid or 0)


def append_journal_entry(
    conn: sqlite3.Connection,
    *,
    scope: RuntimeScope,
    scope_id: str,
    shared_scope_id: str,
    session_id: str,
    turn_number: int,
    role: str,
    content: Any,
    metadata: dict[str, Any] | None = None,
) -> int:
    role = str(role or "").strip().lower()
    if role not in {"user", "assistant", "tool"}:
        return 0
    joined_outer = bool(getattr(conn, "in_transaction", False))
    ensure_journal_schema(conn, commit=False)
    text = _strip_inline_data_urls(clean_text(content))
    if _looks_like_base64_blob(text):
        return 0
    if not text or not _journal_capture_allowed(text):
        return 0
    chunks = _chunk_journal_text(text)
    original_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    first_id = 0
    savepoint = "journal_append" if joined_outer else None
    if savepoint:
        conn.execute(f"SAVEPOINT {savepoint}")
    try:
        for index, chunk in enumerate(chunks, start=1):
            chunk_metadata = dict(metadata or {})
            if len(chunks) > 1:
                chunk_metadata.update(
                    {
                        "chunk_index": index,
                        "chunk_count": len(chunks),
                        "original_content_hash": original_hash,
                        "original_length": len(text),
                        "chunking": "bounded-journal-content",
                    }
                )
            inserted_id = _insert_journal_entry(
                conn,
                scope=scope,
                scope_id=scope_id,
                shared_scope_id=shared_scope_id,
                session_id=session_id,
                turn_number=turn_number,
                role=role,
                text=chunk,
                metadata=chunk_metadata,
            )
            if not first_id:
                first_id = inserted_id
        if savepoint:
            conn.execute(f"RELEASE {savepoint}")
        elif first_id and conn.in_transaction:
            conn.commit()
    except Exception:
        if savepoint:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
        elif conn.in_transaction:
            conn.rollback()
        raise
    return first_id


def _row_to_entry(row: sqlite3.Row) -> JournalEntry:
    try:
        metadata = json.loads(str(row["metadata"] or "{}"))
    except Exception:
        metadata = {}
    try:
        extraction_attempts = int(row["extraction_attempts"] or 0)
    except (IndexError, KeyError):
        extraction_attempts = 0
    return JournalEntry(
        id=int(row["id"]),
        scope_id=str(row["scope_id"]),
        shared_scope_id=str(row["shared_scope_id"]),
        session_id=str(row["session_id"]),
        turn_number=int(row["turn_number"] or 0),
        role=str(row["role"]),
        content=str(row["content"]),
        created_at=str(row["created_at"]),
        processed_run_id=str(row["processed_run_id"] or ""),
        metadata=metadata if isinstance(metadata, dict) else {},
        extraction_attempts=extraction_attempts,
    )


def effective_per_session_limit(
    configured: object, run_limit: int, session_count: int = 1
) -> int:
    """Cap each session only when more than one session is waiting.

    A default of 200 with max_entries_per_digest=80 used to no-op: the
    cap was larger than the window, so one fat old session ate every
    digest. One-session backlogs must still be allowed to fill the
    window.
    """

    bound = max(1, int(run_limit or 1))
    sessions = max(1, int(session_count or 1))
    try:
        raw = int(configured) if configured not in (None, "") else 0  # type: ignore[reportArgumentType]
    except (TypeError, ValueError):
        raw = 0
    if sessions <= 1:
        return bound if raw <= 0 or raw >= bound else min(raw, bound)
    fair = max(1, bound // min(sessions, 8))
    if raw <= 0 or raw >= bound:
        return fair
    return max(1, min(raw, bound - 1))


def load_unprocessed_journal_entries(
    conn: sqlite3.Connection,
    *,
    scope_ids: list[str],
    limit: int = 500,
    excluded_chat_ids: frozenset[str] | set[str] | list[str] | tuple[str, ...] = (),
    per_session_limit: int = 0,
) -> list[JournalEntry]:
    """Load unprocessed entries honoring the per-session resume cursor.

    ``journal_session_digest_state.resume_after_id`` is the durable scheduler:
    each session contributes only the unprocessed rows after that cursor, then
    wraps once that side is empty. ``per_session_limit`` still keeps one fat
    session from monopolizing a multi-session window.
    """

    ensure_journal_schema(conn, commit=False)
    clean_scope_ids = [str(scope_id) for scope_id in scope_ids if str(scope_id)]
    if not clean_scope_ids:
        return []
    clean_excluded = sorted(
        {str(chat_id).strip() for chat_id in excluded_chat_ids if str(chat_id).strip()}
    )
    placeholders = ",".join("?" for _ in clean_scope_ids)
    exclusion_sql = ""
    aliased_exclusion_sql = ""
    purge_exclusion_sql = ""
    aliased_purge_exclusion_sql = ""
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='privacy_purge_source_tombstones'"
    ).fetchone() is not None:
        purge_exclusion_sql = (
            " AND NOT EXISTS (SELECT 1 FROM privacy_purge_source_tombstones ppst "
            "WHERE ppst.journal_entry_id = journal_entries.id)"
        )
        aliased_purge_exclusion_sql = (
            " AND NOT EXISTS (SELECT 1 FROM privacy_purge_source_tombstones ppst "
            "WHERE ppst.journal_entry_id = je.id)"
        )
    params: list[object] = [*clean_scope_ids]
    if clean_excluded:
        excluded_placeholders = ",".join("?" for _ in clean_excluded)
        exclusion_sql = f" AND COALESCE(chat_id, '') NOT IN ({excluded_placeholders})"
        aliased_exclusion_sql = (
            f" AND COALESCE(je.chat_id, '') NOT IN ({excluded_placeholders})"
        )
        params.extend(clean_excluded)
    session_count_sql = f"""
        SELECT COUNT(*) FROM (
            SELECT DISTINCT scope_id, session_id FROM journal_entries
            WHERE scope_id IN ({placeholders})
              AND (processed_run_id IS NULL OR processed_run_id = '')
              {exclusion_sql}
              {purge_exclusion_sql}
        )
    """
    session_count = int(conn.execute(session_count_sql, params).fetchone()[0] or 0)
    session_cap = effective_per_session_limit(
        per_session_limit if per_session_limit else None,
        max(1, int(limit or 500)),
        session_count,
    )
    active_sql = f"""
        WITH eligible AS (
            SELECT
                je.*,
                CASE
                    WHEN je.id > COALESCE(jsds.resume_after_id, 0) THEN 0
                    ELSE 1
                END AS scope_recall_wrap_side
            FROM journal_entries je
            LEFT JOIN journal_session_digest_state jsds
              ON jsds.scope_id = je.scope_id
             AND jsds.session_id = je.session_id
            WHERE je.scope_id IN ({placeholders})
              AND (je.processed_run_id IS NULL OR je.processed_run_id = '')
              {aliased_exclusion_sql}
              {aliased_purge_exclusion_sql}
        ),
        session_side AS (
            SELECT scope_id, session_id, MIN(scope_recall_wrap_side) AS active_side
            FROM eligible
            GROUP BY scope_id, session_id
        ),
        active AS (
            SELECT e.*
            FROM eligible e
            JOIN session_side s
              ON s.scope_id = e.scope_id
             AND s.session_id = e.session_id
             AND e.scope_recall_wrap_side = s.active_side
        )
    """
    if session_count > 1 and session_cap < max(1, int(limit or 500)):
        query = f"""
            {active_sql}
            SELECT * FROM (
                SELECT active.*, ROW_NUMBER() OVER (
                    PARTITION BY scope_id, session_id
                    ORDER BY created_at ASC, id ASC
                ) AS scope_recall_session_rank
                FROM active
            )
            WHERE scope_recall_session_rank <= ?
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """
        query_params = [*params, session_cap, max(1, int(limit or 500))]
    else:
        query = f"""
            {active_sql}
            SELECT * FROM active
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """
        query_params = [*params, max(1, int(limit or 500))]
    rows = conn.execute(query, query_params).fetchall()
    return [_row_to_entry(row) for row in rows]


def mark_entries_processed(
    conn: sqlite3.Connection,
    *,
    entry_ids: list[int],
    run_id: str,
    commit: bool = True,
) -> None:
    if not entry_ids:
        return
    placeholders = ",".join("?" for _ in entry_ids)
    conn.execute(
        f"UPDATE journal_entries SET processed_run_id = ?, processed_at = ?, "
        f"deferred_run_id = '', deferred_at = NULL WHERE id IN ({placeholders})",
        [run_id, now_iso(), *[int(entry_id) for entry_id in entry_ids]],
    )
    if commit:
        conn.commit()


def mark_entries_deferred(
    conn: sqlite3.Connection,
    *,
    entry_ids: list[int],
    run_id: str,
    commit: bool = True,
) -> None:
    """Record budget overflow and increment the per-entry deferral count.

    Deferred is not terminal and not a provider-retry failure. The loader
    resumes from ``journal_session_digest_state`` rather than treating these
    columns as a last-seen marker.
    """

    if not entry_ids:
        return
    placeholders = ",".join("?" for _ in entry_ids)
    conn.execute(
        f"UPDATE journal_entries SET deferred_run_id = ?, deferred_at = ?, "
        f"defer_count = defer_count + 1 WHERE id IN ({placeholders})",
        [run_id, now_iso(), *[int(entry_id) for entry_id in entry_ids]],
    )
    if commit:
        conn.commit()


def clear_current_deferral(
    conn: sqlite3.Connection,
    *,
    entry_ids: list[int],
    commit: bool = True,
) -> None:
    """Clear current budget-deferral markers without resetting ``defer_count``.

    Call this on exactly the covered / non-deferred IDs from one digest
    window (``commit=False`` when the caller owns the outer transaction).
    Historical ``defer_count`` stays as churn metadata, not a fifth state.
    """

    clean_ids = [int(entry_id) for entry_id in entry_ids]
    if not clean_ids:
        return
    placeholders = ",".join("?" for _ in clean_ids)
    conn.execute(
        f"UPDATE journal_entries SET deferred_run_id = '', deferred_at = NULL "
        f"WHERE id IN ({placeholders})",
        clean_ids,
    )
    if commit:
        conn.commit()


def increment_extraction_attempts(
    conn: sqlite3.Connection,
    *,
    entry_ids: list[int],
    commit: bool = True,
) -> dict[int, int]:
    """Increment per-entry unresolved-extraction attempts and return new counts."""

    clean_ids = [int(entry_id) for entry_id in entry_ids]
    if not clean_ids:
        return {}
    placeholders = ",".join("?" for _ in clean_ids)
    conn.execute(
        f"UPDATE journal_entries SET extraction_attempts = extraction_attempts + 1 "
        f"WHERE id IN ({placeholders})",
        clean_ids,
    )
    clear_current_deferral(conn, entry_ids=clean_ids, commit=False)
    rows = conn.execute(
        f"SELECT id, extraction_attempts FROM journal_entries WHERE id IN ({placeholders})",
        clean_ids,
    ).fetchall()
    if commit:
        conn.commit()
    return {int(row["id"]): int(row["extraction_attempts"] or 0) for row in rows}


def increment_retryable_failures(
    conn: sqlite3.Connection,
    *,
    entry_ids: list[int],
    commit: bool = True,
) -> dict[int, int]:
    """Increment durable retryable-provider failures and return new counts.

    This counter is independent of ``extraction_attempts`` so one transient
    outage does not burn the ordinary extraction-quality budget, while a
    persistent retryable failure still has a finite cross-run exit. Callers
    pass ``commit=False`` when they own the digest transaction. Only IDs
    proven to have reached the extractor may be supplied.
    """

    clean_ids = [int(entry_id) for entry_id in entry_ids]
    if not clean_ids:
        return {}
    placeholders = ",".join("?" for _ in clean_ids)
    conn.execute(
        f"UPDATE journal_entries SET retryable_failures = retryable_failures + 1 "
        f"WHERE id IN ({placeholders})",
        clean_ids,
    )
    rows = conn.execute(
        f"SELECT id, retryable_failures FROM journal_entries WHERE id IN ({placeholders})",
        clean_ids,
    ).fetchall()
    if commit:
        conn.commit()
    return {int(row["id"]): int(row["retryable_failures"] or 0) for row in rows}


def reset_retryable_failures(
    conn: sqlite3.Connection,
    *,
    entry_ids: list[int],
    commit: bool = True,
) -> dict[int, int]:
    """Clear durable retryable-failure counts after a non-retryable attempt.

    The active budget is consecutive attempted failures. A later successful or
    deterministic extractor outcome on the same row must not keep accumulating
    stale outage counts. Only the supplied, actually-attempted IDs are touched.
    """

    clean_ids = [int(entry_id) for entry_id in entry_ids]
    if not clean_ids:
        return {}
    placeholders = ",".join("?" for _ in clean_ids)
    conn.execute(
        f"UPDATE journal_entries SET retryable_failures = 0 "
        f"WHERE id IN ({placeholders})",
        clean_ids,
    )
    rows = conn.execute(
        f"SELECT id, retryable_failures FROM journal_entries WHERE id IN ({placeholders})",
        clean_ids,
    ).fetchall()
    if commit:
        conn.commit()
    return {int(row["id"]): int(row["retryable_failures"] or 0) for row in rows}


def load_session_digest_state(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    session_id: str,
) -> dict[str, Any] | None:
    """Return the durable resume cursor for one session, if present."""

    ensure_journal_schema(conn, commit=False)
    row = conn.execute(
        """
        SELECT resume_after_id, last_run_id, defer_rounds, updated_at
        FROM journal_session_digest_state
        WHERE scope_id = ? AND session_id = ?
        """,
        (scope_id, session_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "resume_after_id": int(row["resume_after_id"] or 0),
        "last_run_id": str(row["last_run_id"] or ""),
        "defer_rounds": int(row["defer_rounds"] or 0),
        "updated_at": str(row["updated_at"] or ""),
    }


def upsert_session_digest_state(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    session_id: str,
    resume_after_id: int,
    run_id: str,
    deferred_this_run: bool = False,
    commit: bool = True,
) -> None:
    """Persist the next load cursor for one session."""

    ensure_journal_schema(conn, commit=False)
    increment = 1 if deferred_this_run else 0
    conn.execute(
        """
        INSERT INTO journal_session_digest_state(
            scope_id, session_id, resume_after_id, last_run_id, defer_rounds, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(scope_id, session_id) DO UPDATE SET
            resume_after_id = excluded.resume_after_id,
            last_run_id = excluded.last_run_id,
            defer_rounds = journal_session_digest_state.defer_rounds + excluded.defer_rounds,
            updated_at = excluded.updated_at
        """,
        (
            scope_id,
            session_id,
            int(resume_after_id or 0),
            str(run_id or ""),
            increment,
            now_iso(),
        ),
    )
    if commit:
        conn.commit()


def advance_session_digest_cursors(
    conn: sqlite3.Connection,
    *,
    entries: list[JournalEntry],
    covered_ids: set[int],
    deferred_ids: set[int],
    run_id: str,
    commit: bool = True,
) -> None:
    """Advance each loaded session past the last covered entry this run."""

    grouped: dict[tuple[str, str], list[int]] = {}
    for entry in entries:
        grouped.setdefault(journal_entry_group_identity(entry), []).append(int(entry.id))
    deferred = {int(entry_id) for entry_id in deferred_ids}
    covered = {int(entry_id) for entry_id in covered_ids}
    for (scope_id, session_id), loaded in grouped.items():
        session_covered = [entry_id for entry_id in loaded if entry_id in covered]
        upsert_session_digest_state(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            resume_after_id=next_session_resume_after_id(
                covered_ids=session_covered,
                loaded_ids=loaded,
            ),
            run_id=run_id,
            deferred_this_run=bool(deferred.intersection(loaded)),
            commit=False,
        )
    if commit:
        conn.commit()


def reset_session_digest_cursors(
    conn: sqlite3.Connection,
    *,
    sessions: list[tuple[str, str]] | set[tuple[str, str]],
    commit: bool = True,
) -> None:
    """DELETE resume cursors for sessions whose restored unprocessed IDs are unsafe.

    Source-restore must not copy ``journal_session_digest_state``. Callers
    reset only sessions where remapped or newly inserted unprocessed IDs
    would hide behind a prior high ``resume_after_id``.
    """

    pairs = {
        (str(scope_id), str(session_id))
        for scope_id, session_id in sessions
        if str(scope_id) and str(session_id)
    }
    for scope_id, session_id in pairs:
        conn.execute(
            "DELETE FROM journal_session_digest_state "
            "WHERE scope_id = ? AND session_id = ?",
            (scope_id, session_id),
        )
    if commit:
        conn.commit()


def _journal_unprocessed_count(
    conn: sqlite3.Connection,
    *,
    excluded_chat_ids: frozenset[str] | set[str] | list[str] | tuple[str, ...] = (),
) -> int:
    clean_excluded = sorted(
        {str(chat_id).strip() for chat_id in excluded_chat_ids if str(chat_id).strip()}
    )
    if not clean_excluded:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM journal_entries "
                "WHERE processed_run_id IS NULL OR processed_run_id = ''"
            ).fetchone()[0]
        )
    placeholders = ",".join("?" for _ in clean_excluded)
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM journal_entries "
            "WHERE (processed_run_id IS NULL OR processed_run_id = '') "
            f"AND COALESCE(chat_id, '') NOT IN ({placeholders})",
            clean_excluded,
        ).fetchone()[0]
    )


def _prune_processed_journal(conn: sqlite3.Connection, *, retention_days: int, commit: bool = True) -> int:
    """Delete retained journal rows in batches below SQLite's variable limit."""

    if retention_days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    variable_limit = conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
    batch_size = max(1, min(400, int(variable_limit)))
    pruned = 0
    try:
        while True:
            rows = conn.execute(
                """
                SELECT id FROM journal_entries
                WHERE processed_run_id != '' AND created_at < ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (cutoff, batch_size),
            ).fetchall()
            entry_ids = [int(row["id"]) for row in rows]
            if not entry_ids:
                break
            placeholders = ",".join("?" for _ in entry_ids)
            conn.execute(
                f"DELETE FROM memory_journal_sources WHERE journal_entry_id IN ({placeholders})",
                entry_ids,
            )
            conn.execute(
                f"DELETE FROM journal_rejections WHERE journal_entry_id IN ({placeholders})",
                entry_ids,
            )
            conn.execute(
                f"DELETE FROM journal_entries WHERE id IN ({placeholders})",
                entry_ids,
            )
            pruned += len(entry_ids)
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    return pruned
