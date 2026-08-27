"""Extension boundaries remain optional, inspectable, and inert."""

from __future__ import annotations

import json
from pathlib import Path

from plugins.memory import load_memory_provider

from scope_recall.doctor_extensions import extension_report
from scope_recall.extension_boundary import (
    EXTENSION_BOUNDARIES,
    extension_boundary_status,
    validate_extension_boundaries,
)
from scope_recall.provider_schemas import build_tool_schemas


ROOT = Path(__file__).resolve().parents[1]


def test_extension_registry_is_complete_and_has_no_second_authority_or_scheduler():
    assert validate_extension_boundaries() == ()
    assert {item.name for item in EXTENSION_BOUNDARIES} == {
        "graph",
        "experience",
        "playbook",
        "reflection",
        "external_bridge",
    }
    assert all(not item.core_startup_required for item in EXTENSION_BOUNDARIES)
    assert all(not item.truth_authority for item in EXTENSION_BOUNDARIES)
    assert all(item.disable_path for item in EXTENSION_BOUNDARIES)
    assert {item.scheduler_owner for item in EXTENSION_BOUNDARIES} <= {
        "none",
        "core-background",
    }


def test_all_automatic_extensions_can_be_disabled_while_core_tools_remain():
    config = {
        "tool_schema_profile": "core",
        "relation_extraction_enabled": False,
        "retrieval": {"relation_rerank_enabled": False},
        "experience": {"enabled": False},
        "reflection": {"enabled": False},
    }
    status = {row["name"]: row for row in extension_boundary_status(config)}
    names = {schema["name"] for schema in build_tool_schemas(config)}

    assert all(not row["enabled"] for row in status.values())
    assert names == {
        "scope_recall_store",
        "scope_recall_search",
        "scope_recall_context",
        "scope_recall_profile",
        "scope_recall_memory",
        "scope_recall_entity",
    }


def test_extension_doctor_is_content_free_and_reports_disable_paths():
    secret = "private-extension-config-value"
    payload, check, recommendations = extension_report(
        {
            "experience": {"enabled": False, "provider": secret},
            "reflection": {"enabled": False, "api_key": secret},
        }
    )

    assert check["ok"] is True
    assert recommendations == []
    assert payload["truth_authority"] == "sqlite"
    assert payload["global_scheduler_count_added"] == 0
    assert payload["content_free"] is True
    assert all(row["disable_path"] for row in payload["extensions"])
    assert secret not in json.dumps(payload)


def test_disabled_experience_skips_startup_backfill(tmp_path, monkeypatch):
    config_path = tmp_path / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "experience": {"enabled": False},
                "vector": {"enabled": False},
                "journal": {"background_digest_enabled": False},
            }
        ),
        encoding="utf-8",
    )

    import scope_recall.provider as provider_module

    def forbidden_backfill(*_args, **_kwargs):
        raise AssertionError("disabled Experience must not run startup backfill")

    monkeypatch.setattr(
        provider_module, "backfill_skill_anchors", forbidden_backfill
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    try:
        plugin.initialize(
            "extension-boundary",
            hermes_home=str(tmp_path),
            platform="telegram",
            agent_context="primary",
            agent_identity="test-agent",
            agent_workspace="test-workspace",
            user_id="test-user",
            chat_id="test-chat",
        )
        assert plugin.runtime_status_view()["status"] == "active"
    finally:
        plugin.shutdown()


def test_core_modules_import_only_lazy_experience_boundaries():
    assert "from .experience_store import" not in (ROOT / "provider.py").read_text(
        encoding="utf-8"
    )
    assert "from ...experience_store import" not in (
        ROOT / "_internal" / "runtime" / "process_lifecycle.py"
    ).read_text(encoding="utf-8")
    tooling = (ROOT / "tooling.py").read_text(encoding="utf-8")
    assert "from .experience_preflight import" not in tooling
    assert "from .experience_promotion import" not in tooling
    assert "from .experience_store import" not in tooling
