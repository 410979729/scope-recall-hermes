from __future__ import annotations

import json
import sqlite3

import pytest

from scope_recall.experience_models import ExperienceValidationError
from scope_recall.experience_preflight import experience_preflight
from scope_recall.experience_store import (
    create_playbook,
    experience_stats,
    inspect_playbook,
    record_playbook_feedback,
    review_playbook,
    search_playbooks,
)
from scope_recall.sql_store import ensure_schema


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _create_promoted(conn: sqlite3.Connection, *, playbook_id: str, scope_id: str = "scope-a", shared_scope_id: str = "", confidence: float = 0.9) -> None:
    create_playbook(conn, playbook_id=playbook_id, scope_id=scope_id, shared_scope_id=shared_scope_id, payload=_payload(), status="candidate", confidence=confidence)
    review_playbook(conn, playbook_id=playbook_id, accessible_scope_ids=[scope_id, shared_scope_id], action="promote", reason="fixture")


def _payload(*, task_class: str = "headscale_one_way_acl", title: str = "Headscale one-way ACL") -> dict:
    return {
        "schema_version": "procedural_playbook.v1",
        "task_class": task_class,
        "title": title,
        "trigger": "User asks to let management machines access a target while blocking reverse access.",
        "goal": "Apply one-way access safely with live verification.",
        "preconditions": [
            {"id": "p1", "check": "Read live node list.", "evidence_required": "headscale/tailscale output"}
        ],
        "steps": [
            {
                "number": 1,
                "capability_class": "read_only",
                "action": "Read current ACL policy and live node list.",
                "evidence_required": "policy path plus live nodes",
            },
            {
                "number": 2,
                "capability_class": "local_write",
                "action": "Prepare the minimal ACL diff without applying it yet.",
                "evidence_required": "minimal diff",
            },
        ],
        "pitfalls": [
            {"signal": "tailscale status lists nodes", "mistake": "Assume listing equals reachability", "correction": "Check real connectivity."}
        ],
        "verification": ["policy validates", "positive path works", "negative path is blocked"],
        "cleanup": ["Record backup path and verification output."],
        "reuse_policy": {"default_decision": "guided_reuse"},
    }


def test_create_search_inspect_playbook_with_fts_and_scope_filtering():
    conn = _conn()
    created = create_playbook(
        conn,
        playbook_id="pb_acl",
        scope_id="scope-a",
        shared_scope_id="shared-a",
        payload=_payload(),
        status="candidate",
        confidence=0.61,
        created_from_episode_id="episode-1",
        metadata={"source": "unit-test"},
    )

    assert created["id"] == "pb_acl"
    assert created["status"] == "candidate"
    assert created["task_class"] == "headscale_one_way_acl"
    assert created["requires_operator_review"] is True

    visible = search_playbooks(conn, query="one-way ACL management access", accessible_scope_ids=["scope-a"], limit=5)
    hidden = search_playbooks(conn, query="one-way ACL management access", accessible_scope_ids=["scope-b"], limit=5)

    assert [item["id"] for item in visible] == ["pb_acl"]
    assert hidden == []
    assert visible[0]["match_source"] == "fts"
    assert visible[0]["steps"][0]["capability_class"] == "read_only"

    inspected = inspect_playbook(conn, playbook_id="pb_acl", accessible_scope_ids=["scope-a"])
    assert inspected["found"] is True
    assert inspected["playbook"]["title"] == "Headscale one-way ACL"
    assert inspected["versions"][0]["version"] == 1
    assert inspected["versions"][0]["change_type"] == "create"

    assert inspect_playbook(conn, playbook_id="pb_acl", accessible_scope_ids=["scope-b"])["found"] is False


def test_search_uses_fts_index_instead_of_labeling_python_scan_as_fts():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_fts")

    assert search_playbooks(conn, query="management access", accessible_scope_ids=["scope-a"], limit=5)[0]["match_source"] == "fts"
    conn.execute("DELETE FROM procedural_playbooks_fts WHERE playbook_id = ?", ("pb_fts",))
    conn.commit()

    assert search_playbooks(conn, query="management access", accessible_scope_ids=["scope-a"], limit=5) == []


def test_review_and_feedback_update_status_counts_and_stats():
    conn = _conn()
    create_playbook(conn, playbook_id="pb_acl", scope_id="scope-a", shared_scope_id="", payload=_payload(), confidence=0.9)

    reviewed = review_playbook(conn, playbook_id="pb_acl", accessible_scope_ids=["scope-a"], action="promote", reason="manual review passed")
    assert reviewed["reviewed"] is True
    assert reviewed["status"] == "promoted"
    assert reviewed["version"] == 2

    feedback = record_playbook_feedback(
        conn,
        playbook_id="pb_acl",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        outcome="success",
        decision="guided_reuse",
        evidence=["policy check passed"],
        outcome_reason="fixture success",
    )
    assert feedback["recorded"] is True
    assert feedback["success_count"] == 1
    assert feedback["failure_count"] == 0
    assert feedback["status"] == "promoted"
    assert feedback["confidence"] >= 0.82

    failed = record_playbook_feedback(
        conn,
        playbook_id="pb_acl",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        outcome="failed",
        decision="direct_reuse",
        evidence=["negative check failed"],
        outcome_reason="fixture failure",
    )
    assert failed["failure_count"] == 1
    assert failed["status"] == "needs_review"

    stats = experience_stats(conn, accessible_scope_ids=["scope-a"])
    assert stats["playbooks"]["total"] == 1
    assert stats["playbooks"]["by_status"]["needs_review"] == 1
    assert stats["runs"]["total"] == 2
    assert stats["runs"]["by_outcome"] == {"failed": 1, "success": 1}


def test_feedback_cannot_mutate_playbook_outside_accessible_scope():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_hidden")

    blocked = record_playbook_feedback(
        conn,
        playbook_id="pb_hidden",
        scope_id="scope-b",
        accessible_scope_ids=["scope-b"],
        outcome="failed",
        decision="guided_reuse",
        evidence=["should not be accepted"],
    )

    assert blocked == {"recorded": False, "id": "pb_hidden", "error": "not_found"}
    owner_view = inspect_playbook(conn, playbook_id="pb_hidden", accessible_scope_ids=["scope-a"])
    assert owner_view["playbook"]["status"] == "promoted"
    assert owner_view["playbook"]["failure_count"] == 0
    assert owner_view["runs"] == []


def test_shared_scope_feedback_records_private_run_without_mutating_global_playbook():
    conn = _conn()
    create_playbook(
        conn,
        playbook_id="pb_shared_feedback",
        scope_id="scope-owner",
        shared_scope_id="pool",
        payload=_payload(),
        status="candidate",
        confidence=0.9,
    )
    review_playbook(
        conn,
        playbook_id="pb_shared_feedback",
        accessible_scope_ids=["scope-owner", "pool"],
        action="promote",
        reason="fixture",
    )

    feedback = record_playbook_feedback(
        conn,
        playbook_id="pb_shared_feedback",
        scope_id="scope-consumer",
        accessible_scope_ids=["scope-consumer", "pool"],
        outcome="failed",
        decision="direct_reuse",
        evidence=["consumer environment failed"],
        outcome_reason="private failure",
    )

    owner_view = inspect_playbook(conn, playbook_id="pb_shared_feedback", accessible_scope_ids=["scope-owner"])
    consumer_view = inspect_playbook(conn, playbook_id="pb_shared_feedback", accessible_scope_ids=["scope-consumer", "pool"])

    assert feedback["recorded"] is True
    assert feedback["global_updated"] is False
    assert feedback["status"] == "promoted"
    assert feedback["failure_count"] == 0
    assert owner_view["playbook"]["status"] == "promoted"
    assert owner_view["playbook"]["confidence"] == 0.9
    assert owner_view["playbook"]["failure_count"] == 0
    assert owner_view["runs"] == []
    assert [run["outcome_reason"] for run in consumer_view["runs"]] == ["private failure"]


def test_feedback_rejects_terminal_status_playbooks_without_mutating_counts_or_runs():
    conn = _conn()
    for action, expected_status in [("quarantine", "quarantined"), ("supersede", "superseded")]:
        playbook_id = f"pb_{expected_status}"
        _create_promoted(conn, playbook_id=playbook_id)
        review_playbook(conn, playbook_id=playbook_id, accessible_scope_ids=["scope-a"], action=action, reason="terminal")

        feedback = record_playbook_feedback(
            conn,
            playbook_id=playbook_id,
            scope_id="scope-a",
            accessible_scope_ids=["scope-a"],
            outcome="failed",
            decision="guided_reuse",
            evidence=["must not mutate terminal status"],
        )
        inspected = inspect_playbook(conn, playbook_id=playbook_id, accessible_scope_ids=["scope-a"])

        assert feedback == {"recorded": False, "id": playbook_id, "error": "terminal_status", "status": expected_status}
        assert inspected["playbook"]["status"] == expected_status
        assert inspected["playbook"]["failure_count"] == 0
        assert inspected["runs"] == []


def test_create_playbook_rejects_direct_promoted_status():
    conn = _conn()

    with pytest.raises(ExperienceValidationError):
        create_playbook(conn, playbook_id="pb_promoted", scope_id="scope-a", shared_scope_id="", payload=_payload(), status="promoted", confidence=0.9)

    assert conn.execute("SELECT COUNT(*) FROM procedural_playbooks WHERE id = ?", ("pb_promoted",)).fetchone()[0] == 0


def test_create_playbook_rejects_secret_like_content_and_packet_redacts_legacy_secret_rows():
    conn = _conn()
    secret_payload = _payload()
    secret_payload["steps"][0]["action"] = "Use api_key=not_a_real_key_12345 while editing policy."

    with pytest.raises(ExperienceValidationError):
        create_playbook(conn, playbook_id="pb_secret", scope_id="scope-a", shared_scope_id="", payload=secret_payload)


    create_playbook(conn, playbook_id="pb_legacy", scope_id="scope-a", shared_scope_id="", payload=_payload(), status="candidate", confidence=0.95)
    review_playbook(conn, playbook_id="pb_legacy", accessible_scope_ids=["scope-a"], action="promote", reason="fixture")
    legacy_steps = '[{"number": 1, "capability_class": "read_only", "action": "Read policy with token=legacy_token_example_12345", "evidence_required": "policy"}]'
    conn.execute("UPDATE procedural_playbooks SET steps = ? WHERE id = ?", (legacy_steps, "pb_legacy"))
    conn.execute("DELETE FROM procedural_playbooks_fts WHERE playbook_id = ?", ("pb_legacy",))
    conn.execute(
        "INSERT INTO procedural_playbooks_fts(playbook_id, title, trigger, goal, preconditions, steps, pitfalls, verification) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("pb_legacy", "Headscale one-way ACL", "User asks one-way management", "Apply safely", "[]", legacy_steps, "[]", "policy validates"),
    )
    conn.commit()

    result = experience_preflight(conn, query="one-way management policy", accessible_scope_ids=["scope-a"], config={})
    serialized_result = json.dumps(result, ensure_ascii=False)

    assert result["packet"]
    assert "***" not in result["packet"]
    assert "token=" not in result["packet"].lower()
    assert "[REDACTED_SECRET]" in result["packet"]
    assert "legacy_token_example_12345" not in serialized_result
    assert "token=" not in serialized_result.lower()


def test_feedback_rejects_secret_like_evidence_before_persisting():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_feedback_secret")

    with pytest.raises(ExperienceValidationError):
        record_playbook_feedback(
            conn,
            playbook_id="pb_feedback_secret",
            scope_id="scope-a",
            accessible_scope_ids=["scope-a"],
            outcome="success",
            evidence=["api_key=not_a_real_key_12345"],
            outcome_reason="safe",
        )

    assert conn.execute("SELECT COUNT(*) FROM experience_runs WHERE playbook_id = ?", ("pb_feedback_secret",)).fetchone()[0] == 0


def test_create_rejects_secret_like_playbook_id_and_created_from_episode_id():
    conn = _conn()

    with pytest.raises(ExperienceValidationError):
        create_playbook(
            conn,
            playbook_id="token=not_a_real_key_12345",
            scope_id="scope-a",
            shared_scope_id="",
            payload=_payload(),
        )
    with pytest.raises(ExperienceValidationError):
        create_playbook(
            conn,
            playbook_id="pb_safe",
            scope_id="scope-a",
            shared_scope_id="",
            payload=_payload(),
            created_from_episode_id="api_key=not_a_real_key_12345",
        )

    assert conn.execute("SELECT COUNT(*) FROM procedural_playbooks").fetchone()[0] == 0


def test_playbook_lookup_paths_do_not_echo_secret_like_playbook_id():
    conn = _conn()
    secret_id = "token=legacy_token_example_12345"

    inspected = inspect_playbook(conn, playbook_id=secret_id, accessible_scope_ids=["scope-a"])
    serialized_inspect = json.dumps(inspected, ensure_ascii=False)

    assert "legacy_token_example_12345" not in serialized_inspect
    assert "token=" not in serialized_inspect.lower()
    assert "[REDACTED_SECRET]" in serialized_inspect
    with pytest.raises(ExperienceValidationError):
        review_playbook(conn, playbook_id=secret_id, accessible_scope_ids=["scope-a"], action="promote", reason="safe")
    with pytest.raises(ExperienceValidationError):
        record_playbook_feedback(
            conn,
            playbook_id=secret_id,
            scope_id="scope-a",
            accessible_scope_ids=["scope-a"],
            outcome="success",
            evidence=["safe"],
        )


def test_playbook_secret_like_mapping_keys_are_rejected_and_legacy_keys_are_redacted():
    conn = _conn()
    secret_key_payload = _payload()
    secret_key_payload["preconditions"][0]["token=not_a_real_key_12345"] = "do not persist key"

    with pytest.raises(ExperienceValidationError):
        create_playbook(conn, playbook_id="pb_secret_key", scope_id="scope-a", shared_scope_id="", payload=secret_key_payload)

    create_playbook(conn, playbook_id="pb_legacy_key", scope_id="scope-a", shared_scope_id="", payload=_payload(), status="candidate", confidence=0.95)
    review_playbook(conn, playbook_id="pb_legacy_key", accessible_scope_ids=["scope-a"], action="promote", reason="fixture")
    legacy_preconditions = '[{"token=legacy_token_example_12345": "legacy", "check": "Read live node list", "evidence_required": "node list"}]'
    conn.execute("UPDATE procedural_playbooks SET preconditions = ?, metadata = ? WHERE id = ?", (legacy_preconditions, '{"api_key=legacy_key_name_12345":"legacy"}', "pb_legacy_key"))
    conn.execute("DELETE FROM procedural_playbooks_fts WHERE playbook_id = ?", ("pb_legacy_key",))
    conn.execute(
        "INSERT INTO procedural_playbooks_fts(playbook_id, title, trigger, goal, preconditions, steps, pitfalls, verification) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("pb_legacy_key", "Headscale one-way ACL", "User asks one-way management", "Apply safely", legacy_preconditions, "[]", "[]", "policy validates"),
    )
    conn.commit()

    search_payload = search_playbooks(conn, query="one-way management policy", accessible_scope_ids=["scope-a"], limit=5)
    inspect_payload = inspect_playbook(conn, playbook_id="pb_legacy_key", accessible_scope_ids=["scope-a"])
    preflight_payload = experience_preflight(conn, query="one-way management policy", accessible_scope_ids=["scope-a"], config={})
    serialized = json.dumps({"search": search_payload, "inspect": inspect_payload, "preflight": preflight_payload}, ensure_ascii=False)

    assert "legacy_token_example_12345" not in serialized
    assert "legacy_key_name_12345" not in serialized
    assert "token=" not in serialized.lower()
    assert "api_key=" not in serialized.lower()
    assert "[REDACTED_SECRET]" in serialized


def test_inspect_redacts_legacy_review_change_reason():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_review_secret")
    conn.execute(
        "UPDATE playbook_versions SET change_reason = ? WHERE playbook_id = ? AND change_type = ?",
        ("token=legacy_token_example_12345", "pb_review_secret", "promoted"),
    )
    conn.commit()

    inspected = inspect_playbook(conn, playbook_id="pb_review_secret", accessible_scope_ids=["scope-a"])
    serialized = json.dumps(inspected, ensure_ascii=False)

    assert "legacy_token_example_12345" not in serialized
    assert "token=" not in serialized.lower()
    assert "[REDACTED_SECRET]" in serialized


def test_feedback_rejects_secret_like_decision_and_inspect_redacts_legacy_decision():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_decision_secret")

    with pytest.raises(ExperienceValidationError):
        record_playbook_feedback(
            conn,
            playbook_id="pb_decision_secret",
            scope_id="scope-a",
            accessible_scope_ids=["scope-a"],
            outcome="success",
            decision="token=not_a_real_key_12345",
            evidence=["safe"],
        )
    assert conn.execute("SELECT COUNT(*) FROM experience_runs WHERE playbook_id = ?", ("pb_decision_secret",)).fetchone()[0] == 0

    conn.execute(
        """
        INSERT INTO experience_runs(
            id, playbook_id, scope_id, decision, confidence_at_use, evidence, outcome,
            outcome_reason, model_name, tool_call_count, token_estimate, started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "xrun_legacy_decision",
            "pb_decision_secret",
            "scope-a",
            "token=legacy_token_example_12345",
            0.9,
            "[]",
            "success",
            "safe",
            "model",
            1,
            10,
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    conn.commit()

    inspected = inspect_playbook(conn, playbook_id="pb_decision_secret", accessible_scope_ids=["scope-a"])
    serialized = json.dumps(inspected, ensure_ascii=False)

    assert "legacy_token_example_12345" not in serialized
    assert "token=" not in serialized.lower()
    assert "[REDACTED_SECRET]" in serialized


def test_inspect_and_stats_redact_legacy_secret_like_run_outcome():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_outcome_secret")
    conn.execute(
        """
        INSERT INTO experience_runs(
            id, playbook_id, scope_id, decision, confidence_at_use, evidence, outcome,
            outcome_reason, model_name, tool_call_count, token_estimate, started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "xrun_legacy_outcome",
            "pb_outcome_secret",
            "scope-a",
            "guided_reuse",
            0.9,
            "[]",
            "token=legacy_token_example_12345",
            "safe",
            "model",
            1,
            10,
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    conn.execute("UPDATE procedural_playbooks SET status = ? WHERE id = ?", ("token=legacy_status_example_12345", "pb_outcome_secret"))
    conn.commit()

    inspected = inspect_playbook(conn, playbook_id="pb_outcome_secret", accessible_scope_ids=["scope-a"])
    stats = experience_stats(conn, accessible_scope_ids=["scope-a"])
    serialized = json.dumps({"inspect": inspected, "stats": stats}, ensure_ascii=False)

    assert "legacy_token_example_12345" not in serialized
    assert "legacy_status_example_12345" not in serialized
    assert "token=" not in serialized.lower()
    assert "[REDACTED_SECRET]" in serialized


def test_shared_playbook_inspect_and_stats_filter_runs_by_run_scope():
    conn = _conn()
    create_playbook(conn, playbook_id="pb_shared", scope_id="scope-owner", shared_scope_id="pool", payload=_payload(), status="candidate", confidence=0.9)
    review_playbook(conn, playbook_id="pb_shared", accessible_scope_ids=["scope-owner", "pool"], action="promote", reason="fixture")
    record_playbook_feedback(
        conn,
        playbook_id="pb_shared",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a", "pool"],
        outcome="success",
        outcome_reason="private-A",
    )
    record_playbook_feedback(
        conn,
        playbook_id="pb_shared",
        scope_id="scope-b",
        accessible_scope_ids=["scope-b", "pool"],
        outcome="failed",
        outcome_reason="private-B",
    )

    a_view = inspect_playbook(conn, playbook_id="pb_shared", accessible_scope_ids=["scope-a", "pool"])
    a_stats = experience_stats(conn, accessible_scope_ids=["scope-a", "pool"])

    assert a_view["found"] is True
    assert [run["outcome_reason"] for run in a_view["runs"]] == ["private-A"]
    assert a_stats["runs"]["total"] == 1
    assert a_stats["runs"]["by_outcome"] == {"success": 1}
