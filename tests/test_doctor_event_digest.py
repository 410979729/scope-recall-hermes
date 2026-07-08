"""Tests for event-digest doctor reporting."""

from __future__ import annotations

import json
import sqlite3

from scope_recall.candidate_extraction import extract_candidates_from_packet
from scope_recall.candidate_store import store_event_candidates
from scope_recall.doctor_event_digest import event_digest_report
from scope_recall.event_digest import MemoryEvent, build_evidence_packet
from scope_recall.models import RuntimeScope
from scope_recall.sql_store import ensure_schema


def test_event_digest_report_summarizes_config_without_db(tmp_path):
    payload, check, recommendations = event_digest_report(
        tmp_path,
        {"event_digest": {"enabled": True, "write_candidates": False, "dry_run_log": True, "max_events_per_turn": 3}},
    )

    assert check["ok"] is True
    assert recommendations == []
    assert payload["status"] == "missing_db"
    assert payload["config"]["enabled"] is True
    assert payload["config"]["write_candidates"] is False


def test_event_digest_report_counts_candidate_rows_and_audit_events(tmp_path):
    db_path = tmp_path / "scope-recall" / "memory.sqlite3"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
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
    candidates = extract_candidates_from_packet(packet).candidates
    store_event_candidates(
        conn,
        candidates=candidates,
        scope=RuntimeScope(platform="telegram", user_id="user-a", chat_id="chat-a"),
        scope_id="scope-a",
        session_id="session-a",
        dry_run=False,
    )
    conn.close()

    payload, check, recommendations = event_digest_report(
        tmp_path,
        {"event_digest": {"enabled": True, "write_candidates": True, "dry_run_log": True}},
    )

    assert check["ok"] is True
    assert recommendations == []
    assert payload["candidates_persisted"] == 1
    assert payload["oldest_candidate_at"]
    assert payload["oldest_candidate_age_hours"] >= 0
    assert payload["high_risk_candidate_count"] == 0
    assert payload["audit_events"] == 1
    assert payload["insert_candidate_events"] == 1
    assert payload["audit_missing"] == 0


def test_event_digest_report_flags_missing_audit_coverage(tmp_path):
    db_path = tmp_path / "scope-recall" / "memory.sqlite3"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    metadata = json.dumps({"lifecycle": "candidate", "digest_quality": {"recommended_action": "candidate"}, "risk_flags": ["high_risk_candidate"]})
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
            agent_identity, agent_workspace, session_id, source, target, content, summary,
            created_at, updated_at, last_recalled_turn, dedup_key, metadata
        ) VALUES ('m1', 'scope-a', '', '', '', '', '', '', '', 's', 'event-digest', 'user', 'content', 'summary', 'now', 'now', 0, 'd', ?)
        """,
        (metadata,),
    )
    conn.commit()
    conn.close()

    payload, check, recommendations = event_digest_report(tmp_path, {"event_digest": {"enabled": True}})

    assert check["ok"] is False
    assert payload["audit_missing"] == 1
    assert payload["high_risk_candidate_count"] == 1
    assert recommendations
