"""Tests for tool schema profiles, config schema, and dispatcher alignment.

They keep public tool surfaces compact, explicit, and callable."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from scope_recall.provider_schemas import build_config_schema, build_tool_schemas
from scope_recall.tool_profiles import (
    CORE_TOOL_SCHEMA_POLICY,
    core_schema_budget_gate,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CHECK_RELEASE_PATH = PLUGIN_ROOT / "scripts" / "check.release.py"


def _names(schemas: list[dict]) -> list[str]:
    return [str(schema["name"]) for schema in schemas]


def _load_release_check_module():
    spec = importlib.util.spec_from_file_location(
        "scope_recall_check_release_provider_schemas", CHECK_RELEASE_PATH
    )
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


def test_core_tool_schema_profile_is_default_and_secondary_context_is_disabled():
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


def test_core_tool_schema_budget_and_digest_are_release_gated():
    schemas = build_tool_schemas({"tool_schema_profile": "core"})

    result = core_schema_budget_gate(schemas)

    assert result["ok"] is True, result
    assert result["measured"] == {
        "tool_count": 6,
        "schema_chars": 9531,
        "estimated_schema_tokens": 2383,
        "canonical_schema_sha256": (
            "7380485b5ee769b60383e7f6eabb836dd1637553bbbf17883bb6e564def8f5d6"
        ),
    }
    assert CORE_TOOL_SCHEMA_POLICY["maximum_schema_chars"] == 9600
    assert CORE_TOOL_SCHEMA_POLICY["maximum_estimated_schema_tokens"] == 2400


def test_core_tool_schema_budget_refuses_unreviewed_digest_change():
    schemas = build_tool_schemas({"tool_schema_profile": "core"})
    mutated = [dict(schema) for schema in schemas]
    mutated[0] = {**mutated[0], "description": mutated[0]["description"] + " "}

    result = core_schema_budget_gate(mutated)

    assert result["ok"] is False
    assert "core canonical schema digest changed without policy update" in result[
        "failures"
    ]


def test_release_checker_executes_the_source_core_schema_budget_gate():
    release_check = _load_release_check_module()

    result = release_check.tool_schema_budget_check()

    assert result["ok"] is True, result
    assert result["measured"]["canonical_schema_sha256"] == (
        CORE_TOOL_SCHEMA_POLICY["canonical_schema_sha256"]
    )


def test_canonical_profiles_preserve_aliases_and_do_not_elevate_feature_gates():
    core = set(_names(build_tool_schemas({"tool_schema_profile": "core"})))
    compact = set(_names(build_tool_schemas({"tool_schema_profile": "compact"})))
    compatibility = set(
        _names(build_tool_schemas({"tool_schema_profile": "compatibility"}))
    )
    standard = set(_names(build_tool_schemas({"tool_schema_profile": "standard"})))
    developer = set(_names(build_tool_schemas({"tool_schema_profile": "developer"})))
    maintenance_closed = set(
        _names(build_tool_schemas({"tool_schema_profile": "maintenance"}))
    )
    extension_closed = set(
        _names(
            build_tool_schemas(
                {
                    "tool_schema_profile": "extension",
                    "experience": {"enabled": False},
                }
            )
        )
    )
    extension_open = set(
        _names(
            build_tool_schemas(
                {
                    "tool_schema_profile": "extension",
                    "experience": {"enabled": True},
                }
            )
        )
    )

    assert core == compact
    assert compatibility == standard
    assert maintenance_closed == core
    assert extension_closed == core
    assert {"scope_recall_probe", "scope_recall_stats"} <= developer
    assert "scope_recall_update" not in developer
    assert "scope_recall_purge" not in maintenance_closed
    assert "scope_recall_playbook_search" not in extension_closed
    assert "scope_recall_playbook_search" in extension_open


def test_standard_tool_schema_includes_experience_when_enabled():
    names = _names(
        build_tool_schemas(
            {"tool_schema_profile": "standard", "experience": {"enabled": True}}
        )
    )

    assert "scope_recall_probe" in names
    assert "scope_recall_related" in names
    assert "scope_recall_playbook_search" in names
    assert "scope_recall_experience_stats" in names
    assert "scope_recall_dedupe" not in names


def test_store_schema_requires_one_atomic_fact_or_cohesive_topic_per_call():
    schema = next(
        item
        for item in build_tool_schemas({"tool_schema_profile": "standard"})
        if item["name"] == "scope_recall_store"
    )

    assert "one atomic fact" in schema["description"].lower()
    assert (
        "separate calls"
        in schema["parameters"]["properties"]["content"]["description"].lower()
    )


def test_maintenance_schema_exposure_is_independent_from_experience():
    common_maintenance = {
        "scope_recall_dedupe",
        "scope_recall_govern",
        "scope_recall_repair",
        "scope_recall_hygiene",
        "scope_recall_forgetting_report",
        "scope_recall_forgetting_run",
        "scope_recall_purge",
    }
    experience_maintenance = {
        "scope_recall_playbook_create",
        "scope_recall_playbook_review",
        "scope_recall_experience_promote",
    }

    for experience_enabled in (False, True):
        for maintenance_enabled in (False, True):
            names = set(
                _names(
                    build_tool_schemas(
                        {
                            "tool_schema_profile": "standard",
                            "maintenance_tools_enabled": maintenance_enabled,
                            "experience": {"enabled": experience_enabled},
                        }
                    )
                )
            )
            assert common_maintenance.issubset(names) is maintenance_enabled
            assert experience_maintenance.issubset(names) is (
                experience_enabled and maintenance_enabled
            )


def test_release_stable_tool_names_cover_schema_profiles():
    release_check = _load_release_check_module()
    stable_names = set(release_check.STABLE_TOOL_NAMES)
    compact_names = set(_names(build_tool_schemas({})))
    standard_names = set(
        _names(build_tool_schemas({"tool_schema_profile": "standard"}))
    )
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


def test_fact_and_evolve_tools_align_schema_dispatch_and_release_contracts():
    release_check = _load_release_check_module()
    surfaces = release_check.provider_tool_schema_names_by_surface()
    dispatch_names = release_check.tool_dispatcher_names()
    stable_names = set(release_check.STABLE_TOOL_NAMES)

    for tool_name in ("scope_recall_fact", "scope_recall_evolve"):
        assert tool_name in surfaces["all_referenced"]
        assert tool_name in dispatch_names
        assert tool_name in stable_names


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


def test_temporal_fact_read_tool_is_feature_gated_in_both_profiles():
    assert "scope_recall_fact" not in _names(build_tool_schemas({}))

    compact = _names(build_tool_schemas({"temporal_queries": {"enabled": True}}))
    standard = _names(
        build_tool_schemas(
            {
                "tool_schema_profile": "standard",
                "temporal_queries": {"enabled": True},
            }
        )
    )

    assert compact.count("scope_recall_fact") == 1
    assert standard.count("scope_recall_fact") == 1
    assert "scope_recall_evolve" not in compact
    assert "scope_recall_evolve" not in standard


def test_evolve_tool_requires_maintenance_and_extra_tools_cannot_bypass_gates():
    bypass = _names(
        build_tool_schemas(
            {"tool_schema_extra_tools": ("scope_recall_fact,scope_recall_evolve")}
        )
    )
    maintenance = _names(build_tool_schemas({"maintenance_tools_enabled": True}))

    assert "scope_recall_fact" not in bypass
    assert "scope_recall_evolve" not in bypass
    assert maintenance.count("scope_recall_evolve") == 1
    assert "scope_recall_fact" not in maintenance
