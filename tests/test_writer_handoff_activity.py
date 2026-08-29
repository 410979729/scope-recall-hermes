"""Issue #58 durable-activity boundaries for idle writer handoff."""

from __future__ import annotations

import queue
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import scope_recall.capture as capture_module
from scope_recall._internal.journal.runtime import (
    append_scoped_journal_entries,
    run_provider_background_journal_digest,
)
from scope_recall._internal.runtime.writer_handoff import (
    initialize_writer_handoff_activity,
)
from scope_recall.journal_store import ensure_journal_schema
from scope_recall.models import RuntimeScope


class _ObservedRLock:
    """Expose when a non-creator thread reaches a held lifecycle lock."""

    def __init__(self) -> None:
        self._inner = threading.RLock()
        self._creator_thread = threading.get_ident()
        self.contender_waiting = threading.Event()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if threading.get_ident() != self._creator_thread:
            self.contender_waiting.set()
        return self._inner.acquire(blocking, timeout)

    def release(self) -> None:
        self._inner.release()

    def __enter__(self) -> _ObservedRLock:
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


def _set_old_truth_clock(provider: Any) -> tuple[float, int]:
    initialize_writer_handoff_activity(provider, reset=True)
    old_clock = 1.0
    with provider._writer_handoff_activity_lock:
        provider._writer_handoff_last_truth_activity = old_clock
        generation = int(provider._writer_handoff_activity_generation)
    return old_clock, generation


def _digest_provider(tmp_path: Path) -> SimpleNamespace:
    main_connection = sqlite3.connect(":memory:")
    return SimpleNamespace(
        _shutdown_requested=threading.Event(),
        _hermes_home=tmp_path,
        _foreground_busy_count=0,
        _journal_digest_lock=threading.RLock(),
        _journal_digest_consecutive_failures=0,
        _journal_digest_needs_resume=False,
        _last_journal_digest_status="never_run",
        _last_journal_digest_error="",
        _last_journal_digest_finished=0.0,
        _conn=main_connection,
        _memory_isolated_for_scope=lambda: False,
        _background_digest_scope=lambda: RuntimeScope(),
        _recover_sqlite_connection_after_error=lambda _context: {
            "recovered": False
        },
        _rollback_conn_after_error=lambda _context: None,
        _coerce_journal_float=lambda config, key, default: float(
            config.get(key, default)
        ),
        _background=SimpleNamespace(
            maybe_promote=lambda **_kwargs: None,
            maybe_adjudicate=lambda **_kwargs: None,
        ),
    )


def _run_single_digest(provider: Any, digest_fn: Any) -> None:
    run_provider_background_journal_digest(
        provider,
        {
            "extractor": "heuristic",
            "background_digest_drain_while_idle": False,
            "background_digest_synchronous": True,
            "background_digest_idle_pause_seconds": 0,
            "digest_interval_hours": 2,
        },
        digest_fn=digest_fn,
    )


def test_journal_append_cannot_cross_handoff_fence_after_lifecycle_precheck() -> (
    None
):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    ensure_journal_schema(connection)
    lifecycle = _ObservedRLock()
    provider = SimpleNamespace(
        _writer_lifecycle_lock=lifecycle,
        _truth_writer_role="owner",
        _writer_handoff_fenced=False,
        _shutdown_requested=threading.Event(),
        _lock=threading.RLock(),
        _conn=connection,
        _require_conn=lambda: connection,
        _scope=RuntimeScope(user_id="activity-test"),
        _scope_id="scope-local",
        _shared_scope_id="scope-shared",
        _session_id="journal-linearization",
    )
    provider._truth_writes_blocked = lambda: (
        provider._truth_writer_role != "owner"
        or provider._writer_handoff_fenced
    )
    result: list[int] = []
    finished = threading.Event()

    def append_after_precheck() -> None:
        result.append(
            append_scoped_journal_entries(
                provider,
                [
                    {
                        "turn_number": 1,
                        "role": "user",
                        "content": "This row must not cross the writer handoff fence.",
                    }
                ],
            )
        )
        finished.set()

    try:
        with lifecycle:
            worker = threading.Thread(target=append_after_precheck)
            worker.start()
            assert lifecycle.contender_waiting.wait(timeout=1.0)
            provider._writer_handoff_fenced = True
            provider._truth_writer_role = "reader"
            assert finished.is_set() is False

        worker.join(timeout=2.0)
        assert worker.is_alive() is False
        assert result == [0]
        count = connection.execute(
            "SELECT COUNT(*) FROM journal_entries"
        ).fetchone()[0]
        assert count == 0
    finally:
        connection.close()


def test_relation_maintenance_real_sqlite_mutation_refreshes_truth_activity(
    monkeypatch,
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE relation_activity_probe(value TEXT NOT NULL)")
    connection.commit()
    provider = SimpleNamespace(
        _writer_lifecycle_lock=threading.RLock(),
        _truth_writer_role="owner",
        _shutdown_requested=threading.Event(),
        _maintenance_stop=threading.Event(),
        _vector_store=None,
        _embedder=None,
        _vector_generation_id="",
        _config={
            "relation_extraction_enabled": True,
            "relation_rebuild_chunk_pairs": 1,
            "relation_maintenance_wall_clock_seconds": 0.05,
        },
        _write_queue=queue.Queue(),
        _capture_queue_processing=0,
        _lock=threading.RLock(),
        _conn=connection,
        _require_conn=lambda: connection,
        _relation_maintenance_lock_contention_skips=0,
        _relation_maintenance_consecutive_failures=0,
        _relation_maintenance_busy_skips=0,
    )
    old_clock, generation = _set_old_truth_clock(provider)

    def commit_relation_change(conn: sqlite3.Connection, **_kwargs: object):
        conn.execute(
            "INSERT INTO relation_activity_probe(value) VALUES (?)",
            ("committed",),
        )
        conn.commit()
        return {"failed": 0, "processed": 1}

    monkeypatch.setattr(
        capture_module,
        "drain_relation_frequency_work",
        commit_relation_change,
    )
    try:
        capture_module._drain_relation_rebuild_debt(provider)

        assert connection.execute(
            "SELECT value FROM relation_activity_probe"
        ).fetchall() == [("committed",)]
        assert provider._writer_handoff_last_truth_activity > old_clock
        assert provider._writer_handoff_activity_generation == generation + 1
    finally:
        connection.close()


def test_independent_digest_sqlite_mutation_refreshes_truth_activity(
    tmp_path,
) -> None:
    provider = _digest_provider(tmp_path)
    digest_database = tmp_path / "digest-truth.sqlite3"
    with sqlite3.connect(digest_database) as connection:
        connection.execute("CREATE TABLE digest_probe(value TEXT NOT NULL)")
    old_clock, generation = _set_old_truth_clock(provider)

    def mutate_with_independent_connection(**_kwargs: object) -> dict[str, object]:
        with sqlite3.connect(digest_database) as connection:
            connection.execute(
                "INSERT INTO digest_probe(value) VALUES (?)",
                ("committed",),
            )
        return {
            "ok": True,
            "status": "ok",
            "processed_entries": 1,
            "backlog_after": 0,
            "backlog_delta": -1,
        }

    try:
        _run_single_digest(provider, mutate_with_independent_connection)

        with sqlite3.connect(digest_database) as connection:
            assert connection.execute(
                "SELECT value FROM digest_probe"
            ).fetchall() == [("committed",)]
        assert provider._conn.total_changes == 0
        assert provider._writer_handoff_last_truth_activity > old_clock
        assert provider._writer_handoff_activity_generation == generation + 1
    finally:
        provider._conn.close()


def test_noop_digest_does_not_refresh_truth_activity(tmp_path) -> None:
    provider = _digest_provider(tmp_path)
    old_clock, generation = _set_old_truth_clock(provider)

    def no_op_digest(**_kwargs: object) -> dict[str, object]:
        return {
            "ok": True,
            "status": "ok",
            "processed_entries": 0,
            "backlog_after": 0,
            "backlog_delta": 0,
            "counts": {},
        }

    try:
        _run_single_digest(provider, no_op_digest)

        assert provider._writer_handoff_last_truth_activity == old_clock
        assert provider._writer_handoff_activity_generation == generation
    finally:
        provider._conn.close()
