"""Tests for public Experience tool handlers exposed through Scope Recall.

They verify tool JSON contracts, policy checks, and lifecycle transitions rather than only storage internals."""

from __future__ import annotations

import json

import pytest

from plugins.memory import load_memory_provider
from scope_recall.experience_store import create_playbook, review_playbook
from scope_recall.models import RuntimeScope
from scope_recall.scope import build_shared_scope_id


def _write_scope_recall_config(hermes_home, values):
    config_path = hermes_home / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(values, ensure_ascii=False) + "\n", encoding="utf-8")


@pytest.fixture
def provider(tmp_path):
    _write_scope_recall_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "tool_schema_profile": "standard",
            "maintenance_tools_enabled": True,
            "experience": {"prefetch_enabled": False},
        },
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-experience-tools",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        chat_id="chat-a",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    yield plugin
    plugin.shutdown()


def _payload() -> dict:
    return {
        "schema_version": "procedural_playbook.v1",
        "task_class": "headscale_one_way_acl",
        "title": "Headscale one-way ACL",
        "trigger": "User asks for one-way management access.",
        "goal": "Apply one-way isolation safely.",
        "preconditions": [{"id": "p1", "check": "Read live nodes", "evidence_required": "node list"}],
        "steps": [
            {
                "number": 1,
                "capability_class": "read_only",
                "action": "Read policy and live nodes.",
                "evidence_required": "policy path and live nodes",
            }
        ],
        "pitfalls": [],
        "verification": ["connectivity checks complete"],
        "cleanup": [],
        "reuse_policy": {"default_decision": "direct_reuse", "allow_direct_reuse": True},
    }


def test_experience_tool_schemas_are_registered_in_primary_context(provider):
    names = {schema["name"] for schema in provider.get_tool_schemas()}

    assert "scope_recall_playbook_create" in names
    assert "scope_recall_playbook_search" in names
    assert "scope_recall_playbook_inspect" in names
    assert "scope_recall_experience_preflight" in names
    assert "scope_recall_playbook_feedback" in names
    assert "scope_recall_playbook_review" in names
    assert "scope_recall_experience_stats" in names
    assert "scope_recall_experience_promote" in names
    assert "scope_recall_forgetting_report" in names
    assert "scope_recall_forgetting_run" in names


def test_playbook_tool_flow_create_search_inspect_preflight_feedback(provider):
    created = json.loads(
        provider.handle_tool_call(
            "scope_recall_playbook_create",
            {"id": "pb_tool", "payload": _payload(), "status": "candidate", "confidence": 0.9},
        )
    )
    assert created["created"] is True
    assert created["playbook"]["id"] == "pb_tool"
    reviewed = json.loads(
        provider.handle_tool_call(
            "scope_recall_playbook_review",
            {"id": "pb_tool", "action": "promote", "reason": "fixture review", "dry_run": False},
        )
    )
    assert reviewed["reviewed"] is True

    found = json.loads(provider.handle_tool_call("scope_recall_playbook_search", {"query": "one-way management ACL", "limit": 5}))
    assert found["count"] == 1
    assert found["results"][0]["id"] == "pb_tool"

    inspected = json.loads(provider.handle_tool_call("scope_recall_playbook_inspect", {"id": "pb_tool"}))
    assert inspected["found"] is True
    assert inspected["playbook"]["steps"][0]["capability_class"] == "read_only"

    preflight = json.loads(provider.handle_tool_call("scope_recall_experience_preflight", {"query": "Need one-way headscale ACL"}))
    assert preflight["decision"] == "direct_reuse"
    assert "[read_only]" in preflight["packet"]

    feedback = json.loads(
        provider.handle_tool_call(
            "scope_recall_playbook_feedback",
            {
                "id": "pb_tool",
                "outcome": "success",
                "decision": "direct_reuse",
                "evidence": ["terminal raw: wrote /home/a/private/output.log and 355 passed"],
                "preconditions_checked": [{"id": "p1", "status": "passed", "evidence": "/home/a/private/nodes.txt"}],
                "steps_completed": [{"number": 1, "status": "done", "evidence": "/home/a/private/policy.hujson"}],
                "outcome_reason": "verified from /home/a/private/output.log",
                "model_name": "model at /home/a/private/model.bin",
            },
        )
    )
    assert feedback["recorded"] is True
    assert feedback["success_count"] == 1
    with provider._lock:
        run = provider._require_conn().execute("SELECT evidence, outcome_reason, model_name FROM experience_runs WHERE playbook_id = ?", ("pb_tool",)).fetchone()
    run_text = json.dumps(dict(run), ensure_ascii=False)
    assert "[REDACTED_PATH]" in run_text
    assert "/home/a/private" not in run_text

    stats = json.loads(provider.handle_tool_call("scope_recall_experience_stats", {}))
    assert stats["playbooks"]["total"] == 1
    assert stats["runs"]["total"] == 1


def test_playbook_feedback_updates_writable_owner_scope_for_auto_promoted_playbook(provider):
    with provider._lock:
        conn = provider._require_conn()
        create_playbook(
            conn,
            playbook_id="pb_auto_owned",
            scope_id=provider._scope_id,
            shared_scope_id=provider._shared_scope_id,
            payload=_payload(),
            status="candidate",
            confidence=0.9,
        )
        review_playbook(conn, playbook_id="pb_auto_owned", accessible_scope_ids=provider._accessible_scope_ids, action="promote", reason="fixture auto promotion")

    feedback = json.loads(
        provider.handle_tool_call(
            "scope_recall_playbook_feedback",
            {
                "id": "pb_auto_owned",
                "outcome": "failed",
                "decision": "guided_reuse",
                "evidence": ["live verification contradicted the playbook"],
            },
        )
    )

    assert feedback["recorded"] is True
    assert feedback["global_updated"] is True
    assert feedback["status"] == "needs_review"
    assert feedback["failure_count"] == 1
    with provider._lock:
        row = provider._require_conn().execute("SELECT status, failure_count FROM procedural_playbooks WHERE id = ?", ("pb_auto_owned",)).fetchone()
    assert row["status"] == "needs_review"
    assert row["failure_count"] == 1


def test_playbook_review_tool_can_dedupe_and_merge(provider):
    payload_a = _payload()
    payload_b = _payload()
    provider.handle_tool_call("scope_recall_playbook_create", {"id": "pb_tool_a", "payload": payload_a, "status": "candidate", "confidence": 0.7})
    provider.handle_tool_call("scope_recall_playbook_create", {"id": "pb_tool_b", "payload": payload_b, "status": "candidate", "confidence": 0.9})

    dedupe = json.loads(provider.handle_tool_call("scope_recall_playbook_review", {"action": "dedupe"}))
    assert dedupe["count"] == 1
    assert dedupe["groups"][0]["canonical_id"] == "pb_tool_b"

    dry = json.loads(provider.handle_tool_call("scope_recall_playbook_review", {"action": "merge", "id": "pb_tool_b", "source_ids": ["pb_tool_a"], "reason": "tool dedupe"}))
    assert dry["dry_run"] is True
    assert dry["merged"] is False

    applied = json.loads(
        provider.handle_tool_call(
            "scope_recall_playbook_review",
            {
                "action": "merge",
                "id": "pb_tool_b",
                "source_ids": ["pb_tool_a"],
                "reason": "tool dedupe",
                "dry_run": False,
                "validated_payload": dry,
            },
        )
    )
    assert applied["merged"] is True
    with provider._lock:
        row = provider._require_conn().execute("SELECT status, superseded_by FROM procedural_playbooks WHERE id = ?", ("pb_tool_a",)).fetchone()
    assert row["status"] == "superseded"
    assert row["superseded_by"] == "pb_tool_b"


def test_playbook_review_tool_derives_authenticated_legacy_owner_aliases(tmp_path):
    _write_scope_recall_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "tool_schema_profile": "standard",
            "maintenance_tools_enabled": True,
            "experience": {"prefetch_enabled": False},
            "identity": {
                "cross_platform_shared_scope": True,
                "user_aliases": {"telegram:9000000001": "joy"},  # fixture
            },
        },
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-experience-owner-alias",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="9000000001",  # fixture
        chat_id="9000000001",  # fixture
        gateway_session_key="agent:default:telegram:direct:9000000001",  # fixture
        agent_identity="default",
        agent_workspace="hermes",
    )
    try:
        legacy_scope = build_shared_scope_id(
            RuntimeScope(
                platform="telegram",
                user_id="9000000001",  # fixture
                agent_identity="default",
                agent_workspace="hermes",
            )
        )
        with plugin._lock:
            create_playbook(
                plugin._require_conn(),
                playbook_id="pb_legacy_core",
                scope_id=legacy_scope,
                payload=_payload(),
                status="candidate",
                confidence=0.9,
            )
            promoted = review_playbook(
                plugin._require_conn(),
                playbook_id="pb_legacy_core",
                accessible_scope_ids=[legacy_scope],
                action="promote",
                reason="fixture verified core",
            )
            assert promoted["reviewed"] is True
        created = json.loads(
            plugin.handle_tool_call(
                "scope_recall_playbook_create",
                {
                    "id": "pb_canonical_candidate",
                    "payload": _payload(),
                    "status": "candidate",
                    "confidence": 0.7,
                },
            )
        )
        assert created["created"] is True

        dedupe = json.loads(
            plugin.handle_tool_call(
                "scope_recall_playbook_review",
                {"action": "dedupe"},
            )
        )
        assert dedupe["count"] == 1
        assert {
            item["id"] for item in dedupe["groups"][0]["items"]
        } == {"pb_legacy_core", "pb_canonical_candidate"}

        applied = json.loads(
            plugin.handle_tool_call(
                "scope_recall_playbook_review",
                {
                    "id": "pb_canonical_candidate",
                    "action": "supersede",
                    "superseded_by": "pb_legacy_core",
                    "reason": "identity migration dedupe",
                    "dry_run": False,
                },
            )
        )

        assert applied["reviewed"] is True
        assert applied["status"] == "superseded"
        assert applied["superseded_by"] == "pb_legacy_core"
    finally:
        plugin.shutdown()


def test_playbook_review_tool_non_merge_actions_default_to_dry_run_and_require_apply(provider):
    provider.handle_tool_call("scope_recall_playbook_create", {"id": "pb_review_target", "payload": _payload(), "status": "candidate", "confidence": 0.9})
    provider.handle_tool_call("scope_recall_playbook_create", {"id": "pb_review_source", "payload": _payload(), "status": "candidate", "confidence": 0.7})

    dry_promote = json.loads(provider.handle_tool_call("scope_recall_playbook_review", {"id": "pb_review_target", "action": "promote", "reason": "operator inspected"}))

    assert dry_promote["dry_run"] is True
    assert dry_promote["changed"] is True
    assert dry_promote["reviewed"] is False
    with provider._lock:
        row = provider._require_conn().execute("SELECT status FROM procedural_playbooks WHERE id = ?", ("pb_review_target",)).fetchone()
        anchors = provider._require_conn().execute("SELECT COUNT(*) FROM skill_anchors WHERE playbook_id = ?", ("pb_review_target",)).fetchone()[0]
    assert row["status"] == "candidate"
    assert anchors == 0

    applied_promote = json.loads(
        provider.handle_tool_call(
            "scope_recall_playbook_review",
            {"id": "pb_review_target", "action": "promote", "reason": "operator inspected", "dry_run": False},
        )
    )
    assert applied_promote["dry_run"] is False
    assert applied_promote["reviewed"] is True
    with provider._lock:
        row = provider._require_conn().execute("SELECT status FROM procedural_playbooks WHERE id = ?", ("pb_review_target",)).fetchone()
    assert row["status"] == "promoted"

    dry_supersede = json.loads(
        provider.handle_tool_call(
            "scope_recall_playbook_review",
            {"id": "pb_review_source", "action": "supersede", "superseded_by": "pb_review_target", "reason": "duplicate"},
        )
    )
    assert dry_supersede["dry_run"] is True
    assert dry_supersede["changed"] is True
    with provider._lock:
        row = provider._require_conn().execute("SELECT status, superseded_by FROM procedural_playbooks WHERE id = ?", ("pb_review_source",)).fetchone()
    assert row["status"] == "candidate"
    assert row["superseded_by"] == ""

    applied_supersede = json.loads(
        provider.handle_tool_call(
            "scope_recall_playbook_review",
            {"id": "pb_review_source", "action": "supersede", "superseded_by": "pb_review_target", "reason": "duplicate", "dry_run": False},
        )
    )
    assert applied_supersede["reviewed"] is True
    assert applied_supersede["superseded_by"] == "pb_review_target"


def test_playbook_review_tool_threads_force_cross_class_to_supersede(provider):
    canonical = _payload()
    source = _payload()
    source["task_class"] = "unrelated_task_class"
    source["title"] = "Unrelated playbook"
    provider.handle_tool_call("scope_recall_playbook_create", {"id": "pb_force_target", "payload": canonical, "status": "candidate", "confidence": 0.9})
    provider.handle_tool_call("scope_recall_playbook_create", {"id": "pb_force_source", "payload": source, "status": "candidate", "confidence": 0.7})

    blocked = json.loads(
        provider.handle_tool_call(
            "scope_recall_playbook_review",
            {"id": "pb_force_source", "action": "supersede", "superseded_by": "pb_force_target", "reason": "operator cross-class check", "dry_run": False},
        )
    )
    assert blocked["error"] == "semantic_mismatch"

    forced = json.loads(
        provider.handle_tool_call(
            "scope_recall_playbook_review",
            {
                "id": "pb_force_source",
                "action": "supersede",
                "superseded_by": "pb_force_target",
                "reason": "operator cross-class check",
                "dry_run": False,
                "force_cross_class": True,
            },
        )
    )
    assert forced["reviewed"] is True
    assert forced["superseded_by"] == "pb_force_target"


def test_promoting_playbook_writes_skill_anchors(provider):
    created = json.loads(
        provider.handle_tool_call(
            "scope_recall_playbook_create",
            {
                "id": "pb_anchor",
                "payload": _payload(),
                "status": "candidate",
                "confidence": 0.9,
                "related_skills": ["debugging-and-quality-workflows", "hermes-service-control-guardrails"],
            },
        )
    )
    assert created["created"] is True

    reviewed = json.loads(
        provider.handle_tool_call(
            "scope_recall_playbook_review",
            {"id": "pb_anchor", "action": "promote", "reason": "fixture review", "dry_run": False},
        )
    )
    assert reviewed["reviewed"] is True

    with provider._lock:
        rows = provider._require_conn().execute(
            "SELECT skill_name, load_policy, reason FROM skill_anchors WHERE playbook_id = ? ORDER BY skill_name",
            ("pb_anchor",),
        ).fetchall()

    assert [row["skill_name"] for row in rows] == ["debugging-and-quality-workflows", "hermes-service-control-guardrails"]
    assert {row["load_policy"] for row in rows} == {"optional_reference"}
    assert all("fixture review" in row["reason"] for row in rows)


def test_preflight_blocks_playbook_with_open_skill_conflict(provider):
    provider.handle_tool_call(
        "scope_recall_playbook_create",
        {
            "id": "pb_conflict",
            "payload": _payload(),
            "status": "candidate",
            "confidence": 0.9,
            "related_skills": ["debugging-and-quality-workflows"],
        },
    )
    provider.handle_tool_call("scope_recall_playbook_review", {"id": "pb_conflict", "action": "promote", "reason": "fixture review", "dry_run": False})
    with provider._lock:
        provider._require_conn().execute(
            """
            INSERT INTO skill_conflicts(id, playbook_id, skill_name, conflicting_source, conflict_summary, status, created_at)
            VALUES ('sc_fixture', 'pb_conflict', 'debugging-and-quality-workflows', 'skill', 'Skill changed and contradicts this playbook.', 'open', '2026-01-01T00:00:00+00:00')
            """
        )
        provider._require_conn().commit()

    preflight = json.loads(provider.handle_tool_call("scope_recall_experience_preflight", {"query": "Need one-way headscale ACL"}))

    assert preflight["decision"] == "no_reuse"
    assert "open_skill_conflict" in preflight["reasons"]


def test_preflight_degrades_direct_reuse_when_promoted_playbook_has_related_skills_but_no_anchors(provider):
    provider.handle_tool_call(
        "scope_recall_playbook_create",
        {
            "id": "pb_missing_anchor",
            "payload": _payload(),
            "status": "candidate",
            "confidence": 0.95,
            "related_skills": ["debugging-and-quality-workflows"],
        },
    )
    provider.handle_tool_call("scope_recall_playbook_review", {"id": "pb_missing_anchor", "action": "promote", "reason": "fixture review", "dry_run": False})
    with provider._lock:
        provider._require_conn().execute("DELETE FROM skill_anchors WHERE playbook_id = ?", ("pb_missing_anchor",))
        provider._require_conn().commit()

    preflight = json.loads(provider.handle_tool_call("scope_recall_experience_preflight", {"query": "Need one-way headscale ACL"}))

    assert preflight["decision"] == "guided_reuse"
    assert "missing_skill_anchor" in preflight["reasons"]


def test_feedback_stale_opens_skill_conflict_for_related_skills(provider):
    provider.handle_tool_call(
        "scope_recall_playbook_create",
        {
            "id": "pb_feedback_conflict",
            "payload": _payload(),
            "status": "candidate",
            "confidence": 0.9,
            "related_skills": ["debugging-and-quality-workflows"],
        },
    )
    provider.handle_tool_call("scope_recall_playbook_review", {"id": "pb_feedback_conflict", "action": "promote", "reason": "fixture review", "dry_run": False})

    feedback = json.loads(
        provider.handle_tool_call(
            "scope_recall_playbook_feedback",
            {
                "id": "pb_feedback_conflict",
                "outcome": "stale",
                "decision": "guided_reuse",
                "outcome_reason": "Skill procedure changed; old steps need review.",
            },
        )
    )

    assert feedback["recorded"] is True
    assert feedback["status"] == "needs_review"
    assert feedback["skill_conflicts_opened"] == 1
    with provider._lock:
        row = provider._require_conn().execute(
            "SELECT skill_name, conflicting_source, status, conflict_summary FROM skill_conflicts WHERE playbook_id = ?",
            ("pb_feedback_conflict",),
        ).fetchone()
    assert row is not None
    assert row["skill_name"] == "debugging-and-quality-workflows"
    assert row["conflicting_source"] == "feedback"
    assert row["status"] == "open"
    assert "Skill procedure changed" in row["conflict_summary"]


def test_auto_experience_and_forgetting_tools_smoke(provider):
    promoted = json.loads(provider.handle_tool_call("scope_recall_experience_promote", {"dry_run": True, "limit_sessions": 2}))
    assert promoted["dry_run"] is True
    assert "handbooks_created" in promoted

    stored = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {"content": "Temporary assistant scratch row for forgetting smoke test.", "target": "general"},
        )
    )
    assert stored["stored"] is True

    report = json.loads(provider.handle_tool_call("scope_recall_forgetting_report", {"limit": 10}))
    assert "soft_archive_candidates" in report
    assert "hard_delete_candidates" in report

    dry = json.loads(provider.handle_tool_call("scope_recall_forgetting_run", {"dry_run": True, "limit": 10}))
    assert dry["dry_run"] is True
    assert "archived" in dry


def test_experience_prefetch_can_be_disabled_by_config(provider):
    provider.handle_tool_call(
        "scope_recall_playbook_create",
        {"id": "pb_tool", "payload": _payload(), "status": "candidate", "confidence": 0.9},
    )

    block = provider.prefetch("Need one-way headscale ACL")

    assert "Experience Kernel" not in block
    assert "pb_tool" not in block


def test_experience_prefetch_is_zero_write(provider):
    provider.handle_tool_call(
        "scope_recall_playbook_create",
        {"id": "pb_prefetch_run", "payload": _payload(), "status": "candidate", "confidence": 0.95},
    )
    provider.handle_tool_call("scope_recall_playbook_review", {"id": "pb_prefetch_run", "action": "promote", "reason": "fixture", "dry_run": False})
    provider._config["experience"]["prefetch_enabled"] = True
    with provider._lock:
        before = provider._require_conn().execute("SELECT COUNT(*) FROM experience_runs").fetchone()[0]

    block = provider.prefetch("Need one-way headscale ACL with live node verification")

    assert "Experience Kernel" in block
    assert "pb_prefetch_run" in block
    with provider._lock:
        after = provider._require_conn().execute("SELECT COUNT(*) FROM experience_runs").fetchone()[0]
    assert after == before


def test_playbook_create_tool_rejects_direct_promoted_status(provider):
    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_playbook_create",
            {"id": "pb_promoted", "payload": _payload(), "status": "promoted", "confidence": 0.9},
        )
    )

    assert payload["invalid_arguments"] is True
    assert payload["field"] == "status"
    assert payload["constraint"] == "enum"
    found = json.loads(provider.handle_tool_call("scope_recall_playbook_search", {"query": "one-way management ACL", "limit": 5}))
    assert found["count"] == 0


def test_playbook_create_tool_rejects_non_numeric_confidence(provider):
    result = provider.handle_tool_call(
        "scope_recall_playbook_create",
        {"id": "pb_bad_confidence", "payload": _payload(), "status": "candidate", "confidence": "not_a_number"},
    )

    assert "confidence must be numeric" in result.lower()
    found = json.loads(provider.handle_tool_call("scope_recall_playbook_search", {"query": "one-way management ACL", "limit": 5}))
    assert found["count"] == 0


def test_experience_enabled_false_disables_tool_preflight_and_prefetch(provider):
    created = json.loads(
        provider.handle_tool_call(
            "scope_recall_playbook_create",
            {"id": "pb_disabled", "payload": _payload(), "status": "candidate", "confidence": 0.95},
        )
    )
    assert created["created"] is True
    reviewed = json.loads(
        provider.handle_tool_call(
            "scope_recall_playbook_review",
            {"id": "pb_disabled", "action": "promote", "reason": "fixture", "dry_run": False},
        )
    )
    assert reviewed["reviewed"] is True
    provider._config["experience"]["enabled"] = False
    provider._config["experience"]["prefetch_enabled"] = True

    preflight = json.loads(provider.handle_tool_call("scope_recall_experience_preflight", {"query": "Need one-way headscale ACL"}))
    block = provider.prefetch("Need one-way headscale ACL")

    assert preflight["decision"] == "no_reuse"
    assert "experience_disabled" in preflight["reasons"]
    assert "Experience Kernel" not in block
    assert "pb_disabled" not in block


def test_experience_enabled_false_hides_and_blocks_non_preflight_experience_tools(provider):
    provider._config["experience"]["enabled"] = False

    names = {schema["name"] for schema in provider.get_tool_schemas()}
    assert not {name for name in names if name.startswith("scope_recall_playbook") or name.startswith("scope_recall_experience")}

    search = provider.handle_tool_call("scope_recall_playbook_search", {"query": "one-way management ACL"})
    create = provider.handle_tool_call("scope_recall_playbook_create", {"payload": _payload()})
    stats = provider.handle_tool_call("scope_recall_experience_stats", {})

    assert "experience kernel is disabled" in search.lower()
    assert "experience kernel is disabled" in create.lower()
    assert "experience kernel is disabled" in stats.lower()


def test_playbook_review_tool_rejects_stale_validated_payload(provider):
    provider.handle_tool_call(
        "scope_recall_playbook_create",
        {
            "id": "pb_stale_tool",
            "payload": _payload(),
            "status": "candidate",
            "confidence": 0.9,
        },
    )
    dry_run = json.loads(
        provider.handle_tool_call(
            "scope_recall_playbook_review",
            {
                "id": "pb_stale_tool",
                "action": "promote",
                "reason": "operator inspected",
            },
        )
    )
    assert dry_run["validation_token"]

    with provider._lock:
        provider._require_conn().execute(
            "UPDATE procedural_playbooks SET updated_at = ? WHERE id = ?",
            ("2099-01-01T00:00:00+00:00", "pb_stale_tool"),
        )
        provider._require_conn().commit()

    applied = json.loads(
        provider.handle_tool_call(
            "scope_recall_playbook_review",
            {
                "id": "pb_stale_tool",
                "action": "promote",
                "reason": "operator inspected",
                "dry_run": False,
                "validated_payload": dry_run,
            },
        )
    )
    with provider._lock:
        status = provider._require_conn().execute(
            "SELECT status FROM procedural_playbooks WHERE id = ?",
            ("pb_stale_tool",),
        ).fetchone()[0]

    assert applied["error"] == "stale_validation"
    assert status == "candidate"
