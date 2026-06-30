"""Tests for candidate promotion planning, archive-noise choices, and apply behavior.

They protect promoted-only profile behavior from stale or unsafe candidate debt."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scope_recall.candidate_promotion import candidate_debt_report, classify_candidate_row
from scope_recall.governance_cleanup import governance_audit_coverage_report
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
        safe_decision = classify_candidate_row(safe)
        assert safe_decision.action == "promote"
        assert safe_decision.lane == "promote_safe"
        risky_decision = classify_candidate_row(risky)
        assert risky_decision.action == "keep_candidate"
        assert risky_decision.risk == "high"
        assert risky_decision.lane == "needs_review_high_risk"
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
    assert {item["lane"] for item in dry["reviewed"]} == {"promote_safe", "archive_low_value"}
    assert dry["before"]["candidate_count"] == 2
    assert dry["before"]["by_lane"]["promote_safe"] == 1

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


def test_promote_memory_candidates_action_filter_can_archive_low_value_lane(tmp_path):
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

    script = _load_script_module("promote_memory_candidates_action_filter", SCRIPT_PATH)
    applied = script.promote_memory_candidates(hermes_home, apply=True, action="archive_low_value", batch_id="archive-low-value")

    assert applied["action_filter"] == "archive_low_value"
    assert applied["mutations"] == {"promoted": 0, "archived": 1, "kept": 0, "skipped": 0}
    assert applied["reviewed"] == [
        {
            "id": "noise",
            "target": "ops",
            "source": "journal-digest",
            "decision": "archive",
            "lane": "archive_low_value",
            "effective_action": "archive",
            "reason": "low_value_memory_type:summary",
            "classifier_reason": "low_value_memory_type:summary",
            "risk": "low",
            "confidence": 0.62,
            "importance": 0.5,
            "memory_type": "summary",
            "updated_at": old_at,
            "summary": "Conversation summary",
        }
    ]

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT json_extract(metadata, '$.lifecycle') FROM memories WHERE id='safe'").fetchone()[0] == "candidate"
        assert conn.execute("SELECT json_extract(metadata, '$.lifecycle') FROM memories WHERE id='noise'").fetchone()[0] == "archived"
        event = conn.execute("SELECT event_type, action, batch_id, target_id FROM governance_audit_events").fetchone()
        assert event == ("memory_candidate_promotion", "archive", "archive-low-value", "noise")
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


def test_promote_memory_candidates_operator_review_ids_file_archives_explicit_candidates(tmp_path):
    hermes_home = tmp_path / "hermes"
    db_dir = hermes_home / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = _conn(db_path)
    try:
        _insert_memory(conn, "manual-archive", summary="Risky release", content="Run git push and tag after release approval.")
        _insert_memory(conn, "other", summary="Risky sudo", content="Use sudo only after review.")
    finally:
        conn.close()

    ids_file = tmp_path / "ids.jsonl"
    ids_file.write_text('{"id":"manual-archive"}\n', encoding="utf-8")
    script = _load_script_module("promote_memory_candidates_operator_review", SCRIPT_PATH)
    review_ids = script._load_review_ids_file(str(ids_file))

    dry = script.promote_memory_candidates(
        hermes_home,
        apply=False,
        review_ids=review_ids,
        review_decision="archive",
        review_reason="reviewed as stale release-status task flow",
        batch_id="manual-review",
    )
    assert dry["dry_run"] is True
    assert dry["mutations"] == {"promoted": 0, "archived": 1, "kept": 0, "skipped": 0}
    assert dry["reviewed"][0]["id"] == "manual-archive"
    assert dry["reviewed"][0]["reason"] == "operator_review:reviewed as stale release-status task flow"

    applied = script.promote_memory_candidates(
        hermes_home,
        apply=True,
        review_ids=review_ids,
        review_decision="archive",
        review_reason="reviewed as stale release-status task flow",
        batch_id="manual-review",
    )
    assert applied["mutations"] == {"promoted": 0, "archived": 1, "kept": 0, "skipped": 0}
    assert applied["after"]["candidate_count"] == 1

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT json_extract(metadata, '$.lifecycle') FROM memories WHERE id='manual-archive'").fetchone()[0] == "archived"
        assert conn.execute("SELECT json_extract(metadata, '$.lifecycle') FROM memories WHERE id='other'").fetchone()[0] == "candidate"
        event = conn.execute("SELECT action, reason, batch_id, target_id FROM governance_audit_events").fetchone()
        assert event == ("archive", "operator_review:reviewed as stale release-status task flow", "manual-review", "manual-archive")
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
        assert report["by_lane"]["promote_safe"] == 1
        assert report["oldest_age_hours"] >= 24 * 7
    finally:
        conn.close()

    doctor = _load_script_module("doctor_candidate_debt", DOCTOR_PATH)
    payload, check, recommendations = doctor.memory_candidate_debt_report(hermes_home)
    assert check["ok"] is True
    assert payload["candidate_count"] == 1
    assert payload["by_action"]["promote"] == 1
    assert any("promote.memory_candidates.py" in item for item in recommendations)


def test_promote_memory_candidates_does_not_audit_when_update_is_ignored(tmp_path):
    hermes_home = tmp_path / "hermes"
    db_dir = hermes_home / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = _conn(db_path)
    old_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    try:
        _insert_memory(conn, "safe", summary="Stable workflow", content="Run pytest and doctor before rollout.", updated_at=old_at)
        conn.execute(
            """
            CREATE TRIGGER ignore_safe_candidate_update
            BEFORE UPDATE OF metadata ON memories
            WHEN OLD.id = 'safe'
            BEGIN
                SELECT RAISE(IGNORE);
            END;
            """
        )
        conn.commit()
    finally:
        conn.close()

    script = _load_script_module("promote_memory_candidates_rowcount", SCRIPT_PATH)
    applied = script.promote_memory_candidates(hermes_home, apply=True, batch_id="batch-ignored")

    assert applied["mutations"]["promoted"] == 0
    assert applied["mutations"]["skipped"] == 1
    assert applied["reviewed"][0]["effective_action"] == "skip"
    assert applied["reviewed"][0]["skip_reason"] == "row_not_updated"
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT json_extract(metadata, '$.lifecycle') FROM memories WHERE id='safe'").fetchone()[0] == "candidate"
        assert conn.execute("SELECT COUNT(*) FROM governance_audit_events WHERE target_id = 'safe'").fetchone()[0] == 0
    finally:
        conn.close()


def test_candidate_promotion_archive_counts_as_governance_audited_archive(tmp_path):
    hermes_home = tmp_path / "hermes"
    db_dir = hermes_home / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = _conn(db_path)
    old_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    try:
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

    script = _load_script_module("promote_memory_candidates_audit_coverage", SCRIPT_PATH)
    script.promote_memory_candidates(hermes_home, apply=True, action="archive_low_value", batch_id="archive-low-value")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        report = governance_audit_coverage_report(conn, scope_ids=["scope-test"])
    finally:
        conn.close()

    assert report["status"] == "ready"
    assert report["archived_total"] == 1
    assert report["archived_with_audit"] == 1
    assert report["archived_without_audit"] == 0
