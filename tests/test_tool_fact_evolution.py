"""Public store/update compatibility and structured fact-evolution tool tests."""

from __future__ import annotations

import json

import pytest

from plugins.memory import load_memory_provider

from scope_recall.fact_repository import insert_claim
from scope_recall.fact_tooling import _scope_id_for_mode
from scope_recall.schemas import (
    SCOPE_RECALL_EVOLVE_SCHEMA,
    SCOPE_RECALL_MEMORY_SCHEMA,
    SCOPE_RECALL_STORE_SCHEMA,
    SCOPE_RECALL_UPDATE_SCHEMA,
)
from scope_recall.sql_store import store_row


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
            "fact_evolution": {
                "enabled": False,
                "tool_mode": "preview",
            },
        },
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "tool-fact-session",
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


def _fact_args(
    *,
    value: str,
    action: str = "add",
    target_ids: list[str] | None = None,
    idempotency_key: str,
    valid_from: str = "2026-04-01T00:00:00+00:00",
) -> dict:
    evolution = {
        "action": action,
        "evidence": [
            {
                "source_type": "user_message",
                "source_id": f"message:{idempotency_key}",
                "quote": f"Joy now lives in {value}; please keep this current.",
            }
        ],
        "confidence": 0.98,
        "reason": "direct user statement",
        "idempotency_key": idempotency_key,
    }
    if target_ids is not None:
        evolution["target_ids"] = target_ids
    return {
        "content": f"Joy currently lives in {value}.",
        "target": "user",
        "memory_type": "factual",
        "claim": {
            "subject": "Joy",
            "predicate": "lives in",
            "value": value,
            "cardinality": "single",
            "valid_from": valid_from,
        },
        "evolution": evolution,
    }


def _count(provider, table: str) -> int:
    with provider._lock:
        return int(provider._require_conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_store_update_and_compact_memory_schemas_add_optional_fact_envelopes_only():
    store = SCOPE_RECALL_STORE_SCHEMA["parameters"]
    update = SCOPE_RECALL_UPDATE_SCHEMA["parameters"]
    compact = SCOPE_RECALL_MEMORY_SCHEMA["parameters"]

    assert store["required"] == ["content"]
    assert update["required"] == ["id", "content"]
    assert compact["required"] == ["action"]
    for parameters in (store, update, compact):
        assert {"claim", "evolution"} <= set(parameters["properties"])
        evolution = parameters["properties"]["evolution"]
        assert set(evolution["properties"]["action"]["enum"]) == {
            "noop",
            "add",
            "enrich",
            "supersede",
            "retract",
            "review",
        }
        assert "mode" not in evolution["properties"]
        assert "policy_mode" not in evolution["properties"]
        assert "reviewed_apply" not in evolution["properties"]

    proposal = SCOPE_RECALL_EVOLVE_SCHEMA["parameters"]["properties"]["proposal"]
    assert proposal["properties"]["content"]["maxLength"] == 8000
    assert proposal["properties"]["target_ids"]["maxItems"] == 32
    assert proposal["properties"]["evidence"]["maxItems"] == 32
    assert proposal["properties"]["idempotency_key"]["maxLength"] == 200


def _seed_current_fact(
    provider,
    *,
    memory_id: str = "fact-memory-old",
    value: str = "Mumbai",
) -> str:
    timestamp = "2026-04-01T00:00:00+00:00"
    scope_mode = str(provider._scope_mode_for("user", "tool-store"))
    scope_id = _scope_id_for_mode(provider, scope_mode)
    with provider._lock:
        conn = provider._require_conn()
        scope = provider._scope
        stored_id, _summary, _updated_at, inserted = store_row(
            conn,
            memory_id=memory_id,
            scope_id=scope_id,
            platform=scope.platform,
            user_id=scope.user_id,
            chat_id=scope.chat_id,
            thread_id=scope.thread_id,
            gateway_session_key=scope.gateway_session_key,
            agent_identity=scope.agent_identity,
            agent_workspace=scope.agent_workspace,
            session_id="fact-owner-seed",
            source="tool-store",
            target="user",
            content=f"Asha lives in {value}.",
            metadata=json.dumps(
                {"lifecycle": "active", "memory_type": "factual"}
            ),
            allow_duplicate=True,
            commit=False,
            timestamp=timestamp,
        )
        assert inserted is True and stored_id == memory_id
        insert_claim(
            conn,
            claim_id=f"claim-{memory_id}",
            memory_id=memory_id,
            scope_id=scope_id,
            subject="Asha",
            predicate="lives in",
            value=value,
            cardinality="single",
            assertion_kind="direct",
            valid_from=timestamp,
            recorded_at=timestamp,
            confidence=0.98,
            source_type="user_message",
            source_ref=f"message:{memory_id}",
        )
        conn.commit()
    return memory_id


def _fact_state(provider, memory_id: str) -> tuple[str, str, str, int]:
    with provider._lock:
        conn = provider._require_conn()
        memory = conn.execute(
            "SELECT content FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        claim = conn.execute(
            "SELECT value, status FROM fact_claims WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()
        receipt_count = int(
            conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0]
        )
    return (
        str(memory["content"]) if memory else "",
        str(claim["value"]) if claim else "",
        str(claim["status"]) if claim else "",
        receipt_count,
    )


def test_legacy_update_fails_closed_for_fact_owned_memory(provider):
    memory_id = _seed_current_fact(provider)

    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_update",
            {
                "id": memory_id,
                "content": "Asha lives in Bangalore.",
                "target": "user",
            },
        )
    )

    assert payload["updated"] is False
    assert "structured fact evolution" in payload["error"].lower()
    assert _fact_state(provider, memory_id) == (
        "Asha lives in Mumbai.",
        "Mumbai",
        "current",
        0,
    )


def test_legacy_soft_archive_fails_closed_for_fact_owned_memory(provider):
    memory_id = _seed_current_fact(provider)

    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_forget",
            {"ids": [memory_id], "reason": "replace with corrected fact"},
        )
    )

    assert payload["archived"] == 0
    assert payload["blocked_fact_ids"] == [memory_id]
    assert "structured fact evolution" in payload["error"].lower()
    assert _fact_state(provider, memory_id) == (
        "Asha lives in Mumbai.",
        "Mumbai",
        "current",
        0,
    )


def test_legacy_merge_fails_closed_when_any_memory_owns_a_fact(provider):
    target_id = _seed_current_fact(provider)
    with provider._lock:
        conn = provider._require_conn()
        scope = provider._scope
        source_id, _summary, _updated_at, inserted = store_row(
            conn,
            memory_id="ordinary-source",
            scope_id=_scope_id_for_mode(
                provider, str(provider._scope_mode_for("user", "tool-store"))
            ),
            platform=scope.platform,
            user_id=scope.user_id,
            chat_id=scope.chat_id,
            thread_id=scope.thread_id,
            gateway_session_key=scope.gateway_session_key,
            agent_identity=scope.agent_identity,
            agent_workspace=scope.agent_workspace,
            session_id="merge-source",
            source="tool-store",
            target="user",
            content="Asha relocation note awaiting review.",
            metadata=json.dumps({"memory_type": "factual"}),
            allow_duplicate=True,
        )
        assert inserted is True and source_id == "ordinary-source"

    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_memory",
            {
                "action": "merge",
                "target_id": target_id,
                "source_ids": ["ordinary-source"],
                "content": "Asha lives in Bangalore.",
                "target": "user",
            },
        )
    )

    assert payload["merged"] is False
    assert payload["blocked_fact_ids"] == [target_id]
    assert "structured fact evolution" in payload["error"].lower()
    assert _count(provider, "memories") == 2
    assert _fact_state(provider, target_id) == (
        "Asha lives in Mumbai.",
        "Mumbai",
        "current",
        0,
    )


def test_legacy_hard_delete_fails_closed_for_fact_owned_memory(provider):
    memory_id = _seed_current_fact(provider)
    provider._config["maintenance_tools_enabled"] = True

    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_forget",
            {
                "ids": [memory_id],
                "reason": "operator cleanup",
                "hard_delete": True,
            },
        )
    )

    assert payload["deleted"] == 0
    assert payload["blocked_fact_ids"] == [memory_id]
    assert "structured fact evolution" in payload["error"].lower()
    assert _fact_state(provider, memory_id) == (
        "Asha lives in Mumbai.",
        "Mumbai",
        "current",
        0,
    )


def test_legacy_store_and_update_without_hints_remain_in_place(provider):
    stored = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {
                "content": "Project Vega release command is uv run release.",
                "target": "ops",
                "memory_type": "procedure",
            },
        )
    )
    assert stored["stored"] is True

    updated = json.loads(
        provider.handle_tool_call(
            "scope_recall_update",
            {
                "id": stored["id"],
                "content": "Project Vega release command is uv run release --prod.",
                "target": "ops",
            },
        )
    )

    assert updated["updated"] is True
    assert updated["id"] == stored["id"]
    with provider._lock:
        row = provider._require_conn().execute(
            "SELECT content FROM memories WHERE id = ?", (stored["id"],)
        ).fetchone()
    assert row["content"].endswith("--prod.")
    assert _count(provider, "fact_claims") == 0
    assert _count(provider, "fact_action_receipts") == 0


def test_structured_store_defaults_to_preview_and_does_not_claim_success(provider):
    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            _fact_args(value="Bangalore", idempotency_key="preview-add"),
        )
    )

    assert payload["stored"] is False
    assert payload["applied"] is False
    assert payload["evolution"]["status"] == "preview"
    assert payload["evolution"]["action"] == "review"
    assert payload["evolution"]["receipt"]["requested_action"] == "add"
    assert "runtime_evidence_unverified" in payload["evolution"]["receipt"][
        "reason_codes"
    ]
    assert _count(provider, "memories") == 0
    assert _count(provider, "fact_claims") == 0


def test_structured_store_auto_apply_stays_review_without_runtime_evidence(provider):
    provider._config["fact_evolution"] = {"enabled": True, "tool_mode": "auto_apply"}

    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            _fact_args(value="Mumbai", idempotency_key="tool-add-mumbai"),
        )
    )

    assert payload["stored"] is False
    assert payload["applied"] is False
    assert payload["id"] == ""
    assert payload["evolution"]["status"] == "review"
    assert payload["evolution"]["action"] == "review"
    assert payload["evolution"]["receipt"]["requested_action"] == "add"
    assert "runtime_evidence_unverified" in payload["evolution"]["receipt"][
        "reason_codes"
    ]
    assert _count(provider, "memories") == 0
    assert _count(provider, "fact_claims") == 0
    assert _count(provider, "fact_action_receipts") == 0


def test_tool_caller_cannot_auto_apply_forged_source_id_and_quote(provider):
    provider._config["fact_evolution"] = {"enabled": True, "tool_mode": "auto_apply"}
    args = _fact_args(value="Paris", idempotency_key="forged-tool-source")
    args["evolution"]["evidence"] = [
        {
            "source_type": "user_message",
            "source_id": "message:does-not-exist",
            "quote": "Joy now lives in Paris.",
        }
    ]

    payload = json.loads(provider.handle_tool_call("scope_recall_store", args))

    assert payload["stored"] is False
    assert payload["applied"] is False
    assert payload["evolution"]["status"] == "review"
    assert "runtime_evidence_unverified" in payload["evolution"]["receipt"][
        "reason_codes"
    ]
    assert _count(provider, "memories") == 0
    assert _count(provider, "fact_claims") == 0
    assert _count(provider, "fact_claim_evidence") == 0
    assert _count(provider, "fact_action_receipts") == 0


def test_non_fact_structured_hint_is_rejected_instead_of_silently_going_legacy(provider):
    args = _fact_args(value="Bangalore", idempotency_key="wrong-lane")
    args["memory_type"] = "workflow"

    payload = json.loads(provider.handle_tool_call("scope_recall_store", args))

    assert payload["error"]
    assert "factual memory_type" in payload["error"]
    assert _count(provider, "memories") == 0
    assert _count(provider, "fact_claims") == 0


def test_structured_update_stays_review_only_without_runtime_evidence(provider):
    provider._config["fact_evolution"] = {"enabled": True, "tool_mode": "auto_apply"}
    timestamp = "2026-04-01T00:00:00+00:00"
    scope_mode = str(provider._scope_mode_for("user", "tool-store"))
    scope_id = _scope_id_for_mode(provider, scope_mode)
    with provider._lock:
        conn = provider._require_conn()
        scope = provider._scope
        stored_id, _summary, updated_at, inserted = store_row(
            conn,
            memory_id="history-old",
            scope_id=scope_id,
            platform=scope.platform,
            user_id=scope.user_id,
            chat_id=scope.chat_id,
            thread_id=scope.thread_id,
            gateway_session_key=scope.gateway_session_key,
            agent_identity=scope.agent_identity,
            agent_workspace=scope.agent_workspace,
            session_id="tool-fact-session",
            source="tool-store",
            target="user",
            content="Joy currently lives in Mumbai.",
            metadata=json.dumps(
                {"lifecycle": "active", "memory_type": "factual"}
            ),
            allow_duplicate=True,
            commit=False,
            timestamp=timestamp,
        )
        assert inserted is True and stored_id == "history-old"
        insert_claim(
            conn,
            claim_id="claim-history-old",
            memory_id=stored_id,
            scope_id=scope_id,
            subject="Joy",
            predicate="lives in",
            value="Mumbai",
            valid_from=timestamp,
            recorded_at=timestamp,
            confidence=0.98,
            source_type="user_message",
            source_ref="message:history-old",
        )
        conn.commit()

    update_args = _fact_args(
        value="Bangalore",
        action="supersede",
        target_ids=[stored_id],
        idempotency_key="history-update",
        valid_from="2026-06-01T00:00:00+00:00",
    )
    update_args["id"] = stored_id
    payload = json.loads(provider.handle_tool_call("scope_recall_update", update_args))

    assert payload["updated"] is False
    assert payload["applied"] is False
    assert payload["successor_id"] == ""
    assert payload["evolution"]["status"] == "review"
    assert "runtime_evidence_unverified" in payload["evolution"]["receipt"][
        "reason_codes"
    ]
    assert _count(provider, "memories") == 1
    assert _count(provider, "fact_claims") == 1
    assert _count(provider, "fact_action_receipts") == 0
    with provider._lock:
        conn = provider._require_conn()
        row = conn.execute(
            "SELECT value, status FROM fact_claims WHERE claim_id = ?",
            ("claim-history-old",),
        ).fetchone()
        memory_updated_at = conn.execute(
            "SELECT updated_at FROM memories WHERE id = ?",
            (stored_id,),
        ).fetchone()[0]
    assert tuple(row) == ("Mumbai", "current")
    assert str(memory_updated_at) == updated_at


def test_maintenance_rejects_historical_durable_target_in_local_scope(provider):
    provider._config["maintenance_tools_enabled"] = True
    with provider._lock:
        conn = provider._require_conn()
        scope = provider._scope
        store_row(
            conn,
            memory_id="historical-local-user",
            scope_id=provider._scope_id,
            platform=scope.platform,
            user_id=scope.user_id,
            chat_id=scope.chat_id,
            thread_id=scope.thread_id,
            gateway_session_key=scope.gateway_session_key,
            agent_identity=scope.agent_identity,
            agent_workspace=scope.agent_workspace,
            session_id="legacy",
            source="manual",
            target="user",
            content="Joy lives in Mumbai.",
            timestamp="2026-01-01T00:00:00+00:00",
        )
        conn.commit()

    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_evolve",
            {
                "dry_run": True,
                "proposal": {
                    "action": "supersede",
                    "content": "Joy lives in Bangalore.",
                    "target": "user",
                    "memory_type": "factual",
                    "target_ids": ["historical-local-user"],
                    "claim": {
                        "subject": "Joy",
                        "predicate": "lives in",
                        "value": "Bangalore",
                        "cardinality": "single",
                        "valid_from": "2026-06-01T00:00:00+00:00",
                    },
                    "evidence": [
                        {
                            "source_type": "user_message",
                            "source_id": "message:scope-route",
                            "quote": "I now live in Bangalore.",
                        }
                    ],
                },
            },
        )
    )

    assert "canonical target scope" in payload["error"]
    assert _count(provider, "fact_action_receipts") == 0


def test_tool_request_cannot_raise_apply_mode_above_local_config(provider):
    args = _fact_args(value="Bangalore", idempotency_key="no-escalation")
    args["evolution"]["mode"] = "reviewed_apply"
    args["evolution"]["policy_mode"] = "reviewed_apply"

    payload = json.loads(provider.handle_tool_call("scope_recall_store", args))

    assert payload["stored"] is False
    assert payload["applied"] is False
    assert payload["evolution"]["status"] == "preview"
    assert _count(provider, "memories") == 0
