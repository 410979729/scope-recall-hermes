"""SQLite schema helpers for structured bitemporal factual claims.

The module owns only additive DDL and read-only schema inspection. It does not
choose evolution actions, execute lifecycle changes, call providers, or commit
transactions. ``sql_store.ensure_schema`` remains the compatibility entrypoint.
"""

from __future__ import annotations

import sqlite3
from typing import Any

FACT_CLAIMS_SCHEMA_VERSION = 10800
FACT_CLAIMS_MIGRATION_ID = "0002_fact_claims_v1"
FACT_CLAIMS_MIGRATION_PLUGIN_VERSION = "1.8.0"
FACT_CLAIMS_MIGRATION_DESCRIPTION = (
    "Add bitemporal fact claims, evidence links, and idempotent action receipts"
)

FACT_CLAIM_COLUMNS = frozenset(
    {
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
)
FACT_CLAIM_FTS_COLUMNS = frozenset(
    {"claim_id", "memory_id", "subject_key", "predicate_key", "value", "memory_text"}
)
FACT_CLAIM_FTS_MEMBERSHIP_COLUMNS = frozenset({"claim_id"})
FACT_CLAIM_EVIDENCE_COLUMNS = frozenset(
    {
        "evidence_id",
        "claim_id",
        "source_type",
        "source_ref",
        "evidence_hash",
        "excerpt",
        "recorded_at",
        "metadata",
    }
)
FACT_ACTION_RECEIPT_COLUMNS = frozenset(
    {
        "action_id",
        "idempotency_key",
        "request_hash",
        "scope_id",
        "requested_action",
        "effective_action",
        "status",
        "applied",
        "policy_json",
        "receipt_json",
        "error",
        "created_at",
        "updated_at",
    }
)
FACT_CLAIM_INDEXES = frozenset(
    {
        "idx_fact_claims_scope_fact_recorded",
        "idx_fact_claims_memory",
        "idx_fact_claims_superseded_by",
        "idx_fact_claim_evidence_claim",
        "idx_fact_action_receipts_scope_created",
        "idx_fact_action_receipts_status",
        "uq_fact_claims_current_single_slot",
    }
)

FACT_CLAIM_CRITICAL_INDEX_SQL = {
    "uq_fact_claims_current_single_slot": """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_fact_claims_current_single_slot
            ON fact_claims(scope_id, fact_key)
            WHERE cardinality = 'single'
              AND status = 'current'
              AND retired_at IS NULL
    """,
}

FACT_CLAIM_FTS_DDL = """
    CREATE VIRTUAL TABLE IF NOT EXISTS fact_claims_fts USING fts5(
        claim_id UNINDEXED,
        memory_id UNINDEXED,
        subject_key,
        predicate_key,
        value,
        memory_text,
        tokenize='unicode61 remove_diacritics 2'
    )
"""

FACT_CLAIM_DDL_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS fact_claims (
        claim_id TEXT PRIMARY KEY,
        memory_id TEXT NOT NULL,
        scope_id TEXT NOT NULL,
        subject_key TEXT NOT NULL,
        predicate_key TEXT NOT NULL,
        fact_key TEXT NOT NULL,
        value TEXT NOT NULL,
        normalized_value TEXT NOT NULL,
        value_fingerprint TEXT NOT NULL,
        cardinality TEXT NOT NULL CHECK (cardinality IN ('single', 'multi')),
        assertion_kind TEXT NOT NULL DEFAULT 'direct'
            CHECK (assertion_kind IN ('direct', 'inferred', 'validated')),
        valid_from TEXT,
        valid_to TEXT,
        recorded_at TEXT NOT NULL,
        retired_at TEXT,
        status TEXT NOT NULL DEFAULT 'current'
            CHECK (status IN ('current', 'superseded', 'retracted', 'uncertain')),
        confidence REAL NOT NULL DEFAULT 0.50
            CHECK (confidence >= 0.0 AND confidence <= 1.0),
        superseded_by_claim_id TEXT,
        source_type TEXT NOT NULL,
        source_ref TEXT NOT NULL DEFAULT '',
        evidence_hash TEXT NOT NULL DEFAULT '',
        metadata TEXT NOT NULL DEFAULT '{}',
        FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE,
        FOREIGN KEY(superseded_by_claim_id)
            REFERENCES fact_claims(claim_id) ON DELETE SET NULL
            DEFERRABLE INITIALLY DEFERRED,
        CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from),
        CHECK (retired_at IS NULL OR retired_at >= recorded_at)
    )
    """,
    FACT_CLAIM_FTS_DDL,
    """
    CREATE TABLE IF NOT EXISTS fact_claims_fts_membership (
        claim_id TEXT PRIMARY KEY,
        FOREIGN KEY(claim_id) REFERENCES fact_claims(claim_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fact_claim_evidence (
        evidence_id TEXT PRIMARY KEY,
        claim_id TEXT NOT NULL,
        source_type TEXT NOT NULL,
        source_ref TEXT NOT NULL,
        evidence_hash TEXT NOT NULL,
        excerpt TEXT NOT NULL DEFAULT '',
        recorded_at TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}',
        FOREIGN KEY(claim_id) REFERENCES fact_claims(claim_id) ON DELETE CASCADE,
        UNIQUE(claim_id, source_type, source_ref, evidence_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fact_action_receipts (
        action_id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        request_hash TEXT NOT NULL,
        scope_id TEXT NOT NULL,
        requested_action TEXT NOT NULL,
        effective_action TEXT NOT NULL,
        status TEXT NOT NULL,
        applied INTEGER NOT NULL DEFAULT 0 CHECK (applied IN (0, 1)),
        policy_json TEXT NOT NULL DEFAULT '{}',
        receipt_json TEXT NOT NULL DEFAULT '{}',
        error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (requested_action IN ('noop', 'add', 'enrich', 'supersede', 'retract', 'review')),
        CHECK (effective_action IN ('noop', 'add', 'enrich', 'supersede', 'retract', 'review'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_fact_claims_scope_fact_recorded
        ON fact_claims(scope_id, fact_key, recorded_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_fact_claims_memory
        ON fact_claims(memory_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_fact_claims_superseded_by
        ON fact_claims(superseded_by_claim_id)
        WHERE superseded_by_claim_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_fact_claim_evidence_claim
        ON fact_claim_evidence(claim_id, recorded_at ASC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_fact_action_receipts_scope_created
        ON fact_action_receipts(scope_id, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_fact_action_receipts_status
        ON fact_action_receipts(status, updated_at DESC)
    """,
    *FACT_CLAIM_CRITICAL_INDEX_SQL.values(),
)

FACT_CLAIM_TRIGGER_SQL = {
    "trg_fact_claims_successor_match_on_insert": """
        CREATE TRIGGER IF NOT EXISTS trg_fact_claims_successor_match_on_insert
        BEFORE INSERT ON fact_claims
        WHEN (
            NEW.superseded_by_claim_id IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM fact_claims AS successor
                WHERE successor.claim_id = NEW.superseded_by_claim_id
                  AND (
                      successor.scope_id <> NEW.scope_id
                      OR successor.fact_key <> NEW.fact_key
                      OR successor.status NOT IN ('current', 'uncertain')
                      OR successor.retired_at IS NOT NULL
                  )
            )
        ) OR EXISTS (
            SELECT 1 FROM fact_claims AS predecessor
            WHERE predecessor.superseded_by_claim_id = NEW.claim_id
              AND (
                  predecessor.scope_id <> NEW.scope_id
                  OR predecessor.fact_key <> NEW.fact_key
                  OR NEW.status NOT IN ('current', 'uncertain')
                  OR NEW.retired_at IS NOT NULL
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'fact successor invariant mismatch');
        END
    """,
    "trg_fact_claims_successor_match_on_update": """
        CREATE TRIGGER IF NOT EXISTS trg_fact_claims_successor_match_on_update
        BEFORE UPDATE OF superseded_by_claim_id ON fact_claims
        WHEN NEW.superseded_by_claim_id IS NOT NULL
         AND EXISTS (
            SELECT 1 FROM fact_claims AS successor
            WHERE successor.claim_id = NEW.superseded_by_claim_id
              AND (
                  successor.scope_id <> NEW.scope_id
                  OR successor.fact_key <> NEW.fact_key
                  OR successor.status NOT IN ('current', 'uncertain')
                  OR successor.retired_at IS NOT NULL
              )
         )
        BEGIN
            SELECT RAISE(ABORT, 'fact successor invariant mismatch');
        END
    """,
    "trg_fact_claims_predecessor_match_on_identity_update": """
        CREATE TRIGGER IF NOT EXISTS trg_fact_claims_predecessor_match_on_identity_update
        BEFORE UPDATE OF scope_id, fact_key ON fact_claims
        WHEN EXISTS (
            SELECT 1 FROM fact_claims AS predecessor
            WHERE predecessor.superseded_by_claim_id = NEW.claim_id
              AND (
                  predecessor.scope_id <> NEW.scope_id
                  OR predecessor.fact_key <> NEW.fact_key
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'fact predecessor scope/fact mismatch');
        END
    """,
    "trg_fact_claims_outgoing_match_on_identity_update": """
        CREATE TRIGGER IF NOT EXISTS trg_fact_claims_outgoing_match_on_identity_update
        BEFORE UPDATE OF scope_id, fact_key ON fact_claims
        WHEN NEW.superseded_by_claim_id IS NOT NULL
         AND EXISTS (
            SELECT 1 FROM fact_claims AS successor
            WHERE successor.claim_id = NEW.superseded_by_claim_id
              AND (
                  successor.scope_id <> NEW.scope_id
                  OR successor.fact_key <> NEW.fact_key
              )
         )
        BEGIN
            SELECT RAISE(ABORT, 'fact outgoing successor scope/fact mismatch');
        END
    """,
    "trg_fact_claims_fts_insert": """
        CREATE TRIGGER IF NOT EXISTS trg_fact_claims_fts_insert
        AFTER INSERT ON fact_claims
        BEGIN
            INSERT INTO fact_claims_fts(
                claim_id, memory_id, subject_key, predicate_key, value, memory_text
            )
            SELECT
                NEW.claim_id,
                NEW.memory_id,
                NEW.subject_key,
                NEW.predicate_key,
                NEW.value,
                COALESCE(memory.content, '') || ' ' || COALESCE(memory.summary, '')
            FROM memories AS memory
            WHERE memory.id = NEW.memory_id
              AND memory.scope_id = NEW.scope_id;
            INSERT INTO fact_claims_fts_membership(claim_id)
            SELECT NEW.claim_id
            FROM memories AS memory
            WHERE memory.id = NEW.memory_id
              AND memory.scope_id = NEW.scope_id;
        END
    """,
    "trg_fact_claims_fts_update": """
        CREATE TRIGGER IF NOT EXISTS trg_fact_claims_fts_update
        AFTER UPDATE OF memory_id, scope_id, subject_key, predicate_key, value ON fact_claims
        BEGIN
            DELETE FROM fact_claims_fts WHERE claim_id = OLD.claim_id;
            DELETE FROM fact_claims_fts_membership WHERE claim_id = OLD.claim_id;
            INSERT INTO fact_claims_fts(
                claim_id, memory_id, subject_key, predicate_key, value, memory_text
            )
            SELECT
                NEW.claim_id,
                NEW.memory_id,
                NEW.subject_key,
                NEW.predicate_key,
                NEW.value,
                COALESCE(memory.content, '') || ' ' || COALESCE(memory.summary, '')
            FROM memories AS memory
            WHERE memory.id = NEW.memory_id
              AND memory.scope_id = NEW.scope_id;
            INSERT INTO fact_claims_fts_membership(claim_id)
            SELECT NEW.claim_id
            FROM memories AS memory
            WHERE memory.id = NEW.memory_id
              AND memory.scope_id = NEW.scope_id;
        END
    """,
    "trg_fact_claims_fts_delete": """
        CREATE TRIGGER IF NOT EXISTS trg_fact_claims_fts_delete
        AFTER DELETE ON fact_claims
        BEGIN
            DELETE FROM fact_claims_fts WHERE claim_id = OLD.claim_id;
            DELETE FROM fact_claims_fts_membership WHERE claim_id = OLD.claim_id;
        END
    """,
    "trg_memories_fact_claims_fts_update": """
        CREATE TRIGGER IF NOT EXISTS trg_memories_fact_claims_fts_update
        AFTER UPDATE OF content, summary ON memories
        BEGIN
            UPDATE fact_claims_fts
            SET memory_text = COALESCE(NEW.content, '') || ' ' || COALESCE(NEW.summary, '')
            WHERE memory_id = NEW.id;
        END
    """,
    "trg_memories_fact_claims_fts_delete": """
        CREATE TRIGGER IF NOT EXISTS trg_memories_fact_claims_fts_delete
        BEFORE DELETE ON memories
        BEGIN
            DELETE FROM fact_claims_fts WHERE memory_id = OLD.id;
            DELETE FROM fact_claims_fts_membership
            WHERE claim_id IN (
                SELECT claim_id FROM fact_claims WHERE memory_id = OLD.id
            );
        END
    """,
}
FACT_CLAIM_TRIGGERS = frozenset(FACT_CLAIM_TRIGGER_SQL)


def _normalized_schema_sql(sql: str) -> str:
    normalized = " ".join(sql.strip().rstrip(";").split()).casefold()
    return (
        normalized.replace("create trigger if not exists ", "create trigger ")
        .replace("create unique index if not exists ", "create unique index ")
        .replace("create index if not exists ", "create index ")
    )


def _installed_index_sql(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): str(row[1] or "")
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' "
            "AND tbl_name IN ('fact_claims', 'fact_claim_evidence', "
            "'fact_action_receipts')"
        ).fetchall()
    }


def _installed_trigger_sql(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): str(row[1] or "")
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }


def ensure_temporal_fact_schema(conn: sqlite3.Connection) -> None:
    """Create or repair the fact-ledger schema in the caller transaction.

    Every DDL statement uses ``Connection.execute`` rather than ``executescript``
    so a pending caller transaction is never committed implicitly. Trigger SQL is
    a single source of truth; definition drift is repaired transactionally.
    Existing data is rebuilt only for the one-time FTS-membership migration or
    an actual FTS schema replacement. Routine startup never performs a full FTS
    consistency scan; doctor/repair owns that explicit operational work.
    The caller alone owns commit or rollback.
    """

    membership_existed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='fact_claims_fts_membership'"
    ).fetchone() is not None
    rebuild_fts = not membership_existed
    for statement in FACT_CLAIM_DDL_STATEMENTS:
        conn.execute(statement)

    has_memories_table = "memories" in {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
        ).fetchall()
    }
    fts_columns = _table_columns(conn, "fact_claims_fts")
    if not FACT_CLAIM_FTS_COLUMNS.issubset(fts_columns):
        for trigger_name in FACT_CLAIM_TRIGGER_SQL:
            if "_fts_" in trigger_name:
                conn.execute(f'DROP TRIGGER IF EXISTS "{trigger_name}"')
        conn.execute("DROP TABLE fact_claims_fts")
        conn.execute(FACT_CLAIM_FTS_DDL)
        rebuild_fts = True

    installed_indexes = _installed_index_sql(conn)
    for name, expected_sql in FACT_CLAIM_CRITICAL_INDEX_SQL.items():
        actual_sql = installed_indexes.get(name)
        if (
            actual_sql is not None
            and _normalized_schema_sql(actual_sql)
            != _normalized_schema_sql(expected_sql)
        ):
            conn.execute(f'DROP INDEX "{name}"')
        conn.execute(expected_sql)

    installed = _installed_trigger_sql(conn)
    for name, expected_sql in FACT_CLAIM_TRIGGER_SQL.items():
        if name.startswith("trg_memories_") and not has_memories_table:
            continue
        actual_sql = installed.get(name)
        if actual_sql is not None and _normalized_schema_sql(actual_sql) != _normalized_schema_sql(expected_sql):
            conn.execute(f'DROP TRIGGER "{name}"')
        conn.execute(expected_sql)

    if has_memories_table and rebuild_fts:
        # One-time, linear migration. Rebuilding avoids an O(N²) membership
        # probe against the UNINDEXED FTS claim_id column and makes the companion
        # primary-key table authoritative for all subsequent trigger updates.
        conn.execute("DELETE FROM fact_claims_fts")
        conn.execute("DELETE FROM fact_claims_fts_membership")
        conn.execute(
            """
            INSERT INTO fact_claims_fts(
                claim_id, memory_id, subject_key, predicate_key, value, memory_text
            )
            SELECT
                fc.claim_id,
                fc.memory_id,
                fc.subject_key,
                fc.predicate_key,
                fc.value,
                COALESCE(memory.content, '') || ' ' || COALESCE(memory.summary, '')
            FROM fact_claims AS fc
            JOIN memories AS memory
              ON memory.id = fc.memory_id
             AND memory.scope_id = fc.scope_id
            """
        )
        conn.execute(
            """
            INSERT INTO fact_claims_fts_membership(claim_id)
            SELECT fc.claim_id
            FROM fact_claims AS fc
            JOIN memories AS memory
              ON memory.id = fc.memory_id
             AND memory.scope_id = fc.scope_id
            """
        )


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _claim_id_set_difference(
    conn: sqlite3.Connection,
    *,
    left_table: str,
    right_table: str,
    sample_limit: int,
) -> dict[str, Any]:
    difference_sql = (
        f"SELECT claim_id FROM {left_table} "
        f"EXCEPT SELECT claim_id FROM {right_table}"
    )
    count = int(
        conn.execute(f"SELECT COUNT(*) FROM ({difference_sql})").fetchone()[0]
    )
    bounded_limit = max(1, min(100, int(sample_limit)))
    sample = [
        str(row[0])
        for row in conn.execute(
            f"SELECT claim_id FROM ({difference_sql}) "
            "ORDER BY claim_id ASC LIMIT ?",
            (bounded_limit,),
        ).fetchall()
    ]
    return {
        "count": count,
        "sample_claim_ids": sample,
        "sample_truncated": count > bounded_limit,
    }


def fact_fts_integrity_status(
    conn: sqlite3.Connection,
    *,
    verify_membership_sets: bool = False,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Report fact FTS counts and optional exact bidirectional set identity.

    Routine startup uses the bounded count contract. Canonical doctor opts into
    the more expensive set comparison so equal counts cannot hide swapped or
    orphaned claim identifiers.
    """

    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('fact_claims', 'fact_claims_fts', "
            "'fact_claims_fts_membership')"
        ).fetchall()
    }
    required = {
        "fact_claims",
        "fact_claims_fts",
        "fact_claims_fts_membership",
    }
    missing_tables = sorted(required - tables)
    payload: dict[str, Any] = {
        "claim_count": 0,
        "fts_row_count": 0,
        "fts_distinct_claim_count": 0,
        "membership_count": 0,
        "membership_sets_checked": bool(verify_membership_sets),
        "set_differences": {},
        "missing_tables": missing_tables,
        "current": False,
    }
    if missing_tables:
        return payload

    claim_count = int(conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0])
    fts_counts = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT claim_id) FROM fact_claims_fts"
    ).fetchone()
    membership_count = int(
        conn.execute("SELECT COUNT(*) FROM fact_claims_fts_membership").fetchone()[0]
    )
    count_current = (
        claim_count
        == int(fts_counts[0])
        == int(fts_counts[1])
        == membership_count
    )
    set_differences: dict[str, dict[str, Any]] = {}
    if verify_membership_sets:
        for name, left_table, right_table in (
            ("claims_missing_from_fts", "fact_claims", "fact_claims_fts"),
            ("fts_orphans", "fact_claims_fts", "fact_claims"),
            (
                "claims_missing_from_membership",
                "fact_claims",
                "fact_claims_fts_membership",
            ),
            (
                "membership_orphans",
                "fact_claims_fts_membership",
                "fact_claims",
            ),
        ):
            set_differences[name] = _claim_id_set_difference(
                conn,
                left_table=left_table,
                right_table=right_table,
                sample_limit=sample_limit,
            )
    sets_current = not any(
        int(summary.get("count") or 0) for summary in set_differences.values()
    )
    payload.update(
        {
            "claim_count": claim_count,
            "fts_row_count": int(fts_counts[0]),
            "fts_distinct_claim_count": int(fts_counts[1]),
            "membership_count": membership_count,
            "set_differences": set_differences,
            "current": count_current and sets_current,
        }
    )
    return payload


def temporal_fact_schema_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return a bounded, read-only report for the temporal fact schema."""

    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('fact_claims', 'fact_claims_fts', "
            "'fact_claims_fts_membership', 'fact_claim_evidence', "
            "'fact_action_receipts')"
        ).fetchall()
    }
    required_columns = {
        "fact_claims": FACT_CLAIM_COLUMNS,
        "fact_claims_fts": FACT_CLAIM_FTS_COLUMNS,
        "fact_claims_fts_membership": FACT_CLAIM_FTS_MEMBERSHIP_COLUMNS,
        "fact_claim_evidence": FACT_CLAIM_EVIDENCE_COLUMNS,
        "fact_action_receipts": FACT_ACTION_RECEIPT_COLUMNS,
    }
    missing_tables = sorted(set(required_columns) - tables)
    missing_columns: dict[str, list[str]] = {}
    for table, expected in required_columns.items():
        if table not in tables:
            continue
        missing = sorted(expected - _table_columns(conn, table))
        if missing:
            missing_columns[table] = missing

    fts_integrity = fact_fts_integrity_status(conn)
    indexes = _installed_index_sql(conn)
    missing_indexes = sorted(FACT_CLAIM_INDEXES - indexes.keys())
    invalid_indexes = sorted(
        name
        for name, expected_sql in FACT_CLAIM_CRITICAL_INDEX_SQL.items()
        if name in indexes
        and _normalized_schema_sql(indexes[name])
        != _normalized_schema_sql(expected_sql)
    )
    triggers = _installed_trigger_sql(conn)
    has_memories_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'"
    ).fetchone() is not None
    expected_triggers = {
        name
        for name in FACT_CLAIM_TRIGGERS
        if has_memories_table or not name.startswith("trg_memories_")
    }
    missing_triggers = sorted(expected_triggers - triggers.keys())
    invalid_triggers = sorted(
        name
        for name, expected_sql in FACT_CLAIM_TRIGGER_SQL.items()
        if name in expected_triggers
        and name in triggers
        and _normalized_schema_sql(triggers[name]) != _normalized_schema_sql(expected_sql)
    )
    current = (
        not missing_tables
        and not missing_columns
        and not missing_indexes
        and not invalid_indexes
        and not missing_triggers
        and not invalid_triggers
        and bool(fts_integrity["current"])
    )
    return {
        "schema_version": FACT_CLAIMS_SCHEMA_VERSION,
        "migration_id": FACT_CLAIMS_MIGRATION_ID,
        "current": current,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
        "invalid_indexes": invalid_indexes,
        "missing_triggers": missing_triggers,
        "invalid_triggers": invalid_triggers,
        "fts_integrity": fts_integrity,
    }


__all__ = [
    "FACT_CLAIMS_MIGRATION_DESCRIPTION",
    "FACT_CLAIMS_MIGRATION_ID",
    "FACT_CLAIMS_MIGRATION_PLUGIN_VERSION",
    "FACT_CLAIMS_SCHEMA_VERSION",
    "FACT_CLAIM_CRITICAL_INDEX_SQL",
    "FACT_CLAIM_TRIGGER_SQL",
    "FACT_CLAIM_TRIGGERS",
    "ensure_temporal_fact_schema",
    "fact_fts_integrity_status",
    "temporal_fact_schema_status",
]
