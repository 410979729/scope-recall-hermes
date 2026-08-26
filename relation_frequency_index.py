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
    "relation_frequency_generations",
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
_REQUIRED_TABLE_COLUMNS = {
    "relation_indexed_memories": {
        "memory_id",
        "scope_id",
        "updated_at",
        "visible",
        "entities_json",
        "entities_sha256",
        "indexed_at",
    },
    "relation_entity_postings": {"scope_id", "entity", "memory_id"},
    "relation_scope_entity_frequency": {
        "scope_id",
        "entity",
        "document_count",
        "updated_at",
    },
    "relation_frequency_generations": {
        "memory_id",
        "last_generation",
        "updated_at",
    },
    "relation_frequency_changes": {
        "memory_id",
        "old_scope_id",
        "new_scope_id",
        "work_generation",
        "requested_at",
    },
    "relation_frequency_failures": {
        "memory_id",
        "work_generation",
        "work_revision",
        "old_scope_id",
        "new_scope_id",
        "attempts",
        "status",
        "last_error",
        "last_failed_at",
    },
    "relation_frequency_backfill": {
        "scope_id",
        "status",
        "cursor_memory_id",
        "processed_memories",
        "created_at",
        "updated_at",
        "completed_at",
    },
    "relation_scope_reclassification": {
        "scope_id",
        "active_revision",
        "next_revision",
        "status",
        "cursor_memory_id",
        "pass_processed_memories",
        "total_processed_memories",
        "pass_number",
        "requested_at",
        "updated_at",
        "completed_at",
    },
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


def _decode_indexed_entities(raw: Any, stored_sha256: Any) -> set[str]:
    encoded = str(raw)
    if not str(stored_sha256 or "") or _entities_hash(encoded) != str(stored_sha256):
        raise RuntimeError("relation frequency index receipt mismatch")
    try:
        parsed = json.loads(encoded)
    except Exception as exc:
        raise RuntimeError("relation frequency index receipt is invalid") from exc
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) or not item for item in parsed
    ):
        raise RuntimeError("relation frequency index receipt is invalid")
    entities = {str(item) for item in parsed}
    if len(entities) != len(parsed) or _entities_json(entities) != encoded:
        raise RuntimeError("relation frequency index receipt is not canonical")
    return entities


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

        CREATE TABLE IF NOT EXISTS relation_frequency_generations (
            memory_id TEXT PRIMARY KEY,
            last_generation INTEGER NOT NULL CHECK(last_generation > 0),
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS relation_frequency_changes (
            memory_id TEXT PRIMARY KEY,
            old_scope_id TEXT NOT NULL DEFAULT '',
            new_scope_id TEXT NOT NULL DEFAULT '',
            work_generation INTEGER NOT NULL DEFAULT 1
                CHECK(work_generation > 0),
            requested_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_relation_frequency_changes_old_scope
            ON relation_frequency_changes(old_scope_id, requested_at, memory_id);
        CREATE INDEX IF NOT EXISTS idx_relation_frequency_changes_new_scope
            ON relation_frequency_changes(new_scope_id, requested_at, memory_id);

        CREATE TABLE IF NOT EXISTS relation_frequency_failures (
            memory_id TEXT PRIMARY KEY,
            work_generation INTEGER NOT NULL DEFAULT 0
                CHECK(work_generation >= 0),
            work_revision TEXT NOT NULL DEFAULT '',
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
    change_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(relation_frequency_changes)")
    }
    if "work_generation" not in change_columns:
        conn.execute(
            "ALTER TABLE relation_frequency_changes "
            "ADD COLUMN work_generation INTEGER NOT NULL DEFAULT 1 "
            "CHECK(work_generation > 0)"
        )
    conn.execute(
        """
        INSERT INTO relation_frequency_generations(
            memory_id, last_generation, updated_at
        )
        SELECT memory_id, MAX(1, work_generation), requested_at
        FROM relation_frequency_changes
        WHERE 1
        ON CONFLICT(memory_id) DO UPDATE SET
            last_generation=excluded.last_generation,
            updated_at=excluded.updated_at
        WHERE excluded.last_generation > relation_frequency_generations.last_generation
           OR (
                excluded.last_generation = relation_frequency_generations.last_generation
                AND excluded.updated_at > relation_frequency_generations.updated_at
           )
        """
    )
    failure_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(relation_frequency_failures)")
    }
    had_failure_revision = "work_revision" in failure_columns
    if "work_generation" not in failure_columns:
        conn.execute(
            "ALTER TABLE relation_frequency_failures "
            "ADD COLUMN work_generation INTEGER NOT NULL DEFAULT 0 "
            "CHECK(work_generation >= 0)"
        )
        if had_failure_revision:
            conn.execute(
                """
                UPDATE relation_frequency_failures
                SET work_generation=COALESCE(
                    (SELECT c.work_generation
                     FROM relation_frequency_changes c
                     WHERE c.memory_id=relation_frequency_failures.memory_id
                       AND c.requested_at=relation_frequency_failures.work_revision
                       AND c.old_scope_id=relation_frequency_failures.old_scope_id
                       AND c.new_scope_id=relation_frequency_failures.new_scope_id),
                    0
                )
                """
            )
    if "work_revision" not in failure_columns:
        conn.execute(
            "ALTER TABLE relation_frequency_failures "
            "ADD COLUMN work_revision TEXT NOT NULL DEFAULT ''"
        )
        conn.execute(
            """
            UPDATE relation_frequency_failures
            SET work_revision=COALESCE(
                (SELECT c.requested_at FROM relation_frequency_changes c
                 WHERE c.memory_id=relation_frequency_failures.memory_id),
                ''
            )
            """
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
            INSERT INTO relation_frequency_generations(
                memory_id, last_generation, updated_at
            ) VALUES(
                NEW.id, 1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
            ON CONFLICT(memory_id) DO UPDATE SET
                last_generation=relation_frequency_generations.last_generation+1,
                updated_at=excluded.updated_at;
            INSERT INTO relation_frequency_changes(
                memory_id, old_scope_id, new_scope_id,
                work_generation, requested_at
            )
            SELECT NEW.id, '', NEW.scope_id, last_generation,
                   strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            FROM relation_frequency_generations WHERE memory_id=NEW.id
            ON CONFLICT(memory_id) DO UPDATE SET
                old_scope_id=CASE
                    WHEN relation_frequency_changes.old_scope_id<>''
                    THEN relation_frequency_changes.old_scope_id
                    ELSE excluded.old_scope_id
                END,
                new_scope_id=excluded.new_scope_id,
                work_generation=excluded.work_generation,
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
            INSERT INTO relation_frequency_generations(
                memory_id, last_generation, updated_at
            ) VALUES(
                NEW.id, 1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
            ON CONFLICT(memory_id) DO UPDATE SET
                last_generation=relation_frequency_generations.last_generation+1,
                updated_at=excluded.updated_at;
            INSERT INTO relation_frequency_changes(
                memory_id, old_scope_id, new_scope_id,
                work_generation, requested_at
            )
            SELECT NEW.id, OLD.scope_id, NEW.scope_id, last_generation,
                   strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            FROM relation_frequency_generations WHERE memory_id=NEW.id
            ON CONFLICT(memory_id) DO UPDATE SET
                old_scope_id=CASE
                    WHEN relation_frequency_changes.old_scope_id<>''
                    THEN relation_frequency_changes.old_scope_id
                    ELSE excluded.old_scope_id
                END,
                new_scope_id=excluded.new_scope_id,
                work_generation=excluded.work_generation,
                requested_at=excluded.requested_at;
        END;

        CREATE TRIGGER trg_relation_frequency_delete
        AFTER DELETE ON memories
        BEGIN
            INSERT INTO relation_frequency_generations(
                memory_id, last_generation, updated_at
            ) VALUES(
                OLD.id, 1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
            ON CONFLICT(memory_id) DO UPDATE SET
                last_generation=relation_frequency_generations.last_generation+1,
                updated_at=excluded.updated_at;
            INSERT INTO relation_frequency_changes(
                memory_id, old_scope_id, new_scope_id,
                work_generation, requested_at
            )
            SELECT OLD.id, OLD.scope_id, '', last_generation,
                   strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            FROM relation_frequency_generations WHERE memory_id=OLD.id
            ON CONFLICT(memory_id) DO UPDATE SET
                old_scope_id=CASE
                    WHEN relation_frequency_changes.old_scope_id<>''
                    THEN relation_frequency_changes.old_scope_id
                    ELSE excluded.old_scope_id
                END,
                new_scope_id='',
                work_generation=excluded.work_generation,
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
    missing_columns: dict[str, list[str]] = {}
    for table, expected_columns in _REQUIRED_TABLE_COLUMNS.items():
        if table not in tables:
            continue
        actual_columns = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
        }
        missing = sorted(expected_columns - actual_columns)
        if missing:
            missing_columns[table] = missing
    missing_triggers = (
        sorted(_REQUIRED_TRIGGERS - triggers)
        if "memories" in tables
        else []
    )
    return {
        "current": not missing_tables and not missing_triggers and not missing_columns,
        "schema_version": RELATION_FREQUENCY_FAILURE_SCHEMA_VERSION,
        "missing_tables": missing_tables,
        "missing_triggers": missing_triggers,
        "missing_columns": missing_columns,
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


def supersede_relation_frequency_failure(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    work_generation: int | None = None,
    requested_at: str | None = None,
) -> bool:
    """Dispose an older failed generation before processing exact newer work."""

    clean_id = str(memory_id or "").strip()
    if not clean_id:
        raise ValueError("memory_id is required")
    if not (
        _table_exists(conn, "relation_frequency_changes")
        and _table_exists(conn, "relation_frequency_failures")
    ):
        return False
    change = conn.execute(
        """
        SELECT old_scope_id, new_scope_id, work_generation, requested_at
        FROM relation_frequency_changes WHERE memory_id=?
        """,
        (clean_id,),
    ).fetchone()
    if change is None:
        return False
    current_generation = int(change[2] or 0)
    current_revision = str(change[3] or "")
    if work_generation is not None and current_generation != int(work_generation):
        return False
    if requested_at is not None and current_revision != str(requested_at):
        return False
    failure = conn.execute(
        """
        SELECT work_generation, work_revision, old_scope_id, new_scope_id,
               attempts, status, last_error, last_failed_at
        FROM relation_frequency_failures WHERE memory_id=?
        """,
        (clean_id,),
    ).fetchone()
    if failure is None or int(failure[0] or 0) == current_generation:
        return False
    if not _table_exists(conn, "relation_work_dispositions"):
        raise RuntimeError("relation disposition schema is required for supersession")
    document = {
        "memory_sha256": hashlib.sha256(clean_id.encode("utf-8")).hexdigest(),
        "prior_generation": int(failure[0] or 0),
        "next_generation": current_generation,
        "prior_revision": str(failure[1] or ""),
        "next_revision": current_revision,
        "prior_status": str(failure[5] or ""),
        "prior_attempts": int(failure[4] or 0),
        "prior_failed_at": str(failure[7] or ""),
        "prior_error_sha256": hashlib.sha256(
            str(failure[6] or "").encode("utf-8")
        ).hexdigest(),
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    operation_id = f"frequency-supersede-{fingerprint[:32]}"
    conn.execute(
        """
        INSERT INTO relation_work_dispositions(
            work_kind, work_key, work_revision, scope_id, prior_status,
            prior_updated_at, terminal_state, reason_code, attempts,
            lease_expirations, operation_id, request_fingerprint, disposed_at
        ) VALUES(
            'frequency_change', ?, ?, ?, ?, ?, 'superseded',
            'superseded_by_frequency_change_revision', ?, 0, ?, ?, ?
        )
        ON CONFLICT(work_kind, work_key, work_revision) DO NOTHING
        """,
        (
            clean_id,
            str(int(failure[0] or 0)),
            str(failure[3] or failure[2] or change[1] or change[0] or ""),
            str(failure[5] or ""),
            str(failure[7] or ""),
            max(0, int(failure[4] or 0)),
            operation_id,
            fingerprint,
            _now_iso(),
        ),
    )
    changed = conn.execute(
        """
        DELETE FROM relation_frequency_failures
        WHERE memory_id=? AND work_generation=? AND work_revision=? AND old_scope_id=?
          AND new_scope_id=? AND attempts=? AND status=? AND last_error=?
          AND last_failed_at=?
          AND EXISTS(
              SELECT 1 FROM relation_frequency_changes c
              WHERE c.memory_id=? AND c.work_generation=? AND c.requested_at=?
          )
        """,
        (
            clean_id,
            int(failure[0] or 0),
            str(failure[1] or ""),
            str(failure[2] or ""),
            str(failure[3] or ""),
            int(failure[4] or 0),
            str(failure[5] or ""),
            str(failure[6] or ""),
            str(failure[7] or ""),
            clean_id,
            current_generation,
            current_revision,
        ),
    ).rowcount
    if changed != 1:
        raise RuntimeError("relation frequency failure changed during supersession")
    return True


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


def _scope_has_failure_status(
    conn: sqlite3.Connection,
    scope_id: str,
    status: str,
) -> bool:
    return (
        conn.execute(
            """
            SELECT 1
            FROM relation_frequency_failures f
            LEFT JOIN relation_frequency_changes c ON c.memory_id=f.memory_id
            LEFT JOIN relation_indexed_memories i ON i.memory_id=f.memory_id
            LEFT JOIN memories m ON m.id=f.memory_id
            WHERE f.status=?
              AND (f.old_scope_id=? OR f.new_scope_id=?
                OR c.old_scope_id=? OR c.new_scope_id=?
                OR i.scope_id=? OR m.scope_id=?)
            LIMIT 1
            """,
            (str(status), *(str(scope_id),) * 6),
        ).fetchone()
        is not None
    )


def relation_frequency_scope_failure_status(
    conn: sqlite3.Connection,
    scope_id: str,
) -> str:
    """Return the strongest durable failure state affecting one scope."""

    scope = str(scope_id or "").strip()
    if not scope:
        return ""
    if _scope_has_failure_status(conn, scope, "dead_letter"):
        return "dead_letter"
    if _scope_has_failure_status(conn, scope, "retry"):
        return "retry"
    return ""


def _scope_has_failure_rows(conn: sqlite3.Connection, scope_id: str) -> bool:
    return bool(relation_frequency_scope_failure_status(conn, scope_id))


def _schedule_scope_reclassification(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    revision: int,
) -> None:
    """Retired compatibility hook; Program 0 containment owns policy deltas."""

    del conn, scope_id, revision


def refresh_relation_scope_frequency_receipt(conn: sqlite3.Connection, scope_id: str) -> dict[str, Any] | None:
    scope = str(scope_id or "")
    if not scope or not _scope_backfill_complete(conn, scope):
        return None
    if _scope_has_dirty_rows(conn, scope) or _scope_has_failure_rows(conn, scope):
        return None
    row = conn.execute(
        """
        SELECT corpus_revision, visible_memory_count, statistics_revision
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
    try:
        from .relation_containment import (
            ensure_relation_containment_schema,
            establish_relation_scope_baseline,
            record_relation_scope_target,
        )
    except ImportError:  # pragma: no cover - direct source-script fallback
        from relation_containment import (  # type: ignore[no-redef]
            ensure_relation_containment_schema,
            establish_relation_scope_baseline,
            record_relation_scope_target,
        )
    ensure_relation_containment_schema(conn)
    pristine_zero_baseline = conn.execute(
        """
        SELECT 1 FROM relation_scope_containment
        WHERE scope_id=? AND state='degraded'
          AND reason_code='frequency_receipt_stale'
          AND active_revision=0 AND target_revision=0
          AND attempts_total=0 AND target_attempts=0
        """,
        (scope,),
    ).fetchone()
    if pristine_zero_baseline is not None:
        establish_relation_scope_baseline(
            conn,
            scope_id=scope,
            revision=revision,
            blocked_entities=blocked,
        )
    else:
        record_relation_scope_target(
            conn,
            scope_id=scope,
            prior_statistics_revision=max(0, int(row[2] or 0)),
            target_revision=revision,
            old_blocked_entities=old_blocked or set(),
            new_blocked_entities=blocked,
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

    change = conn.execute(
        """
        SELECT work_generation, requested_at
        FROM relation_frequency_changes WHERE memory_id=?
        """,
        (clean_id,),
    ).fetchone()
    work_generation = int(change[0] or 0) if change is not None else 0
    work_revision = str(change[1] or "") if change is not None else ""
    if change is not None:
        supersede_relation_frequency_failure(
            conn,
            clean_id,
            work_generation=work_generation,
            requested_at=work_revision,
        )

    old = conn.execute(
        """
        SELECT scope_id, visible, entities_json, entities_sha256
        FROM relation_indexed_memories WHERE memory_id=?
        """,
        (clean_id,),
    ).fetchone()
    old_scope = str(old[0] or "") if old is not None else ""
    old_visible = bool(int(old[1] or 0)) if old is not None else False
    if old is None:
        old_entities: set[str] = set()
    else:
        decoded_old_entities = _decode_indexed_entities(old[2], old[3])
        if not old_visible and decoded_old_entities:
            raise RuntimeError("hidden relation frequency index row contains entities")
        old_entities = decoded_old_entities if old_visible else set()

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
    affected_scopes = sorted({scope for scope in (old_scope, new_scope) if scope})
    focus_queued = False
    if change is not None and affected_scopes:
        try:
            from .relation_containment import enqueue_relation_focus_work
        except ImportError:  # pragma: no cover - direct source-script fallback
            from relation_containment import enqueue_relation_focus_work
        focus_queued = enqueue_relation_focus_work(
            conn,
            memory_id=clean_id,
            work_generation=work_generation,
            work_revision=work_revision,
            scope_ids=affected_scopes,
        )
    if change is not None:
        deleted_change = conn.execute(
            """
            DELETE FROM relation_frequency_changes
            WHERE memory_id=? AND work_generation=? AND requested_at=?
            """,
            (clean_id, work_generation, work_revision),
        ).rowcount
        if deleted_change != 1:
            raise RuntimeError("relation frequency change advanced during sync")

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
        "work_generation": work_generation,
        "work_revision": work_revision,
        "focus_queued": focus_queued,
        "receipts": receipts,
    }


def relation_frequency_snapshot(
    conn: sqlite3.Connection,
    scope_id: str,
    *,
    bounded_repair_limit: int = 32,
) -> dict[str, Any] | None:
    """Return a strictly read-only receipt when one scope index is current."""

    del bounded_repair_limit  # compatibility only; repair is maintenance-owned
    scope = str(scope_id or "").strip()
    if not scope or not _scope_backfill_complete(conn, scope):
        return None
    if _scope_has_dirty_rows(conn, scope) or _scope_has_failure_rows(conn, scope):
        return None
    return load_scope_frequency_receipt(conn, scope)


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
    """Fail closed; callers must use the exact containment pair planner."""

    del conn, memory_id, scope_ids, blocked_entities, limit
    return [], True


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
    "ensure_relation_frequency_index_schema",
    "refresh_relation_scope_frequency_receipt",
    "relation_frequency_index_schema_status",
    "relation_frequency_scope_failure_status",
    "relation_frequency_snapshot",
    "relation_frequency_snapshots_by_scope",
    "supersede_relation_frequency_failure",
    "sync_relation_frequency_memory",
]
