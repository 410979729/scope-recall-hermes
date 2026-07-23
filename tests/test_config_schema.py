"""Tests that configuration registry, provider schema, and docs describe the same settings.

They prevent operator-facing config drift as defaults and nested keys evolve."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _leaf_keys(value, prefix=""):
    if isinstance(value, dict):
        keys = []
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            keys.extend(_leaf_keys(child, child_prefix))
        return keys
    return [prefix]


def _leaf_values(value, prefix=""):
    if isinstance(value, dict):
        values = {}
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            values.update(_leaf_values(child, child_prefix))
        return values
    return {prefix: value}


def test_packaged_config_covers_every_code_default_leaf_with_matching_type():
    from scope_recall.config import DEFAULT_CONFIG

    packaged = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    defaults = _leaf_values(DEFAULT_CONFIG)
    packaged_values = _leaf_values(packaged)

    assert set(defaults) <= set(packaged_values)
    mismatched_types = {
        key: (type(defaults[key]).__name__, type(packaged_values[key]).__name__)
        for key in defaults
        if key in packaged_values and type(defaults[key]) is not type(packaged_values[key])
    }
    assert mismatched_types == {}


def test_sensitive_hard_delete_is_fail_closed_by_default():
    from scope_recall.config import DEFAULT_CONFIG

    packaged = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

    assert DEFAULT_CONFIG["forgetting"]["hard_delete_sensitive"] is False
    assert packaged["forgetting"]["hard_delete_sensitive"] is False


def test_config_registry_covers_packaged_config_leaf_keys():
    from scope_recall.config_schema import build_config_registry

    packaged = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    leaf_keys = set(_leaf_keys(packaged))
    registry = build_config_registry()
    registry_keys = {entry["key"] for entry in registry}

    assert leaf_keys <= registry_keys
    assert len(registry_keys) >= 100
    for entry in registry:
        assert entry["key"]
        assert entry["type"]
        assert "default" in entry
        assert entry["description"]
        assert entry["risk"] in {"low", "medium", "high"}
        assert isinstance(entry["restart_required"], bool)


def test_provider_config_schema_uses_registry_for_deep_keys():
    from scope_recall.provider_schemas import build_config_schema

    schema = build_config_schema()
    by_key = {entry["key"]: entry for entry in schema}

    assert "journal.max_entries_per_digest" in by_key
    assert "retrieval.relation_rerank_enabled" in by_key
    assert "vector.embedder.api_key_env" in by_key
    assert "vector.embedder.connection_retry_delays" in by_key
    assert by_key["vector.embedder.api_key_env"]["risk"] == "high"
    assert by_key["vector.embedder.connection_retry_delays"]["default"] == [
        2.0,
        4.0,
        8.0,
    ]
    assert by_key["journal.max_entries_per_digest"]["type"] == "integer"


def test_fact_evolution_registry_exposes_persistence_risk_and_reload_semantics():
    from scope_recall.config_schema import build_config_registry

    by_key = {entry["key"]: entry for entry in build_config_registry()}

    assert by_key["fact_evolution.enabled"]["risk"] == "high"
    assert by_key["fact_evolution.enabled"]["restart_required"] is True

    for key in (
        "fact_evolution.mode",
        "fact_evolution.journal_mode",
        "fact_evolution.nightly_mode",
        "fact_evolution.tool_mode",
        "fact_evolution.maintenance_mode",
    ):
        assert by_key[key]["risk"] == "high"
        expected_choice_risks = {"preview": "medium"}
        if key != "fact_evolution.maintenance_mode":
            expected_choice_risks["auto_apply"] = "high"
        if key in {
            "fact_evolution.mode",
            "fact_evolution.tool_mode",
            "fact_evolution.maintenance_mode",
        }:
            expected_choice_risks["reviewed_apply"] = "high"
        assert by_key[key]["choice_risks"] == expected_choice_risks

    assert by_key["fact_evolution.mode"]["restart_required"] is True
    assert by_key["fact_evolution.tool_mode"]["restart_required"] is True
    assert by_key["fact_evolution.maintenance_mode"]["restart_required"] is True
    assert by_key["fact_evolution.journal_mode"]["restart_required"] is False
    assert by_key["fact_evolution.nightly_mode"]["restart_required"] is False
    assert by_key["fact_evolution.journal_mode"]["choices"] == [
        "preview",
        "auto_apply",
    ]
    assert by_key["fact_evolution.nightly_mode"]["choices"] == [
        "preview",
        "auto_apply",
    ]
    assert by_key["fact_evolution.tool_mode"]["choices"] == [
        "preview",
        "auto_apply",
        "reviewed_apply",
    ]
    assert by_key["fact_evolution.maintenance_mode"]["choices"] == [
        "preview",
        "reviewed_apply",
    ]


def test_curated_memory_mode_choices_match_runtime_modes():
    from scope_recall.config_schema import build_config_registry

    by_key = {entry["key"]: entry for entry in build_config_registry()}
    choices = set(by_key["curated_memory.mode"]["choices"])

    assert {"single-user", "explicit-users", "profile-global", "disabled"} <= choices
    assert "shared" not in choices


def test_configuration_doc_mentions_all_registry_keys():
    from scope_recall.config_schema import build_config_registry

    doc = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    missing = [entry["key"] for entry in build_config_registry() if f"`{entry['key']}`" not in doc]

    assert not missing[:10]
