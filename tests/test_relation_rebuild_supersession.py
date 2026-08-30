"""Concurrency regressions for relation-rebuild lease supersession."""

from __future__ import annotations

# Historical counterexamples for the removed legacy worker. The executable
# retirement contract is covered by test_relation_rebuild_retirement.py and the
# release AST gate; these fixtures remain readable but are no longer collected.
__test__ = False

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

import scope_recall.relation_extraction as relation_extraction
from scope_recall.relation_frequency_index import sync_relation_frequency_memory
from scope_recall.relation_rebuild_queue import (
    claim_relation_rebuild_events,
    drain_relation_rebuild_queue,
    enqueue_relation_rebuild,
)
from scope_recall.sql_store import ensure_schema, store_row


def _connect(path: Path, *, cross_thread: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5, check_same_thread=not cross_thread)
    conn.row_factory = sqlite3.Row
    return conn


def _store(
    conn: sqlite3.Connection, *, memory_id: str, content: str, updated_at: str
) -> None:
    store_row(
        conn,
        memory_id=memory_id,
        scope_id="scope-a",
        platform="test",
        user_id="joy",
        chat_id="",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="test",
        session_id="session-a",
        source="fixture",
        target="project",
        content=content,
        metadata='{"entities":["Project Atlas","Redis"]}',
        allow_duplicate=True,
        timestamp=updated_at,
    )


def test_new_revision_supersedes_old_worker_without_failure_or_dead_letter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "relation-supersession.sqlite3"
    worker_conn = _connect(db_path, cross_thread=True)
    ensure_schema(worker_conn)
    _store(
        worker_conn,
        memory_id="focus",
        content="Project Atlas depends on Redis.",
        updated_at="2026-07-20T00:00:00+00:00",
    )
    _store(
        worker_conn,
        memory_id="peer",
        content="Redis is required by Project Atlas.",
        updated_at="2026-07-20T00:00:00+00:00",
    )
    worker_conn.execute("DELETE FROM relation_rebuild_queue")
    enqueue_relation_rebuild(
        worker_conn,
        scope_id="scope-a",
        focus_memory_id="focus",
        requested_updated_at="2026-07-20T00:00:00+00:00",
        reason="old revision",
        commit=True,
    )

    rebuild_started = threading.Event()
    allow_rebuild = threading.Event()
    original_rebuild = relation_extraction.rebuild_extracted_relations
    call_count = 0
    call_lock = threading.Lock()

    def block_first_rebuild(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal call_count
        with call_lock:
            call_count += 1
            current_call = call_count
        if current_call == 1:
            rebuild_started.set()
            if not allow_rebuild.wait(timeout=5):
                raise RuntimeError("timed out waiting to supersede relation rebuild")
            return {
                "ok": True,
                "compared_pair_count": 1,
                "candidate_count": 0,
                "inserted": 0,
                "deleted": 0,
            }
        return original_rebuild(*args, **kwargs)

    monkeypatch.setattr(
        relation_extraction,
        "rebuild_extracted_relations",
        block_first_rebuild,
    )
    result: dict[str, int] = {}
    worker_errors: list[BaseException] = []

    def worker() -> None:
        try:
            result.update(
                drain_relation_rebuild_queue(
                    worker_conn,
                    max_events=2,
                    pair_limit=1,
                    worker_id="worker-old",
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            worker_errors.append(exc)

    thread = threading.Thread(target=worker, name="relation-superseded-worker")
    thread.start()
    assert rebuild_started.wait(timeout=3)

    writer_conn = _connect(db_path)
    try:
        enqueue_relation_rebuild(
            writer_conn,
            scope_id="scope-a",
            focus_memory_id="focus",
            requested_updated_at="2026-07-20T00:00:01+00:00",
            reason="new revision",
            commit=True,
        )
    finally:
        writer_conn.close()
        allow_rebuild.set()

    thread.join(timeout=10)
    assert not thread.is_alive()
    assert worker_errors == []
    assert result == {
        "claimed": 2,
        "chunks_completed": 2,
        "events_completed": 1,
        "superseded": 0,
        "failed": 0,
        "dead_lettered": 0,
    }

    row = worker_conn.execute(
        """
        SELECT requested_updated_at, status, cursor_memory_id, processed_pairs,
               attempts, failures, lease_owner, last_error
        FROM relation_rebuild_queue
        WHERE scope_id='scope-a' AND focus_memory_id='focus'
        """
    ).fetchone()
    assert tuple(row) == (
        "2026-07-20T00:00:01+00:00",
        "completed",
        "peer",
        2,
        2,
        0,
        "",
        "",
    )
    worker_conn.close()


def test_owned_rebuild_exception_still_counts_as_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Supersession handling must not hide an error while the worker owns the lease."""

    db_path = tmp_path / "relation-owned-failure.sqlite3"
    conn = _connect(db_path)
    ensure_schema(conn)
    _store(
        conn,
        memory_id="focus",
        content="Project Atlas depends on Redis.",
        updated_at="2026-07-20T00:00:00+00:00",
    )
    _store(
        conn,
        memory_id="peer",
        content="Redis is required by Project Atlas.",
        updated_at="2026-07-20T00:00:00+00:00",
    )
    conn.execute("DELETE FROM relation_rebuild_queue")
    enqueue_relation_rebuild(
        conn,
        scope_id="scope-a",
        focus_memory_id="focus",
        requested_updated_at="2026-07-20T00:00:00+00:00",
        reason="owned failure",
        commit=True,
    )

    def fail_rebuild(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("injected owned rebuild failure")

    monkeypatch.setattr(
        relation_extraction,
        "rebuild_extracted_relations",
        fail_rebuild,
    )
    result = drain_relation_rebuild_queue(
        conn,
        max_events=1,
        pair_limit=1,
        max_failures=2,
        worker_id="worker-owner",
    )
    assert result == {
        "claimed": 1,
        "chunks_completed": 0,
        "events_completed": 0,
        "superseded": 0,
        "failed": 1,
        "dead_lettered": 0,
    }
    row = conn.execute(
        """
        SELECT status, failures, lease_owner, last_error
        FROM relation_rebuild_queue
        WHERE scope_id='scope-a' AND focus_memory_id='focus'
        """
    ).fetchone()
    assert tuple(row[:3]) == ("retry", 1, "")
    assert "injected owned rebuild failure" in str(row["last_error"])
    conn.close()


@pytest.mark.parametrize("late_outcome", ["success", "failure"])
def test_same_worker_id_cannot_aba_across_relation_rebuild_leases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    late_outcome: str,
) -> None:
    """A reused public worker id must not authorize an old revision's CAS."""

    db_path = tmp_path / f"relation-worker-aba-{late_outcome}.sqlite3"
    old_conn = _connect(db_path, cross_thread=True)
    ensure_schema(old_conn)
    _store(
        old_conn,
        memory_id="focus",
        content="Project Atlas depends on Redis.",
        updated_at="2026-07-20T00:00:00+00:00",
    )
    _store(
        old_conn,
        memory_id="peer",
        content="Redis is required by Project Atlas.",
        updated_at="2026-07-20T00:00:00+00:00",
    )
    old_conn.execute("DELETE FROM relation_rebuild_queue")
    enqueue_relation_rebuild(
        old_conn,
        scope_id="scope-a",
        focus_memory_id="focus",
        requested_updated_at="rev-1",
        reason="old revision",
        commit=True,
    )

    rebuild_started = threading.Event()
    release_old_worker = threading.Event()

    def delayed_old_rebuild(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        rebuild_started.set()
        if not release_old_worker.wait(timeout=5):
            raise RuntimeError("timed out waiting for same-worker ABA probe")
        if late_outcome == "failure":
            raise RuntimeError("late old-revision exception")
        return {
            "ok": True,
            "compared_pair_count": 1,
            "candidate_count": 0,
            "inserted": 0,
            "deleted": 0,
        }

    monkeypatch.setattr(
        relation_extraction,
        "rebuild_extracted_relations",
        delayed_old_rebuild,
    )
    result: dict[str, int] = {}
    worker_errors: list[BaseException] = []

    def old_worker() -> None:
        try:
            result.update(
                drain_relation_rebuild_queue(
                    old_conn,
                    max_events=1,
                    pair_limit=1,
                    max_failures=1,
                    worker_id="stable-worker",
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            worker_errors.append(exc)

    thread = threading.Thread(target=old_worker, name=f"relation-aba-{late_outcome}")
    thread.start()
    assert rebuild_started.wait(timeout=3)

    replacement_conn = _connect(db_path)
    try:
        enqueue_relation_rebuild(
            replacement_conn,
            scope_id="scope-a",
            focus_memory_id="focus",
            requested_updated_at="rev-2",
            reason="replacement revision",
            commit=True,
        )
        # A newer revision is queued behind the immutable active lease; reusing
        # the public worker id must not make the old token claimable as new work.
        assert claim_relation_rebuild_events(
            replacement_conn,
            worker_id="stable-worker",
            limit=1,
            commit=True,
        ) == []
    finally:
        replacement_conn.close()
        release_old_worker.set()

    thread.join(timeout=10)
    assert not thread.is_alive()
    assert worker_errors == []
    if late_outcome == "success":
        assert result == {
            "claimed": 1,
            "chunks_completed": 1,
            "events_completed": 0,
            "superseded": 0,
            "failed": 0,
            "dead_lettered": 0,
        }
        expected_failures = 0
    else:
        assert result == {
            "claimed": 1,
            "chunks_completed": 0,
            "events_completed": 0,
            "superseded": 0,
            "failed": 1,
            "dead_lettered": 0,
        }
        expected_failures = 1

    replacement_conn = _connect(db_path)
    try:
        replacement_claim = claim_relation_rebuild_events(
            replacement_conn,
            worker_id="stable-worker",
            limit=1,
            commit=True,
        )
        assert len(replacement_claim) == 1
        assert replacement_claim[0]["requested_updated_at"] == "rev-2"
        assert replacement_claim[0]["lease_token"]
    finally:
        replacement_conn.close()

    row = old_conn.execute(
        """
        SELECT requested_updated_at, status, attempts, failures,
               lease_owner, last_error
        FROM relation_rebuild_queue
        WHERE scope_id='scope-a' AND focus_memory_id='focus'
        """
    ).fetchone()
    assert tuple(row[:5]) == (
        "rev-2",
        "processing",
        2,
        expected_failures,
        "stable-worker",
    )
    old_conn.close()


def test_relation_rebuild_reuses_one_scope_frequency_receipt_across_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queue chunks must consume the incremental receipt without truth scans."""

    db_path = tmp_path / "relation-frequency-receipt.sqlite3"
    conn = _connect(db_path)
    ensure_schema(conn)
    _store(
        conn,
        memory_id="focus",
        content="Project Atlas depends on Redis.",
        updated_at="2026-07-20T00:00:00+00:00",
    )
    for index in range(5):
        _store(
            conn,
            memory_id=f"peer-{index}",
            content=f"Peer fixture {index} records an unrelated operational fact.",
            updated_at=f"2026-07-20T00:00:0{index + 1}+00:00",
        )
    conn.execute("DELETE FROM relation_rebuild_queue")
    enqueue_relation_rebuild(
        conn,
        scope_id="scope-a",
        focus_memory_id="focus",
        requested_updated_at="2026-07-20T00:00:00+00:00",
        reason="frequency receipt",
        commit=True,
    )

    original = relation_extraction.scope_high_frequency_relation_entities
    scan_calls = 0

    def count_scope_scan(*args: Any, **kwargs: Any) -> set[str]:
        nonlocal scan_calls
        scan_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        relation_extraction,
        "scope_high_frequency_relation_entities",
        count_scope_scan,
    )
    result = drain_relation_rebuild_queue(
        conn,
        max_events=4,
        pair_limit=2,
        worker_id="frequency-worker",
    )

    assert result["events_completed"] == 1
    assert scan_calls == 0
    row = conn.execute(
        "SELECT status, processed_pairs FROM relation_rebuild_queue WHERE focus_memory_id='focus'"
    ).fetchone()
    assert tuple(row) == ("completed", 5)
    conn.close()


def test_relation_rebuild_progress_survives_scope_corpus_revision_changes(
    tmp_path: Path,
) -> None:
    """Peer writes must not reset the active focus pass or lifetime counters."""

    db_path = tmp_path / "relation-scope-revision.sqlite3"
    conn = _connect(db_path)
    ensure_schema(conn)
    _store(
        conn,
        memory_id="focus",
        content="Project Atlas depends on Redis.",
        updated_at="2026-07-20T00:00:00+00:00",
    )
    for index in range(3):
        _store(
            conn,
            memory_id=f"peer-{index}",
            content=f"Peer fixture {index} records a stable operational fact.",
            updated_at=f"2026-07-20T00:00:0{index + 1}+00:00",
        )
    conn.execute("DELETE FROM relation_rebuild_queue")
    enqueue_relation_rebuild(
        conn,
        scope_id="scope-a",
        focus_memory_id="focus",
        requested_updated_at="2026-07-20T00:00:00+00:00",
        reason="scope revision",
        commit=True,
    )

    first = drain_relation_rebuild_queue(
        conn,
        max_events=1,
        pair_limit=1,
        worker_id="scope-revision-worker",
    )
    assert first["chunks_completed"] == 1
    conn.execute(
        "UPDATE memories SET content=?, updated_at=? WHERE id='peer-2'",
        (
            "Peer fixture 2 changed while the focus event was chunking.",
            "2026-07-20T00:00:10+00:00",
        ),
    )
    sync_relation_frequency_memory(conn, "peer-2")
    enqueue_relation_rebuild(
        conn,
        scope_id="scope-a",
        focus_memory_id="peer-2",
        requested_updated_at="2026-07-20T00:00:10+00:00",
        reason="peer changed during focus pass",
        commit=False,
    )
    conn.commit()

    second = drain_relation_rebuild_queue(
        conn,
        max_events=1,
        pair_limit=1,
        worker_id="scope-revision-worker",
    )
    row = conn.execute(
        "SELECT status, cursor_memory_id, processed_pairs, attempts FROM relation_rebuild_queue WHERE focus_memory_id='focus'"
    ).fetchone()

    assert second["superseded"] == 0
    assert second["chunks_completed"] == 1
    assert tuple(row) == ("pending", "peer-1", 2, 2)
    conn.close()
