from __future__ import annotations

import importlib.util
from pathlib import Path

from scope_recall.provider_schemas import build_config_schema, build_tool_schemas

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CHECK_RELEASE_PATH = PLUGIN_ROOT / "scripts" / "check.release.py"


def _names(schemas: list[dict]) -> list[str]:
    return [str(schema["name"]) for schema in schemas]


def _load_release_check_module():
    spec = importlib.util.spec_from_file_location("scope_recall_check_release_provider_schemas", CHECK_RELEASE_PATH)
    assert spec is not None
    assert spec.loader is not None
    release_check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release_check)
    return release_check


def test_provider_config_schema_contract_contains_existing_keys():
    schema = build_config_schema()
    keys = {item["key"] for item in schema}

    assert len(keys) >= 100
    assert {
        "auto_recall",
        "auto_capture",
        "capture_llm.enabled",
        "capture_raw_user",
        "capture_llm.model",
        "journal.max_entries_per_digest",
        "retrieval.mode",
        "retrieval.relation_rerank_enabled",
        "vector.enabled",
        "vector.backend",
        "vector.fallback_backend",
        "vector.embedder.provider",
        "vector.embedder.model",
        "vector.embedder.api_key_env",
        "maintenance_tools_enabled",
    } <= keys


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


def test_release_stable_tool_names_cover_schema_profiles():
    release_check = _load_release_check_module()
    stable_names = set(release_check.STABLE_TOOL_NAMES)
    compact_names = set(_names(build_tool_schemas({})))
    standard_names = set(_names(build_tool_schemas({"tool_schema_profile": "standard"})))
    maintenance_names = set(
        _names(
            build_tool_schemas(
                {
                    "tool_schema_profile": "standard",
                    "maintenance_tools_enabled": True,
                    "secret_index_tools_enabled": True,
                }
            )
        )
    )

    assert {"scope_recall_memory", "scope_recall_entity"} <= compact_names
    assert compact_names <= stable_names
    assert standard_names <= stable_names
    assert maintenance_names <= stable_names


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
