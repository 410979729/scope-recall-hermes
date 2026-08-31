"""Tests for read-only memory browser CLI routes."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import scope_recall.cli as cli
from scope_recall.sql_store import ensure_schema, store_row

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PLUGIN_ROOT / "scripts" / "memory.browser.py"


def _insert_browser_row(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    scope_id: str = "scope-a",
    source: str = "event-digest",
    target: str = "memory",
    content: str = "Candidate memory waits for operator review.",
    metadata: dict[str, object] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
            agent_identity, agent_workspace, session_id, source, target, content,
            summary, created_at, updated_at, metadata
        ) VALUES (?, ?, 'cli', 'joy', '', '', '', 'yuheng', 'hermes', 'session', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            scope_id,
            source,
            target,
            content,
            content,
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            json.dumps(metadata or {}, ensure_ascii=False),
        ),
    )


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        store_row(
            conn,
            memory_id="mem-project",
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
            target="project",
            content="Project memory browser should list durable project facts read only.",
            metadata=json.dumps({"lifecycle": "active", "memory_type": "project"}, ensure_ascii=False),
            allow_duplicate=True,
        )
        secret_value = "tok" * 6
        private_path = "/home/" + "alice/private.txt"
        store_row(
            conn,
            memory_id="mem-sensitive",
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
            target="project",
            content="Legacy sensitive placeholder before raw import simulation.",
            metadata=json.dumps(
                {"lifecycle": "active", "memory_type": "project"},
                ensure_ascii=False,
            ),
            allow_duplicate=True,
        )
        legacy_content = (
            f"Legacy imported row has api_key={secret_value} and path {private_path}."
        )
        conn.execute(
            "UPDATE memories SET content=?, summary=?, metadata=? "
            "WHERE id='mem-sensitive'",
            (
                legacy_content,
                legacy_content,
                json.dumps(
                    {
                        "lifecycle": "active",
                        "memory_type": "project",
                        "note": f"token: {secret_value} at {private_path}",
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        _insert_browser_row(
            conn,
            memory_id="cand-1",
            metadata={
                "origin_kind": "event_digest",
                "lifecycle": "candidate",
                "candidate_status": "needs_review",
                "review_status": "pending",
                "memory_type": "factual",
                "automatic_admission": {
                    "source": "event_digest",
                    "route": "memory_review",
                    "reviewed": False,
                },
            },
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _run_browser(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=PLUGIN_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_cli_routes_readonly_browser_commands():
    assert cli._match_script_command(["memories", "list", "--target", "project", "--json"]) == (
        "memory.browser.py",
        ["memories", "list", "--target", "project", "--json"],
    )
    assert cli._match_script_command(["memories", "inspect", "--id", "mem-project", "--json"]) == (
        "memory.browser.py",
        ["memories", "inspect", "--id", "mem-project", "--json"],
    )
    assert cli._match_script_command(["candidates", "list", "--json"]) == (
        "memory.browser.py",
        ["candidates", "list", "--json"],
    )
    assert cli._match_script_command(["recall", "explain", "--query", "browser", "--json"]) == (
        "memory.browser.py",
        ["recall", "explain", "--query", "browser", "--json"],
    )


def test_memory_browser_lists_and_inspects_readonly_rows(tmp_path: Path):
    db_path = _make_db(tmp_path)

    listed = _run_browser("memories", "list", "--db", str(db_path), "--target", "project", "--json")
    assert listed.returncode == 0, listed.stderr
    payload = json.loads(listed.stdout)
    assert payload["ok"] is True
    assert payload["read_only"] is True
    assert payload["count"] == 2
    ids = {item["id"] for item in payload["memories"]}
    assert {"mem-project", "mem-sensitive"}.issubset(ids)
    assert all("content" not in item for item in payload["memories"])

    inspected = _run_browser("memories", "inspect", "--db", str(db_path), "--id", "mem-project", "--json")
    assert inspected.returncode == 0, inspected.stderr
    detail = json.loads(inspected.stdout)
    assert detail["ok"] is True
    assert detail["memory"]["content"].startswith("Project memory browser")


def test_memory_browser_redacts_sensitive_legacy_rows_by_default(tmp_path: Path):
    db_path = _make_db(tmp_path)
    secret_value = "tok" * 6
    private_path = "/home/" + "alice/private.txt"

    inspected = _run_browser("memories", "inspect", "--db", str(db_path), "--id", "mem-sensitive", "--json")
    assert inspected.returncode == 0, inspected.stderr
    detail = json.loads(inspected.stdout)
    rendered = json.dumps(detail, ensure_ascii=False)
    assert detail["raw"] is False
    assert detail["memory"]["redacted"] is True
    assert secret_value not in rendered
    assert private_path not in rendered
    assert "[REDACTED_SECRET]" in rendered
    assert "[REDACTED_PATH]" in rendered

    raw = _run_browser("memories", "inspect", "--db", str(db_path), "--id", "mem-sensitive", "--raw", "--json")
    assert raw.returncode == 0, raw.stderr
    raw_detail = json.loads(raw.stdout)
    raw_rendered = json.dumps(raw_detail, ensure_ascii=False)
    assert raw_detail["raw"] is True
    assert raw_detail["memory"]["redacted"] is False
    assert secret_value in raw_rendered
    assert private_path in raw_rendered


def test_memory_browser_lists_candidates_and_explains_recall_preview(tmp_path: Path):
    db_path = _make_db(tmp_path)

    candidates = _run_browser("candidates", "list", "--db", str(db_path), "--json")
    assert candidates.returncode == 0, candidates.stderr
    payload = json.loads(candidates.stdout)
    assert payload["count"] == 1
    assert payload["candidates"][0]["id"] == "cand-1"
    candidate = payload["candidates"][0]
    assert candidate["origin_kind"] == "event_digest"
    assert candidate["source"] == "event-digest"
    assert candidate["lifecycle"] == "candidate"
    assert candidate["review_status"] == "pending"
    assert candidate["automatic_admission"] == {
        "source": "event_digest",
        "route": "memory_review",
        "reviewed": False,
    }

    explain = _run_browser("recall", "explain", "--db", str(db_path), "--query", "project browser", "--json")
    assert explain.returncode == 0, explain.stderr
    trace = json.loads(explain.stdout)
    assert trace["ok"] is True
    assert trace["mode"] == "lexical-readonly-preview"
    assert trace["trace"]["lifecycle_filtered"] == 1
    assert trace["results"][0]["id"] == "mem-project"
    assert "project" in trace["results"][0]["explain"]["matched_terms"]


def test_memory_browser_candidate_list_queries_candidates_directly(tmp_path: Path):
    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for index in range(100):
            store_row(
                conn,
                memory_id=f"active-{index:03d}",
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
                content=f"Active promoted row {index}",
                metadata=json.dumps({"lifecycle": "active", "memory_type": "factual"}, ensure_ascii=False),
                allow_duplicate=True,
            )
        conn.execute("UPDATE memories SET updated_at = '2026-01-01T00:00:00+00:00' WHERE id = 'cand-1'")
        conn.commit()
    finally:
        conn.close()

    candidates = _run_browser("candidates", "list", "--db", str(db_path), "--limit", "20", "--json")
    assert candidates.returncode == 0, candidates.stderr
    payload = json.loads(candidates.stdout)
    assert payload["count"] == 1
    assert payload["candidates"][0]["id"] == "cand-1"


def test_candidate_json_defaults_to_compact_summary_and_full_is_explicit(tmp_path: Path):
    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _insert_browser_row(
            conn,
            memory_id="cand-large",
            content="Large candidate remains readable without dumping its complete evidence payload.",
            metadata={
                "lifecycle": "candidate",
                "memory_type": "factual",
                "confidence": 0.91,
                "evidence_payload": ["evidence-block-" + ("x" * 1000) for _ in range(200)],
            },
        )
        conn.commit()
    finally:
        conn.close()

    compact = _run_browser("candidates", "list", "--db", str(db_path), "--limit", "20", "--json")
    assert compact.returncode == 0, compact.stderr
    compact_payload = json.loads(compact.stdout)
    assert compact_payload["detail"] == "summary"
    assert len(compact.stdout.encode("utf-8")) < 20_000
    compact_large = next(item for item in compact_payload["candidates"] if item["id"] == "cand-large")
    assert "evidence_payload" not in compact_large["metadata"]
    assert compact_large["metadata_omitted_keys_count"] >= 1

    full = _run_browser("candidates", "list", "--db", str(db_path), "--limit", "20", "--full", "--json")
    assert full.returncode == 0, full.stderr
    full_payload = json.loads(full.stdout)
    assert full_payload["detail"] == "full"
    full_large = next(item for item in full_payload["candidates"] if item["id"] == "cand-large")
    assert len(full_large["metadata"]["evidence_payload"]) == 200
    assert len(full.stdout.encode("utf-8")) > len(compact.stdout.encode("utf-8"))


def test_memory_browser_candidates_exclude_processed_event_digest_rows(tmp_path: Path):
    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for lifecycle in ("promoted", "archived", "rejected"):
            _insert_browser_row(
                conn,
                memory_id=f"event-{lifecycle}",
                content=f"Processed event digest row {lifecycle} should stay out of candidates.",
                metadata={"lifecycle": lifecycle, "event_digest": True, "candidate_status": lifecycle},
            )
        _insert_browser_row(
            conn,
            memory_id="event-legacy",
            content="Legacy event digest row without lifecycle remains reviewable.",
            metadata={"event_digest": True},
        )
        conn.commit()
    finally:
        conn.close()

    candidates = _run_browser("candidates", "list", "--db", str(db_path), "--limit", "20", "--json")
    assert candidates.returncode == 0, candidates.stderr
    payload = json.loads(candidates.stdout)
    ids = {item["id"] for item in payload["candidates"]}

    assert ids == {"cand-1", "event-legacy"}
