"""SQLite truth-store schema, migration, and row-level helper functions.

This is the authoritative durable store; companion vector/graph state must be rebuildable from rows managed here."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifacts import enrich_content_with_artifact_anchors, merge_artifact_metadata
from .capture_filters import (
    classify_transport_noise,
    contains_secret_like_text,
    sanitize_capture_text,
    sanitize_report_text,
    sanitize_structured_value,
)
from .gating import compact_text, dedup_key
from .governance import classify_memory, merge_metadata
from .graph import backfill_memory_entities, ensure_graph_schema, sync_memory_entities
from .fact_repository import (
    FACT_EXECUTOR_MUTATION_AUTHORITY,
    require_fact_mutation_authority,
)
from .freshness import upsert_memory_freshness
from .lifecycle_policy import (
    _metadata_lifecycle_expr,
    _normalized_sql_token,
    ordinary_recall_lifecycle_visible,
    ordinary_recall_lifecycle_visible_sql,
)
from .lexical_generation import (
    LEXICAL_MIGRATION_DESCRIPTION,
    LEXICAL_MIGRATION_ID,
    LEXICAL_MIGRATION_PLUGIN_VERSION,
    LEXICAL_SCHEMA_VERSION,
    ensure_lexical_generation_schema,
    lexical_schema_status,
)
from .operator_ledger import (
    OPERATOR_LEDGER_MIGRATION_DESCRIPTION,
    OPERATOR_LEDGER_MIGRATION_ID,
    OPERATOR_LEDGER_MIGRATION_PLUGIN_VERSION,
    OPERATOR_LEDGER_SCHEMA_VERSION,
    ensure_operator_ledger_schema,
    operator_ledger_schema_status,
)
from .privacy_purge_schema import (
    PRIVACY_PURGE_MIGRATION_DESCRIPTION,
    PRIVACY_PURGE_MIGRATION_ID,
    PRIVACY_PURGE_MIGRATION_PLUGIN_VERSION,
    PRIVACY_PURGE_SCHEMA_VERSION,
    ensure_privacy_purge_schema,
    privacy_purge_schema_status,
)
from .relation_containment import (
    RELATION_CONTAINMENT_MIGRATION_DESCRIPTION,
    RELATION_CONTAINMENT_MIGRATION_ID,
    RELATION_CONTAINMENT_MIGRATION_PLUGIN_VERSION,
    RELATION_CONTAINMENT_SCHEMA_VERSION,
    ensure_relation_containment_schema,
    relation_containment_schema_status,
)
from .relation_policy_generation import (
    RELATION_POLICY_GENERATION_MIGRATION_DESCRIPTION,
    RELATION_POLICY_GENERATION_MIGRATION_ID,
    RELATION_POLICY_GENERATION_MIGRATION_PLUGIN_VERSION,
    RELATION_POLICY_GENERATION_SCHEMA_VERSION,
    ensure_relation_policy_generation_schema,
    relation_policy_generation_schema_status,
)
from .relation_frequency_index import (
    RELATION_FREQUENCY_FAILURE_MIGRATION_DESCRIPTION,
    RELATION_FREQUENCY_FAILURE_MIGRATION_ID,
    RELATION_FREQUENCY_FAILURE_MIGRATION_PLUGIN_VERSION,
    RELATION_FREQUENCY_FAILURE_SCHEMA_VERSION,
    RELATION_FREQUENCY_INDEX_MIGRATION_DESCRIPTION,
    RELATION_FREQUENCY_INDEX_MIGRATION_ID,
    RELATION_FREQUENCY_INDEX_MIGRATION_PLUGIN_VERSION,
    RELATION_FREQUENCY_INDEX_SCHEMA_VERSION,
    ensure_relation_frequency_index_schema,
    relation_frequency_index_schema_status,
    sync_relation_frequency_memory,
)
from .sqlite_params import chunked_sql_parameters
from .relation_rebuild_queue import (
    RELATION_REBUILD_EXPIRY_MIGRATION_DESCRIPTION,
    RELATION_REBUILD_EXPIRY_MIGRATION_ID,
    RELATION_REBUILD_EXPIRY_MIGRATION_PLUGIN_VERSION,
    RELATION_REBUILD_EXPIRY_SCHEMA_VERSION,
    RELATION_REBUILD_LEASE_MIGRATION_DESCRIPTION,
    RELATION_REBUILD_LEASE_MIGRATION_ID,
    RELATION_REBUILD_LEASE_MIGRATION_PLUGIN_VERSION,
    RELATION_REBUILD_LEASE_SCHEMA_VERSION,
    RELATION_REBUILD_MIGRATION_DESCRIPTION,
    RELATION_REBUILD_MIGRATION_ID,
    RELATION_REBUILD_MIGRATION_PLUGIN_VERSION,
    RELATION_REBUILD_SCHEMA_VERSION,
    RELATION_REBUILD_PROGRESS_MIGRATION_DESCRIPTION,
    RELATION_REBUILD_PROGRESS_MIGRATION_ID,
    RELATION_REBUILD_PROGRESS_MIGRATION_PLUGIN_VERSION,
    RELATION_REBUILD_PROGRESS_SCHEMA_VERSION,
    ensure_relation_rebuild_schema,
    relation_rebuild_schema_status,
)
from .relation_scope_state import (
    RELATION_SCOPE_RECEIPT_MIGRATION_DESCRIPTION,
    RELATION_SCOPE_RECEIPT_MIGRATION_ID,
    RELATION_SCOPE_RECEIPT_MIGRATION_PLUGIN_VERSION,
    RELATION_SCOPE_RECEIPT_SCHEMA_VERSION,
)
from .sqlite_schema import execute_script_transaction_neutral
from .temporal_facts import (
    FACT_CLAIMS_MIGRATION_DESCRIPTION,
    FACT_CLAIMS_MIGRATION_ID,
    FACT_CLAIMS_MIGRATION_PLUGIN_VERSION,
    FACT_CLAIMS_SCHEMA_VERSION,
    ensure_temporal_fact_schema,
    temporal_fact_schema_status,
)
from .vector_generation import enqueue_current_vector_event
from .vector_reconciliation import (
    VECTOR_RECONCILIATION_MIGRATION_DESCRIPTION,
    VECTOR_RECONCILIATION_MIGRATION_ID,
    VECTOR_RECONCILIATION_MIGRATION_PLUGIN_VERSION,
    VECTOR_RECONCILIATION_SCHEMA_VERSION,
    ensure_vector_reconciliation_schema,
    vector_reconciliation_schema_status,
)

ENTRY_DELIMITER = "\n§\n"
SCHEMA_VERSION = PRIVACY_PURGE_SCHEMA_VERSION
BASELINE_SCHEMA_VERSION = 10600
BASELINE_MIGRATION_ID = "0001_baseline_v1_6_0"
BASELINE_MIGRATION_PLUGIN_VERSION = "1.6.0"
BASELINE_MIGRATION_DESCRIPTION = "Baseline schema ledger for scope-recall v1.6.0"


class UnsupportedSchemaVersionError(RuntimeError):
    """Raised before writes when a database was created by newer code."""


EXPECTED_SCHEMA_MIGRATIONS: tuple[dict[str, Any], ...] = (
    {
        "id": BASELINE_MIGRATION_ID,
        "plugin_version": BASELINE_MIGRATION_PLUGIN_VERSION,
        "description": BASELINE_MIGRATION_DESCRIPTION,
        "schema_version": BASELINE_SCHEMA_VERSION,
    },
    {
        "id": FACT_CLAIMS_MIGRATION_ID,
        "plugin_version": FACT_CLAIMS_MIGRATION_PLUGIN_VERSION,
        "description": FACT_CLAIMS_MIGRATION_DESCRIPTION,
        "schema_version": FACT_CLAIMS_SCHEMA_VERSION,
    },
    {
        "id": RELATION_REBUILD_MIGRATION_ID,
        "plugin_version": RELATION_REBUILD_MIGRATION_PLUGIN_VERSION,
        "description": RELATION_REBUILD_MIGRATION_DESCRIPTION,
        "schema_version": RELATION_REBUILD_SCHEMA_VERSION,
    },
    {
        "id": OPERATOR_LEDGER_MIGRATION_ID,
        "plugin_version": OPERATOR_LEDGER_MIGRATION_PLUGIN_VERSION,
        "description": OPERATOR_LEDGER_MIGRATION_DESCRIPTION,
        "schema_version": OPERATOR_LEDGER_SCHEMA_VERSION,
    },
    {
        "id": RELATION_REBUILD_LEASE_MIGRATION_ID,
        "plugin_version": RELATION_REBUILD_LEASE_MIGRATION_PLUGIN_VERSION,
        "description": RELATION_REBUILD_LEASE_MIGRATION_DESCRIPTION,
        "schema_version": RELATION_REBUILD_LEASE_SCHEMA_VERSION,
    },
    {
        "id": RELATION_SCOPE_RECEIPT_MIGRATION_ID,
        "plugin_version": RELATION_SCOPE_RECEIPT_MIGRATION_PLUGIN_VERSION,
        "description": RELATION_SCOPE_RECEIPT_MIGRATION_DESCRIPTION,
        "schema_version": RELATION_SCOPE_RECEIPT_SCHEMA_VERSION,
    },
    {
        "id": RELATION_FREQUENCY_INDEX_MIGRATION_ID,
        "plugin_version": RELATION_FREQUENCY_INDEX_MIGRATION_PLUGIN_VERSION,
        "description": RELATION_FREQUENCY_INDEX_MIGRATION_DESCRIPTION,
        "schema_version": RELATION_FREQUENCY_INDEX_SCHEMA_VERSION,
    },
    {
        "id": RELATION_REBUILD_PROGRESS_MIGRATION_ID,
        "plugin_version": RELATION_REBUILD_PROGRESS_MIGRATION_PLUGIN_VERSION,
        "description": RELATION_REBUILD_PROGRESS_MIGRATION_DESCRIPTION,
        "schema_version": RELATION_REBUILD_PROGRESS_SCHEMA_VERSION,
    },
    {
        "id": VECTOR_RECONCILIATION_MIGRATION_ID,
        "plugin_version": VECTOR_RECONCILIATION_MIGRATION_PLUGIN_VERSION,
        "description": VECTOR_RECONCILIATION_MIGRATION_DESCRIPTION,
        "schema_version": VECTOR_RECONCILIATION_SCHEMA_VERSION,
    },
    {
        "id": RELATION_REBUILD_EXPIRY_MIGRATION_ID,
        "plugin_version": RELATION_REBUILD_EXPIRY_MIGRATION_PLUGIN_VERSION,
        "description": RELATION_REBUILD_EXPIRY_MIGRATION_DESCRIPTION,
        "schema_version": RELATION_REBUILD_EXPIRY_SCHEMA_VERSION,
    },
    {
        "id": RELATION_FREQUENCY_FAILURE_MIGRATION_ID,
        "plugin_version": RELATION_FREQUENCY_FAILURE_MIGRATION_PLUGIN_VERSION,
        "description": RELATION_FREQUENCY_FAILURE_MIGRATION_DESCRIPTION,
        "schema_version": RELATION_FREQUENCY_FAILURE_SCHEMA_VERSION,
    },
    {
        "id": LEXICAL_MIGRATION_ID,
        "plugin_version": LEXICAL_MIGRATION_PLUGIN_VERSION,
        "description": LEXICAL_MIGRATION_DESCRIPTION,
        "schema_version": LEXICAL_SCHEMA_VERSION,
    },
    {
        "id": RELATION_CONTAINMENT_MIGRATION_ID,
        "plugin_version": RELATION_CONTAINMENT_MIGRATION_PLUGIN_VERSION,
        "description": RELATION_CONTAINMENT_MIGRATION_DESCRIPTION,
        "schema_version": RELATION_CONTAINMENT_SCHEMA_VERSION,
    },
    {
        "id": RELATION_POLICY_GENERATION_MIGRATION_ID,
        "plugin_version": RELATION_POLICY_GENERATION_MIGRATION_PLUGIN_VERSION,
        "description": RELATION_POLICY_GENERATION_MIGRATION_DESCRIPTION,
        "schema_version": RELATION_POLICY_GENERATION_SCHEMA_VERSION,
    },
    {
        "id": PRIVACY_PURGE_MIGRATION_ID,
        "plugin_version": PRIVACY_PURGE_MIGRATION_PLUGIN_VERSION,
        "description": PRIVACY_PURGE_MIGRATION_DESCRIPTION,
        "schema_version": PRIVACY_PURGE_SCHEMA_VERSION,
    },
)


def _schema_migration_checksum(
    *,
    migration_id: str,
    plugin_version: str,
    description: str,
    schema_version: int,
) -> str:
    """Hash one immutable migration specification.

    Each migration carries its own schema version so increasing the current
    version cannot invalidate historical ledger checksums.
    """

    payload = json.dumps(
        {
            "id": migration_id,
            "plugin_version": plugin_version,
            "description": description,
            "schema_version": schema_version,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _expected_migration_row(spec: dict[str, Any]) -> dict[str, str]:
    return {
        "id": str(spec["id"]),
        "plugin_version": str(spec["plugin_version"]),
        "description": str(spec["description"]),
        "checksum": _schema_migration_checksum(
            migration_id=str(spec["id"]),
            plugin_version=str(spec["plugin_version"]),
            description=str(spec["description"]),
            schema_version=int(spec["schema_version"]),
        ),
        "status": "applied",
        "error": "",
    }


def _assert_supported_schema_version(conn: sqlite3.Connection) -> int:
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if user_version > SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"database schema version {user_version} is newer than supported "
            f"version {SCHEMA_VERSION}"
        )
    return user_version


def ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    """Apply additive schema migrations without committing the caller's work."""

    existing_user_version = _assert_supported_schema_version(conn)
    ensure_temporal_fact_schema(conn)
    ensure_relation_rebuild_schema(conn)
    ensure_relation_frequency_index_schema(conn)
    ensure_relation_containment_schema(conn)
    ensure_relation_policy_generation_schema(conn)
    ensure_vector_reconciliation_schema(conn)
    ensure_operator_ledger_schema(conn)
    ensure_lexical_generation_schema(conn)
    ensure_privacy_purge_schema(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL,
            plugin_version TEXT NOT NULL,
            description TEXT NOT NULL,
            checksum TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'applied',
            error TEXT NOT NULL DEFAULT ''
        );
        """
    )
    existing_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(schema_migrations)").fetchall()
    }
    column_migrations = {
        "applied_at": "ALTER TABLE schema_migrations ADD COLUMN applied_at TEXT NOT NULL DEFAULT ''",
        "plugin_version": "ALTER TABLE schema_migrations ADD COLUMN plugin_version TEXT NOT NULL DEFAULT ''",
        "description": "ALTER TABLE schema_migrations ADD COLUMN description TEXT NOT NULL DEFAULT ''",
        "checksum": "ALTER TABLE schema_migrations ADD COLUMN checksum TEXT NOT NULL DEFAULT ''",
        "status": "ALTER TABLE schema_migrations ADD COLUMN status TEXT NOT NULL DEFAULT 'applied'",
        "error": "ALTER TABLE schema_migrations ADD COLUMN error TEXT NOT NULL DEFAULT ''",
    }
    for column, statement in column_migrations.items():
        if column not in existing_columns:
            conn.execute(statement)

    for spec in EXPECTED_SCHEMA_MIGRATIONS:
        expected = _expected_migration_row(spec)
        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations(
                id, applied_at, plugin_version, description, checksum, status, error
            ) VALUES (?, ?, ?, ?, ?, 'applied', '')
            """,
            (
                expected["id"],
                now_iso(),
                expected["plugin_version"],
                expected["description"],
                expected["checksum"],
            ),
        )
    if existing_user_version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _row_to_dict(
    cursor: sqlite3.Cursor,
    row: sqlite3.Row | tuple[Any, ...],
) -> dict[str, Any]:
    columns = [str(item[0]) for item in cursor.description or []]
    return {column: row[index] for index, column in enumerate(columns)}


def schema_migration_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return live schema and immutable migration-ledger status read-only."""

    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    try:
        table = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if table is None:
            rows: list[dict[str, Any]] = []
        else:
            columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(schema_migrations)"
                ).fetchall()
            }
            select_columns = [
                "id" if "id" in columns else "'' AS id",
                "applied_at" if "applied_at" in columns else "'' AS applied_at",
                "plugin_version" if "plugin_version" in columns else "'' AS plugin_version",
                "description" if "description" in columns else "'' AS description",
                "checksum" if "checksum" in columns else "'' AS checksum",
                "status" if "status" in columns else "'applied' AS status",
                "error" if "error" in columns else "'' AS error",
            ]
            order_by = "id" if "id" in columns else "rowid"
            cursor = conn.execute(
                f"SELECT {', '.join(select_columns)} "
                f"FROM schema_migrations ORDER BY {order_by}"
            )
            rows = [_row_to_dict(cursor, row) for row in cursor.fetchall()]
    except sqlite3.OperationalError as exc:
        if "schema_migrations" not in str(exc):
            raise
        rows = []

    applied_ids = {
        str(row.get("id") or "")
        for row in rows
        if str(row.get("status") or "") == "applied"
    }
    expected_rows = {
        str(spec["id"]): _expected_migration_row(spec)
        for spec in EXPECTED_SCHEMA_MIGRATIONS
    }
    missing = [
        migration_id
        for migration_id in expected_rows
        if migration_id not in applied_ids
    ]
    invalid_migrations: list[dict[str, Any]] = []
    for row in rows:
        migration_id = str(row.get("id") or "")
        expected = expected_rows.get(migration_id)
        if expected is None:
            continue
        mismatches = [
            key
            for key, expected_value in expected.items()
            if str(row.get(key) or "") != str(expected_value)
        ]
        if mismatches:
            invalid_migrations.append(
                {"id": migration_id, "mismatches": mismatches}
            )

    newer_schema = user_version > SCHEMA_VERSION
    temporal_status = temporal_fact_schema_status(conn)
    relation_status = relation_rebuild_schema_status(conn)
    relation_frequency_status = relation_frequency_index_schema_status(conn)
    relation_containment_status = relation_containment_schema_status(conn)
    relation_policy_generation_status = relation_policy_generation_schema_status(conn)
    vector_reconciliation_status = vector_reconciliation_schema_status(conn)
    operator_status = operator_ledger_schema_status(conn)
    lexical_status = lexical_schema_status(conn)
    privacy_purge_status = privacy_purge_schema_status(conn)
    return {
        "schema_version": SCHEMA_VERSION,
        "user_version": user_version,
        "current": (
            user_version == SCHEMA_VERSION
            and not newer_schema
            and not missing
            and not invalid_migrations
            and bool(temporal_status["current"])
            and bool(relation_status["current"])
            and bool(relation_frequency_status["current"])
            and bool(relation_containment_status["current"])
            and bool(relation_policy_generation_status["current"])
            and bool(vector_reconciliation_status["current"])
            and bool(operator_status["current"])
            and bool(lexical_status["current"])
            and bool(privacy_purge_status["current"])
        ),
        "newer_schema": newer_schema,
        "missing_migrations": missing,
        "invalid_migrations": invalid_migrations,
        "applied_migrations": rows,
        "temporal_facts": temporal_status,
        "relation_rebuild_queue": relation_status,
        "relation_frequency_index": relation_frequency_status,
        "relation_containment": relation_containment_status,
        "relation_policy_generation": relation_policy_generation_status,
        "vector_reconciliation": vector_reconciliation_status,
        "operator_ledger": operator_status,
        "lexical_generation": lexical_status,
        "privacy_purge": privacy_purge_status,
    }



def ensure_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    # This guard must run before any DDL so opening a database created by newer
    # code cannot partially mutate it with an older runtime.
    _assert_supported_schema_version(conn)
    for statement in (
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
        )
        """,
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            memory_id UNINDEXED,
            content,
            summary
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_scope_recall_scope_updated
            ON memories(scope_id, updated_at DESC)
        """,
    ):
        conn.execute(statement)
    ensure_memory_columns(conn)
    ensure_graph_schema(conn)
    ensure_experience_schema(conn)
    ensure_governance_schema(conn)
    ensure_relation_rebuild_schema(conn)
    ensure_relation_frequency_index_schema(conn)
    ensure_relation_containment_schema(conn)
    from .vector_generation import ensure_vector_generation_schema

    ensure_vector_generation_schema(conn)
    ensure_vector_reconciliation_schema(conn)
    ensure_privacy_purge_schema(conn)
    ensure_schema_migrations(conn)
    rebuild_fts_if_empty(conn, commit=False)
    backfill_memory_entities(conn)
    if commit:
        conn.commit()


def ensure_governance_schema(conn: sqlite3.Connection) -> None:
    """Create/migrate governance audit schema before mutation transactions.

    Do not call this from `record_governance_audit_event()`: schema DDL must be
    completed before business rows are mutated. Keeping the audit insert helper
    DDL-free preserves the caller's transaction atomicity.
    """

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
    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_governance_audit_batch ON governance_audit_events(batch_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_governance_audit_target ON governance_audit_events(target_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_governance_audit_type_action ON governance_audit_events(event_type, action, created_at)",
    ):
        conn.execute(statement)


def _require_governance_audit_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'governance_audit_events'").fetchone()
    if row is None:
        raise RuntimeError("governance_audit_events schema is not initialized; call ensure_schema(conn) before recording audit events")


def _redact_governance_payload(value: Any) -> Any:
    """Redact report/audit payloads before they become durable governance rows.

    Defensive boundary: governance audit survives cleanup/forgetting actions.
    Never bypass this helper in `record_governance_audit_event`, or hard-deleted
    secrets can be resurrected from `before_json`/`after_json`.
    """

    return sanitize_structured_value(value)[0]


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
    _require_governance_audit_schema(conn)
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
    """Create or migrate Experience Kernel tables in the SQLite truth store.

    The schema helper is idempotent because it may run during startup, tests, or release smoke checks before any Experience tools are used."""
    execute_script_transaction_neutral(
        conn,
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
    try:
        conn.execute(statement)
    except sqlite3.OperationalError as exc:
        # Concurrent first-boot initializes can race PRAGMA table_info -> ADD
        # COLUMN. Treat already-present columns as success so Desktop principal
        # cold-start remains fail-closed only on real schema problems.
        if "duplicate column name" not in str(exc).lower():
            raise


def ensure_memory_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    for column in ("chat_id", "thread_id", "gateway_session_key", "dedup_key", "metadata"):
        if column not in existing:
            _add_memory_column(conn, column)
            existing.add(column)
    for row in conn.execute("SELECT id, content FROM memories WHERE dedup_key IS NULL OR dedup_key = ''").fetchall():
        conn.execute("UPDATE memories SET dedup_key = ? WHERE id = ?", (dedup_key(str(row["content"])), row["id"]))
    conn.execute("UPDATE memories SET metadata = '{}' WHERE metadata IS NULL OR metadata = ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scope_recall_dedup ON memories(scope_id, target, dedup_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scope_recall_target_updated ON memories(target, updated_at DESC)")


def _fts_counts(conn: sqlite3.Connection) -> dict[str, int | bool]:
    """Return lifecycle-aware integrity counts for the lexical companion.

    ``memory_rows`` remains the total SQLite truth count for observability while
    ``expected_fts_rows`` is the ordinary-recall-visible subset that belongs in
    FTS. Hidden lifecycle rows are truth, but never expected lexical members.
    """

    visible_sql = ordinary_recall_lifecycle_visible_sql("m")
    memory_rows = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
    expected_fts_rows = int(
        conn.execute(
            f"SELECT COUNT(*) FROM memories AS m WHERE {visible_sql}"
        ).fetchone()[0]
    )
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
    hidden_fts_rows = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM memories_fts AS f
            JOIN memories AS m ON m.id = f.memory_id
            WHERE NOT ({visible_sql})
            """
        ).fetchone()[0]
    )
    missing_fts_rows = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM memories AS m
            LEFT JOIN memories_fts AS f ON f.memory_id = m.id
            WHERE f.memory_id IS NULL AND {visible_sql}
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
    healthy = (
        stale_fts_rows == 0
        and hidden_fts_rows == 0
        and missing_fts_rows == 0
        and duplicate_fts_extra_rows == 0
        and fts_rows == expected_fts_rows
    )
    return {
        "memory_rows": memory_rows,
        "expected_fts_rows": expected_fts_rows,
        "fts_rows": fts_rows,
        "stale_fts_rows": stale_fts_rows,
        "hidden_fts_rows": hidden_fts_rows,
        "missing_fts_rows": missing_fts_rows,
        "duplicate_fts_extra_rows": duplicate_fts_extra_rows,
        "healthy": healthy,
    }


def fts_integrity_report(conn: sqlite3.Connection) -> dict[str, int | bool]:
    return _fts_counts(conn)


def reconcile_fts_index(
    conn: sqlite3.Connection,
    *,
    commit: bool = True,
) -> dict[str, Any]:
    before = _fts_counts(conn)
    needs_rebuild = not bool(before["healthy"])
    if needs_rebuild:
        visible_sql = ordinary_recall_lifecycle_visible_sql("m")
        conn.execute("DELETE FROM memories_fts")
        conn.execute(
            "INSERT INTO memories_fts(memory_id, content, summary) "
            f"SELECT m.id, m.content, m.summary FROM memories AS m WHERE {visible_sql}"
        )
        if commit:
            conn.commit()
    after = _fts_counts(conn)
    return {"rebuilt": needs_rebuild, "before": before, "after": after}


def rebuild_fts_if_empty(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    """Bootstrap a wholly empty memory FTS index without scanning for drift.

    Routine startup performs only two bounded existence probes. Partial, duplicate,
    stale index states are operational anomalies and must be inspected and
    repaired explicitly through :func:`reconcile_fts_index`, which returns a
    receipt instead of silently rewriting the truth companion index.
    """

    visible_sql = ordinary_recall_lifecycle_visible_sql("m")
    memory_has_rows = bool(
        conn.execute(
            f"SELECT EXISTS(SELECT 1 FROM memories AS m WHERE {visible_sql} LIMIT 1)"
        ).fetchone()[0]
    )
    fts_has_rows = bool(
        conn.execute("SELECT EXISTS(SELECT 1 FROM memories_fts LIMIT 1)").fetchone()[0]
    )
    if memory_has_rows and not fts_has_rows:
        conn.execute(
            "INSERT INTO memories_fts(memory_id, content, summary) "
            f"SELECT m.id, m.content, m.summary FROM memories AS m WHERE {visible_sql}"
        )
        if commit:
            conn.commit()


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
    commit: bool = True,
    timestamp: str = "",
    enqueue_vector_intent: bool = True,
    fact_projection_authority: str = "",
) -> tuple[str, str, str, bool]:

    """Insert one durable memory row into the SQLite truth store.

    The helper centralizes IDs, timestamps, scope, metadata serialization, and
    duplicate-sensitive fields used by downstream companions. ``commit=False``
    lets cross-surface coordinators retain the caller-owned transaction.
    """
    now = timestamp or now_iso()
    content = sanitize_capture_text(content)
    if not content:
        return "", "", now, False
    if contains_secret_like_text(content):
        raise ValueError("plaintext secret-like content rejected at durable store boundary")
    preflight_metadata = merge_metadata(
        dict(classify_memory(content, target, source)), metadata
    )
    if str(preflight_metadata.get("lifecycle") or "").strip().lower() == "candidate":
        transport = classify_transport_noise(content)
        if transport.blocked:
            raise ValueError(
                "transport noise rejected at candidate store boundary: "
                + ",".join(transport.reason_codes)
            )
    content = enrich_content_with_artifact_anchors(content)
    summary = compact_text(content, 220)
    key = dedup_key(content)
    requested_fact_metadata: dict[str, Any] | None = None
    if fact_projection_authority:
        if fact_projection_authority != FACT_EXECUTOR_MUTATION_AUTHORITY:
            raise PermissionError(
                "initial canonical fact Projection requires Fact Executor authority"
            )
        try:
            raw_fact_metadata = (
                json.loads(metadata) if isinstance(metadata, str) else metadata
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("canonical fact Projection metadata is invalid") from exc
        if not isinstance(raw_fact_metadata, dict):
            raise ValueError("canonical fact Projection metadata must be an object")
        requested_fact_metadata = raw_fact_metadata
        requested_lifecycle = str(
            requested_fact_metadata.get("lifecycle") or ""
        ).strip().lower()
        if requested_lifecycle not in {"active", "promoted"}:
            raise ValueError(
                "initial canonical fact Projection must be recall-visible"
            )
        if not str(requested_fact_metadata.get("fact_claim_id") or "").strip() or not str(
            requested_fact_metadata.get("fact_claim_key") or ""
        ).strip():
            raise ValueError(
                "initial canonical fact Projection requires Claim identity"
            )
    candidate_ingress = (
        str(preflight_metadata.get("lifecycle") or "").strip().lower()
        == "candidate"
    )
    if not allow_duplicate:
        # Candidate ingress is observation-only against live/provisional
        # truth: an unreviewed Event Digest must not refresh a promoted or
        # still-open row, enqueue companion work, or alter ranking. Archived
        # history is not a live duplicate. Reusing ``1=1`` would let an
        # archived row suppress a distinct new candidate and contradict the
        # journal/nightly contract that never mutates or reactivates it.
        lifecycle_clause = (
            f"{_normalized_sql_token(_metadata_lifecycle_expr('m'))} != 'archived'"
            if candidate_ingress
            else ordinary_recall_lifecycle_visible_sql("m")
        )
        existing = conn.execute(
            f"""
            SELECT m.id, m.summary, m.updated_at, m.metadata
            FROM memories AS m
            WHERE m.scope_id = ?
              AND m.target = ?
              AND m.dedup_key = ?
              AND {lifecycle_clause}
            ORDER BY m.updated_at DESC
            LIMIT 1
            """,
            (scope_id, target, key),
        ).fetchone()
        if existing is not None:
            if candidate_ingress:
                return (
                    str(existing["id"]),
                    str(existing["summary"]),
                    str(existing["updated_at"]),
                    False,
                )
            try:
                tracked = conn.execute(
                    "SELECT 1 FROM fact_freshness WHERE subject_type='memory' AND subject_id=?",
                    (str(existing["id"]),),
                ).fetchone()
                if tracked is None:
                    try:
                        existing_metadata = json.loads(
                            str(existing["metadata"] or "{}")
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        existing_metadata = {}
                    if not isinstance(existing_metadata, dict):
                        existing_metadata = {}
                    upsert_memory_freshness(
                        conn,
                        memory_id=str(existing["id"]),
                        metadata=existing_metadata,
                        content=content,
                        commit=False,
                    )
                conn.execute(
                    "UPDATE memories SET updated_at = ? WHERE id = ?",
                    (now, existing["id"]),
                )
                sync_relation_frequency_memory(conn, str(existing["id"]))
                if enqueue_vector_intent:
                    enqueue_current_vector_event(
                        conn,
                        memory_id=str(existing["id"]),
                        operation="upsert",
                        updated_at=now,
                        reason="durable duplicate-store timestamp update",
                    )
                if commit:
                    conn.commit()
            except BaseException:
                if commit and conn.in_transaction:
                    conn.rollback()
                raise
            return str(existing["id"]), str(existing["summary"]), now, False

    classified_metadata = dict(classify_memory(content, target, source))
    reviewed_fact_lifecycle = ""
    if requested_fact_metadata is not None:
        requested_lifecycle = str(
            requested_fact_metadata.get("lifecycle") or ""
        ).strip().lower()
        classification_blocks_visibility = (
            str(classified_metadata.get("lifecycle") or "").strip().lower()
            == "candidate"
            or bool(classified_metadata.get("expires_at"))
        )
        if not classification_blocks_visibility:
            # A reviewed Claim may promote the otherwise-scratch ``general``
            # projection. Temporary/expiring content stays provisional so the
            # Executor postcondition rejects and rolls the whole unit back.
            reviewed_fact_lifecycle = requested_lifecycle
    metadata_payload = merge_metadata(
        dict(classified_metadata),
        metadata,
        reviewed_fact_lifecycle=reviewed_fact_lifecycle,
    )
    metadata_payload = merge_artifact_metadata(metadata_payload, content)
    safe_metadata, _ = sanitize_structured_value(metadata_payload)
    metadata_payload = safe_metadata if isinstance(safe_metadata, dict) else {}
    if str(metadata_payload.get("lifecycle") or "").strip().lower() == "candidate":
        transport = classify_transport_noise(content)
        if transport.blocked:
            raise ValueError(
                "transport noise rejected at candidate store boundary: "
                + ",".join(transport.reason_codes)
            )
    metadata_json = json.dumps(metadata_payload, ensure_ascii=False, sort_keys=True)

    try:
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
        upsert_memory_freshness(
            conn,
            memory_id=memory_id,
            metadata=metadata_payload,
            content=content,
            commit=False,
        )
        lifecycle = str(metadata_payload.get("lifecycle") or "").strip().lower()
        if ordinary_recall_lifecycle_visible(lifecycle=lifecycle, target=target):
            conn.execute(
                "INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)",
                (memory_id, content, summary),
            )
        sync_memory_entities(
            conn,
            memory_id=memory_id,
            content=content,
            target=target,
            metadata=metadata_payload,
        )
        sync_relation_frequency_memory(conn, memory_id)
        if enqueue_vector_intent:
            enqueue_current_vector_event(
                conn,
                memory_id=memory_id,
                operation="upsert",
                updated_at=now,
                reason="durable memory insert",
            )
        if commit:
            conn.commit()
    except BaseException:
        if commit and conn.in_transaction:
            conn.rollback()
        raise
    return memory_id, summary, now, True


def update_row(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    content: str,
    target: str | None = None,
    scope_id: str | None = None,
    scope_ids: list[str] | tuple[str, ...] | None = None,
    fact_mutation_authority: str = "",
    enqueue_vector_intent: bool = True,
) -> tuple[bool, str, str]:
    """Update one SQLite truth row without committing.

    The helper preserves metadata/lifecycle invariants and rejects legacy edits
    to any memory that owns structured claims. The caller owns commit/rollback;
    only the Fact Executor may pass its explicit mutation authority.
    """
    content = sanitize_capture_text(content)
    if not content:
        return False, "", ""
    if contains_secret_like_text(content):
        raise ValueError("plaintext secret-like content rejected at durable store boundary")
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
    require_fact_mutation_authority(
        conn,
        [memory_id],
        operation="legacy memory update",
        authority=fact_mutation_authority,
    )
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
        "lifecycle",
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
    safe_metadata, _ = sanitize_structured_value(metadata_payload)
    metadata_payload = safe_metadata if isinstance(safe_metadata, dict) else {}
    lifecycle = str(metadata_payload.get("lifecycle") or "").strip().lower()
    if lifecycle == "candidate":
        transport = classify_transport_noise(content)
        if transport.blocked:
            raise ValueError(
                "transport noise rejected at candidate update boundary: "
                + ",".join(transport.reason_codes)
            )
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
    if ordinary_recall_lifecycle_visible(lifecycle=lifecycle, target=new_target):
        conn.execute("INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)", (memory_id, content, summary))
        sync_memory_entities(conn, memory_id=memory_id, content=content, target=new_target, metadata=metadata_payload)
    else:
        if _table_exists(conn, "memory_entities"):
            conn.execute("DELETE FROM memory_entities WHERE memory_id = ?", (memory_id,))
        peer_rows = conn.execute(
            """
            SELECT CASE WHEN source_memory_id = ? THEN target_memory_id ELSE source_memory_id END AS peer_id
            FROM memory_relations
            WHERE source_memory_id = ? OR target_memory_id = ?
            """,
            (memory_id, memory_id, memory_id),
        ).fetchall()
        peer_ids = [str(peer["peer_id"] or "") for peer in peer_rows if str(peer["peer_id"] or "")]
        conn.execute(
            "DELETE FROM memory_relations WHERE source_memory_id = ? OR target_memory_id = ?",
            (memory_id, memory_id),
        )
        if _table_exists(conn, "fact_freshness"):
            conn.execute(
                "DELETE FROM fact_freshness WHERE subject_type = 'memory' AND subject_id = ?",
                (memory_id,),
            )
        _sync_conflict_metadata_after_relation_delete(conn, peer_ids)
    sync_relation_frequency_memory(conn, memory_id)
    if enqueue_vector_intent:
        enqueue_current_vector_event(
            conn,
            memory_id=memory_id,
            operation="upsert",
            updated_at=updated_at,
            reason="durable memory update",
        )
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
        safe_metadata, _ = sanitize_structured_value(metadata)
        metadata = safe_metadata if isinstance(safe_metadata, dict) else {}
        conn.execute(
            "UPDATE memories SET metadata = ? WHERE id = ?",
            (json.dumps(metadata, ensure_ascii=False, sort_keys=True), memory_id),
        )


def _scoped_memory_ids_for_delete(
    conn: sqlite3.Connection,
    ids: list[str],
    *,
    scope_id: str | None,
    scope_ids: list[str] | tuple[str, ...] | None,
) -> list[str]:
    """Return existing IDs in caller order without exceeding SQL variables."""

    if scope_ids is None and scope_id is None:
        return list(ids)
    if scope_ids is not None:
        clean_scope_ids = list(
            dict.fromkeys(str(item) for item in scope_ids if str(item))
        )
        if not clean_scope_ids:
            return []
        scope_clause = "scope_id IN (" + ",".join("?" for _ in clean_scope_ids) + ")"
        scope_params = clean_scope_ids
    else:
        scope_clause = "scope_id = ?"
        scope_params = [str(scope_id)]

    found: set[str] = set()
    for id_chunk in chunked_sql_parameters(
        conn,
        ids,
        reserved=len(scope_params),
    ):
        placeholders = ",".join("?" for _ in id_chunk)
        rows = conn.execute(
            f"SELECT id FROM memories WHERE id IN ({placeholders}) AND {scope_clause}",
            [*id_chunk, *scope_params],
        ).fetchall()
        found.update(str(row["id"]) for row in rows)
    return [memory_id for memory_id in ids if memory_id in found]


def delete_rows(
    conn: sqlite3.Connection,
    ids: list[str],
    *,
    scope_id: str | None = None,
    scope_ids: list[str] | tuple[str, ...] | None = None,
    commit: bool = True,
) -> int:
    """Delete selected truth/companion rows in bounded SQL chunks.

    Public maintenance callers keep the historical commit-by-default behavior.
    Atomic hard-delete domains pass ``commit=False`` so audit and vector intent
    remain in the caller-owned transaction. Chunking never commits between
    statements, preserving that transaction ownership and rollback behavior.
    """

    requested_ids = list(
        dict.fromkeys(str(memory_id) for memory_id in ids if str(memory_id).strip())
    )
    if not requested_ids:
        return 0
    scoped_ids = _scoped_memory_ids_for_delete(
        conn,
        requested_ids,
        scope_id=scope_id,
        scope_ids=scope_ids,
    )
    if not scoped_ids:
        return 0

    before = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
    scoped_id_set = set(scoped_ids)
    conflict_peer_ids: set[str] = set()
    for id_chunk in chunked_sql_parameters(
        conn,
        scoped_ids,
        variables_per_item=2,
    ):
        placeholders = ",".join("?" for _ in id_chunk)
        conflict_peer_rows = conn.execute(
            f"""
            SELECT target_memory_id AS peer_id
            FROM memory_relations
            WHERE relation_type = 'contradicts'
              AND source_memory_id IN ({placeholders})
            UNION
            SELECT source_memory_id AS peer_id
            FROM memory_relations
            WHERE relation_type = 'contradicts'
              AND target_memory_id IN ({placeholders})
            """,
            [*id_chunk, *id_chunk],
        ).fetchall()
        conflict_peer_ids.update(
            str(row["peer_id"])
            for row in conflict_peer_rows
            if str(row["peer_id"]) and str(row["peer_id"]) not in scoped_id_set
        )
        conn.execute(
            f"DELETE FROM memories_fts WHERE memory_id IN ({placeholders})",
            id_chunk,
        )
        conn.execute(
            f"DELETE FROM memory_entities WHERE memory_id IN ({placeholders})",
            id_chunk,
        )
        conn.execute(
            f"DELETE FROM memory_feedback WHERE memory_id IN ({placeholders})",
            id_chunk,
        )
        conn.execute(
            f"DELETE FROM memory_relations "
            f"WHERE source_memory_id IN ({placeholders}) "
            f"OR target_memory_id IN ({placeholders})",
            [*id_chunk, *id_chunk],
        )
        if _table_exists(conn, "memory_digest_sources"):
            conn.execute(
                f"DELETE FROM memory_digest_sources WHERE memory_id IN ({placeholders})",
                id_chunk,
            )
        if _table_exists(conn, "memory_journal_sources"):
            conn.execute(
                f"DELETE FROM memory_journal_sources WHERE memory_id IN ({placeholders})",
                id_chunk,
            )
        if _table_exists(conn, "fact_freshness"):
            conn.execute(
                f"DELETE FROM fact_freshness "
                f"WHERE subject_type = 'memory' "
                f"AND subject_id IN ({placeholders})",
                id_chunk,
            )
        conn.execute(
            f"DELETE FROM memories WHERE id IN ({placeholders})",
            id_chunk,
        )

    for memory_id in scoped_ids:
        sync_relation_frequency_memory(conn, memory_id)
    _sync_conflict_metadata_after_relation_delete(conn, sorted(conflict_peer_ids))
    if commit:
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
    """Return exact-text groups without crossing durable semantic types."""

    memory_type_sql = (
        "CASE WHEN json_valid(m.metadata) THEN LOWER(TRIM(COALESCE("
        "NULLIF(json_extract(m.metadata, '$.memory_type'), ''), "
        "NULLIF(json_extract(m.metadata, '$.category'), ''), ''))) ELSE '' END"
    )
    conditions = [ordinary_recall_lifecycle_visible_sql("m")]
    if scope_ids is not None:
        clean_scope_ids = [str(item) for item in scope_ids if str(item)]
        if not clean_scope_ids:
            return []
        conditions.append(f"m.scope_id IN ({','.join('?' for _ in clean_scope_ids)})")
        params: tuple[Any, ...] = tuple(clean_scope_ids)
    elif scope_id:
        conditions.append("m.scope_id = ?")
        params = (scope_id,)
    else:
        params = ()
    where = "WHERE " + " AND ".join(f"({condition})" for condition in conditions)
    rows = conn.execute(
        f"""
        SELECT m.scope_id, m.target, m.dedup_key,
               {memory_type_sql} AS memory_type,
               COUNT(*) AS count
        FROM memories AS m
        {where}
        GROUP BY m.scope_id, m.target, m.dedup_key, {memory_type_sql}
        HAVING COUNT(*) > 1
        ORDER BY count DESC
        """,
        params,
    ).fetchall()
    groups: list[dict[str, Any]] = []
    for row in rows:
        members = conn.execute(
            f"""
            SELECT m.id, m.content, m.created_at, m.updated_at
            FROM memories AS m
            WHERE m.scope_id = ?
              AND m.target = ?
              AND m.dedup_key = ?
              AND {memory_type_sql} = ?
              AND {ordinary_recall_lifecycle_visible_sql('m')}
            ORDER BY m.updated_at DESC, m.created_at DESC, m.id DESC
            """,
            (
                row["scope_id"],
                row["target"],
                row["dedup_key"],
                row["memory_type"],
            ),
        ).fetchall()
        groups.append(
            {
                "scope_id": row["scope_id"],
                "target": row["target"],
                "dedup_key": row["dedup_key"],
                "memory_type": row["memory_type"],
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
