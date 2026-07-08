"""Tests for explicit event-candidate storage and governance audit."""

from __future__ import annotations

import json
import sqlite3

from scope_recall.candidate_extraction import extract_candidates_from_packet
from scope_recall.candidate_store import store_event_candidates
from scope_recall.event_digest import MemoryEvent, build_evidence_packet
from scope_recall.models import RuntimeScope
from scope_recall.sql_store import ensure_schema


def test_store_event_candidates_writes_candidate_lifecycle_and_audit(tmp_path):
    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    packet = build_evidence_packet(
        MemoryEvent(
            kind="task_closeout",
            scope_id="scope-a",
            session_id="session-a",
            turn_number=4,
            content="User prefers concise Chinese release reports with exact verification outputs.",
            metadata={},
        )
    )
    result = extract_candidates_from_packet(packet)
    scope = RuntimeScope(platform="telegram", user_id="user-a", chat_id="chat-a", agent_identity="yuheng", agent_workspace="hermes")

    report = store_event_candidates(
        conn,
        candidates=result.candidates,
        scope=scope,
        scope_id="scope-a",
        session_id="session-a",
        dry_run=False,
    )

    assert report["inserted"] == 1
    assert report["dry_run"] is False
    row = conn.execute("SELECT * FROM memories").fetchone()
    metadata = json.loads(row["metadata"])
    assert row["target"] == "user"
    assert row["source"] == "event-digest"
    assert metadata["lifecycle"] == "candidate"
    assert metadata["memory_type"] == "preference"
    assert metadata["evidence_refs"] == ["session:session-a:turn:4"]
    audit = conn.execute("SELECT * FROM governance_audit_events").fetchone()
    assert audit["event_type"] == "event_candidate"
    assert audit["action"] == "insert_candidate"
    assert audit["target_id"] == row["id"]
    assert audit["dry_run"] == 0


def test_store_event_candidates_dry_run_does_not_write_rows(tmp_path):
    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    packet = build_evidence_packet(
        MemoryEvent(
            kind="task_closeout",
            scope_id="scope-a",
            session_id="session-a",
            turn_number=4,
            content="User prefers concise Chinese release reports with exact verification outputs.",
            metadata={},
        )
    )
    result = extract_candidates_from_packet(packet)
    scope = RuntimeScope(platform="telegram", user_id="user-a", chat_id="chat-a", agent_identity="yuheng", agent_workspace="hermes")

    report = store_event_candidates(
        conn,
        candidates=result.candidates,
        scope=scope,
        scope_id="scope-a",
        session_id="session-a",
        dry_run=True,
    )

    assert report["inserted"] == 0
    assert report["planned"] == 1
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM governance_audit_events").fetchone()[0] == 0
