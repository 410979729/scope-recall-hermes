"""Tests for schema migration order, ledger state, and backwards compatibility.

They protect live upgrades from partial or out-of-order migration drift."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from scope_recall.lexical_generation import (
    LEXICAL_MIGRATION_ID,
    LEXICAL_SCHEMA_VERSION,
    LEXICAL_SHADOW_TABLE,
)
from scope_recall.operator_ledger import (
    OPERATOR_LEDGER_MIGRATION_ID,
    OPERATOR_LEDGER_SCHEMA_VERSION,
)
from scope_recall.relation_containment import (
    RELATION_CONTAINMENT_MIGRATION_ID,
    RELATION_CONTAINMENT_SCHEMA_VERSION,
)
from scope_recall.relation_frequency_index import (
    RELATION_FREQUENCY_FAILURE_MIGRATION_ID,
    RELATION_FREQUENCY_INDEX_MIGRATION_ID,
)
from scope_recall.relation_rebuild_queue import (
    RELATION_REBUILD_EXPIRY_MIGRATION_ID,
    RELATION_REBUILD_LEASE_MIGRATION_ID,
    RELATION_REBUILD_LEASE_SCHEMA_VERSION,
    RELATION_REBUILD_MIGRATION_ID,
    RELATION_REBUILD_PROGRESS_MIGRATION_ID,
)
from scope_recall.relation_scope_state import RELATION_SCOPE_RECEIPT_MIGRATION_ID
from scope_recall.vector_reconciliation import (
    VECTOR_RECONCILIATION_MIGRATION_ID,
    VECTOR_RECONCILIATION_SCHEMA_VERSION,
)
from scope_recall.sql_store import ensure_schema, schema_migration_status
from scope_recall.temporal_facts import FACT_CLAIMS_MIGRATION_ID

ROOT = Path(__file__).resolve().parents[1]


def test_schema_migration_status_reports_baseline_after_ensure_schema():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    before = schema_migration_status(conn)
    ensure_schema(conn)
    after = schema_migration_status(conn)

    assert before["current"] is False
    assert before["missing_migrations"] == [
        "0001_baseline_v1_6_0",
        FACT_CLAIMS_MIGRATION_ID,
        RELATION_REBUILD_MIGRATION_ID,
        OPERATOR_LEDGER_MIGRATION_ID,
        RELATION_REBUILD_LEASE_MIGRATION_ID,
        RELATION_SCOPE_RECEIPT_MIGRATION_ID,
        RELATION_FREQUENCY_INDEX_MIGRATION_ID,
        RELATION_REBUILD_PROGRESS_MIGRATION_ID,
        VECTOR_RECONCILIATION_MIGRATION_ID,
        RELATION_REBUILD_EXPIRY_MIGRATION_ID,
        RELATION_FREQUENCY_FAILURE_MIGRATION_ID,
        LEXICAL_MIGRATION_ID,
        RELATION_CONTAINMENT_MIGRATION_ID,
    ]
    assert after["current"] is True
    assert after["user_version"] == after["schema_version"]
    assert [row["id"] for row in after["applied_migrations"]] == [
        "0001_baseline_v1_6_0",
        FACT_CLAIMS_MIGRATION_ID,
        RELATION_REBUILD_MIGRATION_ID,
        OPERATOR_LEDGER_MIGRATION_ID,
        RELATION_REBUILD_LEASE_MIGRATION_ID,
        RELATION_SCOPE_RECEIPT_MIGRATION_ID,
        RELATION_FREQUENCY_INDEX_MIGRATION_ID,
        RELATION_REBUILD_PROGRESS_MIGRATION_ID,
        VECTOR_RECONCILIATION_MIGRATION_ID,
        RELATION_REBUILD_EXPIRY_MIGRATION_ID,
        RELATION_FREQUENCY_FAILURE_MIGRATION_ID,
        LEXICAL_MIGRATION_ID,
        RELATION_CONTAINMENT_MIGRATION_ID,
    ]
    assert after["schema_version"] == RELATION_CONTAINMENT_SCHEMA_VERSION
    assert after["lexical_generation"]["current"] is True
    assert after["relation_containment"]["current"] is True
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"lexical_generations", "lexical_generation_state"} <= tables
    assert LEXICAL_SHADOW_TABLE not in tables


def test_relation_lease_token_migration_upgrades_pre_0005_queue_in_place():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    historical_checksums = {
        str(row["id"]): str(row["checksum"])
        for row in conn.execute(
            "SELECT id, checksum FROM schema_migrations WHERE id <> ? ORDER BY id",
            (RELATION_REBUILD_LEASE_MIGRATION_ID,),
        ).fetchall()
    }
    conn.execute("ALTER TABLE relation_rebuild_queue DROP COLUMN lease_token")
    conn.execute(
        "DELETE FROM schema_migrations WHERE id=?",
        (RELATION_REBUILD_LEASE_MIGRATION_ID,),
    )
    conn.execute(f"PRAGMA user_version = {OPERATOR_LEDGER_SCHEMA_VERSION}")
    conn.commit()

    before = schema_migration_status(conn)
    assert before["current"] is False
    assert before["missing_migrations"] == [RELATION_REBUILD_LEASE_MIGRATION_ID]
    assert before["relation_rebuild_queue"]["missing_columns"] == ["lease_token"]

    ensure_schema(conn)
    after = schema_migration_status(conn)
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(relation_rebuild_queue)")
    }
    restored_checksums = {
        str(row["id"]): str(row["checksum"])
        for row in conn.execute(
            "SELECT id, checksum FROM schema_migrations WHERE id <> ? ORDER BY id",
            (RELATION_REBUILD_LEASE_MIGRATION_ID,),
        ).fetchall()
    }

    assert "lease_token" in columns
    assert after["current"] is True
    assert after["missing_migrations"] == []
    assert restored_checksums == historical_checksums
    conn.close()


def test_relation_scope_receipt_migration_upgrades_pre_0006_in_place():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    historical_checksums = {
        str(row["id"]): str(row["checksum"])
        for row in conn.execute(
            "SELECT id, checksum FROM schema_migrations WHERE id <> ? ORDER BY id",
            (RELATION_SCOPE_RECEIPT_MIGRATION_ID,),
        ).fetchall()
    }
    for trigger in (
        "trg_relation_scope_revision_insert",
        "trg_relation_scope_revision_update_same",
        "trg_relation_scope_revision_update_move",
        "trg_relation_scope_revision_update_timestamp",
        "trg_relation_scope_revision_cleanup_move",
        "trg_relation_scope_revision_delete",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    conn.execute("DROP TABLE relation_scope_statistics")
    for column in (
        "blocked_entities_sha256",
        "blocked_entities_json",
        "corpus_revision",
    ):
        conn.execute(f"ALTER TABLE relation_rebuild_queue DROP COLUMN {column}")
    conn.execute(
        "DELETE FROM schema_migrations WHERE id=?",
        (RELATION_SCOPE_RECEIPT_MIGRATION_ID,),
    )
    conn.execute(f"PRAGMA user_version = {RELATION_REBUILD_LEASE_SCHEMA_VERSION}")
    conn.commit()

    before = schema_migration_status(conn)
    assert before["current"] is False
    assert before["missing_migrations"] == [RELATION_SCOPE_RECEIPT_MIGRATION_ID]
    assert before["relation_rebuild_queue"]["missing_columns"] == [
        "blocked_entities_json",
        "blocked_entities_sha256",
        "corpus_revision",
    ]
    assert before["relation_rebuild_queue"]["scope_state"]["current"] is False

    ensure_schema(conn)
    after = schema_migration_status(conn)
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(relation_rebuild_queue)")
    }
    restored_checksums = {
        str(row["id"]): str(row["checksum"])
        for row in conn.execute(
            "SELECT id, checksum FROM schema_migrations WHERE id <> ? ORDER BY id",
            (RELATION_SCOPE_RECEIPT_MIGRATION_ID,),
        ).fetchall()
    }

    assert {
        "corpus_revision",
        "blocked_entities_json",
        "blocked_entities_sha256",
    } <= columns
    assert after["current"] is True
    assert after["relation_rebuild_queue"]["scope_state"]["current"] is True
    assert restored_checksums == historical_checksums
    conn.close()


def test_relation_lease_expiry_budget_migration_upgrades_pre_0010_in_place():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    historical_checksums = {
        str(row["id"]): str(row["checksum"])
        for row in conn.execute(
            "SELECT id, checksum FROM schema_migrations WHERE id <> ? ORDER BY id",
            (RELATION_REBUILD_EXPIRY_MIGRATION_ID,),
        ).fetchall()
    }
    conn.execute("ALTER TABLE relation_rebuild_queue DROP COLUMN lease_expirations")
    conn.execute(
        "ALTER TABLE relation_rebuild_queue DROP COLUMN pass_lease_expirations"
    )
    conn.execute(
        "DELETE FROM schema_migrations WHERE id=?",
        (RELATION_REBUILD_EXPIRY_MIGRATION_ID,),
    )
    conn.execute(f"PRAGMA user_version = {VECTOR_RECONCILIATION_SCHEMA_VERSION}")
    conn.commit()

    before = schema_migration_status(conn)
    assert before["current"] is False
    assert before["missing_migrations"] == [RELATION_REBUILD_EXPIRY_MIGRATION_ID]
    assert before["relation_rebuild_queue"]["missing_columns"] == [
        "lease_expirations",
        "pass_lease_expirations",
    ]

    ensure_schema(conn)
    after = schema_migration_status(conn)
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(relation_rebuild_queue)")
    }
    restored_checksums = {
        str(row["id"]): str(row["checksum"])
        for row in conn.execute(
            "SELECT id, checksum FROM schema_migrations WHERE id <> ? ORDER BY id",
            (RELATION_REBUILD_EXPIRY_MIGRATION_ID,),
        ).fetchall()
    }

    assert {"lease_expirations", "pass_lease_expirations"} <= columns
    assert after["current"] is True
    assert after["missing_migrations"] == []
    assert restored_checksums == historical_checksums
    conn.close()


def test_relation_containment_migration_upgrades_pre_0013_in_place():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    historical_checksums = {
        str(row["id"]): str(row["checksum"])
        for row in conn.execute(
            "SELECT id, checksum FROM schema_migrations WHERE id <> ? ORDER BY id",
            (RELATION_CONTAINMENT_MIGRATION_ID,),
        ).fetchall()
    }
    for table in (
        "relation_focus_work_scopes",
        "relation_focus_work",
        "relation_work_dispositions",
        "relation_scope_containment",
    ):
        conn.execute(f"DROP TABLE {table}")
    conn.execute(
        "DELETE FROM schema_migrations WHERE id=?",
        (RELATION_CONTAINMENT_MIGRATION_ID,),
    )
    conn.execute(f"PRAGMA user_version = {LEXICAL_SCHEMA_VERSION}")
    conn.commit()

    before = schema_migration_status(conn)
    assert before["current"] is False
    assert before["missing_migrations"] == [RELATION_CONTAINMENT_MIGRATION_ID]
    assert before["relation_containment"]["current"] is False

    ensure_schema(conn)
    after = schema_migration_status(conn)
    restored_checksums = {
        str(row["id"]): str(row["checksum"])
        for row in conn.execute(
            "SELECT id, checksum FROM schema_migrations WHERE id <> ? ORDER BY id",
            (RELATION_CONTAINMENT_MIGRATION_ID,),
        ).fetchall()
    }

    assert after["current"] is True
    assert after["missing_migrations"] == []
    assert after["relation_containment"]["current"] is True
    assert restored_checksums == historical_checksums
    conn.close()


def test_migrate_status_script_reports_schema_ledger_read_only(tmp_path):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    db_path = storage / "memory.sqlite3"
    writer = sqlite3.connect(db_path)
    writer.row_factory = sqlite3.Row
    try:
        ensure_schema(writer)
        before = writer.total_changes
    finally:
        writer.close()

    proc = subprocess.run(
        [sys.executable, "scripts/migrate.status.py", "--hermes-home", str(hermes_home), "--json"],
        cwd=ROOT,
        encoding="utf-8",
        capture_output=True,
        timeout=60,
        env={"PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["schema_version"] == "migration_status_report.v1"
    assert payload["db"] == str(db_path.resolve())
    assert payload["schema_migrations"]["current"] is True
    assert payload["schema_migrations"]["applied_migrations"][0]["id"] == "0001_baseline_v1_6_0"

    verifier = sqlite3.connect(db_path)
    try:
        assert verifier.total_changes == 0
        assert verifier.execute("PRAGMA user_version").fetchone()[0] == payload["schema_migrations"]["user_version"]
        assert before >= 1
    finally:
        verifier.close()


def test_operator_cli_migrate_status_routes_to_schema_status_script():
    import scope_recall.cli as cli

    assert cli._SCRIPT_COMMANDS[("migrate", "status")][0] == "migrate.status.py"
