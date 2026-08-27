"""Finite, leased relation-policy generations built on Program 0 containment.

The Program 2 path is additive and feature-gated.  Program 0 remains the
default execution path.  This module persists only relation-domain metadata;
it does not create a universal durable-work table or move relation payloads out
of ``memory_relations``.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from .durable_work import (
    DURABLE_WORK_ITEM_STATES,
    DurableWorkDescriptor,
    DurableWorkLease,
    canonical_snapshot_hash,
    durable_work_health,
)
from .relation_containment import (
    DEFAULT_RELATION_BACKOFF_BASE_SECONDS,
    DEFAULT_RELATION_BACKOFF_MAX_SECONDS,
    DEFAULT_RELATION_CANDIDATE_CAP,
    DEFAULT_RELATION_MAX_ATTEMPTS,
    RelationContainmentPlan,
    plan_relation_scope_delta,
    relation_focus_scope_has_debt,
)
from .relation_scope_state import blocked_entities_receipt_hash
from .sqlite_recovery import is_sqlite_lock_contention
from .sqlite_schema import execute_script_transaction_neutral


RELATION_POLICY_GENERATION_SCHEMA_VERSION = 10814
RELATION_POLICY_GENERATION_MIGRATION_ID = (
    "0014_relation_policy_generation_v1_10_6"
)
RELATION_POLICY_GENERATION_MIGRATION_PLUGIN_VERSION = "1.10.6"
RELATION_POLICY_GENERATION_MIGRATION_DESCRIPTION = (
    "Add finite relation policy generations, leased items, and edge provenance"
)
RELATION_POLICY_VERSION = "relation-policy.v1"

RELATION_GENERATION_STATES = frozenset(
    {
        "building",
        "pending",
        "processing",
        "retry",
        "completed",
        "poisoned",
        "cancelled",
        "superseded",
        "blocked",
    }
)
RELATION_GENERATION_TERMINAL_STATES = frozenset(
    {"completed", "poisoned", "cancelled", "superseded", "blocked"}
)

_GENERATION_TABLE = "relation_policy_generations"
_ITEM_TABLE = "relation_generation_items"
_PROVENANCE_TABLE = "relation_edge_provenance"
_REQUIRED_COLUMNS: Mapping[str, frozenset[str]] = {
    _GENERATION_TABLE: frozenset(
        {
            "generation_id",
            "scope_id",
            "idempotency_key",
            "scope_snapshot_json",
            "authority_snapshot_json",
            "policy_version",
            "relation_revision",
            "source_corpus_revision",
            "frozen_upper_bound",
            "old_blocked_entities_json",
            "old_blocked_entities_sha256",
            "new_blocked_entities_json",
            "new_blocked_entities_sha256",
            "delta_json",
            "delta_sha256",
            "item_set_hash",
            "item_total",
            "cursor",
            "state",
            "reason_code",
            "attempts",
            "max_attempts",
            "not_before",
            "lease_owner",
            "lease_token",
            "lease_generation",
            "lease_expires_at",
            "lease_expirations",
            "lock_contention",
            "last_error_class",
            "last_error_code",
            "last_progress_at",
            "created_at",
            "started_at",
            "completed_at",
            "updated_at",
        }
    ),
    _ITEM_TABLE: frozenset(
        {
            "generation_id",
            "item_id",
            "item_ordinal",
            "left_memory_id",
            "right_memory_id",
            "pair_key",
            "state",
            "attempt",
            "max_attempts",
            "not_before",
            "lease_owner",
            "lease_token",
            "lease_generation",
            "lease_expires_at",
            "last_error_class",
            "last_error_code",
            "last_progress_at",
            "receipt_json",
            "created_at",
            "updated_at",
            "completed_at",
        }
    ),
    _PROVENANCE_TABLE: frozenset(
        {
            "relation_identity",
            "generation_id",
            "policy_version",
            "support_kind",
            "support_entities_json",
            "evidence_hash",
            "reviewed",
            "manual",
            "created_at",
        }
    ),
}
_REQUIRED_TRIGGERS = frozenset(
    {
        "trg_relation_generation_terminal_no_revive",
        "trg_relation_generation_transition",
        "trg_relation_generation_identity_immutable",
        "trg_relation_generation_set_immutable",
        "trg_relation_generation_delete_immutable",
        "trg_relation_generation_items_insert_building",
        "trg_relation_generation_item_identity_immutable",
        "trg_relation_generation_item_policy_immutable",
        "trg_relation_generation_item_transition",
        "trg_relation_generation_item_terminal_no_revive",
        "trg_relation_generation_item_terminal_receipt_immutable",
        "trg_relation_generation_item_delete_immutable",
        "trg_relation_edge_provenance_immutable",
        "trg_relation_edge_provenance_delete_immutable",
    }
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _canonical_entities_json(value: Any) -> tuple[str, set[str]]:
    try:
        decoded = json.loads(str(value or "[]"))
    except json.JSONDecodeError as exc:
        raise ValueError("blocked entity snapshot is invalid JSON") from exc
    if not isinstance(decoded, list):
        raise ValueError("blocked entity snapshot must be a JSON list")
    entities = {str(item) for item in decoded if str(item)}
    return _canonical_json(sorted(entities)), entities


def _generation_identity(
    *,
    scope_id: str,
    relation_revision: int,
    policy_version: str,
    old_hash: str,
    new_hash: str,
) -> str:
    payload = _canonical_json(
        [scope_id, int(relation_revision), policy_version, old_hash, new_hash]
    )
    return f"rpg-{_sha256_text(payload)}"


def relation_pair_key(
    scope_id: str, relation_revision: int, left_memory_id: str, right_memory_id: str
) -> str:
    left = str(left_memory_id or "")
    right = str(right_memory_id or "")
    if not scope_id or not left or not right or left == right:
        raise ValueError("scope and two distinct memory ids are required")
    if right < left:
        left, right = right, left
    return _canonical_json([str(scope_id), int(relation_revision), left, right])


def _item_identity(pair_key: str) -> str:
    return f"rgi-{_sha256_text(pair_key)}"


def _relation_identity(source_id: str, target_id: str, relation_type: str) -> str:
    return f"rel-{_sha256_text(_canonical_json([source_id, target_id, relation_type]))}"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def ensure_relation_policy_generation_schema(conn: sqlite3.Connection) -> None:
    """Create the three additive relation-domain logical objects."""

    execute_script_transaction_neutral(
        conn,
        f"""
        CREATE TABLE IF NOT EXISTS {_GENERATION_TABLE} (
            generation_id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            scope_snapshot_json TEXT NOT NULL,
            authority_snapshot_json TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            relation_revision INTEGER NOT NULL CHECK(relation_revision >= 0),
            source_corpus_revision INTEGER NOT NULL
                CHECK(source_corpus_revision >= 0),
            frozen_upper_bound INTEGER NOT NULL CHECK(frozen_upper_bound > 0),
            old_blocked_entities_json TEXT NOT NULL,
            old_blocked_entities_sha256 TEXT NOT NULL,
            new_blocked_entities_json TEXT NOT NULL,
            new_blocked_entities_sha256 TEXT NOT NULL,
            delta_json TEXT NOT NULL,
            delta_sha256 TEXT NOT NULL,
            item_set_hash TEXT NOT NULL DEFAULT '',
            item_total INTEGER NOT NULL DEFAULT 0 CHECK(item_total >= 0),
            cursor INTEGER NOT NULL DEFAULT 0 CHECK(cursor >= 0),
            state TEXT NOT NULL CHECK(state IN (
                'building','pending','processing','retry','completed',
                'poisoned','cancelled','superseded','blocked'
            )),
            reason_code TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
            max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
            not_before TEXT NOT NULL DEFAULT '',
            lease_owner TEXT NOT NULL DEFAULT '',
            lease_token TEXT NOT NULL DEFAULT '',
            lease_generation INTEGER NOT NULL DEFAULT 0
                CHECK(lease_generation >= 0),
            lease_expires_at TEXT NOT NULL DEFAULT '',
            lease_expirations INTEGER NOT NULL DEFAULT 0
                CHECK(lease_expirations >= 0),
            lock_contention INTEGER NOT NULL DEFAULT 0
                CHECK(lock_contention >= 0),
            last_error_class TEXT NOT NULL DEFAULT '',
            last_error_code TEXT NOT NULL DEFAULT '',
            last_progress_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            UNIQUE(scope_id, relation_revision, policy_version),
            CHECK(cursor <= item_total),
            CHECK(state='building' OR length(item_set_hash)=64)
        );
        CREATE INDEX IF NOT EXISTS idx_relation_policy_generation_claim
            ON {_GENERATION_TABLE}(state, not_before, created_at, generation_id);
        CREATE INDEX IF NOT EXISTS idx_relation_policy_generation_scope
            ON {_GENERATION_TABLE}(scope_id, relation_revision, state);

        CREATE TABLE IF NOT EXISTS {_ITEM_TABLE} (
            generation_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            item_ordinal INTEGER NOT NULL CHECK(item_ordinal > 0),
            left_memory_id TEXT NOT NULL,
            right_memory_id TEXT NOT NULL,
            pair_key TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL CHECK(state IN (
                'pending','processing','retry','completed',
                'poisoned','cancelled','superseded'
            )),
            attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
            max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
            not_before TEXT NOT NULL DEFAULT '',
            lease_owner TEXT NOT NULL DEFAULT '',
            lease_token TEXT NOT NULL DEFAULT '',
            lease_generation INTEGER NOT NULL DEFAULT 0
                CHECK(lease_generation >= 0),
            lease_expires_at TEXT NOT NULL DEFAULT '',
            last_error_class TEXT NOT NULL DEFAULT '',
            last_error_code TEXT NOT NULL DEFAULT '',
            last_progress_at TEXT NOT NULL DEFAULT '',
            receipt_json TEXT NOT NULL DEFAULT '{{}}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(generation_id, item_id),
            UNIQUE(generation_id, item_ordinal),
            CHECK(left_memory_id < right_memory_id),
            CHECK(attempt <= max_attempts),
            FOREIGN KEY(generation_id)
                REFERENCES {_GENERATION_TABLE}(generation_id)
        );
        CREATE INDEX IF NOT EXISTS idx_relation_generation_item_claim
            ON {_ITEM_TABLE}(generation_id, state, not_before, item_ordinal);

        CREATE TABLE IF NOT EXISTS {_PROVENANCE_TABLE} (
            relation_identity TEXT NOT NULL,
            generation_id TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            support_kind TEXT NOT NULL,
            support_entities_json TEXT NOT NULL,
            evidence_hash TEXT NOT NULL,
            reviewed INTEGER NOT NULL DEFAULT 0 CHECK(reviewed IN (0, 1)),
            manual INTEGER NOT NULL DEFAULT 0 CHECK(manual IN (0, 1)),
            created_at TEXT NOT NULL,
            PRIMARY KEY(relation_identity, generation_id),
            FOREIGN KEY(generation_id)
                REFERENCES {_GENERATION_TABLE}(generation_id)
        );
        CREATE INDEX IF NOT EXISTS idx_relation_edge_provenance_generation
            ON {_PROVENANCE_TABLE}(generation_id, relation_identity);

        CREATE TRIGGER IF NOT EXISTS trg_relation_generation_terminal_no_revive
        BEFORE UPDATE OF state ON {_GENERATION_TABLE}
        WHEN OLD.state IN ('completed','poisoned','cancelled','superseded','blocked')
             AND NEW.state <> OLD.state
        BEGIN
            SELECT RAISE(ABORT, 'terminal relation generation cannot be revived');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_relation_generation_transition
        BEFORE UPDATE OF state ON {_GENERATION_TABLE}
        WHEN NEW.state <> OLD.state AND NOT (
             (OLD.state='building' AND NEW.state IN (
                 'pending','blocked','cancelled','superseded'
             ))
          OR (OLD.state='pending' AND NEW.state IN (
                 'processing','poisoned','cancelled','superseded'
             ))
          OR (OLD.state='processing' AND NEW.state IN (
                 'pending','retry','completed','poisoned','cancelled','superseded'
             ))
          OR (OLD.state='retry' AND NEW.state IN (
                 'processing','poisoned','cancelled','superseded'
             ))
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid relation generation transition');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_relation_generation_set_immutable
        BEFORE UPDATE OF scope_id, policy_version, relation_revision,
                         source_corpus_revision, frozen_upper_bound,
                         old_blocked_entities_json,
                         old_blocked_entities_sha256,
                         new_blocked_entities_json,
                         new_blocked_entities_sha256,
                         delta_json, delta_sha256, item_set_hash, item_total
        ON {_GENERATION_TABLE}
        WHEN OLD.state <> 'building' AND (
             NEW.scope_id <> OLD.scope_id
          OR NEW.policy_version <> OLD.policy_version
          OR NEW.relation_revision <> OLD.relation_revision
          OR NEW.source_corpus_revision <> OLD.source_corpus_revision
          OR NEW.frozen_upper_bound <> OLD.frozen_upper_bound
          OR NEW.old_blocked_entities_json <> OLD.old_blocked_entities_json
          OR NEW.old_blocked_entities_sha256 <> OLD.old_blocked_entities_sha256
          OR NEW.new_blocked_entities_json <> OLD.new_blocked_entities_json
          OR NEW.new_blocked_entities_sha256 <> OLD.new_blocked_entities_sha256
          OR NEW.delta_json <> OLD.delta_json
          OR NEW.delta_sha256 <> OLD.delta_sha256
          OR NEW.item_set_hash <> OLD.item_set_hash
          OR NEW.item_total <> OLD.item_total
        )
        BEGIN
            SELECT RAISE(ABORT, 'relation generation item set is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_relation_generation_identity_immutable
        BEFORE UPDATE OF generation_id, scope_id, idempotency_key,
                         scope_snapshot_json, authority_snapshot_json,
                         policy_version, relation_revision,
                         source_corpus_revision, frozen_upper_bound,
                         old_blocked_entities_json,
                         old_blocked_entities_sha256,
                         new_blocked_entities_json,
                         new_blocked_entities_sha256,
                         delta_json, delta_sha256, max_attempts, created_at
        ON {_GENERATION_TABLE}
        WHEN NEW.generation_id <> OLD.generation_id
          OR NEW.scope_id <> OLD.scope_id
          OR NEW.idempotency_key <> OLD.idempotency_key
          OR NEW.scope_snapshot_json <> OLD.scope_snapshot_json
          OR NEW.authority_snapshot_json <> OLD.authority_snapshot_json
          OR NEW.policy_version <> OLD.policy_version
          OR NEW.relation_revision <> OLD.relation_revision
          OR NEW.source_corpus_revision <> OLD.source_corpus_revision
          OR NEW.frozen_upper_bound <> OLD.frozen_upper_bound
          OR NEW.old_blocked_entities_json <> OLD.old_blocked_entities_json
          OR NEW.old_blocked_entities_sha256 <> OLD.old_blocked_entities_sha256
          OR NEW.new_blocked_entities_json <> OLD.new_blocked_entities_json
          OR NEW.new_blocked_entities_sha256 <> OLD.new_blocked_entities_sha256
          OR NEW.delta_json <> OLD.delta_json
          OR NEW.delta_sha256 <> OLD.delta_sha256
          OR NEW.max_attempts <> OLD.max_attempts
          OR NEW.created_at <> OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'relation generation identity is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_relation_generation_delete_immutable
        BEFORE DELETE ON {_GENERATION_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'relation generation history is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_relation_generation_items_insert_building
        BEFORE INSERT ON {_ITEM_TABLE}
        WHEN COALESCE((
            SELECT state FROM {_GENERATION_TABLE}
            WHERE generation_id=NEW.generation_id
        ), '') <> 'building'
        BEGIN
            SELECT RAISE(ABORT, 'relation generation item set is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_relation_generation_item_identity_immutable
        BEFORE UPDATE OF generation_id, item_id, item_ordinal,
                         left_memory_id, right_memory_id, pair_key,
                         max_attempts, created_at
        ON {_ITEM_TABLE}
        WHEN NEW.generation_id <> OLD.generation_id
          OR NEW.item_id <> OLD.item_id
          OR NEW.item_ordinal <> OLD.item_ordinal
          OR NEW.left_memory_id <> OLD.left_memory_id
          OR NEW.right_memory_id <> OLD.right_memory_id
          OR NEW.pair_key <> OLD.pair_key
          OR NEW.max_attempts <> OLD.max_attempts
          OR NEW.created_at <> OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'relation generation item identity is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_relation_generation_item_policy_immutable
        BEFORE UPDATE OF max_attempts, created_at ON {_ITEM_TABLE}
        WHEN NEW.max_attempts <> OLD.max_attempts
          OR NEW.created_at <> OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'relation generation item identity is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_relation_generation_item_transition
        BEFORE UPDATE OF state ON {_ITEM_TABLE}
        WHEN NEW.state <> OLD.state AND NOT (
             (OLD.state='pending' AND NEW.state IN (
                 'processing','cancelled','superseded'
             ))
          OR (OLD.state='processing' AND NEW.state IN (
                 'retry','completed','poisoned','cancelled','superseded'
             ))
          OR (OLD.state='retry' AND NEW.state IN (
                 'processing','poisoned','cancelled','superseded'
             ))
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid relation generation item transition');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_relation_generation_item_terminal_no_revive
        BEFORE UPDATE OF state ON {_ITEM_TABLE}
        WHEN OLD.state IN ('completed','poisoned','cancelled','superseded')
             AND NEW.state <> OLD.state
        BEGIN
            SELECT RAISE(ABORT, 'terminal relation generation item cannot be revived');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_relation_generation_item_terminal_receipt_immutable
        BEFORE UPDATE OF receipt_json, completed_at ON {_ITEM_TABLE}
        WHEN OLD.state IN ('completed','poisoned','cancelled','superseded')
             AND (
                  NEW.receipt_json <> OLD.receipt_json
               OR NEW.completed_at <> OLD.completed_at
             )
        BEGIN
            SELECT RAISE(ABORT, 'terminal relation generation item receipt is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_relation_generation_item_delete_immutable
        BEFORE DELETE ON {_ITEM_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'relation generation item history is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_relation_edge_provenance_immutable
        BEFORE UPDATE ON {_PROVENANCE_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'relation edge provenance is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_relation_edge_provenance_delete_immutable
        BEFORE DELETE ON {_PROVENANCE_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'relation edge provenance is immutable');
        END;
        """,
    )


def relation_policy_generation_schema_status(
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    missing_tables: list[str] = []
    missing_columns: dict[str, list[str]] = {}
    for table, required in _REQUIRED_COLUMNS.items():
        if not _table_exists(conn, table):
            missing_tables.append(table)
            continue
        columns = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        missing = sorted(required - columns)
        if missing:
            missing_columns[table] = missing
    triggers = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    missing_triggers = sorted(_REQUIRED_TRIGGERS - triggers)
    return {
        "schema_version": RELATION_POLICY_GENERATION_SCHEMA_VERSION,
        "current": not missing_tables and not missing_columns and not missing_triggers,
        "missing_tables": sorted(missing_tables),
        "missing_columns": missing_columns,
        "missing_triggers": missing_triggers,
    }


def _target_row(conn: sqlite3.Connection) -> sqlite3.Row | tuple[Any, ...] | None:
    return conn.execute(
        """
        SELECT c.scope_id, c.active_revision, c.target_revision,
               c.active_blocked_entities_json,
               c.active_blocked_entities_sha256,
               c.target_blocked_entities_json,
               c.target_blocked_entities_sha256,
               c.state, c.reason_code, c.max_attempts,
               s.corpus_revision, s.statistics_revision,
               s.blocked_entities_json, s.blocked_entities_sha256
        FROM relation_scope_containment c
        JOIN relation_scope_statistics s ON s.scope_id=c.scope_id
        WHERE c.target_revision>c.active_revision
          AND c.state='degraded'
          AND c.reason_code IN (
              'relation_policy_revision_pending',
              'relation_generation_pending',
              'relation_generation_retry',
              'relation_generation_dependency_unavailable'
          )
        ORDER BY c.updated_at, c.scope_id
        LIMIT 1
        """
    ).fetchone()


def _generation_descriptor(row: sqlite3.Row | tuple[Any, ...]) -> DurableWorkDescriptor:
    return DurableWorkDescriptor(
        work_id=str(row[0]),
        domain_type="relation_policy_generation",
        idempotency_key=str(row[1]),
        scope_snapshot=json.loads(str(row[2])),
        authority_snapshot=json.loads(str(row[3])),
        policy_version=str(row[4]),
        generation=int(row[5]),
        frozen_upper_bound=int(row[6]),
        item_set_hash=str(row[7]),
        created_at=str(row[8]),
    )


def relation_generation_descriptor(
    conn: sqlite3.Connection, generation_id: str
) -> DurableWorkDescriptor | None:
    row = conn.execute(
        f"""
        SELECT generation_id, idempotency_key, scope_snapshot_json,
               authority_snapshot_json, policy_version, relation_revision,
               frozen_upper_bound, item_set_hash, created_at
        FROM {_GENERATION_TABLE} WHERE generation_id=?
        """,
        (str(generation_id),),
    ).fetchone()
    return None if row is None else _generation_descriptor(row)


def restore_program0_relation_containment(
    conn: sqlite3.Connection,
    *,
    candidate_cap: int,
) -> int:
    """Re-stage only an exact Program 2 target for Program 0 rollback.

    In-flight Program 2 work is first terminated as superseded, then handed to
    Program 0 in the same transaction.  Poisoned Program 2 work may be handed
    back once because Program 0 owns a separate atomic execution path.  A
    cap-blocked target is handed back only when the configured cap increased
    beyond both recorded caps; if Program 0 blocks at that new cap, another
    tick cannot create an unbounded retry loop.
    """

    if not relation_policy_generation_schema_status(conn)["current"]:
        return 0
    cap = max(1, min(int(candidate_cap), 5000))
    rows = conn.execute(
        f"""
        SELECT g.generation_id, g.state,
               c.scope_id, c.active_revision, c.target_revision,
               c.active_blocked_entities_json,
               c.active_blocked_entities_sha256,
               c.target_blocked_entities_json,
               c.target_blocked_entities_sha256,
               c.reason_code, c.candidate_cap, c.state
        FROM relation_scope_containment c
        JOIN {_GENERATION_TABLE} g
          ON g.scope_id=c.scope_id
         AND g.relation_revision=c.target_revision
         AND g.policy_version=?
        JOIN relation_scope_statistics s ON s.scope_id=c.scope_id
        WHERE c.target_revision>c.active_revision
          AND g.old_blocked_entities_json=c.active_blocked_entities_json
          AND g.old_blocked_entities_sha256=c.active_blocked_entities_sha256
          AND g.new_blocked_entities_json=c.target_blocked_entities_json
          AND g.new_blocked_entities_sha256=c.target_blocked_entities_sha256
          AND s.corpus_revision=c.target_revision
          AND s.statistics_revision=c.target_revision
          AND s.blocked_entities_json=c.target_blocked_entities_json
          AND s.blocked_entities_sha256=c.target_blocked_entities_sha256
          AND (
                (
                    c.state='degraded'
                    AND c.reason_code IN (
                        'relation_generation_pending',
                        'relation_generation_retry',
                        'relation_generation_dependency_unavailable'
                    )
                    AND g.state IN ('building','pending','processing','retry')
                )
             OR (
                    c.state='blocked'
                    AND g.state IN ('blocked','poisoned')
                    AND (
                         c.reason_code IN (
                             'relation_generation_item_poisoned',
                             'relation_generation_item_set_mismatch'
                         )
                      OR (
                             c.reason_code='affected_candidate_cap_exceeded'
                         AND ?>c.candidate_cap
                         AND ?>g.frozen_upper_bound
                      )
                    )
                )
          )
        ORDER BY c.updated_at, c.scope_id
        LIMIT 1
        """,
        (RELATION_POLICY_VERSION, cap, cap),
    ).fetchall()
    restored = 0
    now = _now_iso()
    for row in rows:
        generation_id = str(row[0])
        generation_state = str(row[1])
        containment_state = str(row[11])
        savepoint = f"relation_generation_program0_rollback_{uuid.uuid4().hex}"
        conn.execute(f"SAVEPOINT {savepoint}")
        if generation_state in {"building", "pending", "processing", "retry"}:
            _supersede_generation(
                conn,
                generation_id,
                reason_code="program0_rollback",
            )
        changed = conn.execute(
            """
            UPDATE relation_scope_containment
            SET state='degraded', reason_code='relation_policy_revision_pending',
                candidate_cap=?, item_total=0, completed_items=0,
                target_attempts=0, next_attempt_at='', updated_at=?
            WHERE scope_id=? AND active_revision=? AND target_revision=?
              AND active_blocked_entities_json=?
              AND active_blocked_entities_sha256=?
              AND target_blocked_entities_json=?
              AND target_blocked_entities_sha256=?
              AND state=? AND reason_code=? AND candidate_cap=?
            """,
            (
                cap,
                now,
                str(row[2]),
                int(row[3]),
                int(row[4]),
                str(row[5]),
                str(row[6]),
                str(row[7]),
                str(row[8]),
                containment_state,
                str(row[9]),
                int(row[10]),
            ),
        ).rowcount
        if changed != 1:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            continue
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        restored += 1
    return restored


def _supersede_older_generations(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    relation_revision: int,
    now: str,
) -> int:
    rows = conn.execute(
        f"""
        SELECT generation_id FROM {_GENERATION_TABLE}
        WHERE scope_id=? AND relation_revision<?
          AND state IN ('building','pending','processing','retry')
        ORDER BY relation_revision, generation_id
        """,
        (scope_id, int(relation_revision)),
    ).fetchall()
    for row in rows:
        generation_id = str(row[0])
        conn.execute(
            f"""
            UPDATE {_ITEM_TABLE}
            SET state='superseded', lease_owner='', lease_token='',
                lease_expires_at='', completed_at=?, updated_at=?
            WHERE generation_id=?
              AND state IN ('pending','processing','retry')
            """,
            (now, now, generation_id),
        )
        conn.execute(
            f"""
            UPDATE {_GENERATION_TABLE}
            SET state='superseded', reason_code='newer_relation_revision',
                lease_owner='', lease_token='', lease_expires_at='',
                completed_at=?, updated_at=?
            WHERE generation_id=?
              AND state IN ('building','pending','processing','retry')
            """,
            (now, now, generation_id),
        )
    return len(rows)


def _validate_target_snapshots(
    row: sqlite3.Row | tuple[Any, ...],
) -> tuple[str, int, int, str, str, set[str], str, str, set[str]]:
    scope_id = str(row[0])
    active_revision = int(row[1] or 0)
    target_revision = int(row[2] or 0)
    old_json, old_entities = _canonical_entities_json(row[3])
    old_hash = str(row[4] or "")
    new_json, new_entities = _canonical_entities_json(row[5])
    new_hash = str(row[6] or "")
    if old_hash != blocked_entities_receipt_hash(
        scope_id, active_revision, old_entities
    ):
        raise ValueError("relation generation active snapshot receipt mismatch")
    if new_hash != blocked_entities_receipt_hash(
        scope_id, target_revision, new_entities
    ):
        raise ValueError("relation generation target snapshot receipt mismatch")
    corpus_revision = int(row[10] or 0)
    statistics_revision = int(row[11] or 0)
    statistics_json, statistics_entities = _canonical_entities_json(row[12])
    statistics_hash = str(row[13] or "")
    if not (
        corpus_revision == statistics_revision == target_revision
        and statistics_json == new_json
        and statistics_entities == new_entities
        and statistics_hash == new_hash
    ):
        raise RuntimeError("relation generation source corpus revision changed")
    return (
        scope_id,
        active_revision,
        target_revision,
        old_json,
        old_hash,
        old_entities,
        new_json,
        new_hash,
        new_entities,
    )


def materialize_relation_policy_generation(
    conn: sqlite3.Connection,
    *,
    candidate_cap: int = DEFAULT_RELATION_CANDIDATE_CAP,
    max_attempts: int = DEFAULT_RELATION_MAX_ATTEMPTS,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    commit: bool = True,
) -> dict[str, Any]:
    """Atomically freeze exactly one current target and its complete item set."""

    ensure_relation_policy_generation_schema(conn)
    row = _target_row(conn)
    if row is None:
        return {"status": "idle", "created": False, "item_total": 0}
    (
        scope_id,
        active_revision,
        target_revision,
        old_json,
        old_hash,
        old_entities,
        new_json,
        new_hash,
        new_entities,
    ) = _validate_target_snapshots(row)
    cap = max(1, min(int(candidate_cap), 5000))
    attempts = max(1, min(int(max_attempts), 20))
    generation_id = _generation_identity(
        scope_id=scope_id,
        relation_revision=target_revision,
        policy_version=RELATION_POLICY_VERSION,
        old_hash=old_hash,
        new_hash=new_hash,
    )
    existing = conn.execute(
        f"SELECT state, reason_code, item_total FROM {_GENERATION_TABLE} WHERE generation_id=?",
        (generation_id,),
    ).fetchone()
    if existing is not None:
        return {
            "status": str(existing[0]),
            "reason_code": str(existing[1] or ""),
            "generation_id": generation_id,
            "created": False,
            "item_total": int(existing[2] or 0),
        }

    plan: RelationContainmentPlan = plan_relation_scope_delta(
        conn,
        scope_id=scope_id,
        target_revision=target_revision,
        old_blocked_entities=old_entities,
        new_blocked_entities=new_entities,
        candidate_cap=cap,
        deadline_monotonic=deadline_monotonic,
        clock=clock,
    )
    pairs = tuple(plan.pairs)
    pair_keys = [
        relation_pair_key(scope_id, target_revision, left, right)
        for left, right in pairs
    ]
    item_set_hash = canonical_snapshot_hash({"pairs": pair_keys})
    delta = sorted(old_entities ^ new_entities)
    delta_json = _canonical_json(delta)
    now = _now_iso()
    scope_snapshot = _canonical_json(
        {"scope_id": scope_id, "relation_revision": target_revision}
    )
    authority_snapshot = _canonical_json(
        {
            "scope_id": scope_id,
            "writer_authority": "truth_writer",
            "relation_revision": target_revision,
        }
    )
    idempotency_key = _sha256_text(
        _canonical_json(
            [scope_id, target_revision, RELATION_POLICY_VERSION, old_hash, new_hash]
        )
    )
    started_transaction = not conn.in_transaction
    if started_transaction:
        conn.execute("BEGIN")
    savepoint = f"relation_generation_materialize_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        _supersede_older_generations(
            conn,
            scope_id=scope_id,
            relation_revision=target_revision,
            now=now,
        )
        conn.execute(
            f"""
            INSERT INTO {_GENERATION_TABLE}(
                generation_id, scope_id, idempotency_key, scope_snapshot_json,
                authority_snapshot_json, policy_version, relation_revision,
                source_corpus_revision, frozen_upper_bound,
                old_blocked_entities_json, old_blocked_entities_sha256,
                new_blocked_entities_json, new_blocked_entities_sha256,
                delta_json, delta_sha256, item_set_hash, item_total, cursor,
                state, reason_code, attempts, max_attempts, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, 0,
                     'building', '', 0, ?, ?, ?)
            """,
            (
                generation_id,
                scope_id,
                idempotency_key,
                scope_snapshot,
                authority_snapshot,
                RELATION_POLICY_VERSION,
                target_revision,
                target_revision,
                cap,
                old_json,
                old_hash,
                new_json,
                new_hash,
                delta_json,
                _sha256_text(delta_json),
                attempts,
                now,
                now,
            ),
        )
        if not plan.blocked:
            conn.executemany(
                f"""
                INSERT INTO {_ITEM_TABLE}(
                    generation_id, item_id, item_ordinal, left_memory_id,
                    right_memory_id, pair_key, state, attempt, max_attempts,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                [
                    (
                        generation_id,
                        _item_identity(pair_key),
                        ordinal,
                        left,
                        right,
                        pair_key,
                        attempts,
                        now,
                        now,
                    )
                    for ordinal, ((left, right), pair_key) in enumerate(
                        zip(pairs, pair_keys), 1
                    )
                ],
            )
        final_state = "blocked" if plan.blocked else "pending"
        final_reason = plan.reason_code if plan.blocked else "items_pending"
        conn.execute(
            f"""
            UPDATE {_GENERATION_TABLE}
            SET item_set_hash=?, item_total=?, state=?, reason_code=?, updated_at=?
            WHERE generation_id=? AND state='building'
            """,
            (
                item_set_hash,
                len(pairs),
                final_state,
                final_reason,
                now,
                generation_id,
            ),
        )
        if plan.blocked:
            conn.execute(
                """
                UPDATE relation_scope_containment
                SET state='blocked', reason_code=?, candidate_cap=?,
                    affected_count=?, item_total=0, completed_items=0,
                    last_attempt_at=?, updated_at=?
                WHERE scope_id=? AND active_revision=? AND target_revision=?
                  AND state='degraded'
                """,
                (
                    plan.reason_code,
                    cap,
                    int(plan.affected_count),
                    now,
                    now,
                    scope_id,
                    active_revision,
                    target_revision,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE relation_scope_containment
                SET reason_code='relation_generation_pending', candidate_cap=?,
                    affected_count=?, item_total=?, completed_items=0,
                    updated_at=?
                WHERE scope_id=? AND active_revision=? AND target_revision=?
                  AND state='degraded'
                """,
                (
                    cap,
                    int(plan.affected_count),
                    len(pairs),
                    now,
                    scope_id,
                    active_revision,
                    target_revision,
                ),
            )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if started_transaction and conn.in_transaction:
            conn.rollback()
        raise
    if commit:
        conn.commit()
    return {
        "status": final_state,
        "reason_code": final_reason,
        "generation_id": generation_id,
        "created": True,
        "item_total": len(pairs),
        "affected_count": int(plan.affected_count),
        "item_set_hash": item_set_hash,
    }


def _retry_delay_seconds(attempt: int, *, base: float, maximum: float) -> float:
    return min(maximum, base * (2 ** max(0, int(attempt) - 1)))


def _recover_expired_processing_items(
    conn: sqlite3.Connection, generation_id: str, now: str
) -> tuple[int, int]:
    rows = conn.execute(
        f"""
        SELECT item_id, attempt, max_attempts FROM {_ITEM_TABLE}
        WHERE generation_id=? AND state='processing'
        ORDER BY item_ordinal
        """,
        (generation_id,),
    ).fetchall()
    retry = 0
    poisoned = 0
    for row in rows:
        state = "poisoned" if int(row[1]) >= int(row[2]) else "retry"
        completed_at = now if state == "poisoned" else ""
        conn.execute(
            f"""
            UPDATE {_ITEM_TABLE}
            SET state=?, not_before='', lease_owner='', lease_token='',
                lease_expires_at='', last_error_class='contention',
                last_error_code='lease_expired', completed_at=?, updated_at=?
            WHERE generation_id=? AND item_id=? AND state='processing'
            """,
            (state, completed_at, now, generation_id, str(row[0])),
        )
        if state == "poisoned":
            poisoned += 1
        else:
            retry += 1
    return retry, poisoned


def _claim_generation(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    item_budget: int,
    wall_clock_budget: float,
    lease_seconds: int,
) -> tuple[str, DurableWorkLease] | None:
    now_dt = _now()
    now = now_dt.isoformat()
    row = conn.execute(
        f"""
        SELECT generation_id, state, lease_generation, lease_token,
               lease_expires_at
        FROM {_GENERATION_TABLE}
        WHERE (
              state IN ('pending','retry')
              AND (not_before='' OR not_before<=?)
        ) OR (
              state='processing'
              AND lease_expires_at<>'' AND lease_expires_at<=?
        )
        ORDER BY created_at, generation_id
        LIMIT 1
        """,
        (now, now),
    ).fetchone()
    if row is None:
        return None
    generation_id = str(row[0])
    prior_state = str(row[1])
    prior_generation = int(row[2] or 0)
    if prior_state == "processing":
        _recover_expired_processing_items(conn, generation_id, now)
    token = uuid.uuid4().hex
    next_generation = prior_generation + 1
    expires = (now_dt + timedelta(seconds=max(1, int(lease_seconds)))).isoformat()
    lease = DurableWorkLease(
        worker_id=str(worker_id or "relation-maintenance"),
        lease_token=token,
        lease_generation=next_generation,
        lease_expires_at=expires,
        bounded_item_budget=max(1, int(item_budget)),
        bounded_wall_clock_budget=max(0.01, float(wall_clock_budget)),
    )
    changed = conn.execute(
        f"""
        UPDATE {_GENERATION_TABLE}
        SET state='processing', reason_code='lease_active', attempts=attempts+1,
            lease_owner=?, lease_token=?, lease_generation=?,
            lease_expires_at=?, lease_expirations=lease_expirations+?,
            started_at=CASE WHEN started_at='' THEN ? ELSE started_at END,
            updated_at=?
        WHERE generation_id=? AND state=? AND lease_generation=?
          AND lease_token=?
        """,
        (
            lease.worker_id,
            lease.lease_token,
            lease.lease_generation,
            lease.lease_expires_at,
            int(prior_state == "processing"),
            now,
            now,
            generation_id,
            prior_state,
            prior_generation,
            str(row[3] or ""),
        ),
    ).rowcount
    return (generation_id, lease) if changed == 1 else None


def _generation_matches_current_target(
    conn: sqlite3.Connection, generation_id: str
) -> bool:
    row = conn.execute(
        f"""
        SELECT g.scope_id, g.relation_revision,
               g.old_blocked_entities_sha256,
               g.new_blocked_entities_sha256,
               c.active_revision, c.target_revision,
               c.active_blocked_entities_sha256,
               c.target_blocked_entities_sha256,
               s.corpus_revision, s.statistics_revision,
               s.blocked_entities_sha256
        FROM {_GENERATION_TABLE} g
        JOIN relation_scope_containment c ON c.scope_id=g.scope_id
        JOIN relation_scope_statistics s ON s.scope_id=g.scope_id
        WHERE g.generation_id=?
        """,
        (generation_id,),
    ).fetchone()
    return bool(
        row is not None
        and int(row[1]) == int(row[5]) == int(row[8]) == int(row[9])
        and int(row[4]) < int(row[5])
        and str(row[2]) == str(row[6])
        and str(row[3]) == str(row[7]) == str(row[10])
    )


def _generation_item_set_valid(
    conn: sqlite3.Connection, generation_id: str
) -> bool:
    generation = conn.execute(
        f"""
        SELECT scope_id, relation_revision, item_set_hash, item_total
        FROM {_GENERATION_TABLE} WHERE generation_id=?
        """,
        (generation_id,),
    ).fetchone()
    if generation is None:
        return False
    scope_id = str(generation[0])
    revision = int(generation[1])
    rows = conn.execute(
        f"""
        SELECT item_id, item_ordinal, left_memory_id, right_memory_id, pair_key
        FROM {_ITEM_TABLE} WHERE generation_id=? ORDER BY item_ordinal
        """,
        (generation_id,),
    ).fetchall()
    pair_keys: list[str] = []
    for expected_ordinal, row in enumerate(rows, 1):
        left_id = str(row[2])
        right_id = str(row[3])
        expected_pair = relation_pair_key(scope_id, revision, left_id, right_id)
        if not (
            int(row[1]) == expected_ordinal
            and str(row[4]) == expected_pair
            and str(row[0]) == _item_identity(expected_pair)
        ):
            return False
        pair_keys.append(expected_pair)
    return bool(
        len(rows) == int(generation[3] or 0)
        and canonical_snapshot_hash({"pairs": pair_keys}) == str(generation[2])
    )


def _poison_generation_item_set(
    conn: sqlite3.Connection, generation_id: str
) -> None:
    now = _now_iso()
    generation = conn.execute(
        f"SELECT scope_id, relation_revision FROM {_GENERATION_TABLE} WHERE generation_id=?",
        (generation_id,),
    ).fetchone()
    conn.execute(
        f"""
        UPDATE {_ITEM_TABLE}
        SET state='poisoned', lease_owner='', lease_token='', lease_expires_at='',
            last_error_class='poison', last_error_code='item_set_mismatch',
            completed_at=?, updated_at=?
        WHERE generation_id=? AND state='processing'
        """,
        (now, now, generation_id),
    )
    conn.execute(
        f"""
        UPDATE {_ITEM_TABLE}
        SET state='cancelled', lease_owner='', lease_token='', lease_expires_at='',
            last_error_class='poison', last_error_code='item_set_mismatch',
            completed_at=?, updated_at=?
        WHERE generation_id=? AND state IN ('pending','retry')
        """,
        (now, now, generation_id),
    )
    conn.execute(
        f"""
        UPDATE {_GENERATION_TABLE}
        SET state='poisoned', reason_code='relation_generation_item_set_mismatch',
            lease_owner='', lease_token='', lease_expires_at='',
            completed_at=?, updated_at=?
        WHERE generation_id=? AND state IN ('pending','processing','retry')
        """,
        (now, now, generation_id),
    )
    if generation is not None:
        conn.execute(
            """
            UPDATE relation_scope_containment
            SET state='blocked',
                reason_code='relation_generation_item_set_mismatch',
                last_progress_at=?, updated_at=?
            WHERE scope_id=? AND target_revision=? AND state='degraded'
            """,
            (now, now, str(generation[0]), int(generation[1])),
        )


def _supersede_generation(
    conn: sqlite3.Connection, generation_id: str, *, reason_code: str
) -> None:
    now = _now_iso()
    conn.execute(
        f"""
        UPDATE {_ITEM_TABLE}
        SET state='superseded', lease_owner='', lease_token='',
            lease_expires_at='', completed_at=?, updated_at=?
        WHERE generation_id=? AND state IN ('pending','processing','retry')
        """,
        (now, now, generation_id),
    )
    generation = conn.execute(
        f"SELECT state FROM {_GENERATION_TABLE} WHERE generation_id=?",
        (generation_id,),
    ).fetchone()
    if generation is not None and str(generation[0]) == "building":
        pair_keys = [
            str(row[0])
            for row in conn.execute(
                f"""
                SELECT pair_key FROM {_ITEM_TABLE}
                WHERE generation_id=? ORDER BY item_ordinal
                """,
                (generation_id,),
            )
        ]
        conn.execute(
            f"""
            UPDATE {_GENERATION_TABLE}
            SET item_set_hash=?, item_total=?, state='superseded', reason_code=?,
                lease_owner='', lease_token='', lease_expires_at='',
                completed_at=?, updated_at=?
            WHERE generation_id=? AND state='building'
            """,
            (
                canonical_snapshot_hash({"pairs": pair_keys}),
                len(pair_keys),
                reason_code,
                now,
                now,
                generation_id,
            ),
        )
        return
    conn.execute(
        f"""
        UPDATE {_GENERATION_TABLE}
        SET state='superseded', reason_code=?, lease_owner='', lease_token='',
            lease_expires_at='', completed_at=?, updated_at=?
        WHERE generation_id=? AND state IN ('pending','processing','retry')
        """,
        (reason_code, now, now, generation_id),
    )


def _record_edge_provenance(
    conn: sqlite3.Connection,
    *,
    generation_id: str,
    item_id: str,
    left_id: str,
    right_id: str,
    batch_id: str,
    support_entities_json: str,
) -> list[str]:
    prefix = f"relation-extraction:{batch_id}%"
    rows = conn.execute(
        """
        SELECT source_memory_id, target_memory_id, relation_type
        FROM memory_relations
        WHERE ((source_memory_id=? AND target_memory_id=?)
            OR (source_memory_id=? AND target_memory_id=?))
          AND LOWER(COALESCE(note, '')) LIKE LOWER(?)
        ORDER BY source_memory_id, target_memory_id, relation_type
        """,
        (left_id, right_id, right_id, left_id, prefix),
    ).fetchall()
    now = _now_iso()
    identities: list[str] = []
    for row in rows:
        source_id = str(row[0])
        target_id = str(row[1])
        relation_type = str(row[2])
        identity = _relation_identity(source_id, target_id, relation_type)
        evidence_hash = _sha256_text(
            _canonical_json(
                [generation_id, item_id, source_id, target_id, relation_type]
            )
        )
        conn.execute(
            f"""
            INSERT OR IGNORE INTO {_PROVENANCE_TABLE}(
                relation_identity, generation_id, policy_version,
                support_kind, support_entities_json, evidence_hash,
                reviewed, manual, created_at
            ) VALUES(?, ?, ?, 'policy_delta', ?, ?, 0, 0, ?)
            """,
            (
                identity,
                generation_id,
                RELATION_POLICY_VERSION,
                support_entities_json,
                evidence_hash,
                now,
            ),
        )
        identities.append(identity)
    return identities


def _fail_item(
    conn: sqlite3.Connection,
    *,
    generation_id: str,
    item_id: str,
    lease: DurableWorkLease,
    error_class: str,
    error_code: str,
    backoff_base_seconds: float,
    backoff_max_seconds: float,
) -> str:
    row = conn.execute(
        f"SELECT attempt, max_attempts FROM {_ITEM_TABLE} WHERE generation_id=? AND item_id=?",
        (generation_id, item_id),
    ).fetchone()
    if row is None:
        return "superseded"
    attempt = int(row[0] or 0)
    permanent = error_class in {"permanent", "poison", "authority_revoked", "epoch_mismatch"}
    exhausted = permanent or attempt >= int(row[1] or 1)
    state = "poisoned" if exhausted else "retry"
    now = _now()
    not_before = ""
    completed_at = ""
    if exhausted:
        completed_at = now.isoformat()
    else:
        delay = _retry_delay_seconds(
            attempt,
            base=max(0.1, float(backoff_base_seconds)),
            maximum=max(float(backoff_base_seconds), float(backoff_max_seconds)),
        )
        not_before = (now + timedelta(seconds=delay)).isoformat()
    changed = conn.execute(
        f"""
        UPDATE {_ITEM_TABLE}
        SET state=?, not_before=?, lease_owner='', lease_token='',
            lease_expires_at='', last_error_class=?, last_error_code=?,
            completed_at=?, updated_at=?
        WHERE generation_id=? AND item_id=? AND state='processing'
          AND lease_owner=? AND lease_token=? AND lease_generation=?
        """,
        (
            state,
            not_before,
            error_class,
            error_code,
            completed_at,
            now.isoformat(),
            generation_id,
            item_id,
            lease.worker_id,
            lease.lease_token,
            lease.lease_generation,
        ),
    ).rowcount
    return state if changed == 1 else "superseded"


def _item_counts(conn: sqlite3.Connection, generation_id: str) -> dict[str, int]:
    counts = {state: 0 for state in DURABLE_WORK_ITEM_STATES}
    for row in conn.execute(
        f"SELECT state, COUNT(*) FROM {_ITEM_TABLE} WHERE generation_id=? GROUP BY state",
        (generation_id,),
    ).fetchall():
        counts[str(row[0])] = int(row[1] or 0)
    return counts


def _monotonic_cursor(conn: sqlite3.Connection, generation_id: str) -> int:
    row = conn.execute(
        f"""
        SELECT MIN(item_ordinal) FROM {_ITEM_TABLE}
        WHERE generation_id=? AND state NOT IN ('completed','poisoned','cancelled','superseded')
        """,
        (generation_id,),
    ).fetchone()
    if row is None or row[0] is None:
        total = conn.execute(
            f"SELECT item_total FROM {_GENERATION_TABLE} WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
        return int(total[0] or 0) if total is not None else 0
    return max(0, int(row[0]) - 1)


def _finish_generation(
    conn: sqlite3.Connection,
    *,
    generation_id: str,
    lease: DurableWorkLease,
) -> tuple[str, dict[str, int]]:
    counts = _item_counts(conn, generation_id)
    cursor = _monotonic_cursor(conn, generation_id)
    now = _now_iso()
    poisoned = int(counts.get("poisoned", 0))
    runnable = sum(int(counts.get(key, 0)) for key in ("pending", "processing", "retry"))
    if poisoned:
        state = "poisoned"
        reason = "relation_generation_item_poisoned"
        completed_at = now
    elif runnable == 0:
        state = "completed"
        reason = ""
        completed_at = now
    elif int(counts.get("pending", 0)) or int(counts.get("processing", 0)):
        state = "pending"
        reason = "items_pending"
        completed_at = ""
    elif int(counts.get("retry", 0)):
        state = "retry"
        reason = "items_retryable"
        completed_at = ""
    else:
        state = "pending"
        reason = "items_pending"
        completed_at = ""
    not_before = ""
    if state == "retry":
        retry_due = conn.execute(
            f"""
            SELECT COALESCE(MIN(not_before), '') FROM {_ITEM_TABLE}
            WHERE generation_id=? AND state='retry'
            """,
            (generation_id,),
        ).fetchone()
        not_before = str(retry_due[0] or "") if retry_due is not None else ""
    if state == "completed" and not _generation_matches_current_target(
        conn, generation_id
    ):
        _supersede_generation(
            conn,
            generation_id,
            reason_code="relation_generation_epoch_mismatch",
        )
        return "superseded", counts
    generation = conn.execute(
        f"""
        SELECT scope_id, relation_revision, new_blocked_entities_json,
               new_blocked_entities_sha256, item_total
        FROM {_GENERATION_TABLE} WHERE generation_id=?
        """,
        (generation_id,),
    ).fetchone()
    if generation is None:
        return "superseded", counts
    scope_id = str(generation[0])
    relation_revision = int(generation[1])
    savepoint = f"relation_generation_finish_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {savepoint}")
    changed = conn.execute(
        f"""
        UPDATE {_GENERATION_TABLE}
        SET state=?, reason_code=?, cursor=CASE WHEN cursor<? THEN ? ELSE cursor END,
            not_before=?,
            lease_owner='', lease_token='', lease_expires_at='',
            completed_at=?, last_progress_at=?, updated_at=?
        WHERE generation_id=? AND state='processing'
          AND lease_owner=? AND lease_token=? AND lease_generation=?
        """,
        (
            state,
            reason,
            cursor,
            cursor,
            not_before,
            completed_at,
            now,
            now,
            generation_id,
            lease.worker_id,
            lease.lease_token,
            lease.lease_generation,
        ),
    ).rowcount
    if changed != 1:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return "superseded", counts
    if state == "poisoned":
        conn.execute(
            """
            UPDATE relation_scope_containment
            SET state='blocked', reason_code='relation_generation_item_poisoned',
                completed_items=?, last_progress_at=?, updated_at=?
            WHERE scope_id=? AND target_revision=? AND state='degraded'
            """,
            (
                int(counts.get("completed", 0)),
                now,
                now,
                scope_id,
                relation_revision,
            ),
        )
    elif state == "completed":
        focus_pending = relation_focus_scope_has_debt(conn, scope_id)
        containment_state = "degraded" if focus_pending else "ready"
        containment_reason = "focus_relation_sync_pending" if focus_pending else ""
        containment_changed = conn.execute(
            """
            UPDATE relation_scope_containment
            SET state=?, reason_code=?, active_revision=target_revision,
                active_blocked_entities_json=target_blocked_entities_json,
                active_blocked_entities_sha256=target_blocked_entities_sha256,
                completed_items=item_total, attempts_total=attempts_total+1,
                target_attempts=target_attempts+1, next_attempt_at='',
                last_attempt_at=?, last_progress_at=?, updated_at=?
            WHERE scope_id=? AND target_revision=? AND state='degraded'
              AND target_blocked_entities_json=?
              AND target_blocked_entities_sha256=?
            """,
            (
                containment_state,
                containment_reason,
                now,
                now,
                now,
                scope_id,
                relation_revision,
                str(generation[2]),
                str(generation[3]),
            ),
        ).rowcount
        if containment_changed != 1:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            _supersede_generation(
                conn,
                generation_id,
                reason_code="relation_generation_epoch_mismatch",
            )
            return "superseded", counts
        state = containment_state
    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    return state, counts


def drain_relation_policy_generation(
    conn: sqlite3.Connection,
    *,
    worker_id: str = "relation-maintenance",
    item_limit: int = 250,
    candidate_cap: int = DEFAULT_RELATION_CANDIDATE_CAP,
    max_attempts: int = DEFAULT_RELATION_MAX_ATTEMPTS,
    wall_clock_seconds: float = 0.5,
    lease_seconds: int | None = None,
    backoff_base_seconds: float = DEFAULT_RELATION_BACKOFF_BASE_SECONDS,
    backoff_max_seconds: float = DEFAULT_RELATION_BACKOFF_MAX_SECONDS,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    commit: bool = True,
) -> dict[str, Any]:
    """Materialize, lease, and execute one bounded finite generation batch."""

    ensure_relation_policy_generation_schema(conn)
    started_transaction = not conn.in_transaction
    if started_transaction:
        conn.execute("BEGIN")
    deadline = (
        float(deadline_monotonic)
        if deadline_monotonic is not None
        else clock() + max(0.01, float(wall_clock_seconds))
    )
    try:
        materialized = materialize_relation_policy_generation(
            conn,
            candidate_cap=candidate_cap,
            max_attempts=max_attempts,
            deadline_monotonic=deadline,
            clock=clock,
            commit=False,
        )
        if str(materialized.get("status")) == "blocked":
            if commit:
                conn.commit()
            return {
                **materialized,
                "attempted": 0,
                "completed": 0,
                "failed": 1,
            }
        if clock() >= deadline:
            if commit:
                conn.commit()
            return {
                "status": "deferred_budget",
                "reason_code": "shared_maintenance_budget_exhausted",
                "attempted": 0,
                "completed": 0,
                "failed": 0,
            }
        bounded_limit = max(1, min(int(item_limit), 1000))
        lease_duration = (
            max(1, int(lease_seconds))
            if lease_seconds is not None
            else max(1, min(300, int(math.ceil(max(0.01, wall_clock_seconds) * 4))))
        )
        claimed = _claim_generation(
            conn,
            worker_id=worker_id,
            item_budget=bounded_limit,
            wall_clock_budget=max(0.01, float(wall_clock_seconds)),
            lease_seconds=lease_duration,
        )
        if claimed is None:
            if commit:
                conn.commit()
            return {
                "status": str(materialized.get("status") or "idle"),
                "reason_code": str(materialized.get("reason_code") or ""),
                "attempted": 0,
                "completed": 0,
                "failed": 0,
            }
        generation_id, lease = claimed
        if not _generation_matches_current_target(conn, generation_id):
            _supersede_generation(
                conn, generation_id, reason_code="relation_generation_epoch_mismatch"
            )
            if commit:
                conn.commit()
            return {
                "status": "superseded",
                "reason_code": "relation_generation_epoch_mismatch",
                "generation_id": generation_id,
                "attempted": 0,
                "completed": 0,
                "failed": 0,
            }
        if not _generation_item_set_valid(conn, generation_id):
            _poison_generation_item_set(conn, generation_id)
            if commit:
                conn.commit()
            return {
                "status": "blocked",
                "reason_code": "relation_generation_item_set_mismatch",
                "generation_id": generation_id,
                "attempted": 0,
                "completed": 0,
                "failed": 1,
            }
        generation = conn.execute(
            f"""
            SELECT delta_json, new_blocked_entities_json
            FROM {_GENERATION_TABLE} WHERE generation_id=?
            """,
            (generation_id,),
        ).fetchone()
        support_entities_json = str(generation[0] if generation is not None else "[]")
        blocked_entities = set(
            json.loads(str(generation[1] if generation is not None else "[]"))
        )
        items = conn.execute(
            f"""
            SELECT item_id, left_memory_id, right_memory_id
            FROM {_ITEM_TABLE}
            WHERE generation_id=? AND state IN ('pending','retry')
              AND (not_before='' OR not_before<=?)
            ORDER BY item_ordinal
            LIMIT ?
            """,
            (generation_id, _now_iso(), bounded_limit),
        ).fetchall()
        attempted = 0
        completed = 0
        failed = 0
        for row in items:
            if clock() >= deadline:
                break
            item_id = str(row[0])
            left_id = str(row[1])
            right_id = str(row[2])
            marked = conn.execute(
                f"""
                UPDATE {_ITEM_TABLE}
                SET state='processing', attempt=attempt+1,
                    lease_owner=?, lease_token=?, lease_generation=?,
                    lease_expires_at=?, updated_at=?
                WHERE generation_id=? AND item_id=?
                  AND state IN ('pending','retry') AND attempt<max_attempts
                """,
                (
                    lease.worker_id,
                    lease.lease_token,
                    lease.lease_generation,
                    lease.lease_expires_at,
                    _now_iso(),
                    generation_id,
                    item_id,
                ),
            ).rowcount
            if marked != 1:
                continue
            attempted += 1
            savepoint = f"relation_generation_item_{uuid.uuid4().hex}"
            conn.execute(f"SAVEPOINT {savepoint}")
            try:
                from .relation_extraction import rebuild_extracted_relations

                batch_id = f"generation-{generation_id}-{item_id}"
                preview = rebuild_extracted_relations(
                    conn,
                    memory_ids=[left_id, right_id],
                    focus_memory_ids=[left_id],
                    dry_run=True,
                    batch_id=f"{batch_id}-preview",
                    max_pairs=1,
                    max_candidates=24,
                    commit=False,
                    blocked_entities=blocked_entities,
                )
                if not bool(preview.get("ok")):
                    raise RuntimeError(
                        str(preview.get("error") or "relation generation preview failed")
                    )
                if clock() >= deadline:
                    raise TimeoutError("relation generation wall-clock budget exceeded")
                if not _generation_matches_current_target(conn, generation_id):
                    conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    _supersede_generation(
                        conn,
                        generation_id,
                        reason_code="relation_generation_epoch_mismatch",
                    )
                    if commit:
                        conn.commit()
                    return {
                        "status": "superseded",
                        "reason_code": "relation_generation_epoch_mismatch",
                        "generation_id": generation_id,
                        "attempted": attempted,
                        "completed": completed,
                        "failed": failed,
                    }
                applied = rebuild_extracted_relations(
                    conn,
                    memory_ids=[left_id, right_id],
                    focus_memory_ids=[left_id],
                    dry_run=False,
                    batch_id=batch_id,
                    max_pairs=1,
                    max_candidates=24,
                    commit=False,
                    blocked_entities=blocked_entities,
                )
                if not bool(applied.get("ok")):
                    raise RuntimeError(
                        str(applied.get("error") or "relation generation apply failed")
                    )
                relation_ids = _record_edge_provenance(
                    conn,
                    generation_id=generation_id,
                    item_id=item_id,
                    left_id=left_id,
                    right_id=right_id,
                    batch_id=batch_id,
                    support_entities_json=support_entities_json,
                )
                now = _now_iso()
                receipt = _canonical_json(
                    {
                        "generation_id": generation_id,
                        "item_id": item_id,
                        "relation_identities": relation_ids,
                        "inserted": int(applied.get("inserted") or 0),
                        "deleted": int(applied.get("deleted") or 0),
                    }
                )
                changed = conn.execute(
                    f"""
                    UPDATE {_ITEM_TABLE}
                    SET state='completed', lease_owner='', lease_token='',
                        lease_expires_at='', last_error_class='',
                        last_error_code='', last_progress_at=?, receipt_json=?,
                        completed_at=?, updated_at=?
                    WHERE generation_id=? AND item_id=? AND state='processing'
                      AND lease_owner=? AND lease_token=? AND lease_generation=?
                    """,
                    (
                        now,
                        receipt,
                        now,
                        now,
                        generation_id,
                        item_id,
                        lease.worker_id,
                        lease.lease_token,
                        lease.lease_generation,
                    ),
                ).rowcount
                if changed != 1:
                    raise RuntimeError("relation generation item lease changed")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                completed += 1
            except Exception as exc:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                if is_sqlite_lock_contention(exc):
                    error_class = "contention"
                    error_code = "sqlite_lock_contention"
                    conn.execute(
                        f"UPDATE {_GENERATION_TABLE} SET lock_contention=lock_contention+1 WHERE generation_id=?",
                        (generation_id,),
                    )
                elif isinstance(exc, TimeoutError):
                    error_class = "retriable"
                    error_code = "maintenance_budget_exceeded"
                else:
                    error_class = "retriable"
                    error_code = "relation_generation_apply_failed"
                _fail_item(
                    conn,
                    generation_id=generation_id,
                    item_id=item_id,
                    lease=lease,
                    error_class=error_class,
                    error_code=error_code,
                    backoff_base_seconds=backoff_base_seconds,
                    backoff_max_seconds=backoff_max_seconds,
                )
                failed += 1
                break
        final_state, counts = _finish_generation(
            conn, generation_id=generation_id, lease=lease
        )
        if commit:
            conn.commit()
        return {
            "status": final_state,
            "reason_code": "" if final_state == "ready" else final_state,
            "generation_id": generation_id,
            "attempted": attempted,
            "completed": completed,
            "failed": failed,
            "item_counts": counts,
        }
    except Exception:
        if started_transaction and conn.in_transaction:
            conn.rollback()
        raise


def _age_seconds(value: Any) -> float:
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (_now() - parsed.astimezone(timezone.utc)).total_seconds())


def relation_policy_generation_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return the shared content-free health contract for relation generations."""

    schema = relation_policy_generation_schema_status(conn)
    if not schema["current"]:
        return {
            **durable_work_health(
                domain_type="relation_policy_generation",
                state="disabled",
                reason_code="schema_missing",
                auto_recoverable=False,
                operator_action_required=False,
            ),
            "generation_counts": {},
            "stale_generation_count": 0,
            **schema,
        }
    generation_counts = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            f"SELECT state, COUNT(*) FROM {_GENERATION_TABLE} GROUP BY state"
        ).fetchall()
    }
    active_generation_counts = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            f"""
            SELECT g.state, COUNT(*)
            FROM {_GENERATION_TABLE} g
            JOIN relation_scope_containment c
              ON c.scope_id=g.scope_id
             AND c.target_revision=g.relation_revision
            WHERE c.target_revision>c.active_revision
            GROUP BY g.state
            """
        ).fetchall()
    }
    item_counts = {state: 0 for state in DURABLE_WORK_ITEM_STATES}
    for row in conn.execute(
        f"""
        SELECT i.state, COUNT(*)
        FROM {_ITEM_TABLE} i
        JOIN {_GENERATION_TABLE} g ON g.generation_id=i.generation_id
        JOIN relation_scope_containment c
          ON c.scope_id=g.scope_id
         AND c.target_revision=g.relation_revision
        WHERE c.target_revision>c.active_revision
        GROUP BY i.state
        """
    ).fetchall():
        item_counts[str(row[0])] = int(row[1])
    oldest = conn.execute(
        f"""
        SELECT MIN(g.created_at)
        FROM {_GENERATION_TABLE} g
        JOIN relation_scope_containment c
          ON c.scope_id=g.scope_id
         AND c.target_revision=g.relation_revision
        WHERE c.target_revision>c.active_revision
          AND g.state IN ('building','pending','processing','retry','poisoned','blocked')
        """
    ).fetchone()
    progress = conn.execute(
        f"""
        SELECT COALESCE(MAX(g.last_progress_at), ''),
               COALESCE(SUM(g.lease_expirations), 0),
               COALESCE(SUM(g.lock_contention), 0),
               COALESCE(SUM(g.cursor), 0), COALESCE(SUM(g.item_total), 0)
        FROM {_GENERATION_TABLE} g
        JOIN relation_scope_containment c
          ON c.scope_id=g.scope_id
         AND c.target_revision=g.relation_revision
        WHERE c.target_revision>c.active_revision
        """
    ).fetchone()
    poisoned = int(active_generation_counts.get("poisoned", 0))
    blocked = int(active_generation_counts.get("blocked", 0))
    runnable = sum(
        int(active_generation_counts.get(key, 0))
        for key in ("building", "pending", "processing", "retry")
    )
    if poisoned or blocked:
        state = "blocked"
        reason = "operator_action_required"
        auto_recoverable = False
        operator_required = True
    elif runnable:
        state = "degraded"
        reason = "relation_generation_debt"
        auto_recoverable = True
        operator_required = False
    else:
        state = "ready"
        reason = "healthy"
        auto_recoverable = True
        operator_required = False
    oldest_value = str(oldest[0] or "") if oldest is not None else ""
    total = int(progress[4] or 0) if progress is not None else 0
    cursor = int(progress[3] or 0) if progress is not None else 0
    return {
        **durable_work_health(
            domain_type="relation_policy_generation",
            state=state,
            reason_code=reason,
            item_counts=item_counts,
            oldest_age_seconds=_age_seconds(oldest_value),
            last_progress_at=str(progress[0] or "") if progress is not None else "",
            progress_rate=(cursor / total) if total else 0.0,
            lease_expirations=int(progress[1] or 0) if progress is not None else 0,
            lock_contention=int(progress[2] or 0) if progress is not None else 0,
            auto_recoverable=auto_recoverable,
            operator_action_required=operator_required,
            fairness={
                "scope": "oldest_generation_first",
                "domain": "relation",
                "foreground_pressure": "bounded_wall_clock",
            },
        ),
        "generation_counts": generation_counts,
        "active_generation_counts": active_generation_counts,
        "stale_generation_count": int(generation_counts.get("superseded", 0)),
        **schema,
    }


__all__ = [
    "RELATION_GENERATION_STATES",
    "RELATION_GENERATION_TERMINAL_STATES",
    "RELATION_POLICY_GENERATION_MIGRATION_DESCRIPTION",
    "RELATION_POLICY_GENERATION_MIGRATION_ID",
    "RELATION_POLICY_GENERATION_MIGRATION_PLUGIN_VERSION",
    "RELATION_POLICY_GENERATION_SCHEMA_VERSION",
    "RELATION_POLICY_VERSION",
    "drain_relation_policy_generation",
    "ensure_relation_policy_generation_schema",
    "materialize_relation_policy_generation",
    "relation_generation_descriptor",
    "relation_pair_key",
    "relation_policy_generation_report",
    "relation_policy_generation_schema_status",
    "restore_program0_relation_containment",
]
