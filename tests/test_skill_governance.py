from __future__ import annotations

import sqlite3

from scope_recall.experience_store import backfill_skill_anchors, create_playbook, review_playbook
from scope_recall.sql_store import ensure_schema


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _payload() -> dict:
    return {
        "schema_version": "procedural_playbook.v1",
        "task_class": "skill_governance_fixture",
        "title": "Skill governance fixture",
        "trigger": "Need reusable workflow while preserving Skill authority.",
        "goal": "Reuse playbook only when Skill governance gates pass.",
        "preconditions": [{"id": "p1", "check": "Read live evidence", "evidence_required": "evidence"}],
        "steps": [
            {
                "number": 1,
                "capability_class": "read_only",
                "action": "Inspect current source of truth before reuse.",
                "evidence_required": "live source checked",
            }
        ],
        "pitfalls": [],
        "verification": ["source truth checked"],
        "cleanup": [],
        "reuse_policy": {"default_decision": "direct_reuse", "allow_direct_reuse": True},
    }


def test_backfill_skill_anchors_repairs_existing_promoted_playbook_missing_anchors():
    conn = _conn()
    create_playbook(
        conn,
        playbook_id="pb_backfill_anchor",
        scope_id="scope-a",
        shared_scope_id="shared-a",
        payload=_payload(),
        status="candidate",
        confidence=0.9,
        related_skills=["debugging-and-quality-workflows"],
    )
    review_playbook(
        conn,
        playbook_id="pb_backfill_anchor",
        accessible_scope_ids=["scope-a", "shared-a"],
        action="promote",
        reason="fixture review",
    )
    conn.execute("DELETE FROM skill_anchors WHERE playbook_id = ?", ("pb_backfill_anchor",))
    conn.commit()

    result = backfill_skill_anchors(conn)

    assert result == {"checked": 1, "backfilled": 1}
    row = conn.execute("SELECT skill_name, reason FROM skill_anchors WHERE playbook_id = ?", ("pb_backfill_anchor",)).fetchone()
    assert row is not None
    assert row["skill_name"] == "debugging-and-quality-workflows"
    assert "startup backfill" in row["reason"]


def test_backfill_skill_anchors_is_idempotent_when_anchors_exist():
    conn = _conn()
    create_playbook(
        conn,
        playbook_id="pb_backfill_idempotent",
        scope_id="scope-a",
        shared_scope_id="shared-a",
        payload=_payload(),
        status="candidate",
        confidence=0.9,
        related_skills=["debugging-and-quality-workflows"],
    )
    review_playbook(
        conn,
        playbook_id="pb_backfill_idempotent",
        accessible_scope_ids=["scope-a", "shared-a"],
        action="promote",
        reason="fixture review",
    )

    result = backfill_skill_anchors(conn)

    assert result == {"checked": 1, "backfilled": 0}
    assert conn.execute("SELECT COUNT(*) FROM skill_anchors WHERE playbook_id = ?", ("pb_backfill_idempotent",)).fetchone()[0] == 1


def test_backfill_skill_anchors_preserves_manual_governance_anchors():
    conn = _conn()
    create_playbook(
        conn,
        playbook_id="pb_backfill_preserve_manual",
        scope_id="scope-a",
        shared_scope_id="shared-a",
        payload=_payload(),
        status="candidate",
        confidence=0.9,
        related_skills=["debugging-and-quality-workflows", "hermes-service-control-guardrails"],
    )
    review_playbook(
        conn,
        playbook_id="pb_backfill_preserve_manual",
        accessible_scope_ids=["scope-a", "shared-a"],
        action="promote",
        reason="fixture review",
    )
    conn.execute(
        """
        INSERT INTO skill_anchors(id, playbook_id, skill_name, load_policy, reason, created_at)
        VALUES ('manual-anchor', 'pb_backfill_preserve_manual', 'manual-operator-anchor', 'required', 'manual governance', '2026-01-01T00:00:00+00:00')
        """
    )
    conn.execute(
        "DELETE FROM skill_anchors WHERE playbook_id = ? AND skill_name = ?",
        ("pb_backfill_preserve_manual", "hermes-service-control-guardrails"),
    )
    conn.commit()

    result = backfill_skill_anchors(conn)

    assert result == {"checked": 1, "backfilled": 1}
    rows = conn.execute(
        "SELECT skill_name, load_policy, reason FROM skill_anchors WHERE playbook_id = ? ORDER BY skill_name",
        ("pb_backfill_preserve_manual",),
    ).fetchall()
    by_skill = {row["skill_name"]: row for row in rows}
    assert set(by_skill) == {
        "debugging-and-quality-workflows",
        "hermes-service-control-guardrails",
        "manual-operator-anchor",
    }
    assert by_skill["manual-operator-anchor"]["load_policy"] == "required"
    assert by_skill["manual-operator-anchor"]["reason"] == "manual governance"
