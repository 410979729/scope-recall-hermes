"""Issue #60 accident-shape regressions for bounded relation maintenance."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import queue
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.memory import load_memory_provider

import scope_recall.capture as capture
import scope_recall.relation_containment as containment
import scope_recall.relation_extraction as relation_extraction
import scope_recall.relation_frequency_maintenance as maintenance
import scope_recall._internal.runtime.writer_handoff as writer_handoff
from scope_recall.relation_containment import (
    complete_relation_focus_work,
    enqueue_relation_focus_work,
)
from scope_recall.sql_store import ensure_schema


_POISON_ID = "a-issue-60-poison"
_HEALTHY_ID = "z-issue-60-healthy"
_SCOPE_ID = "issue-60-scope"
_POISON_INITIAL_ATTEMPTS = 1_667
_EARLY = datetime(2026, 8, 29, 0, 0, tzinfo=timezone.utc)
_FUTURE = datetime(2026, 8, 29, 0, 1, tzinfo=timezone.utc)
_DUE = datetime(2026, 8, 29, 0, 2, tzinfo=timezone.utc)
_DETAILS_SCHEMA = "scope-recall.issue-60-regression.v1"
_DETAILS_OUTPUT_ENV = "SCOPE_RECALL_ISSUE_60_DETAILS_OUTPUT"
_OBSERVATIONS: dict[str, object] = {}


def _publish_details() -> None:
    output = str(os.environ.get(_DETAILS_OUTPUT_ENV) or "").strip()
    if not output:
        return
    expected = {
        "poison_initial_attempts",
        "early_retry_count",
        "terminal_revive_count",
        "healthy_item_completed",
        "legacy_queue_mutation_count",
        "simulated_seconds",
        "maintenance_transactions",
        "prefetch_timeout_observed",
        "prefetch_max_wait_ms",
    }
    assert set(_OBSERVATIONS) == expected
    Path(output).write_text(
        json.dumps(
            {
                "schema_version": _DETAILS_SCHEMA,
                **_OBSERVATIONS,
                "active_instance_touched": False,
                "result": "passed",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _insert_memory(conn: sqlite3.Connection, memory_id: str) -> None:
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES(?, ?, 'issue-60-regression', 'project', ?, ?, ?, ?, '{}')
        """,
        (
            memory_id,
            _SCOPE_ID,
            f"Issue 60 finite relation item {memory_id}",
            memory_id,
            _EARLY.isoformat(),
            _EARLY.isoformat(),
        ),
    )


def _legacy_snapshot(conn: sqlite3.Connection) -> tuple[object, ...]:
    row = conn.execute(
        """
        SELECT id, scope_id, focus_memory_id, requested_updated_at, reason,
               status, attempts, available_at, lease_owner, lease_token,
               COALESCE(lease_expires_at, ''), updated_at
        FROM relation_rebuild_queue
        """
    ).fetchone()
    assert row is not None
    return tuple(row)


def _legacy_mutations(statements: list[str]) -> list[str]:
    mutations: list[str] = []
    for statement in statements:
        normalized = " ".join(statement.casefold().split())
        if "relation_rebuild_queue" not in normalized:
            continue
        if normalized.startswith(("insert ", "update ", "delete ", "replace ")):
            mutations.append(normalized)
    return mutations


def _drain(conn: sqlite3.Connection) -> dict[str, Any]:
    return maintenance.drain_relation_frequency_work(
        conn,
        change_limit=0,
        focus_limit=2,
        backfill_limit=1,
        relation_candidate_cap=8,
        relation_max_attempts=20,
        wall_clock_seconds=0.5,
        deadline_monotonic=100.5,
        clock=lambda: 100.0,
        commit=True,
    )


def test_issue_60_retry_due_time_is_bounded_and_healthy_work_is_not_starved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 1667-attempt item waits until due, terminates, and never revives."""

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    for memory_id in (_POISON_ID, _HEALTHY_ID):
        _insert_memory(conn, memory_id)
    for table in (
        "relation_frequency_changes",
        "relation_frequency_failures",
        "relation_frequency_backfill",
        "relation_entity_postings",
        "relation_indexed_memories",
        "relation_scope_entity_frequency",
    ):
        conn.execute(f"DELETE FROM {table}")
    assert enqueue_relation_focus_work(
        conn,
        memory_id=_POISON_ID,
        work_generation=1,
        work_revision="issue-60-poison-revision",
        scope_ids=[_SCOPE_ID],
        max_attempts=20,
    )
    assert enqueue_relation_focus_work(
        conn,
        memory_id=_HEALTHY_ID,
        work_generation=1,
        work_revision="issue-60-healthy-revision",
        scope_ids=[_SCOPE_ID],
        max_attempts=20,
    )
    conn.execute(
        """
        UPDATE relation_focus_work
        SET status='retry', attempts=?, next_attempt_at=?, updated_at=?
        WHERE memory_id=?
        """,
        (
            _POISON_INITIAL_ATTEMPTS,
            _FUTURE.isoformat(),
            _EARLY.isoformat(),
            _POISON_ID,
        ),
    )
    conn.execute(
        """
        INSERT INTO relation_rebuild_queue(
            scope_id, focus_memory_id, requested_updated_at, reason,
            status, attempts, available_at, lease_owner, lease_token,
            lease_expires_at, created_at, updated_at
        ) VALUES(?, ?, 'issue-60-old-revision', 'retired issue 60 fixture',
                 'pending', ?, ?, 'legacy-worker', 'legacy-token', ?, ?, ?)
        """,
        (
            _SCOPE_ID,
            _POISON_ID,
            _POISON_INITIAL_ATTEMPTS,
            _EARLY.isoformat(),
            _EARLY.isoformat(),
            _EARLY.isoformat(),
            _EARLY.isoformat(),
        ),
    )
    conn.commit()
    legacy_before = _legacy_snapshot(conn)
    wall_now = [_EARLY]
    monkeypatch.setattr(maintenance, "_now_iso", lambda: wall_now[0].isoformat())
    monkeypatch.setattr(containment, "_now", lambda: wall_now[0])
    monkeypatch.setattr(containment, "_now_iso", lambda: wall_now[0].isoformat())
    sync_calls: list[str] = []

    def controlled_sync(
        target: sqlite3.Connection,
        *,
        memory_id: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        sync_calls.append(memory_id)
        if memory_id == _POISON_ID:
            raise RuntimeError("injected issue 60 poison relation item")
        assert complete_relation_focus_work(
            target,
            memory_id=memory_id,
            work_generation=1,
        )
        return {"status": "synced"}

    monkeypatch.setattr(
        relation_extraction,
        "sync_extracted_relations_for_memory",
        controlled_sync,
    )
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    early_result = _drain(conn)
    early_poison = conn.execute(
        "SELECT status, attempts, next_attempt_at FROM relation_focus_work WHERE memory_id=?",
        (_POISON_ID,),
    ).fetchone()
    assert tuple(early_poison) == (
        "retry",
        _POISON_INITIAL_ATTEMPTS,
        _FUTURE.isoformat(),
    )
    assert early_result["focused_memories"] == 1
    assert sync_calls == [_HEALTHY_ID]
    assert conn.execute(
        "SELECT 1 FROM relation_focus_work WHERE memory_id=?", (_HEALTHY_ID,)
    ).fetchone() is None

    wall_now[0] = _DUE
    due_result = _drain(conn)
    terminal = conn.execute(
        "SELECT status, attempts, next_attempt_at FROM relation_focus_work WHERE memory_id=?",
        (_POISON_ID,),
    ).fetchone()
    assert tuple(terminal) == ("dead_letter", _POISON_INITIAL_ATTEMPTS + 1, "")
    assert due_result["focus_dead_letter_failures"] == 1
    assert sync_calls == [_HEALTHY_ID, _POISON_ID]

    for _ in range(3):
        _drain(conn)
    conn.set_trace_callback(None)
    terminal_after_idle = conn.execute(
        "SELECT status, attempts FROM relation_focus_work WHERE memory_id=?",
        (_POISON_ID,),
    ).fetchone()
    assert tuple(terminal_after_idle) == (
        "dead_letter",
        _POISON_INITIAL_ATTEMPTS + 1,
    )
    assert sync_calls == [_HEALTHY_ID, _POISON_ID]
    assert _legacy_snapshot(conn) == legacy_before
    assert _legacy_mutations(statements) == []
    _OBSERVATIONS.update(
        {
            "poison_initial_attempts": _POISON_INITIAL_ATTEMPTS,
            "early_retry_count": 0,
            "terminal_revive_count": 0,
            "healthy_item_completed": True,
            "legacy_queue_mutation_count": len(_legacy_mutations(statements)),
        }
    )
    conn.close()


class _TickQueue:
    def __init__(self, tick_count: int, observed_now: list[float]) -> None:
        self._remaining = tick_count
        self._tick_count = tick_count
        self._observed_now = observed_now
        self.task_done_count = 0

    def get(self, *, timeout: float) -> object | None:
        assert timeout == 0.2
        if self._remaining:
            self._observed_now[0] = float(self._tick_count - self._remaining)
            self._remaining -= 1
            raise queue.Empty
        return None

    def task_done(self) -> None:
        self.task_done_count += 1


def test_issue_60_sixty_one_idle_ticks_do_not_restore_one_second_hammer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real maintenance opens two transactions in 61 ticks, not one per tick."""

    observed_now = [-1.0]
    write_queue = _TickQueue(61, observed_now)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    conn.commit()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    lifecycle_lock = threading.RLock()
    provider = SimpleNamespace(
        _write_queue=write_queue,
        _stop=threading.Event(),
        _maintenance_stop=threading.Event(),
        _last_relation_rebuild_drain=0.0,
        _relation_maintenance_consecutive_failures=0,
        _relation_maintenance_lock_contention_skips=0,
        _capture_queue_processing=0,
        _truth_writer_role="owner",
        _shutdown_requested=threading.Event(),
        _writer_lifecycle_lock=lifecycle_lock,
        _lock=threading.RLock(),
        _conn=conn,
        _require_conn=lambda: conn,
        _config={
            "relation_maintenance_interval_seconds": 30.0,
            "relation_maintenance_backoff_base_seconds": 5.0,
            "relation_maintenance_backoff_max_seconds": 300.0,
            "relation_maintenance_wall_clock_seconds": 0.05,
            "relation_rebuild_chunk_pairs": 1,
            "relation_extraction_enabled": True,
        },
    )
    monkeypatch.setattr(capture.time, "monotonic", lambda: observed_now[0])
    monkeypatch.setattr(
        writer_handoff,
        "maybe_schedule_idle_writer_handoff",
        lambda _provider: False,
    )
    try:
        capture.writer_loop(provider)
    finally:
        conn.set_trace_callback(None)
        conn.close()

    begin_statements = [
        statement
        for statement in statements
        if " ".join(statement.casefold().split()) == "begin"
    ]
    assert len(begin_statements) == 2
    assert len(begin_statements) < 61
    assert provider._last_relation_rebuild_drain == 60.0
    assert write_queue.task_done_count == 1
    _OBSERVATIONS.update(
        {
            "simulated_seconds": 61,
            "maintenance_transactions": len(begin_statements),
        }
    )


class _ObservedRLock:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.maintenance_holding = threading.Event()
        self.prefetch_waiting = threading.Event()
        self.maintenance_thread_id = 0

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if (
            self.maintenance_holding.is_set()
            and threading.get_ident() != self.maintenance_thread_id
        ):
            self.prefetch_waiting.set()
        if timeout == -1:
            return self._lock.acquire(blocking)
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> _ObservedRLock:
        assert self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _write_provider_config(hermes_home: Path) -> None:
    path = hermes_home / "scope-recall" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "vector": {"enabled": False},
                "relation_extraction_enabled": True,
                "relation_maintenance_interval_seconds": 3600.0,
                "relation_maintenance_wall_clock_seconds": 0.05,
                "experience": {"enabled": True, "prefetch_enabled": True},
            }
        ),
        encoding="utf-8",
    )


def test_issue_60_maintenance_is_nonblocking_and_prefetch_is_bounded_zero_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded maintenance lock section cannot impose the old 8s prefetch wait."""

    _write_provider_config(tmp_path)
    provider = load_memory_provider("scope-recall")
    assert provider is not None
    provider.initialize(
        "issue-60",
        hermes_home=str(tmp_path),
        platform="cli",
        user_id="issue-60-user",
        chat_id="issue-60-chat",
        agent_identity="issue-60-test",
        agent_workspace="hermes",
        agent_context="primary",
    )
    observed_lock = _ObservedRLock()
    provider._lock = observed_lock
    provider._last_relation_rebuild_drain = time.monotonic()
    runtime_provider_module = sys.modules[type(provider).__module__]
    maintenance_started = threading.Event()
    maintenance_finished = threading.Event()
    release_maintenance = threading.Event()
    drain_calls: list[dict[str, float]] = []

    def bounded_drain(
        _conn: sqlite3.Connection,
        **kwargs: object,
    ) -> dict[str, object]:
        wall_clock_seconds = float(kwargs["wall_clock_seconds"])
        drain_calls.append(
            {
                "wall_clock_seconds": wall_clock_seconds,
                "deadline_monotonic": float(kwargs["deadline_monotonic"]),
            }
        )
        observed_lock.maintenance_thread_id = threading.get_ident()
        observed_lock.maintenance_holding.set()
        maintenance_started.set()
        release_maintenance.wait(timeout=wall_clock_seconds)
        observed_lock.maintenance_holding.clear()
        return {
            "changed_memories": 0,
            "focused_memories": 0,
            "backfilled_memories": 0,
            "failed": 0,
        }

    monkeypatch.setattr(capture, "drain_relation_frequency_work", bounded_drain)
    monkeypatch.setattr(
        runtime_provider_module,
        "render_current_turn_recall",
        lambda _provider, _query: "",
    )
    monkeypatch.setattr(
        runtime_provider_module,
        "run_experience_preflight",
        lambda _provider, *, query: {"packet": f"prefetched:{query}"},
    )
    prefetch_result: dict[str, object] = {}

    def run_maintenance() -> None:
        try:
            capture._drain_relation_rebuild_debt(provider)
        finally:
            maintenance_finished.set()

    def run_prefetch() -> None:
        started = time.perf_counter()
        prefetch_result["text"] = provider.prefetch("issue-60-query")
        prefetch_result["wait_seconds"] = time.perf_counter() - started

    maintenance_thread = threading.Thread(target=run_maintenance, daemon=True)
    prefetch_thread = threading.Thread(target=run_prefetch, daemon=True)
    try:
        changes_before = provider._require_conn().total_changes
        maintenance_thread.start()
        assert maintenance_started.wait(timeout=0.5)
        prefetch_thread.start()
        assert observed_lock.prefetch_waiting.wait(timeout=0.5)
        assert maintenance_finished.wait(timeout=0.5)
        maintenance_thread.join(timeout=0.5)
        prefetch_thread.join(timeout=0.5)
        assert not maintenance_thread.is_alive()
        assert not prefetch_thread.is_alive()
        assert prefetch_result["text"] == "prefetched:issue-60-query"
        wait_seconds = float(prefetch_result["wait_seconds"])
        assert wait_seconds <= 0.55
        assert len(drain_calls) == 1
        assert drain_calls[0]["wall_clock_seconds"] == 0.05
        assert provider._require_conn().total_changes == changes_before

        # With a foreground query lock already held by another thread,
        # maintenance must return immediately without entering the drain.
        nonblocking_done = threading.Event()
        drain_count_before = len(drain_calls)
        started = time.perf_counter()

        def contend() -> None:
            capture._drain_relation_rebuild_debt(provider)
            nonblocking_done.set()

        with provider._lock:
            contender = threading.Thread(target=contend, daemon=True)
            contender.start()
            assert nonblocking_done.wait(timeout=0.25)
        contender.join(timeout=0.25)
        assert not contender.is_alive()
        assert time.perf_counter() - started <= 0.25
        assert len(drain_calls) == drain_count_before
        assert provider._require_conn().total_changes == changes_before
    finally:
        release_maintenance.set()
        maintenance_thread.join(timeout=1.0)
        prefetch_thread.join(timeout=1.0)
        provider.shutdown()
    _OBSERVATIONS.update(
        {
            "prefetch_timeout_observed": False,
            "prefetch_max_wait_ms": int(math.ceil(wait_seconds * 1000.0)),
        }
    )
    _publish_details()
