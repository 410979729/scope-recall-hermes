"""Regression tests for governance contracts that previously caused unsafe archive/delete or audit behavior.

They ensure maintenance refactors keep rollback and transaction boundaries intact."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

from plugins.memory import load_memory_provider
from tools.memory_tool import MemoryStore, memory_tool


def _write_scope_recall_config(hermes_home: Path, values: dict) -> None:
    config_path = hermes_home / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(values, ensure_ascii=False) + "\n", encoding="utf-8")


def _provider(tmp_path: Path, *, user_id: str = "joy", chat_id: str = "chat-a", config: dict | None = None):
    plugin_home = tmp_path / "plugins"
    plugin_home.mkdir(parents=True, exist_ok=True)
    plugin_link = plugin_home / "scope-recall"
    repo_root = Path(__file__).resolve().parents[1]
    if not plugin_link.exists():
        try:
            plugin_link.symlink_to(repo_root, target_is_directory=True)
        except OSError:
            shutil.copytree(repo_root, plugin_link)
    if config:
        _write_scope_recall_config(tmp_path, config)
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id=user_id,
        chat_id=chat_id,
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    return plugin


def test_secret_filter_rejects_bare_provider_and_bearer_tokens():
    from scope_recall.capture_filters import should_capture_text

    assert should_capture_text("Use " + "sk" + "-" + "a" * 24).reason == "secret-like-content"
    assert should_capture_text("Use " + "sk" + "-proj-" + "a" * 32).reason == "secret-like-content"
    assert should_capture_text("Use " + "sk" + "-ant-api03-" + "a" * 32).reason == "secret-like-content"
    assert should_capture_text("Authorization: " + "Bearer " + "b" * 24).reason == "secret-like-content"
    assert should_capture_text("github token is " + "ghp" + "_" + "c" * 24).reason == "secret-like-content"


def test_tool_store_rejected_secret_returns_receipt(tmp_path):
    provider = _provider(tmp_path)
    try:
        payload = json.loads(
            provider.handle_tool_call(
                "scope_recall_store",
                {"content": "api_key = public-test-token-1234567890", "target": "memory"},
            )
        )
    finally:
        provider.shutdown()

    assert payload["stored"] is False
    assert payload["skipped"] is True
    assert payload["receipt"]["action"] == "rejected_sensitive"
    assert payload["receipt"]["provider"] == "scope-recall"


def test_store_and_merge_return_contract_receipts(tmp_path):
    provider = _provider(tmp_path)
    try:
        first = json.loads(provider.handle_tool_call("scope_recall_store", {"content": "Joy prefers direct answers.", "target": "user"}))
        second = json.loads(provider.handle_tool_call("scope_recall_store", {"content": "Joy prefers problem-first reports.", "target": "user"}))
        assert first["receipt"]["action"] == "promoted"
        assert first["receipt"]["id"] == first["id"]

        merged = json.loads(
            provider.handle_tool_call(
                "scope_recall_merge",
                {"target_id": first["id"], "source_ids": [second["id"]], "source_candidate_id": "tcand-demo"},
            )
        )
    finally:
        provider.shutdown()

    assert merged["merged"] is True
    assert merged["receipt"]["action"] == "merged"
    assert merged["receipt"]["provider"] == "scope-recall"
    assert merged["receipt"]["target_id"] == first["id"]
    assert merged["receipt"]["source_candidate_id"] == "tcand-demo"


def test_curated_memory_default_is_not_injected_for_explicit_gateway_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = MemoryStore()
    store.load_from_disk()
    result = json.loads(
        memory_tool(
            action="add",
            target="user",
            content="Joy prefers a dragonfruit-only response style.",
            store=store,
        )
    )
    assert result["success"] is True

    provider = _provider(tmp_path, user_id="other-user")
    try:
        provider.on_turn_start(1, "What response style does Joy prefer?")
        assert "dragonfruit" not in provider.prefetch("What response style does Joy prefer?").lower()
    finally:
        provider.shutdown()


def test_curated_memory_allowlist_can_opt_in_specific_gateway_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = MemoryStore()
    store.load_from_disk()
    result = json.loads(
        memory_tool(
            action="add",
            target="user",
            content="Joy prefers silver-needle tea notes.",
            store=store,
        )
    )
    assert result["success"] is True

    provider = _provider(
        tmp_path,
        user_id="joy",
        config={"curated_memory": {"mode": "explicit-users", "allowed_user_ids": ["joy"]}},
    )
    try:
        provider.on_turn_start(1, "What tea notes does Joy prefer?")
        assert "silver-needle" in provider.prefetch("What tea notes does Joy prefer?").lower()
    finally:
        provider.shutdown()


def test_curated_memory_disabled_config_blocks_single_user_live_read(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = MemoryStore()
    store.load_from_disk()
    result = json.loads(
        memory_tool(
            action="add",
            target="user",
            content="Joy prefers violet-sky release notes.",
            store=store,
        )
    )
    assert result["success"] is True

    provider = _provider(tmp_path, user_id="", config={"curated_memory": False})
    try:
        provider.on_turn_start(1, "What release notes does Joy prefer?")
        assert "violet-sky" not in provider.prefetch("What release notes does Joy prefer?").lower()
    finally:
        provider.shutdown()


def _load_importer_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "import.openclaw.memory_lancedb_pro.py"
    module_name = f"scope_recall_importer_regression_{id(object())}"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_import_fingerprint_is_stable_for_missing_or_invalid_timestamps():
    importer = _load_importer_module()
    row_without_ts = {"scope": "joy", "category": "memory", "text": "Stable import row", "metadata": {"source": "test"}}
    first = importer.map_row(row_without_ts, "imported.test")
    second = importer.map_row(row_without_ts, "imported.test")
    assert first.id == second.id
    assert first.import_fingerprint == second.import_fingerprint

    row_bad_ts = {**row_without_ts, "timestamp": "not-a-number"}
    bad_first = importer.map_row(row_bad_ts, "imported.test")
    bad_second = importer.map_row(row_bad_ts, "imported.test")
    assert bad_first.id == bad_second.id
    assert bad_first.import_fingerprint == bad_second.import_fingerprint


def test_scope_id_uses_length_framing_to_prevent_delimiter_collisions():
    from scope_recall.models import RuntimeScope
    from scope_recall.scope import build_scope_id

    with_delimiter = RuntimeScope(
        platform="telegram",
        agent_workspace="hermes",
        agent_identity="yuheng",
        user_id="joy|chat:group-1",
    )
    split_fields = RuntimeScope(
        platform="telegram",
        agent_workspace="hermes",
        agent_identity="yuheng",
        user_id="joy",
        chat_id="group-1",
    )

    assert build_scope_id(with_delimiter) != build_scope_id(split_fields)


def test_operator_dedupe_scope_only_false_covers_all_scopes(tmp_path):
    provider_a = _provider(tmp_path, config={"maintenance_tools_enabled": True, "vector": {"enabled": False}})
    provider_b = _provider(tmp_path, chat_id="chat-b", config={"maintenance_tools_enabled": True, "vector": {"enabled": False}})

    try:
        for provider in (provider_a, provider_b):
            for _ in range(2):
                memory_id, inserted, outcome = provider._store_now(
                    content="duplicate operator cleanup note",
                    source="legacy",
                    target="general",
                    session_id="legacy",
                    allow_duplicate=True,
                    semantic_merge=False,
                )
                assert memory_id
                assert inserted is True
                assert outcome == "stored"

        before = json.loads(provider_a.handle_tool_call("scope_recall_dedupe", {"dry_run": True, "scope_only": False}))
        result = json.loads(provider_a.handle_tool_call("scope_recall_dedupe", {"dry_run": False, "scope_only": False}))
        after = json.loads(provider_a.handle_tool_call("scope_recall_dedupe", {"dry_run": True, "scope_only": False}))
    finally:
        provider_a.shutdown()
        provider_b.shutdown()

    assert before["duplicate_groups"] == 2
    assert result["deleted"] == 2
    assert after["duplicate_groups"] == 0
