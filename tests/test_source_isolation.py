"""Public chat source-isolation and N-1 privacy boundary tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from plugins.memory import load_memory_provider
from scope_recall.journal import run_journal_digest
from scope_recall.journal_store import append_journal_entry, ensure_journal_schema
from scope_recall.models import RuntimeScope
from scope_recall.scope import build_scope_id, build_shared_scope_id
from scope_recall.source_isolation import (
    chat_is_memory_isolated,
    memory_isolated_chat_ids,
    scope_is_memory_isolated,
)
from scope_recall.sql_store import ensure_schema

ISOLATED_CHAT = "isolated-chat-fixture"
ALLOWED_CHAT = "allowed-chat-fixture"


def _write_config(home: Path) -> None:
    storage = home / "scope-recall"
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "config.json").write_text(
        json.dumps(
            {
                "memory_isolated_chat_ids": [ISOLATED_CHAT],
                "auto_capture": True,
                "capture_raw_user": True,
                "vector": {"enabled": False},
                "journal": {
                    "enabled": True,
                    "digest_on_session_end": False,
                    "background_digest_enabled": False,
                    "extractor": "heuristic",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _initialize_provider(home: Path, *, chat_id: str, session_id: str):
    provider = load_memory_provider("scope-recall")
    assert provider is not None
    provider.initialize(
        session_id,
        hermes_home=str(home),
        platform="telegram",
        user_id="owner-fixture",
        chat_id=chat_id,
        agent_identity="test-agent",
        agent_workspace="test-workspace",
        agent_context="primary",
    )
    return provider


def test_source_isolation_policy_normalizes_runtime_only_identifiers() -> None:
    config = {"memory_isolated_chat_ids": ["  isolated-a  ", "", "isolated-b"]}
    scope = RuntimeScope(chat_id="isolated-a")

    assert memory_isolated_chat_ids(config) == frozenset({"isolated-a", "isolated-b"})
    assert chat_is_memory_isolated("isolated-b", config) is True
    assert scope_is_memory_isolated(scope, config) is True
    assert chat_is_memory_isolated("allowed", config) is False


def test_isolated_chat_denies_prompt_recall_tools_capture_and_journal(tmp_path: Path) -> None:
    _write_config(tmp_path)
    allowed = _initialize_provider(
        tmp_path,
        chat_id=ALLOWED_CHAT,
        session_id="allowed-session",
    )
    try:
        assert allowed.suppress_builtin_memory() is False
        stored = json.loads(
            allowed.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Allowed chat durable sentinel remains private.",
                    "target": "memory",
                },
            )
        )
        assert stored["stored"] is True
        allowed.flush(timeout=5.0)
    finally:
        allowed.shutdown()

    isolated = _initialize_provider(
        tmp_path,
        chat_id=ISOLATED_CHAT,
        session_id="isolated-session",
    )
    try:
        before_memories = isolated._require_conn().execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0]
        before_journal = isolated._require_conn().execute(
            "SELECT COUNT(*) FROM journal_entries"
        ).fetchone()[0]

        prompt = isolated.system_prompt_block()
        assert isolated.suppress_builtin_memory() is True
        assert "Disabled for this chat" in prompt
        assert "durable sentinel" not in prompt
        assert isolated.prefetch("What private memory exists?") == ""
        assert isolated.get_tool_schemas() == []
        tool_result = json.loads(
            isolated.handle_tool_call(
                "scope_recall_store",
                {"content": "This isolated content must never persist.", "target": "user"},
            )
        )
        assert "disabled for this chat" in str(tool_result["error"])

        isolated.sync_turn(
            "This isolated user turn must never persist.",
            "This isolated assistant turn must never persist.",
            messages=[
                {
                    "role": "tool",
                    "name": "fixture-tool",
                    "content": "isolated tool trace must not persist",
                }
            ],
        )
        assert (
            isolated.on_pre_compress(
                [
                    {
                        "role": "user",
                        "content": "isolated compression content must not persist",
                    }
                ]
            )
            == ""
        )
        isolated.on_session_end(
            [
                {
                    "role": "tool",
                    "name": "fixture-tool",
                    "content": "isolated session trace must not persist",
                }
            ]
        )
        isolated.flush(timeout=5.0)

        after_memories = isolated._require_conn().execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0]
        after_journal = isolated._require_conn().execute(
            "SELECT COUNT(*) FROM journal_entries"
        ).fetchone()[0]
        assert after_memories == before_memories == 1
        assert after_journal == before_journal == 0
    finally:
        isolated.shutdown()


def test_historical_isolated_journal_backlog_is_not_consumed(tmp_path: Path) -> None:
    _write_config(tmp_path)
    storage = tmp_path / "scope-recall"
    db_path = storage / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_journal_schema(conn)
    isolated_scope = RuntimeScope(
        platform="telegram",
        user_id="owner-fixture",
        chat_id=ISOLATED_CHAT,
        agent_identity="test-agent",
        agent_workspace="test-workspace",
        agent_context="primary",
    )
    entry_id = append_journal_entry(
        conn,
        scope=isolated_scope,
        scope_id=build_scope_id(isolated_scope),
        shared_scope_id=build_shared_scope_id(isolated_scope),
        session_id="historical-isolated-session",
        turn_number=1,
        role="user",
        content="Historical isolated journal says the owner prefers a private sentinel.",
    )
    assert entry_id > 0
    conn.close()

    result = run_journal_digest(
        hermes_home=tmp_path,
        extractor="heuristic",
        scope=None,
        dry_run=False,
    )

    conn = sqlite3.connect(db_path)
    try:
        memory_count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        row = conn.execute(
            "SELECT processed_run_id FROM journal_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
    finally:
        conn.close()
    assert result["status"] == "no_unprocessed_journal"
    assert memory_count == 0
    assert row is not None
    assert row[0] == ""

    explicit = run_journal_digest(
        hermes_home=tmp_path,
        extractor="heuristic",
        scope=isolated_scope,
        dry_run=False,
    )
    assert explicit["status"] == "source_isolated"
    assert explicit["source_isolated"] is True
