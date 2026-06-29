from __future__ import annotations

from pathlib import Path

import scope_recall.journal as journal_module
import scope_recall.journal_store as journal_store


def test_journal_store_module_exports_identity_match_journal_reexports():
    assert journal_module.JournalEntry is journal_store.JournalEntry
    assert journal_module.DATA_URL_PREFIX_RE is journal_store.DATA_URL_PREFIX_RE
    assert journal_module.BASE64ISH_RE is journal_store.BASE64ISH_RE
    assert journal_module._strip_inline_data_urls is journal_store._strip_inline_data_urls
    assert journal_module._looks_like_base64_blob is journal_store._looks_like_base64_blob
    assert journal_module._journal_entry_for_digest is journal_store._journal_entry_for_digest
    assert journal_module.ensure_journal_schema is journal_store.ensure_journal_schema
    assert journal_module._metadata_json is journal_store._metadata_json
    assert journal_module._journal_capture_allowed is journal_store._journal_capture_allowed
    assert journal_module._chunk_journal_text is journal_store._chunk_journal_text
    assert journal_module._insert_journal_entry is journal_store._insert_journal_entry
    assert journal_module.append_journal_entry is journal_store.append_journal_entry
    assert journal_module._row_to_entry is journal_store._row_to_entry
    assert journal_module.load_unprocessed_journal_entries is journal_store.load_unprocessed_journal_entries
    assert journal_module.mark_entries_processed is journal_store.mark_entries_processed
    assert journal_module._journal_unprocessed_count is journal_store._journal_unprocessed_count
    assert journal_module._prune_processed_journal is journal_store._prune_processed_journal


def test_journal_store_has_no_static_journal_import():
    assert journal_store.__file__ is not None
    source = Path(journal_store.__file__).read_text(encoding="utf-8")
    assert "from . import journal" not in source
    assert "from .journal import" not in source
    assert "from scope_recall import journal" not in source
    assert "import scope_recall.journal" not in source
    assert "journal_llm" not in source


def test_journal_store_append_load_mark_and_prune_round_trip(tmp_path):
    import sqlite3

    from scope_recall.models import RuntimeScope
    from scope_recall.scope import build_scope_id, build_shared_scope_id

    scope = RuntimeScope(
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="default",
        agent_workspace="hermes",
        agent_context="primary",
    )
    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    journal_store.ensure_journal_schema(conn)
    entry_id = journal_store.append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="round-trip",
        turn_number=1,
        role="user",
        content="Round trip journal store workflow should be loaded then marked processed.",
    )
    assert entry_id > 0
    assert journal_store._journal_unprocessed_count(conn) == 1
    entries = journal_store.load_unprocessed_journal_entries(conn, scope_ids=[build_scope_id(scope)], limit=10)
    assert [entry.id for entry in entries] == [entry_id]
    assert entries[0].content == "Round trip journal store workflow should be loaded then marked processed."

    journal_store.mark_entries_processed(conn, entry_ids=[entry_id], run_id="run-1")
    assert journal_store._journal_unprocessed_count(conn) == 0

    conn.execute("UPDATE journal_entries SET created_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (entry_id,))
    conn.execute(
        "INSERT OR REPLACE INTO journal_rejections(journal_entry_id, run_id, reason, candidate, created_at) VALUES (?, 'run-1', 'test', 'candidate', '2000-01-01T00:00:00+00:00')",
        (entry_id,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO memory_journal_sources(memory_id, journal_entry_id, run_id, created_at) VALUES ('memory-1', ?, 'run-1', '2000-01-01T00:00:00+00:00')",
        (entry_id,),
    )
    conn.commit()
    assert journal_store._prune_processed_journal(conn, retention_days=1) == 1
    assert conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM journal_rejections").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM memory_journal_sources").fetchone()[0] == 0
