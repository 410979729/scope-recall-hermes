"""Tests for the unified Scope Recall governance scheduler."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from scope_recall.governance_scheduler import run_governance_cycle
from scope_recall.journal import append_journal_entry, ensure_journal_schema
from scope_recall.models import RuntimeScope
from scope_recall.scope import build_scope_id, build_shared_scope_id
from scope_recall.sql_store import ensure_schema, now_iso, store_row

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _scope() -> RuntimeScope:
    return RuntimeScope(
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        agent_context="primary",
    )


def _seed_db(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_journal_schema(conn)
    scope = _scope()
    shared_scope_id = build_shared_scope_id(scope)
    entry_id = append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=shared_scope_id,
        session_id="scheduler-fixture",
        turn_number=1,
        role="user",
        content="scope-recall governance scheduler dry-run fixture",
    )
    now = now_iso()
    conn.execute(
        """
        INSERT INTO journal_rejections(journal_entry_id, run_id, reason, candidate, created_at)
        VALUES(?, 'run-1', 'dead-letter:auth', '{}', ?)
        """,
        (entry_id, now),
    )
    store_row(
        conn,
        memory_id="template-noise",
        scope_id=shared_scope_id,
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="scheduler-fixture",
        source="journal-digest",
        target="memory",
        content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。",
        metadata=json.dumps({"memory_type": "summary"}, ensure_ascii=False),
        allow_duplicate=True,
    )
    store_row(
        conn,
        memory_id="candidate-safe",
        scope_id=shared_scope_id,
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="scheduler-fixture",
        source="manual",
        target="memory",
        content="Reusable fact with evidence anchor for scheduler fixture.",
        metadata=json.dumps(
            {
                "memory_type": "factual",
                "confidence": 0.9,
                "importance": 0.7,
                "evidence_refs": ["journal:fixture"],
            },
            ensure_ascii=False,
        ),
        allow_duplicate=True,
    )
    candidate_metadata = {
        "lifecycle": "candidate",
        "memory_type": "factual",
        "confidence": 0.9,
        "importance": 0.7,
        "evidence_refs": ["journal:fixture"],
    }
    conn.execute("UPDATE memories SET metadata = ? WHERE id = 'candidate-safe'", (json.dumps(candidate_metadata, ensure_ascii=False),))
    conn.execute(
        """
        UPDATE fact_freshness
        SET fact_key='fixture', truth_type='config', validator_kind='manual',
            ttl_days=7, last_checked_at=?, valid_until='2026-01-01T00:00:00+00:00',
            status='needs_live_check', stale_reason='fixture', updated_at=?
        WHERE subject_type='memory' AND subject_id='template-noise'
        """,
        (now, now),
    )
    conn.commit()


def test_governance_scheduler_dry_run_is_query_only_on_readonly_connection(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    writer = sqlite3.connect(db_path)
    writer.row_factory = sqlite3.Row
    try:
        _seed_db(writer)
    finally:
        writer.close()

    readonly = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    readonly.row_factory = sqlite3.Row
    readonly.execute("PRAGMA query_only=ON")
    try:
        payload = run_governance_cycle(readonly, scope_ids=[build_shared_scope_id(_scope())], dry_run=True, apply_safe=False)
    finally:
        readonly.close()

    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["summary"]["journal_unprocessed"] == 1
    assert payload["summary"]["journal_dead_letter"] == 1
    assert payload["summary"]["candidate_count"] == 1
    assert payload["summary"]["fact_needs_live_check"] == 2
    assert payload["summary"]["cleanup_candidates"] == 1
    assert "candidate_memory_triage" in payload["action_items"]

    verifier = sqlite3.connect(db_path)
    verifier.row_factory = sqlite3.Row
    try:
        metadata = json.loads(verifier.execute("SELECT metadata FROM memories WHERE id = 'template-noise'").fetchone()["metadata"])
        assert metadata.get("lifecycle") != "archived"
        assert verifier.execute("SELECT COUNT(*) FROM governance_audit_events").fetchone()[0] == 0
    finally:
        verifier.close()


def test_governance_scheduler_global_report_includes_forgetting_subreport(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        _seed_db(conn)
        payload = run_governance_cycle(conn, scope_ids=None, accessible_scope_ids=None, dry_run=True, apply_safe=False, limit=10)
    finally:
        conn.close()

    assert payload["summary"]["candidate_count"] == 1
    assert payload["forgetting"]["total_rows"] == 2
    assert payload["summary"]["forgetting_review_debt"] == 1
    assert payload["summary"]["forgetting_soft_archive_candidates"] == 1


def test_governance_scheduler_forwards_accessible_scope_ids_to_subreports(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        ensure_journal_schema(conn)
        now = now_iso()
        candidate_metadata = json.dumps(
            {"lifecycle": "candidate", "memory_type": "factual", "confidence": 0.9, "importance": 0.7, "evidence_refs": ["journal:fixture"]},
            ensure_ascii=False,
        )
        for scope_id, memory_id in (("scope-a", "candidate-a"), ("scope-b", "candidate-b")):
            conn.execute(
                """
                INSERT INTO journal_entries(scope_id, shared_scope_id, session_id, turn_number, role, content, content_hash, created_at)
                VALUES (?, '', ?, 1, 'user', 'scope-specific scheduler fixture', ?, ?)
                """,
                (scope_id, f"journal-{scope_id}", f"hash-{scope_id}", now),
            )
            conn.execute(
                """
                INSERT INTO procedural_playbooks(
                    id, scope_id, shared_scope_id, task_class, title, trigger, goal, steps, pitfalls,
                    verification, status, confidence, metadata, created_at, updated_at
                ) VALUES (?, ?, '', 'ops', ?, 'trigger', 'goal', '[]', '[]', '[]', 'needs_review', 0.8, '{}', ?, ?)
                """,
                (f"playbook-{scope_id}", scope_id, f"Playbook {scope_id}", now, now),
            )
            store_row(
                conn,
                memory_id=memory_id,
                scope_id=scope_id,
                platform="telegram",
                user_id="joy",
                chat_id="dm",
                thread_id="",
                gateway_session_key="",
                agent_identity="yuheng",
                agent_workspace="hermes",
                session_id="scheduler-scope-fixture",
                source="manual",
                target="memory",
                content=f"Reusable scope {scope_id} fact with evidence anchor.",
                metadata=candidate_metadata,
                allow_duplicate=True,
            )
            conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (candidate_metadata, memory_id))
            conn.execute(
                """
                UPDATE fact_freshness
                SET fact_key='fixture', truth_type='config', validator_kind='manual',
                    ttl_days=7, last_checked_at=?, valid_until='2026-01-01T00:00:00+00:00',
                    status='needs_live_check', stale_reason='fixture', updated_at=?
                WHERE subject_type='memory' AND subject_id=?
                """,
                (now, now, memory_id),
            )
        conn.commit()

        payload = run_governance_cycle(conn, scope_ids=["scope-a"], accessible_scope_ids=["scope-a"], dry_run=True, apply_safe=False, limit=50)
    finally:
        conn.close()

    assert payload["summary"]["candidate_count"] == 1
    assert payload["summary"]["journal_unprocessed"] == 1
    assert payload["summary"]["experience_needs_review"] == 1
    assert payload["candidate_memory"]["samples"][0]["id"] == "candidate-a"
    assert payload["candidate_memory"]["samples"][0]["scope_id"] == "scope-a"
    assert payload["summary"]["fact_needs_live_check"] == 1
    assert payload["forgetting"]["total_rows"] == 1


def test_governance_scheduler_cleanup_uses_accessible_scope_when_scope_ids_omitted():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        for scope_id in ("scope-a", "scope-b"):
            store_row(
                conn,
                memory_id=f"template-{scope_id}",
                scope_id=scope_id,
                platform="telegram",
                user_id="joy",
                chat_id="dm",
                thread_id="",
                gateway_session_key="",
                agent_identity="yuheng",
                agent_workspace="hermes",
                session_id="cleanup-scope-fixture",
                source="journal-digest",
                target="memory",
                content=f"Journal digest memory stale template for {scope_id}: user: 继续 assistant: 完成。",
                metadata=json.dumps({"memory_type": "summary"}, ensure_ascii=False),
                allow_duplicate=True,
            )
        payload = run_governance_cycle(conn, scope_ids=None, accessible_scope_ids=["scope-a"], dry_run=True, apply_safe=False, limit=10)
    finally:
        conn.close()

    assert payload["summary"]["cleanup_candidates"] == 1
    assert [item["id"] for item in payload["cleanup"]["items"]] == ["template-scope-a"]


def test_governance_scheduler_explicit_empty_scope_ids_fail_closed_for_reports():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        _seed_db(conn)
        payload = run_governance_cycle(conn, scope_ids=[], accessible_scope_ids=None, dry_run=True, apply_safe=False, limit=10)
    finally:
        conn.close()

    assert payload["summary"]["candidate_count"] == 0
    assert payload["summary"]["cleanup_candidates"] == 0
    assert payload["summary"]["fact_needs_live_check"] == 0
    assert payload["forgetting"]["total_rows"] == 0
    assert payload["summary"]["forgetting_review_debt"] == 0


def test_governance_scheduler_requires_apply_safe_for_mutating_mode(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        _seed_db(conn)
        before = conn.total_changes
        payload = run_governance_cycle(conn, scope_ids=[build_shared_scope_id(_scope())], dry_run=False, apply_safe=False)
        after = conn.total_changes
    finally:
        conn.close()

    assert payload["ok"] is False
    assert payload["error"] == "apply_requires_apply_safe"
    assert after == before


def test_governance_scheduler_apply_safe_archives_only_cleanup_candidates_with_audit(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        _seed_db(conn)
        payload = run_governance_cycle(conn, scope_ids=[build_shared_scope_id(_scope())], dry_run=False, apply_safe=True)
        archived_meta = json.loads(conn.execute("SELECT metadata FROM memories WHERE id = 'template-noise'").fetchone()["metadata"])
        candidate_meta = json.loads(conn.execute("SELECT metadata FROM memories WHERE id = 'candidate-safe'").fetchone()["metadata"])
        audit_count = conn.execute("SELECT COUNT(*) FROM governance_audit_events WHERE event_type = 'memory_cleanup'").fetchone()[0]
    finally:
        conn.close()

    assert payload["ok"] is True
    assert payload["dry_run"] is False
    assert payload["apply_safe"] is True
    assert payload["applied"]["cleanup"]["archived"] == 1
    assert payload["applied"]["cleanup"]["batch_id"].startswith("cleanup-")
    assert archived_meta["lifecycle"] == "archived"
    assert archived_meta["rollback_batch_id"] == payload["applied"]["cleanup"]["batch_id"]
    assert candidate_meta["lifecycle"] == "candidate"
    assert audit_count == 1


def test_governance_scheduler_apply_safe_without_scope_fails_closed():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        _seed_db(conn)
        before = conn.total_changes
        payload = run_governance_cycle(conn, scope_ids=[], dry_run=False, apply_safe=True)
        after = conn.total_changes
        archived_meta = json.loads(conn.execute("SELECT metadata FROM memories WHERE id = 'template-noise'").fetchone()["metadata"])
        audit_count = conn.execute("SELECT COUNT(*) FROM governance_audit_events WHERE event_type = 'memory_cleanup'").fetchone()[0]
    finally:
        conn.close()

    assert payload["ok"] is False
    assert payload["error"] == "apply_requires_scope_id"
    assert after == before
    assert archived_meta.get("lifecycle") != "archived"
    assert audit_count == 0


def test_governance_scheduler_script_apply_safe_without_scope_is_argparse_error(tmp_path):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    db_path = storage / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_db(conn)
    finally:
        conn.close()

    result = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "scripts" / "governance.scheduler.py"),
            "--hermes-home",
            str(hermes_home),
            "--apply-safe",
            "--json",
        ],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 2
    assert "--apply-safe requires at least one --scope-id" in result.stderr
    assert result.stdout == ""


def test_governance_scheduler_script_defaults_to_hermes_home_env(tmp_path):
    hermes_home = tmp_path / "env-hermes-home"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    db_path = storage / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_db(conn)
    finally:
        conn.close()

    fake_home = tmp_path / "plain-home"
    fake_home.mkdir()
    env = dict(os.environ)
    env["HERMES_HOME"] = str(hermes_home)
    env["HOME"] = str(fake_home)
    env["PYTHONIOENCODING"] = "cp1252"
    result = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "scripts" / "governance.scheduler.py"), "--dry-run", "--json"],
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )

    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "governance_scheduler.v1"
    assert payload["dry_run"] is True
    assert payload["summary"]["journal_unprocessed"] == 1
    assert payload["summary"]["candidate_count"] == 1


def test_governance_scheduler_script_emits_json_for_tmp_home(tmp_path):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    db_path = storage / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_db(conn)
    finally:
        conn.close()

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp1252"
    result = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "scripts" / "governance.scheduler.py"),
            "--hermes-home",
            str(hermes_home),
            "--dry-run",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )

    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "governance_scheduler.v1"
    assert payload["dry_run"] is True
    assert payload["summary"]["journal_unprocessed"] == 1
    assert payload["summary"]["candidate_count"] == 1
