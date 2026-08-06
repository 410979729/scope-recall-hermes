"""Recoverable supplemental lexical-index generations for CJK recall.

The legacy ``memories_fts`` table remains authoritative for its existing lexical
channel.  This module owns an explicit shadow FTS5 trigram generation, bounded
backfill progress, truth-table maintenance triggers, integrity evidence, and a
compare-and-swap activation pointer.  Creating the additive metadata schema does
not create or activate the shadow index.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .lifecycle_policy import ordinary_recall_lifecycle_visible_sql

LEXICAL_SCHEMA_VERSION = 10812
LEXICAL_MIGRATION_ID = "0012_lexical_shadow_index_v1_9_0"
LEXICAL_MIGRATION_PLUGIN_VERSION = "1.9.0"
LEXICAL_MIGRATION_DESCRIPTION = (
    "Recoverable supplemental CJK lexical shadow index and activation pointer"
)
LEXICAL_GENERATION_ID = "cjk-trigram-v1"
LEXICAL_SHADOW_TABLE = "memories_fts_cjk_v1"
_CURRENT_KEY = "current_generation"
_TRIGGER_NAMES = (
    "trg_lexical_cjk_v1_insert",
    "trg_lexical_cjk_v1_update",
    "trg_lexical_cjk_v1_delete",
)
LEXICAL_QUALITY_PROVENANCE = {
    "contract": "scope-recall.lexical-quality.v1",
    "evaluator": "lexical_migration.validate_lexical_generation",
    "synthetic_corpus": "cjk-high-interference-v1",
    "retrieval_algorithm": "legacy-plus-shadow-v1",
}
_REQUIRED_TABLES = {"lexical_generations", "lexical_generation_state"}
_REQUIRED_GENERATION_COLUMNS = {
    "generation_id",
    "table_name",
    "tokenizer",
    "status",
    "source_max_rowid",
    "last_backfilled_rowid",
    "quality_ok",
    "quality_json",
    "created_at",
    "updated_at",
    "activated_at",
    "error",
}



class LexicalGenerationError(RuntimeError):
    """A lexical generation violated migration, integrity, or CAS policy."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _trigger_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }


def _require_supported_generation(generation_id: str) -> None:
    if str(generation_id) != LEXICAL_GENERATION_ID:
        raise LexicalGenerationError(
            f"unsupported lexical generation: {generation_id!r}"
        )


def ensure_lexical_generation_schema(conn: sqlite3.Connection) -> None:
    """Create additive manifest/pointer tables without creating shadow storage."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lexical_generations (
            generation_id TEXT PRIMARY KEY,
            table_name TEXT NOT NULL UNIQUE,
            tokenizer TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('building', 'ready', 'active', 'failed', 'retired')
            ),
            source_max_rowid INTEGER NOT NULL DEFAULT 0,
            last_backfilled_rowid INTEGER NOT NULL DEFAULT 0,
            quality_ok INTEGER NOT NULL DEFAULT 0,
            quality_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            activated_at TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lexical_generation_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO lexical_generation_state(key, value, updated_at)
        VALUES (?, '', ?)
        """,
        (_CURRENT_KEY, _now_iso()),
    )


def lexical_schema_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Inspect additive lexical metadata schema without mutating the database."""

    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing_tables = sorted(_REQUIRED_TABLES - tables)
    missing_columns: list[str] = []
    if "lexical_generations" in tables:
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(lexical_generations)")
        }
        missing_columns = sorted(_REQUIRED_GENERATION_COLUMNS - columns)
    return {
        "current": not missing_tables and not missing_columns,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
    }


def current_generation_id(conn: sqlite3.Connection) -> str:
    """Read the active supplemental generation pointer without schema writes."""

    if not _table_exists(conn, "lexical_generation_state"):
        return ""
    row = conn.execute(
        "SELECT value FROM lexical_generation_state WHERE key=?",
        (_CURRENT_KEY,),
    ).fetchone()
    return str(row[0] or "") if row else ""


def _install_shadow_triggers(conn: sqlite3.Connection) -> None:
    visible_new = ordinary_recall_lifecycle_visible_sql("NEW")
    conn.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAMES[0]}")
    conn.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAMES[1]}")
    conn.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAMES[2]}")
    conn.execute(
        f"""
        CREATE TRIGGER {_TRIGGER_NAMES[0]}
        AFTER INSERT ON memories
        WHEN {visible_new}
        BEGIN
            INSERT INTO {LEXICAL_SHADOW_TABLE}(memory_id, content, summary)
            VALUES (NEW.id, NEW.content, NEW.summary);
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER {_TRIGGER_NAMES[1]}
        AFTER UPDATE ON memories
        BEGIN
            DELETE FROM {LEXICAL_SHADOW_TABLE} WHERE memory_id = OLD.id;
            INSERT INTO {LEXICAL_SHADOW_TABLE}(memory_id, content, summary)
            SELECT NEW.id, NEW.content, NEW.summary
            WHERE {visible_new};
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER {_TRIGGER_NAMES[2]}
        AFTER DELETE ON memories
        BEGIN
            DELETE FROM {LEXICAL_SHADOW_TABLE} WHERE memory_id = OLD.id;
        END
        """
    )


def create_shadow_generation(
    conn: sqlite3.Connection,
    generation_id: str = LEXICAL_GENERATION_ID,
) -> dict[str, Any]:
    """Create or resume the explicit shadow generation without backfilling it."""

    _require_supported_generation(generation_id)
    ensure_lexical_generation_schema(conn)
    try:
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {LEXICAL_SHADOW_TABLE} USING fts5(
                memory_id UNINDEXED,
                content,
                summary,
                tokenize='trigram'
            )
            """
        )
    except sqlite3.OperationalError as exc:
        raise LexicalGenerationError(
            "SQLite FTS5 trigram tokenizer is unavailable"
        ) from exc
    _install_shadow_triggers(conn)
    max_row = int(
        conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM memories").fetchone()[0]
    )
    now = _now_iso()
    existing = conn.execute(
        "SELECT status, source_max_rowid FROM lexical_generations WHERE generation_id=?",
        (generation_id,),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO lexical_generations(
                generation_id, table_name, tokenizer, status,
                source_max_rowid, last_backfilled_rowid,
                quality_ok, quality_json, created_at, updated_at,
                activated_at, error
            ) VALUES (?, ?, 'trigram', 'building', ?, 0, 0, '{}', ?, ?, '', '')
            """,
            (generation_id, LEXICAL_SHADOW_TABLE, max_row, now, now),
        )
    else:
        status = str(existing[0] or "")
        if status == "retired":
            raise LexicalGenerationError(
                "retired lexical generation cannot be resumed in place"
            )
        if status in {"building", "failed"}:
            conn.execute(
                """
                UPDATE lexical_generations
                SET status='building', source_max_rowid=MAX(source_max_rowid, ?),
                    updated_at=?, error=''
                WHERE generation_id=?
                """,
                (max_row, now, generation_id),
            )
    return generation_status(conn, generation_id)


def generation_status(
    conn: sqlite3.Connection,
    generation_id: str = LEXICAL_GENERATION_ID,
) -> dict[str, Any]:
    """Return one generation manifest with parsed quality evidence."""

    if not _table_exists(conn, "lexical_generations"):
        return {
            "generation_id": generation_id,
            "status": "schema_missing",
            "quality_ok": False,
        }
    row = conn.execute(
        "SELECT * FROM lexical_generations WHERE generation_id=?",
        (generation_id,),
    ).fetchone()
    if row is None:
        return {
            "generation_id": generation_id,
            "status": "absent",
            "quality_ok": False,
        }
    columns = [str(item[0]) for item in conn.execute(
        "SELECT * FROM lexical_generations WHERE generation_id=?",
        (generation_id,),
    ).description or []]
    payload = {columns[index]: row[index] for index in range(len(columns))}
    payload["quality_ok"] = bool(payload.get("quality_ok"))
    try:
        quality = json.loads(str(payload.get("quality_json") or "{}"))
    except Exception:
        quality = {}
    payload["quality"] = quality if isinstance(quality, dict) else {}
    payload["current_generation_id"] = current_generation_id(conn)
    return payload


def backfill_generation(
    conn: sqlite3.Connection,
    generation_id: str = LEXICAL_GENERATION_ID,
    *,
    batch_size: int = 500,
    reconcile: bool = False,
) -> dict[str, Any]:
    """Reconcile one bounded rowid page into the shadow table.

    Progress and FTS rows are changed in the caller's transaction.  A crash before
    commit therefore advances neither side, making resume idempotent.
    """

    _require_supported_generation(generation_id)
    if batch_size < 1 or batch_size > 10_000:
        raise ValueError("batch_size must be between 1 and 10000")
    manifest = generation_status(conn, generation_id)
    if manifest.get("status") in {"absent", "schema_missing"}:
        raise LexicalGenerationError("shadow generation has not been created")
    if not _table_exists(conn, LEXICAL_SHADOW_TABLE):
        raise LexicalGenerationError("shadow generation storage is missing")
    if reconcile:
        source_max = int(
            conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM memories").fetchone()[0]
        )
        start = 0
        conn.execute(
            """
            UPDATE lexical_generations
            SET status='building', source_max_rowid=?, last_backfilled_rowid=0,
                quality_ok=0, quality_json='{}', updated_at=?, error=''
            WHERE generation_id=?
            """,
            (source_max, _now_iso(), generation_id),
        )
    else:
        source_max = int(manifest.get("source_max_rowid") or 0)
        start = int(manifest.get("last_backfilled_rowid") or 0)
    rows = conn.execute(
        """
        SELECT rowid, id
        FROM memories
        WHERE rowid > ? AND rowid <= ?
        ORDER BY rowid ASC
        LIMIT ?
        """,
        (start, source_max, batch_size),
    ).fetchall()
    visible = ordinary_recall_lifecycle_visible_sql("memories")
    for row in rows:
        memory_id = str(row[1])
        conn.execute(
            f"DELETE FROM {LEXICAL_SHADOW_TABLE} WHERE memory_id=?",
            (memory_id,),
        )
        conn.execute(
            f"""
            INSERT INTO {LEXICAL_SHADOW_TABLE}(memory_id, content, summary)
            SELECT id, content, summary
            FROM memories
            WHERE id=? AND {visible}
            """,
            (memory_id,),
        )
    last = int(rows[-1][0]) if rows else source_max
    complete = last >= source_max
    conn.execute(
        """
        UPDATE lexical_generations
        SET last_backfilled_rowid=?, updated_at=?
        WHERE generation_id=?
        """,
        (last, _now_iso(), generation_id),
    )
    return {
        "generation_id": generation_id,
        "status": "building",
        "processed": len(rows),
        "last_backfilled_rowid": last,
        "source_max_rowid": source_max,
        "complete": complete,
    }


def generation_integrity_report(
    conn: sqlite3.Connection,
    generation_id: str = LEXICAL_GENERATION_ID,
) -> dict[str, Any]:
    """Compare shadow membership/content to recall-visible SQLite truth rows."""

    _require_supported_generation(generation_id)
    table_present = _table_exists(conn, LEXICAL_SHADOW_TABLE)
    present_triggers = _trigger_names(conn)
    missing_triggers = sorted(set(_TRIGGER_NAMES) - present_triggers)
    if not table_present:
        return {
            "generation_id": generation_id,
            "healthy": False,
            "table_present": False,
            "missing_triggers": missing_triggers,
            "expected_rows": 0,
            "indexed_rows": 0,
            "missing_rows": 0,
            "stale_rows": 0,
            "hidden_rows": 0,
            "duplicate_rows": 0,
            "content_drift_rows": 0,
        }
    visible_m = ordinary_recall_lifecycle_visible_sql("m")
    expected = int(
        conn.execute(f"SELECT COUNT(*) FROM memories m WHERE {visible_m}").fetchone()[0]
    )
    indexed = int(
        conn.execute(f"SELECT COUNT(*) FROM {LEXICAL_SHADOW_TABLE}").fetchone()[0]
    )
    distinct = int(
        conn.execute(
            f"SELECT COUNT(DISTINCT memory_id) FROM {LEXICAL_SHADOW_TABLE}"
        ).fetchone()[0]
    )
    missing = int(
        conn.execute(
            f"""
            SELECT COUNT(*) FROM memories m
            WHERE {visible_m}
              AND NOT EXISTS (
                  SELECT 1 FROM {LEXICAL_SHADOW_TABLE} f WHERE f.memory_id=m.id
              )
            """
        ).fetchone()[0]
    )
    stale = int(
        conn.execute(
            f"""
            SELECT COUNT(*) FROM {LEXICAL_SHADOW_TABLE} f
            WHERE NOT EXISTS (SELECT 1 FROM memories m WHERE m.id=f.memory_id)
            """
        ).fetchone()[0]
    )
    hidden = int(
        conn.execute(
            f"""
            SELECT COUNT(*) FROM {LEXICAL_SHADOW_TABLE} f
            JOIN memories m ON m.id=f.memory_id
            WHERE NOT ({visible_m})
            """
        ).fetchone()[0]
    )
    drift = int(
        conn.execute(
            f"""
            SELECT COUNT(*) FROM {LEXICAL_SHADOW_TABLE} f
            JOIN memories m ON m.id=f.memory_id
            WHERE f.content <> m.content OR f.summary <> m.summary
            """
        ).fetchone()[0]
    )
    duplicates = max(0, indexed - distinct)
    healthy = (
        not missing_triggers
        and indexed == expected
        and missing == 0
        and stale == 0
        and hidden == 0
        and duplicates == 0
        and drift == 0
    )
    return {
        "generation_id": generation_id,
        "healthy": healthy,
        "table_present": True,
        "missing_triggers": missing_triggers,
        "expected_rows": expected,
        "indexed_rows": indexed,
        "missing_rows": missing,
        "stale_rows": stale,
        "hidden_rows": hidden,
        "duplicate_rows": duplicates,
        "content_drift_rows": drift,
    }


def lexical_source_binding(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return a privacy-safe logical fingerprint of ordinary-recall truth rows."""

    visible_m = ordinary_recall_lifecycle_visible_sql("m")
    rows = conn.execute(
        f"""
        SELECT m.rowid, m.id, m.updated_at, m.target, m.content, m.summary, m.metadata
        FROM memories m
        WHERE {visible_m}
        ORDER BY m.rowid
        """
    ).fetchall()
    digest = hashlib.sha256()
    for row in rows:
        for value in row:
            encoded = str(value if value is not None else "").encode(
                "utf-8", errors="surrogatepass"
            )
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    max_rowid = int(
        conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM memories").fetchone()[0]
    )
    return {
        "algorithm": "sha256-visible-memory-v1",
        "fingerprint": digest.hexdigest(),
        "visible_rows": len(rows),
        "max_rowid": max_rowid,
    }


def lexical_quality_evidence_fingerprint(receipt: dict[str, Any]) -> str:
    """Hash a structured quality receipt without its self-hash field."""

    payload = {
        key: value
        for key, value in receipt.items()
        if key != "evidence_fingerprint"
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


_QUALITY_RECEIPT_KEYS = {
    "ok",
    "status",
    "generation_id",
    "synthetic_cjk_queries",
    "synthetic_cjk_expected_found",
    "live_cjk_queries",
    "live_cjk_expected_found",
    "english_queries",
    "english_regressions",
    "cjk_queries",
    "cjk_expected_found",
    "integrity",
    "source_binding",
    "provenance",
    "evidence_fingerprint",
    "contains_raw_samples",
}


def _quality_count(receipt: dict[str, Any], key: str) -> int:
    value = receipt.get(key)
    if type(value) is not int or int(value) < 0:
        raise LexicalGenerationError("quality receipt count schema is invalid")
    return int(value)


def _validate_quality_receipt(
    conn: sqlite3.Connection,
    generation_id: str,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Validate and canonicalize the complete privacy-safe quality contract."""

    if not isinstance(receipt, dict) or set(receipt) != _QUALITY_RECEIPT_KEYS:
        raise LexicalGenerationError("quality receipt schema is invalid")
    if receipt.get("ok") is not True or receipt.get("status") != "ready":
        raise LexicalGenerationError("quality receipt is not approved")
    if receipt.get("generation_id") != generation_id:
        raise LexicalGenerationError("quality receipt generation binding is invalid")
    if receipt.get("contains_raw_samples") is not False:
        raise LexicalGenerationError("quality receipt privacy contract is invalid")
    if receipt.get("provenance") != LEXICAL_QUALITY_PROVENANCE:
        raise LexicalGenerationError("quality receipt provenance is invalid")
    if receipt.get("evidence_fingerprint") != lexical_quality_evidence_fingerprint(
        receipt
    ):
        raise LexicalGenerationError("quality receipt evidence fingerprint is invalid")

    synthetic_queries = _quality_count(receipt, "synthetic_cjk_queries")
    synthetic_found = _quality_count(receipt, "synthetic_cjk_expected_found")
    live_queries = _quality_count(receipt, "live_cjk_queries")
    live_found = _quality_count(receipt, "live_cjk_expected_found")
    english_queries = _quality_count(receipt, "english_queries")
    english_regressions = _quality_count(receipt, "english_regressions")
    cjk_queries = _quality_count(receipt, "cjk_queries")
    cjk_found = _quality_count(receipt, "cjk_expected_found")
    if synthetic_queries < 3 or synthetic_found != synthetic_queries:
        raise LexicalGenerationError("quality receipt synthetic CJK gate is invalid")
    if live_found != live_queries:
        raise LexicalGenerationError("quality receipt live CJK gate is invalid")
    if cjk_queries != synthetic_queries + live_queries or cjk_found != cjk_queries:
        raise LexicalGenerationError("quality receipt aggregate CJK gate is invalid")
    if english_queries < 1 or english_regressions != 0:
        raise LexicalGenerationError("quality receipt English gate is invalid")

    source_binding = lexical_source_binding(conn)
    if receipt.get("source_binding") != source_binding:
        raise LexicalGenerationError("quality receipt source binding is invalid")
    integrity = generation_integrity_report(conn, generation_id)
    if receipt.get("integrity") != integrity or not bool(integrity.get("healthy")):
        raise LexicalGenerationError("quality receipt integrity binding is invalid")

    return {
        "ok": True,
        "status": "ready",
        "generation_id": generation_id,
        "synthetic_cjk_queries": synthetic_queries,
        "synthetic_cjk_expected_found": synthetic_found,
        "live_cjk_queries": live_queries,
        "live_cjk_expected_found": live_found,
        "english_queries": english_queries,
        "english_regressions": 0,
        "cjk_queries": cjk_queries,
        "cjk_expected_found": cjk_found,
        "integrity": integrity,
        "source_binding": source_binding,
        "provenance": dict(LEXICAL_QUALITY_PROVENANCE),
        "evidence_fingerprint": str(receipt["evidence_fingerprint"]),
        "contains_raw_samples": False,
    }


def mark_generation_ready(
    conn: sqlite3.Connection,
    generation_id: str = LEXICAL_GENERATION_ID,
    *,
    quality_receipt: dict[str, Any],
) -> dict[str, Any]:
    """Mark a fully reconciled generation READY after integrity/quality gates."""

    _require_supported_generation(generation_id)
    report = generation_integrity_report(conn, generation_id)
    if not bool(report.get("healthy")):
        raise LexicalGenerationError("lexical generation integrity gate failed")
    canonical_receipt = _validate_quality_receipt(
        conn,
        generation_id,
        quality_receipt,
    )
    updated = conn.execute(
        """
        UPDATE lexical_generations
        SET status='ready', quality_ok=1, quality_json=?, updated_at=?, error=''
        WHERE generation_id=? AND status IN ('building', 'failed', 'ready')
        """,
        (
            json.dumps(canonical_receipt, ensure_ascii=False, sort_keys=True),
            _now_iso(),
            generation_id,
        ),
    )
    if updated.rowcount != 1:
        raise LexicalGenerationError("lexical generation is not eligible for READY")
    return generation_status(conn, generation_id)


def activate_generation(
    conn: sqlite3.Connection,
    generation_id: str = LEXICAL_GENERATION_ID,
    *,
    expected_current: str,
) -> dict[str, Any]:
    """CAS-activate a READY generation while retaining legacy storage."""

    _require_supported_generation(generation_id)
    actual = current_generation_id(conn)
    if actual != str(expected_current):
        raise LexicalGenerationError(
            f"lexical activation CAS conflict: expected {expected_current!r}, actual {actual!r}"
        )
    manifest = generation_status(conn, generation_id)
    if manifest.get("status") != "ready" or manifest.get("quality_ok") is not True:
        raise LexicalGenerationError("lexical generation is not reviewed READY")
    quality = manifest.get("quality")
    if not isinstance(quality, dict):
        raise LexicalGenerationError("lexical generation quality receipt is missing")
    _validate_quality_receipt(conn, generation_id, quality)
    now = _now_iso()
    if actual:
        conn.execute(
            "UPDATE lexical_generations SET status='ready', updated_at=? WHERE generation_id=?",
            (now, actual),
        )
    pointer = conn.execute(
        """
        UPDATE lexical_generation_state
        SET value=?, updated_at=?
        WHERE key=? AND value=?
        """,
        (generation_id, now, _CURRENT_KEY, actual),
    )
    if pointer.rowcount != 1:
        raise LexicalGenerationError("lexical activation CAS conflict during pointer update")
    conn.execute(
        """
        UPDATE lexical_generations
        SET status='active', activated_at=?, updated_at=?
        WHERE generation_id=?
        """,
        (now, now, generation_id),
    )
    return generation_status(conn, generation_id)


def rollback_generation(
    conn: sqlite3.Connection,
    *,
    expected_current: str,
) -> dict[str, Any]:
    """CAS-disable the supplemental generation without deleting either index."""

    actual = current_generation_id(conn)
    if actual != str(expected_current):
        raise LexicalGenerationError(
            f"lexical rollback CAS conflict: expected {expected_current!r}, actual {actual!r}"
        )
    if not actual:
        return {"status": "legacy", "current_generation_id": ""}
    now = _now_iso()
    pointer = conn.execute(
        """
        UPDATE lexical_generation_state
        SET value='', updated_at=?
        WHERE key=? AND value=?
        """,
        (now, _CURRENT_KEY, actual),
    )
    if pointer.rowcount != 1:
        raise LexicalGenerationError("lexical rollback CAS conflict during pointer update")
    conn.execute(
        "UPDATE lexical_generations SET status='ready', updated_at=? WHERE generation_id=?",
        (now, actual),
    )
    return {
        "status": "legacy",
        "current_generation_id": "",
        "retained_generation_id": actual,
        "legacy_table_retained": True,
        "shadow_table_retained": _table_exists(conn, LEXICAL_SHADOW_TABLE),
    }


def supplemental_table_for_search(
    conn: sqlite3.Connection,
    generation_override: str | None = None,
    *,
    allow_unreviewed_override: bool = False,
) -> str:
    """Resolve a reviewed active/override table name for bounded query use."""

    generation_id = (
        current_generation_id(conn)
        if generation_override is None
        else str(generation_override)
    )
    if not generation_id:
        return ""
    _require_supported_generation(generation_id)
    manifest = generation_status(conn, generation_id)
    allowed_statuses = {"active"}
    if generation_override is not None:
        allowed_statuses = (
            {"building", "ready", "active"}
            if allow_unreviewed_override
            else {"ready", "active"}
        )
    if manifest.get("status") not in allowed_statuses:
        return ""
    if not allow_unreviewed_override and manifest.get("quality_ok") is not True:
        return ""
    if str(manifest.get("table_name") or "") != LEXICAL_SHADOW_TABLE:
        return ""
    if not _table_exists(conn, LEXICAL_SHADOW_TABLE):
        return ""
    return LEXICAL_SHADOW_TABLE


def lexical_generation_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return read-only operator health for the supplemental lexical channel."""

    schema = lexical_schema_status(conn)
    current = current_generation_id(conn)
    if not bool(schema.get("current")):
        return {
            "status": "schema_missing",
            "healthy": not bool(current),
            "current_generation_id": current,
            "schema": schema,
            "manifest": {},
            "integrity": {},
        }
    generation_id = current or LEXICAL_GENERATION_ID
    manifest = generation_status(conn, generation_id)
    manifest_status = str(manifest.get("status") or "absent")
    integrity: dict[str, Any] = {}
    if manifest_status not in {"absent", "schema_missing"}:
        try:
            integrity = generation_integrity_report(conn, generation_id)
        except LexicalGenerationError as exc:
            integrity = {"healthy": False, "error": str(exc)}
    if current:
        healthy = (
            manifest_status == "active"
            and bool(manifest.get("quality_ok"))
            and bool(integrity.get("healthy"))
        )
        status = "active" if healthy else "needs_repair"
    else:
        healthy = True
        status = (
            manifest_status
            if manifest_status in {"building", "ready", "failed"}
            else "legacy"
        )
    return {
        "status": status,
        "healthy": healthy,
        "current_generation_id": current,
        "schema": schema,
        "manifest": manifest,
        "integrity": integrity,
        "legacy_table_retained": _table_exists(conn, "memories_fts"),
        "shadow_table_present": _table_exists(conn, LEXICAL_SHADOW_TABLE),
    }
