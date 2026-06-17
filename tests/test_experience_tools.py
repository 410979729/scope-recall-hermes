from __future__ import annotations

import json

import pytest

from plugins.memory import load_memory_provider


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
            {"id": "pb_tool", "action": "promote", "reason": "fixture review"},
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
            {"id": "pb_tool", "outcome": "success", "decision": "direct_reuse", "evidence": ["fixture checked"]},
        )
    )
    assert feedback["recorded"] is True
    assert feedback["success_count"] == 1

    stats = json.loads(provider.handle_tool_call("scope_recall_experience_stats", {}))
    assert stats["playbooks"]["total"] == 1
    assert stats["runs"]["total"] == 1


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


def test_experience_prefetch_is_disabled_by_default(provider):
    provider.handle_tool_call(
        "scope_recall_playbook_create",
        {"id": "pb_tool", "payload": _payload(), "status": "candidate", "confidence": 0.9},
    )

    block = provider.prefetch("Need one-way headscale ACL")

    assert "Experience Kernel" not in block
    assert "pb_tool" not in block


def test_playbook_create_tool_rejects_direct_promoted_status(provider):
    result = provider.handle_tool_call(
        "scope_recall_playbook_create",
        {"id": "pb_promoted", "payload": _payload(), "status": "promoted", "confidence": 0.9},
    )

    assert "promoted" in result
    assert "review" in result.lower()
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
            {"id": "pb_disabled", "action": "promote", "reason": "fixture"},
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
