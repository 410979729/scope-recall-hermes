"""Replay-generation tests for reusable Experience playbooks.

These cases make sure successful tasks can become guided playbooks, final
failures do not auto-enable procedures, and weak-model preflight receives live
checks plus verification steps.
"""
from __future__ import annotations

import sqlite3

from scope_recall.experience_evidence import extract_evidence_anchors
from scope_recall.experience_preflight import experience_preflight
from scope_recall.experience_quality import assess_experience_quality
from scope_recall.experience_store import create_playbook, review_playbook
from scope_recall.experience_synthesis import build_experience_playbook_payload
from scope_recall.sql_store import ensure_schema
from scope_recall.task_boundary import classify_task_closure


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _entry(entry_id: int, role: str, content: str) -> dict:
    return {"id": entry_id, "role": role, "content": content, "session_id": "experience-replay-fixture"}


def _successful_entries() -> list[dict]:
    return [
        _entry(1, "user", "Fix Scope Recall governance scheduler and verify tests."),
        _entry(2, "assistant", "I will inspect current code, patch the scheduler, and run pytest."),
        _entry(3, "tool", "python3 -m pytest tests/test_governance_scheduler.py -q\n4 passed"),
        _entry(4, "assistant", "完成：tests/test_governance_scheduler.py 4 passed，scheduler dry-run JSON verified."),
    ]


def test_successful_task_generates_valid_guided_playbook_with_replay_terms():
    entries = _successful_entries()
    closure = classify_task_closure(entries)
    anchors = extract_evidence_anchors(entries)
    quality = assess_experience_quality(entries, goal="Fix Scope Recall governance scheduler", tool_names=["terminal", "patch"], verification=["pytest tests passed"], risk_level="medium")
    payload = build_experience_playbook_payload(
        task_class="scope_recall_governance_scheduler",
        title="Scope Recall governance scheduler repair",
        goal="Fix Scope Recall governance scheduler",
        risk_level="high",
        tool_names=["terminal", "patch"],
        verification=["pytest tests passed", "dry-run JSON parsed"],
        evidence_anchors=anchors,
    )

    assert closure.state == "success"
    assert quality["decision"] in {"needs_review", "auto_promote_eligible"}
    assert payload["reuse_policy"]["default_decision"] == "guided_reuse"
    assert payload["reuse_policy"]["source_evidence_anchor_count"] >= 2
    assert any("pytest" in item for item in payload["verification"])
    assert "必须停下问 Joy" in " ".join(payload["reuse_policy"]["must_stop_and_ask_joy"])


def test_final_failure_is_not_accepted_for_experience_generation_even_with_historical_success():
    entries = [
        _entry(1, "user", "Fix release gate."),
        _entry(2, "tool", "python3 -m pytest tests/test_release.py -q\n10 passed"),
        _entry(3, "assistant", "最终失败：release gate 仍然缺少 pyright required file，未完成。"),
    ]

    closure = classify_task_closure(entries)
    quality = assess_experience_quality(entries, goal="Fix release gate", tool_names=["terminal"], verification=["pytest passed"], risk_level="medium")

    assert closure.state == "failed"
    assert quality["decision"] == "reject"
    assert "final_failure_signal" in quality["reasons"]


def test_preflight_for_generated_playbook_returns_verification_steps_for_weak_model():
    conn = _conn()
    try:
        entries = _successful_entries()
        anchors = extract_evidence_anchors(entries)
        payload = build_experience_playbook_payload(
            task_class="scope_recall_governance_scheduler",
            title="Scope Recall governance scheduler repair",
            goal="Fix Scope Recall governance scheduler",
            risk_level="high",
            tool_names=["terminal", "patch"],
            verification=["pytest tests passed", "dry-run JSON parsed"],
            evidence_anchors=anchors,
        )
        create_playbook(conn, playbook_id="pb_scheduler_repair", scope_id="scope-a", shared_scope_id="", payload=payload, status="candidate", confidence=0.9, evidence_anchors=anchors)
        review_playbook(conn, playbook_id="pb_scheduler_repair", accessible_scope_ids=["scope-a"], action="promote", reason="fixture")

        preflight = experience_preflight(
            conn,
            query="Scope Recall governance scheduler repair pytest dry-run JSON",
            accessible_scope_ids=["scope-a"],
            config={"experience": {"packet_max_chars": 2400}},
        )
    finally:
        conn.close()

    assert preflight["decision"] == "guided_reuse"
    assert preflight["summary"]["playbook_id"] == "pb_scheduler_repair"
    assert "pytest tests passed" in preflight["packet"]
    assert "dry-run JSON parsed" in preflight["packet"]
    assert preflight["summary"]["source_evidence_anchor_count"] >= 2
