"""Public store-contract regressions for merge and scope safety.

These tests keep exact deduplication, fuzzy merge opt-in, and target-derived scope
routing separate so a convenient store call cannot silently rewrite or mis-scope
memory truth.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from plugins.memory import load_memory_provider
from scope_recall.sql_store import ensure_schema, store_row


def _provider(tmp_path):
    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "config.json").write_text(
        json.dumps(
            {
                "vector": {"enabled": False},
                "relation_extraction_enabled": False,
                "retrieval": {"mode": "lexical", "min_score": 0.01},
            }
        ),
        encoding="utf-8",
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-store-contracts",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="agent-a",
        agent_workspace="workspace-a",
        user_id="operator",
        chat_id="dm",
    )
    return plugin


def _store(plugin, content: str, target: str, **extra):
    return json.loads(
        plugin.handle_tool_call(
            "scope_recall_store",
            {"content": content, "target": target, **extra},
        )
    )


def test_fuzzy_semantic_merge_is_off_by_default(tmp_path):
    plugin = _provider(tmp_path)
    try:
        first = _store(plugin, "Backups run daily at 02:00.", "project")
        second = _store(plugin, "Backups run daily at 03:00.", "project")

        assert first["stored"] is True
        assert second["stored"] is True
        assert second.get("merged") is not True
        assert second["id"] != first["id"]
        assert plugin._require_conn().execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0] == 2
    finally:
        plugin.shutdown()


def test_exact_duplicate_suppression_remains_enabled_without_fuzzy_merge(tmp_path):
    plugin = _provider(tmp_path)
    try:
        first = _store(plugin, "Project Atlas deploys from the signed release tag.", "project")
        second = _store(plugin, "Project Atlas deploys from the signed release tag.", "project")

        assert first["stored"] is True
        assert second["stored"] is False
        assert second["duplicate"] is True
        assert second["id"] == first["id"]
    finally:
        plugin.shutdown()


def test_explicit_semantic_merge_accepts_only_contained_additive_text(tmp_path):
    plugin = _provider(tmp_path)
    try:
        first = _store(plugin, "Project Atlas deployment checklist is reviewed.", "project")
        second = _store(
            plugin,
            "Project Atlas deployment checklist is reviewed and signed.",
            "project",
            semantic_merge=True,
        )

        assert first["stored"] is True
        assert second["stored"] is False
        assert second["merged"] is True
        assert second["id"] == first["id"]
    finally:
        plugin.shutdown()


def test_semantic_merge_never_reports_success_when_update_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    plugin = _provider(tmp_path)
    try:
        first = _store(
            plugin,
            "Project Atlas deployment checklist is reviewed.",
            "project",
        )
        assert first["stored"] is True
        store_service = plugin._store_now.__func__.__globals__["store_memory_now"]
        monkeypatch.setitem(
            store_service.__globals__,
            "update_memory",
            lambda *_args, **_kwargs: False,
        )

        with pytest.raises(RuntimeError, match="semantic merge update failed"):
            plugin._store_now(
                content="Project Atlas deployment checklist is reviewed and signed.",
                source="tool-store",
                target="project",
                session_id="session-store-contracts",
                semantic_merge=True,
            )
    finally:
        plugin.shutdown()


@pytest.mark.parametrize(
    ("existing", "candidate"),
    [
        (
            "Project Atlas deployment target is staging.",
            "Project Atlas deployment target is production.",
        ),
        (
            "Alice owns the deployment checklist.",
            "Bob owns the deployment checklist.",
        ),
        (
            "Backups run daily at 02:00.",
            "Backups run daily at 03:00.",
        ),
    ],
)
def test_explicit_semantic_merge_does_not_absorb_changed_assertions(
    tmp_path, existing, candidate
):
    plugin = _provider(tmp_path)
    try:
        first = _store(plugin, existing, "project")
        second = _store(plugin, candidate, "project", semantic_merge=True)

        assert first["stored"] is True
        assert second["stored"] is True
        assert second.get("merged") is not True
        assert second["id"] != first["id"]
    finally:
        plugin.shutdown()


@pytest.mark.parametrize(
    ("target", "scope_mode"),
    [("general", "shared"), ("project", "local")],
)
def test_public_store_rejects_scope_override_that_violates_target_contract(
    tmp_path, target, scope_mode
):
    plugin = _provider(tmp_path)
    try:
        payload = _store(
            plugin,
            "Scope mismatch sentinel must never be stored.",
            target,
            scope_mode=scope_mode,
        )

        assert payload.get("error")
        assert payload.get("invalid_scope_mode") is True
        assert payload.get("target") == target
        assert payload.get("scope_mode") == scope_mode
        assert plugin._require_conn().execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0] == 0
    finally:
        plugin.shutdown()


def test_internal_store_boundary_rejects_mismatched_scope_before_write(tmp_path):
    plugin = _provider(tmp_path)
    try:
        with pytest.raises(ValueError, match="scope mode"):
            plugin._store_now(
                content="Internal scope mismatch sentinel.",
                source="tool-store",
                target="project",
                session_id="session-store-contracts",
                semantic_merge=False,
                scope_mode="local",
            )
        assert plugin._require_conn().execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0] == 0
    finally:
        plugin.shutdown()


def test_sql_store_rejects_plaintext_secret_like_content(tmp_path):
    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    secret = "sk-" + "S" * 24

    with pytest.raises(ValueError, match="secret-like content rejected"):
        store_row(
            conn,
            memory_id="secret-row",
            scope_id="scope-a",
            platform="test",
            user_id="joy",
            chat_id="dm",
            thread_id="",
            gateway_session_key="",
            agent_identity="yuheng",
            agent_workspace="hermes",
            session_id="session",
            source="test",
            target="memory",
            content=f"API credential api_key={secret}",
            allow_duplicate=True,
        )

    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    conn.close()
