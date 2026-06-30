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

from .capture_filters import should_capture_text
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
    "append_journal_entry",
    "ensure_journal_schema",
    "load_unprocessed_journal_entries",
    "mark_entries_processed",
]

DATA_URL_PREFIX_RE = re.compile(r"data:[a-z0-9.+-]+/[a-z0-9.+-]+;base64,", re.IGNORECASE)
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


def _strip_inline_data_urls(text: str) -> str:
    match = DATA_URL_PREFIX_RE.search(text)
    if not match:
        return text
    media_type = text[match.start() : match.end()].split(";", 1)[0].removeprefix("data:") or "attachment"
    return clean_text(f"{text[:match.start()]}[inline {media_type} data omitted]")


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


def ensure_journal_schema(conn: sqlite3.Connection) -> None:
    """Create or migrate journal capture tables.

    This helper is safe to call from startup and tests; it should only establish schema, not process backlog."""
    conn.executescript(
        """
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
        """
    )
    conn.commit()


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
    conn.commit()
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
    ensure_journal_schema(conn)
    role = str(role or "").strip().lower()
    if role not in {"user", "assistant", "tool"}:
        return 0
    text = _strip_inline_data_urls(clean_text(content))
    if _looks_like_base64_blob(text):
        return 0
    if not text or not _journal_capture_allowed(text):
        return 0
    chunks = _chunk_journal_text(text)
    original_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    first_id = 0
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
    return first_id


def _row_to_entry(row: sqlite3.Row) -> JournalEntry:
    try:
        metadata = json.loads(str(row["metadata"] or "{}"))
    except Exception:
        metadata = {}
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
    )


def load_unprocessed_journal_entries(conn: sqlite3.Connection, *, scope_ids: list[str], limit: int = 500) -> list[JournalEntry]:
    ensure_journal_schema(conn)
    clean_scope_ids = [str(scope_id) for scope_id in scope_ids if str(scope_id)]
    if not clean_scope_ids:
        return []
    placeholders = ",".join("?" for _ in clean_scope_ids)
    rows = conn.execute(
        f"""
        SELECT * FROM journal_entries
        WHERE scope_id IN ({placeholders}) AND (processed_run_id IS NULL OR processed_run_id = '')
        ORDER BY created_at ASC, id ASC
        LIMIT ?
        """,
        [*clean_scope_ids, max(1, int(limit or 500))],
    ).fetchall()
    return [_row_to_entry(row) for row in rows]


def mark_entries_processed(conn: sqlite3.Connection, *, entry_ids: list[int], run_id: str) -> None:
    if not entry_ids:
        return
    placeholders = ",".join("?" for _ in entry_ids)
    conn.execute(
        f"UPDATE journal_entries SET processed_run_id = ?, processed_at = ? WHERE id IN ({placeholders})",
        [run_id, now_iso(), *[int(entry_id) for entry_id in entry_ids]],
    )
    conn.commit()


def _journal_unprocessed_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM journal_entries WHERE processed_run_id IS NULL OR processed_run_id = ''").fetchone()[0]
    )


def _prune_processed_journal(conn: sqlite3.Connection, *, retention_days: int) -> int:
    if retention_days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    rows = conn.execute(
        """
        SELECT id FROM journal_entries
        WHERE processed_run_id != '' AND created_at < ?
        """,
        (cutoff,),
    ).fetchall()
    entry_ids = [int(row["id"]) for row in rows]
    if not entry_ids:
        return 0
    placeholders = ",".join("?" for _ in entry_ids)
    conn.execute(f"DELETE FROM memory_journal_sources WHERE journal_entry_id IN ({placeholders})", entry_ids)
    conn.execute(f"DELETE FROM journal_rejections WHERE journal_entry_id IN ({placeholders})", entry_ids)
    conn.execute(f"DELETE FROM journal_entries WHERE id IN ({placeholders})", entry_ids)
    conn.commit()
    return len(entry_ids)
