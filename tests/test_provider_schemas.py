from __future__ import annotations

from scope_recall.provider_schemas import build_config_schema, build_tool_schemas


def _names(schemas: list[dict]) -> list[str]:
    return [str(schema["name"]) for schema in schemas]


def test_provider_config_schema_contract_contains_existing_keys():
    keys = [item["key"] for item in build_config_schema()]

    assert keys == [
        "auto_recall",
        "auto_capture",
        "capture_llm.enabled",
        "capture_raw_user",
        "capture_llm.model",
        "vector.enabled",
        "vector.backend",
        "vector.fallback_backend",
        "vector.embedder.provider",
        "vector.embedder.model",
        "maintenance_tools_enabled",
    ]


def test_compact_tool_schema_profile_is_default_and_secondary_context_is_disabled():
    assert _names(build_tool_schemas({})) == [
        "scope_recall_store",
        "scope_recall_search",
        "scope_recall_context",
        "scope_recall_profile",
        "scope_recall_memory",
        "scope_recall_entity",
    ]
    assert build_tool_schemas({}, agent_context="subagent") == []
    assert build_tool_schemas({"enable_tools": False}) == []


def test_standard_tool_schema_includes_experience_when_enabled():
    names = _names(build_tool_schemas({"tool_schema_profile": "standard", "experience": {"enabled": True}}))

    assert "scope_recall_probe" in names
    assert "scope_recall_related" in names
    assert "scope_recall_playbook_search" in names
    assert "scope_recall_experience_stats" in names
    assert "scope_recall_dedupe" not in names


def test_maintenance_secret_and_extra_tools_are_opt_in_without_duplicates():
    names = _names(
        build_tool_schemas(
            {
                "tool_schema_profile": "compact",
                "maintenance_tools_enabled": True,
                "secret_index_tools_enabled": True,
                "tool_schema_extra_tools": "scope_recall_benchmark, scope_recall_store_secret_index, missing_tool",
            }
        )
    )

    assert "scope_recall_dedupe" in names
    assert "scope_recall_forgetting_run" in names
    assert "scope_recall_store_secret_index" in names
    assert "scope_recall_benchmark" in names
    assert names.count("scope_recall_store_secret_index") == 1
