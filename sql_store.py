from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .gating import compact_text, dedup_key
from .governance import classify_memory

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
    rebuild_fts_if_empty(conn)
    conn.commit()


def _add_memory_column(conn: sqlite3.Connection, column: str) -> None:
    allowed = {
        "chat_id": "ALTER TABLE memories ADD COLUMN chat_id TEXT",
        "thread_id": "ALTER TABLE memories ADD COLUMN thread_id TEXT",
        "gateway_session_key": "ALTER TABLE memories ADD COLUMN gateway_session_key TEXT",
        "dedup_key": "ALTER TABLE memories ADD COLUMN dedup_key TEXT",
        "metadata": "ALTER TABLE memories ADD COLUMN metadata TEXT",
    }
    statement = allowed.get(column)
    if statement is None:
        raise ValueError(f"unsupported memories column: {column}")
    conn.execute(statement)


def ensure_memory_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    for column in ("chat_id", "thread_id", "gateway_session_key", "dedup_key", "metadata"):
        if column not in existing:
            _add_memory_column(conn, column)
    for row in conn.execute("SELECT id, content FROM memories WHERE dedup_key IS NULL OR dedup_key = ''").fetchall():
        conn.execute("UPDATE memories SET dedup_key = ? WHERE id = ?", (dedup_key(str(row["content"])), row["id"]))
    conn.execute("UPDATE memories SET metadata = '{}' WHERE metadata IS NULL OR metadata = ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scope_recall_dedup ON memories(scope_id, target, dedup_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scope_recall_target_updated ON memories(target, updated_at DESC)")


def rebuild_fts_if_empty(conn: sqlite3.Connection) -> None:
    memory_count = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
    fts_count = int(conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0])
    if memory_count and fts_count == 0:
        conn.execute("INSERT INTO memories_fts(memory_id, content, summary) SELECT id, content, summary FROM memories")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    metadata: str = "{}",
    allow_duplicate: bool = False,
) -> tuple[str, str, str, bool]:
    now = now_iso()
    summary = compact_text(content, 220)
    key = dedup_key(content)
    if not allow_duplicate:
        existing = conn.execute(
            """
            SELECT id, summary, updated_at
            FROM memories
            WHERE scope_id = ? AND target = ? AND dedup_key = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (scope_id, target, key),
        ).fetchone()
        if existing is not None:
            conn.execute("UPDATE memories SET updated_at = ? WHERE id = ?", (now, existing["id"]))
            conn.commit()
            return str(existing["id"]), str(existing["summary"]), now, False

    metadata_payload = dict(classify_memory(content, target))
    if metadata:
        try:
            metadata_payload.update(json.loads(metadata))
        except Exception:
            metadata_payload["raw_metadata"] = str(metadata)
    metadata_json = json.dumps(metadata_payload, ensure_ascii=False, sort_keys=True)

    conn.execute(
        """
        INSERT INTO memories (
            id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
            agent_identity, agent_workspace,
            session_id, source, target, content, summary, created_at, updated_at, last_recalled_turn,
            dedup_key, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
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
            key,
            metadata_json,
        ),
    )
    conn.execute(
        "INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)",
        (memory_id, content, summary),
    )
    conn.commit()
    return memory_id, summary, now, True


def update_row(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    content: str,
    target: str | None = None,
    scope_id: str | None = None,
    scope_ids: list[str] | tuple[str, ...] | None = None,
) -> tuple[bool, str, str]:
    if scope_ids is not None:
        clean_scope_ids = [str(item) for item in scope_ids if str(item)]
        if not clean_scope_ids:
            return False, "", ""
        where = f"id = ? AND scope_id IN ({','.join('?' for _ in clean_scope_ids)})"
        params: tuple[Any, ...] = (memory_id, *clean_scope_ids)
    elif scope_id is not None:
        where = "id = ? AND scope_id = ?"
        params = (memory_id, scope_id)
    else:
        where = "id = ?"
        params = (memory_id,)
    row = conn.execute(f"SELECT * FROM memories WHERE {where}", params).fetchone()
    if row is None:
        return False, "", ""
    new_target = target or str(row["target"])
    summary = compact_text(content, 220)
    updated_at = now_iso()
    metadata_payload: dict[str, Any] = {}
    try:
        metadata_payload.update(json.loads(str(row["metadata"] or "{}")))
    except Exception:
        pass
    metadata_payload.update(classify_memory(content, new_target))
    metadata_json = json.dumps(metadata_payload, ensure_ascii=False, sort_keys=True)
    conn.execute(
        """
        UPDATE memories
        SET content = ?, summary = ?, target = ?, updated_at = ?, dedup_key = ?, metadata = ?
        WHERE id = ? AND scope_id = ?
        """,
        (content, summary, new_target, updated_at, dedup_key(content), metadata_json, memory_id, str(row["scope_id"])),
    )
    conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
    conn.execute("INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)", (memory_id, content, summary))
    conn.commit()
    return True, summary, updated_at


def delete_rows(
    conn: sqlite3.Connection,
    ids: list[str],
    *,
    scope_id: str | None = None,
    scope_ids: list[str] | tuple[str, ...] | None = None,
) -> int:
    ids = [str(memory_id) for memory_id in ids if str(memory_id).strip()]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    if scope_ids is not None:
        clean_scope_ids = [str(item) for item in scope_ids if str(item)]
        if not clean_scope_ids:
            return 0
        scoped_ids = [
            str(row["id"])
            for row in conn.execute(
                f"SELECT id FROM memories WHERE id IN ({placeholders}) AND scope_id IN ({','.join('?' for _ in clean_scope_ids)})",
                [*ids, *clean_scope_ids],
            ).fetchall()
        ]
    elif scope_id is None:
        scoped_ids = ids
    else:
        scoped_ids = [
            str(row["id"])
            for row in conn.execute(f"SELECT id FROM memories WHERE id IN ({placeholders}) AND scope_id = ?", [*ids, scope_id]).fetchall()
        ]
    if not scoped_ids:
        return 0
    placeholders = ",".join("?" for _ in scoped_ids)
    before = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
    conn.execute(f"DELETE FROM memories_fts WHERE memory_id IN ({placeholders})", scoped_ids)
    conn.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", scoped_ids)
    conn.commit()
    after = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
    return max(0, before - after)


def exact_duplicate_groups(
    conn: sqlite3.Connection,
    *,
    scope_id: str | None = None,
    scope_ids: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    if scope_ids is not None:
        clean_scope_ids = [str(item) for item in scope_ids if str(item)]
        if not clean_scope_ids:
            return []
        where = f"WHERE scope_id IN ({','.join('?' for _ in clean_scope_ids)})"
        params: tuple[Any, ...] = tuple(clean_scope_ids)
    elif scope_id:
        where = "WHERE scope_id = ?"
        params = (scope_id,)
    else:
        where = ""
        params = ()
    rows = conn.execute(
        f"""
        SELECT scope_id, target, dedup_key, COUNT(*) AS count
        FROM memories
        {where}
        GROUP BY scope_id, target, dedup_key
        HAVING COUNT(*) > 1
        ORDER BY count DESC
        """,
        params,
    ).fetchall()
    groups: list[dict[str, Any]] = []
    for row in rows:
        members = conn.execute(
            """
            SELECT id, content, created_at, updated_at
            FROM memories
            WHERE scope_id = ? AND target = ? AND dedup_key = ?
            ORDER BY updated_at DESC, created_at DESC, id DESC
            """,
            (row["scope_id"], row["target"], row["dedup_key"]),
        ).fetchall()
        groups.append(
            {
                "scope_id": row["scope_id"],
                "target": row["target"],
                "dedup_key": row["dedup_key"],
                "count": int(row["count"]),
                "keep_id": str(members[0]["id"]),
                "delete_ids": [str(member["id"]) for member in members[1:]],
                "preview": str(members[0]["content"])[:180],
            }
        )
    return groups


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
