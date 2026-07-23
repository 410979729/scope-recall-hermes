"""Public temporal fact-query and maintenance-evolution tool contracts."""

from __future__ import annotations

import json

import pytest

from plugins.memory import load_memory_provider
from scope_recall.fact_repository import insert_claim


def _write_config(home, values: dict) -> None:
    path = home / "scope-recall" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values) + "\n", encoding="utf-8")


@pytest.fixture
def provider(tmp_path):
    _write_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "fact_evolution": {"enabled": False, "maintenance_mode": "preview"},
            "temporal_queries": {"enabled": False, "timezone": "UTC"},
            "maintenance_tools_enabled": False,
        },
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "temporal-tools-session",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    yield plugin
    plugin.shutdown()


def _proposal(*, value: str, key: str) -> dict:
    return {
        "action": "add",
        "claim": {
            "subject": "Joy",
            "predicate": "lives in",
            "value": value,
            "cardinality": "single",
            "valid_from": "2026-04-01T00:00:00+00:00",
        },
        "content": f"Joy currently lives in {value}.",
        "target": "user",
        "memory_type": "factual",
        "evidence": [
            {
                "source_type": "user_message",
                "source_id": f"message:{key}",
                "quote": f"Joy now lives in {value}; please keep this current.",
            }
        ],
        "confidence": 0.98,
        "reason": "direct user statement",
        "idempotency_key": key,
    }


def _count(provider, table: str) -> int:
    with provider._lock:
        return int(
            provider._require_conn()
            .execute(f"SELECT COUNT(*) FROM {table}")
            .fetchone()[0]
        )


def test_temporal_fact_dispatch_requires_feature_gate(provider):
    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_fact",
            {"action": "current", "subject": "Joy", "predicate": "lives in"},
        )
    )

    assert "temporal_queries.enabled" in payload["error"]


def test_temporal_fact_dispatch_reads_current_view_without_writes(provider):
    provider._config["fact_evolution"] = {
        "enabled": True,
        "maintenance_mode": "reviewed_apply",
    }
    provider._config["maintenance_tools_enabled"] = True
    applied = json.loads(
        provider.handle_tool_call(
            "scope_recall_evolve",
            {"proposal": _proposal(value="Mumbai", key="query-seed"), "dry_run": False},
        )
    )
    assert applied["applied"] is True
    before = {
        table: _count(provider, table)
        for table in ("memories", "fact_claims", "fact_action_receipts")
    }

    provider._config["temporal_queries"] = {"enabled": True, "timezone": "UTC"}
    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_fact",
            {"action": "current", "subject": "Joy", "predicate": "lives in"},
        )
    )

    assert payload["action"] == "current"
    assert payload["count"] == 1
    assert payload["facts"][0]["claim"]["value"] == "Mumbai"
    assert "message:query-seed" in {
        item["source_ref"] for item in payload["facts"][0]["evidence"]
    }
    assert {
        table: _count(provider, table)
        for table in ("memories", "fact_claims", "fact_action_receipts")
    } == before


def test_search_empty_results_exposes_top_level_temporal_diagnostics(provider):
    provider._config["temporal_queries"] = {
        "enabled": True,
        "timezone": "UTC",
        "current_limit": 50,
    }

    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_search",
            {"query": "alpha beta gamma", "limit": 5},
        )
    )

    assert payload["count"] == 0
    assert payload["results"] == []
    diagnostics = payload["temporal_candidate_diagnostics"]
    assert diagnostics["candidate_count"] == 0
    assert diagnostics["token_count"] == 3
    assert diagnostics["covered_token_count"] == 3
    assert diagnostics["token_coverage_complete"] is True
    assert diagnostics["complete"] is True
    assert "semantic_tokens" not in diagnostics
    assert "token_routes" not in diagnostics

    # A later non-temporal search must not leak diagnostics from the prior call.
    provider._config["temporal_queries"]["enabled"] = False
    second = json.loads(
        provider.handle_tool_call(
            "scope_recall_search",
            {"query": "second query", "limit": 5},
        )
    )
    assert "temporal_candidate_diagnostics" not in second


def test_temporal_fact_dispatch_never_returns_inaccessible_scope(provider):
    provider._config["fact_evolution"] = {
        "enabled": True,
        "maintenance_mode": "reviewed_apply",
    }
    provider._config["maintenance_tools_enabled"] = True
    accessible = json.loads(
        provider.handle_tool_call(
            "scope_recall_evolve",
            {"proposal": _proposal(value="Mumbai", key="scope-visible"), "dry_run": False},
        )
    )
    assert accessible["applied"] is True
    with provider._lock:
        conn = provider._require_conn()
        conn.execute(
            """
            INSERT INTO memories(
                id, scope_id, source, target, content, summary,
                created_at, updated_at, metadata
            ) VALUES (
                'memory-inaccessible', 'scope-inaccessible', 'fixture', 'user',
                'Joy lives in Tokyo.', 'Joy lives in Tokyo.',
                '2026-05-01T00:00:00+00:00', '2026-05-01T00:00:00+00:00', ?
            )
            """,
            (json.dumps({"lifecycle": "promoted", "memory_type": "factual"}),),
        )
        insert_claim(
            conn,
            claim_id="claim-inaccessible",
            memory_id="memory-inaccessible",
            scope_id="scope-inaccessible",
            subject="Joy",
            predicate="lives in",
            value="Tokyo",
            cardinality="single",
            assertion_kind="direct",
            valid_from="2026-05-01T00:00:00+00:00",
            recorded_at="2026-05-01T00:00:00+00:00",
            confidence=0.99,
            source_type="user_message",
            source_ref="message:scope-hidden",
        )
        conn.commit()

    provider._config["temporal_queries"] = {"enabled": True, "timezone": "UTC"}
    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_fact",
            {"action": "current", "subject": "Joy", "predicate": "lives in"},
        )
    )

    assert [item["claim"]["value"] for item in payload["facts"]] == ["Mumbai"]
    assert all(
        item["claim"]["scope_id"] != "scope-inaccessible"
        for item in payload["facts"]
    )


def test_evolve_dispatch_requires_maintenance_gate(provider):
    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_evolve",
            {"proposal": _proposal(value="Pune", key="maintenance-gate")},
        )
    )

    assert "maintenance_tools_enabled" in payload["error"]
    assert _count(provider, "memories") == 0


def test_evolve_rejects_non_boolean_dry_run(provider):
    provider._config["maintenance_tools_enabled"] = True
    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_evolve",
            {
                "proposal": _proposal(value="Pune", key="non-boolean-dry-run"),
                "dry_run": "false",
            },
        )
    )

    assert "dry_run must be a boolean" in payload["error"]
    assert _count(provider, "fact_action_receipts") == 0


def test_temporal_fact_dispatch_rejects_oversized_strings_before_query(provider):
    provider._config["temporal_queries"] = {"enabled": True, "timezone": "UTC"}
    cases = [
        (
            {"action": "current", "subject": "s" * 201, "predicate": "lives in"},
            "subject exceeds 200",
        ),
        (
            {"action": "current", "subject": "Joy", "predicate": "p" * 121},
            "predicate exceeds 120",
        ),
        (
            {
                "action": "as_of",
                "subject": "Joy",
                "predicate": "lives in",
                "at": "2" * 65,
            },
            "query instant exceeds 64",
        ),
        (
            {
                "action": "as_of",
                "subject": "Joy",
                "predicate": "lives in",
                "at": "2026-05-01T00:00:00+00:00",
                "known_at": "2" * 65,
            },
            "query instant exceeds 64",
        ),
    ]

    for args, expected in cases:
        payload = json.loads(provider.handle_tool_call("scope_recall_fact", args))
        assert expected in payload["error"]
    assert _count(provider, "fact_action_receipts") == 0


def test_temporal_fact_dispatch_rejects_malformed_limits(provider):
    provider._config["temporal_queries"] = {
        "enabled": True,
        "timezone": "UTC",
        "current_limit": 50,
    }
    for invalid in ("abc", "5", [], True, 1.5):
        payload = json.loads(
            provider.handle_tool_call(
                "scope_recall_fact",
                {
                    "action": "current",
                    "subject": "Joy",
                    "predicate": "lives in",
                    "limit": invalid,
                },
            )
        )
        assert payload["error"] == "limit must be an integer"

    for invalid in (0, -1, 101):
        payload = json.loads(
            provider.handle_tool_call(
                "scope_recall_fact",
                {
                    "action": "current",
                    "subject": "Joy",
                    "predicate": "lives in",
                    "limit": invalid,
                },
            )
        )
        assert payload["error"] == "limit must be between 1 and 100"

    for invalid in (False, 1.5, "50", "not-an-integer"):
        provider._config["temporal_queries"]["current_limit"] = invalid
        payload = json.loads(
            provider.handle_tool_call(
                "scope_recall_fact",
                {"action": "current", "subject": "Joy", "predicate": "lives in"},
            )
        )
        assert payload["error"] == (
            "temporal_queries.current_limit must be an integer"
        )

    for invalid in (0, 101):
        provider._config["temporal_queries"]["current_limit"] = invalid
        payload = json.loads(
            provider.handle_tool_call(
                "scope_recall_fact",
                {"action": "current", "subject": "Joy", "predicate": "lives in"},
            )
        )
        assert payload["error"] == (
            "temporal_queries.current_limit must be between 1 and 100"
        )


def test_temporal_fact_dispatch_supports_as_of_and_history(provider):
    provider._config["maintenance_tools_enabled"] = True
    provider._config["fact_evolution"] = {
        "enabled": True,
        "maintenance_mode": "reviewed_apply",
    }
    original = json.loads(
        provider.handle_tool_call(
            "scope_recall_evolve",
            {
                "proposal": _proposal(value="Mumbai", key="query-history-old"),
                "dry_run": False,
            },
        )
    )
    old_id = original["receipt"]["memory_ids"][-1]
    successor = _proposal(value="Bangalore", key="query-history-new")
    successor["action"] = "supersede"
    successor["target_ids"] = [old_id]
    successor["claim"]["valid_from"] = "2026-06-01T00:00:00+00:00"
    provider._config["fact_evolution"]["maintenance_mode"] = "reviewed_apply"
    changed = json.loads(
        provider.handle_tool_call(
            "scope_recall_evolve",
            {"proposal": successor, "dry_run": False},
        )
    )
    assert changed["applied"] is True
    before = {
        table: _count(provider, table)
        for table in ("memories", "fact_claims", "fact_action_receipts")
    }

    provider._config["temporal_queries"] = {"enabled": True, "timezone": "UTC"}
    as_of = json.loads(
        provider.handle_tool_call(
            "scope_recall_fact",
            {
                "action": "as_of",
                "subject": "Joy",
                "predicate": "lives in",
                "at": "2026-05-01T00:00:00+00:00",
            },
        )
    )
    history = json.loads(
        provider.handle_tool_call(
            "scope_recall_fact",
            {"action": "history", "subject": "Joy", "predicate": "lives in"},
        )
    )

    assert as_of["count"] == 1
    assert as_of["facts"][0]["claim"]["value"] == "Mumbai"
    assert [item["claim"]["value"] for item in history["facts"]] == [
        "Mumbai",
        "Bangalore",
    ]
    assert history["facts"][0]["transition"]["superseded_by_claim_id"]
    assert {
        table: _count(provider, table)
        for table in ("memories", "fact_claims", "fact_action_receipts")
    } == before


def test_evolve_defaults_to_dry_run_and_local_policy_still_controls_apply(provider):
    provider._config["maintenance_tools_enabled"] = True
    provider._config["fact_evolution"] = {
        "enabled": True,
        "maintenance_mode": "reviewed_apply",
    }
    preview = json.loads(
        provider.handle_tool_call(
            "scope_recall_evolve",
            {"proposal": _proposal(value="Pune", key="dry-run-default")},
        )
    )
    assert preview["applied"] is False
    assert preview["status"] == "preview"
    assert _count(provider, "memories") == 0

    provider._config["fact_evolution"] = {
        "enabled": False,
        "maintenance_mode": "reviewed_apply",
    }
    disabled = json.loads(
        provider.handle_tool_call(
            "scope_recall_evolve",
            {
                "proposal": _proposal(value="Pune", key="disabled-policy"),
                "dry_run": False,
            },
        )
    )
    assert disabled["applied"] is False
    assert disabled["status"] == "preview"
    assert _count(provider, "memories") == 0

    provider._config["fact_evolution"] = {
        "enabled": True,
        "maintenance_mode": "reviewed_apply",
    }
    applied = json.loads(
        provider.handle_tool_call(
            "scope_recall_evolve",
            {
                "proposal": _proposal(value="Pune", key="explicit-apply"),
                "dry_run": False,
            },
        )
    )
    assert applied["applied"] is True
    assert applied["status"] == "applied"
    assert applied["action"] == "add"
    assert _count(provider, "memories") == 1
    assert _count(provider, "fact_claims") == 1


def test_evolve_without_targets_ignores_caller_scope_fields(provider):
    provider._config["maintenance_tools_enabled"] = True
    provider._config["fact_evolution"] = {
        "enabled": True,
        "maintenance_mode": "reviewed_apply",
    }
    proposal = _proposal(value="Pune", key="runtime-bound-scope")
    proposal["scope_id"] = "scope-attacker-controlled"
    proposal["claim"]["scope_id"] = "scope-attacker-controlled"

    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_evolve",
            {"proposal": proposal, "dry_run": False},
        )
    )
    memory_id = payload["receipt"]["memory_ids"][-1]
    with provider._lock:
        row = provider._require_conn().execute(
            "SELECT scope_id FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()

    assert payload["applied"] is True
    assert str(row["scope_id"]) in set(provider._writable_scope_ids)
    assert str(row["scope_id"]) != "scope-attacker-controlled"


def test_evolve_rejects_oversized_target_list_before_lookup(provider):
    provider._config["maintenance_tools_enabled"] = True
    proposal = _proposal(value="Pune", key="too-many-targets")
    proposal["action"] = "retract"
    proposal.pop("claim")
    proposal.pop("content")
    proposal["target_ids"] = [f"memory-{index}" for index in range(33)]

    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_evolve",
            {"proposal": proposal, "dry_run": False},
        )
    )

    assert "exceeds 32" in payload["error"]
    assert _count(provider, "fact_action_receipts") == 0


def test_evolve_rejects_overlong_content_and_idempotency_before_hashing(provider):
    provider._config["maintenance_tools_enabled"] = True
    overlong_content = _proposal(value="Pune", key="bounded-content")
    overlong_content["content"] = "x" * 8001
    content_payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_evolve",
            {"proposal": overlong_content, "dry_run": False},
        )
    )

    overlong_key = _proposal(value="Pune", key="k" * 201)
    key_payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_evolve",
            {"proposal": overlong_key, "dry_run": False},
        )
    )

    assert "content exceeds 8000" in content_payload["error"]
    assert "idempotency_key exceeds 200" in key_payload["error"]
    assert _count(provider, "fact_action_receipts") == 0


def test_evolve_rejects_targets_spanning_writable_scopes(provider):
    provider._config["maintenance_tools_enabled"] = True
    shared = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {
                "content": "Shared factual fixture.",
                "target": "user",
                "memory_type": "factual",
            },
        )
    )
    local = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {
                "content": "Local factual fixture.",
                "target": "general",
                "memory_type": "factual",
            },
        )
    )
    proposal = _proposal(value="Pune", key="mixed-writable-scopes")
    proposal["action"] = "supersede"
    proposal["target_ids"] = [shared["id"], local["id"]]

    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_evolve",
            {"proposal": proposal, "dry_run": False},
        )
    )

    assert "share one writable scope" in payload["error"]
    assert _count(provider, "fact_action_receipts") == 0


def test_evolve_rejects_readable_target_removed_from_writable_scopes(provider):
    provider._config["maintenance_tools_enabled"] = True
    stored = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {
                "content": "Readable but temporarily non-writable fixture.",
                "target": "user",
                "memory_type": "factual",
            },
        )
    )
    with provider._lock:
        target_scope = str(
            provider._require_conn()
            .execute("SELECT scope_id FROM memories WHERE id = ?", (stored["id"],))
            .fetchone()[0]
        )
    original_writable = list(provider._writable_scope_ids)
    assert target_scope in original_writable
    provider._writable_scope_ids = [
        scope_id for scope_id in original_writable if scope_id != target_scope
    ]
    proposal = _proposal(value="Pune", key="readable-not-writable")
    proposal["action"] = "supersede"
    proposal["target_ids"] = [stored["id"]]
    try:
        payload = json.loads(
            provider.handle_tool_call(
                "scope_recall_evolve",
                {"proposal": proposal, "dry_run": False},
            )
        )
    finally:
        provider._writable_scope_ids = original_writable

    assert "not found in writable scopes" in payload["error"]
    assert _count(provider, "fact_action_receipts") == 0


def test_evolve_high_risk_action_requires_local_reviewed_apply(provider):
    provider._config["maintenance_tools_enabled"] = True
    provider._config["fact_evolution"] = {
        "enabled": True,
        "maintenance_mode": "reviewed_apply",
    }
    seeded = json.loads(
        provider.handle_tool_call(
            "scope_recall_evolve",
            {
                "proposal": _proposal(value="Mumbai", key="risk-seed"),
                "dry_run": False,
            },
        )
    )
    old_id = seeded["receipt"]["memory_ids"][-1]
    supersede = _proposal(value="Bangalore", key="risk-supersede")
    supersede["action"] = "supersede"
    supersede["target_ids"] = [old_id]
    supersede["claim"]["valid_from"] = "2026-06-01T00:00:00+00:00"
    # A direct caller may send unknown control keys, but they cannot elevate
    # the local execution mode.
    supersede["mode"] = "reviewed_apply"
    provider._config["fact_evolution"]["maintenance_mode"] = "preview"

    auto = json.loads(
        provider.handle_tool_call(
            "scope_recall_evolve",
            {"proposal": supersede, "dry_run": False},
        )
    )
    assert auto["applied"] is False
    assert auto["status"] == "preview"
    assert _count(provider, "memories") == 1

    provider._config["fact_evolution"]["maintenance_mode"] = "reviewed_apply"
    reviewed = json.loads(
        provider.handle_tool_call(
            "scope_recall_evolve",
            {"proposal": supersede, "dry_run": False},
        )
    )
    assert reviewed["applied"] is True
    assert reviewed["status"] == "applied"
    assert _count(provider, "memories") == 2
    assert _count(provider, "fact_claims") == 2
