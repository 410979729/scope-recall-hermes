"""Incremental relation entity index and recoverable maintenance debt.

The relation extractor must not rescan every memory in a scope inside a foreground
write transaction.  This companion stores one canonical entity membership row per
visible memory plus per-scope/entity document counts.  Public truth mutations call
``sync_relation_frequency_memory`` in the same SQLite transaction.  Triggers only
record dirty ids for direct SQL or crash recovery; a bounded maintenance worker
repairs those ids and pages legacy backfill without loading a whole scope.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

try:
    from .graph import lifecycle_is_hidden, load_metadata, metadata_entities
    from .relation_entity_policy import (
        distinctive_relation_entity,
        high_frequency_document_threshold,
        normalize_relation_entity,
    )
    from .relation_scope_state import (
        blocked_entities_receipt_hash,
        decode_blocked_entities_receipt,
        load_scope_frequency_receipt,
    )
    from .sqlite_schema import execute_script_transaction_neutral
except ImportError:  # pragma: no cover - direct source-script execution fallback
    from graph import lifecycle_is_hidden, load_metadata, metadata_entities
    from relation_entity_policy import (  # type: ignore[no-redef]
        distinctive_relation_entity,
        high_frequency_document_threshold,
        normalize_relation_entity,
    )
    from relation_scope_state import (  # type: ignore[no-redef]
        blocked_entities_receipt_hash,
        decode_blocked_entities_receipt,
        load_scope_frequency_receipt,
    )
    from sqlite_schema import execute_script_transaction_neutral

RELATION_FREQUENCY_INDEX_SCHEMA_VERSION = 10805
RELATION_FREQUENCY_INDEX_MIGRATION_ID = "0007_relation_frequency_index_v1_8_0"
RELATION_FREQUENCY_INDEX_MIGRATION_PLUGIN_VERSION = "1.8.0"
RELATION_FREQUENCY_INDEX_MIGRATION_DESCRIPTION = (
    "Incremental scope/entity relation frequency index with bounded recovery"
)
RELATION_FREQUENCY_FAILURE_SCHEMA_VERSION = 10811
RELATION_FREQUENCY_FAILURE_MIGRATION_ID = (
    "0011_relation_frequency_failure_queue_v1_8_0"
)
RELATION_FREQUENCY_FAILURE_MIGRATION_PLUGIN_VERSION = "1.8.0"
RELATION_FREQUENCY_FAILURE_MIGRATION_DESCRIPTION = (
    "Bounded retry and dead-letter evidence for relation-frequency dirty rows"
)

_REQUIRED_TABLES = {
    "relation_indexed_memories",
    "relation_entity_postings",
    "relation_scope_entity_frequency",
    "relation_frequency_changes",
    "relation_frequency_backfill",
    "relation_scope_reclassification",
    "relation_frequency_failures",
}
_REQUIRED_TRIGGERS = {
    "trg_relation_frequency_insert",
    "trg_relation_frequency_update",
    "trg_relation_frequency_delete",
}


class RelationFrequencyIndexNotReady(RuntimeError):
    """The requested scope still has bounded index repair/backfill debt."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _canonical_entities(*, metadata: Any, content: str, target: str) -> set[str]:
    parsed = load_metadata(metadata)
    return {
        normalize_relation_entity(entity)
        for entity in metadata_entities(parsed, content, target)
        if distinctive_relation_entity(entity)
    }


def _entities_json(entities: Iterable[str]) -> str:
    return json.dumps(
        sorted({str(entity) for entity in entities if str(entity)}),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _entities_hash(entities_json: str) -> str:
    return hashlib.sha256(str(entities_json).encode("utf-8")).hexdigest()


def _decode_entities(raw: Any) -> set[str]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except Exception:
        return set()
    if not isinstance(parsed, list):
        return set()
    return {str(item) for item in parsed if isinstance(item, str) and str(item)}


def ensure_relation_frequency_index_schema(conn: sqlite3.Connection) -> None:
    """Create additive index/debt state without scanning or parsing truth rows."""

    first_install = not _table_exists(conn, "relation_indexed_memories")
    execute_script_transaction_neutral(
        conn,
        """
        CREATE TABLE IF NOT EXISTS relation_indexed_memories (
            memory_id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT '',
            visible INTEGER NOT NULL DEFAULT 0 CHECK(visible IN (0,1)),
            entities_json TEXT NOT NULL DEFAULT '[]',
            entities_sha256 TEXT NOT NULL DEFAULT '',
            indexed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_relation_indexed_memories_scope
            ON relation_indexed_memories(scope_id, memory_id);

        CREATE TABLE IF NOT EXISTS relation_entity_postings (
            scope_id TEXT NOT NULL,
            entity TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            PRIMARY KEY(scope_id, entity, memory_id)
        );
        CREATE INDEX IF NOT EXISTS idx_relation_entity_postings_memory
            ON relation_entity_postings(memory_id, scope_id, entity);

        CREATE TABLE IF NOT EXISTS relation_scope_entity_frequency (
            scope_id TEXT NOT NULL,
            entity TEXT NOT NULL,
            document_count INTEGER NOT NULL CHECK(document_count > 0),
            updated_at TEXT NOT NULL,
            PRIMARY KEY(scope_id, entity)
        );
        CREATE INDEX IF NOT EXISTS idx_relation_frequency_threshold
            ON relation_scope_entity_frequency(scope_id, document_count, entity);

        CREATE TABLE IF NOT EXISTS relation_frequency_changes (
            memory_id TEXT PRIMARY KEY,
            old_scope_id TEXT NOT NULL DEFAULT '',
            new_scope_id TEXT NOT NULL DEFAULT '',
            requested_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_relation_frequency_changes_old_scope
            ON relation_frequency_changes(old_scope_id, requested_at, memory_id);
        CREATE INDEX IF NOT EXISTS idx_relation_frequency_changes_new_scope
            ON relation_frequency_changes(new_scope_id, requested_at, memory_id);

        CREATE TABLE IF NOT EXISTS relation_frequency_failures (
            memory_id TEXT PRIMARY KEY,
            old_scope_id TEXT NOT NULL DEFAULT '',
            new_scope_id TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
            status TEXT NOT NULL DEFAULT 'retry'
                CHECK(status IN ('retry','dead_letter')),
            last_error TEXT NOT NULL DEFAULT '',
            last_failed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_relation_frequency_failures_status
            ON relation_frequency_failures(status, last_failed_at, memory_id);

        CREATE TABLE IF NOT EXISTS relation_frequency_backfill (
            scope_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','complete')),
            cursor_memory_id TEXT NOT NULL DEFAULT '',
            processed_memories INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_relation_frequency_backfill_status
            ON relation_frequency_backfill(status, updated_at, scope_id);

        CREATE TABLE IF NOT EXISTS relation_scope_reclassification (
            scope_id TEXT PRIMARY KEY,
            active_revision INTEGER NOT NULL DEFAULT 0,
            next_revision INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','complete')),
            cursor_memory_id TEXT NOT NULL DEFAULT '',
            pass_processed_memories INTEGER NOT NULL DEFAULT 0,
            total_processed_memories INTEGER NOT NULL DEFAULT 0,
            pass_number INTEGER NOT NULL DEFAULT 1,
            requested_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_relation_reclassification_status
            ON relation_scope_reclassification(status, updated_at, scope_id);
        """,
    )
    if not _table_exists(conn, "memories"):
        return

    # Trigger SQL cannot parse relation entities safely.  It records only the
    # exact memory id and before/after scope, leaving semantic delta computation
    # to the bounded Python worker or the public mutation transaction.
    for trigger in sorted(_REQUIRED_TRIGGERS):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    execute_script_transaction_neutral(
        conn,
        """
        CREATE TRIGGER trg_relation_frequency_insert
        AFTER INSERT ON memories
        BEGIN
            INSERT OR IGNORE INTO relation_frequency_backfill(
                scope_id, status, cursor_memory_id, processed_memories,
                created_at, updated_at, completed_at
            ) VALUES(
                NEW.scope_id, 'complete', '', 0,
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            );
            INSERT INTO relation_frequency_changes(
                memory_id, old_scope_id, new_scope_id, requested_at
            ) VALUES(
                NEW.id, '', NEW.scope_id,
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
            ON CONFLICT(memory_id) DO UPDATE SET
                old_scope_id=CASE
                    WHEN relation_frequency_changes.old_scope_id<>''
                    THEN relation_frequency_changes.old_scope_id
                    ELSE excluded.old_scope_id
                END,
                new_scope_id=excluded.new_scope_id,
                requested_at=excluded.requested_at;
        END;

        CREATE TRIGGER trg_relation_frequency_update
        AFTER UPDATE OF scope_id, content, target, metadata, updated_at ON memories
        WHEN OLD.scope_id IS NOT NEW.scope_id
          OR OLD.content IS NOT NEW.content
          OR OLD.target IS NOT NEW.target
          OR OLD.updated_at IS NOT NEW.updated_at
          OR COALESCE(json_extract(OLD.metadata, '$.entities'), '[]')
             IS NOT COALESCE(json_extract(NEW.metadata, '$.entities'), '[]')
          OR COALESCE(json_extract(OLD.metadata, '$.lifecycle'), 'active')
             IS NOT COALESCE(json_extract(NEW.metadata, '$.lifecycle'), 'active')
        BEGIN
            INSERT OR IGNORE INTO relation_frequency_backfill(
                scope_id, status, cursor_memory_id, processed_memories,
                created_at, updated_at, completed_at
            ) VALUES(
                NEW.scope_id, 'complete', '', 0,
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            );
            INSERT INTO relation_frequency_changes(
                memory_id, old_scope_id, new_scope_id, requested_at
            ) VALUES(
                NEW.id, OLD.scope_id, NEW.scope_id,
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
            ON CONFLICT(memory_id) DO UPDATE SET
                old_scope_id=CASE
                    WHEN relation_frequency_changes.old_scope_id<>''
                    THEN relation_frequency_changes.old_scope_id
                    ELSE excluded.old_scope_id
                END,
                new_scope_id=excluded.new_scope_id,
                requested_at=excluded.requested_at;
        END;

        CREATE TRIGGER trg_relation_frequency_delete
        AFTER DELETE ON memories
        BEGIN
            INSERT INTO relation_frequency_changes(
                memory_id, old_scope_id, new_scope_id, requested_at
            ) VALUES(
                OLD.id, OLD.scope_id, '',
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
            ON CONFLICT(memory_id) DO UPDATE SET
                old_scope_id=CASE
                    WHEN relation_frequency_changes.old_scope_id<>''
                    THEN relation_frequency_changes.old_scope_id
                    ELSE excluded.old_scope_id
                END,
                new_scope_id='',
                requested_at=excluded.requested_at;
        END;
        """,
    )

    if first_install:
        # Existing scopes are legacy debt.  Seeding one row per indexed scope is
        # cheap and does not parse metadata/content; each truth page is processed
        # later under an explicit cursor.
        now = _now_iso()
        conn.execute(
            """
            INSERT OR IGNORE INTO relation_frequency_backfill(
                scope_id, status, cursor_memory_id, processed_memories,
                created_at, updated_at, completed_at
            )
            SELECT scope_id, 'pending', '', 0, ?, ?, NULL
            FROM memories
            GROUP BY scope_id
            """,
            (now, now),
        )


def relation_frequency_index_schema_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return additive table/trigger readiness without mutating SQLite."""

    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    triggers = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    missing_tables = sorted(_REQUIRED_TABLES - tables)
    missing_triggers = (
        sorted(_REQUIRED_TRIGGERS - triggers)
        if "memories" in tables
        else []
    )
    return {
        "current": not missing_tables and not missing_triggers,
        "schema_version": RELATION_FREQUENCY_FAILURE_SCHEMA_VERSION,
        "missing_tables": missing_tables,
        "missing_triggers": missing_triggers,
        "failures": {
            "retry": int(
                conn.execute(
                    "SELECT COUNT(*) FROM relation_frequency_failures WHERE status='retry'"
                ).fetchone()[0]
            )
            if "relation_frequency_failures" in tables
            else 0,
            "dead_letter": int(
                conn.execute(
                    "SELECT COUNT(*) FROM relation_frequency_failures WHERE status='dead_letter'"
                ).fetchone()[0]
            )
            if "relation_frequency_failures" in tables
            else 0,
        },
    }


def _stored_blocked_entities(
    conn: sqlite3.Connection, scope_id: str
) -> set[str] | None:
    row = conn.execute(
        """
        SELECT statistics_revision, blocked_entities_json, blocked_entities_sha256
        FROM relation_scope_statistics WHERE scope_id=?
        """,
        (str(scope_id),),
    ).fetchone()
    if row is None or int(row[0] or -1) < 0:
        return None
    return decode_blocked_entities_receipt(
        scope_id=str(scope_id),
        corpus_revision=int(row[0]),
        blocked_entities_json=str(row[1] or "[]"),
        blocked_entities_sha256=str(row[2] or ""),
    )


def _blocked_entities_from_counts(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    visible_memory_count: int,
) -> set[str]:
    threshold = high_frequency_document_threshold(visible_memory_count)
    if threshold is None:
        return set()
    return {
        str(row[0])
        for row in conn.execute(
            """
            SELECT entity
            FROM relation_scope_entity_frequency
            WHERE scope_id=? AND document_count>=?
            ORDER BY entity
            """,
            (str(scope_id), int(threshold)),
        ).fetchall()
    }


def _scope_backfill_complete(conn: sqlite3.Connection, scope_id: str) -> bool:
    row = conn.execute(
        "SELECT status FROM relation_frequency_backfill WHERE scope_id=?",
        (str(scope_id),),
    ).fetchone()
    return row is not None and str(row[0]) == "complete"


def _scope_has_dirty_rows(conn: sqlite3.Connection, scope_id: str) -> bool:
    return (
        conn.execute(
            """
            SELECT 1 FROM relation_frequency_changes
            WHERE old_scope_id=? OR new_scope_id=?
            LIMIT 1
            """,
            (str(scope_id), str(scope_id)),
        ).fetchone()
        is not None
    )


def _schedule_scope_reclassification(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    revision: int,
) -> None:
    now = _now_iso()
    row = conn.execute(
        """
        SELECT status, cursor_memory_id, pass_processed_memories,
               active_revision, next_revision
        FROM relation_scope_reclassification WHERE scope_id=?
        """,
        (str(scope_id),),
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO relation_scope_reclassification(
                scope_id, active_revision, next_revision, status,
                cursor_memory_id, pass_processed_memories,
                total_processed_memories, pass_number,
                requested_at, updated_at, completed_at
            ) VALUES(?, ?, 0, 'pending', '', 0, 0, 1, ?, ?, NULL)
            """,
            (str(scope_id), int(revision), now, now),
        )
        return
    in_progress = str(row[0]) == "pending" and (
        bool(str(row[1] or "")) or int(row[2] or 0) > 0
    )
    if in_progress:
        conn.execute(
            """
            UPDATE relation_scope_reclassification
            SET next_revision=MAX(next_revision, ?), requested_at=?, updated_at=?
            WHERE scope_id=?
            """,
            (int(revision), now, now, str(scope_id)),
        )
        return
    if int(row[3] or 0) == int(revision) and str(row[0]) == "pending":
        return
    conn.execute(
        """
        UPDATE relation_scope_reclassification
        SET active_revision=?, next_revision=0, status='pending',
            cursor_memory_id='', pass_processed_memories=0,
            pass_number=pass_number+1, requested_at=?, updated_at=?, completed_at=NULL
        WHERE scope_id=?
        """,
        (int(revision), now, now, str(scope_id)),
    )


def refresh_relation_scope_frequency_receipt(conn: sqlite3.Connection, scope_id: str) -> dict[str, Any] | None:
    scope = str(scope_id or "")
    if not scope or not _scope_backfill_complete(conn, scope):
        return None
    if _scope_has_dirty_rows(conn, scope):
        return None
    row = conn.execute(
        """
        SELECT corpus_revision, visible_memory_count
        FROM relation_scope_statistics WHERE scope_id=?
        """,
        (scope,),
    ).fetchone()
    if row is None:
        return None
    revision = int(row[0] or 0)
    visible_count = max(0, int(row[1] or 0))
    old_blocked = _stored_blocked_entities(conn, scope)
    blocked = _blocked_entities_from_counts(
        conn,
        scope_id=scope,
        visible_memory_count=visible_count,
    )
    blocked_json = _entities_json(blocked)
    digest = blocked_entities_receipt_hash(scope, revision, blocked)
    changed = conn.execute(
        """
        UPDATE relation_scope_statistics
        SET statistics_revision=?, blocked_entities_json=?,
            blocked_entities_sha256=?, updated_at=?
        WHERE scope_id=? AND corpus_revision=?
        """,
        (revision, blocked_json, digest, _now_iso(), scope, revision),
    ).rowcount
    if changed != 1:
        # Another connection advanced corpus_revision after this snapshot was
        # read.  Returning its receipt would let relation work bind a stale
        # high-frequency policy; callers must defer and retry from fresh truth.
        return None
    if old_blocked is not None and old_blocked != blocked:
        _schedule_scope_reclassification(
            conn,
            scope_id=scope,
            revision=revision,
        )
    return {
        "scope_id": scope,
        "corpus_revision": revision,
        "visible_memory_count": visible_count,
        "blocked_entities": blocked,
        "blocked_entities_json": blocked_json,
        "blocked_entities_sha256": digest,
        "cache_hit": True,
    }


def _decrement_frequency(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    entity: str,
    now: str,
) -> None:
    row = conn.execute(
        """
        SELECT document_count
        FROM relation_scope_entity_frequency
        WHERE scope_id=? AND entity=?
        """,
        (scope_id, entity),
    ).fetchone()
    if row is None or int(row[0] or 0) <= 0:
        raise RuntimeError(
            f"relation entity frequency underflow: scope={scope_id!r} entity={entity!r}"
        )
    if int(row[0]) == 1:
        changed = conn.execute(
            """
            DELETE FROM relation_scope_entity_frequency
            WHERE scope_id=? AND entity=? AND document_count=1
            """,
            (scope_id, entity),
        ).rowcount
    else:
        changed = conn.execute(
            """
            UPDATE relation_scope_entity_frequency
            SET document_count=document_count-1, updated_at=?
            WHERE scope_id=? AND entity=? AND document_count>1
            """,
            (now, scope_id, entity),
        ).rowcount
    if changed != 1:
        raise RuntimeError(
            f"relation entity frequency CAS failed: scope={scope_id!r} entity={entity!r}"
        )


def _increment_frequency(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    entity: str,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO relation_scope_entity_frequency(
            scope_id, entity, document_count, updated_at
        ) VALUES(?, ?, 1, ?)
        ON CONFLICT(scope_id, entity) DO UPDATE SET
            document_count=relation_scope_entity_frequency.document_count+1,
            updated_at=excluded.updated_at
        """,
        (scope_id, entity, now),
    )


def sync_relation_frequency_memory(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    refresh_receipts: bool = True,
) -> dict[str, Any]:
    """Apply one truth row's entity/count delta inside the caller transaction."""

    clean_id = str(memory_id or "").strip()
    if not clean_id:
        raise ValueError("memory_id is required")
    if not _table_exists(conn, "relation_indexed_memories"):
        raise RuntimeError("relation frequency schema is not initialized")

    old = conn.execute(
        """
        SELECT scope_id, visible, entities_json
        FROM relation_indexed_memories WHERE memory_id=?
        """,
        (clean_id,),
    ).fetchone()
    old_scope = str(old[0] or "") if old is not None else ""
    old_visible = bool(int(old[1] or 0)) if old is not None else False
    old_entities = _decode_entities(old[2]) if old_visible and old is not None else set()

    current = conn.execute(
        """
        SELECT id, scope_id, target, content, updated_at, metadata
        FROM memories WHERE id=?
        """,
        (clean_id,),
    ).fetchone()
    if current is None:
        new_scope = ""
        new_visible = False
        new_entities: set[str] = set()
        new_updated_at = ""
    else:
        new_scope = str(current[1] or "")
        metadata = load_metadata(current[5])
        new_visible = not lifecycle_is_hidden(metadata)
        new_entities = (
            _canonical_entities(
                metadata=metadata,
                content=str(current[3] or ""),
                target=str(current[2] or ""),
            )
            if new_visible
            else set()
        )
        new_updated_at = str(current[4] or "")

    now = _now_iso()
    old_memberships = {(old_scope, entity) for entity in old_entities if old_scope}
    new_memberships = {(new_scope, entity) for entity in new_entities if new_scope}
    removed = sorted(old_memberships - new_memberships)
    added = sorted(new_memberships - old_memberships)

    for scope_id, entity in removed:
        deleted = conn.execute(
            """
            DELETE FROM relation_entity_postings
            WHERE scope_id=? AND entity=? AND memory_id=?
            """,
            (scope_id, entity, clean_id),
        ).rowcount
        if deleted == 1:
            _decrement_frequency(conn, scope_id=scope_id, entity=entity, now=now)
    for scope_id, entity in added:
        inserted = conn.execute(
            """
            INSERT OR IGNORE INTO relation_entity_postings(scope_id, entity, memory_id)
            VALUES(?, ?, ?)
            """,
            (scope_id, entity, clean_id),
        ).rowcount
        if inserted == 1:
            _increment_frequency(conn, scope_id=scope_id, entity=entity, now=now)

    if old_visible and (not new_visible or old_scope != new_scope):
        conn.execute(
            """
            UPDATE relation_scope_statistics
            SET visible_memory_count=MAX(0, visible_memory_count-1)
            WHERE scope_id=?
            """,
            (old_scope,),
        )
    if new_visible and (not old_visible or old_scope != new_scope):
        conn.execute(
            """
            UPDATE relation_scope_statistics
            SET visible_memory_count=visible_memory_count+1
            WHERE scope_id=?
            """,
            (new_scope,),
        )

    if current is None:
        conn.execute(
            "DELETE FROM relation_indexed_memories WHERE memory_id=?", (clean_id,)
        )
    else:
        encoded = _entities_json(new_entities)
        conn.execute(
            """
            INSERT INTO relation_indexed_memories(
                memory_id, scope_id, updated_at, visible,
                entities_json, entities_sha256, indexed_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                scope_id=excluded.scope_id,
                updated_at=excluded.updated_at,
                visible=excluded.visible,
                entities_json=excluded.entities_json,
                entities_sha256=excluded.entities_sha256,
                indexed_at=excluded.indexed_at
            """,
            (
                clean_id,
                new_scope,
                new_updated_at,
                int(new_visible),
                encoded,
                _entities_hash(encoded),
                now,
            ),
        )
    conn.execute("DELETE FROM relation_frequency_changes WHERE memory_id=?", (clean_id,))

    affected_scopes = sorted({scope for scope in (old_scope, new_scope) if scope})
    receipts: dict[str, dict[str, Any] | None] = {}
    if refresh_receipts:
        for scope in affected_scopes:
            receipts[scope] = refresh_relation_scope_frequency_receipt(conn, scope)
    return {
        "memory_id": clean_id,
        "old_scope_id": old_scope,
        "new_scope_id": new_scope,
        "old_visible": old_visible,
        "new_visible": new_visible,
        "removed_entities": len(removed),
        "added_entities": len(added),
        "receipts": receipts,
    }


def relation_frequency_snapshot(
    conn: sqlite3.Connection,
    scope_id: str,
    *,
    bounded_repair_limit: int = 32,
) -> dict[str, Any] | None:
    """Return an O(1)+blocked-result snapshot when one scope index is current.

    A small dirty-id backlog may be repaired synchronously for direct-SQL test or
    recovery compatibility.  Legacy backfill is never hidden here: callers must
    defer relation work until the background cursor reaches completion.
    """

    scope = str(scope_id or "").strip()
    if not scope or not _scope_backfill_complete(conn, scope):
        return None
    bounded = max(0, min(int(bounded_repair_limit), 256))
    query_only = bool(int(conn.execute("PRAGMA query_only").fetchone()[0] or 0))
    dirty = _scope_has_dirty_rows(conn, scope)
    if not dirty:
        receipt = load_scope_frequency_receipt(conn, scope)
        if receipt is not None:
            return receipt
        if query_only:
            return None
    if dirty and bounded and not query_only:
        rows = conn.execute(
            """
            SELECT memory_id FROM relation_frequency_changes
            WHERE old_scope_id=? OR new_scope_id=?
            ORDER BY requested_at, memory_id
            LIMIT ?
            """,
            (scope, scope, bounded + 1),
        ).fetchall()
        if len(rows) > bounded:
            return None
        for row in rows:
            sync_relation_frequency_memory(
                conn,
                str(row[0]),
                refresh_receipts=False,
            )
    if _scope_has_dirty_rows(conn, scope):
        return None
    refreshed = refresh_relation_scope_frequency_receipt(conn, scope)
    if refreshed is not None:
        return refreshed
    return None


def relation_frequency_snapshots_by_scope(
    conn: sqlite3.Connection,
    scope_ids: Iterable[str] | None = None,
) -> dict[str, dict[str, Any] | None]:
    scopes = sorted({str(scope) for scope in (scope_ids or []) if str(scope)})
    if not scopes:
        scopes = [
            str(row[0])
            for row in conn.execute(
                "SELECT scope_id FROM relation_frequency_backfill ORDER BY scope_id"
            ).fetchall()
        ]
    return {scope: relation_frequency_snapshot(conn, scope) for scope in scopes}


def bounded_relation_peer_ids(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    scope_ids: Iterable[str],
    blocked_entities: set[str],
    limit: int,
) -> tuple[list[str], bool]:
    """Select posting-list peers and a recent fallback without a scope COUNT/scan."""

    clean_id = str(memory_id or "")
    scopes = sorted({str(scope) for scope in scope_ids if str(scope)})
    bounded = max(1, min(int(limit), 5000))
    if not clean_id or not scopes:
        return [], False
    placeholders = ",".join("?" for _ in scopes)
    entity_rows = conn.execute(
        f"""
        SELECT entity FROM relation_entity_postings
        WHERE memory_id=? AND scope_id IN ({placeholders})
        ORDER BY entity
        """,
        (clean_id, *scopes),
    ).fetchall()
    entities = [str(row[0]) for row in entity_rows if str(row[0]) not in blocked_entities]
    selected: list[str] = []
    selected_set: set[str] = set()
    per_entity_limit = max(2, min(bounded + 1, (bounded // max(1, len(entities))) + 2))
    for entity in entities:
        rows = conn.execute(
            f"""
            SELECT memory_id
            FROM relation_entity_postings
            WHERE scope_id IN ({placeholders}) AND entity=? AND memory_id<>?
            ORDER BY memory_id
            LIMIT ?
            """,
            (*scopes, entity, clean_id, per_entity_limit),
        ).fetchall()
        for row in rows:
            peer_id = str(row[0])
            if peer_id not in selected_set:
                selected_set.add(peer_id)
                selected.append(peer_id)
                if len(selected) >= bounded:
                    break
        if len(selected) >= bounded:
            break

    remaining = bounded - len(selected)
    if remaining > 0:
        exclusion = [clean_id, *selected]
        exclusion_placeholders = ",".join("?" for _ in exclusion)
        try:
            from .graph import lifecycle_visible_sql
        except ImportError:  # pragma: no cover
            from graph import lifecycle_visible_sql
        rows = conn.execute(
            f"""
            SELECT m.id
            FROM memories m
            WHERE m.scope_id IN ({placeholders})
              AND m.id NOT IN ({exclusion_placeholders})
              AND {lifecycle_visible_sql('m')}
            ORDER BY m.updated_at DESC, m.id DESC
            LIMIT ?
            """,
            (*scopes, *exclusion, remaining),
        ).fetchall()
        for row in rows:
            peer_id = str(row[0])
            if peer_id not in selected_set:
                selected_set.add(peer_id)
                selected.append(peer_id)

    exclusion = [clean_id, *selected]
    exclusion_placeholders = ",".join("?" for _ in exclusion)
    try:
        from .graph import lifecycle_visible_sql
    except ImportError:  # pragma: no cover
        from graph import lifecycle_visible_sql
    has_more = (
        conn.execute(
            f"""
            SELECT 1 FROM memories m
            WHERE m.scope_id IN ({placeholders})
              AND m.id NOT IN ({exclusion_placeholders})
              AND {lifecycle_visible_sql('m')}
            LIMIT 1
            """,
            (*scopes, *exclusion),
        ).fetchone()
        is not None
    )
    return selected, has_more


__all__ = [
    "RELATION_FREQUENCY_FAILURE_MIGRATION_DESCRIPTION",
    "RELATION_FREQUENCY_FAILURE_MIGRATION_ID",
    "RELATION_FREQUENCY_FAILURE_MIGRATION_PLUGIN_VERSION",
    "RELATION_FREQUENCY_FAILURE_SCHEMA_VERSION",
    "RELATION_FREQUENCY_INDEX_MIGRATION_DESCRIPTION",
    "RELATION_FREQUENCY_INDEX_MIGRATION_ID",
    "RELATION_FREQUENCY_INDEX_MIGRATION_PLUGIN_VERSION",
    "RELATION_FREQUENCY_INDEX_SCHEMA_VERSION",
    "RelationFrequencyIndexNotReady",
    "bounded_relation_peer_ids",
    "ensure_relation_frequency_index_schema",
    "refresh_relation_scope_frequency_receipt",
    "relation_frequency_index_schema_status",
    "relation_frequency_snapshot",
    "relation_frequency_snapshots_by_scope",
    "sync_relation_frequency_memory",
]
