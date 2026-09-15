"""Scope-level corpus revisions and reusable relation-frequency receipts.

The relation queue processes one focus memory over many peer chunks.  Relation
semantics must stay bound to one visible corpus while that event is in flight,
and expensive scope statistics must be shared by every event at that corpus
revision.  SQLite ``memories`` triggers own the revision counter; queue and graph
writes therefore cannot invalidate the cache they consume.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

try:
    from .sqlite_schema import execute_script_transaction_neutral
except ImportError:  # pragma: no cover - direct source-script execution fallback
    from sqlite_schema import execute_script_transaction_neutral

RELATION_SCOPE_RECEIPT_SCHEMA_VERSION = 10804
RELATION_SCOPE_RECEIPT_MIGRATION_ID = "0006_relation_scope_receipt_v1_8_0"
RELATION_SCOPE_RECEIPT_MIGRATION_PLUGIN_VERSION = "1.8.0"
RELATION_SCOPE_RECEIPT_MIGRATION_DESCRIPTION = (
    "Bind relation rebuild chunks to cached per-scope corpus statistics"
)

_SCOPE_TABLE = "relation_scope_statistics"
_SCOPE_REVISION_TRIGGERS = frozenset(
    {
        "trg_relation_scope_revision_insert",
        "trg_relation_scope_revision_update_same",
        "trg_relation_scope_revision_update_move",
        "trg_relation_scope_revision_update_timestamp",
        "trg_relation_scope_revision_cleanup_move",
        "trg_relation_scope_revision_delete",
    }
)
_REQUIRED_COLUMNS = {
    "scope_id",
    "corpus_revision",
    "statistics_revision",
    "visible_memory_count",
    "blocked_entities_json",
    "blocked_entities_sha256",
    "updated_at",
}


class ScopeCorpusChanged(RuntimeError):
    """Raised when a statistics computation loses its scope-revision CAS."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def ensure_relation_scope_state_schema(conn: sqlite3.Connection) -> None:
    """Create the scope revision/cache table and relation-relevant truth triggers.

    The triggers watch only columns that can change relation candidates or
    lifecycle visibility.  ``updated_at`` participates in supersession ordering
    and therefore invalidates the receipt; recall counters and queue/edge writes
    do not.
    """

    execute_script_transaction_neutral(
        conn,
        """
        CREATE TABLE IF NOT EXISTS relation_scope_statistics (
            scope_id TEXT PRIMARY KEY,
            corpus_revision INTEGER NOT NULL DEFAULT 0,
            statistics_revision INTEGER NOT NULL DEFAULT -1,
            visible_memory_count INTEGER NOT NULL DEFAULT 0,
            blocked_entities_json TEXT NOT NULL DEFAULT '[]',
            blocked_entities_sha256 TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_relation_scope_statistics_revision
            ON relation_scope_statistics(corpus_revision, statistics_revision);
        """,
    )
    if not _table_exists(conn, "memories"):
        return

    # Existing databases predate the triggers.  Revision 1 means "known corpus,
    # statistics not yet computed" and forces the first queue event to rebuild
    # the cache rather than trusting an empty default.
    conn.execute(
        """
        INSERT OR IGNORE INTO relation_scope_statistics(
            scope_id, corpus_revision, statistics_revision,
            visible_memory_count, blocked_entities_json,
            blocked_entities_sha256, updated_at
        )
        SELECT scope_id, 1, -1, 0, '[]', '', ?
        FROM memories
        GROUP BY scope_id
        """,
        (_now_iso(),),
    )
    for trigger in sorted(_SCOPE_REVISION_TRIGGERS):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    execute_script_transaction_neutral(
        conn,
        """
        CREATE TRIGGER trg_relation_scope_revision_insert
        AFTER INSERT ON memories
        BEGIN
            INSERT INTO relation_scope_statistics(
                scope_id, corpus_revision, statistics_revision,
                visible_memory_count, blocked_entities_json,
                blocked_entities_sha256, updated_at
            ) VALUES(
                NEW.scope_id, 1, -1, 0, '[]', '',
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
            ON CONFLICT(scope_id) DO UPDATE SET
                corpus_revision=relation_scope_statistics.corpus_revision + 1,
                updated_at=excluded.updated_at;
        END;

        CREATE TRIGGER trg_relation_scope_revision_update_same
        AFTER UPDATE OF scope_id, content, target, metadata ON memories
        WHEN OLD.scope_id = NEW.scope_id
         AND (
              OLD.content IS NOT NEW.content
           OR OLD.target IS NOT NEW.target
           OR COALESCE(json_extract(OLD.metadata, '$.entities'), '[]')
              IS NOT COALESCE(json_extract(NEW.metadata, '$.entities'), '[]')
           OR COALESCE(json_extract(OLD.metadata, '$.lifecycle'), 'active')
              IS NOT COALESCE(json_extract(NEW.metadata, '$.lifecycle'), 'active')
         )
        BEGIN
            INSERT INTO relation_scope_statistics(
                scope_id, corpus_revision, statistics_revision,
                visible_memory_count, blocked_entities_json,
                blocked_entities_sha256, updated_at
            ) VALUES(
                NEW.scope_id, 1, -1, 0, '[]', '',
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
            ON CONFLICT(scope_id) DO UPDATE SET
                corpus_revision=relation_scope_statistics.corpus_revision + 1,
                updated_at=excluded.updated_at;
        END;

        CREATE TRIGGER trg_relation_scope_revision_update_move
        AFTER UPDATE OF scope_id, content, target, metadata ON memories
        WHEN OLD.scope_id <> NEW.scope_id
        BEGIN
            INSERT INTO relation_scope_statistics(
                scope_id, corpus_revision, statistics_revision,
                visible_memory_count, blocked_entities_json,
                blocked_entities_sha256, updated_at
            ) VALUES(
                OLD.scope_id, 1, -1, 0, '[]', '',
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
            ON CONFLICT(scope_id) DO UPDATE SET
                corpus_revision=relation_scope_statistics.corpus_revision + 1,
                updated_at=excluded.updated_at;
            INSERT INTO relation_scope_statistics(
                scope_id, corpus_revision, statistics_revision,
                visible_memory_count, blocked_entities_json,
                blocked_entities_sha256, updated_at
            ) VALUES(
                NEW.scope_id, 1, -1, 0, '[]', '',
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
            ON CONFLICT(scope_id) DO UPDATE SET
                corpus_revision=relation_scope_statistics.corpus_revision + 1,
                updated_at=excluded.updated_at;
        END;

        CREATE TRIGGER trg_relation_scope_revision_update_timestamp
        AFTER UPDATE OF updated_at ON memories
        WHEN OLD.scope_id = NEW.scope_id
         AND OLD.updated_at IS NOT NEW.updated_at
         AND OLD.content IS NEW.content
         AND OLD.target IS NEW.target
         AND OLD.metadata IS NEW.metadata
        BEGIN
            INSERT INTO relation_scope_statistics(
                scope_id, corpus_revision, statistics_revision,
                visible_memory_count, blocked_entities_json,
                blocked_entities_sha256, updated_at
            ) VALUES(
                NEW.scope_id, 1, -1, 0, '[]', '',
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
            ON CONFLICT(scope_id) DO UPDATE SET
                corpus_revision=relation_scope_statistics.corpus_revision + 1,
                updated_at=excluded.updated_at;
        END;

        CREATE TRIGGER trg_relation_scope_revision_cleanup_move
        AFTER UPDATE OF scope_id ON memories
        WHEN OLD.scope_id <> NEW.scope_id
        BEGIN
            -- Relations are scope-owned companion state. Reviewed and
            -- deterministic edges alike must be re-evaluated after a move.
            DELETE FROM memory_relations
            WHERE source_memory_id = NEW.id OR target_memory_id = NEW.id;
        END;

        CREATE TRIGGER trg_relation_scope_revision_delete
        AFTER DELETE ON memories
        BEGIN
            INSERT INTO relation_scope_statistics(
                scope_id, corpus_revision, statistics_revision,
                visible_memory_count, blocked_entities_json,
                blocked_entities_sha256, updated_at
            ) VALUES(
                OLD.scope_id, 1, -1, 0, '[]', '',
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
            ON CONFLICT(scope_id) DO UPDATE SET
                corpus_revision=relation_scope_statistics.corpus_revision + 1,
                updated_at=excluded.updated_at;
        END;
        """,
    )


def relation_scope_state_schema_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return additive table/trigger status without mutating SQLite."""

    if not _table_exists(conn, _SCOPE_TABLE):
        return {
            "current": False,
            "table_present": False,
            "missing_columns": sorted(_REQUIRED_COLUMNS),
            "missing_triggers": sorted(_SCOPE_REVISION_TRIGGERS),
        }
    columns = {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({_SCOPE_TABLE})").fetchall()
    }
    triggers = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_relation_scope_revision_%'"
        ).fetchall()
    }
    missing_columns = sorted(_REQUIRED_COLUMNS - columns)
    missing_triggers = (
        sorted(_SCOPE_REVISION_TRIGGERS - triggers)
        if _table_exists(conn, "memories")
        else []
    )
    return {
        "current": not missing_columns and not missing_triggers,
        "table_present": True,
        "missing_columns": missing_columns,
        "missing_triggers": missing_triggers,
    }


def current_scope_corpus_revision(conn: sqlite3.Connection, scope_id: str) -> int:
    """Read the O(1) relation-relevant truth revision for one scope."""

    row = conn.execute(
        "SELECT corpus_revision FROM relation_scope_statistics WHERE scope_id=?",
        (str(scope_id),),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def blocked_entities_receipt_hash(
    scope_id: str,
    corpus_revision: int,
    blocked_entities: Iterable[str],
) -> str:
    """Hash the semantic inputs persisted in both scope cache and queue event."""

    payload = json.dumps(
        {
            "scope_id": str(scope_id),
            "corpus_revision": int(corpus_revision),
            "blocked_entities": sorted({str(item) for item in blocked_entities}),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def decode_blocked_entities_receipt(
    *,
    scope_id: str,
    corpus_revision: int,
    blocked_entities_json: str,
    blocked_entities_sha256: str,
) -> set[str] | None:
    """Decode one receipt only when its JSON shape and integrity hash agree."""

    try:
        parsed = json.loads(str(blocked_entities_json or "[]"))
    except Exception:
        return None
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        return None
    blocked = {str(item) for item in parsed if str(item)}
    expected = blocked_entities_receipt_hash(scope_id, corpus_revision, blocked)
    if not blocked_entities_sha256 or expected != str(blocked_entities_sha256):
        return None
    return blocked


def load_scope_frequency_receipt(
    conn: sqlite3.Connection,
    scope_id: str,
) -> dict[str, Any] | None:
    """Return a verified cache only when it matches the current corpus revision."""

    row = conn.execute(
        """
        SELECT corpus_revision, statistics_revision, visible_memory_count,
               blocked_entities_json, blocked_entities_sha256
        FROM relation_scope_statistics
        WHERE scope_id=?
        """,
        (str(scope_id),),
    ).fetchone()
    if row is None or int(row[0]) != int(row[1]):
        return None
    revision = int(row[0])
    blocked = decode_blocked_entities_receipt(
        scope_id=str(scope_id),
        corpus_revision=revision,
        blocked_entities_json=str(row[3] or "[]"),
        blocked_entities_sha256=str(row[4] or ""),
    )
    if blocked is None:
        return None
    return {
        "scope_id": str(scope_id),
        "corpus_revision": revision,
        "visible_memory_count": int(row[2] or 0),
        "blocked_entities": blocked,
        "blocked_entities_json": json.dumps(
            sorted(blocked), ensure_ascii=False, separators=(",", ":")
        ),
        "blocked_entities_sha256": str(row[4]),
        "cache_hit": True,
    }


def store_scope_frequency_receipt(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    expected_corpus_revision: int,
    visible_memory_count: int,
    blocked_entities: Iterable[str],
) -> dict[str, Any]:
    """Persist one cache with a corpus-revision CAS.

    The caller computes outside a truth write.  If any memory changed in that
    window, the trigger advances ``corpus_revision`` and this update affects no
    row, forcing the queue event to restart instead of mixing two corpora.
    """

    blocked = sorted({str(item) for item in blocked_entities if str(item)})
    blocked_json = json.dumps(blocked, ensure_ascii=False, separators=(",", ":"))
    digest = blocked_entities_receipt_hash(
        str(scope_id), int(expected_corpus_revision), blocked
    )
    changed = conn.execute(
        """
        UPDATE relation_scope_statistics
        SET statistics_revision=?, visible_memory_count=?,
            blocked_entities_json=?, blocked_entities_sha256=?, updated_at=?
        WHERE scope_id=? AND corpus_revision=?
        """,
        (
            int(expected_corpus_revision),
            max(0, int(visible_memory_count)),
            blocked_json,
            digest,
            _now_iso(),
            str(scope_id),
            int(expected_corpus_revision),
        ),
    ).rowcount
    if changed != 1:
        raise ScopeCorpusChanged(
            f"scope corpus revision changed while computing statistics: {scope_id}"
        )
    return {
        "scope_id": str(scope_id),
        "corpus_revision": int(expected_corpus_revision),
        "visible_memory_count": max(0, int(visible_memory_count)),
        "blocked_entities": set(blocked),
        "blocked_entities_json": blocked_json,
        "blocked_entities_sha256": digest,
        "cache_hit": False,
    }


__all__ = [
    "RELATION_SCOPE_RECEIPT_MIGRATION_DESCRIPTION",
    "RELATION_SCOPE_RECEIPT_MIGRATION_ID",
    "RELATION_SCOPE_RECEIPT_MIGRATION_PLUGIN_VERSION",
    "RELATION_SCOPE_RECEIPT_SCHEMA_VERSION",
    "ScopeCorpusChanged",
    "blocked_entities_receipt_hash",
    "current_scope_corpus_revision",
    "decode_blocked_entities_receipt",
    "ensure_relation_scope_state_schema",
    "load_scope_frequency_receipt",
    "relation_scope_state_schema_status",
    "store_scope_frequency_receipt",
]
