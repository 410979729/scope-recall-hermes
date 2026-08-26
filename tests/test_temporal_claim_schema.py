"""Schema and migration contracts for the bitemporal fact ledger."""

from __future__ import annotations

import sqlite3

import pytest

from scope_recall.lexical_generation import (
    LEXICAL_MIGRATION_ID,
    LEXICAL_SCHEMA_VERSION,
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
    RELATION_FREQUENCY_FAILURE_SCHEMA_VERSION,
    RELATION_FREQUENCY_INDEX_MIGRATION_ID,
)
from scope_recall.relation_rebuild_queue import (
    RELATION_REBUILD_EXPIRY_MIGRATION_ID,
    RELATION_REBUILD_EXPIRY_SCHEMA_VERSION,
    RELATION_REBUILD_LEASE_MIGRATION_ID,
    RELATION_REBUILD_LEASE_SCHEMA_VERSION,
    RELATION_REBUILD_MIGRATION_ID,
    RELATION_REBUILD_PROGRESS_MIGRATION_ID,
)
from scope_recall.relation_scope_state import RELATION_SCOPE_RECEIPT_MIGRATION_ID
from scope_recall.sql_store import (
    BASELINE_MIGRATION_ID,
    SCHEMA_VERSION,
    UnsupportedSchemaVersionError,
    ensure_schema,
    schema_migration_status,
)
from scope_recall.temporal_facts import (
    FACT_CLAIMS_MIGRATION_ID,
    FACT_CLAIMS_SCHEMA_VERSION,
    ensure_temporal_fact_schema,
    temporal_fact_schema_status,
)
from scope_recall.vector_reconciliation import (
    VECTOR_RECONCILIATION_MIGRATION_ID,
    VECTOR_RECONCILIATION_SCHEMA_VERSION,
)


REQUIRED_CLAIM_COLUMNS = {
    "claim_id",
    "memory_id",
    "scope_id",
    "subject_key",
    "predicate_key",
    "fact_key",
    "value",
    "normalized_value",
    "value_fingerprint",
    "cardinality",
    "assertion_kind",
    "valid_from",
    "valid_to",
    "recorded_at",
    "retired_at",
    "status",
    "confidence",
    "superseded_by_claim_id",
    "source_type",
    "source_ref",
    "evidence_hash",
    "metadata",
}
REQUIRED_EVIDENCE_COLUMNS = {
    "evidence_id",
    "claim_id",
    "source_type",
    "source_ref",
    "evidence_hash",
    "excerpt",
    "recorded_at",
    "metadata",
}
REQUIRED_INDEXES = {
    "idx_fact_claims_scope_fact_recorded",
    "idx_fact_claims_memory",
    "idx_fact_claims_superseded_by",
    "idx_fact_claim_evidence_claim",
    "uq_fact_claims_current_single_slot",
}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _insert_memory(conn: sqlite3.Connection, memory_id: str, scope_id: str = "scope-1") -> None:
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary, created_at, updated_at
        ) VALUES (?, ?, 'test', 'memory', 'content', 'summary', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
        """,
        (memory_id, scope_id),
    )


def _insert_claim(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    memory_id: str,
    cardinality: str = "single",
    status: str = "current",
) -> None:
    conn.execute(
        """
        INSERT INTO fact_claims(
            claim_id, memory_id, scope_id, subject_key, predicate_key, fact_key,
            value, normalized_value, value_fingerprint, cardinality,
            assertion_kind, recorded_at, status, confidence, source_type
        ) VALUES (?, ?, 'scope-1', 'asha', 'lives_in', 'fact:v1:slot',
                  ?, ?, ?, ?, 'direct', '2026-01-01T00:00:00+00:00',
                  ?, 0.90, 'message')
        """,
        (
            claim_id,
            memory_id,
            f"value-{claim_id}",
            f"value-{claim_id}",
            f"fingerprint-{claim_id}",
            cardinality,
            status,
        ),
    )


def test_fresh_schema_creates_fact_tables_columns_indexes_and_ledger():
    conn = _conn()

    ensure_schema(conn)

    tables = {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    indexes = {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    }
    assert {"fact_claims", "fact_claim_evidence"}.issubset(tables)
    assert REQUIRED_CLAIM_COLUMNS.issubset(_table_columns(conn, "fact_claims"))
    assert REQUIRED_EVIDENCE_COLUMNS.issubset(_table_columns(conn, "fact_claim_evidence"))
    assert REQUIRED_INDEXES.issubset(indexes)

    temporal = temporal_fact_schema_status(conn)
    ledger = schema_migration_status(conn)
    assert temporal["current"] is True
    assert temporal["missing_tables"] == []
    assert temporal["missing_columns"] == {}
    assert temporal["missing_indexes"] == []
    assert temporal["missing_triggers"] == []
    assert temporal["invalid_triggers"] == []
    assert ledger["current"] is True
    assert (
        ledger["user_version"]
        == SCHEMA_VERSION
        == RELATION_CONTAINMENT_SCHEMA_VERSION
    )
    assert (
        SCHEMA_VERSION
        > LEXICAL_SCHEMA_VERSION
        > RELATION_FREQUENCY_FAILURE_SCHEMA_VERSION
        > RELATION_REBUILD_EXPIRY_SCHEMA_VERSION
        > VECTOR_RECONCILIATION_SCHEMA_VERSION
        > RELATION_REBUILD_LEASE_SCHEMA_VERSION
        > OPERATOR_LEDGER_SCHEMA_VERSION
        > FACT_CLAIMS_SCHEMA_VERSION
    )
    assert [row["id"] for row in ledger["applied_migrations"]] == [
        BASELINE_MIGRATION_ID,
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
    assert all(len(str(row["checksum"])) == 64 for row in ledger["applied_migrations"])


def test_temporal_schema_status_reports_missing_successor_trigger():
    conn = _conn()
    ensure_schema(conn)
    conn.execute("DROP TRIGGER trg_fact_claims_successor_match_on_update")

    report = temporal_fact_schema_status(conn)

    assert report["current"] is False
    assert report["missing_triggers"] == [
        "trg_fact_claims_successor_match_on_update"
    ]
    assert report["invalid_triggers"] == []


def test_temporal_helper_preserves_pending_transaction_and_rolls_back_all_ddl():
    conn = _conn()
    conn.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
    conn.commit()
    conn.execute("BEGIN")
    conn.execute("INSERT INTO sentinel(value) VALUES ('pending')")

    ensure_temporal_fact_schema(conn)

    assert conn.in_transaction is True
    assert temporal_fact_schema_status(conn)["current"] is True
    conn.rollback()

    assert conn.execute("SELECT COUNT(*) FROM sentinel").fetchone()[0] == 0
    schema_objects = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE name LIKE 'fact_%' OR name LIKE 'idx_fact_%' "
        "OR name LIKE 'uq_fact_%' OR name LIKE 'trg_fact_%'"
    ).fetchall()
    assert schema_objects == []


def test_temporal_schema_status_detects_and_repairs_trigger_definition_drift():
    conn = _conn()
    ensure_schema(conn)
    name = "trg_fact_claims_successor_match_on_insert"
    conn.execute(f'DROP TRIGGER "{name}"')
    conn.execute(
        f"""
        CREATE TRIGGER {name}
        BEFORE INSERT ON fact_claims
        BEGIN
            SELECT 1;
        END
        """
    )

    drifted = temporal_fact_schema_status(conn)
    assert drifted["current"] is False
    assert drifted["missing_triggers"] == []
    assert drifted["invalid_triggers"] == [name]

    ensure_temporal_fact_schema(conn)

    repaired = temporal_fact_schema_status(conn)
    assert repaired["current"] is True
    assert repaired["invalid_triggers"] == []
    installed_sql = str(
        conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name = ?",
            (name,),
        ).fetchone()[0]
    )
    assert "successor.status NOT IN" in installed_sql
    assert "NEW.retired_at IS NOT NULL" in installed_sql


@pytest.mark.parametrize(
    "replacement_sql",
    [
        'CREATE INDEX "{name}" ON fact_claims(scope_id)',
        'CREATE UNIQUE INDEX "{name}" ON fact_claims(scope_id, subject_key)',
        'CREATE UNIQUE INDEX "{name}" ON fact_claims(scope_id, fact_key)',
    ],
    ids=["non-unique", "wrong-columns", "missing-partial-where"],
)
def test_temporal_schema_detects_and_repairs_same_name_wrong_unique_index(
    replacement_sql,
):
    conn = _conn()
    ensure_schema(conn)
    name = "uq_fact_claims_current_single_slot"
    conn.execute(f'DROP INDEX "{name}"')
    conn.execute(replacement_sql.format(name=name))

    drifted = temporal_fact_schema_status(conn)

    assert drifted["current"] is False
    assert drifted["missing_indexes"] == []
    assert drifted["invalid_indexes"] == [name]

    ensure_temporal_fact_schema(conn)

    repaired = temporal_fact_schema_status(conn)
    assert repaired["current"] is True
    assert repaired["invalid_indexes"] == []
    installed_sql = " ".join(
        str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name = ?",
                (name,),
            ).fetchone()[0]
        ).casefold().split()
    )
    assert "create unique index" in installed_sql
    assert "on fact_claims(scope_id, fact_key)" in installed_sql
    assert "where cardinality = 'single'" in installed_sql
    assert "and status = 'current'" in installed_sql
    assert "and retired_at is null" in installed_sql


def test_fact_schema_ensure_is_idempotent():
    conn = _conn()
    ensure_schema(conn)
    first = temporal_fact_schema_status(conn)
    first_sql = {
        str(row["name"]): str(row["sql"])
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE name LIKE 'fact_claim%' ORDER BY name"
        ).fetchall()
    }

    ensure_schema(conn)

    assert temporal_fact_schema_status(conn) == first
    assert {
        str(row["name"]): str(row["sql"])
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE name LIKE 'fact_claim%' ORDER BY name"
        ).fetchall()
    } == first_sql
    assert conn.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE id = ?",
        (FACT_CLAIMS_MIGRATION_ID,),
    ).fetchone()[0] == 1


def test_legacy_schema_upgrade_adds_fact_ledger_without_rebuilding_memories():
    conn = _conn()
    ensure_schema(conn)
    _insert_memory(conn, "legacy-memory")
    conn.execute("DROP TABLE fact_claim_evidence")
    conn.execute("DROP TABLE fact_claims")
    conn.execute("DELETE FROM schema_migrations WHERE id = ?", (FACT_CLAIMS_MIGRATION_ID,))
    conn.execute("PRAGMA user_version = 10600")

    ensure_schema(conn)

    assert conn.execute("SELECT content FROM memories WHERE id='legacy-memory'").fetchone()[0] == "content"
    assert temporal_fact_schema_status(conn)["current"] is True
    assert schema_migration_status(conn)["current"] is True


def test_future_schema_version_fails_closed_before_any_schema_write():
    conn = _conn()
    future_version = SCHEMA_VERSION + 1
    conn.execute(f"PRAGMA user_version = {future_version}")

    with pytest.raises(UnsupportedSchemaVersionError, match="newer than supported"):
        ensure_schema(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == future_version
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','index')").fetchone()[0] == 0


def test_single_current_slot_is_unique_but_multi_and_retired_rows_can_coexist():
    conn = _conn()
    ensure_schema(conn)
    for memory_id in ("memory-1", "memory-2", "memory-3", "memory-4"):
        _insert_memory(conn, memory_id)

    _insert_claim(conn, claim_id="claim-1", memory_id="memory-1")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_claim(conn, claim_id="claim-2", memory_id="memory-2")
    _insert_claim(
        conn,
        claim_id="claim-3",
        memory_id="memory-3",
        cardinality="single",
        status="superseded",
    )
    _insert_claim(
        conn,
        claim_id="claim-4",
        memory_id="memory-4",
        cardinality="multi",
        status="current",
    )

    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 3


def test_temporal_schema_status_is_read_only(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    writer = sqlite3.connect(db_path)
    writer.row_factory = sqlite3.Row
    try:
        ensure_schema(writer)
    finally:
        writer.close()

    readonly = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    readonly.row_factory = sqlite3.Row
    readonly.execute("PRAGMA query_only=ON")
    try:
        before = readonly.total_changes
        report = temporal_fact_schema_status(readonly)
        after = readonly.total_changes
    finally:
        readonly.close()

    assert report["current"] is True
    assert before == after == 0
