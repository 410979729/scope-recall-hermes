"""Tests for the bounded, content-free candidate hygiene report."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from scope_recall.candidate_hygiene import (
    MAX_CANDIDATE_HYGIENE_LIMIT,
    build_candidate_hygiene_report,
    candidate_hygiene_report,
)
from scope_recall.sql_store import ensure_schema

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PLUGIN_ROOT / "scripts" / "report.candidate_hygiene.py"
PUBLIC_CANDIDATE_FIELDS = {
    "id",
    "origin_kind",
    "source",
    "lifecycle",
    "target",
    "memory_type",
    "transport_noise",
    "correction_possible",
    "evidence_count",
    "automatic_admission",
    "review_status",
    "recommended_action",
}


def _create_database(tmp_path: Path) -> Path:
    database_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(database_path)
    try:
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
    finally:
        conn.close()
    return database_path


def _insert_candidate(
    database_path: Path,
    memory_id: str,
    *,
    content: str,
    summary: str = "private candidate summary",
    source: str = "event-digest",
    metadata: dict[str, object] | None = None,
) -> None:
    payload: dict[str, object] = {
        "origin_kind": "event_digest",
        "lifecycle": "candidate",
        "memory_type": "workflow",
        "confidence": 0.92,
        "importance": 0.72,
        "evidence_refs": ["journal:fixture"],
        "review_status": "pending",
        "automatic_admission": {
            "source": "event_digest",
            "route": "memory_review",
            "reviewed": False,
        },
        **(metadata or {}),
    }
    conn = sqlite3.connect(database_path)
    try:
        conn.execute(
            """
            INSERT INTO memories(
                id, scope_id, platform, user_id, chat_id, thread_id,
                gateway_session_key, agent_identity, agent_workspace,
                session_id, source, target, content, summary, created_at,
                updated_at, last_recalled_turn, metadata
            ) VALUES (?, 'scope-a', '', '', '', '', '', '', '', '', ?,
                      'memory', ?, ?, '2026-08-30T00:00:00+00:00',
                      '2026-08-30T00:00:00+00:00', 0, ?)
            """,
            (
                memory_id,
                source,
                content,
                summary,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_report_is_read_only_bounded_and_never_emits_candidate_text(tmp_path: Path) -> None:
    database_path = _create_database(tmp_path)
    secret_content = "[CONTEXT COMPACTION] PRIVATE WRAPPER BODY"
    secret_summary = "PRIVATE SUMMARY MUST NEVER BE RENDERED"
    _insert_candidate(
        database_path,
        "transport-candidate",
        content=secret_content,
        summary=secret_summary,
        metadata={
            "automatic_admission": {
                "source": "event_digest",
                "route": "memory_review",
                "reviewed": False,
                "content": "NESTED PRIVATE CONTENT",
            }
        },
    )

    report = candidate_hygiene_report(database_path, limit=100_000)
    rendered = json.dumps(report, ensure_ascii=False)
    candidate = report["candidates"][0]

    assert report["read_only"] is True
    assert report["query_only"] is True
    assert report["total_changes"] == 0
    assert report["limit"] == MAX_CANDIDATE_HYGIENE_LIMIT
    assert set(candidate) == PUBLIC_CANDIDATE_FIELDS
    assert candidate["transport_noise"] is True
    assert candidate["recommended_action"] == "archive_transport_wrapper"
    assert candidate["correction_possible"] is None
    assert candidate["evidence_count"] == 1
    assert candidate["automatic_admission"] == {
        "source": "event_digest",
        "route": "memory_review",
        "reviewed": False,
    }
    assert secret_content not in rendered
    assert secret_summary not in rendered
    assert "NESTED PRIVATE CONTENT" not in rendered

    verify = sqlite3.connect(database_path)
    try:
        assert verify.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    finally:
        verify.close()


def test_report_does_not_infer_correction_and_keeps_review_boundary(tmp_path: Path) -> None:
    database_path = _create_database(tmp_path)
    _insert_candidate(
        database_path,
        "review-required",
        content="User prefers concise verified answers.",
        metadata={"memory_type": "preference"},
    )
    _insert_candidate(
        database_path,
        "known-correction",
        content="A corrected preference awaits review.",
        metadata={
            "memory_type": "preference",
            "correction_possible": True,
            "superseded_by": "promoted-replacement",
        },
    )

    report = candidate_hygiene_report(database_path, limit=10)
    by_id = {row["id"]: row for row in report["candidates"]}

    assert by_id["review-required"]["correction_possible"] is None
    assert by_id["review-required"]["recommended_action"] == "needs_review"
    assert by_id["review-required"]["origin_kind"] == "event_digest"
    assert by_id["review-required"]["lifecycle"] == "candidate"
    assert by_id["review-required"]["review_status"] == "pending"
    assert by_id["known-correction"]["correction_possible"] is True
    assert by_id["known-correction"]["recommended_action"] == "supersede_candidate"


def test_script_uses_the_same_content_free_read_only_contract(tmp_path: Path) -> None:
    database_path = _create_database(tmp_path)
    private_text = "private procedure content should stay inside classification"
    _insert_candidate(
        database_path,
        "script-candidate",
        source="tool-store",
        content=private_text,
        metadata={
            "origin_kind": "tool_store",
            "automatic_admission": {},
            "review_status": "reviewed",
            "admission_reviewed_at": "2026-08-30T00:00:00+00:00",
        },
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--db",
            str(database_path),
            "--limit",
            "1",
        ],
        cwd=PLUGIN_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    report = json.loads(result.stdout)
    candidate = report["candidates"][0]

    assert report["total_changes"] == 0
    assert set(candidate) == PUBLIC_CANDIDATE_FIELDS
    assert candidate["recommended_action"] == "promote_after_review"
    assert private_text not in result.stdout
    assert "content" not in candidate
    assert "summary" not in candidate


def test_library_report_restores_caller_connection_state() -> None:
    conn = sqlite3.connect(":memory:")
    original_factory = conn.row_factory
    ensure_schema(conn)
    before = conn.total_changes

    report = build_candidate_hygiene_report(conn)

    assert report["total_changes"] == 0
    assert conn.total_changes == before
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 0
    assert conn.row_factory is original_factory
    conn.execute("CREATE TABLE caller_can_still_write(value TEXT)")
    conn.close()
