from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .capture_filters import sanitize_report_text, should_capture_text
from .config import load_runtime_config
from .gating import clean_text, compact_text, dedup_key
from .governance import is_conflicting, merge_memory_text, normalize_memory_type, semantic_similarity
from .graph import normalize_entity
from .models import RuntimeScope
from .nightly_digest import (
    DigestOptions,
    MessageRecord,
    ScopeProfile,
    SessionBundle,
    build_prompt,
    call_llm,
    existing_memory_context,
    parse_llm_candidates,
    resolve_llm_config,
    session_chunks,
)
from .scope import accessible_scope_ids, build_scope_id, build_shared_scope_id, canonical_user_id, normalize_scope_identity, writable_scope_ids
from .sql_store import ensure_schema, now_iso, store_row, update_row
from .vector_runtime import upsert_vector_record

JOURNAL_TARGETS = {"user", "memory", "project", "ops"}
DATA_URL_PREFIX_RE = re.compile(r"data:[a-z0-9.+-]+/[a-z0-9.+-]+;base64,", re.IGNORECASE)
BASE64ISH_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")


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


@dataclass
class JournalDigestCandidate:
    content: str
    target: str = "memory"
    memory_type: str = "summary"
    importance: float = 0.65
    confidence: float = 0.70
    entities: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    reason: str = ""
    entry_ids: list[int] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)


def ensure_journal_schema(conn: sqlite3.Connection) -> None:
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


def _unique(values: list[str], *, limit: int = 16) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        clean = str(value or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        output.append(clean)
        if len(output) >= limit:
            break
    return output


def _entry_entities(entries: list[JournalEntry]) -> list[str]:
    from .graph import extract_entities

    values: list[str] = []
    for entry in entries:
        values.extend(extract_entities(entry.content))
    return _unique([entity for entity in (normalize_entity(value) for value in values) if entity], limit=12)


_GENERIC_TOPIC_ENTITIES = {
    "scope-recall",
    "scope",
    "recall",
    "memory",
    "memories",
    "journal",
    "digest",
    "plugin",
    "assistant",
    "user",
    "记忆",
    "插件",
    "已验证",
    "验证",
    "已确定",
    "确定",
    "任务",
    "主题",
    "同一个",
}


def _topic_entities(entries: list[JournalEntry]) -> list[str]:
    entities = _entry_entities(entries)
    specific = [entity for entity in entities if entity not in _GENERIC_TOPIC_ENTITIES and not entity.startswith("session")]
    return _unique(specific or entities, limit=8)


def _topic_tags(entries: list[JournalEntry]) -> list[str]:
    tags = [f"topic:{entity}" for entity in _topic_entities(entries)[:6]]
    session_tags = [f"session:{session_id}" for session_id in _unique([entry.session_id for entry in entries], limit=4)]
    return _unique([*tags, *session_tags], limit=12)


def _topic_label(entries: list[JournalEntry], fallback: str) -> str:
    topics = _topic_entities(entries)
    if topics:
        return ", ".join(topics[:4])
    return fallback


_DOMAIN_TOPIC_HINTS = {
    "release",
    "gate",
    "ci",
    "wheel",
    "manifest",
    "version",
    "check.release",
    "pytest",
    "rrf",
    "bm25",
    "retrieval",
    "vector",
    "lancedb",
    "tailscale",
    "remote",
    "network",
    "firewall",
    "credential",
    "secret",
    "journal",
    "digest",
    "merge",
    "upsert",
    "scope-recall",
    "发布",
    "版本",
    "召回",
    "向量",
    "远程",
    "客户",
    "授权",
    "网络",
    "防火墙",
    "记忆",
    "日记",
    "合并",
}


def _topic_signature(entries: list[JournalEntry]) -> set[str]:
    text = "\n".join(entry.content for entry in entries).lower()
    signature = {hint for hint in _DOMAIN_TOPIC_HINTS if hint.lower() in text}
    signature.update(_topic_entities(entries)[:8])
    return {item for item in signature if item}


def _segment_session_entries(entries: list[JournalEntry]) -> list[list[JournalEntry]]:
    segments: list[list[JournalEntry]] = []
    current: list[JournalEntry] = []
    current_signature: set[str] = set()
    for entry in entries:
        probe = [entry]
        probe_signature = _topic_signature(probe)
        if entry.role == "user" and current:
            overlap = current_signature & probe_signature
            if current_signature and probe_signature and not overlap:
                segments.append(current)
                current = []
                current_signature = set()
        current.append(entry)
        current_signature |= probe_signature
    if current:
        segments.append(current)
    return segments


def _classify_target_and_type(text: str) -> tuple[str, str, list[str]]:
    lowered = text.lower()
    if any(token in lowered for token in ["prefers", "preference", "joy prefers", "用户偏好", "希望", "偏好"]):
        return "user", "preference", ["preference"]
    if any(token in lowered for token in ["deploy", "restart", "systemctl", "端口", "服务", "重启", "部署", "排障"]):
        return "ops", "workflow", ["ops", "workflow"]
    if any(token in lowered for token in ["scope-recall", "plugin", "插件", "memory", "记忆", "journal", "digest", "merge", "upsert"]):
        return "memory", "decision", ["memory-governance", "journal-digest"]
    return "memory", "summary", ["journal-digest"]


def _looks_like_historical_template_noise(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if lowered.startswith("operations workflow summary from journal digest:") or lowered.startswith("operations workflow summary"):
        return True
    if lowered.startswith("journal digest memory"):
        return True
    return False


def _digest_role_summary(entries: list[JournalEntry], role: str, *, limit: int) -> str:
    chunks = [
        entry.content.strip()
        for entry in entries
        if entry.role == role and entry.content.strip() and not _looks_like_historical_template_noise(entry.content)
    ]
    if not chunks:
        return ""
    return compact_text("；".join(chunks), limit)


def _heuristic_candidate_content(target: str, topic_label: str, entries: list[JournalEntry]) -> str:
    user_summary = _digest_role_summary(entries, "user", limit=300)
    assistant_summary = _digest_role_summary(entries, "assistant", limit=520)
    parts: list[str] = []
    if target == "ops":
        parts.append("可复用运维流程")
    elif target == "memory":
        parts.append("可复用记忆治理决策")
    else:
        parts.append("可复用对话事实摘要")
    if topic_label:
        parts.append(f"主题：{topic_label}")
    if user_summary:
        parts.append(f"用户意图/约束：{user_summary}")
    if assistant_summary:
        parts.append(f"处理/结论：{assistant_summary}")
    return "。".join(parts) + "。"


def heuristic_journal_candidates(entries: list[JournalEntry]) -> list[JournalDigestCandidate]:
    if not entries:
        return []
    # Production-safe fallback: keep related consecutive turns together, but do
    # not let a long Telegram/Hermes session become one global memory bucket.
    groups: dict[str, list[JournalEntry]] = {}
    for entry in entries:
        key = f"session:{entry.session_id or 'unknown'}"
        groups.setdefault(key, []).append(entry)

    candidates: list[JournalDigestCandidate] = []
    for key, session_entries in groups.items():
        for segment_index, group_entries in enumerate(_segment_session_entries(session_entries), start=1):
            digest_entries = [
                entry
                for entry in group_entries
                if entry.role != "tool" and not _looks_like_historical_template_noise(entry.content)
            ]
            if not digest_entries or not any(entry.role == "user" for entry in digest_entries):
                continue
            combined = "\n".join(f"{entry.role}: {entry.content}" for entry in digest_entries)
            target, memory_type, tags = _classify_target_and_type(combined)
            session_ids = _unique([entry.session_id for entry in digest_entries], limit=12)
            entry_ids = [entry.id for entry in digest_entries]
            entities = _entry_entities(digest_entries)
            segment_key = f"{key}:segment:{segment_index}"
            topic_label = _topic_label(digest_entries, segment_key.replace("session:", "session "))
            content = _heuristic_candidate_content(target, topic_label, digest_entries)
            candidates.append(
                JournalDigestCandidate(
                    content=content,
                    target=target,
                    memory_type=memory_type,
                    importance=0.78 if target in {"memory", "ops"} else 0.62,
                    confidence=0.78,
                    entities=entities,
                    tags=_unique([*tags, *_topic_tags(digest_entries), key, segment_key], limit=16),
                    reason="journal digest grouped related consecutive conversation turns",
                    entry_ids=entry_ids,
                    session_ids=session_ids,
                )
            )
    return candidates


def candidate_metadata(candidate: JournalDigestCandidate, run_id: str) -> dict[str, Any]:
    return {
        "memory_type": normalize_memory_type(candidate.memory_type, "summary"),
        "importance": max(0.0, min(1.0, float(candidate.importance))),
        "confidence": max(0.0, min(1.0, float(candidate.confidence))),
        "entities": candidate.entities,
        "tags": _unique([*candidate.tags, "journal-digest"], limit=20),
        "journal_run_id": run_id,
        "journal_entry_ids": candidate.entry_ids[:200],
        "journal_session_ids": candidate.session_ids[:40],
        "journal_reason": candidate.reason,
    }


_WORKFLOW_CONTINUATION_TOKENS = {
    "journal-first",
    "journal-digest",
    "journal",
    "digest",
    "merge/upsert",
    "merge",
    "upsert",
    "日记",
    "合并",
}


def _workflow_continuation_tokens(content: str, tags: set[str], entities: set[str]) -> set[str]:
    del content  # generated heuristic prefixes contain "Journal digest" for every candidate
    values: list[str] = []
    for tag in tags:
        clean = tag.lower()
        if clean.startswith("topic:"):
            values.append(clean.removeprefix("topic:"))
    values.extend(entity.lower() for entity in entities)
    haystack = "\n".join(values)
    return {token for token in _WORKFLOW_CONTINUATION_TOKENS if token in haystack}


def _is_workflow_continuation(candidate_tokens: set[str], existing_tokens: set[str]) -> bool:
    if candidate_tokens & existing_tokens:
        return True
    update_tokens = {"merge/upsert", "merge", "upsert", "合并"}
    journal_anchor_tokens = {"journal-first", "journal", "digest", "journal-digest", "日记"}
    return bool(candidate_tokens & update_tokens and existing_tokens & journal_anchor_tokens)


def _metadata_entities(metadata: dict[str, Any]) -> set[str]:
    raw = metadata.get("entities", []) if isinstance(metadata, dict) else []
    return {str(entity).strip() for entity in raw if str(entity).strip()}


def _find_match(conn: sqlite3.Connection, scope_ids: list[str], candidate: JournalDigestCandidate) -> tuple[str, str, float]:
    placeholders = ",".join("?" for _ in scope_ids)
    rows = conn.execute(
        f"""
        SELECT id, content, metadata
        FROM memories
        WHERE scope_id IN ({placeholders}) AND target = ?
        ORDER BY updated_at DESC
        LIMIT 300
        """,
        [*scope_ids, candidate.target],
    ).fetchall()
    best_id = ""
    best_content = ""
    best_score = 0.0
    candidate_key = dedup_key(candidate.content)
    candidate_entities = set(candidate.entities)
    candidate_tags = set(candidate.tags)
    candidate_topic_tags = {tag for tag in candidate_tags if tag.startswith("topic:")}
    candidate_session_tags = {tag for tag in candidate_tags if tag.startswith("session:")}
    for row in rows:
        content = str(row["content"])
        if dedup_key(content) == candidate_key:
            return str(row["id"]), content, 1.0
        score = semantic_similarity(content, candidate.content)
        try:
            metadata = json.loads(str(row["metadata"] or "{}"))
        except Exception:
            metadata = {}
        existing_tags = set(str(tag) for tag in metadata.get("tags", []) if str(tag).strip()) if isinstance(metadata, dict) else set()
        existing_entities = _metadata_entities(metadata)
        existing_topic_tags = {tag for tag in existing_tags if tag.startswith("topic:")}
        existing_session_tags = {tag for tag in existing_tags if tag.startswith("session:")}
        same_session = bool(candidate_session_tags & existing_session_tags)
        same_topic = bool(candidate_topic_tags & existing_topic_tags)
        candidate_workflow_tokens = _workflow_continuation_tokens(candidate.content, candidate_tags, candidate_entities)
        existing_workflow_tokens = _workflow_continuation_tokens(content, existing_tags, existing_entities)
        workflow_continuation = _is_workflow_continuation(candidate_workflow_tokens, existing_workflow_tokens)
        lower = content.lower()
        entity_hits = sum(1 for entity in candidate_entities if entity and entity in lower)
        tag_hits = sum(1 for tag in candidate_tags if tag and tag in lower)
        score = max(score, min(0.86, score + entity_hits * 0.08 + tag_hits * 0.04))
        if same_session and (same_topic or workflow_continuation):
            score = max(score, 0.58)
        elif same_topic:
            score = max(score, 0.56)
        elif candidate_topic_tags and existing_topic_tags:
            score = min(score, 0.52)
        if score > best_score:
            best_id = str(row["id"])
            best_content = content
            best_score = score
    return best_id, best_content, best_score


def _memory_scope_id(conn: sqlite3.Connection, memory_id: str) -> str:
    row = conn.execute("SELECT scope_id FROM memories WHERE id = ?", (memory_id,)).fetchone()
    return str(row["scope_id"] if row is not None else "")


def _record_journal_sources(conn: sqlite3.Connection, *, memory_id: str, run_id: str, entry_ids: list[int]) -> None:
    now = now_iso()
    conn.executemany(
        """
        INSERT OR REPLACE INTO memory_journal_sources(memory_id, journal_entry_id, run_id, created_at)
        VALUES (?, ?, ?, ?)
        """,
        [(memory_id, int(entry_id), run_id, now) for entry_id in entry_ids],
    )


def _record_journal_rejection(conn: sqlite3.Connection, *, run_id: str, entry_ids: list[int], reason: str, candidate: JournalDigestCandidate) -> None:
    now = now_iso()
    snippet = compact_text(sanitize_report_text(candidate.content), 500)
    conn.executemany(
        """
        INSERT OR REPLACE INTO journal_rejections(journal_entry_id, run_id, reason, candidate, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [(int(entry_id), run_id, reason, snippet, now) for entry_id in entry_ids],
    )


def _quarantine_journal_entries(conn: sqlite3.Connection, *, run_id: str, entries: list[JournalEntry], reason: str, error: Exception) -> None:
    entry_ids = [int(entry.id) for entry in entries]
    _record_journal_rejection(
        conn,
        run_id=run_id,
        entry_ids=entry_ids,
        reason=reason,
        candidate=JournalDigestCandidate(
            content=sanitize_report_text(f"{reason}: {type(error).__name__}: {str(error)[:400]}"),
            target="memory",
            entry_ids=entry_ids,
        ),
    )


def _merge_metadata(conn: sqlite3.Connection, *, memory_id: str, candidate: JournalDigestCandidate, run_id: str) -> None:
    from .graph import load_metadata, sync_memory_entities

    row = conn.execute("SELECT content, target, metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        return
    existing = load_metadata(row["metadata"])
    incoming = candidate_metadata(candidate, run_id)
    for key in ("entities", "tags", "journal_entry_ids", "journal_session_ids"):
        existing_values = existing.get(key) if isinstance(existing.get(key), list) else []
        incoming_values = incoming.get(key) if isinstance(incoming.get(key), list) else []
        merged = _unique([*map(str, existing_values), *map(str, incoming_values)], limit=240 if key == "journal_entry_ids" else 40)
        if merged:
            existing[key] = merged
    for key in ("journal_run_id", "journal_reason", "memory_type"):
        if incoming.get(key):
            existing[key] = incoming[key]
    existing["importance"] = max(float(existing.get("importance") or 0.0), float(incoming.get("importance") or 0.0))
    existing["confidence"] = max(float(existing.get("confidence") or 0.0), float(incoming.get("confidence") or 0.0))
    conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (json.dumps(existing, ensure_ascii=False, sort_keys=True), memory_id))
    sync_memory_entities(conn, memory_id=memory_id, content=str(row["content"]), target=str(row["target"]), metadata=existing)


def _candidate_allowed(candidate: JournalDigestCandidate) -> bool:
    if candidate.target not in JOURNAL_TARGETS:
        return False
    if len(candidate.content) < 40:
        return False
    if _looks_like_historical_template_noise(candidate.content):
        return False
    lowered = candidate.content.lower()
    if "operations workflow summary from journal digest:" in lowered or "journal digest memory" in lowered:
        return False
    return should_capture_text(candidate.content).allowed


def _cross_platform_metadata(scope: RuntimeScope, config: dict[str, Any] | None = None) -> dict[str, Any]:
    canonical = canonical_user_id(scope, config)
    metadata = {"raw_platform": scope.platform or "cli", "raw_user_id": scope.user_id or "local"}
    if canonical:
        metadata["canonical_user"] = canonical
        metadata["scope_identity_mode"] = "canonical"
    return metadata


def apply_journal_candidates(
    conn: sqlite3.Connection,
    vector_runtime: Any,
    scope: RuntimeScope,
    *,
    run_id: str,
    candidates: list[JournalDigestCandidate],
    dry_run: bool = False,
    runtime_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scope = normalize_scope_identity(scope, runtime_config)
    scope_ids = accessible_scope_ids(scope, runtime_config)
    write_scope_ids = writable_scope_ids(scope, runtime_config)
    shared_scope_id = build_shared_scope_id(scope, runtime_config)
    counts = Counter()
    actions: list[dict[str, Any]] = []
    processed_entry_ids: set[int] = set()
    for candidate in candidates:
        if not _candidate_allowed(candidate):
            counts["skipped"] += 1
            actions.append({"action": "skip", "reason": "candidate filtered", "entry_ids": candidate.entry_ids})
            processed_entry_ids.update(int(entry_id) for entry_id in candidate.entry_ids)
            if not dry_run:
                _record_journal_rejection(conn, run_id=run_id, entry_ids=candidate.entry_ids, reason="candidate filtered", candidate=candidate)
                conn.commit()
            continue
        match_id, match_content, score = _find_match(conn, scope_ids, candidate)
        match_scope_id = _memory_scope_id(conn, match_id) if match_id else ""
        match_is_writable = bool(match_scope_id and match_scope_id in set(write_scope_ids))
        if match_id and score >= 0.88:
            counts["skipped"] += 1
            actions.append({"action": "skip", "reason": "existing memory covers candidate", "id": match_id, "score": round(score, 4), "entry_ids": candidate.entry_ids})
            processed_entry_ids.update(int(entry_id) for entry_id in candidate.entry_ids)
            if not dry_run:
                _record_journal_rejection(conn, run_id=run_id, entry_ids=candidate.entry_ids, reason="existing memory covers candidate", candidate=candidate)
                conn.commit()
            continue
        if match_id and match_is_writable and score >= 0.55 and not is_conflicting(match_content, candidate.content):
            merged = merge_memory_text(match_content, candidate.content)
            if candidate.content not in merged and "merge/upsert" in candidate.content.lower():
                merged = f"{merged}\n§\n{candidate.content}"
            counts["updated"] += 1
            actions.append({"action": "update", "id": match_id, "score": round(score, 4), "entry_ids": candidate.entry_ids})
            if not dry_run:
                updated, summary, updated_at = update_row(
                    conn,
                    memory_id=match_id,
                    content=merged,
                    target=candidate.target,
                    scope_ids=write_scope_ids,
                )
                if updated:
                    _merge_metadata(conn, memory_id=match_id, candidate=candidate, run_id=run_id)
                    _record_journal_sources(conn, memory_id=match_id, run_id=run_id, entry_ids=candidate.entry_ids)
                    conn.commit()
                    processed_entry_ids.update(int(entry_id) for entry_id in candidate.entry_ids)
                    if vector_runtime is not None:
                        row = conn.execute("SELECT scope_id FROM memories WHERE id = ?", (match_id,)).fetchone()
                        row_scope_id = str(row["scope_id"] if row else shared_scope_id)
                        upsert_vector_record(
                            vector_runtime,
                            id=match_id,
                            source="journal-digest",
                            target=candidate.target,
                            content=merged,
                            summary=summary,
                            updated_at=updated_at,
                            scope_id=row_scope_id,
                        )
            continue
        memory_id = uuid.uuid4().hex
        counts["inserted"] += 1
        actions.append({"action": "insert", "id": memory_id, "target": candidate.target, "entry_ids": candidate.entry_ids})
        if not dry_run:
            stored_id, summary, updated_at, inserted = store_row(
                conn,
                memory_id=memory_id,
                scope_id=shared_scope_id,
                platform=scope.platform,
                user_id=scope.user_id,
                chat_id=scope.chat_id,
                thread_id=scope.thread_id,
                gateway_session_key=scope.gateway_session_key,
                agent_identity=scope.agent_identity,
                agent_workspace=scope.agent_workspace,
                session_id=",".join(candidate.session_ids[:3]),
                source="journal-digest",
                target=candidate.target,
                content=candidate.content,
                metadata=json.dumps({**_cross_platform_metadata(scope, runtime_config), **candidate_metadata(candidate, run_id)}, ensure_ascii=False, sort_keys=True),
            )
            if inserted:
                _record_journal_sources(conn, memory_id=stored_id, run_id=run_id, entry_ids=candidate.entry_ids)
                conn.commit()
                processed_entry_ids.update(int(entry_id) for entry_id in candidate.entry_ids)
                if vector_runtime is not None:
                    upsert_vector_record(
                        vector_runtime,
                        id=stored_id,
                        source="journal-digest",
                        target=candidate.target,
                        content=candidate.content,
                        summary=summary,
                        updated_at=updated_at,
                        scope_id=shared_scope_id,
                    )
            else:
                counts["inserted"] -= 1
                counts["updated"] += 1
                actions.append({"action": "update", "reason": "duplicate store_row", "id": stored_id, "entry_ids": candidate.entry_ids})
                _merge_metadata(conn, memory_id=stored_id, candidate=candidate, run_id=run_id)
                _record_journal_sources(conn, memory_id=stored_id, run_id=run_id, entry_ids=candidate.entry_ids)
                conn.commit()
                processed_entry_ids.update(int(entry_id) for entry_id in candidate.entry_ids)
    return {"counts": dict(counts), "actions": actions, "processed_entry_ids": sorted(processed_entry_ids)}


def mark_entries_processed(conn: sqlite3.Connection, *, entry_ids: list[int], run_id: str) -> None:
    if not entry_ids:
        return
    placeholders = ",".join("?" for _ in entry_ids)
    conn.execute(
        f"UPDATE journal_entries SET processed_run_id = ?, processed_at = ? WHERE id IN ({placeholders})",
        [run_id, now_iso(), *[int(entry_id) for entry_id in entry_ids]],
    )
    conn.commit()


def _parse_entry_timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _journal_session_bundles(entries: list[JournalEntry]) -> list[SessionBundle]:
    grouped: dict[str, list[JournalEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.session_id or "unknown", []).append(entry)
    bundles: list[SessionBundle] = []
    for session_id, session_entries in grouped.items():
        session_entries.sort(key=lambda item: (item.turn_number, item.id))
        digest_entries = [entry for entry in (_journal_entry_for_digest(item) for item in session_entries) if entry is not None]
        if not digest_entries:
            continue
        original_roles = {entry.role for entry in digest_entries}
        messages: list[MessageRecord] = []
        tool_names: list[str] = []
        for entry in digest_entries:
            if entry.role == "tool":
                tool_name = str(entry.metadata.get("tool_name") or "").strip()
                if tool_name:
                    tool_names.append(tool_name)
                continue
            role = entry.role if entry.role in {"user", "assistant"} else "assistant"
            content = entry.content
            messages.append(
                MessageRecord(
                    id=entry.id,
                    session_id=entry.session_id,
                    role=role,
                    content=content,
                    timestamp=_parse_entry_timestamp(entry.created_at),
                    tool_name=str(entry.metadata.get("tool_name") or ""),
                )
            )
        if not messages or not any(message.role == "user" for message in messages):
            if original_roles == {"tool"}:
                bundles.append(
                    SessionBundle(
                        id=session_id,
                        source="journal-tool-only",
                        title=session_id,
                        messages=[],
                        tool_names=_unique(tool_names, limit=24),
                        is_task=bool(tool_names),
                        completed=False,
                    )
                )
            continue
        title = compact_text(next((message.content for message in messages if message.role == "user"), session_id), 100)
        text = "\n".join(message.content for message in messages).lower()
        is_task = bool(tool_names) or any(token in text for token in ["fix", "debug", "deploy", "release", "verify", "修", "排障", "部署", "验证", "实现"])
        original_roles = {entry.role for entry in digest_entries}
        bundles.append(
            SessionBundle(
                id=session_id,
                source="journal-tool-only" if original_roles == {"tool"} else "journal",
                title=title,
                messages=messages,
                tool_names=_unique(tool_names, limit=24),
                is_task=is_task,
                completed=any(token in text for token in ["passed", "通过", "完成", "验证"]),
            )
        )
    return bundles


def _journal_from_digest_candidate(candidate: Any) -> JournalDigestCandidate:
    return JournalDigestCandidate(
        content=str(candidate.content),
        target=str(candidate.target or "memory"),
        memory_type=str(candidate.memory_type or "summary"),
        importance=float(candidate.importance or 0.55),
        confidence=float(candidate.confidence or 0.65),
        entities=list(candidate.entities or []),
        tags=_unique([*list(candidate.tags or []), "journal-digest", "llm-digest"], limit=20),
        reason=str(candidate.reason or "llm journal digest extraction"),
        entry_ids=[int(item) for item in list(candidate.message_ids or [])],
        session_ids=[str(candidate.session_id)] if getattr(candidate, "session_id", "") else [],
    )


def llm_journal_candidates(
    conn: sqlite3.Connection,
    *,
    entries: list[JournalEntry],
    hermes_home: Path,
    scope: RuntimeScope,
    journal_config: dict[str, Any],
) -> list[JournalDigestCandidate]:
    runtime_config = _runtime_config(hermes_home)
    options = DigestOptions(
        hermes_home=hermes_home,
        digest_date=datetime.now(timezone.utc).date(),
        extractor="llm",
        chunk_chars=_coerce_positive_int(journal_config.get("llm_chunk_chars"), 7000),
        max_session_chars=_coerce_positive_int(journal_config.get("llm_max_session_chars"), 16000),
        model=str(journal_config.get("model") or ""),
        base_url=str(journal_config.get("base_url") or ""),
        endpoint=str(journal_config.get("endpoint") or journal_config.get("chat_endpoint") or ""),
        append_v1=_config_bool(journal_config, "append_v1", True) if "append_v1" in journal_config else None,
        api_key=str(journal_config.get("api_key") or ""),
        timeout=float(journal_config.get("timeout") or journal_config.get("llm_timeout") or 60.0),
    )
    llm_config = resolve_llm_config(hermes_home, options)
    active_scope = normalize_scope_identity(scope, runtime_config)
    profile = ScopeProfile(
        scope=active_scope,
        scope_id=build_scope_id(active_scope, runtime_config),
        shared_scope_id=build_shared_scope_id(active_scope, runtime_config),
        accessible_scope_ids=accessible_scope_ids(active_scope, runtime_config),
    )
    existing = existing_memory_context(conn, profile)
    output: list[JournalDigestCandidate] = []
    max_attempts = _coerce_positive_int(journal_config.get("llm_max_attempts") or journal_config.get("llm_retry_attempts"), 3)
    retry_delay = _coerce_nonnegative_float(journal_config.get("llm_retry_delay"), 1.0)
    for bundle in _journal_session_bundles(entries):
        if bundle.source == "journal-tool-only":
            continue
        bundle_candidates: list[Any] = []
        for chunk in session_chunks(bundle, chunk_chars=options.chunk_chars, max_session_chars=options.max_session_chars):
            prompt = build_prompt(bundle, chunk, existing)
            raw = _call_llm_with_retries(
                prompt,
                model=llm_config["model"],
                base_url=llm_config["base_url"],
                api_key=llm_config["api_key"],
                timeout=options.timeout,
                api_mode=llm_config.get("api_mode", "chat_completions"),
                endpoint=str(llm_config.get("endpoint") or ""),
                append_v1=bool(llm_config.get("append_v1", True)),
                max_attempts=max_attempts,
                retry_delay=retry_delay,
            )
            bundle_candidates.extend(parse_llm_candidates(raw, bundle=bundle))
        output.extend(_journal_from_digest_candidate(candidate) for candidate in bundle_candidates)
    return [candidate for candidate in output if candidate.entry_ids]


def _config_bool(config: dict[str, Any], key: str, default: bool = False) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _collect_journal_candidates(
    conn: sqlite3.Connection,
    *,
    entries: list[JournalEntry],
    hermes_home: Path,
    scope: RuntimeScope,
    journal_config: dict[str, Any],
    requested_extractor: str,
) -> tuple[list[JournalDigestCandidate], str, str]:
    if requested_extractor == "llm":
        fallback_allowed = _config_bool(journal_config, "allow_heuristic_fallback", False)
        try:
            candidates = llm_journal_candidates(conn, entries=entries, hermes_home=hermes_home, scope=scope, journal_config=journal_config)
            if candidates:
                return candidates, "llm", ""
            if fallback_allowed:
                return heuristic_journal_candidates(entries), "heuristic-fallback", "llm produced no candidates"
            return [], "llm", "llm produced no candidates"
        except Exception:
            if fallback_allowed:
                try:
                    return heuristic_journal_candidates(entries), "heuristic-fallback", "llm failed; heuristic fallback enabled"
                except Exception:
                    pass
            raise
    return heuristic_journal_candidates(entries), "heuristic", ""


def _scope_from_row(row: sqlite3.Row | None) -> RuntimeScope:
    return RuntimeScope(
        platform=str(row["platform"] if row else "telegram") or "telegram",
        user_id=str(row["user_id"] if row else "") or "local",
        chat_id=str(row["chat_id"] if row else ""),
        thread_id=str(row["thread_id"] if row else ""),
        gateway_session_key=str(row["gateway_session_key"] if row else ""),
        agent_identity=str(row["agent_identity"] if row else "default") or "default",
        agent_workspace=str(row["agent_workspace"] if row else "hermes") or "hermes",
        agent_context="primary",
    )


def _infer_scope_from_journal(conn: sqlite3.Connection) -> RuntimeScope:
    row = conn.execute(
        """
        SELECT platform, user_id, chat_id, thread_id, gateway_session_key, agent_identity, agent_workspace
        FROM journal_entries
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT platform, user_id, chat_id, thread_id, gateway_session_key, agent_identity, agent_workspace
            FROM memories
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ).fetchone()
    return _scope_from_row(row)


def _unprocessed_scopes(conn: sqlite3.Connection, *, limit: int = 1000) -> list[RuntimeScope]:
    rows = conn.execute(
        """
        SELECT platform, user_id, chat_id, thread_id, gateway_session_key, agent_identity, agent_workspace, MIN(id) AS first_id
        FROM journal_entries
        WHERE processed_run_id IS NULL OR processed_run_id = ''
        GROUP BY scope_id
        ORDER BY first_id ASC
        LIMIT ?
        """,
        (max(1, int(limit or 1000)),),
    ).fetchall()
    return [_scope_from_row(row) for row in rows]


def _open_digest_connection(db_path: Path, *, dry_run: bool) -> sqlite3.Connection:
    if dry_run:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        if db_path.exists():
            source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                source.backup(conn)
            finally:
                source.close()
        return conn
    return sqlite3.connect(db_path, timeout=30)


def _runtime_config(hermes_home: Path) -> dict[str, Any]:
    plugin_dir = Path(__file__).resolve().parent
    storage_dir = hermes_home / "scope-recall"
    return load_runtime_config(plugin_dir, storage_dir)


def _journal_runtime_config(hermes_home: Path) -> dict[str, Any]:
    config = _runtime_config(hermes_home)
    raw_journal = config.get("journal")
    return raw_journal if isinstance(raw_journal, dict) else {}


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, parsed)


def _journal_unprocessed_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM journal_entries WHERE processed_run_id IS NULL OR processed_run_id = ''").fetchone()[0]
    )


def _dynamic_journal_digest_limit(conn: sqlite3.Connection, *, configured_limit: int, journal_config: dict[str, Any]) -> int:
    if not _config_bool(journal_config, "dynamic_max_entries_enabled", True):
        return configured_limit
    backlog = _journal_unprocessed_count(conn)
    threshold = _coerce_positive_int(journal_config.get("dynamic_backlog_threshold"), configured_limit * 4)
    if backlog <= threshold:
        return configured_limit
    default_ceiling = max(configured_limit, 500)
    ceiling = _coerce_positive_int(journal_config.get("max_entries_per_digest_ceiling"), default_ceiling)
    return min(backlog, max(configured_limit, ceiling))


def _quarantine_classification(error: Exception) -> tuple[str, dict[str, Any]]:
    if isinstance(error, JournalDigestLLMError):
        classification = "retry_exhausted" if error.retryable else "dead_letter"
        reason_prefix = "retry-exhausted" if error.retryable else "dead-letter"
        sanitized = sanitize_report_text(str(error)[:400])
        return f"{reason_prefix}:{error.error_kind}", {
            "classification": classification,
            "kind": error.error_kind,
            "retryable": bool(error.retryable),
            "attempts": int(error.attempts),
            "message": sanitized,
        }
    kind, retryable = _classify_llm_digest_error(error)
    classification = "retry_exhausted" if retryable else "dead_letter"
    reason_prefix = "retry-exhausted" if retryable else "dead-letter"
    return f"{reason_prefix}:{kind}", {
        "classification": classification,
        "kind": kind,
        "retryable": retryable,
        "attempts": 1,
        "message": sanitize_report_text(f"{type(error).__name__}: {str(error)[:400]}"),
    }


def _coerce_nonnegative_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, parsed)


class JournalDigestLLMError(RuntimeError):
    def __init__(self, message: str, *, attempts: int, error_kind: str, retryable: bool) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.error_kind = error_kind
        self.retryable = retryable


def _classify_llm_digest_error(exc: Exception) -> tuple[str, bool]:
    message = str(exc or "").lower()
    if isinstance(exc, TimeoutError) or "timeout" in message or "timed out" in message:
        return "timeout", True
    if "429" in message or "rate limit" in message or "too many requests" in message:
        return "rate_limit", True
    if any(token in message for token in ("500", "502", "503", "504", "server error", "bad gateway", "service unavailable", "gateway timeout")):
        return "server", True
    if any(token in message for token in ("connection", "network", "temporarily", "reset by peer", "remote end closed")):
        return "network", True
    if any(token in message for token in ("401", "403", "unauthorized", "forbidden", "invalid api key", "permission")):
        return "auth", False
    if any(token in message for token in ("402", "quota", "billing", "insufficient_quota")):
        return "quota", False
    if any(token in message for token in ("json", "parse", "decode")):
        return "parse", False
    return "unknown", True


def _call_llm_with_retries(
    prompt: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    timeout: float,
    api_mode: str,
    max_attempts: int,
    retry_delay: float,
    endpoint: str = "",
    append_v1: bool = True,
) -> str:
    last_error: Exception | None = None
    last_kind = "unknown"
    last_retryable = True
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            return call_llm(
                prompt,
                model=model,
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
                api_mode=api_mode,
                endpoint=endpoint,
                append_v1=append_v1,
            )
        except Exception as exc:
            last_error = exc
            last_kind, last_retryable = _classify_llm_digest_error(exc)
            if (not last_retryable) or attempt >= max_attempts:
                raise JournalDigestLLMError(
                    f"{last_kind} after {attempt} attempt(s): {type(exc).__name__}: {sanitize_report_text(str(exc)[:400])}",
                    attempts=attempt,
                    error_kind=last_kind,
                    retryable=last_retryable,
                ) from exc
            if retry_delay > 0:
                time.sleep(retry_delay)
    assert last_error is not None
    raise JournalDigestLLMError(
        f"{last_kind} after {max_attempts} attempt(s): {type(last_error).__name__}: {sanitize_report_text(str(last_error)[:400])}",
        attempts=max_attempts,
        error_kind=last_kind,
        retryable=last_retryable,
    ) from last_error


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


def run_journal_digest(
    *,
    hermes_home: Path,
    extractor: str = "llm",
    scope: RuntimeScope | None = None,
    interval_label: str = "manual",
    limit_entries: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    hermes_home = hermes_home.expanduser().resolve()
    storage_dir = hermes_home / "scope-recall"
    if not dry_run:
        storage_dir.mkdir(parents=True, exist_ok=True)
    db_path = storage_dir / "memory.sqlite3"
    conn = _open_digest_connection(db_path, dry_run=dry_run)
    conn.row_factory = sqlite3.Row
    run_id = uuid.uuid4().hex
    started_at = now_iso()
    vector_runtime = None
    runtime_config = _runtime_config(hermes_home)
    raw_journal = runtime_config.get("journal")
    journal_config = raw_journal if isinstance(raw_journal, dict) else {}
    configured_limit = _coerce_positive_int(journal_config.get("max_entries_per_digest"), 500)
    effective_limit = _coerce_positive_int(limit_entries, configured_limit) if limit_entries is not None else configured_limit
    retention_days = int(journal_config.get("retention_days") or 0)
    requested_extractor = str(extractor or journal_config.get("extractor") or "llm").strip().lower()
    extractor_used = requested_extractor
    try:
        ensure_schema(conn)
        ensure_journal_schema(conn)
        if limit_entries is None:
            effective_limit = _dynamic_journal_digest_limit(conn, configured_limit=configured_limit, journal_config=journal_config)
        backlog_before = _journal_unprocessed_count(conn)
        active_scopes = [scope] if scope is not None else _unprocessed_scopes(conn, limit=effective_limit)
        if not active_scopes:
            return {
                "ok": True,
                "status": "no_unprocessed_journal",
                "run_id": run_id,
                "processed_entries": 0,
                "inserted": 0,
                "updated": 0,
                "skipped": 0,
                "extractor_requested": requested_extractor,
                "extractor_used": extractor_used,
            }

        total_loaded_entries = 0
        total_candidates = 0
        processed_entry_ids: list[int] = []
        counts = Counter()
        extractor_counts = Counter()
        quarantine_counts = Counter()
        extractor_errors: list[Any] = []
        actions: list[dict[str, Any]] = []
        for active_scope in active_scopes:
            remaining = max(0, effective_limit - total_loaded_entries)
            if remaining <= 0:
                break
            active_scope = normalize_scope_identity(active_scope, runtime_config)
            scope_ids = accessible_scope_ids(active_scope, runtime_config)
            entries = load_unprocessed_journal_entries(conn, scope_ids=scope_ids, limit=remaining)
            if not entries:
                continue
            total_loaded_entries += len(entries)
            try:
                candidates, scope_extractor_used, extractor_error = _collect_journal_candidates(
                    conn,
                    entries=entries,
                    hermes_home=hermes_home,
                    scope=active_scope,
                    journal_config=journal_config,
                    requested_extractor=requested_extractor,
                )
            except Exception as exc:
                if requested_extractor != "llm":
                    raise
                scope_extractor_used = "llm-quarantine"
                quarantine_reason, quarantine_meta = _quarantine_classification(exc)
                extractor_error = quarantine_meta
                candidates = []
                quarantine_entry_ids = [int(entry.id) for entry in entries]
                counts["skipped"] += len(quarantine_entry_ids)
                quarantine_counts[str(quarantine_meta["classification"])] += len(quarantine_entry_ids)
                actions.append(
                    {
                        "action": "skip",
                        "reason": quarantine_reason,
                        "entry_count": len(quarantine_entry_ids),
                        "entry_ids": quarantine_entry_ids[:20],
                        "classification": quarantine_meta,
                    }
                )
                if not dry_run:
                    _quarantine_journal_entries(
                        conn,
                        run_id=run_id,
                        entries=entries,
                        reason=quarantine_reason,
                        error=exc,
                    )
                processed_entry_ids.extend(quarantine_entry_ids)
            extractor_counts[scope_extractor_used] += 1
            if extractor_error:
                extractor_errors.append(extractor_error)
            if scope_extractor_used == "llm-quarantine":
                continue
            total_candidates += len(candidates)
            candidate_entry_ids: set[int] = set()
            for candidate in candidates:
                for entry_id in candidate.entry_ids:
                    try:
                        candidate_entry_ids.add(int(entry_id))
                    except (TypeError, ValueError):
                        continue
            loaded_entry_ids = {int(entry.id) for entry in entries}
            if not dry_run:
                try:
                    from .nightly_digest import DigestVectorRuntime, ScopeProfile

                    vector_runtime = DigestVectorRuntime(
                        hermes_home=hermes_home,
                        conn=conn,
                        scope=ScopeProfile(
                            scope=active_scope,
                            scope_id=build_scope_id(active_scope, runtime_config),
                            shared_scope_id=build_shared_scope_id(active_scope, runtime_config),
                            accessible_scope_ids=accessible_scope_ids(active_scope, runtime_config),
                        ),
                    )
                except Exception:
                    vector_runtime = None
            applied = apply_journal_candidates(conn, vector_runtime, active_scope, run_id=run_id, candidates=candidates, dry_run=dry_run, runtime_config=runtime_config)
            counts.update(applied["counts"])
            applied_entry_ids = {int(entry_id) for entry_id in applied.get("processed_entry_ids", [])}
            reviewed_without_candidate_ids = sorted(loaded_entry_ids - candidate_entry_ids)
            if reviewed_without_candidate_ids:
                counts["skipped"] += len(reviewed_without_candidate_ids)
                actions.append(
                    {
                        "action": "skip",
                        "reason": "no durable memory candidate",
                        "entry_count": len(reviewed_without_candidate_ids),
                        "entry_ids": reviewed_without_candidate_ids[:20],
                    }
                )
                if not dry_run:
                    _record_journal_rejection(
                        conn,
                        run_id=run_id,
                        entry_ids=reviewed_without_candidate_ids,
                        reason="no durable memory candidate",
                        candidate=JournalDigestCandidate(
                            content="No durable memory candidate was produced for this reviewed journal entry.",
                            target="memory",
                            entry_ids=reviewed_without_candidate_ids,
                        ),
                    )
            processed_entry_ids.extend(sorted(applied_entry_ids | set(reviewed_without_candidate_ids)))
            actions.extend(applied["actions"])
            if vector_runtime is not None:
                try:
                    vector_runtime.close()
                except Exception:
                    pass
                vector_runtime = None

        if total_loaded_entries == 0:
            return {
                "ok": True,
                "status": "no_unprocessed_journal",
                "run_id": run_id,
                "processed_entries": 0,
                "inserted": 0,
                "updated": 0,
                "skipped": 0,
                "extractor_requested": requested_extractor,
                "extractor_used": extractor_used,
            }
        unique_processed_entry_ids = sorted(set(processed_entry_ids))
        if extractor_counts:
            extractor_used = next(iter(extractor_counts)) if len(extractor_counts) == 1 else "mixed"
        else:
            extractor_used = requested_extractor
        pruned_entries = 0
        if not dry_run:
            mark_entries_processed(conn, entry_ids=unique_processed_entry_ids, run_id=run_id)
            pruned_entries = _prune_processed_journal(conn, retention_days=retention_days)
            conn.execute(
                """
                INSERT INTO journal_digest_runs(id, started_at, finished_at, status, extractor, interval_label,
                    processed_entries, inserted, updated, skipped, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    started_at,
                    now_iso(),
                    "ok",
                    extractor_used,
                    interval_label,
                    len(unique_processed_entry_ids),
                    counts.get("inserted", 0),
                    counts.get("updated", 0),
                    counts.get("skipped", 0),
                    json.dumps(
                        {
                            "candidate_count": total_candidates,
                            "loaded_entries": total_loaded_entries,
                            "actions": actions[:50],
                            "extractor_requested": requested_extractor,
                            "extractor_used": extractor_used,
                            "extractor_counts": dict(extractor_counts),
                            "extractor_errors": extractor_errors[:5],
                            "quarantine_counts": dict(quarantine_counts),
                            "backlog_before": backlog_before,
                            "limit_entries": effective_limit,
                            "retention_days": retention_days,
                            "pruned_journal_entries": pruned_entries,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            conn.commit()
        return {
            "ok": True,
            "status": "dry_run" if dry_run else "ok",
            "run_id": run_id,
            "processed_entries": total_loaded_entries if dry_run else len(unique_processed_entry_ids),
            "loaded_entries": total_loaded_entries,
            "candidates": total_candidates,
            "inserted": counts.get("inserted", 0),
            "updated": counts.get("updated", 0),
            "skipped": counts.get("skipped", 0),
            "extractor_requested": requested_extractor,
            "extractor_used": extractor_used,
            "quarantine_counts": dict(quarantine_counts),
            "backlog_before": backlog_before,
            "limit_entries": effective_limit,
            "pruned_journal_entries": pruned_entries,
            "actions": actions[:50],
        }
    except Exception as exc:
        if not dry_run:
            ensure_journal_schema(conn)
            conn.execute(
                """
                INSERT OR REPLACE INTO journal_digest_runs(id, started_at, finished_at, status, extractor, interval_label, error)
                VALUES (?, ?, ?, 'error', ?, ?, ?)
                """,
                (run_id, started_at, now_iso(), requested_extractor, interval_label, sanitize_report_text(str(exc)[:1000])),
            )
            conn.commit()
        raise
    finally:
        if vector_runtime is not None:
            try:
                vector_runtime.close()
            except Exception:
                pass
        conn.close()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Digest scope-recall journal entries into high-quality durable memories")
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", "~/.hermes"), help="Hermes home/profile path")
    parser.add_argument("--extractor", choices=["llm", "heuristic"], default="llm", help="Extraction backend; default is LLM-first. Use heuristic only as an explicit operator fallback.")
    parser.add_argument("--interval-label", default="manual", help="Human-readable schedule label, e.g. 2h")
    parser.add_argument("--limit-entries", type=int, default=None, help="Maximum unprocessed journal entries per run; defaults to journal.max_entries_per_digest")
    parser.add_argument("--dry-run", action="store_true", help="Plan without writing memories or advancing watermarks")
    parser.add_argument("--verbose", action="store_true", help="Print full JSON result")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    started = time.time()
    try:
        result = run_journal_digest(
            hermes_home=Path(args.hermes_home),
            extractor=str(args.extractor),
            interval_label=str(args.interval_label),
            limit_entries=max(1, int(args.limit_entries)) if args.limit_entries is not None else None,
            dry_run=bool(args.dry_run),
        )
        result["elapsed_seconds"] = round(time.time() - started, 3)
        if args.verbose or args.dry_run:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            compact = {key: result.get(key) for key in ("ok", "status", "processed_entries", "candidates", "inserted", "updated", "skipped")}
            print(json.dumps(compact, ensure_ascii=False))
        return 0 if result.get("ok") else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": sanitize_report_text(str(exc))}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
