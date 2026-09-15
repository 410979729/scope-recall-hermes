"""Additive deny-first privacy purge ledger schema."""

from __future__ import annotations

import sqlite3
from typing import Any

from .sqlite_schema import execute_script_transaction_neutral

PRIVACY_PURGE_SCHEMA_VERSION = 10815
PRIVACY_PURGE_MIGRATION_ID = "0015_privacy_purge_v2_0_0"
PRIVACY_PURGE_MIGRATION_PLUGIN_VERSION = "2.0.0"
PRIVACY_PURGE_MIGRATION_DESCRIPTION = (
    "Deny-first privacy purge operations, tombstones, source deny, and vector intent receipts"
)

_REQUIRED_TABLE_COLUMNS = {
    "privacy_purge_operations": {
        "operation_id",
        "request_fingerprint",
        "scope_set_hash",
        "target_count",
        "source_count",
        "vector_intent_count",
        "status",
        "created_at",
        "updated_at",
        "denied_at",
        "erased_at",
    },
    "privacy_purge_tombstones": {
        "operation_id",
        "target_hash",
        "content_hash",
        "created_at",
    },
    "privacy_purge_source_tombstones": {
        "operation_id",
        "journal_entry_id",
        "source_hash",
        "created_at",
    },
    "privacy_purge_vector_intents": {
        "operation_id",
        "event_key",
        "target_hash",
        "completed",
        "completed_at",
        "created_at",
    },
}


def ensure_privacy_purge_schema(conn: sqlite3.Connection) -> None:
    """Create the content-free purge ledger without committing caller work."""

    execute_script_transaction_neutral(
        conn,
        """
        CREATE TABLE IF NOT EXISTS privacy_purge_operations (
            operation_id TEXT PRIMARY KEY,
            request_fingerprint TEXT NOT NULL,
            scope_set_hash TEXT NOT NULL,
            target_count INTEGER NOT NULL,
            source_count INTEGER NOT NULL DEFAULT 0,
            vector_intent_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL
                CHECK(status IN ('denied','erasure_pending_vector','completed')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            denied_at TEXT NOT NULL,
            erased_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_privacy_purge_operations_status
            ON privacy_purge_operations(status, updated_at, operation_id);

        CREATE TABLE IF NOT EXISTS privacy_purge_tombstones (
            operation_id TEXT NOT NULL,
            target_hash TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(operation_id, target_hash)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_privacy_purge_target_hash
            ON privacy_purge_tombstones(target_hash);

        CREATE TABLE IF NOT EXISTS privacy_purge_source_tombstones (
            operation_id TEXT NOT NULL,
            journal_entry_id INTEGER NOT NULL,
            source_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(operation_id, journal_entry_id)
        );
        CREATE INDEX IF NOT EXISTS idx_privacy_purge_source_entry
            ON privacy_purge_source_tombstones(journal_entry_id);

        CREATE TABLE IF NOT EXISTS privacy_purge_vector_intents (
            operation_id TEXT NOT NULL,
            event_key TEXT NOT NULL,
            target_hash TEXT NOT NULL,
            completed INTEGER NOT NULL DEFAULT 0 CHECK(completed IN (0,1)),
            completed_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY(operation_id, event_key)
        );
        CREATE INDEX IF NOT EXISTS idx_privacy_purge_vector_operation
            ON privacy_purge_vector_intents(operation_id, event_key);
        """,
    )


def privacy_purge_schema_status(conn: sqlite3.Connection) -> dict[str, Any]:
    missing_tables: list[str] = []
    missing_columns: dict[str, list[str]] = {}
    for table, required in _REQUIRED_TABLE_COLUMNS.items():
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists is None:
            missing_tables.append(table)
            continue
        columns = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        missing = sorted(required - columns)
        if missing:
            missing_columns[table] = missing
    return {
        "current": not missing_tables and not missing_columns,
        "schema_version": PRIVACY_PURGE_SCHEMA_VERSION,
        "missing_tables": sorted(missing_tables),
        "missing_columns": missing_columns,
    }


__all__ = [
    "PRIVACY_PURGE_MIGRATION_DESCRIPTION",
    "PRIVACY_PURGE_MIGRATION_ID",
    "PRIVACY_PURGE_MIGRATION_PLUGIN_VERSION",
    "PRIVACY_PURGE_SCHEMA_VERSION",
    "ensure_privacy_purge_schema",
    "privacy_purge_schema_status",
]
