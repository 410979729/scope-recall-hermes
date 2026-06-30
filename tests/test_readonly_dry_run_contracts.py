"""Tests that read-only and dry-run commands do not mutate runtime state.

These contracts are central to safe operator workflows and release checks."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from scope_recall.governance_cleanup import apply_cleanup
from scope_recall.journal import append_journal_entry, ensure_journal_schema, run_journal_digest
from scope_recall.models import RuntimeScope
from scope_recall.scope import build_scope_id, build_shared_scope_id
from scope_recall.sql_store import ensure_schema, store_row


def _scope() -> RuntimeScope:
    return RuntimeScope(
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        agent_context="primary",
    )


def test_governance_cleanup_dry_run_is_query_only_on_readonly_connection(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    writer = sqlite3.connect(db_path)
    writer.row_factory = sqlite3.Row
    try:
        ensure_schema(writer)
        store_row(
            writer,
            memory_id="template-noise",
            scope_id="shared-scope",
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
            content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。",
            allow_duplicate=True,
        )
    finally:
        writer.close()

    readonly = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    readonly.row_factory = sqlite3.Row
    readonly.execute("PRAGMA query_only=ON")
    try:
        result = apply_cleanup(readonly, scope_ids=["shared-scope"], dry_run=True, limit=20, batch_id="dry-query-only")
    finally:
        readonly.close()

    assert result["dry_run"] is True
    assert result["candidate_count"] == 1
    assert result["archived"] == 0

    verifier = sqlite3.connect(db_path)
    verifier.row_factory = sqlite3.Row
    try:
        row = verifier.execute("SELECT metadata FROM memories WHERE id = 'template-noise'").fetchone()
        metadata = json.loads(row["metadata"] or "{}")
        assert metadata.get("lifecycle") != "archived"
    finally:
        verifier.close()


def test_journal_digest_dry_run_reads_source_db_read_only_and_does_not_mutate(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(json.dumps({"vector": {"enabled": False}}), encoding="utf-8")
    db_path = storage / "memory.sqlite3"
    writer = sqlite3.connect(db_path)
    writer.row_factory = sqlite3.Row
    try:
        ensure_schema(writer)
        ensure_journal_schema(writer)
        scope = _scope()
        entry_id = append_journal_entry(
            writer,
            scope=scope,
            scope_id=build_scope_id(scope),
            shared_scope_id=build_shared_scope_id(scope),
            session_id="dry-run-source-readonly",
            turn_number=1,
            role="user",
            content="scope-recall journal dry-run must read source DB in read-only mode.",
        )
        writer.commit()
    finally:
        writer.close()

    import scope_recall.journal as journal_module

    calls: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []
    real_connect = sqlite3.connect

    def capture_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        calls.append((database, args, kwargs))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(journal_module.sqlite3, "connect", capture_connect)

    result = run_journal_digest(hermes_home=hermes_home, extractor="heuristic", scope=_scope(), interval_label="test", limit_entries=50, dry_run=True)

    assert result["status"] == "dry_run"
    assert any(str(database) == ":memory:" for database, _args, _kwargs in calls)
    assert any(str(database) == f"file:{db_path}?mode=ro" and kwargs.get("uri") is True for database, _args, kwargs in calls)

    verifier = real_connect(db_path)
    verifier.row_factory = sqlite3.Row
    try:
        row = verifier.execute("SELECT processed_run_id FROM journal_entries WHERE id = ?", (entry_id,)).fetchone()
        assert row["processed_run_id"] == ""
        assert verifier.execute("SELECT COUNT(*) FROM journal_digest_runs").fetchone()[0] == 0
        assert verifier.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    finally:
        verifier.close()
