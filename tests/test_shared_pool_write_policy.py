from __future__ import annotations

import json

from plugins.memory import load_memory_provider
from scope_recall.sql_store import store_row


def _write_config(hermes_home, values):
    config_path = hermes_home / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(values, ensure_ascii=False) + "\n", encoding="utf-8")


def _provider(tmp_path, *, write_enabled: bool, maintenance_tools_enabled: bool = False):
    _write_config(
        tmp_path,
        {
            "maintenance_tools_enabled": maintenance_tools_enabled,
            "vector": {"enabled": False},
            "shared_pool": {"enabled": True, "pool_id": "beidou", "write_enabled": write_enabled},
            "retrieval": {"mode": "lexical", "min_score": 0.01},
        },
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-shared-pool-write",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="dm",
    )
    return plugin


def test_shared_pool_write_fails_closed_by_default(tmp_path):
    plugin = _provider(tmp_path, write_enabled=False)
    try:
        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Shared pool write should require explicit opt in.", "target": "memory", "scope_mode": "shared_pool"},
            )
        )
        stats = json.loads(plugin.handle_tool_call("scope_recall_stats", {}))

        assert payload["stored"] is False
        assert payload["skipped"] is True
        assert payload["skip_reason"] == "shared_pool_write_disabled"
        assert payload["scope_mode"] == "shared_pool"
        assert stats["shared_pool"]["enabled"] is True
        assert stats["shared_pool"]["write_enabled"] is False
        assert stats["shared_pool"]["memories"] == 0
    finally:
        plugin.shutdown()


def test_shared_pool_write_enabled_stores_into_pool_scope(tmp_path):
    plugin = _provider(tmp_path, write_enabled=True)
    try:
        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Shared pool opt-in memory for Beidou agents.", "target": "memory", "scope_mode": "shared_pool"},
            )
        )
        stats = json.loads(plugin.handle_tool_call("scope_recall_stats", {}))
        row = plugin._require_conn().execute("SELECT target, scope_id, metadata FROM memories WHERE id = ?", (payload["id"],)).fetchone()
        metadata = json.loads(row["metadata"])

        assert payload["stored"] is True
        assert payload["scope_mode"] == "shared_pool"
        assert row["target"] == "memory"
        assert row["scope_id"] == stats["shared_pool"]["scope_id"]
        assert metadata["scope_mode"] == "shared_pool"
        assert stats["shared_pool"]["write_enabled"] is True
        assert stats["shared_pool"]["memories"] == 1
    finally:
        plugin.shutdown()


def test_forgetting_run_does_not_mutate_read_only_shared_pool(tmp_path):
    plugin = _provider(tmp_path, write_enabled=False, maintenance_tools_enabled=True)
    try:
        store_row(
            plugin._require_conn(),
            memory_id="pool-noise",
            scope_id=plugin._shared_pool_scope_id,
            platform="telegram",
            user_id="joy",
            chat_id="dm",
            thread_id="",
            gateway_session_key="",
            agent_identity="yuheng",
            agent_workspace="hermes",
            session_id="session",
            source="journal-digest",
            target="memory",
            content="Journal digest memory decision/workflow about test: user: 继续 assistant: 完成。",
            metadata=json.dumps({"scope_mode": "shared_pool"}),
        )

        payload = json.loads(plugin.handle_tool_call("scope_recall_forgetting_run", {"dry_run": False, "limit": 20}))
        row = plugin._require_conn().execute("SELECT metadata FROM memories WHERE id = 'pool-noise'").fetchone()
        metadata = json.loads(row["metadata"])

        assert payload["archived"] == 0
        assert metadata.get("lifecycle") != "archived"
        assert plugin._shared_pool_scope_id in plugin._accessible_scope_ids
        assert plugin._shared_pool_scope_id not in plugin._writable_scope_ids
    finally:
        plugin.shutdown()


def test_explicit_scope_mode_is_respected_and_semantic_merge_stays_in_scope(tmp_path):
    plugin = _provider(tmp_path, write_enabled=True)
    try:
        local = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Project Atlas deploy command uses alpha flag.",
                    "target": "project",
                    "scope_mode": "local",
                },
            )
        )
        shared = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Project Atlas deploy command uses alpha flag.",
                    "target": "project",
                    "scope_mode": "shared",
                },
            )
        )

        assert local["stored"] is True
        assert local["scope_mode"] == "local"
        assert shared["stored"] is True
        assert shared["scope_mode"] == "shared"
        rows = plugin._require_conn().execute("SELECT id, scope_id FROM memories WHERE id IN (?, ?)", (local["id"], shared["id"])).fetchall()
        by_id = {row["id"]: row["scope_id"] for row in rows}
        assert by_id[local["id"]] == plugin._scope_id
        assert by_id[shared["id"]] == plugin._shared_scope_id
    finally:
        plugin.shutdown()


def test_shared_pool_memory_can_be_updated_and_merged_inside_pool(tmp_path):
    plugin = _provider(tmp_path, write_enabled=True)
    try:
        first = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Shared pool release checklist alpha.", "target": "memory", "scope_mode": "shared_pool"},
            )
        )
        second = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Shared pool incident checklist beta.", "target": "memory", "scope_mode": "shared_pool"},
            )
        )
        updated = json.loads(
            plugin.handle_tool_call(
                "scope_recall_update",
                {"id": first["id"], "content": "Shared pool release checklist alpha updated.", "target": "memory"},
            )
        )
        merged = json.loads(
            plugin.handle_tool_call(
                "scope_recall_merge",
                {"target_id": first["id"], "source_ids": [second["id"]]},
            )
        )

        assert first["stored"] is True
        assert second["stored"] is True
        assert updated["updated"] is True
        assert updated["scope_mode"] == "shared_pool"
        assert merged["merged"] is True
        assert merged["scope_mode"] == "shared_pool"
        assert merged["deleted"] == 1
        target_row = plugin._require_conn().execute("SELECT scope_id FROM memories WHERE id = ?", (first["id"],)).fetchone()
        assert target_row["scope_id"] == plugin._shared_pool_scope_id
        assert plugin._require_conn().execute("SELECT COUNT(*) FROM memories WHERE id = ?", (second["id"],)).fetchone()[0] == 0
    finally:
        plugin.shutdown()
