from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

from scope_recall.journal import ensure_journal_schema
from scope_recall.sql_store import ensure_schema, store_row

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DOCTOR_PATH = PLUGIN_ROOT / "scripts" / "doctor.py"


def _doctor_module():
    spec = importlib.util.spec_from_file_location("scope_recall_doctor", DOCTOR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _conn(hermes_home: Path) -> sqlite3.Connection:
    db_dir = hermes_home / "scope-recall"
    db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_dir / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_journal_schema(conn)
    return conn


def test_journal_report_surfaces_digest_health_and_batch_policy(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        """
        INSERT INTO journal_entries(scope_id, shared_scope_id, session_id, turn_number, role, content, content_hash, created_at, processed_run_id)
        VALUES ('scope', 'shared', 's', 1, 'user', 'hello', 'h1', '2026-01-01T00:00:00+00:00', 'run-ok')
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
        ],
    )
    conn.execute(
        "INSERT INTO journal_rejections(journal_entry_id, run_id, reason, candidate, created_at) VALUES (1, 'run-quarantine', 'retry-exhausted:timeout', '', '2026-01-02T00:00:01+00:00')"
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
    assert health["recovery_queue"]["retry_exhausted_candidates"] == 1
    assert payload["backlog"]["batch_policy"]["max_entries_per_digest"] == 80
    assert any("llm-quarantine" in item or "retry/dead-letter" in item for item in recommendations)


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
