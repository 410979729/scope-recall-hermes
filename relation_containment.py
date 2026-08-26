"""Finite relation-policy containment for legacy Scope Recall relation debt.

Program 0 containment deliberately does not introduce the long-term immutable
relation-generation model.  It records whether generated relation evidence is
safe for one scope and applies only a complete, cap-bounded policy delta.  The
legacy full-scope rebuild queue remains inspectable for operator cleanup, but is
not a source of new or automatically executed work.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

try:
    from .relation_scope_state import blocked_entities_receipt_hash
    from .sqlite_recovery import is_sqlite_lock_contention
    from .sqlite_schema import execute_script_transaction_neutral
except ImportError:  # pragma: no cover - direct source-script fallback
    from relation_scope_state import blocked_entities_receipt_hash
    from sqlite_recovery import is_sqlite_lock_contention
    from sqlite_schema import execute_script_transaction_neutral


RELATION_CONTAINMENT_SCHEMA_VERSION = 10813
RELATION_CONTAINMENT_MIGRATION_ID = "0013_relation_containment_v1_10_6"
RELATION_CONTAINMENT_MIGRATION_PLUGIN_VERSION = "1.10.6"
RELATION_CONTAINMENT_MIGRATION_DESCRIPTION = (
    "Contain legacy relation fan-out with scope health and terminal dispositions"
)

RELATION_CONTAINMENT_STATES = frozenset(
    {"ready", "degraded", "blocked", "disabled"}
)
DEFAULT_RELATION_CANDIDATE_CAP = 250
DEFAULT_RELATION_MAX_ATTEMPTS = 5
DEFAULT_RELATION_BACKOFF_BASE_SECONDS = 5.0
DEFAULT_RELATION_BACKOFF_MAX_SECONDS = 300.0
_GENERATED_NOTE_PATTERN = "relation-extraction:%"
_MAX_PLANNER_ENTITY_TERMS = 256

_SCOPE_COLUMNS = {
    "scope_id",
    "state",
    "reason_code",
    "active_revision",
    "target_revision",
    "active_blocked_entities_json",
    "active_blocked_entities_sha256",
    "target_blocked_entities_json",
    "target_blocked_entities_sha256",
    "candidate_cap",
    "affected_count",
    "item_total",
    "completed_items",
    "attempts_total",
    "target_attempts",
    "max_attempts",
    "lock_contention_skips",
    "next_attempt_at",
    "progress_started_at",
    "last_attempt_at",
    "last_progress_at",
    "created_at",
    "updated_at",
}
_DISPOSITION_COLUMNS = {
    "work_kind",
    "work_key",
    "work_revision",
    "scope_id",
    "prior_status",
    "prior_updated_at",
    "terminal_state",
    "reason_code",
    "attempts",
    "lease_expirations",
    "operation_id",
    "request_fingerprint",
    "disposed_at",
}
_FOCUS_WORK_COLUMNS = {
    "memory_id",
    "work_generation",
    "work_revision",
    "scope_ids_json",
    "scope_ids_sha256",
    "status",
    "attempts",
    "max_attempts",
    "next_attempt_at",
    "last_error",
    "created_at",
    "updated_at",
}
_FOCUS_SCOPE_COLUMNS = {"memory_id", "work_generation", "scope_id"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (str(table),),
        ).fetchone()
        is not None
    )


def _canonical_entities(values: Iterable[str]) -> tuple[str, str, set[str]]:
    entities = sorted({str(item) for item in values if str(item)})
    encoded = json.dumps(entities, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return encoded, digest, set(entities)


def _record_superseded_containment_target(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    active_revision: int,
    target_revision: int,
    next_target_revision: int,
    prior_state: str,
    prior_reason: str,
    target_attempts: int,
    prior_updated_at: str,
    disposed_at: str,
) -> None:
    """Retain terminal evidence before a newer policy target replaces it."""

    document = {
        "scope_id": str(scope_id),
        "active_revision": int(active_revision),
        "target_revision": int(target_revision),
        "next_target_revision": int(next_target_revision),
        "prior_state": str(prior_state),
        "prior_reason": str(prior_reason),
        "target_attempts": max(0, int(target_attempts)),
        "prior_updated_at": str(prior_updated_at or ""),
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    conn.execute(
        """
        INSERT INTO relation_work_dispositions(
            work_kind, work_key, work_revision, scope_id, prior_status,
            prior_updated_at, terminal_state, reason_code, attempts,
            lease_expirations, operation_id, request_fingerprint, disposed_at
        ) VALUES(
            'containment_target', ?, ?, ?, ?, ?, 'superseded',
            'superseded_by_relation_policy_revision', ?, 0, ?, ?, ?
        )
        """,
        (
            str(scope_id),
            str(int(target_revision)),
            str(scope_id),
            str(prior_state),
            str(prior_updated_at or ""),
            max(0, int(target_attempts)),
            f"containment-supersede-{fingerprint[:32]}",
            fingerprint,
            str(disposed_at),
        ),
    )


def _decode_entities(value: Any) -> set[str] | None:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) or not item for item in parsed
    ):
        return None
    entities = {str(item) for item in parsed}
    if len(entities) != len(parsed):
        return None
    return entities


def ensure_relation_containment_schema(conn: sqlite3.Connection) -> None:
    """Create additive Program 0 containment state without reading content."""

    execute_script_transaction_neutral(
        conn,
        """
        CREATE TABLE IF NOT EXISTS relation_scope_containment (
            scope_id TEXT PRIMARY KEY,
            state TEXT NOT NULL
                CHECK(state IN ('ready','degraded','blocked','disabled')),
            reason_code TEXT NOT NULL DEFAULT '',
            active_revision INTEGER NOT NULL DEFAULT 0 CHECK(active_revision >= 0),
            target_revision INTEGER NOT NULL DEFAULT 0 CHECK(target_revision >= 0),
            active_blocked_entities_json TEXT NOT NULL DEFAULT '[]',
            active_blocked_entities_sha256 TEXT NOT NULL DEFAULT '',
            target_blocked_entities_json TEXT NOT NULL DEFAULT '[]',
            target_blocked_entities_sha256 TEXT NOT NULL DEFAULT '',
            candidate_cap INTEGER NOT NULL DEFAULT 250 CHECK(candidate_cap > 0),
            affected_count INTEGER NOT NULL DEFAULT 0 CHECK(affected_count >= 0),
            item_total INTEGER NOT NULL DEFAULT 0 CHECK(item_total >= 0),
            completed_items INTEGER NOT NULL DEFAULT 0
                CHECK(completed_items >= 0 AND completed_items <= item_total),
            attempts_total INTEGER NOT NULL DEFAULT 0 CHECK(attempts_total >= 0),
            target_attempts INTEGER NOT NULL DEFAULT 0 CHECK(target_attempts >= 0),
            max_attempts INTEGER NOT NULL DEFAULT 5 CHECK(max_attempts > 0),
            lock_contention_skips INTEGER NOT NULL DEFAULT 0
                CHECK(lock_contention_skips >= 0),
            next_attempt_at TEXT NOT NULL DEFAULT '',
            progress_started_at TEXT NOT NULL DEFAULT '',
            last_attempt_at TEXT NOT NULL DEFAULT '',
            last_progress_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(target_revision >= active_revision),
            CHECK(state <> 'ready' OR active_revision = target_revision)
        );
        CREATE INDEX IF NOT EXISTS idx_relation_scope_containment_due
            ON relation_scope_containment(state, next_attempt_at, updated_at);

        CREATE TABLE IF NOT EXISTS relation_work_dispositions (
            work_kind TEXT NOT NULL
                CHECK(work_kind IN (
                    'rebuild_queue','scope_reclassification','containment_target',
                    'frequency_change','focus_sync'
                )),
            work_key TEXT NOT NULL,
            work_revision TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            prior_status TEXT NOT NULL,
            prior_updated_at TEXT NOT NULL DEFAULT '',
            terminal_state TEXT NOT NULL
                CHECK(terminal_state IN (
                    'completed','poisoned','cancelled','superseded'
                )),
            reason_code TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
            lease_expirations INTEGER NOT NULL DEFAULT 0
                CHECK(lease_expirations >= 0),
            operation_id TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            disposed_at TEXT NOT NULL,
            PRIMARY KEY(work_kind, work_key, work_revision),
            UNIQUE(operation_id, work_kind, work_key, work_revision)
        );
        CREATE INDEX IF NOT EXISTS idx_relation_work_dispositions_scope
            ON relation_work_dispositions(scope_id, terminal_state, disposed_at);

        CREATE TABLE IF NOT EXISTS relation_focus_work (
            memory_id TEXT PRIMARY KEY,
            work_generation INTEGER NOT NULL CHECK(work_generation > 0),
            work_revision TEXT NOT NULL,
            scope_ids_json TEXT NOT NULL,
            scope_ids_sha256 TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','retry','dead_letter')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
            max_attempts INTEGER NOT NULL DEFAULT 5 CHECK(max_attempts > 0),
            next_attempt_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_relation_focus_work_due
            ON relation_focus_work(status, next_attempt_at, updated_at, memory_id);

        CREATE TABLE IF NOT EXISTS relation_focus_work_scopes (
            memory_id TEXT NOT NULL,
            work_generation INTEGER NOT NULL CHECK(work_generation > 0),
            scope_id TEXT NOT NULL,
            PRIMARY KEY(memory_id, work_generation, scope_id)
        );
        CREATE INDEX IF NOT EXISTS idx_relation_focus_work_scopes_scope
            ON relation_focus_work_scopes(scope_id, memory_id, work_generation);
        """,
    )

    if not _table_exists(conn, "relation_scope_statistics"):
        return
    now = _now_iso()
    missing = conn.execute(
        """
        SELECT s.scope_id, s.statistics_revision, s.corpus_revision,
               s.blocked_entities_json, s.blocked_entities_sha256
        FROM relation_scope_statistics s
        LEFT JOIN relation_scope_containment c ON c.scope_id=s.scope_id
        WHERE c.scope_id IS NULL
        ORDER BY s.scope_id
        """
    ).fetchall()
    for row in missing:
        scope = str(row[0])
        statistics_revision = int(row[1] or 0)
        corpus_revision = int(row[2] or 0)
        current = statistics_revision == corpus_revision
        legacy_generated = False
        if (
            current
            and _table_exists(conn, "memory_relations")
            and _table_exists(conn, "memories")
        ):
            legacy_generated = (
                conn.execute(
                    """
                    SELECT 1
                    FROM memory_relations r
                    JOIN memories s ON s.id=r.source_memory_id
                    JOIN memories t ON t.id=r.target_memory_id
                    WHERE s.scope_id=? AND t.scope_id=?
                      AND LOWER(COALESCE(r.note, '')) LIKE ?
                    LIMIT 1
                    """,
                    (scope, scope, _GENERATED_NOTE_PATTERN),
                ).fetchone()
                is not None
            )
        if legacy_generated:
            state = "blocked"
            reason = "legacy_generated_relations_unverified"
        elif current:
            state = "ready"
            reason = ""
        else:
            state = "degraded"
            reason = "frequency_receipt_stale"
        active_revision = corpus_revision if current else 0
        initial_target_revision = corpus_revision if current else 0
        active_json = str(row[3]) if current else "[]"
        active_sha = str(row[4]) if current else ""
        target_json = str(row[3]) if current else "[]"
        target_sha = str(row[4]) if current else ""
        conn.execute(
            """
            INSERT INTO relation_scope_containment(
                scope_id, state, reason_code, active_revision, target_revision,
                active_blocked_entities_json, active_blocked_entities_sha256,
                target_blocked_entities_json, target_blocked_entities_sha256,
                candidate_cap, affected_count, item_total, completed_items,
                attempts_total, target_attempts, max_attempts,
                lock_contention_skips, next_attempt_at, progress_started_at,
                last_attempt_at, last_progress_at, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 250, 0, 0, 0, 0, 0, 5, 0,
                     '', '', '', '', ?, ?)
            """,
            (
                scope,
                state,
                reason,
                active_revision,
                initial_target_revision,
                active_json,
                active_sha,
                target_json,
                target_sha,
                now,
                now,
            ),
        )


def relation_containment_schema_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Inspect the containment schema without writes."""

    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    scope_columns = (
        {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(relation_scope_containment)"
            ).fetchall()
        }
        if "relation_scope_containment" in tables
        else set()
    )
    disposition_columns = (
        {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(relation_work_dispositions)"
            ).fetchall()
        }
        if "relation_work_dispositions" in tables
        else set()
    )
    focus_work_columns = (
        {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(relation_focus_work)"
            ).fetchall()
        }
        if "relation_focus_work" in tables
        else set()
    )
    focus_scope_columns = (
        {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(relation_focus_work_scopes)"
            ).fetchall()
        }
        if "relation_focus_work_scopes" in tables
        else set()
    )
    missing_scope = sorted(_SCOPE_COLUMNS - scope_columns)
    missing_dispositions = sorted(_DISPOSITION_COLUMNS - disposition_columns)
    missing_focus_work = sorted(_FOCUS_WORK_COLUMNS - focus_work_columns)
    missing_focus_scopes = sorted(_FOCUS_SCOPE_COLUMNS - focus_scope_columns)
    return {
        "current": (
            not missing_scope
            and not missing_dispositions
            and not missing_focus_work
            and not missing_focus_scopes
        ),
        "schema_version": RELATION_CONTAINMENT_SCHEMA_VERSION,
        "scope_table_present": "relation_scope_containment" in tables,
        "disposition_table_present": "relation_work_dispositions" in tables,
        "focus_work_table_present": "relation_focus_work" in tables,
        "focus_scope_table_present": "relation_focus_work_scopes" in tables,
        "missing_scope_columns": missing_scope,
        "missing_disposition_columns": missing_dispositions,
        "missing_focus_work_columns": missing_focus_work,
        "missing_focus_scope_columns": missing_focus_scopes,
    }


def _canonical_scope_ids(values: Iterable[str]) -> tuple[str, str, list[str]]:
    scopes = sorted({str(item).strip() for item in values if str(item).strip()})
    encoded = json.dumps(scopes, ensure_ascii=False, separators=(",", ":"))
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), scopes


def _decode_scope_ids(encoded: Any, digest: Any) -> list[str] | None:
    raw = str(encoded)
    if not str(digest or "") or hashlib.sha256(raw.encode("utf-8")).hexdigest() != str(
        digest
    ):
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) or not item.strip() for item in parsed
    ):
        return None
    canonical, _canonical_digest, scopes = _canonical_scope_ids(parsed)
    if canonical != raw or len(scopes) != len(parsed):
        return None
    return scopes


def _focus_scope_rows(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    work_generation: int,
) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            """
            SELECT scope_id FROM relation_focus_work_scopes
            WHERE memory_id=? AND work_generation=? ORDER BY scope_id
            """,
            (str(memory_id), int(work_generation)),
        ).fetchall()
    ]


def _record_focus_work_disposition(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    work_generation: int,
    scopes: Iterable[str],
    prior_status: str,
    prior_updated_at: str,
    attempts: int,
    terminal_state: str,
    reason_code: str,
) -> None:
    encoded, digest, clean_scopes = _canonical_scope_ids(scopes)
    document = {
        "memory_sha256": hashlib.sha256(str(memory_id).encode("utf-8")).hexdigest(),
        "work_generation": int(work_generation),
        "scope_ids_sha256": digest,
        "prior_status": str(prior_status),
        "prior_updated_at": str(prior_updated_at or ""),
        "attempts": max(0, int(attempts)),
        "terminal_state": str(terminal_state),
        "reason_code": str(reason_code),
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    disposed_at = _now_iso()
    for scope in clean_scopes:
        scope_digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]
        conn.execute(
            """
            INSERT INTO relation_work_dispositions(
                work_kind, work_key, work_revision, scope_id, prior_status,
                prior_updated_at, terminal_state, reason_code, attempts,
                lease_expirations, operation_id, request_fingerprint, disposed_at
            ) VALUES('focus_sync', ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            ON CONFLICT(work_kind, work_key, work_revision) DO NOTHING
            """,
            (
                f"{memory_id}|{scope_digest}",
                str(int(work_generation)),
                scope,
                str(prior_status),
                str(prior_updated_at or ""),
                str(terminal_state),
                str(reason_code),
                max(0, int(attempts)),
                f"focus-{terminal_state}-{fingerprint[:24]}-{scope_digest}",
                fingerprint,
                disposed_at,
            ),
        )
    del encoded


def enqueue_relation_focus_work(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    work_generation: int,
    work_revision: str,
    scope_ids: Iterable[str],
    max_attempts: int = DEFAULT_RELATION_MAX_ATTEMPTS,
) -> bool:
    """Persist one bounded focus repair, superseding only an older exact item."""

    ensure_relation_containment_schema(conn)
    clean_id = str(memory_id or "").strip()
    generation = int(work_generation)
    revision = str(work_revision or "")
    if not clean_id or generation <= 0 or not revision:
        raise ValueError("focus work requires memory_id, generation, and revision")
    _incoming_json, _incoming_sha, incoming_scopes = _canonical_scope_ids(scope_ids)
    if not incoming_scopes:
        return False
    existing = conn.execute(
        """
        SELECT work_generation, work_revision, scope_ids_json, scope_ids_sha256,
               status, attempts, max_attempts, next_attempt_at, last_error,
               created_at, updated_at
        FROM relation_focus_work WHERE memory_id=?
        """,
        (clean_id,),
    ).fetchone()
    scopes = incoming_scopes
    if existing is not None:
        existing_generation = int(existing[0] or 0)
        existing_scopes = _decode_scope_ids(existing[2], existing[3])
        physical_scopes = _focus_scope_rows(
            conn,
            memory_id=clean_id,
            work_generation=existing_generation,
        )
        if existing_scopes is None or physical_scopes != existing_scopes:
            raise RuntimeError("relation focus work scope receipt mismatch")
        if existing_generation > generation:
            return False
        if existing_generation == generation:
            if str(existing[1] or "") != revision or existing_scopes != scopes:
                raise RuntimeError("relation focus work generation receipt mismatch")
            return True
        scopes = sorted(set(existing_scopes) | set(incoming_scopes))
        _record_focus_work_disposition(
            conn,
            memory_id=clean_id,
            work_generation=existing_generation,
            scopes=existing_scopes,
            prior_status=str(existing[4]),
            prior_updated_at=str(existing[10] or ""),
            attempts=int(existing[5] or 0),
            terminal_state="superseded",
            reason_code="superseded_by_focus_work_generation",
        )
        changed = conn.execute(
            """
            DELETE FROM relation_focus_work_scopes
            WHERE memory_id=? AND work_generation=?
            """,
            (clean_id, existing_generation),
        ).rowcount
        if changed != len(existing_scopes):
            raise RuntimeError("relation focus work scopes changed during supersession")
    encoded, digest, scopes = _canonical_scope_ids(scopes)
    now = _now_iso()
    attempts_limit = max(1, min(int(max_attempts), 20))
    if existing is None:
        conn.execute(
            """
            INSERT INTO relation_focus_work(
                memory_id, work_generation, work_revision, scope_ids_json,
                scope_ids_sha256, status, attempts, max_attempts,
                next_attempt_at, last_error, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, 'pending', 0, ?, '', '', ?, ?)
            """,
            (
                clean_id,
                generation,
                revision,
                encoded,
                digest,
                attempts_limit,
                now,
                now,
            ),
        )
    else:
        changed = conn.execute(
            """
            UPDATE relation_focus_work
            SET work_generation=?, work_revision=?, scope_ids_json=?,
                scope_ids_sha256=?, status='pending', attempts=0,
                max_attempts=?, next_attempt_at='', last_error='', updated_at=?
            WHERE memory_id=? AND work_generation=? AND work_revision=?
              AND scope_ids_json=? AND scope_ids_sha256=? AND status=?
              AND attempts=? AND max_attempts=? AND next_attempt_at=?
              AND last_error=? AND created_at=? AND updated_at=?
            """,
            (
                generation,
                revision,
                encoded,
                digest,
                attempts_limit,
                now,
                clean_id,
                int(existing[0] or 0),
                str(existing[1] or ""),
                str(existing[2]),
                str(existing[3] or ""),
                str(existing[4]),
                int(existing[5] or 0),
                int(existing[6] or 0),
                str(existing[7] or ""),
                str(existing[8] or ""),
                str(existing[9] or ""),
                str(existing[10] or ""),
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("relation focus work changed during supersession")
    # Connection adapters used by lifecycle failure/rollback guards expose the
    # DB-API execute surface but are not required to proxy ``executemany``.
    # The scope set is already finite and receipt-bound, so keep this compatible
    # without changing transactional semantics.
    for scope in scopes:
        conn.execute(
            """
            INSERT INTO relation_focus_work_scopes(
                memory_id, work_generation, scope_id
            ) VALUES(?, ?, ?)
            """,
            (clean_id, generation, scope),
        )
    return True


def load_relation_focus_work(
    conn: sqlite3.Connection,
    memory_id: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT work_generation, work_revision, scope_ids_json, scope_ids_sha256,
               status, attempts, max_attempts, next_attempt_at, last_error,
               created_at, updated_at
        FROM relation_focus_work WHERE memory_id=?
        """,
        (str(memory_id),),
    ).fetchone()
    if row is None:
        return None
    scopes = _decode_scope_ids(row[2], row[3])
    if scopes is None or scopes != _focus_scope_rows(
        conn,
        memory_id=str(memory_id),
        work_generation=int(row[0] or 0),
    ):
        raise RuntimeError("relation focus work scope receipt mismatch")
    return {
        "memory_id": str(memory_id),
        "work_generation": int(row[0] or 0),
        "work_revision": str(row[1] or ""),
        "scope_ids": scopes,
        "status": str(row[4]),
        "attempts": int(row[5] or 0),
        "max_attempts": int(row[6] or 0),
        "next_attempt_at": str(row[7] or ""),
        "last_error": str(row[8] or ""),
        "created_at": str(row[9] or ""),
        "updated_at": str(row[10] or ""),
    }


def complete_relation_focus_work(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    work_generation: int,
) -> bool:
    work = load_relation_focus_work(conn, memory_id)
    if work is None or int(work["work_generation"]) != int(work_generation):
        return False
    _record_focus_work_disposition(
        conn,
        memory_id=str(memory_id),
        work_generation=int(work_generation),
        scopes=work["scope_ids"],
        prior_status=str(work["status"]),
        prior_updated_at=str(work["updated_at"]),
        attempts=int(work["attempts"]),
        terminal_state="completed",
        reason_code="focus_relation_sync_completed",
    )
    deleted_scopes = conn.execute(
        """
        DELETE FROM relation_focus_work_scopes
        WHERE memory_id=? AND work_generation=?
        """,
        (str(memory_id), int(work_generation)),
    ).rowcount
    if deleted_scopes != len(work["scope_ids"]):
        raise RuntimeError("relation focus work scopes changed during completion")
    deleted = conn.execute(
        """
        DELETE FROM relation_focus_work
        WHERE memory_id=? AND work_generation=? AND work_revision=?
          AND status=? AND attempts=? AND max_attempts=?
          AND next_attempt_at=? AND last_error=? AND created_at=? AND updated_at=?
        """,
        (
            str(memory_id),
            int(work_generation),
            str(work["work_revision"]),
            str(work["status"]),
            int(work["attempts"]),
            int(work["max_attempts"]),
            str(work["next_attempt_at"]),
            str(work["last_error"]),
            str(work["created_at"]),
            str(work["updated_at"]),
        ),
    ).rowcount
    if deleted != 1:
        raise RuntimeError("relation focus work changed during completion")
    return True


def defer_relation_focus_work(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    work_generation: int,
    delay_seconds: float,
    reason_code: str,
) -> bool:
    work = load_relation_focus_work(conn, memory_id)
    if work is None or int(work["work_generation"]) != int(work_generation):
        return False
    next_attempt = (_now() + timedelta(seconds=max(0.0, float(delay_seconds)))).isoformat()
    changed = conn.execute(
        """
        UPDATE relation_focus_work
        SET status='retry', next_attempt_at=?, last_error=?, updated_at=?
        WHERE memory_id=? AND work_generation=? AND work_revision=?
          AND status=? AND attempts=? AND max_attempts=?
          AND next_attempt_at=? AND last_error=? AND created_at=? AND updated_at=?
        """,
        (
            next_attempt,
            str(reason_code)[:120],
            _now_iso(),
            str(memory_id),
            int(work_generation),
            str(work["work_revision"]),
            str(work["status"]),
            int(work["attempts"]),
            int(work["max_attempts"]),
            str(work["next_attempt_at"]),
            str(work["last_error"]),
            str(work["created_at"]),
            str(work["updated_at"]),
        ),
    ).rowcount
    return changed == 1


def record_relation_focus_failure(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    work_generation: int,
    error_class: str,
    reason_code: str,
    permanent: bool = False,
    backoff_base_seconds: float = DEFAULT_RELATION_BACKOFF_BASE_SECONDS,
    backoff_max_seconds: float = DEFAULT_RELATION_BACKOFF_MAX_SECONDS,
) -> str:
    work = load_relation_focus_work(conn, memory_id)
    if work is None or int(work["work_generation"]) != int(work_generation):
        return "superseded"
    attempt = int(work["attempts"]) + 1
    exhausted = bool(permanent or attempt >= int(work["max_attempts"]))
    status = "dead_letter" if exhausted else "retry"
    delay = _retry_delay_seconds(
        attempt,
        base_seconds=backoff_base_seconds,
        max_seconds=backoff_max_seconds,
    )
    next_attempt = "" if exhausted else (_now() + timedelta(seconds=delay)).isoformat()
    last_error = f"{str(error_class)[:80]}:{str(reason_code)[:120]}"
    now = _now_iso()
    changed = conn.execute(
        """
        UPDATE relation_focus_work
        SET status=?, attempts=?, next_attempt_at=?, last_error=?, updated_at=?
        WHERE memory_id=? AND work_generation=? AND work_revision=?
          AND status=? AND attempts=? AND max_attempts=?
          AND next_attempt_at=? AND last_error=? AND created_at=? AND updated_at=?
        """,
        (
            status,
            attempt,
            next_attempt,
            last_error,
            now,
            str(memory_id),
            int(work_generation),
            str(work["work_revision"]),
            str(work["status"]),
            int(work["attempts"]),
            int(work["max_attempts"]),
            str(work["next_attempt_at"]),
            str(work["last_error"]),
            str(work["created_at"]),
            str(work["updated_at"]),
        ),
    ).rowcount
    if changed != 1:
        return "superseded"
    if exhausted:
        _record_focus_work_disposition(
            conn,
            memory_id=str(memory_id),
            work_generation=int(work_generation),
            scopes=work["scope_ids"],
            prior_status=status,
            prior_updated_at=now,
            attempts=attempt,
            terminal_state="poisoned",
            reason_code=str(reason_code),
        )
    return status


def relation_focus_scope_has_debt(conn: sqlite3.Connection, scope_id: str) -> bool:
    return (
        conn.execute(
            """
            SELECT 1 FROM relation_focus_work_scopes s
            JOIN relation_focus_work w
              ON w.memory_id=s.memory_id
             AND w.work_generation=s.work_generation
            WHERE s.scope_id=? LIMIT 1
            """,
            (str(scope_id),),
        ).fetchone()
        is not None
    )


@dataclass(frozen=True)
class RelationContainmentPlan:
    """Complete bounded pair plan, or a cap+1 sentinel with no partial pairs."""

    scope_id: str
    target_revision: int
    candidate_cap: int
    affected_count: int
    pairs: tuple[tuple[str, str], ...]
    blocked: bool
    reason_code: str


def _add_pair(
    pairs: set[tuple[str, str]],
    left: str,
    right: str,
    *,
    cap: int,
) -> bool:
    left_id = str(left or "")
    right_id = str(right or "")
    if not left_id or not right_id or left_id == right_id:
        return False
    pair = (left_id, right_id) if left_id < right_id else (right_id, left_id)
    pairs.add(pair)
    return len(pairs) > cap


def _postings_for_entity(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    entity: str,
    limit: int,
) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            """
            SELECT memory_id
            FROM relation_entity_postings
            WHERE scope_id=? AND entity=?
            ORDER BY memory_id
            LIMIT ?
            """,
            (scope_id, entity, max(1, int(limit))),
        ).fetchall()
    ]


def plan_relation_scope_delta(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    target_revision: int,
    old_blocked_entities: Iterable[str],
    new_blocked_entities: Iterable[str],
    candidate_cap: int = DEFAULT_RELATION_CANDIDATE_CAP,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> RelationContainmentPlan:
    """Build a complete pair set from delta postings and generated neighbors.

    The collector retains at most ``cap + 1`` canonical pairs.  Observing the
    sentinel discards the partial set so callers cannot accidentally apply it.
    No query enumerates the scope or falls back to all pairs.
    """

    clean_scope = str(scope_id or "").strip()
    if not clean_scope:
        raise ValueError("scope_id is required")
    cap = max(1, min(int(candidate_cap), 5000))
    changed_entities = sorted(
        {str(item) for item in old_blocked_entities if str(item)}
        ^ {str(item) for item in new_blocked_entities if str(item)}
    )
    if len(changed_entities) > _MAX_PLANNER_ENTITY_TERMS:
        return RelationContainmentPlan(
            clean_scope,
            int(target_revision),
            cap,
            cap + 1,
            (),
            True,
            "affected_candidate_cap_exceeded",
        )
    pairs: set[tuple[str, str]] = set()
    affected_memories: set[str] = set()

    for entity in changed_entities:
        if deadline_monotonic is not None and clock() >= deadline_monotonic:
            raise TimeoutError("relation containment planning budget exceeded")
        ids = _postings_for_entity(
            conn,
            scope_id=clean_scope,
            entity=entity,
            limit=cap + 2,
        )
        affected_memories.update(ids[: cap + 1])
        if len(affected_memories) > cap:
            return RelationContainmentPlan(
                clean_scope,
                int(target_revision),
                cap,
                cap + 1,
                (),
                True,
                "affected_candidate_cap_exceeded",
            )
        for index, left in enumerate(ids):
            if deadline_monotonic is not None and clock() >= deadline_monotonic:
                raise TimeoutError("relation containment planning budget exceeded")
            for right in ids[index + 1 :]:
                if _add_pair(pairs, left, right, cap=cap):
                    return RelationContainmentPlan(
                        clean_scope,
                        int(target_revision),
                        cap,
                        cap + 1,
                        (),
                        True,
                        "affected_candidate_cap_exceeded",
                    )

    if affected_memories and _table_exists(conn, "memory_relations"):
        if deadline_monotonic is not None and clock() >= deadline_monotonic:
            raise TimeoutError("relation containment planning budget exceeded")
        seed_ids = sorted(affected_memories)
        placeholders = ",".join("?" for _ in seed_ids)
        rows = conn.execute(
            f"""
            SELECT DISTINCT
                   CASE WHEN r.source_memory_id < r.target_memory_id
                        THEN r.source_memory_id ELSE r.target_memory_id END AS left_id,
                   CASE WHEN r.source_memory_id < r.target_memory_id
                        THEN r.target_memory_id ELSE r.source_memory_id END AS right_id
            FROM memory_relations r
            JOIN memories s ON s.id=r.source_memory_id
            JOIN memories t ON t.id=r.target_memory_id
            WHERE s.scope_id=? AND t.scope_id=?
              AND LOWER(COALESCE(r.note, '')) LIKE ?
              AND r.source_memory_id<>r.target_memory_id
              AND (r.source_memory_id IN ({placeholders})
                   OR r.target_memory_id IN ({placeholders}))
            ORDER BY left_id, right_id
            LIMIT ?
            """,
            (
                clean_scope,
                clean_scope,
                _GENERATED_NOTE_PATTERN,
                *seed_ids,
                *seed_ids,
                cap + 1,
            ),
        ).fetchall()
        for row in rows:
            if _add_pair(pairs, str(row[0]), str(row[1]), cap=cap):
                return RelationContainmentPlan(
                    clean_scope,
                    int(target_revision),
                    cap,
                    cap + 1,
                    (),
                    True,
                    "affected_candidate_cap_exceeded",
                )

    ordered = tuple(sorted(pairs))
    return RelationContainmentPlan(
        clean_scope,
        int(target_revision),
        cap,
        len(ordered),
        ordered,
        False,
        "",
    )


def plan_focus_relation_pairs(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    memory_id: str,
    blocked_entities: Iterable[str],
    candidate_cap: int,
    target_revision: int = 0,
) -> RelationContainmentPlan:
    """Plan one focus memory from postings and generated neighbors only."""

    scope = str(scope_id or "").strip()
    focus = str(memory_id or "").strip()
    if not scope or not focus:
        raise ValueError("scope_id and memory_id are required")
    cap = max(1, min(int(candidate_cap), 5000))
    blocked = {str(item) for item in blocked_entities if str(item)}
    entity_rows = conn.execute(
        """
        SELECT entity FROM relation_entity_postings
        WHERE scope_id=? AND memory_id=?
        ORDER BY entity
        LIMIT ?
        """,
        (scope, focus, _MAX_PLANNER_ENTITY_TERMS + 1),
    ).fetchall()
    if len(entity_rows) > _MAX_PLANNER_ENTITY_TERMS:
        return RelationContainmentPlan(
            scope,
            int(target_revision),
            cap,
            cap + 1,
            (),
            True,
            "affected_candidate_cap_exceeded",
        )
    entities = [str(row[0]) for row in entity_rows if str(row[0]) not in blocked]
    peers: set[str] = set()
    if entities:
        placeholders = ",".join("?" for _ in entities)
        rows = conn.execute(
            f"""
            SELECT DISTINCT memory_id
            FROM relation_entity_postings
            WHERE scope_id=? AND entity IN ({placeholders}) AND memory_id<>?
            ORDER BY memory_id
            LIMIT ?
            """,
            (scope, *entities, focus, cap + 1),
        ).fetchall()
        peers.update(str(row[0]) for row in rows)
    if len(peers) <= cap and _table_exists(conn, "memory_relations"):
        rows = conn.execute(
            """
            SELECT DISTINCT CASE WHEN r.source_memory_id=?
                                 THEN r.target_memory_id
                                 ELSE r.source_memory_id END AS peer_id
            FROM memory_relations r
            WHERE LOWER(COALESCE(r.note, '')) LIKE ?
              AND (r.source_memory_id=? OR r.target_memory_id=?)
              AND r.source_memory_id<>r.target_memory_id
            ORDER BY peer_id
            LIMIT ?
            """,
            (
                focus,
                _GENERATED_NOTE_PATTERN,
                focus,
                focus,
                cap + 1,
            ),
        ).fetchall()
        peers.update(str(row[0]) for row in rows if str(row[0]) != focus)
    if len(peers) > cap:
        return RelationContainmentPlan(
            scope,
            int(target_revision),
            cap,
            cap + 1,
            (),
            True,
            "affected_candidate_cap_exceeded",
        )
    pairs = tuple(
        sorted(
            (focus, peer) if focus < peer else (peer, focus)
            for peer in peers
            if peer != focus
        )
    )
    return RelationContainmentPlan(
        scope,
        int(target_revision),
        cap,
        len(pairs),
        pairs,
        False,
        "",
    )


def record_relation_scope_target(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    prior_statistics_revision: int,
    target_revision: int,
    old_blocked_entities: Iterable[str],
    new_blocked_entities: Iterable[str],
    candidate_cap: int = DEFAULT_RELATION_CANDIDATE_CAP,
    max_attempts: int = DEFAULT_RELATION_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Atomically mark a new policy target untrusted before applying its delta."""

    ensure_relation_containment_schema(conn)
    scope = str(scope_id or "").strip()
    if not scope:
        raise ValueError("scope_id is required")
    old_json, _old_json_sha, old_set = _canonical_entities(old_blocked_entities)
    new_json, _new_json_sha, new_set = _canonical_entities(new_blocked_entities)
    old_sha = blocked_entities_receipt_hash(
        scope, max(0, int(prior_statistics_revision)), old_set
    )
    target_sha = blocked_entities_receipt_hash(scope, int(target_revision), new_set)
    now = _now_iso()
    cap = max(1, min(int(candidate_cap), 5000))
    attempts = max(1, min(int(max_attempts), 20))
    existing = conn.execute(
        """
        SELECT target_revision, target_blocked_entities_sha256, state,
               reason_code, target_attempts, active_revision,
               active_blocked_entities_json,
               active_blocked_entities_sha256, updated_at
        FROM relation_scope_containment WHERE scope_id=?
        """,
        (scope,),
    ).fetchone()
    if existing is not None and (
        str(existing[2]) == "disabled"
        or str(existing[3]) == "legacy_generated_relations_unverified"
    ):
        return {
            "changed": False,
            "scope_id": scope,
            "state": str(existing[2]),
            "reason_code": str(existing[3]),
            "target_revision": int(existing[0] or 0),
            "target_attempts": int(existing[4] or 0),
        }
    if (
        existing is not None
        and int(existing[0]) == int(target_revision)
        and str(existing[1]) == target_sha
    ):
        return {
            "changed": False,
            "scope_id": scope,
            "state": str(existing[2]),
            "target_revision": int(existing[0]),
            "target_attempts": int(existing[4] or 0),
        }
    if existing is not None and int(target_revision) <= int(existing[0] or 0):
        raise ValueError("relation containment target revision must increase")
    if existing is not None:
        _record_superseded_containment_target(
            conn,
            scope_id=scope,
            active_revision=int(existing[5] or 0),
            target_revision=int(existing[0] or 0),
            next_target_revision=int(target_revision),
            prior_state=str(existing[2]),
            prior_reason=str(existing[3] or ""),
            target_attempts=int(existing[4] or 0),
            prior_updated_at=str(existing[8] or ""),
            disposed_at=now,
        )
    active_revision = (
        int(existing[5] or 0)
        if existing is not None
        else max(0, int(prior_statistics_revision))
    )
    active_json = str(existing[6] or "[]") if existing is not None else old_json
    active_sha = str(existing[7] or "") if existing is not None else old_sha
    active_set = _decode_entities(active_json)
    if active_set is None or active_sha != blocked_entities_receipt_hash(
        scope, active_revision, active_set
    ):
        raise RuntimeError("relation containment active receipt mismatch")
    target_reason = (
        "relation_policy_revision_pending"
        if active_set != new_set
        else "focus_relation_sync_pending"
    )
    conn.execute(
        """
        INSERT INTO relation_scope_containment(
            scope_id, state, reason_code, active_revision, target_revision,
            active_blocked_entities_json, active_blocked_entities_sha256,
            target_blocked_entities_json, target_blocked_entities_sha256,
            candidate_cap, affected_count, item_total, completed_items,
            attempts_total, target_attempts, max_attempts,
            lock_contention_skips, next_attempt_at, progress_started_at,
            last_attempt_at, last_progress_at, created_at, updated_at
        ) VALUES(
            ?, 'degraded', ?, ?, ?,
            ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, ?, 0, '', ?, '', '', ?, ?
        )
        ON CONFLICT(scope_id) DO UPDATE SET
            state='degraded', reason_code=excluded.reason_code,
            target_revision=excluded.target_revision,
            target_blocked_entities_json=excluded.target_blocked_entities_json,
            target_blocked_entities_sha256=excluded.target_blocked_entities_sha256,
            candidate_cap=excluded.candidate_cap,
            affected_count=0, item_total=0, completed_items=0,
            target_attempts=0, max_attempts=excluded.max_attempts,
            next_attempt_at='', progress_started_at=excluded.progress_started_at,
            last_attempt_at='', updated_at=excluded.updated_at
        """,
        (
            scope,
            target_reason,
            active_revision,
            max(0, int(target_revision)),
            active_json,
            active_sha,
            new_json,
            target_sha,
            cap,
            attempts,
            now,
            now,
            now,
        ),
    )
    return {
        "changed": True,
        "scope_id": scope,
        "state": "degraded",
        "reason_code": target_reason,
        "target_revision": int(target_revision),
        "target_attempts": 0,
    }


def establish_relation_scope_baseline(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    revision: int,
    blocked_entities: Iterable[str],
) -> dict[str, Any]:
    """Accept a fresh receipt only when no unverified generated edge exists."""

    ensure_relation_containment_schema(conn)
    scope = str(scope_id or "").strip()
    if not scope:
        raise ValueError("scope_id is required")
    encoded, _json_sha, entities = _canonical_entities(blocked_entities)
    receipt_sha = blocked_entities_receipt_hash(scope, int(revision), entities)
    legacy_generated = False
    if _table_exists(conn, "memory_relations") and _table_exists(conn, "memories"):
        legacy_generated = (
            conn.execute(
                """
                SELECT 1
                FROM memory_relations r
                JOIN memories s ON s.id=r.source_memory_id
                JOIN memories t ON t.id=r.target_memory_id
                WHERE s.scope_id=? AND t.scope_id=?
                  AND LOWER(COALESCE(r.note, '')) LIKE ?
                LIMIT 1
                """,
                (scope, scope, _GENERATED_NOTE_PATTERN),
            ).fetchone()
            is not None
        )
    now = _now_iso()
    state = "blocked" if legacy_generated else "ready"
    reason = "legacy_generated_relations_unverified" if legacy_generated else ""
    conn.execute(
        """
        INSERT INTO relation_scope_containment(
            scope_id, state, reason_code, active_revision, target_revision,
            active_blocked_entities_json, active_blocked_entities_sha256,
            target_blocked_entities_json, target_blocked_entities_sha256,
            candidate_cap, affected_count, item_total, completed_items,
            attempts_total, target_attempts, max_attempts,
            lock_contention_skips, next_attempt_at, progress_started_at,
            last_attempt_at, last_progress_at, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 250, 0, 0, 0, 0, 0, 5, 0,
                 '', '', '', '', ?, ?)
        ON CONFLICT(scope_id) DO UPDATE SET
            state=excluded.state, reason_code=excluded.reason_code,
            active_revision=excluded.active_revision,
            target_revision=excluded.target_revision,
            active_blocked_entities_json=excluded.active_blocked_entities_json,
            active_blocked_entities_sha256=excluded.active_blocked_entities_sha256,
            target_blocked_entities_json=excluded.target_blocked_entities_json,
            target_blocked_entities_sha256=excluded.target_blocked_entities_sha256,
            affected_count=0, item_total=0, completed_items=0,
            target_attempts=0, next_attempt_at='', updated_at=excluded.updated_at
        WHERE relation_scope_containment.state='degraded'
          AND relation_scope_containment.reason_code='frequency_receipt_stale'
          AND relation_scope_containment.active_revision=0
          AND relation_scope_containment.target_revision=0
          AND relation_scope_containment.attempts_total=0
          AND relation_scope_containment.target_attempts=0
        """,
        (
            scope,
            state,
            reason,
            int(revision),
            int(revision),
            encoded,
            receipt_sha,
            encoded,
            receipt_sha,
            now,
            now,
        ),
    )
    return {
        "scope_id": scope,
        "state": state,
        "reason_code": reason,
        "active_revision": int(revision),
        "target_revision": int(revision),
    }


def stage_relation_scope_focus_generation(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    revision: int,
    blocked_entities: Iterable[str],
) -> bool:
    """Create an immutable same-policy generation pending exact focus sync."""

    scope = str(scope_id or "").strip()
    if not scope or not _table_exists(conn, "relation_scope_containment"):
        return False
    encoded, _json_sha, entities = _canonical_entities(blocked_entities)
    receipt_sha = blocked_entities_receipt_hash(scope, int(revision), entities)
    row = conn.execute(
        """
        SELECT state, reason_code, active_revision, target_revision,
               active_blocked_entities_json, active_blocked_entities_sha256,
               target_blocked_entities_json, target_blocked_entities_sha256,
               target_attempts, updated_at
        FROM relation_scope_containment WHERE scope_id=?
        """,
        (scope,),
    ).fetchone()
    if row is None:
        return False
    state = str(row[0])
    reason = str(row[1] or "")
    active_revision = int(row[2] or 0)
    target_revision = int(row[3] or 0)
    if (
        state == "degraded"
        and reason == "focus_relation_sync_pending"
        and target_revision == int(revision)
        and str(row[6]) == encoded
        and str(row[7] or "") == receipt_sha
    ):
        return True
    if (
        state not in {"ready", "degraded"}
        or reason not in {"", "frequency_receipt_stale", "frequency_change_pending"}
        or active_revision != target_revision
        or int(revision) <= target_revision
    ):
        return False
    active_entities = _decode_entities(row[4])
    target_entities = _decode_entities(row[6])
    if not (
        active_entities is not None
        and target_entities is not None
        and active_entities == target_entities == entities
        and str(row[5] or "")
        == blocked_entities_receipt_hash(scope, active_revision, active_entities)
        and str(row[7] or "")
        == blocked_entities_receipt_hash(scope, target_revision, target_entities)
    ):
        return False
    if not conn.in_transaction:
        conn.execute("BEGIN")
    savepoint = "relation_focus_generation_stage"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        now = _now_iso()
        _record_superseded_containment_target(
            conn,
            scope_id=scope,
            active_revision=active_revision,
            target_revision=target_revision,
            next_target_revision=int(revision),
            prior_state=state,
            prior_reason=reason,
            target_attempts=int(row[8] or 0),
            prior_updated_at=str(row[9] or ""),
            disposed_at=now,
        )
        changed = conn.execute(
            """
            UPDATE relation_scope_containment
            SET state='degraded', reason_code='focus_relation_sync_pending',
                target_revision=?, target_blocked_entities_json=?,
                target_blocked_entities_sha256=?, affected_count=0,
                item_total=0, completed_items=0, target_attempts=0,
                next_attempt_at='', progress_started_at=?, last_attempt_at='',
                updated_at=?
            WHERE scope_id=? AND state=? AND reason_code=?
              AND active_revision=? AND target_revision=?
              AND active_blocked_entities_json=?
              AND active_blocked_entities_sha256=?
              AND target_blocked_entities_json=?
              AND target_blocked_entities_sha256=? AND updated_at=?
            """,
            (
                int(revision),
                encoded,
                receipt_sha,
                now,
                now,
                scope,
                state,
                reason,
                active_revision,
                target_revision,
                str(row[4]),
                str(row[5] or ""),
                str(row[6]),
                str(row[7] or ""),
                str(row[9] or ""),
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("relation focus generation changed while staging")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return False
    return True


def reconcile_relation_scope_frequency_current(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    revision: int,
    blocked_entities: Iterable[str],
) -> bool:
    """Compatibility name for staging, never an automatic ready transition."""

    return stage_relation_scope_focus_generation(
        conn,
        scope_id=scope_id,
        revision=revision,
        blocked_entities=blocked_entities,
    )


def confirm_relation_scope_focus_generation(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    revision: int,
    blocked_entities: Iterable[str],
    affected_count: int,
) -> bool:
    """CAS a staged generation ready only after its bounded focus sync succeeds."""

    scope = str(scope_id or "").strip()
    if not scope:
        return False
    encoded, _json_sha, entities = _canonical_entities(blocked_entities)
    receipt_sha = blocked_entities_receipt_hash(scope, int(revision), entities)
    row = conn.execute(
        """
        SELECT state, reason_code, active_revision, target_revision,
               active_blocked_entities_json, active_blocked_entities_sha256,
               target_blocked_entities_json, target_blocked_entities_sha256,
               updated_at
        FROM relation_scope_containment WHERE scope_id=?
        """,
        (scope,),
    ).fetchone()
    if row is None:
        return False
    if (
        str(row[0]) == "ready"
        and int(row[2] or 0) == int(row[3] or 0) == int(revision)
        and str(row[4]) == str(row[6]) == encoded
        and str(row[5] or "") == str(row[7] or "") == receipt_sha
    ):
        return True
    active_entities = _decode_entities(row[4])
    if not (
        str(row[0]) == "degraded"
        and str(row[1] or "") == "focus_relation_sync_pending"
        and int(row[2] or 0) < int(row[3] or 0) == int(revision)
        and active_entities is not None
        and active_entities == entities
        and str(row[5] or "")
        == blocked_entities_receipt_hash(scope, int(row[2] or 0), active_entities)
        and str(row[6]) == encoded
        and str(row[7] or "") == receipt_sha
    ):
        return False
    now = _now_iso()
    changed = conn.execute(
        """
        UPDATE relation_scope_containment
        SET state='ready', reason_code='', active_revision=target_revision,
            active_blocked_entities_json=target_blocked_entities_json,
            active_blocked_entities_sha256=target_blocked_entities_sha256,
            affected_count=?, item_total=?, completed_items=?,
            attempts_total=attempts_total+1,
            target_attempts=target_attempts+1, last_attempt_at=?,
            last_progress_at=?, next_attempt_at='', updated_at=?
        WHERE scope_id=? AND state='degraded'
          AND reason_code='focus_relation_sync_pending'
          AND active_revision=? AND target_revision=?
          AND active_blocked_entities_json=?
          AND active_blocked_entities_sha256=?
          AND target_blocked_entities_json=?
          AND target_blocked_entities_sha256=? AND updated_at=?
          AND EXISTS(
              SELECT 1 FROM relation_scope_statistics s
              JOIN relation_frequency_backfill b ON b.scope_id=s.scope_id
              WHERE s.scope_id=? AND s.corpus_revision=?
                AND s.statistics_revision=? AND s.blocked_entities_json=?
                AND s.blocked_entities_sha256=? AND b.status='complete'
          )
        """,
        (
            max(0, int(affected_count)),
            max(0, int(affected_count)),
            max(0, int(affected_count)),
            now,
            now,
            now,
            scope,
            int(row[2] or 0),
            int(revision),
            str(row[4]),
            str(row[5] or ""),
            encoded,
            receipt_sha,
            str(row[8] or ""),
            scope,
            int(revision),
            int(revision),
            encoded,
            receipt_sha,
        ),
    ).rowcount
    return changed == 1


def _retry_delay_seconds(
    attempt: int,
    *,
    base_seconds: float,
    max_seconds: float,
) -> float:
    exponent = max(0, min(int(attempt) - 1, 12))
    return min(max(0.1, float(max_seconds)), max(0.1, float(base_seconds)) * (2**exponent))


def _mark_plan_blocked(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    active_revision: int,
    target_revision: int,
    active_entities_json: str,
    active_entities_sha256: str,
    target_entities_json: str,
    target_entities_sha256: str,
    reason_code: str,
    candidate_cap: int,
    affected_count: int,
    exhausted: bool,
    next_attempt_at: str = "",
) -> bool:
    now = _now_iso()
    changed = conn.execute(
        """
        UPDATE relation_scope_containment
        SET state=?, reason_code=?, candidate_cap=?, affected_count=?,
            item_total=0, completed_items=0,
            attempts_total=attempts_total+1,
            target_attempts=target_attempts+1,
            last_attempt_at=?, next_attempt_at=?, updated_at=?
        WHERE scope_id=? AND active_revision=? AND target_revision=?
          AND active_blocked_entities_json=?
          AND active_blocked_entities_sha256=?
          AND target_blocked_entities_json=?
          AND target_blocked_entities_sha256=?
          AND state='degraded'
        """,
        (
            "blocked" if exhausted else "degraded",
            str(reason_code),
            max(1, int(candidate_cap)),
            max(0, int(affected_count)),
            now,
            str(next_attempt_at),
            now,
            str(scope_id),
            int(active_revision),
            int(target_revision),
            str(active_entities_json),
            str(active_entities_sha256),
            str(target_entities_json),
            str(target_entities_sha256),
        ),
    ).rowcount
    return changed == 1


def drain_relation_containment_scope(
    conn: sqlite3.Connection,
    *,
    candidate_cap: int = DEFAULT_RELATION_CANDIDATE_CAP,
    max_attempts: int = DEFAULT_RELATION_MAX_ATTEMPTS,
    wall_clock_seconds: float = 0.5,
    backoff_base_seconds: float = DEFAULT_RELATION_BACKOFF_BASE_SECONDS,
    backoff_max_seconds: float = DEFAULT_RELATION_BACKOFF_MAX_SECONDS,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    commit: bool = True,
) -> dict[str, Any]:
    """Apply at most one due relation-policy delta atomically.

    Preview occurs for every exact pair before the first relation mutation.  A
    deadline, candidate-cap, extraction, or CAS failure rolls back the entire
    relation savepoint.  Health state is the only permitted write on failure.
    """

    ensure_relation_containment_schema(conn)
    now = _now_iso()
    row = conn.execute(
        """
        SELECT scope_id, active_revision, target_revision,
               active_blocked_entities_json, active_blocked_entities_sha256,
               target_blocked_entities_json, target_blocked_entities_sha256,
               state, target_attempts, max_attempts
        FROM relation_scope_containment
        WHERE state='degraded'
          AND target_revision>active_revision
          AND reason_code IN (
              'relation_policy_revision_pending',
              'maintenance_budget_exceeded',
              'relation_containment_plan_failed',
              'relation_containment_apply_failed'
          )
          AND (next_attempt_at='' OR next_attempt_at<=?)
        ORDER BY updated_at, scope_id
        LIMIT 1
        """,
        (now,),
    ).fetchone()
    if row is None:
        return {"status": "idle", "attempted": 0, "completed": 0, "failed": 0}

    scope = str(row[0])
    active_revision = int(row[1] or 0)
    target_revision = int(row[2] or 0)
    active_entities_json = str(row[3])
    active_entities_sha256 = str(row[4] or "")
    target_entities_json = str(row[5])
    target_entities_sha256 = str(row[6] or "")
    old_blocked = _decode_entities(active_entities_json)
    new_blocked = _decode_entities(target_entities_json)
    active_receipt_valid = (
        old_blocked is not None
        and active_entities_sha256
        == blocked_entities_receipt_hash(scope, active_revision, old_blocked)
    )
    target_receipt_valid = (
        new_blocked is not None
        and target_entities_sha256
        == blocked_entities_receipt_hash(scope, target_revision, new_blocked)
    )
    if not (active_receipt_valid and target_receipt_valid):
        now = _now_iso()
        changed = conn.execute(
            """
            UPDATE relation_scope_containment
            SET state='blocked', reason_code='relation_policy_receipt_mismatch',
                attempts_total=attempts_total+1,
                target_attempts=target_attempts+1,
                last_attempt_at=?, updated_at=?
            WHERE scope_id=? AND active_revision=? AND target_revision=?
              AND active_blocked_entities_json=?
              AND active_blocked_entities_sha256=?
              AND target_blocked_entities_json=?
              AND target_blocked_entities_sha256=?
              AND state='degraded'
            """,
            (
                now,
                now,
                scope,
                active_revision,
                target_revision,
                active_entities_json,
                active_entities_sha256,
                target_entities_json,
                target_entities_sha256,
            ),
        ).rowcount
        if changed != 1:
            return {
                "status": "superseded",
                "reason_code": "containment_target_changed",
                "attempted": 1,
                "completed": 0,
                "failed": 0,
                "affected_count": 0,
            }
        if commit:
            conn.commit()
        return {
            "status": "blocked",
            "reason_code": "relation_policy_receipt_mismatch",
            "attempted": 1,
            "completed": 0,
            "failed": 1,
            "affected_count": 0,
        }
    assert old_blocked is not None
    assert new_blocked is not None
    attempt = int(row[8] or 0) + 1
    configured_max = max(1, min(int(max_attempts or row[9] or 5), 20))
    cap = max(1, min(int(candidate_cap), 5000))
    deadline = (
        float(deadline_monotonic)
        if deadline_monotonic is not None
        else clock() + max(0.01, float(wall_clock_seconds))
    )

    try:
        plan = plan_relation_scope_delta(
            conn,
            scope_id=scope,
            target_revision=target_revision,
            old_blocked_entities=old_blocked,
            new_blocked_entities=new_blocked,
            candidate_cap=cap,
            deadline_monotonic=deadline,
            clock=clock,
        )
    except Exception as exc:
        if is_sqlite_lock_contention(exc):
            record_relation_lock_contention(conn, scope_ids=[scope])
            if commit:
                conn.commit()
            return {
                "status": "retry",
                "reason_code": "sqlite_lock_contention",
                "attempted": 1,
                "completed": 0,
                "failed": 0,
                "affected_count": 0,
            }
        exhausted = attempt >= configured_max
        retry_reason = (
            "maintenance_budget_exceeded"
            if isinstance(exc, TimeoutError)
            else "relation_containment_plan_failed"
        )
        delay = _retry_delay_seconds(
            attempt,
            base_seconds=backoff_base_seconds,
            max_seconds=backoff_max_seconds,
        )
        next_attempt = (
            "" if exhausted else (_now() + timedelta(seconds=delay)).isoformat()
        )
        marked = _mark_plan_blocked(
            conn,
            scope_id=scope,
            active_revision=active_revision,
            target_revision=target_revision,
            active_entities_json=active_entities_json,
            active_entities_sha256=active_entities_sha256,
            target_entities_json=target_entities_json,
            target_entities_sha256=target_entities_sha256,
            reason_code=(
                "maintenance_attempts_exhausted"
                if exhausted
                else retry_reason
            ),
            candidate_cap=cap,
            affected_count=0,
            exhausted=exhausted,
            next_attempt_at=next_attempt,
        )
        if not marked:
            return {
                "status": "superseded",
                "reason_code": "containment_target_changed",
                "attempted": 1,
                "completed": 0,
                "failed": 0,
                "affected_count": 0,
            }
        if commit:
            conn.commit()
        return {
            "status": "blocked" if exhausted else "retry",
            "reason_code": (
                "maintenance_attempts_exhausted"
                if exhausted
                else retry_reason
            ),
            "attempted": 1,
            "completed": 0,
            "failed": 1,
            "affected_count": 0,
        }
    if plan.blocked:
        marked = _mark_plan_blocked(
            conn,
            scope_id=scope,
            active_revision=active_revision,
            target_revision=target_revision,
            active_entities_json=active_entities_json,
            active_entities_sha256=active_entities_sha256,
            target_entities_json=target_entities_json,
            target_entities_sha256=target_entities_sha256,
            reason_code=plan.reason_code,
            candidate_cap=cap,
            affected_count=plan.affected_count,
            exhausted=True,
        )
        if not marked:
            return {
                "status": "superseded",
                "reason_code": "containment_target_changed",
                "attempted": 1,
                "completed": 0,
                "failed": 0,
                "affected_count": plan.affected_count,
            }
        if commit:
            conn.commit()
        return {
            "status": "blocked",
            "reason_code": plan.reason_code,
            "attempted": 1,
            "completed": 0,
            "failed": 1,
            "affected_count": plan.affected_count,
        }

    try:
        from .relation_extraction import rebuild_extracted_relations
    except ImportError:  # pragma: no cover
        from relation_extraction import rebuild_extracted_relations

    savepoint = "relation_containment_apply"
    if not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        for left_id, right_id in plan.pairs:
            if clock() >= deadline:
                raise TimeoutError("relation containment wall-clock budget exceeded")
            preview = rebuild_extracted_relations(
                conn,
                scope_ids=[scope],
                memory_ids=[left_id, right_id],
                focus_memory_ids=[left_id],
                dry_run=True,
                batch_id=f"containment-{target_revision}-preview",
                max_pairs=1,
                max_candidates=24,
                commit=False,
                blocked_entities=new_blocked,
            )
            if not bool(preview.get("ok")):
                raise RuntimeError(
                    str(preview.get("error") or "relation containment preview failed")
                )
        for left_id, right_id in plan.pairs:
            if clock() >= deadline:
                raise TimeoutError("relation containment wall-clock budget exceeded")
            applied = rebuild_extracted_relations(
                conn,
                scope_ids=[scope],
                memory_ids=[left_id, right_id],
                focus_memory_ids=[left_id],
                dry_run=False,
                batch_id=f"containment-{target_revision}",
                max_pairs=1,
                max_candidates=24,
                commit=False,
                blocked_entities=new_blocked,
            )
            if not bool(applied.get("ok")):
                raise RuntimeError(
                    str(applied.get("error") or "relation containment apply failed")
                )
        active_json, _active_json_sha, active_set = _canonical_entities(new_blocked)
        active_sha = blocked_entities_receipt_hash(scope, target_revision, active_set)
        finished = _now_iso()
        focus_pending = relation_focus_scope_has_debt(conn, scope)
        final_state = "degraded" if focus_pending else "ready"
        final_reason = "focus_relation_sync_pending" if focus_pending else ""
        changed = conn.execute(
            """
            UPDATE relation_scope_containment
            SET state=?, reason_code=?, active_revision=target_revision,
                active_blocked_entities_json=?,
                active_blocked_entities_sha256=?, candidate_cap=?,
                affected_count=?, item_total=?, completed_items=?,
                attempts_total=attempts_total+1,
                target_attempts=target_attempts+1,
                last_attempt_at=?, last_progress_at=?, next_attempt_at='',
                updated_at=?
            WHERE scope_id=? AND active_revision=? AND target_revision=?
              AND active_blocked_entities_json=?
              AND active_blocked_entities_sha256=?
              AND target_blocked_entities_json=?
              AND target_blocked_entities_sha256=?
              AND state='degraded'
            """,
            (
                final_state,
                final_reason,
                active_json,
                active_sha,
                cap,
                plan.affected_count,
                len(plan.pairs),
                len(plan.pairs),
                finished,
                finished,
                finished,
                scope,
                active_revision,
                target_revision,
                active_entities_json,
                active_entities_sha256,
                target_entities_json,
                target_entities_sha256,
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("relation containment target changed before commit")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception as exc:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if is_sqlite_lock_contention(exc):
            record_relation_lock_contention(conn, scope_ids=[scope])
            if commit:
                conn.commit()
            return {
                "status": "retry",
                "reason_code": "sqlite_lock_contention",
                "attempted": 1,
                "completed": 0,
                "failed": 0,
                "affected_count": plan.affected_count,
            }
        exhausted = attempt >= configured_max
        reason = (
            "maintenance_budget_exceeded"
            if isinstance(exc, TimeoutError)
            else "relation_containment_apply_failed"
        )
        delay = _retry_delay_seconds(
            attempt,
            base_seconds=backoff_base_seconds,
            max_seconds=backoff_max_seconds,
        )
        next_attempt = "" if exhausted else (_now() + timedelta(seconds=delay)).isoformat()
        marked = _mark_plan_blocked(
            conn,
            scope_id=scope,
            active_revision=active_revision,
            target_revision=target_revision,
            active_entities_json=active_entities_json,
            active_entities_sha256=active_entities_sha256,
            target_entities_json=target_entities_json,
            target_entities_sha256=target_entities_sha256,
            reason_code=("maintenance_attempts_exhausted" if exhausted else reason),
            candidate_cap=cap,
            affected_count=plan.affected_count,
            exhausted=exhausted,
            next_attempt_at=next_attempt,
        )
        if not marked:
            return {
                "status": "superseded",
                "reason_code": "containment_target_changed",
                "attempted": 1,
                "completed": 0,
                "failed": 0,
                "affected_count": plan.affected_count,
            }
        if commit:
            conn.commit()
        return {
            "status": "blocked" if exhausted else "retry",
            "reason_code": "maintenance_attempts_exhausted" if exhausted else reason,
            "attempted": 1,
            "completed": 0,
            "failed": 1,
            "affected_count": plan.affected_count,
        }

    if commit:
        conn.commit()
    return {
        "status": final_state,
        "reason_code": final_reason,
        "attempted": 1,
        "completed": 1,
        "failed": 0,
        "affected_count": plan.affected_count,
    }


def mark_relation_scope_degraded(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    reason_code: str,
    target_revision: int | None = None,
    candidate_cap: int = DEFAULT_RELATION_CANDIDATE_CAP,
    affected_count: int = 0,
    operator_action_required: bool = False,
) -> None:
    """Fail closed for a scope without mutating generated relation rows."""

    ensure_relation_containment_schema(conn)
    scope = str(scope_id or "").strip()
    if not scope:
        return
    receipt = conn.execute(
        """
        SELECT corpus_revision, statistics_revision, blocked_entities_json,
               blocked_entities_sha256
        FROM relation_scope_statistics WHERE scope_id=?
        """,
        (scope,),
    ).fetchone()
    explicit_target = target_revision is not None
    revision = max(0, int(target_revision or 0)) if explicit_target else 0
    receipt_current = bool(
        receipt
        and explicit_target
        and int(receipt[0] or 0) == revision
        and int(receipt[1] or 0) == revision
    )
    blocked_json = str(receipt[2] or "[]") if receipt_current else "[]"
    blocked_sha = str(receipt[3] or "") if receipt_current else ""
    now = _now_iso()
    state = "blocked" if operator_action_required else "degraded"
    existing = conn.execute(
        """
        SELECT state, target_revision, reason_code, active_revision,
               target_attempts, updated_at
        FROM relation_scope_containment
        WHERE scope_id=?
        """,
        (scope,),
    ).fetchone()
    if existing is not None:
        current_state = str(existing[0])
        current_target = int(existing[1] or 0)
        current_reason = str(existing[2] or "")
        active_revision = int(existing[3] or 0)
        if current_state == "disabled" or current_reason == "legacy_generated_relations_unverified":
            return
        if current_state in {"blocked", "disabled"} and (
            not explicit_target or revision <= current_target
        ):
            return
        if explicit_target and revision < current_target:
            return
        if explicit_target:
            if revision > current_target:
                _record_superseded_containment_target(
                    conn,
                    scope_id=scope,
                    active_revision=active_revision,
                    target_revision=current_target,
                    next_target_revision=revision,
                    prior_state=current_state,
                    prior_reason=current_reason,
                    target_attempts=int(existing[4] or 0),
                    prior_updated_at=str(existing[5] or ""),
                    disposed_at=now,
                )
            conn.execute(
                """
                UPDATE relation_scope_containment
                SET state=?, reason_code=?,
                    target_attempts=CASE
                        WHEN ? > target_revision THEN 0 ELSE target_attempts END,
                    target_revision=?,
                    target_blocked_entities_json=?,
                    target_blocked_entities_sha256=?, candidate_cap=?,
                    affected_count=?, item_total=0, completed_items=0,
                    next_attempt_at='', updated_at=?
                WHERE scope_id=? AND target_revision<=?
                """,
                (
                    state,
                    str(reason_code or "relation_signal_untrusted"),
                    revision,
                    revision,
                    blocked_json,
                    blocked_sha,
                    max(1, min(int(candidate_cap), 5000)),
                    max(0, int(affected_count)),
                    now,
                    scope,
                    revision,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE relation_scope_containment
                SET state=?, reason_code=?, updated_at=?
                WHERE scope_id=? AND state NOT IN ('blocked','disabled')
                """,
                (
                    state,
                    str(reason_code or "relation_signal_untrusted"),
                    now,
                    scope,
                ),
            )
        return
    conn.execute(
        """
        INSERT INTO relation_scope_containment(
            scope_id, state, reason_code, active_revision, target_revision,
            active_blocked_entities_json, active_blocked_entities_sha256,
            target_blocked_entities_json, target_blocked_entities_sha256,
            candidate_cap, affected_count, item_total, completed_items,
            attempts_total, target_attempts, max_attempts,
            lock_contention_skips, next_attempt_at, progress_started_at,
            last_attempt_at, last_progress_at, created_at, updated_at
        ) VALUES(?, ?, ?, 0, ?, '[]', '', ?, ?, ?, ?, 0, 0, 0, 0, 5, 0,
                 '', '', '', '', ?, ?)
        """,
        (
            scope,
            state,
            str(reason_code or "relation_signal_untrusted"),
            revision,
            blocked_json,
            blocked_sha,
            max(1, min(int(candidate_cap), 5000)),
            max(0, int(affected_count)),
            now,
            now,
        ),
    )


def record_relation_lock_contention(
    conn: sqlite3.Connection,
    *,
    scope_ids: Iterable[str] | None = None,
    count: int = 1,
) -> int:
    """Persist accumulated nonblocking scheduler skips on known scope rows."""

    if not _table_exists(conn, "relation_scope_containment"):
        return 0
    scopes = sorted({str(item) for item in (scope_ids or []) if str(item)})
    params: list[Any] = [max(1, int(count)), _now_iso()]
    where = ""
    if scopes:
        where = f" WHERE scope_id IN ({','.join('?' for _ in scopes)})"
        params.extend(scopes)
    return int(
        conn.execute(
            """
            UPDATE relation_scope_containment
            SET lock_contention_skips=lock_contention_skips+?, updated_at=?
            """
            + where,
            params,
        ).rowcount
    )


def generated_relation_scope_policy(
    conn: sqlite3.Connection,
    scope_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Read generated-edge trust policy for exact scopes without mutations."""

    scopes = sorted({str(item) for item in scope_ids if str(item)})
    if not scopes:
        return {}
    policy: dict[str, dict[str, Any]] = {
        scope: {
            "state": "disabled",
            "reason_code": "containment_state_missing",
            "active_revision": 0,
            "target_revision": 0,
            "generated_signal_enabled": False,
        }
        for scope in scopes
    }
    try:
        from .relation_frequency_index import relation_frequency_index_schema_status
        from .relation_scope_state import relation_scope_state_schema_status
    except ImportError:  # pragma: no cover - direct source-script fallback
        from relation_frequency_index import (  # type: ignore[no-redef]
            relation_frequency_index_schema_status,
        )
        from relation_scope_state import (  # type: ignore[no-redef]
            relation_scope_state_schema_status,
        )

    schema_current = (
        bool(relation_containment_schema_status(conn).get("current"))
        and bool(relation_scope_state_schema_status(conn).get("current"))
        and bool(relation_frequency_index_schema_status(conn).get("current"))
    )
    if not schema_current:
        for scope in scopes:
            policy[scope]["reason_code"] = "relation_schema_missing"
        return policy
    placeholders = ",".join("?" for _ in scopes)
    rows = conn.execute(
        f"""
        SELECT c.scope_id, c.state, c.reason_code,
               c.active_revision, c.target_revision,
               c.active_blocked_entities_json,
               c.active_blocked_entities_sha256,
               c.target_blocked_entities_json,
               c.target_blocked_entities_sha256,
               s.statistics_revision, s.corpus_revision,
               s.blocked_entities_json, s.blocked_entities_sha256,
               b.status
        FROM relation_scope_containment c
        LEFT JOIN relation_scope_statistics s ON s.scope_id=c.scope_id
        LEFT JOIN relation_frequency_backfill b ON b.scope_id=c.scope_id
        WHERE c.scope_id IN ({placeholders})
        """,
        scopes,
    ).fetchall()
    for row in rows:
        state = str(row[1])
        reason = str(row[2] or "")
        active_revision = int(row[3] or 0)
        target_revision = int(row[4] or 0)
        statistics_revision = int(row[9] or 0) if row[9] is not None else -1
        corpus_revision = int(row[10] or 0) if row[10] is not None else -2
        statistics_entities = _decode_entities(row[11])
        active_entities = _decode_entities(row[5])
        target_entities = _decode_entities(row[7])
        statistics_receipt_valid = (
            statistics_entities is not None
            and str(row[12] or "")
            == blocked_entities_receipt_hash(
                str(row[0]), corpus_revision, statistics_entities
            )
        )
        active_receipt_valid = (
            active_entities is not None
            and str(row[6] or "")
            == blocked_entities_receipt_hash(
                str(row[0]), active_revision, active_entities
            )
        )
        target_receipt_valid = (
            target_entities is not None
            and str(row[8] or "")
            == blocked_entities_receipt_hash(
                str(row[0]), target_revision, target_entities
            )
        )
        frequency_current = (
            statistics_revision == corpus_revision
            and str(row[13] or "") == "complete"
            and statistics_receipt_valid
        )
        if state not in {"blocked", "disabled"}:
            if not frequency_current:
                state = "degraded"
                reason = "frequency_receipt_stale"
            elif not (active_receipt_valid and target_receipt_valid):
                state = "blocked"
                reason = "relation_policy_receipt_mismatch"
            elif target_revision > active_revision:
                if target_entities != statistics_entities:
                    state = "blocked"
                    reason = "relation_policy_target_mismatch"
                elif target_revision != corpus_revision:
                    state = "degraded"
                    reason = "relation_generation_stale"
            elif active_entities != target_entities or active_entities != statistics_entities:
                state = "blocked"
                reason = "relation_policy_receipt_mismatch"
            elif active_revision != corpus_revision:
                state = "degraded"
                reason = "relation_generation_stale"
        policy[str(row[0])] = {
            "state": state,
            "reason_code": reason,
            "active_revision": active_revision,
            "target_revision": target_revision,
            "generated_signal_enabled": (
                state == "ready" and active_revision == target_revision
            ),
        }
    dirty_scopes: set[str] = set()
    if _table_exists(conn, "relation_frequency_changes"):
        dirty_rows = conn.execute(
            f"""
            SELECT old_scope_id, new_scope_id FROM relation_frequency_changes
            WHERE old_scope_id IN ({placeholders})
               OR new_scope_id IN ({placeholders})
            """,
            [*scopes, *scopes],
        ).fetchall()
        for row in dirty_rows:
            dirty_scopes.update(
                str(value) for value in row if str(value or "") in policy
            )
    for scope in dirty_scopes:
        if policy[scope]["state"] not in {"blocked", "disabled"}:
            policy[scope].update(
                {
                    "state": "degraded",
                    "reason_code": "frequency_change_pending",
                    "generated_signal_enabled": False,
                }
            )
    retry_failure_scopes: set[str] = set()
    dead_failure_scopes: set[str] = set()
    if _table_exists(conn, "relation_frequency_failures"):
        failure_rows = conn.execute(
            f"""
            SELECT f.old_scope_id, f.new_scope_id, f.status,
                   c.old_scope_id, c.new_scope_id, i.scope_id, m.scope_id
            FROM relation_frequency_failures f
            LEFT JOIN relation_frequency_changes c ON c.memory_id=f.memory_id
            LEFT JOIN relation_indexed_memories i ON i.memory_id=f.memory_id
            LEFT JOIN memories m ON m.id=f.memory_id
            WHERE f.old_scope_id IN ({placeholders})
               OR f.new_scope_id IN ({placeholders})
               OR c.old_scope_id IN ({placeholders})
               OR c.new_scope_id IN ({placeholders})
               OR i.scope_id IN ({placeholders})
               OR m.scope_id IN ({placeholders})
            """,
            [*scopes, *scopes, *scopes, *scopes, *scopes, *scopes],
        ).fetchall()
        for row in failure_rows:
            target = (
                dead_failure_scopes
                if str(row[2]) == "dead_letter"
                else retry_failure_scopes
            )
            target.update(
                str(value)
                for value in (*row[:2], *row[3:])
                if str(value or "") in policy
            )
    for scope in retry_failure_scopes:
        if policy[scope]["state"] not in {"blocked", "disabled"}:
            policy[scope].update(
                {
                    "state": "degraded",
                    "reason_code": "frequency_maintenance_retry",
                    "generated_signal_enabled": False,
                }
            )
    for scope in dead_failure_scopes:
        if policy[scope]["state"] not in {"blocked", "disabled"}:
            policy[scope].update(
                {
                    "state": "blocked",
                    "reason_code": "frequency_maintenance_poisoned",
                    "generated_signal_enabled": False,
                }
            )
    if (
        _table_exists(conn, "relation_focus_work")
        and _table_exists(conn, "relation_focus_work_scopes")
    ):
        focus_rows = conn.execute(
            f"""
            SELECT DISTINCT w.memory_id, w.work_generation, w.status, s.scope_id
            FROM relation_focus_work w
            JOIN relation_focus_work_scopes s
              ON s.memory_id=w.memory_id
             AND s.work_generation=w.work_generation
            WHERE s.scope_id IN ({placeholders})
            """,
            scopes,
        ).fetchall()
        validated_focus: dict[str, bool] = {}
        for row in focus_rows:
            memory_id = str(row[0])
            scope = str(row[3])
            valid = validated_focus.get(memory_id)
            if valid is None:
                try:
                    work = load_relation_focus_work(conn, memory_id)
                    valid = bool(
                        work
                        and int(work["work_generation"]) == int(row[1] or 0)
                    )
                except RuntimeError:
                    valid = False
                validated_focus[memory_id] = valid
            if policy[scope]["state"] == "disabled":
                continue
            if not valid or str(row[2]) == "dead_letter":
                policy[scope].update(
                    {
                        "state": "blocked",
                        "reason_code": (
                            "focus_relation_work_receipt_mismatch"
                            if not valid
                            else "focus_relation_sync_poisoned"
                        ),
                        "generated_signal_enabled": False,
                    }
                )
            elif policy[scope]["state"] != "blocked":
                policy[scope].update(
                    {
                        "state": "degraded",
                        "reason_code": "focus_relation_sync_pending",
                        "generated_signal_enabled": False,
                    }
                )
    legacy_scopes: set[str] = set()
    if _table_exists(conn, "relation_rebuild_queue"):
        legacy_scopes.update(
            str(row[0])
            for row in conn.execute(
                f"""
                SELECT DISTINCT q.scope_id FROM relation_rebuild_queue q
                WHERE q.scope_id IN ({placeholders}) AND q.status<>'completed'
                  AND NOT EXISTS (
                      SELECT 1 FROM relation_work_dispositions d
                      WHERE d.work_kind='rebuild_queue'
                        AND d.work_key=CAST(q.id AS TEXT)
                        AND d.work_revision=q.requested_updated_at
                  )
                """,
                scopes,
            ).fetchall()
        )
    if _table_exists(conn, "relation_scope_reclassification"):
        legacy_scopes.update(
            str(row[0])
            for row in conn.execute(
                f"""
                SELECT DISTINCT scope_id FROM relation_scope_reclassification
                WHERE scope_id IN ({placeholders}) AND status<>'complete'
                """,
                scopes,
            ).fetchall()
        )
    for scope in legacy_scopes:
        payload = policy.setdefault(scope, {})
        if payload.get("state") not in {"blocked", "disabled"}:
            payload.update(
                {
                    "state": "blocked",
                    "reason_code": "legacy_unbounded_work_present",
                    "generated_signal_enabled": False,
                }
            )
    return policy


def _iso_age_seconds(value: Any) -> float:
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (_now() - parsed.astimezone(timezone.utc)).total_seconds())


def _frequency_health_counts(
    conn: sqlite3.Connection,
    scopes: list[str],
) -> dict[str, dict[str, int]]:
    """Aggregate content-free frequency debt across every current scope endpoint."""

    counts: dict[str, dict[str, int]] = {}
    outer_where = " WHERE scope_id<>''"
    params: list[Any] = []
    if scopes:
        outer_where += f" AND scope_id IN ({','.join('?' for _ in scopes)})"
        params.extend(scopes)
    if _table_exists(conn, "relation_frequency_changes"):
        sql = (
            "SELECT scope_id, COUNT(*) FROM ("
            "SELECT c.memory_id, c.old_scope_id AS scope_id "
            "FROM relation_frequency_changes c "
            "LEFT JOIN relation_frequency_failures f "
            "ON f.memory_id=c.memory_id AND f.work_generation=c.work_generation "
            "WHERE f.status IS NULL "
            "UNION SELECT c.memory_id, c.new_scope_id AS scope_id "
            "FROM relation_frequency_changes c "
            "LEFT JOIN relation_frequency_failures f "
            "ON f.memory_id=c.memory_id AND f.work_generation=c.work_generation "
            "WHERE f.status IS NULL"
            ")"
            + outer_where
            + " GROUP BY scope_id"
        )
        for row in conn.execute(sql, params).fetchall():
            counts.setdefault(str(row[0]), {})["pending"] = int(row[1])
    if _table_exists(conn, "relation_frequency_failures"):
        sql = (
            "SELECT scope_id, status, COUNT(*) FROM ("
            "SELECT f.memory_id, f.status, f.old_scope_id AS scope_id "
            "FROM relation_frequency_failures f "
            "UNION SELECT f.memory_id, f.status, f.new_scope_id AS scope_id "
            "FROM relation_frequency_failures f "
            "UNION SELECT f.memory_id, f.status, c.old_scope_id AS scope_id "
            "FROM relation_frequency_failures f "
            "LEFT JOIN relation_frequency_changes c ON c.memory_id=f.memory_id "
            "AND c.work_generation=f.work_generation "
            "UNION SELECT f.memory_id, f.status, c.new_scope_id AS scope_id "
            "FROM relation_frequency_failures f "
            "LEFT JOIN relation_frequency_changes c ON c.memory_id=f.memory_id "
            "AND c.work_generation=f.work_generation "
            "UNION SELECT f.memory_id, f.status, i.scope_id AS scope_id "
            "FROM relation_frequency_failures f "
            "LEFT JOIN relation_indexed_memories i ON i.memory_id=f.memory_id "
            "UNION SELECT f.memory_id, f.status, m.scope_id AS scope_id "
            "FROM relation_frequency_failures f "
            "LEFT JOIN memories m ON m.id=f.memory_id"
            ")"
            + outer_where
            + " GROUP BY scope_id, status"
        )
        for row in conn.execute(sql, params).fetchall():
            bucket = counts.setdefault(str(row[0]), {})
            key = "poisoned" if str(row[1]) == "dead_letter" else "retry"
            bucket[key] = int(bucket.get(key, 0)) + int(row[2])
    if (
        _table_exists(conn, "relation_focus_work")
        and _table_exists(conn, "relation_focus_work_scopes")
    ):
        focus_where = (
            " WHERE NOT (w.status='dead_letter' AND EXISTS ("
            "SELECT 1 FROM relation_work_dispositions d "
            "WHERE d.work_kind='focus_sync' AND d.scope_id=s.scope_id "
            "AND d.work_revision=CAST(w.work_generation AS TEXT) "
            "AND substr(d.work_key,1,length(w.memory_id)+1)=w.memory_id||'|' "
            "AND d.terminal_state='poisoned'))"
        )
        focus_params: list[Any] = []
        if scopes:
            focus_where += f" AND s.scope_id IN ({','.join('?' for _ in scopes)})"
            focus_params.extend(scopes)
        focus_sql = (
            "SELECT s.scope_id, w.status, COUNT(*) "
            "FROM relation_focus_work_scopes s "
            "JOIN relation_focus_work w ON w.memory_id=s.memory_id "
            "AND w.work_generation=s.work_generation"
            + focus_where
            + " GROUP BY s.scope_id, w.status"
        )
        for row in conn.execute(focus_sql, focus_params).fetchall():
            bucket = counts.setdefault(str(row[0]), {})
            status = str(row[1])
            key = (
                "poisoned"
                if status == "dead_letter"
                else ("retry" if status == "retry" else "pending")
            )
            bucket[key] = int(bucket.get(key, 0)) + int(row[2])
    return counts


def relation_containment_report(
    conn: sqlite3.Connection,
    *,
    scope_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return content-free, scope-filtered Program 0 relation health."""

    status = relation_containment_schema_status(conn)
    if not status["current"]:
        return {
            "status": "schema_missing",
            "state": "degraded",
            "reason_code": "containment_schema_missing",
            "scopes": [],
            **status,
        }
    scopes = sorted({str(item) for item in (scope_ids or []) if str(item)})
    where = ""
    params: list[Any] = []
    if scopes:
        where = f" WHERE c.scope_id IN ({','.join('?' for _ in scopes)})"
        params.extend(scopes)
    rows = conn.execute(
        """
        SELECT c.scope_id, c.state, c.reason_code, c.active_revision,
               c.target_revision, c.candidate_cap, c.affected_count,
               c.item_total, c.completed_items, c.attempts_total,
               c.target_attempts, c.max_attempts, c.lock_contention_skips,
               c.progress_started_at, c.last_progress_at, c.updated_at,
               c.created_at
        FROM relation_scope_containment c
        """
        + where
        + " ORDER BY c.scope_id",
        params,
    ).fetchall()
    disposition_counts: dict[str, dict[str, int]] = {}
    disposition_sql = (
        "SELECT scope_id, terminal_state, COUNT(*), SUM(lease_expirations) "
        "FROM relation_work_dispositions"
    )
    disposition_params: list[Any] = []
    if scopes:
        disposition_sql += f" WHERE scope_id IN ({','.join('?' for _ in scopes)})"
        disposition_params.extend(scopes)
    disposition_sql += " GROUP BY scope_id, terminal_state"
    for row in conn.execute(disposition_sql, disposition_params).fetchall():
        bucket = disposition_counts.setdefault(str(row[0]), {})
        bucket[str(row[1])] = int(row[2])
        bucket["lease_expirations"] = int(bucket.get("lease_expirations", 0)) + int(
            row[3] or 0
        )

    legacy_counts: dict[str, dict[str, int]] = {}
    if _table_exists(conn, "relation_rebuild_queue"):
        queue_sql = (
            "SELECT q.scope_id, q.status, COUNT(*), SUM(q.lease_expirations) "
            "FROM relation_rebuild_queue q "
            "WHERE q.status<>'completed' AND NOT EXISTS ("
            "SELECT 1 FROM relation_work_dispositions d "
            "WHERE d.work_kind='rebuild_queue' AND d.work_key=CAST(q.id AS TEXT) "
            "AND d.work_revision=q.requested_updated_at)"
        )
        queue_params: list[Any] = []
        if scopes:
            queue_sql += f" AND q.scope_id IN ({','.join('?' for _ in scopes)})"
            queue_params.extend(scopes)
        queue_sql += " GROUP BY q.scope_id, q.status"
        for row in conn.execute(queue_sql, queue_params).fetchall():
            bucket = legacy_counts.setdefault(str(row[0]), {})
            bucket[str(row[1])] = int(row[2])
            bucket["lease_expirations"] = int(bucket.get("lease_expirations", 0)) + int(
                row[3] or 0
            )
    if _table_exists(conn, "relation_scope_reclassification"):
        reclass_sql = (
            "SELECT scope_id, COUNT(*) FROM relation_scope_reclassification "
            "WHERE status<>'complete'"
        )
        reclass_params: list[Any] = []
        if scopes:
            reclass_sql += f" AND scope_id IN ({','.join('?' for _ in scopes)})"
            reclass_params.extend(scopes)
        reclass_sql += " GROUP BY scope_id"
        for row in conn.execute(reclass_sql, reclass_params).fetchall():
            legacy_counts.setdefault(str(row[0]), {})[
                "reclassification_pending"
            ] = int(row[1])

    frequency_counts = _frequency_health_counts(conn, scopes)
    report_scopes = sorted(
        set(scopes)
        | {str(row[0]) for row in rows}
        | set(legacy_counts)
        | set(frequency_counts)
    )
    effective_policy = generated_relation_scope_policy(conn, report_scopes)
    payloads: list[dict[str, Any]] = []
    for row in rows:
        scope = str(row[0])
        stored_state = str(row[1])
        stored_reason = str(row[2] or "")
        effective = effective_policy.get(scope) or {}
        item_total = int(row[7] or 0)
        completed = int(row[8] or 0)
        dispositions = disposition_counts.get(scope, {})
        legacy = legacy_counts.get(scope, {})
        frequency = frequency_counts.get(scope, {})
        legacy_runnable = sum(
            int(legacy.get(key, 0)) for key in ("pending", "retry", "processing")
        ) + int(legacy.get("reclassification_pending", 0))
        poisoned = (
            int(dispositions.get("poisoned", 0))
            + int(legacy.get("dead_letter", 0))
            + int(frequency.get("poisoned", 0))
        )
        effective_state = str(effective.get("state") or stored_state)
        effective_reason = str(effective.get("reason_code") or stored_reason)
        if stored_reason == "maintenance_attempts_exhausted":
            poisoned += 1
        state = "blocked" if legacy_runnable else effective_state
        reason = (
            "legacy_unbounded_work_present" if legacy_runnable else effective_reason
        )
        containment_pending = (
            max(1, item_total - completed)
            if (
                stored_state == "degraded"
                and stored_reason != "focus_relation_sync_pending"
                and int(row[4] or 0) > int(row[3] or 0)
            )
            else 0
        )
        pending = (
            containment_pending
            + int(legacy.get("pending", 0))
            + int(legacy.get("reclassification_pending", 0))
            + int(frequency.get("pending", 0))
        )
        processing = int(legacy.get("processing", 0))
        retry = (
            int(stored_state == "degraded" and int(row[10] or 0) > 0)
            + int(legacy.get("retry", 0))
            + int(frequency.get("retry", 0))
        )
        operator_required = state in {"blocked", "disabled"} or bool(
            poisoned or legacy_runnable
        )
        has_work = bool(pending or processing or retry or poisoned or state != "ready")
        age_anchor = (row[13] or row[16]) if has_work else ""
        elapsed = _iso_age_seconds(age_anchor)
        progress_rate = (completed / elapsed) if completed and elapsed > 0 else 0.0
        payloads.append(
            {
                "state": state,
                "reason_code": reason,
                "scope_id": scope,
                "active_revision": int(row[3] or 0),
                "target_revision": int(row[4] or 0),
                "candidate_cap": int(row[5] or 0),
                "affected_count": int(row[6] or 0),
                "item_total": item_total,
                "pending": pending,
                "processing": processing,
                "retry": retry,
                "completed": completed + int(dispositions.get("completed", 0)),
                "poisoned": poisoned,
                "cancelled": int(dispositions.get("cancelled", 0)),
                "superseded": int(dispositions.get("superseded", 0)),
                "oldest_age_seconds": round(_iso_age_seconds(age_anchor), 3),
                "last_progress_at": str(row[14] or ""),
                "progress_rate": round(progress_rate, 6),
                "attempts_total": int(row[9] or 0),
                "max_attempts": int(row[11] or 0),
                "lease_expirations": int(dispositions.get("lease_expirations", 0))
                + int(legacy.get("lease_expirations", 0)),
                "lock_contention_skips": int(row[12] or 0),
                "auto_recoverable": state == "degraded" and not operator_required,
                "operator_action_required": operator_required,
                "legacy_runnable": legacy_runnable,
                "stale_generation_count": int(
                    not bool(effective.get("generated_signal_enabled"))
                ),
            }
        )
    present_scopes = {str(item["scope_id"]) for item in payloads}
    for scope in report_scopes:
        if scope in present_scopes:
            continue
        dispositions = disposition_counts.get(scope, {})
        legacy = legacy_counts.get(scope, {})
        frequency = frequency_counts.get(scope, {})
        legacy_runnable = sum(
            int(legacy.get(key, 0)) for key in ("pending", "retry", "processing")
        ) + int(legacy.get("reclassification_pending", 0))
        poisoned = (
            int(dispositions.get("poisoned", 0))
            + int(legacy.get("dead_letter", 0))
            + int(frequency.get("poisoned", 0))
        )
        pending = (
            int(legacy.get("pending", 0))
            + int(legacy.get("reclassification_pending", 0))
            + int(frequency.get("pending", 0))
        )
        processing = int(legacy.get("processing", 0))
        retry = int(legacy.get("retry", 0)) + int(frequency.get("retry", 0))
        reason = (
            "legacy_unbounded_work_present"
            if legacy_runnable
            else (
                "relation_work_poisoned"
                if poisoned
                else "containment_state_missing"
            )
        )
        payloads.append(
            {
                "state": "blocked" if legacy_runnable or poisoned else "disabled",
                "reason_code": reason,
                "scope_id": scope,
                "active_revision": 0,
                "target_revision": 0,
                "candidate_cap": 0,
                "affected_count": 0,
                "item_total": 0,
                "pending": pending,
                "processing": processing,
                "retry": retry,
                "completed": int(dispositions.get("completed", 0)),
                "poisoned": poisoned,
                "cancelled": int(dispositions.get("cancelled", 0)),
                "superseded": int(dispositions.get("superseded", 0)),
                "oldest_age_seconds": 0.0,
                "last_progress_at": "",
                "progress_rate": 0.0,
                "attempts_total": 0,
                "max_attempts": 0,
                "lease_expirations": int(
                    dispositions.get("lease_expirations", 0)
                ) + int(legacy.get("lease_expirations", 0)),
                "lock_contention_skips": 0,
                "auto_recoverable": False,
                "operator_action_required": True,
                "legacy_runnable": legacy_runnable,
                "stale_generation_count": 1,
            }
        )
    payloads.sort(key=lambda item: str(item["scope_id"]))
    overall = "ready"
    reason = ""
    if any(item["operator_action_required"] for item in payloads):
        overall = "blocked"
        reason = "operator_action_required"
    elif any(item["state"] != "ready" for item in payloads):
        overall = "degraded"
        reason = "relation_scope_degraded"
    return {
        "status": overall,
        "state": overall,
        "reason_code": reason,
        "scope_count": len(payloads),
        "scopes": payloads,
        **status,
    }


__all__ = [
    "DEFAULT_RELATION_BACKOFF_BASE_SECONDS",
    "DEFAULT_RELATION_BACKOFF_MAX_SECONDS",
    "DEFAULT_RELATION_CANDIDATE_CAP",
    "DEFAULT_RELATION_MAX_ATTEMPTS",
    "RELATION_CONTAINMENT_MIGRATION_DESCRIPTION",
    "RELATION_CONTAINMENT_MIGRATION_ID",
    "RELATION_CONTAINMENT_MIGRATION_PLUGIN_VERSION",
    "RELATION_CONTAINMENT_SCHEMA_VERSION",
    "RELATION_CONTAINMENT_STATES",
    "RelationContainmentPlan",
    "complete_relation_focus_work",
    "confirm_relation_scope_focus_generation",
    "defer_relation_focus_work",
    "drain_relation_containment_scope",
    "enqueue_relation_focus_work",
    "establish_relation_scope_baseline",
    "ensure_relation_containment_schema",
    "generated_relation_scope_policy",
    "load_relation_focus_work",
    "mark_relation_scope_degraded",
    "plan_focus_relation_pairs",
    "plan_relation_scope_delta",
    "record_relation_focus_failure",
    "record_relation_lock_contention",
    "record_relation_scope_target",
    "reconcile_relation_scope_frequency_current",
    "relation_containment_report",
    "relation_containment_schema_status",
    "relation_focus_scope_has_debt",
    "stage_relation_scope_focus_generation",
]
