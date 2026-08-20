"""Tests for journal schema, capture filtering, chunking, and processed flags.

They protect journal rows as operational evidence for later digest/recovery."""

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


def test_journal_retention_prunes_in_bounded_sqlite_batches(tmp_path):
    """Retention must stay below deployments with a low SQLite variable cap."""

    import sqlite3

    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    journal_store.ensure_journal_schema(conn)
    conn.executemany(
        """
        INSERT INTO journal_entries(
            scope_id, shared_scope_id, platform, user_id, chat_id, thread_id,
            gateway_session_key, agent_identity, agent_workspace, session_id,
            turn_number, role, content, content_hash, created_at,
            processed_run_id, processed_at, metadata
        ) VALUES ('scope', 'shared', 'telegram', 'joy', 'dm', '', '',
                  'default', 'hermes', ?, 1, 'user', ?, ?,
                  '2000-01-01T00:00:00+00:00', 'run-1',
                  '2000-01-01T00:00:00+00:00', '{}')
        """,
        [
            (f"session-{index}", f"content-{index}", f"hash-{index}")
            for index in range(1200)
        ],
    )
    conn.commit()
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 500)

    assert journal_store._prune_processed_journal(conn, retention_days=1) == 1200
    assert conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0

def test_journal_schema_adds_session_cursor_and_defer_count(tmp_path):
    import sqlite3

    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE journal_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_id TEXT NOT NULL,
            shared_scope_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL DEFAULT 0,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            processed_run_id TEXT NOT NULL DEFAULT '',
            processed_at TEXT,
            metadata TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.commit()
    journal_store.ensure_journal_schema(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(journal_entries)")}
    assert "extraction_attempts" in columns
    assert "deferred_run_id" in columns
    assert "deferred_at" in columns
    assert "defer_count" in columns
    assert "retryable_failures" in columns
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "journal_session_digest_state" in tables
    journal_store.ensure_journal_schema(conn)
    assert {row[1] for row in conn.execute("PRAGMA table_info(journal_entries)")} == columns


def test_unprocessed_loader_honors_session_resume_cursor(tmp_path):
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
    scope_id = build_scope_id(scope)
    entry_ids = [
        journal_store.append_journal_entry(
            conn,
            scope=scope,
            scope_id=scope_id,
            shared_scope_id=build_shared_scope_id(scope),
            session_id="fat-cursor",
            turn_number=index,
            role="user",
            content=f"cursor resume row {index} with enough durable journal text to stay captured.",
        )
        for index in range(1, 6)
    ]
    journal_store.upsert_session_digest_state(
        conn,
        scope_id=scope_id,
        session_id="fat-cursor",
        resume_after_id=entry_ids[1],
        run_id="run-1",
    )
    loaded = journal_store.load_unprocessed_journal_entries(
        conn, scope_ids=[scope_id], limit=50
    )
    assert [entry.id for entry in loaded] == entry_ids[2:]

    journal_store.mark_entries_processed(conn, entry_ids=entry_ids[2:], run_id="run-2")
    wrapped = journal_store.load_unprocessed_journal_entries(
        conn, scope_ids=[scope_id], limit=50
    )
    assert [entry.id for entry in wrapped] == entry_ids[:2]


def _insert_loader_row(
    conn,
    *,
    scope_id: str,
    session_id: str,
    turn: int,
    content: str,
    chat_id: str = "dm",
    created_at: str = "2026-08-19T00:00:00+00:00",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO journal_entries(
            scope_id, shared_scope_id, platform, user_id, chat_id, thread_id,
            gateway_session_key, agent_identity, agent_workspace, session_id,
            turn_number, role, content, content_hash, created_at,
            processed_run_id, metadata
        ) VALUES (?, 'shared', 'telegram', 'joy', ?, '', '',
                  'default', 'hermes', ?, ?, 'user', ?, ?, ?, '', '{}')
        """,
        (
            scope_id,
            chat_id,
            session_id,
            turn,
            content,
            f"hash-{scope_id}-{session_id}-{turn}",
            created_at,
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def test_loader_partitions_per_scope_session_pair_not_shared_session_string(tmp_path):
    """Two scopes sharing one session string must not share one per-session cap."""

    import sqlite3

    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    journal_store.ensure_journal_schema(conn)
    scope_a = "scope-a"
    scope_b = "scope-b"
    shared_session = "shared-session"
    a_ids = [
        _insert_loader_row(
            conn,
            scope_id=scope_a,
            session_id=shared_session,
            turn=index,
            content=f"scope-a shared session row {index} with durable journal text.",
            created_at=f"2026-08-19T00:00:0{index}+00:00",
        )
        for index in (1, 2)
    ]
    b_ids = [
        _insert_loader_row(
            conn,
            scope_id=scope_b,
            session_id=shared_session,
            turn=index,
            content=f"scope-b shared session row {index} with durable journal text.",
            created_at=f"2026-08-19T00:01:0{index}+00:00",
        )
        for index in (1, 2)
    ]
    excluded_id = _insert_loader_row(
        conn,
        scope_id=scope_a,
        session_id=shared_session,
        turn=9,
        content="excluded isolated chat row must not consume the pair budget.",
        chat_id="isolated-chat",
        created_at="2026-08-19T00:00:00+00:00",
    )

    loaded = journal_store.load_unprocessed_journal_entries(
        conn,
        scope_ids=[scope_a, scope_b],
        limit=10,
        per_session_limit=1,
        excluded_chat_ids={"isolated-chat"},
    )
    loaded_ids = [entry.id for entry in loaded]
    assert excluded_id not in loaded_ids
    by_pair = {}
    for entry in loaded:
        by_pair.setdefault((entry.scope_id, entry.session_id), []).append(entry.id)
    assert set(by_pair) == {(scope_a, shared_session), (scope_b, shared_session)}
    assert by_pair[(scope_a, shared_session)] == [a_ids[0]]
    assert by_pair[(scope_b, shared_session)] == [b_ids[0]]

    journal_store.upsert_session_digest_state(
        conn,
        scope_id=scope_a,
        session_id=shared_session,
        resume_after_id=a_ids[0],
        run_id="run-cursor",
    )
    after_cursor = journal_store.load_unprocessed_journal_entries(
        conn,
        scope_ids=[scope_a, scope_b],
        limit=10,
        per_session_limit=1,
        excluded_chat_ids={"isolated-chat"},
    )
    after_ids = [entry.id for entry in after_cursor]
    assert a_ids[1] in after_ids
    assert b_ids[0] in after_ids
    assert a_ids[0] not in after_ids

    journal_store.mark_entries_processed(conn, entry_ids=[a_ids[1], b_ids[0]], run_id="run-2")
    wrapped = journal_store.load_unprocessed_journal_entries(
        conn,
        scope_ids=[scope_a, scope_b],
        limit=10,
        per_session_limit=1,
        excluded_chat_ids={"isolated-chat"},
    )
    wrapped_ids = [entry.id for entry in wrapped]
    assert a_ids[0] in wrapped_ids
    assert b_ids[1] in wrapped_ids

    limited = journal_store.load_unprocessed_journal_entries(
        conn,
        scope_ids=[scope_a, scope_b],
        limit=1,
        per_session_limit=1,
        excluded_chat_ids={"isolated-chat"},
    )
    assert len(limited) == 1


def test_clear_current_deferral_keeps_defer_count(tmp_path):
    import sqlite3

    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    journal_store.ensure_journal_schema(conn)
    entry_id = _insert_loader_row(
        conn,
        scope_id="scope",
        session_id="session",
        turn=1,
        content="covered pending row must drop current deferral only.",
    )
    journal_store.mark_entries_deferred(conn, entry_ids=[entry_id], run_id="run-defer")
    journal_store.clear_current_deferral(conn, entry_ids=[entry_id], commit=False)
    conn.commit()
    row = conn.execute(
        "SELECT deferred_run_id, deferred_at, defer_count FROM journal_entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    assert str(row["deferred_run_id"] or "") == ""
    assert row["deferred_at"] is None
    assert int(row["defer_count"] or 0) == 1


def test_cursor_and_leave_writes_roll_back_together(tmp_path):
    import sqlite3

    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    journal_store.ensure_journal_schema(conn)
    first = _insert_loader_row(
        conn,
        scope_id="scope",
        session_id="session",
        turn=1,
        content="atomic first journal row for rollback seam.",
    )
    second = _insert_loader_row(
        conn,
        scope_id="scope",
        session_id="session",
        turn=2,
        content="atomic second journal row for rollback seam.",
    )
    entries = journal_store.load_unprocessed_journal_entries(
        conn, scope_ids=["scope"], limit=10
    )
    conn.execute("BEGIN")
    journal_store.increment_extraction_attempts(conn, entry_ids=[first], commit=False)
    journal_store.clear_current_deferral(conn, entry_ids=[first], commit=False)
    journal_store.mark_entries_deferred(conn, entry_ids=[second], run_id="run-x", commit=False)
    journal_store.mark_entries_processed(conn, entry_ids=[first], run_id="run-x", commit=False)
    journal_store.advance_session_digest_cursors(
        conn,
        entries=entries,
        covered_ids={first},
        deferred_ids={second},
        run_id="run-x",
        commit=False,
    )
    conn.execute(
        "INSERT INTO journal_rejections(journal_entry_id, run_id, reason, candidate, created_at) "
        "VALUES (?, 'run-x', 'no durable memory candidate', '', '2026-08-19T00:00:00+00:00')",
        (first,),
    )
    conn.rollback()
    row = conn.execute(
        "SELECT processed_run_id, deferred_run_id, defer_count, extraction_attempts "
        "FROM journal_entries WHERE id = ?",
        (first,),
    ).fetchone()
    other = conn.execute(
        "SELECT deferred_run_id, defer_count FROM journal_entries WHERE id = ?",
        (second,),
    ).fetchone()
    assert str(row["processed_run_id"] or "") == ""
    assert int(row["extraction_attempts"] or 0) == 0
    assert str(other["deferred_run_id"] or "") == ""
    assert int(other["defer_count"] or 0) == 0
    assert journal_store.load_session_digest_state(
        conn, scope_id="scope", session_id="session"
    ) is None
    assert conn.execute("SELECT COUNT(*) FROM journal_rejections").fetchone()[0] == 0

def test_journal_schema_adds_retryable_failures_and_increments_durably(tmp_path):
    import sqlite3

    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE journal_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_id TEXT NOT NULL,
            shared_scope_id TEXT NOT NULL,
            platform TEXT,
            user_id TEXT,
            chat_id TEXT,
            thread_id TEXT,
            gateway_session_key TEXT,
            agent_identity TEXT,
            agent_workspace TEXT,
            session_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL DEFAULT 0,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            processed_run_id TEXT NOT NULL DEFAULT '',
            processed_at TEXT,
            metadata TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    conn.commit()
    journal_store.ensure_journal_schema(conn)
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(journal_entries)")
    }
    assert "retryable_failures" in columns
    conn.execute(
        """
        INSERT INTO journal_entries(
            scope_id, shared_scope_id, session_id, turn_number, role, content,
            content_hash, created_at, processed_run_id, metadata
        ) VALUES ('scope', 'shared', 's', 1, 'user', 'durable retryable counter',
                  'hash-1', '2026-01-01T00:00:00+00:00', '', '{}')
        """
    )
    conn.commit()
    first = journal_store.increment_retryable_failures(conn, entry_ids=[1])
    second = journal_store.increment_retryable_failures(conn, entry_ids=[1])
    assert first[1] == 1
    assert second[1] == 2
    row = conn.execute(
        "SELECT retryable_failures, extraction_attempts FROM journal_entries WHERE id=1"
    ).fetchone()
    assert int(row["retryable_failures"]) == 2
    assert int(row["extraction_attempts"] or 0) == 0


def test_reset_retryable_failures_clears_only_requested_ids_without_commit(tmp_path):
    import sqlite3

    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    journal_store.ensure_journal_schema(conn)
    conn.executemany(
        """
        INSERT INTO journal_entries(
            scope_id, shared_scope_id, session_id, turn_number, role, content,
            content_hash, created_at, processed_run_id, metadata, retryable_failures
        ) VALUES (?, 'shared', 's', ?, 'user', ?, ?, '2026-01-01T00:00:00+00:00',
                  '', '{}', ?)
        """,
        [
            ("scope", 1, "reset attempted row", "hash-reset-1", 4),
            ("scope", 2, "leave deferred row", "hash-reset-2", 7),
        ],
    )
    conn.commit()
    conn.execute("BEGIN")
    cleared = journal_store.reset_retryable_failures(
        conn, entry_ids=[1], commit=False
    )
    assert cleared == {1: 0}
    in_txn = {
        int(row["id"]): int(row["retryable_failures"] or 0)
        for row in conn.execute(
            "SELECT id, retryable_failures FROM journal_entries ORDER BY id"
        )
    }
    assert in_txn == {1: 0, 2: 7}
    attempts = conn.execute(
        "SELECT extraction_attempts FROM journal_entries WHERE id=1"
    ).fetchone()
    assert int(attempts["extraction_attempts"] or 0) == 0
    conn.rollback()
    after_rollback = {
        int(row["id"]): int(row["retryable_failures"] or 0)
        for row in conn.execute(
            "SELECT id, retryable_failures FROM journal_entries ORDER BY id"
        )
    }
    assert after_rollback == {1: 4, 2: 7}
