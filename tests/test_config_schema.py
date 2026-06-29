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
    assert by_key["vector.embedder.api_key_env"]["risk"] == "high"
    assert by_key["journal.max_entries_per_digest"]["type"] == "integer"


def test_configuration_doc_mentions_all_registry_keys():
    from scope_recall.config_schema import build_config_registry

    doc = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    missing = [entry["key"] for entry in build_config_registry() if f"`{entry['key']}`" not in doc]

    assert not missing[:10]
