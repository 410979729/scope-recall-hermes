"""Recall Inspector contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugins.memory import load_memory_provider
from scope_recall.provider_schemas import build_tool_schemas

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def provider(tmp_path: Path):
    config_path = tmp_path / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"vector": {"enabled": False}}), encoding="utf-8"
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "inspector-session",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="inspector-chat",
    )
    yield plugin
    plugin.shutdown()


def _tool(provider, name: str, arguments: dict[str, object]) -> dict[str, object]:
    return json.loads(provider.handle_tool_call(name, arguments))


def test_inspector_uses_exact_production_packet_and_stays_zero_write(provider) -> None:
    stored = _tool(
        provider,
        "scope_recall_store",
        {
            "content": "Project Meridian uses the amber deployment checklist.",
            "target": "project",
        },
    )
    assert stored["stored"] is True

    connection = provider._require_conn()
    before = connection.total_changes
    payload = _tool(
        provider,
        "scope_recall_inspector",
        {"query": "Meridian amber deployment checklist", "limit": 5},
    )

    assert connection.total_changes == before
    assert payload["schema"] == "scope_recall.recall_inspector.v1"
    assert payload["read_only"] is True
    assert payload["include_content"] is False
    assert payload["result_count"] >= 1
    packet = provider.recall_service_view().last_recall_packet
    assert type(packet).__name__ == "RecallPacket"
    assert payload["packet"]["candidate_fingerprint"] == packet.candidate_fingerprint
    assert payload["packet"]["schema"] == packet.schema

    result = next(
        item for item in payload["results"] if item["id"] == stored["id"]
    )
    assert "content" not in result
    assert result["why_hit"]["why_included"]
    assert result["truth"]["classification"] in {
        "current",
        "historical",
        "untracked",
    }
    assert isinstance(result["truth"]["conflict"], bool)
    assert result["provenance"]["source"]
    assert isinstance(result["provenance"]["evidence_kinds"], list)
    assert result["token_cost"]["estimated_tokens"] >= 1
    assert "retrieval_score" in result["confidence"]
    assert "updated_at" in result["timeline"]
    assert result["action_plans"]["correction"]["arguments"] == {
        "action": "update",
        "id": stored["id"],
    }
    assert result["action_plans"]["archive"]["arguments"] == {
        "action": "forget",
        "id": stored["id"],
    }
    purge = result["action_plans"]["purge_impact"]
    assert purge["arguments"] == {"action": "plan", "id": stored["id"]}
    assert purge["requires_maintenance_tools"] is True
    assert "separate confirmations" in purge["effect"]


def test_inspector_content_and_text_output_are_explicit(provider) -> None:
    stored = _tool(
        provider,
        "scope_recall_store",
        {"content": "Orion rollback uses checkpoint Delta-7.", "target": "ops"},
    )
    payload = _tool(
        provider,
        "scope_recall_inspector",
        {
            "query": "Orion rollback checkpoint Delta-7",
            "include_content": True,
            "format": "text",
        },
    )

    result = next(
        item for item in payload["results"] if item["id"] == stored["id"]
    )
    assert result["content"] == "Orion rollback uses checkpoint Delta-7."
    assert "Scope Recall Inspector (read-only)" in payload["rendered_text"]
    assert len(payload["rendered_text"]) <= 12000


def test_inspector_is_developer_only_and_does_not_expand_default_profiles() -> None:
    def names(config: dict[str, object]) -> set[str]:
        return {str(schema["name"]) for schema in build_tool_schemas(config)}

    assert "scope_recall_inspector" not in names({})
    assert "scope_recall_inspector" not in names(
        {"tool_schema_profile": "compatibility"}
    )
    assert "scope_recall_inspector" in names({"tool_schema_profile": "developer"})


def test_inspector_source_has_no_private_storage_capability() -> None:
    source = (ROOT / "recall_inspector.py").read_text(encoding="utf-8").lower()

    for forbidden in (
        "import sqlite3",
        "_require_conn",
        "query_connection",
        "from memories",
        "join memories",
    ):
        assert forbidden not in source
    assert "search_memories" in source
    assert "last_recall_packet" in source
