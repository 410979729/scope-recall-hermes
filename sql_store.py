from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifacts import enrich_content_with_artifact_anchors, merge_artifact_metadata
from .capture_filters import sanitize_report_text
from .gating import compact_text, dedup_key
from .governance import classify_memory, merge_metadata
from .graph import backfill_memory_entities, ensure_graph_schema, sync_memory_entities

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
    ensure_graph_schema(conn)
    ensure_experience_schema(conn)
    ensure_governance_schema(conn)
    rebuild_fts_if_empty(conn)
    backfill_memory_entities(conn)
    conn.commit()


def ensure_governance_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS governance_audit_events (
            id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            action TEXT NOT NULL,
            scope_id TEXT NOT NULL DEFAULT '',
            target_id TEXT NOT NULL DEFAULT '',
            batch_id TEXT NOT NULL DEFAULT '',
            before_json TEXT NOT NULL DEFAULT '{}',
            after_json TEXT NOT NULL DEFAULT '{}',
            reason TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL DEFAULT 'scope-recall',
            dry_run INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """
    )
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(governance_audit_events)").fetchall()}
    migrations = {
        "event_type": "ALTER TABLE governance_audit_events ADD COLUMN event_type TEXT NOT NULL DEFAULT ''",
        "action": "ALTER TABLE governance_audit_events ADD COLUMN action TEXT NOT NULL DEFAULT ''",
        "scope_id": "ALTER TABLE governance_audit_events ADD COLUMN scope_id TEXT NOT NULL DEFAULT ''",
        "target_id": "ALTER TABLE governance_audit_events ADD COLUMN target_id TEXT NOT NULL DEFAULT ''",
        "batch_id": "ALTER TABLE governance_audit_events ADD COLUMN batch_id TEXT NOT NULL DEFAULT ''",
        "before_json": "ALTER TABLE governance_audit_events ADD COLUMN before_json TEXT NOT NULL DEFAULT '{}'",
        "after_json": "ALTER TABLE governance_audit_events ADD COLUMN after_json TEXT NOT NULL DEFAULT '{}'",
        "reason": "ALTER TABLE governance_audit_events ADD COLUMN reason TEXT NOT NULL DEFAULT ''",
        "actor": "ALTER TABLE governance_audit_events ADD COLUMN actor TEXT NOT NULL DEFAULT 'scope-recall'",
        "dry_run": "ALTER TABLE governance_audit_events ADD COLUMN dry_run INTEGER NOT NULL DEFAULT 0",
        "created_at": "ALTER TABLE governance_audit_events ADD COLUMN created_at TEXT NOT NULL DEFAULT ''",
    }
    for column, statement in migrations.items():
        if column not in existing:
            conn.execute(statement)
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_governance_audit_batch
            ON governance_audit_events(batch_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_governance_audit_target
            ON governance_audit_events(target_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_governance_audit_type_action
            ON governance_audit_events(event_type, action, created_at);
        """
    )


def _redact_governance_payload(value: Any) -> Any:
    """Redact report/audit payloads before they become durable governance rows.

    Defensive boundary: governance audit survives cleanup/forgetting actions.
    Never bypass this helper in `record_governance_audit_event`, or hard-deleted
    secrets can be resurrected from `before_json`/`after_json`.
    """

    if isinstance(value, dict):
        return {str(key): _redact_governance_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_governance_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_governance_payload(item) for item in value]
    if isinstance(value, str):
        return sanitize_report_text(value)
    return value


def record_governance_audit_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    event_type: str,
    action: str,
    scope_id: str = "",
    target_id: str = "",
    batch_id: str = "",
    before: Any | None = None,
    after: Any | None = None,
    reason: str = "",
    actor: str = "scope-recall",
    dry_run: bool = False,
    created_at: str | None = None,
) -> None:
    ensure_governance_schema(conn)
    conn.execute(
        """
        INSERT INTO governance_audit_events (
            id, event_type, action, scope_id, target_id, batch_id,
            before_json, after_json, reason, actor, dry_run, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            event_type,
            action,
            scope_id,
            target_id,
            batch_id,
            json.dumps(_redact_governance_payload(before if before is not None else {}), ensure_ascii=False, sort_keys=True),
            json.dumps(_redact_governance_payload(after if after is not None else {}), ensure_ascii=False, sort_keys=True),
            sanitize_report_text(reason),
            sanitize_report_text(actor or "scope-recall"),
            1 if dry_run else 0,
            created_at or now_iso(),
        ),
    )


def ensure_experience_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_episodes (
            id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            shared_scope_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL,
            task_class TEXT NOT NULL DEFAULT '',
            task_goal TEXT NOT NULL,
            user_intent TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            outcome TEXT NOT NULL DEFAULT 'unknown',
            started_at TEXT NOT NULL,
            ended_at TEXT,
            message_ids TEXT NOT NULL DEFAULT '[]',
            journal_entry_ids TEXT NOT NULL DEFAULT '[]',
            tool_names TEXT NOT NULL DEFAULT '[]',
            evidence TEXT NOT NULL DEFAULT '[]',
            verification TEXT NOT NULL DEFAULT '[]',
            environment TEXT NOT NULL DEFAULT '{}',
            metadata TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS procedural_playbooks (
            id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            shared_scope_id TEXT NOT NULL DEFAULT '',
            task_class TEXT NOT NULL,
            title TEXT NOT NULL,
            trigger TEXT NOT NULL,
            goal TEXT NOT NULL,
            preconditions TEXT NOT NULL DEFAULT '[]',
            steps TEXT NOT NULL DEFAULT '[]',
            pitfalls TEXT NOT NULL DEFAULT '[]',
            verification TEXT NOT NULL DEFAULT '[]',
            cleanup TEXT NOT NULL DEFAULT '[]',
            evidence_anchors TEXT NOT NULL DEFAULT '[]',
            related_skills TEXT NOT NULL DEFAULT '[]',
            environment_constraints TEXT NOT NULL DEFAULT '{}',
            reuse_policy TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'candidate',
            confidence REAL NOT NULL DEFAULT 0.50,
            success_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            stale_count INTEGER NOT NULL DEFAULT 0,
            created_from_episode_id TEXT NOT NULL DEFAULT '',
            superseded_by TEXT NOT NULL DEFAULT '',
            last_used_at TEXT,
            last_verified_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS procedural_playbooks_fts USING fts5(
            playbook_id UNINDEXED,
            title,
            trigger,
            goal,
            preconditions,
            steps,
            pitfalls,
            verification
        );

        CREATE TABLE IF NOT EXISTS playbook_versions (
            id TEXT PRIMARY KEY,
            playbook_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            change_type TEXT NOT NULL,
            change_reason TEXT NOT NULL DEFAULT '',
            snapshot TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS experience_runs (
            id TEXT PRIMARY KEY,
            playbook_id TEXT NOT NULL,
            episode_id TEXT NOT NULL DEFAULT '',
            scope_id TEXT NOT NULL,
            decision TEXT NOT NULL,
            confidence_at_use REAL NOT NULL DEFAULT 0.0,
            preconditions_checked TEXT NOT NULL DEFAULT '[]',
            steps_completed TEXT NOT NULL DEFAULT '[]',
            evidence TEXT NOT NULL DEFAULT '[]',
            outcome TEXT NOT NULL DEFAULT 'unknown',
            outcome_reason TEXT NOT NULL DEFAULT '',
            model_name TEXT NOT NULL DEFAULT '',
            tool_call_count INTEGER NOT NULL DEFAULT 0,
            token_estimate INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            metadata TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS reflection_events (
            id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL,
            playbook_id TEXT NOT NULL DEFAULT '',
            scope_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            outcome TEXT NOT NULL,
            evidence TEXT NOT NULL DEFAULT '[]',
            mistakes TEXT NOT NULL DEFAULT '[]',
            root_causes TEXT NOT NULL DEFAULT '[]',
            corrections TEXT NOT NULL DEFAULT '[]',
            proposed_updates TEXT NOT NULL DEFAULT '[]',
            applied_updates TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS fact_freshness (
            id TEXT PRIMARY KEY,
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            fact_key TEXT NOT NULL,
            truth_type TEXT NOT NULL,
            validator_kind TEXT NOT NULL DEFAULT '',
            validator_spec TEXT NOT NULL DEFAULT '{}',
            ttl_days INTEGER NOT NULL DEFAULT 0,
            last_checked_at TEXT,
            valid_until TEXT,
            status TEXT NOT NULL DEFAULT 'unknown',
            stale_reason TEXT NOT NULL DEFAULT '',
            superseded_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS skill_anchors (
            id TEXT PRIMARY KEY,
            playbook_id TEXT NOT NULL,
            skill_name TEXT NOT NULL,
            load_policy TEXT NOT NULL DEFAULT 'optional_reference',
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS skill_conflicts (
            id TEXT PRIMARY KEY,
            playbook_id TEXT NOT NULL,
            skill_name TEXT NOT NULL DEFAULT '',
            conflicting_source TEXT NOT NULL DEFAULT '',
            conflict_summary TEXT NOT NULL,
            resolution TEXT NOT NULL DEFAULT 'needs_live_check',
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            metadata TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_task_episodes_scope_status
            ON task_episodes(scope_id, status, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_task_episodes_shared_scope
            ON task_episodes(shared_scope_id, status, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_experience_playbooks_scope_task_status
            ON procedural_playbooks(scope_id, task_class, status, confidence DESC);
        CREATE INDEX IF NOT EXISTS idx_experience_playbooks_shared_scope
            ON procedural_playbooks(shared_scope_id, task_class, status, confidence DESC);
        CREATE INDEX IF NOT EXISTS idx_playbook_versions_playbook_version
            ON playbook_versions(playbook_id, version DESC);
        CREATE INDEX IF NOT EXISTS idx_experience_runs_playbook_started
            ON experience_runs(playbook_id, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_experience_runs_scope_outcome
            ON experience_runs(scope_id, outcome, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_reflection_events_scope_created
            ON reflection_events(scope_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_fact_freshness_subject
            ON fact_freshness(subject_type, subject_id, fact_key);
        CREATE INDEX IF NOT EXISTS idx_fact_freshness_status
            ON fact_freshness(status, valid_until);
        CREATE INDEX IF NOT EXISTS idx_skill_anchors_playbook
            ON skill_anchors(playbook_id, skill_name);
        CREATE INDEX IF NOT EXISTS idx_skill_conflicts_playbook_status
            ON skill_conflicts(playbook_id, status);
        """
    )


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


def _fts_counts(conn: sqlite3.Connection) -> dict[str, int | bool]:
    memory_rows = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
    fts_rows = int(conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0])
    stale_fts_rows = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM memories_fts AS f
            LEFT JOIN memories AS m ON m.id = f.memory_id
            WHERE m.id IS NULL
            """
        ).fetchone()[0]
    )
    missing_fts_rows = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM memories AS m
            LEFT JOIN memories_fts AS f ON f.memory_id = m.id
            WHERE f.memory_id IS NULL
            """
        ).fetchone()[0]
    )
    duplicate_fts_extra_rows = int(
        conn.execute(
            """
            SELECT COALESCE(SUM(extra), 0)
            FROM (
                SELECT COUNT(*) - 1 AS extra
                FROM memories_fts
                GROUP BY memory_id
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
    )
    healthy = stale_fts_rows == 0 and missing_fts_rows == 0 and duplicate_fts_extra_rows == 0 and fts_rows == memory_rows
    return {
        "memory_rows": memory_rows,
        "fts_rows": fts_rows,
        "stale_fts_rows": stale_fts_rows,
        "missing_fts_rows": missing_fts_rows,
        "duplicate_fts_extra_rows": duplicate_fts_extra_rows,
        "healthy": healthy,
    }


def fts_integrity_report(conn: sqlite3.Connection) -> dict[str, int | bool]:
    return _fts_counts(conn)


def reconcile_fts_index(conn: sqlite3.Connection) -> dict[str, Any]:
    before = _fts_counts(conn)
    needs_rebuild = not bool(before["healthy"])
    if needs_rebuild:
        conn.execute("DELETE FROM memories_fts")
        conn.execute("INSERT INTO memories_fts(memory_id, content, summary) SELECT id, content, summary FROM memories")
        conn.commit()
    after = _fts_counts(conn)
    return {"rebuilt": needs_rebuild, "before": before, "after": after}


def rebuild_fts_if_empty(conn: sqlite3.Connection) -> None:
    reconcile_fts_index(conn)


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
    content = enrich_content_with_artifact_anchors(content)
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

    metadata_payload = merge_metadata(dict(classify_memory(content, target, source)), metadata)
    metadata_payload = merge_artifact_metadata(metadata_payload, content)
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
    sync_memory_entities(conn, memory_id=memory_id, content=content, target=target, metadata=metadata_payload)
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
    content = enrich_content_with_artifact_anchors(content)
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
    old_metadata: dict[str, Any] = {}
    try:
        old_metadata.update(json.loads(str(row["metadata"] or "{}")))
    except Exception:
        pass
    classified_metadata = classify_memory(content, new_target, str(row["source"]))
    metadata_payload = dict(old_metadata)
    metadata_payload.update(classified_metadata)

    # Updates should reclassify content/target-derived policy fields, but must
    # not erase accumulated quality/governance evidence that came from explicit
    # feedback or prior conflict review.
    for protected_key in (
        "feedback_count",
        "helpful_count",
        "unhelpful_count",
        "relation_types",
        "conflict_count",
        "conflict_review_count",
        "conflict_review_ids",
        "needs_conflict_review",
    ):
        if protected_key in old_metadata:
            metadata_payload[protected_key] = old_metadata[protected_key]
    try:
        feedback_count = int(old_metadata.get("feedback_count") or 0)
    except (TypeError, ValueError):
        feedback_count = 0
    if feedback_count > 0 and "trust" in old_metadata:
        metadata_payload["trust"] = old_metadata["trust"]
    try:
        old_importance = float(old_metadata.get("importance") or 0.0)
        new_importance = float(classified_metadata.get("importance") or 0.0)
    except (TypeError, ValueError):
        old_importance = new_importance = 0.0
    if old_importance > new_importance:
        metadata_payload["importance"] = old_metadata["importance"]
    metadata_payload = merge_artifact_metadata(metadata_payload, content)
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
    sync_memory_entities(conn, memory_id=memory_id, content=content, target=new_target, metadata=metadata_payload)
    conn.commit()
    return True, summary, updated_at


def _sync_conflict_metadata_after_relation_delete(conn: sqlite3.Connection, memory_ids: list[str]) -> None:
    for memory_id in sorted({str(item) for item in memory_ids if str(item)}):
        row = conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            continue
        try:
            metadata = json.loads(str(row["metadata"] or "{}"))
        except Exception:
            metadata = {}
        relation_rows = conn.execute(
            """
            SELECT target_memory_id AS peer_id
            FROM memory_relations
            WHERE source_memory_id = ? AND relation_type = 'contradicts'
            UNION
            SELECT source_memory_id AS peer_id
            FROM memory_relations
            WHERE target_memory_id = ? AND relation_type = 'contradicts'
            """,
            (memory_id, memory_id),
        ).fetchall()
        conflict_ids = sorted({str(rel["peer_id"]) for rel in relation_rows if str(rel["peer_id"]) and str(rel["peer_id"]) != memory_id})
        relation_types = metadata.get("relation_types")
        if not isinstance(relation_types, list):
            relation_types = []
        relation_types = [str(item) for item in relation_types if str(item) and str(item) != "contradicts"]
        if conflict_ids:
            relation_types.append("contradicts")
            metadata["conflict_review_ids"] = conflict_ids
            metadata["conflict_count"] = len(conflict_ids)
            metadata["conflict_review_count"] = len(conflict_ids)
            metadata["needs_conflict_review"] = True
        else:
            metadata["conflict_review_ids"] = []
            metadata["conflict_count"] = 0
            metadata["conflict_review_count"] = 0
            metadata["needs_conflict_review"] = False
        metadata["relation_types"] = relation_types
        conn.execute(
            "UPDATE memories SET metadata = ? WHERE id = ?",
            (json.dumps(metadata, ensure_ascii=False, sort_keys=True), memory_id),
        )


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
    conflict_peer_rows = conn.execute(
        f"""
        SELECT target_memory_id AS peer_id
        FROM memory_relations
        WHERE relation_type = 'contradicts' AND source_memory_id IN ({placeholders})
        UNION
        SELECT source_memory_id AS peer_id
        FROM memory_relations
        WHERE relation_type = 'contradicts' AND target_memory_id IN ({placeholders})
        """,
        [*scoped_ids, *scoped_ids],
    ).fetchall()
    conflict_peer_ids = [str(row["peer_id"]) for row in conflict_peer_rows if str(row["peer_id"]) and str(row["peer_id"]) not in scoped_ids]
    conn.execute(f"DELETE FROM memories_fts WHERE memory_id IN ({placeholders})", scoped_ids)
    conn.execute(f"DELETE FROM memory_entities WHERE memory_id IN ({placeholders})", scoped_ids)
    conn.execute(f"DELETE FROM memory_feedback WHERE memory_id IN ({placeholders})", scoped_ids)
    conn.execute(
        f"DELETE FROM memory_relations WHERE source_memory_id IN ({placeholders}) OR target_memory_id IN ({placeholders})",
        [*scoped_ids, *scoped_ids],
    )
    if _table_exists(conn, "memory_digest_sources"):
        conn.execute(f"DELETE FROM memory_digest_sources WHERE memory_id IN ({placeholders})", scoped_ids)
    if _table_exists(conn, "memory_journal_sources"):
        conn.execute(f"DELETE FROM memory_journal_sources WHERE memory_id IN ({placeholders})", scoped_ids)
    conn.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", scoped_ids)
    _sync_conflict_metadata_after_relation_delete(conn, conflict_peer_ids)
    conn.commit()
    after = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
    return max(0, before - after)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1", (table,)).fetchone()
    return row is not None


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
