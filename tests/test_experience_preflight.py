"""Tests for Experience preflight scoring, durable evidence, and promotion gates.

They prevent automation from promoting playbooks without auditable quality checks."""

from __future__ import annotations

import sqlite3

from scope_recall.experience_preflight import _query_is_low_signal, experience_preflight
from scope_recall.experience_store import create_playbook, record_playbook_feedback, review_playbook
from scope_recall.sql_store import ensure_schema


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _create_promoted(conn: sqlite3.Connection, *, playbook_id: str, payload: dict | None = None, confidence: float = 0.9) -> None:
    create_playbook(conn, playbook_id=playbook_id, scope_id="scope-a", shared_scope_id="", payload=payload or _payload(), status="candidate", confidence=confidence)
    review_playbook(conn, playbook_id=playbook_id, accessible_scope_ids=["scope-a"], action="promote", reason="fixture")


def _payload(*, risky: bool = False, reuse_policy: dict | None = None) -> dict:
    second_class = "service_control" if risky else "local_write"
    return {
        "schema_version": "procedural_playbook.v1",
        "task_class": "headscale_one_way_acl",
        "title": "Headscale/Tailscale one-way management ACL",
        "trigger": "User asks for one-way management access to a target node.",
        "goal": "Apply one-way isolation without losing remote access.",
        "preconditions": [
            {"id": "p1", "check": "Identify target node and management nodes from live output.", "evidence_required": "live node list"},
            {"id": "p2", "check": "Confirm rollback before applying ACL.", "evidence_required": "backup path and restore command"},
        ],
        "steps": [
            {
                "number": 1,
                "capability_class": "read_only",
                "action": "Read current policy and node list.",
                "evidence_required": "policy path plus live node list",
            },
            {
                "number": 2,
                "capability_class": second_class,
                "action": "Prepare/apply the minimal one-way ACL change after validation.",
                "evidence_required": "minimal diff, validation output, rollback path",
            },
        ],
        "pitfalls": [
            {"signal": "node appears in status", "mistake": "Treat visibility as reachability", "correction": "Run connectivity checks."}
        ],
        "verification": ["management reaches target", "target cannot reach unrelated nodes"],
        "cleanup": ["Retain rollback backup."],
        "reuse_policy": reuse_policy or {"default_decision": "direct_reuse", "allow_direct_reuse": True},
    }


def test_preflight_returns_direct_reuse_packet_for_safe_promoted_playbook():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_safe", confidence=0.91)

    result = experience_preflight(
        conn,
        query="Need one-way Headscale ACL so management can access target but target cannot reach others",
        accessible_scope_ids=["scope-a"],
        config={"experience": {"direct_reuse_min_confidence": 0.82, "packet_max_chars": 1800}},
    )

    assert result["decision"] == "direct_reuse"
    assert result["playbook"]["id"] == "pb_safe"
    assert "[read_only]" in result["packet"]
    assert "[local_write]" in result["packet"]
    assert "Required live checks" in result["packet"]
    assert result["requires_live_check"] is True
    assert result["run"] == {"recorded": False}
    assert conn.execute("SELECT COUNT(*) FROM experience_runs").fetchone()[0] == 0


def test_preflight_packet_and_summary_expose_stop_rules_and_evidence_anchors():
    conn = _conn()
    payload = _payload(
        reuse_policy={
            "default_decision": "guided_reuse",
            "allow_direct_reuse": False,
            "risk_level": "high",
            "must_stop_and_ask_joy": ["涉及发布或重启时必须停下问 Joy。"],
            "prohibited_auto_actions": ["不得自动 push、tag、release 或 restart。"],
            "source_evidence_anchor_count": 3,
            "source_evidence_anchor_kinds": ["user_statement", "test_command", "assistant_closure"],
        }
    )
    _create_promoted(conn, playbook_id="pb_policy_summary", payload=payload, confidence=0.91)

    result = experience_preflight(
        conn,
        query="Need one-way Headscale ACL so management can access target but target cannot reach others",
        accessible_scope_ids=["scope-a"],
        config={"experience": {"packet_max_chars": 2400}},
    )

    assert result["decision"] == "guided_reuse"
    assert "Must stop and ask Joy" in result["packet"]
    assert "不得自动 push" in result["packet"]
    assert "Evidence anchors" in result["packet"]
    assert result["summary"] == {
        "playbook_id": "pb_policy_summary",
        "title": "Headscale/Tailscale one-way management ACL",
        "decision": "guided_reuse",
        "confidence": 0.91,
        "risk_level": "high",
        "preconditions": ["Identify target node and management nodes from live output.", "Confirm rollback before applying ACL."],
        "verification": ["management reaches target", "target cannot reach unrelated nodes"],
        "must_stop_and_ask_joy": ["涉及发布或重启时必须停下问 Joy。"],
        "prohibited_auto_actions": ["不得自动 push、tag、release 或 restart。"],
        "source_evidence_anchor_count": 3,
        "source_evidence_anchor_kinds": ["user_statement", "test_command", "assistant_closure"],
        "reasons": ["policy_default_guided_reuse", "policy_disallows_direct_reuse"],
    }


def test_preflight_summary_handles_invalid_source_evidence_anchor_count():
    conn = _conn()
    payload = _payload(reuse_policy={"source_evidence_anchor_count": "not-a-number", "source_evidence_anchor_kinds": ["test_command"]})
    _create_promoted(conn, playbook_id="pb_bad_anchor_count", payload=payload, confidence=0.91)

    result = experience_preflight(
        conn,
        query="Need one-way Headscale ACL so management can access target but target cannot reach others",
        accessible_scope_ids=["scope-a"],
        config={"experience": {"packet_max_chars": 2400}},
    )

    assert result["decision"] in {"direct_reuse", "guided_reuse"}
    assert result["summary"]["source_evidence_anchor_count"] == 0
    assert result["summary"]["source_evidence_anchor_kinds"] == ["test_command"]


def test_preflight_can_record_reuse_run_with_pending_live_check_evidence():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_record", confidence=0.91)

    result = experience_preflight(
        conn,
        query="Need one-way Headscale ACL so management can access target but target cannot reach others",
        accessible_scope_ids=["scope-a"],
        config={"experience": {"direct_reuse_min_confidence": 0.82}},
        record_run=True,
        scope_id="scope-a",
    )

    assert result["run"]["recorded"] is True
    row = conn.execute("SELECT * FROM experience_runs WHERE id = ?", (result["run"]["run_id"],)).fetchone()
    assert row is not None
    assert row["playbook_id"] == "pb_record"
    assert row["decision"] == "direct_reuse"
    assert row["outcome"] == "unknown"
    assert "awaiting outcome feedback" in row["outcome_reason"]
    assert "pending_live_check" in row["preconditions_checked"]
    assert "not_started" in row["steps_completed"]
    assert "experience_preflight" in row["evidence"]
    assert not row["finished_at"]


def test_preflight_respects_experience_enabled_master_switch():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_disabled", confidence=0.95)

    result = experience_preflight(
        conn,
        query="Need one-way Headscale ACL so management can access target but target cannot reach others",
        accessible_scope_ids=["scope-a"],
        config={"experience": {"enabled": False, "prefetch_enabled": True}},
    )

    assert result["decision"] == "no_reuse"
    assert result["packet"] == ""
    assert "experience_disabled" in result["reasons"]


def test_preflight_downgrades_risky_or_stale_playbooks_to_guided_reuse():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_risky", payload=_payload(risky=True), confidence=0.93)

    result = experience_preflight(
        conn,
        query="Apply one-way Headscale ACL and reload service safely",
        accessible_scope_ids=["scope-a"],
        config={"experience": {"allow_risky_direct_reuse": False}},
    )

    assert result["decision"] == "guided_reuse"
    assert "capability_requires_review" in result["reasons"]
    assert "service_control" in result["packet"]


def test_preflight_is_quiet_for_short_or_unmatched_queries_and_respects_scope():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_hidden", confidence=0.95)

    short = experience_preflight(conn, query="hi", accessible_scope_ids=["scope-a"], config={})
    hidden = experience_preflight(conn, query="one-way headscale acl", accessible_scope_ids=["scope-b"], config={})

    assert short["decision"] == "no_reuse"
    assert short["packet"] == ""
    assert "low_signal" in short["reasons"]
    assert hidden["decision"] == "no_reuse"
    assert hidden["packet"] == ""
    assert "no_matching_playbook" in hidden["reasons"]


def test_preflight_treats_cjk_queries_as_meaningful_signal():
    conn = _conn()

    assert _query_is_low_signal("需要一键回滚并验证连通性", min_chars=8) is False
    assert _query_is_low_signal("你好", min_chars=8) is True

    result = experience_preflight(conn, query="需要一键回滚并验证连通性", accessible_scope_ids=["scope-a"], config={})

    assert result["decision"] == "no_reuse"
    assert "low_signal" not in result["reasons"]
    assert "no_matching_playbook" in result["reasons"]


def test_preflight_fails_closed_when_core_playbook_json_is_corrupt():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_corrupt", payload=_payload(risky=True), confidence=0.95)
    conn.execute("UPDATE procedural_playbooks SET steps = ? WHERE id = ?", ("{not-json", "pb_corrupt"))
    conn.commit()

    result = experience_preflight(
        conn,
        query="Apply one-way Headscale ACL and reload service safely",
        accessible_scope_ids=["scope-a"],
        config={"experience": {"allow_risky_direct_reuse": True}},
    )

    assert result["decision"] == "no_reuse"
    assert result["packet"] == ""
    assert "corrupt_playbook_payload" in result["reasons"]
    assert result["playbook"]["requires_operator_review"] is True
    assert "steps" in result["playbook"]["payload_corrupt_fields"]


def test_preflight_respects_playbook_reuse_policy_before_direct_reuse():
    conn = _conn()
    _create_promoted(
        conn,
        playbook_id="pb_policy_guided",
        payload=_payload(reuse_policy={"default_decision": "guided_reuse", "allow_direct_reuse": False}),
        confidence=0.95,
    )

    result = experience_preflight(
        conn,
        query="Need one-way Headscale ACL so management can access target",
        accessible_scope_ids=["scope-a"],
        config={"experience": {"direct_reuse_min_confidence": 0.82}},
    )

    assert result["decision"] == "guided_reuse"
    assert "policy_disallows_direct_reuse" in result["reasons"]
    assert "promoted_confident_match" not in result["reasons"]


def test_preflight_honors_no_reuse_policy_without_rendering_packet():
    conn = _conn()
    _create_promoted(
        conn,
        playbook_id="pb_policy_no_reuse",
        payload=_payload(reuse_policy={"default_decision": "no_reuse"}),
        confidence=0.95,
    )

    result = experience_preflight(
        conn,
        query="Need one-way Headscale ACL so management can access target",
        accessible_scope_ids=["scope-a"],
        config={},
    )

    assert result["decision"] == "no_reuse"
    assert result["packet"] == ""
    assert "policy_default_no_reuse" in result["reasons"]


def test_preflight_skips_blocked_top_candidate_and_uses_next_safe_match():
    conn = _conn()
    blocked_policy = {"default_decision": "no_reuse", "allow_direct_reuse": False}
    _create_promoted(conn, playbook_id="pb_blocked", payload=_payload(reuse_policy=blocked_policy), confidence=0.95)
    _create_promoted(conn, playbook_id="pb_safe_next", payload=_payload(), confidence=0.9)

    result = experience_preflight(
        conn,
        query="Need one-way Headscale ACL so management can access target but target cannot reach others",
        accessible_scope_ids=["scope-a"],
        config={"experience": {"direct_reuse_min_confidence": 0.82}},
    )

    assert result["decision"] == "direct_reuse"
    assert result["playbook"]["id"] == "pb_safe_next"
    assert result["packet"]
    assert any(skipped["id"] == "pb_blocked" and "policy_default_no_reuse" in skipped["reasons"] for skipped in result["skipped_candidates"])


def test_preflight_does_not_render_packet_for_needs_review_or_quarantined_playbooks():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_needs_review", confidence=0.94)
    record_playbook_feedback(
        conn,
        playbook_id="pb_needs_review",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        outcome="failed",
        evidence=["fixture failure"],
    )
    needs_review = experience_preflight(
        conn,
        query="Need one-way Headscale ACL so management can access target",
        accessible_scope_ids=["scope-a"],
        config={},
    )

    assert needs_review["decision"] == "no_reuse"
    assert needs_review["packet"] == ""
    assert "no_promoted_playbook" in needs_review["reasons"]

    _create_promoted(conn, playbook_id="pb_quarantined", confidence=0.94)
    review_playbook(conn, playbook_id="pb_quarantined", accessible_scope_ids=["scope-a"], action="quarantine", reason="bad fixture")
    quarantined = experience_preflight(
        conn,
        query="Need one-way Headscale ACL so management can access target",
        accessible_scope_ids=["scope-a"],
        config={},
    )

    assert quarantined["decision"] == "no_reuse"
    assert quarantined["packet"] == ""
    assert "no_promoted_playbook" in quarantined["reasons"]
