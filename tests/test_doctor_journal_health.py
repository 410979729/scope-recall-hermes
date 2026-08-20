"""Tests for journal health doctor reporting, dead-letter categories, and backlog thresholds.

These cases protect recovery visibility for auth/quota and retry-exhausted journal debt."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

from scope_recall.journal import ensure_journal_schema
from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.graph import ensure_graph_schema
from scope_recall.truth_connection import connect_truth_database

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DOCTOR_PATH = PLUGIN_ROOT / "scripts" / "doctor.py"
REPAIR_GRAPH_PATH = PLUGIN_ROOT / "scripts" / "repair.graph_hygiene.py"
REPAIR_VECTOR_PATH = PLUGIN_ROOT / "scripts" / "repair.vector_index.py"


def _load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _doctor_module():
    return _load_script_module("scope_recall_doctor", DOCTOR_PATH)


def _repair_graph_module():
    return _load_script_module("scope_recall_repair_graph_hygiene", REPAIR_GRAPH_PATH)


def _repair_vector_module():
    return _load_script_module("scope_recall_repair_vector_index", REPAIR_VECTOR_PATH)


def _conn(hermes_home: Path) -> sqlite3.Connection:
    db_path = hermes_home / "scope-recall" / "memory.sqlite3"
    conn = connect_truth_database(db_path, mode="rwc")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_journal_schema(conn)
    return conn


def _set_lifecycle(conn: sqlite3.Connection, memory_id: str, lifecycle: str) -> None:
    row = conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
    metadata = json.loads(str(row["metadata"] or "{}"))
    metadata["lifecycle"] = lifecycle
    conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (json.dumps(metadata, ensure_ascii=False, sort_keys=True), memory_id))
    conn.commit()


def _store_memory(conn: sqlite3.Connection, *, memory_id: str, content: str, target: str = "memory", lifecycle: str = "active") -> None:
    store_row(
        conn,
        memory_id=memory_id,
        scope_id="shared",
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="session",
        source="tool-store",
        target=target,
        content=content,
    )
    if lifecycle != "active":
        _set_lifecycle(conn, memory_id, lifecycle)


def test_journal_report_surfaces_digest_health_and_batch_policy(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        """
        INSERT INTO journal_entries(scope_id, shared_scope_id, session_id, turn_number, role, content, content_hash, created_at, processed_run_id)
        VALUES ('scope', 'shared', 's', 1, 'user', 'hello', 'h1', '2026-01-01T00:00:00+00:00', 'run-quarantine')
        """
    )
    conn.execute(
        """
        INSERT INTO journal_entries(scope_id, shared_scope_id, session_id, turn_number, role, content, content_hash, created_at, processed_run_id)
        VALUES ('scope', 'shared', 's', 2, 'assistant', 'blocked by auth', 'h2', '2026-01-02T00:00:00+00:00', 'run-dead-letter')
        """
    )
    conn.executemany(
        """
        INSERT INTO journal_digest_runs(id, started_at, finished_at, status, extractor, processed_entries, inserted, updated, skipped, error, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')
        """,
        [
            ("run-ok", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00", "ok", "llm", 1, 1, 0, 0, None),
            ("run-quarantine", "2026-01-02T00:00:00+00:00", "2026-01-02T00:00:01+00:00", "ok", "llm-quarantine", 80, 0, 0, 80, None),
            ("run-fallback", "2026-01-03T00:00:00+00:00", "2026-01-03T00:00:01+00:00", "ok_with_fallback", "heuristic-fallback", 10, 1, 0, 0, None),
            ("run-dead-letter", "2026-01-04T00:00:00+00:00", "2026-01-04T00:00:01+00:00", "dead_letter", "llm", 1, 0, 0, 1, "auth failure"),
        ],
    )
    conn.execute(
        "INSERT INTO journal_rejections(journal_entry_id, run_id, reason, candidate, created_at) VALUES (1, 'run-quarantine', 'retry-exhausted:timeout', '', '2026-01-02T00:00:01+00:00')"
    )
    conn.execute(
        "INSERT INTO journal_rejections(journal_entry_id, run_id, reason, candidate, created_at) VALUES (2, 'run-dead-letter', 'dead-letter:auth token expired', '', '2026-01-04T00:00:01+00:00')"
    )
    conn.commit()
    conn.close()
    doctor = _doctor_module()

    payload, check, recommendations = doctor.journal_report(
        tmp_path,
        journal_config={"max_entries_per_digest": 80, "dynamic_backlog_threshold": 500, "max_entries_per_digest_ceiling": 1200},
    )

    assert check["ok"] is True
    health = payload["digest_health"]
    assert health["status"] == "degraded"
    assert health["status_counts"]["ok"] == 2
    assert health["status_counts"]["ok_with_fallback"] == 1
    assert health["extractor_counts"]["llm-quarantine"]["runs"] == 1
    assert health["retry_exhausted_rejections"] == 1
    assert health["dead_letter_rejections"] == 1
    assert health["rejection_categories"] == {"auth": 1, "timeout": 1}
    assert health["retry_exhausted_categories"] == {"timeout": 1}
    assert health["dead_letter_categories"] == {"auth": 1}
    assert health["recovery_queue"]["retry_exhausted_candidates"] == 1
    assert health["recovery_queue"]["dead_letter_candidates"] == 1
    assert health["recovery_queue"]["retry_exhausted_categories"] == {"timeout": 1}
    assert health["recovery_queue"]["dead_letter_categories"] == {"auth": 1}
    assert payload["backlog"]["batch_policy"]["max_entries_per_digest"] == 80
    assert any("llm-quarantine" in item or "retry/dead-letter" in item for item in recommendations)


def test_journal_report_fails_when_provider_risk_no_insert_streak_exceeds_threshold(tmp_path):
    conn = _conn(tmp_path)
    risk_metadata = json.dumps(
        {
            "productive_writes": 0,
            "no_insert_reason": "provider_or_schema_risk",
            "health_flags": ["no_productive_write", "extractor_error"],
        },
        sort_keys=True,
    )
    conn.executemany(
        """
        INSERT INTO journal_digest_runs(id, started_at, finished_at, status, extractor, processed_entries, inserted, updated, skipped, error, metadata)
        VALUES (?, ?, ?, 'ok', 'llm', 10, 0, 0, 10, NULL, ?)
        """,
        [
            ("run-risk-1", "2026-01-03T00:00:00+00:00", "2026-01-03T00:00:01+00:00", risk_metadata),
            ("run-risk-2", "2026-01-04T00:00:00+00:00", "2026-01-04T00:00:01+00:00", risk_metadata),
        ],
    )
    conn.commit()
    conn.close()
    doctor = _doctor_module()

    payload, check, recommendations = doctor.journal_report(tmp_path, journal_config={"no_insert_fail_streak": 2})

    assert check["ok"] is False
    assert any("no productive writes" in failure for failure in check["failures"])
    health = payload["digest_health"]
    assert health["status"] == "degraded"
    assert health["recent_no_insert_risk_runs"] == 2
    assert health["recent_no_insert_reasons"] == {"provider_or_schema_risk": 2}
    assert "recent_no_productive_write_risk" in health["reasons"]
    assert any("no productive writes" in item for item in recommendations)


def test_journal_risk_streak_resets_after_productive_run(tmp_path):
    conn = _conn(tmp_path)
    risk_metadata = json.dumps(
        {
            "productive_writes": 0,
            "no_insert_reason": "provider_or_schema_risk",
            "health_flags": ["no_productive_write", "extractor_error"],
        },
        sort_keys=True,
    )
    productive_metadata = json.dumps(
        {"productive_writes": 1, "no_insert_reason": ""}, sort_keys=True
    )
    conn.executemany(
        """
        INSERT INTO journal_digest_runs(
            id, started_at, finished_at, status, extractor,
            processed_entries, inserted, updated, skipped, error, metadata
        ) VALUES (?, ?, ?, 'ok', 'llm', 10, ?, 0, ?, NULL, ?)
        """,
        [
            ("risk-old-1", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00", 0, 10, risk_metadata),
            ("risk-old-2", "2026-01-02T00:00:00+00:00", "2026-01-02T00:00:01+00:00", 0, 10, risk_metadata),
            ("success-reset", "2026-01-03T00:00:00+00:00", "2026-01-03T00:00:01+00:00", 1, 9, productive_metadata),
            ("risk-latest", "2026-01-04T00:00:00+00:00", "2026-01-04T00:00:01+00:00", 0, 10, risk_metadata),
        ],
    )
    conn.commit()
    conn.close()
    doctor = _doctor_module()

    payload, check, recommendations = doctor.journal_report(
        tmp_path, journal_config={"no_insert_fail_streak": 3}
    )

    health = payload["digest_health"]
    assert health["recent_no_insert_risk_runs"] == 3
    assert health["recent_no_insert_risk_streak"] == 1
    assert check["ok"] is True


def test_journal_report_does_not_degrade_for_operator_classified_quarantine_and_sourced_retry(tmp_path):
    conn = _conn(tmp_path)
    _store_memory(conn, memory_id="memory-from-retry", content="Retry-exhausted entry already produced durable memory.")
    conn.execute(
        """
        INSERT INTO journal_entries(id, scope_id, shared_scope_id, session_id, turn_number, role, content, content_hash, created_at, processed_run_id, processed_at)
        VALUES (10, 'scope', 'shared', 's', 1, 'user', 'already handled', 'h10', '2026-01-01T00:00:00+00:00', 'run-timeout', '2026-01-01T00:00:01+00:00')
        """
    )
    conn.execute(
        """
        INSERT INTO journal_digest_runs(id, started_at, finished_at, status, extractor, processed_entries, inserted, updated, skipped, error, metadata)
        VALUES ('run-timeout', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:01+00:00', 'ok', 'llm-quarantine', 1, 0, 0, 1, NULL, ?)
        """,
        (json.dumps({"operator_classification": "no_replay", "classification_reason": "handled via source link"}, sort_keys=True),),
    )
    conn.execute(
        "INSERT INTO journal_rejections(journal_entry_id, run_id, reason, candidate, created_at) VALUES (10, 'run-timeout', 'retry-exhausted:timeout', '', '2026-01-01T00:00:01+00:00')"
    )
    conn.execute(
        "INSERT INTO memory_journal_sources(memory_id, journal_entry_id, run_id, created_at) VALUES ('memory-from-retry', 10, 'run-timeout', '2026-01-01T00:00:02+00:00')"
    )
    conn.commit()
    conn.close()
    doctor = _doctor_module()

    payload, check, recommendations = doctor.journal_report(tmp_path, journal_config={"max_entries_per_digest": 80})

    assert check["ok"] is True
    health = payload["digest_health"]
    assert health["status"] == "ready"
    assert health["llm_quarantine_runs"] == 0
    assert health["historical_llm_quarantine_runs"] == 1
    assert health["retry_exhausted_rejections"] == 0
    assert health["historical_retry_exhausted_rejections"] == 1
    assert health["recovery_queue"]["retry_exhausted_candidates"] == 0
    assert not any("llm-quarantine" in item or "retry/dead-letter" in item for item in recommendations)


def test_journal_report_treats_operator_classified_rejection_reasons_as_resolved_quarantine(tmp_path):
    conn = _conn(tmp_path)
    metadata = json.dumps(
        {
            "productive_writes": 0,
            "no_insert_reason": "quarantine",
            "health_flags": ["quarantine", "extractor_error", "no_productive_write"],
        },
        sort_keys=True,
    )
    conn.execute(
        """
        INSERT INTO journal_entries(id, scope_id, shared_scope_id, session_id, turn_number, role, content, content_hash, created_at, processed_run_id, processed_at)
        VALUES (20, 'scope', 'shared', 's', 1, 'tool', 'Tool execution summary output_preview=omitted', 'h20', '2026-01-05T00:00:00+00:00', 'run-quota-classified', '2026-01-05T00:00:01+00:00')
        """
    )
    conn.execute(
        """
        INSERT INTO journal_digest_runs(id, started_at, finished_at, status, extractor, processed_entries, inserted, updated, skipped, error, metadata)
        VALUES ('run-quota-classified', '2026-01-05T00:00:00+00:00', '2026-01-05T00:00:01+00:00', 'ok', 'llm-quarantine', 1, 0, 0, 1, NULL, ?)
        """,
        (metadata,),
    )
    conn.execute(
        """
        INSERT INTO journal_rejections(journal_entry_id, run_id, reason, candidate, created_at)
        VALUES (20, 'run-quota-classified', 'operator-classified:no_replay:retry-exhausted:rate_limit', 'operator reviewed quota/tool noise', '2026-01-05T00:00:01+00:00')
        """
    )
    conn.commit()
    conn.close()
    doctor = _doctor_module()

    payload, check, recommendations = doctor.journal_report(tmp_path, journal_config={"no_insert_fail_streak": 1})

    assert check["ok"] is True
    health = payload["digest_health"]
    assert health["status"] == "ready"
    assert health["llm_quarantine_runs"] == 0
    assert health["historical_llm_quarantine_runs"] == 1
    assert health["recent_no_insert_risk_runs"] == 0
    assert health["recent_no_insert_explicit_skip_runs"] == 1
    assert health["recovery_queue"]["retry_exhausted_candidates"] == 0
    assert not any("no productive writes" in item for item in recommendations)


def test_journal_report_treats_operator_review_no_durable_digest_run_as_explicit_skip(tmp_path):
    conn = _conn(tmp_path)
    metadata = json.dumps(
        {
            "productive_writes": 0,
            "no_insert_reason": "operator_review_no_durable_memory",
            "operator_classification": "no_durable_memory",
        },
        sort_keys=True,
    )
    conn.execute(
        """
        INSERT INTO journal_digest_runs(id, started_at, finished_at, status, extractor, processed_entries, inserted, updated, skipped, error, metadata)
        VALUES ('operator-review-no-durable', '2026-01-06T00:00:00+00:00', '2026-01-06T00:00:01+00:00', 'ok', 'operator-reviewed-no-durable', 12, 0, 0, 12, NULL, ?)
        """,
        (metadata,),
    )
    conn.commit()
    conn.close()
    doctor = _doctor_module()

    payload, check, recommendations = doctor.journal_report(tmp_path, journal_config={"no_insert_fail_streak": 1})

    assert check["ok"] is True
    health = payload["digest_health"]
    assert health["status"] == "ready"
    assert health["recent_no_insert_risk_runs"] == 0
    assert health["recent_no_insert_explicit_skip_runs"] == 1
    assert health["recent_no_insert_reasons"] == {}
    assert not any("no productive writes" in item for item in recommendations)


def test_doctor_vector_report_marks_empty_index_needs_repair_when_sqlite_has_indexable_memories(tmp_path):
    conn = _conn(tmp_path)
    store_row(
        conn,
        memory_id="durable-memory",
        scope_id="shared",
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="session",
        source="tool-store",
        target="memory",
        content="Scope Recall durable memory should be indexed.",
    )
    conn.close()
    vector_path = tmp_path / "scope-recall" / "vector.sqlite3"
    vector = sqlite3.connect(vector_path)
    try:
        vector.execute(
            """
            CREATE TABLE vector_records (
                id TEXT PRIMARY KEY,
                scope_id TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                target TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                vector_json TEXT NOT NULL
            )
            """
        )
        vector.execute("CREATE TABLE vector_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        vector.execute("INSERT INTO vector_meta(key, value) VALUES ('dimensions', '2'), ('table_name', 'memories')")
        vector.commit()
    finally:
        vector.close()
    doctor = _doctor_module()

    payload, check, recommendations = doctor.sqlite_vector_report(tmp_path)

    assert payload["status"] == "needs_repair"
    assert payload["ready"] is False
    assert payload["expected_indexable_rows"] == 1
    assert check["ok"] is False
    assert any("Vector companion is empty" in item for item in recommendations)


def test_doctor_vector_report_marks_lifecycle_hidden_vector_ids_stale(tmp_path):
    conn = _conn(tmp_path)
    _store_memory(conn, memory_id="active-memory", content="Active vector truth should stay indexed.")
    _store_memory(conn, memory_id="archived-memory", content="Archived vector truth should be removed.", lifecycle="archived")
    conn.close()
    vector_path = tmp_path / "scope-recall" / "vector.sqlite3"
    vector = sqlite3.connect(vector_path)
    try:
        vector.execute(
            """
            CREATE TABLE vector_records (
                id TEXT PRIMARY KEY,
                scope_id TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                target TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                vector_json TEXT NOT NULL
            )
            """
        )
        vector.execute("CREATE TABLE vector_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        vector.execute("INSERT INTO vector_meta(key, value) VALUES ('dimensions', '2'), ('table_name', 'memories')")
        vector.executemany(
            "INSERT INTO vector_records(id, scope_id, source, target, content, summary, updated_at, vector_json) VALUES (?, 'shared', 'tool-store', 'memory', ?, ?, '2026-01-01T00:00:00+00:00', '[0.0, 0.0]')",
            [
                ("active-memory", "Active vector truth should stay indexed.", "Active vector truth should stay indexed."),
                ("archived-memory", "Archived vector truth should be removed.", "Archived vector truth should be removed."),
            ],
        )
        vector.commit()
    finally:
        vector.close()
    doctor = _doctor_module()

    payload, check, recommendations = doctor.sqlite_vector_report(tmp_path)

    assert payload["status"] == "needs_repair"
    assert payload["stale_vector_id_count"] == 1
    assert payload["stale_vector_id_samples"] == ["archived-memory"]
    assert check["ok"] is False
    assert any("stale ids" in item for item in recommendations)


def test_doctor_vector_report_classifies_stale_debt_by_reason(tmp_path):
    conn = _conn(tmp_path)
    _store_memory(conn, memory_id="active-memory", content="Active vector truth should stay indexed.")
    _store_memory(conn, memory_id="archived-memory", content="Archived vector truth should be removed.", lifecycle="archived")
    _store_memory(conn, memory_id="general-memory", content="General memory is excluded by index policy.", target="general")
    conn.close()
    vector_path = tmp_path / "scope-recall" / "vector.sqlite3"
    vector = sqlite3.connect(vector_path)
    try:
        vector.execute(
            """
            CREATE TABLE vector_records (
                id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
                target TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '', vector_json TEXT NOT NULL
            )
            """
        )
        vector.execute("CREATE TABLE vector_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        vector.execute("INSERT INTO vector_meta(key, value) VALUES ('dimensions', '2'), ('table_name', 'memories')")
        vector.executemany(
            "INSERT INTO vector_records(id, scope_id, source, target, content, summary, updated_at, vector_json) VALUES (?, 'shared', 'tool-store', ?, ?, ?, '2026-01-01T00:00:00+00:00', '[0.0, 0.0]')",
            [
                ("active-memory", "memory", "active", "active"),
                ("archived-memory", "memory", "archived", "archived"),
                ("general-memory", "general", "general", "general"),
                ("orphan-memory", "memory", "orphan", "orphan"),
            ],
        )
        vector.commit()
    finally:
        vector.close()
    doctor = _doctor_module()

    payload, check, _recommendations = doctor.sqlite_vector_report(tmp_path, index_general=False)

    assert payload["status"] == "needs_repair"
    assert payload["terminal_hidden_vector_id_count"] == 1
    assert payload["terminal_hidden_vector_id_samples"] == ["archived-memory"]
    assert payload["policy_excluded_vector_id_count"] == 1
    assert payload["policy_excluded_vector_id_samples"] == ["general-memory"]
    assert payload["orphan_vector_id_count"] == 1
    assert payload["orphan_vector_id_samples"] == ["orphan-memory"]
    assert payload["stale_vector_id_count"] == 3
    assert check["ok"] is False


def test_repair_vector_index_load_rows_excludes_lifecycle_hidden_memories(tmp_path):
    conn = _conn(tmp_path)
    _store_memory(conn, memory_id="active-memory", content="Active vector repair row.")
    _store_memory(conn, memory_id="archived-memory", content="Archived vector repair row.", lifecycle="archived")
    conn.close()
    repair = _repair_vector_module()

    rows = repair.load_rows(tmp_path / "scope-recall" / "memory.sqlite3")

    assert [str(row["id"]) for row in rows] == ["active-memory"]


def test_doctor_sqlite_report_surfaces_orphan_graph_rows(tmp_path):
    conn = _conn(tmp_path)
    ensure_graph_schema(conn)
    conn.execute("INSERT INTO memory_entities(memory_id, entity, weight, source) VALUES ('missing-memory', 'ghost-entity', 1.0, 'metadata')")
    conn.execute(
        """
        INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
        VALUES ('missing-source', 'missing-target', 'supports', 0.5, 'orphan fixture', '2026-01-01T00:00:00+00:00')
        """
    )
    conn.commit()
    conn.close()
    doctor = _doctor_module()

    payload, check, recommendations = doctor.sqlite_report(tmp_path)

    assert payload["status"] == "needs_repair"
    assert payload["graph_hygiene"]["orphan_entities"] == 1
    assert payload["graph_hygiene"]["orphan_relations"] == 1
    assert payload["graph_hygiene"]["orphan_relation_sources"] == 1
    assert payload["graph_hygiene"]["orphan_relation_targets"] == 1
    assert check["ok"] is False
    assert any("graph hygiene" in item.lower() or "orphan" in item.lower() for item in recommendations)


def test_doctor_sqlite_report_surfaces_governance_audit_coverage(tmp_path):
    conn = _conn(tmp_path)
    _store_memory(conn, memory_id="legacy-archived", content="Legacy archived memory should be visible in coverage report.", lifecycle="archived")
    conn.execute("DELETE FROM memory_entities WHERE memory_id = 'legacy-archived'")
    conn.execute("DELETE FROM memories_fts WHERE memory_id = 'legacy-archived'")
    conn.execute("DELETE FROM memory_relations WHERE source_memory_id = 'legacy-archived' OR target_memory_id = 'legacy-archived'")
    conn.commit()
    conn.close()
    doctor = _doctor_module()

    payload, check, recommendations = doctor.sqlite_report(tmp_path)

    coverage = payload["governance_audit_coverage"]
    assert coverage["status"] == "needs_review"
    assert coverage["legacy_coverage"]["missing_audit"] == 1
    assert coverage["legacy_coverage"]["backfill_candidates"] == 1
    assert check["ok"] is True
    assert any("Legacy archived memories without governance audit coverage" in item for item in recommendations)


def test_doctor_sqlite_report_surfaces_lifecycle_hidden_graph_rows(tmp_path):
    conn = _conn(tmp_path)
    ensure_graph_schema(conn)
    _store_memory(conn, memory_id="active-memory", content="Active graph truth should remain.")
    _store_memory(conn, memory_id="archived-memory", content="Archived graph truth should not retain companion rows.", lifecycle="archived")
    conn.execute("INSERT OR REPLACE INTO memory_entities(memory_id, entity, weight, source) VALUES ('archived-memory', 'project-atlas', 1.0, 'fixture')")
    conn.execute(
        """
        INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
        VALUES ('archived-memory', 'active-memory', 'supports', 0.5, 'hidden fixture', '2026-01-01T00:00:00+00:00')
        """
    )
    conn.commit()
    conn.close()
    doctor = _doctor_module()

    payload, check, recommendations = doctor.sqlite_report(tmp_path)

    assert payload["status"] == "needs_repair"
    assert payload["graph_hygiene"]["hidden_lifecycle_entities"] >= 1
    assert payload["graph_hygiene"]["hidden_lifecycle_relations"] == 1
    assert payload["graph_hygiene"]["hidden_lifecycle_relation_sources"] == 1
    assert check["ok"] is False
    assert any("hidden-lifecycle" in item or "hidden lifecycle" in item.lower() for item in recommendations)


def test_repair_graph_hygiene_dry_run_and_apply_remove_orphans(tmp_path):
    conn = _conn(tmp_path)
    ensure_graph_schema(conn)
    conn.execute("INSERT INTO memory_entities(memory_id, entity, weight, source) VALUES ('missing-memory', 'ghost-entity', 1.0, 'metadata')")
    conn.execute(
        """
        INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
        VALUES ('missing-source', 'missing-target', 'supports', 0.5, 'orphan fixture', '2026-01-01T00:00:00+00:00')
        """
    )
    conn.commit()
    conn.close()
    repair = _repair_graph_module()
    doctor = _doctor_module()

    dry_run = repair.repair_graph_hygiene(tmp_path, apply=False)
    assert dry_run["dry_run"] is True
    assert dry_run["before"]["orphan_entities"] == 1
    assert dry_run["after"]["orphan_entities"] == 1
    assert dry_run["deleted"] == {"memory_entities": 1, "memory_relations": 1}

    applied = repair.repair_graph_hygiene(tmp_path, apply=True)
    assert applied["dry_run"] is False
    assert applied["deleted"] == {"memory_entities": 1, "memory_relations": 1}
    assert applied["after"]["orphan_entities"] == 0
    assert applied["after"]["orphan_relations"] == 0

    payload, check, _ = doctor.sqlite_report(tmp_path)
    assert payload["status"] == "ready"
    assert check["ok"] is True


def test_repair_graph_hygiene_removes_lifecycle_hidden_companion_rows(tmp_path):
    conn = _conn(tmp_path)
    ensure_graph_schema(conn)
    _store_memory(conn, memory_id="active-memory", content="Active graph truth should remain.")
    _store_memory(conn, memory_id="archived-memory", content="Archived graph truth should not retain companion rows.", lifecycle="archived")
    conn.execute("INSERT OR REPLACE INTO memory_entities(memory_id, entity, weight, source) VALUES ('archived-memory', 'project-atlas', 1.0, 'fixture')")
    conn.execute(
        """
        INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
        VALUES ('archived-memory', 'active-memory', 'supports', 0.5, 'hidden fixture', '2026-01-01T00:00:00+00:00')
        """
    )
    conn.commit()
    conn.close()
    repair = _repair_graph_module()

    applied = repair.repair_graph_hygiene(tmp_path, apply=True)

    assert applied["deleted"]["memory_entities"] >= 1
    assert applied["deleted"]["memory_relations"] == 1
    assert applied["after"]["hidden_lifecycle_entities"] == 0
    assert applied["after"]["hidden_lifecycle_relations"] == 0

def test_journal_report_surfaces_deferred_backlog_metrics(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        """
        INSERT INTO journal_entries(
            scope_id, shared_scope_id, session_id, turn_number, role, content,
            content_hash, created_at, processed_run_id, deferred_run_id, deferred_at, defer_count
        ) VALUES
            ('scope', 'shared', 'fat', 1, 'user', 'deferred once', 'h-def-1',
             '2026-08-18T00:00:00+00:00', '', 'run-defer-1', '2026-08-18T01:00:00+00:00', 1),
            ('scope', 'shared', 'fat', 2, 'user', 'deferred again', 'h-def-2',
             '2026-08-18T00:00:00+00:00', '', 'run-defer-2', '2026-08-18T02:00:00+00:00', 3)
        """
    )
    conn.commit()
    conn.close()
    doctor = _doctor_module()

    payload, check, recommendations = doctor.journal_report(tmp_path)

    assert check["ok"] is True
    deferred = payload["backlog"]["deferred"]
    assert deferred["count"] == 2
    assert deferred["oldest_deferred_age_hours"] > 0
    assert deferred["repeat_deferred_count"] == 1
    assert deferred["max_defer_count"] == 3
    assert any("budget-deferred" in item for item in recommendations)
    assert any("repeatedly deferred" in item for item in recommendations)


def test_journal_report_deferred_count_excludes_historical_churn_without_current_marker(
    tmp_path,
):
    conn = _conn(tmp_path)
    conn.execute(
        """
        INSERT INTO journal_entries(
            scope_id, shared_scope_id, session_id, turn_number, role, content,
            content_hash, created_at, processed_run_id, deferred_run_id, deferred_at, defer_count
        ) VALUES
            ('scope', 'shared', 'fat', 1, 'user', 'currently deferred', 'h-cur',
             '2026-08-18T00:00:00+00:00', '', 'run-defer-1', '2026-08-18T01:00:00+00:00', 1),
            ('scope', 'shared', 'fat', 2, 'user', 'covered pending with churn', 'h-hist',
             '2026-08-18T00:00:00+00:00', '', '', NULL, 4)
        """
    )
    conn.commit()
    conn.close()
    doctor = _doctor_module()

    payload, check, _recommendations = doctor.journal_report(tmp_path)

    assert check["ok"] is True
    deferred = payload["backlog"]["deferred"]
    assert deferred["count"] == 1
    assert deferred["max_defer_count"] == 4
    assert deferred["repeat_deferred_count"] == 1

def test_journal_report_surfaces_pending_retryable_failures_without_row_contents(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        """
        INSERT INTO journal_entries(
            scope_id, shared_scope_id, session_id, turn_number, role, content,
            content_hash, created_at, processed_run_id, retryable_failures
        ) VALUES (
            'scope', 'shared', 's', 1, 'user',
            'secret journal row body must not appear in doctor output',
            'h-retryable', '2026-08-19T00:00:00+00:00', '', 2
        )
        """
    )
    conn.commit()
    conn.close()
    doctor = _doctor_module()
    payload, check, recommendations = doctor.journal_report(
        tmp_path,
        journal_config={"retryable_failures_quarantine": 2},
    )
    assert check["ok"] is True
    retryable = payload["backlog"]["retryable_failures"]
    assert retryable["pending_entries"] == 1
    assert retryable["threshold"] == 2
    assert "pending_retryable_failures" in payload["digest_health"]["reasons"]
    joined = " ".join(recommendations)
    assert "durable retryable LLM failures" in joined
    assert "secret journal row body" not in json.dumps(payload, ensure_ascii=False)
    assert "secret journal row body" not in joined
