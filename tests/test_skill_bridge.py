"""Tests for Experience-to-Skill bridge candidate schema."""

from __future__ import annotations

import pytest

from scope_recall.experience_models import ExperienceValidationError
from scope_recall.experience_store import create_playbook, review_playbook
from scope_recall.skill_bridge import SKILL_CANDIDATE_SCHEMA_VERSION, generate_skill_candidates, record_skill_feedback, validate_skill_candidate
from scope_recall.sql_store import ensure_schema

import sqlite3


def _candidate_payload(**overrides):
    payload = {
        "schema_version": SKILL_CANDIDATE_SCHEMA_VERSION,
        "source_playbook_id": "pb_123",
        "title": "Scope Recall rollout smoke checks",
        "trigger_conditions": ["When preparing a scope-recall rollout"],
        "steps": ["Run focused pytest before release gate", "Run doctor/dashboard smoke after config changes"],
        "verification": ["Focused pytest exits 0", "Doctor/dashboard expose expected sections"],
        "pitfalls": ["Do not auto-write formal skills from candidates"],
        "risk_class": "medium",
        "evidence_refs": ["playbook:pb_123", "session:s1:turn:4"],
    }
    payload.update(overrides)
    return payload


def test_validate_skill_candidate_normalizes_and_serializes_payload():
    candidate = validate_skill_candidate(_candidate_payload(risk_class="MEDIUM", title="  Scope Recall rollout smoke checks  "))

    assert candidate.schema_version == "skill_candidate.v1"
    assert candidate.source_playbook_id == "pb_123"
    assert candidate.title == "Scope Recall rollout smoke checks"
    assert candidate.risk_class == "medium"
    assert candidate.requires_operator_review is True
    assert candidate.to_dict() == {
        "schema_version": "skill_candidate.v1",
        "source_playbook_id": "pb_123",
        "title": "Scope Recall rollout smoke checks",
        "trigger_conditions": ["When preparing a scope-recall rollout"],
        "steps": ["Run focused pytest before release gate", "Run doctor/dashboard smoke after config changes"],
        "verification": ["Focused pytest exits 0", "Doctor/dashboard expose expected sections"],
        "pitfalls": ["Do not auto-write formal skills from candidates"],
        "risk_class": "medium",
        "evidence_refs": ["playbook:pb_123", "session:s1:turn:4"],
        "requires_operator_review": True,
    }


def test_validate_skill_candidate_requires_evidence_and_steps():
    with pytest.raises(ExperienceValidationError, match="evidence_refs must contain at least one item"):
        validate_skill_candidate(_candidate_payload(evidence_refs=[]))
    with pytest.raises(ExperienceValidationError, match="steps must contain at least one item"):
        validate_skill_candidate(_candidate_payload(steps=[]))


def test_validate_skill_candidate_rejects_invalid_risk_and_secret_like_content():
    with pytest.raises(ExperienceValidationError, match="risk_class must be one of"):
        validate_skill_candidate(_candidate_payload(risk_class="critical"))
    with pytest.raises(ExperienceValidationError, match="secret-like content is not allowed"):
        validate_skill_candidate(_candidate_payload(steps=["Use " + "api_" + "key=" + "sk" + "-tes...alue before rollout"]))


def _playbook_payload(*, capability_class: str = "read_only") -> dict:
    return {
        "schema_version": "procedural_playbook.v1",
        "task_class": "scope-recall-rollout",
        "title": "Scope Recall rollout smoke checks",
        "trigger": "When preparing a Scope Recall rollout",
        "goal": "Verify rollout readiness before publishing.",
        "preconditions": [{"name": "clean worktree", "required": True}],
        "steps": [
            {
                "number": 1,
                "capability_class": capability_class,
                "action": "Run focused pytest before release gate.",
                "evidence_required": "pytest output exits 0",
                "why": "Catches regressions before packaging.",
                "previous_mistakes": [],
            }
        ],
        "pitfalls": [{"summary": "Do not publish without release gate output."}],
        "verification": ["Focused pytest exits 0", "Doctor/dashboard expose expected sections"],
        "cleanup": ["Remove temporary smoke directories"],
        "reuse_policy": {"min_confidence": 0.75},
    }


def _conn(tmp_path):
    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def test_generate_skill_candidates_from_successful_reviewed_playbooks(tmp_path):
    conn = _conn(tmp_path)
    create_playbook(
        conn,
        playbook_id="pb_success",
        scope_id="scope-a",
        payload=_playbook_payload(),
        evidence_anchors=["session:s1:turn:4"],
        confidence=0.91,
    )
    conn.execute("UPDATE procedural_playbooks SET status='reviewed', success_count=2, failure_count=0, stale_count=0 WHERE id='pb_success'")
    conn.commit()

    report = generate_skill_candidates(conn, accessible_scope_ids=["scope-a"], dry_run=True)

    assert report["dry_run"] is True
    assert report["count"] == 1
    candidate = report["candidates"][0]
    assert candidate["schema_version"] == "skill_candidate.v1"
    assert candidate["source_playbook_id"] == "pb_success"
    assert candidate["title"] == "Scope Recall rollout smoke checks"
    assert candidate["trigger_conditions"] == ["When preparing a Scope Recall rollout"]
    assert candidate["steps"] == ["Run focused pytest before release gate."]
    assert candidate["risk_class"] == "low"
    assert candidate["evidence_refs"] == ["playbook:pb_success", "session:s1:turn:4"]


def test_generate_skill_candidates_rejects_failed_or_stale_playbooks(tmp_path):
    conn = _conn(tmp_path)
    for playbook_id, failure_count, stale_count in [("pb_failed", 1, 0), ("pb_stale", 0, 1)]:
        create_playbook(
            conn,
            playbook_id=playbook_id,
            scope_id="scope-a",
            payload=_playbook_payload(),
            evidence_anchors=[f"playbook:{playbook_id}"],
            confidence=0.91,
        )
        conn.execute(
            "UPDATE procedural_playbooks SET status='reviewed', success_count=2, failure_count=?, stale_count=? WHERE id=?",
            (failure_count, stale_count, playbook_id),
        )
    conn.commit()

    report = generate_skill_candidates(conn, accessible_scope_ids=["scope-a"], dry_run=True)

    assert report["count"] == 0
    assert {item["id"]: item["reason"] for item in report["rejected"]} == {
        "pb_failed": "negative_or_stale_feedback",
        "pb_stale": "negative_or_stale_feedback",
    }


def test_generate_skill_candidates_marks_risky_playbooks_high_review_only(tmp_path):
    conn = _conn(tmp_path)
    create_playbook(
        conn,
        playbook_id="pb_risky",
        scope_id="scope-a",
        payload=_playbook_payload(capability_class="service_control"),
        evidence_anchors=["session:s1:turn:9"],
        confidence=0.9,
    )
    conn.execute("UPDATE procedural_playbooks SET status='reviewed', success_count=3 WHERE id='pb_risky'")
    conn.commit()

    report = generate_skill_candidates(conn, accessible_scope_ids=["scope-a"], dry_run=True)

    assert report["count"] == 1
    candidate = report["candidates"][0]
    assert candidate["risk_class"] == "high"
    assert candidate["requires_operator_review"] is True


def test_record_skill_feedback_routes_skill_failure_to_playbook_until_threshold(tmp_path):
    conn = _conn(tmp_path)
    create_playbook(
        conn,
        playbook_id="pb_skill_feedback",
        scope_id="scope-a",
        payload=_playbook_payload(),
        evidence_anchors=["session:s1:turn:4"],
        related_skills=["scope-recall-release"],
        confidence=0.91,
    )
    review_playbook(conn, playbook_id="pb_skill_feedback", accessible_scope_ids=["scope-a"], action="promote", reason="fixture")

    first = record_skill_feedback(
        conn,
        skill_name="scope-recall-release",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        outcome="failed",
        evidence=["skill smoke failed once"],
        outcome_reason="generated skill is stale",
        failure_threshold=2,
    )
    row_after_first = conn.execute("SELECT status, failure_count FROM procedural_playbooks WHERE id = ?", ("pb_skill_feedback",)).fetchone()

    assert first["recorded"] is True
    assert first["count"] == 1
    assert first["needs_review_count"] == 0
    assert first["results"][0]["id"] == "pb_skill_feedback"
    assert first["results"][0]["status"] == "promoted"
    assert row_after_first["status"] == "promoted"
    assert row_after_first["failure_count"] == 1
    run = conn.execute("SELECT evidence FROM experience_runs WHERE playbook_id = ?", ("pb_skill_feedback",)).fetchone()
    assert "skill:scope-recall-release" in run["evidence"]

    second = record_skill_feedback(
        conn,
        skill_name="scope-recall-release",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        outcome="failed",
        evidence=["skill smoke failed again"],
        outcome_reason="generated skill is still stale",
        failure_threshold=2,
    )

    assert second["needs_review_count"] == 1
    assert second["results"][0]["status"] == "needs_review"
