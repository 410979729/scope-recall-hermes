from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from scope_recall.digest_durable_work import (
    journal_durable_health,
    journal_entry_descriptor,
    journal_entry_item,
    native_digest_lease_snapshot,
    nightly_durable_health,
    nightly_run_descriptor,
    nightly_run_item,
)
from scope_recall.journal_store import ensure_journal_schema
from scope_recall.nightly_digest import ensure_digest_schema


def _journal_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_journal_schema(conn)
    return conn


def _insert_journal_entry(
    conn: sqlite3.Connection,
    *,
    entry_id: int,
    created_at: str,
    processed_run_id: str = "",
    retryable_failures: int = 0,
    content: str = "journal secret body",
) -> None:
    conn.execute(
        """
        INSERT INTO journal_entries(
            id, scope_id, shared_scope_id, session_id, turn_number, role,
            content, content_hash, created_at, processed_run_id, processed_at,
            extraction_attempts, retryable_failures
        ) VALUES (?, 'scope-a', 'shared-a', 'session-a', ?, 'user', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry_id,
            entry_id,
            content,
            f"hash-{entry_id}",
            created_at,
            processed_run_id,
            created_at if processed_run_id else None,
            retryable_failures,
            retryable_failures,
        ),
    )


def test_journal_projection_preserves_run_identity_without_content():
    conn = _journal_connection()
    _insert_journal_entry(
        conn,
        entry_id=1,
        created_at="2026-08-27T10:00:00+00:00",
        processed_run_id="run-dead",
        content="TOP-SECRET-JOURNAL-CONTENT",
    )
    conn.execute(
        """
        INSERT INTO journal_digest_runs(
            id, started_at, finished_at, status, extractor, processed_entries,
            inserted, updated, skipped, metadata
        ) VALUES ('run-dead', '2026-08-27T10:00:00+00:00',
                  '2026-08-27T10:01:00+00:00', 'dead_letter', 'llm', 1, 0, 0, 1, '{}')
        """
    )
    conn.execute(
        """
        INSERT INTO journal_rejections(
            journal_entry_id, run_id, reason, candidate, created_at
        ) VALUES (1, 'run-dead', 'dead-letter:TOP-SECRET-ERROR', '',
                  '2026-08-27T10:01:00+00:00')
        """
    )
    conn.commit()

    descriptor = journal_entry_descriptor(conn, 1)
    item = journal_entry_item(conn, 1)

    assert descriptor is not None
    assert descriptor.frozen_upper_bound == 1
    assert item is not None
    assert item.state == "poisoned"
    assert item.receipt["processed_run_id"] == "run-dead"
    projected = json.dumps(
        {"descriptor": descriptor.as_dict(), "item": item.as_dict()}
    )
    assert "TOP-SECRET-JOURNAL-CONTENT" not in projected
    assert "TOP-SECRET-ERROR" not in projected
    conn.close()


def test_journal_health_unifies_retry_poison_age_and_fairness():
    conn = _journal_connection()
    _insert_journal_entry(
        conn,
        entry_id=1,
        created_at="2026-08-27T11:00:00+00:00",
        retryable_failures=2,
    )
    _insert_journal_entry(
        conn,
        entry_id=2,
        created_at="2026-08-27T10:00:00+00:00",
        processed_run_id="run-dead",
    )
    _insert_journal_entry(
        conn,
        entry_id=3,
        created_at="2026-08-27T09:00:00+00:00",
        processed_run_id="run-ok",
    )
    conn.executemany(
        """
        INSERT INTO journal_digest_runs(
            id, started_at, finished_at, status, extractor, processed_entries,
            inserted, updated, skipped, metadata
        ) VALUES (?, ?, ?, ?, 'llm', 1, 0, 0, 1, '{}')
        """,
        [
            (
                "run-dead",
                "2026-08-27T10:00:00+00:00",
                "2026-08-27T10:01:00+00:00",
                "dead_letter",
            ),
            (
                "run-ok",
                "2026-08-27T11:30:00+00:00",
                "2026-08-27T11:31:00+00:00",
                "ok",
            ),
        ],
    )
    conn.execute(
        """
        INSERT INTO journal_rejections(
            journal_entry_id, run_id, reason, candidate, created_at
        ) VALUES (2, 'run-dead', 'dead-letter:auth', '',
                  '2026-08-27T10:01:00+00:00')
        """
    )
    conn.execute(
        """
        INSERT INTO memory_journal_sources(
            memory_id, journal_entry_id, run_id, created_at
        ) VALUES ('memory-ok', 3, 'run-ok', '2026-08-27T11:31:00+00:00')
        """
    )
    conn.commit()
    before = conn.total_changes
    conn.execute("PRAGMA query_only=ON")

    health = journal_durable_health(
        conn,
        now=datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc),
    )

    assert health["state"] == "needs_repair"
    assert health["item_counts"]["retry"] == 1
    assert health["item_counts"]["poisoned"] == 1
    assert health["item_counts"]["completed"] == 1
    assert health["oldest_age_seconds"] == 7200.0
    assert health["fairness"]["pending_session_count"] == 1
    assert health["lease"]["lease_token_persisted"] is False
    assert conn.total_changes == before
    conn.close()


def _nightly_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_digest_schema(conn)
    return conn


def _insert_nightly_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    started_at: str,
    status: str,
    source_db: str,
    metadata: dict[str, object] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO nightly_digest_runs(
            id, digest_date, source_db, started_at, finished_at, extractor,
            model, dry_run, status, inserted, updated, skipped, deleted,
            error, metadata
        ) VALUES (?, '2026-08-27', ?, ?, ?, 'llm', 'fixture', 0, ?, 0, 0, 0, 0,
                  'TOP-SECRET-NIGHTLY-ERROR', ?)
        """,
        (
            run_id,
            source_db,
            started_at,
            "2026-08-27T11:59:00+00:00",
            status,
            json.dumps(metadata or {}, sort_keys=True),
        ),
    )


def test_nightly_projection_hashes_local_source_and_classifies_retry():
    conn = _nightly_connection()
    private_source = r"C:\Users\operator\private\state.db"
    _insert_nightly_run(
        conn,
        run_id="nightly-retry",
        started_at="2026-08-27T11:00:00+00:00",
        status="error",
        source_db=private_source,
        metadata={
            "extractor_fallbacks": [
                {"kind": "timeout", "retryable": True, "attempts": 2}
            ]
        },
    )
    conn.commit()

    descriptor = nightly_run_descriptor(conn, "nightly-retry")
    item = nightly_run_item(conn, "nightly-retry")
    health = nightly_durable_health(
        conn,
        now=datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc),
    )

    assert descriptor is not None
    assert item is not None
    assert item.state == "retry"
    assert item.attempt == 2
    assert health["state"] == "degraded"
    assert health["reason_code"] == "latest_run_retry"
    assert health["oldest_age_seconds"] == 3600.0
    projected = json.dumps(
        {"descriptor": descriptor.as_dict(), "item": item.as_dict()}
    )
    assert private_source not in projected
    assert "TOP-SECRET-NIGHTLY-ERROR" not in projected
    assert item.receipt["source_db_hash"]
    conn.close()


def test_nightly_health_uses_latest_run_and_respects_handled_fallback():
    conn = _nightly_connection()
    _insert_nightly_run(
        conn,
        run_id="old-error",
        started_at="2026-08-27T09:00:00+00:00",
        status="error",
        source_db="old.db",
    )
    _insert_nightly_run(
        conn,
        run_id="latest-ok",
        started_at="2026-08-27T11:00:00+00:00",
        status="ok_with_fallback",
        source_db="current.db",
        metadata={"operator_classification": "accepted_fallback"},
    )
    conn.commit()

    health = nightly_durable_health(conn)

    assert health["state"] == "ready"
    assert health["latest_run_id"] == "latest-ok"
    assert health["item_counts"]["completed"] == 1
    assert health["item_counts"]["poisoned"] == 0
    conn.close()


def test_native_lease_snapshot_is_role_only_and_does_not_invent_tokens(tmp_path):
    storage = tmp_path / "scope-recall"
    storage.mkdir()
    private_value = r"C:\Users\operator\private"
    (storage / ".truth-writer.lease.info").write_text(
        json.dumps({"role": "journal_digest", "path": private_value}),
        encoding="utf-8",
    )

    lease = native_digest_lease_snapshot(
        storage,
        domain_roles={"journal_digest"},
    )

    assert lease["state"] == "owner_hint_present"
    assert lease["owner_role"] == "journal_digest"
    assert lease["owner_matches_domain"] is True
    assert lease["lease_token_persisted"] is False
    assert private_value not in json.dumps(lease)
