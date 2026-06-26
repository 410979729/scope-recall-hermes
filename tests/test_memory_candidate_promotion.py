from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scope_recall.candidate_promotion import candidate_debt_report, classify_candidate_row
from scope_recall.sql_store import ensure_schema

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PLUGIN_ROOT / "scripts" / "promote.memory_candidates.py"
DOCTOR_PATH = PLUGIN_ROOT / "scripts" / "doctor.py"


def _load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _insert_memory(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    target: str = "ops",
    source: str = "journal-digest",
    summary: str = "candidate summary",
    content: str = "candidate content",
    metadata: dict | None = None,
    updated_at: str | None = None,
) -> None:
    at = updated_at or datetime.now(timezone.utc).isoformat()
    payload = {
        "lifecycle": "candidate",
        "memory_type": "workflow",
        "confidence": 0.82,
        "importance": 0.66,
        **(metadata or {}),
    }
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
            agent_identity, agent_workspace, session_id, source, target, content, summary,
            created_at, updated_at, last_recalled_turn, metadata
        ) VALUES (?, ?, '', '', '', '', '', '', '', '', ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            memory_id,
            "scope-test",
            source,
            target,
            content,
            summary,
            at,
            at,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    conn.commit()


def test_candidate_classifier_promotes_stable_rows_and_keeps_high_risk(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = _conn(db_path)
    try:
        _insert_memory(conn, "safe", summary="Stable workflow", content="Run pytest and doctor before rollout.")
        _insert_memory(conn, "risky", summary="Risky release", content="Run git push and tag after release approval.")
        safe = conn.execute("SELECT * FROM memories WHERE id='safe'").fetchone()
        risky = conn.execute("SELECT * FROM memories WHERE id='risky'").fetchone()
        assert classify_candidate_row(safe).action == "promote"
        risky_decision = classify_candidate_row(risky)
        assert risky_decision.action == "keep_candidate"
        assert risky_decision.risk == "high"
    finally:
        conn.close()


def test_promote_memory_candidates_dry_run_is_read_only_and_apply_audits(tmp_path):
    hermes_home = tmp_path / "hermes"
    db_dir = hermes_home / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = _conn(db_path)
    old_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    try:
        _insert_memory(conn, "safe", summary="Stable workflow", content="Run pytest and doctor before rollout.", updated_at=old_at)
        _insert_memory(
            conn,
            "noise",
            summary="Conversation summary",
            content="One-off transcript digest that should not become a durable profile row.",
            metadata={"memory_type": "summary", "confidence": 0.62, "importance": 0.5},
            updated_at=old_at,
        )
    finally:
        conn.close()

    script = _load_script_module("promote_memory_candidates_script", SCRIPT_PATH)
    dry = script.promote_memory_candidates(hermes_home, apply=False)
    assert dry["dry_run"] is True
    assert dry["mutations"]["promoted"] == 1
    assert dry["before"]["candidate_count"] == 2

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM governance_audit_events").fetchone()[0] == 0
        lifecycle = conn.execute("SELECT json_extract(metadata, '$.lifecycle') FROM memories WHERE id='safe'").fetchone()[0]
        assert lifecycle == "candidate"
    finally:
        conn.close()

    applied = script.promote_memory_candidates(hermes_home, apply=True, batch_id="batch-test")
    assert applied["dry_run"] is False
    assert applied["mutations"]["promoted"] == 1
    assert applied["mutations"]["archived"] == 0
    assert applied["after"]["candidate_count"] == 1

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT json_extract(metadata, '$.lifecycle') FROM memories WHERE id='safe'").fetchone()[0] == "promoted"
        assert conn.execute("SELECT json_extract(metadata, '$.lifecycle') FROM memories WHERE id='noise'").fetchone()[0] == "candidate"
        event = conn.execute("SELECT event_type, action, batch_id, target_id FROM governance_audit_events").fetchone()
        assert event == ("memory_candidate_promotion", "promote", "batch-test", "safe")
    finally:
        conn.close()


def test_promote_memory_candidates_cli_dry_run_wins_and_sanitizes_reviewed_summaries(tmp_path):
    hermes_home = tmp_path / "hermes"
    db_dir = hermes_home / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = _conn(db_path)
    try:
        _insert_memory(
            conn,
            "risky-secret",
            summary="token=ghp_abcdefghijklmnopqrstuvwxyz123456 at /home/a/private/file.txt",
            content="Do not promote this credential-like candidate.",
            metadata={"memory_type": "workflow", "confidence": 0.9, "importance": 0.9},
        )
    finally:
        conn.close()

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--hermes-home", str(hermes_home), "--apply", "--dry-run", "--json"],
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    reviewed_summary = payload["reviewed"][0]["summary"]
    assert "ghp_" not in reviewed_summary
    assert "/home/a" not in reviewed_summary
    assert "[REDACTED_SECRET]" in reviewed_summary
    assert "[REDACTED_PATH]" in reviewed_summary

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM governance_audit_events").fetchone()[0] == 0
        assert conn.execute("SELECT json_extract(metadata, '$.lifecycle') FROM memories WHERE id='risky-secret'").fetchone()[0] == "candidate"
    finally:
        conn.close()


def test_candidate_debt_report_and_doctor_surface_backlog(tmp_path):
    hermes_home = tmp_path / "hermes"
    db_dir = hermes_home / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = _conn(db_path)
    try:
        old_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        _insert_memory(conn, "safe", summary="Stable workflow", content="Run pytest and doctor before rollout.", updated_at=old_at)
        report = candidate_debt_report(conn)
        assert report["candidate_count"] == 1
        assert report["by_action"]["promote"] == 1
        assert report["oldest_age_hours"] >= 24 * 7
    finally:
        conn.close()

    doctor = _load_script_module("doctor_candidate_debt", DOCTOR_PATH)
    payload, check, recommendations = doctor.memory_candidate_debt_report(hermes_home)
    assert check["ok"] is True
    assert payload["candidate_count"] == 1
    assert payload["by_action"]["promote"] == 1
    assert any("promote.memory_candidates.py" in item for item in recommendations)
