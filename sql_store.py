from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .gating import compact_text

ENTRY_DELIMITER = "\n§\n"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            platform TEXT,
            user_id TEXT,
            chat_id TEXT,
            thread_id TEXT,
            gateway_session_key TEXT,
            agent_identity TEXT,
            agent_workspace TEXT,
            session_id TEXT,
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            content TEXT NOT NULL,
            summary TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_recalled_turn INTEGER NOT NULL DEFAULT 0
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            memory_id UNINDEXED,
            content,
            summary
        );
        CREATE INDEX IF NOT EXISTS idx_scope_recall_scope_updated
            ON memories(scope_id, updated_at DESC);
        """
    )
    ensure_memory_columns(conn)
    conn.commit()



def _add_memory_column(conn: sqlite3.Connection, column: str) -> None:
    allowed = {
        "chat_id": "ALTER TABLE memories ADD COLUMN chat_id TEXT",
        "thread_id": "ALTER TABLE memories ADD COLUMN thread_id TEXT",
        "gateway_session_key": "ALTER TABLE memories ADD COLUMN gateway_session_key TEXT",
    }
    statement = allowed.get(column)
    if statement is None:
        raise ValueError(f"unsupported memories column: {column}")
    conn.execute(statement)



def ensure_memory_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    for column in ("chat_id", "thread_id", "gateway_session_key"):
        if column not in existing:
            _add_memory_column(conn, column)



def store_row(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    scope_id: str,
    platform: str,
    user_id: str,
    chat_id: str,
    thread_id: str,
    gateway_session_key: str,
    agent_identity: str,
    agent_workspace: str,
    session_id: str,
    source: str,
    target: str,
    content: str,
) -> tuple[str, str, str]:
    now = datetime.now(timezone.utc).isoformat()
    summary = compact_text(content, 220)
    conn.execute(
        """
        INSERT INTO memories (
            id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
            agent_identity, agent_workspace,
            session_id, source, target, content, summary, created_at, updated_at, last_recalled_turn
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            memory_id,
            scope_id,
            platform,
            user_id,
            chat_id,
            thread_id,
            gateway_session_key,
            agent_identity,
            agent_workspace,
            session_id,
            source,
            target,
            content,
            summary,
            now,
            now,
        ),
    )
    conn.execute(
        "INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)",
        (memory_id, content, summary),
    )
    conn.commit()
    return memory_id, summary, now



def iter_curated_entries(hermes_home: Path | None) -> list[tuple[str, str, str]]:
    if hermes_home is None:
        return []
    memories_dir = hermes_home / "memories"
    output: list[tuple[str, str, str]] = []
    for filename, target in (("USER.md", "user"), ("MEMORY.md", "memory")):
        path = memories_dir / filename
        if not path.exists():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        entries = [entry.strip() for entry in raw.split(ENTRY_DELIMITER) if entry.strip()]
        if not entries and raw.strip():
            entries = [raw.strip()]
        try:
            updated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
        except OSError:
            updated_at = datetime.now(timezone.utc).isoformat()
        for entry in entries:
            output.append((target, entry, updated_at))
    return output



def curated_recall_item_id(target: str, content: str) -> str:
    return f"curated:{target}:{hashlib.sha1(content.encode('utf-8')).hexdigest()}"
