from __future__ import annotations

import json
import logging
from pathlib import Path

from plugins.memory import load_memory_provider
from scope_recall.capture_llm import _parse_response


def _write_scope_recall_config(hermes_home: Path, values: dict) -> None:
    config_path = hermes_home / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(values, ensure_ascii=False) + "\n", encoding="utf-8")


def _provider(tmp_path: Path, config: dict):
    _write_scope_recall_config(tmp_path, config)
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "audit-regression-session",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    return plugin


def test_string_false_config_values_remain_disabled(tmp_path):
    provider = _provider(
        tmp_path,
        {
            "maintenance_tools_enabled": "false",
            "shared_pool": {"enabled": "false", "pool_id": "beidou"},
            "vector": {"enabled": "false"},
        },
    )
    try:
        assert provider._vector_enabled is False
        assert provider._shared_pool_enabled is False
        assert provider._shared_pool_scope_id == ""
        assert all("shared-pool" not in scope_id for scope_id in provider._accessible_scope_ids)

        schema_names = {schema["name"] for schema in provider.get_tool_schemas()}
        assert "scope_recall_govern" not in schema_names
        blocked = json.loads(provider.handle_tool_call("scope_recall_govern", {"dry_run": True}))
        assert blocked["error"] == "scope_recall_govern requires maintenance_tools_enabled=true"
    finally:
        provider.shutdown()


def test_forget_requires_explicit_accessible_ids_before_archiving(tmp_path):
    provider = _provider(tmp_path, {"vector": {"enabled": False}})
    try:
        stored = json.loads(
            provider.handle_tool_call(
                "scope_recall_store",
                {"content": "Temporary audit delete note should only disappear by exact id.", "target": "memory"},
            )
        )
        assert stored["stored"] is True

        blocked = json.loads(provider.handle_tool_call("scope_recall_forget", {"query": "Temporary audit delete", "limit": 5}))
        assert blocked["error"] == "ids are required for scope_recall_forget; search or inspect first, then pass exact ids"
        with provider._lock:
            still_there = provider._require_conn().execute("SELECT COUNT(*) FROM memories WHERE id = ?", (stored["id"],)).fetchone()[0]
        assert still_there == 1

        archived = json.loads(provider.handle_tool_call("scope_recall_forget", {"ids": [stored["id"]]}))
        assert archived["archived"] == 1
        assert archived["deleted"] == 0
        assert archived["ids"] == [stored["id"]]
        with provider._lock:
            row = provider._require_conn().execute("SELECT metadata FROM memories WHERE id = ?", (stored["id"],)).fetchone()
        assert row is not None
        assert json.loads(row["metadata"])["lifecycle"] == "archived"
    finally:
        provider.shutdown()


def test_capture_llm_parse_failures_log_metadata_not_raw_sensitive_text(caplog):
    raw = 'secret-token sk-test-SHOULD-NOT-LOG before json [{"action":"insert","content":"unterminated secret memory'
    caplog.set_level(logging.WARNING, logger="scope_recall.capture_llm")

    assert _parse_response(raw) == []

    log_text = caplog.text
    assert "sk-test-SHOULD-NOT-LOG" not in log_text
    assert "unterminated secret memory" not in log_text
    assert "raw_len=" in log_text
