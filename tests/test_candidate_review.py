"""Tests for dry-run-first candidate review commands."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import scope_recall.cli as cli
from scope_recall.sql_store import ensure_schema, store_row

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PLUGIN_ROOT / "scripts" / "candidate.review.py"


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        store_row(
            conn,
            memory_id="candidate-1",
            scope_id="scope-a",
            platform="cli",
            user_id="joy",
            chat_id="",
            thread_id="",
            gateway_session_key="",
            agent_identity="yuheng",
            agent_workspace="hermes",
            session_id="session",
            source="event-digest",
            target="memory",
            content="Candidate review commands should be dry-run first.",
            metadata=json.dumps({"event_digest": True, "lifecycle": "candidate"}, ensure_ascii=False),
            allow_duplicate=True,
        )
        store_row(
            conn,
            memory_id="memory-keep",
            scope_id="scope-a",
            platform="cli",
            user_id="joy",
            chat_id="",
            thread_id="",
            gateway_session_key="",
            agent_identity="yuheng",
            agent_workspace="hermes",
            session_id="session",
            source="tool-store",
            target="memory",
            content="Existing promoted memory can supersede a candidate.",
            allow_duplicate=True,
        )
    finally:
        conn.close()
    return db_path


def _metadata(db_path: Path, memory_id: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return json.loads(row["metadata"])
    finally:
        conn.close()


def _run_review(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=PLUGIN_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_cli_routes_candidate_review_commands_dry_run_first():
    assert cli._match_script_command(["candidates", "promote", "--id", "candidate-1", "--json"]) == (
        "candidate.review.py",
        ["promote", "--dry-run", "--id", "candidate-1", "--json"],
    )
    assert cli._match_script_command(["candidates", "archive", "--id", "candidate-1", "--apply", "--json"]) == (
        "candidate.review.py",
        ["archive", "--id", "candidate-1", "--apply", "--json"],
    )
    assert cli._match_script_command(["candidates", "supersede", "--id", "candidate-1", "--superseded-by", "memory-keep"]) == (
        "candidate.review.py",
        ["supersede", "--dry-run", "--id", "candidate-1", "--superseded-by", "memory-keep"],
    )


def test_candidate_review_promote_defaults_to_dry_run_without_mutation(tmp_path: Path):
    db_path = _make_db(tmp_path)

    result = _run_review("promote", "--db", str(db_path), "--id", "candidate-1", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["after"]["lifecycle"] == "promoted"
    assert _metadata(db_path, "candidate-1").get("candidate_review_action") is None


def test_candidate_review_apply_archives_and_writes_audit_event(tmp_path: Path):
    db_path = _make_db(tmp_path)

    result = _run_review("archive", "--db", str(db_path), "--id", "candidate-1", "--apply", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["dry_run"] is False
    assert payload["applied"] is True
    metadata = _metadata(db_path, "candidate-1")
    assert metadata["lifecycle"] == "archived"
    assert metadata["candidate_review_action"] == "archive"
    conn = sqlite3.connect(db_path)
    try:
        audit_count = conn.execute("SELECT COUNT(*) FROM governance_audit_events WHERE event_type = 'memory_candidate_review'").fetchone()[0]
    finally:
        conn.close()
    assert audit_count == 1


def test_candidate_review_supersede_requires_existing_replacement(tmp_path: Path):
    db_path = _make_db(tmp_path)

    missing = _run_review("supersede", "--db", str(db_path), "--id", "candidate-1", "--superseded-by", "missing", "--json")
    assert missing.returncode == 1
    assert "superseded-by memory not found" in json.loads(missing.stdout)["error"]

    applied = _run_review(
        "supersede",
        "--db",
        str(db_path),
        "--id",
        "candidate-1",
        "--superseded-by",
        "memory-keep",
        "--apply",
        "--json",
    )
    assert applied.returncode == 0, applied.stderr
    metadata = _metadata(db_path, "candidate-1")
    assert metadata["lifecycle"] == "superseded"
    assert metadata["superseded_by"] == "memory-keep"
