"""Causal-order regressions for committed vector-outbox replay."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.vector_generation import (
    GenerationIdentity,
    bootstrap_legacy_generation,
    claim_vector_events,
    enqueue_current_vector_event,
    enqueue_vector_event,
)
from scope_recall.vector_outbox_replay import replay_committed_vector_events


class _Embedder:
    def embed(self, text: str) -> list[float]:
        return [float(len(text)), 1.0]


class _BlockingStore:
    """Block the first physical mutation so a newer truth writer can race it."""

    def __init__(
        self,
        *,
        initial: dict[str, dict[str, Any]] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        self.records = dict(initial or {})
        self.conn = conn
        self.mutation_started = threading.Event()
        self.allow_mutation = threading.Event()
        self.order: list[str] = []
        self.transaction_states: list[bool] = []

    def _wait(self) -> None:
        if self.conn is not None:
            self.transaction_states.append(self.conn.in_transaction)
        self.mutation_started.set()
        if not self.allow_mutation.wait(timeout=5):
            raise RuntimeError("timed out waiting to release physical mutation")

    def delete_by_ids(self, ids: list[str]) -> None:
        self._wait()
        for memory_id in ids:
            self.records.pop(memory_id, None)
        self.order.append("physical-delete")

    def upsert_records(self, rows: list[dict[str, Any]]) -> None:
        self._wait()
        for row in rows:
            self.records[str(row["id"])] = dict(row)
        self.order.append("physical-upsert")


class _RecordingStore:
    """Record non-blocking companion mutations for stale-intent probes."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []

    def delete_by_ids(self, ids: list[str]) -> None:
        for memory_id in ids:
            self.records.pop(memory_id, None)
            self.calls.append(("delete", memory_id))

    def upsert_records(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            memory_id = str(row["id"])
            self.records[memory_id] = dict(row)
            self.calls.append(("upsert", memory_id))


@pytest.fixture
def _identity() -> GenerationIdentity:
    return GenerationIdentity(
        backend="sqlite",
        provider="local",
        model="hash",
        dimensions=2,
    )


def _connect(path: Path, *, cross_thread: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5, check_same_thread=not cross_thread)
    conn.row_factory = sqlite3.Row
    return conn


def _store_memory(conn: sqlite3.Connection, *, content: str, updated_at: str) -> None:
    store_row(
        conn,
        memory_id="memory-1",
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
        target="memory",
        content=content,
        metadata="{}",
        allow_duplicate=True,
        timestamp=updated_at,
    )


def _replay(
    conn: sqlite3.Connection,
    store: _BlockingStore,
    generation_id: str,
    db_lock: threading.RLock,
) -> dict[str, int]:
    return replay_committed_vector_events(
        conn,
        generation_id=generation_id,
        vector_store=store,
        embedder=_Embedder(),
        vector_text=lambda summary, content: f"{summary}\n{content}".strip(),
        should_index_row=lambda _target, _metadata: True,
        default_scope_id="scope-a",
        db_lock=db_lock,
        mutation_context=nullcontext,
        limit=1,
        worker_id="worker-old",
    )


def _run_race(
    tmp_path: Path,
    identity: GenerationIdentity,
    *,
    old_operation: str,
) -> tuple[_BlockingStore, sqlite3.Connection]:
    db_path = tmp_path / f"{old_operation}.sqlite3"
    worker_conn = _connect(db_path, cross_thread=True)
    ensure_schema(worker_conn)

    if old_operation == "upsert":
        _store_memory(
            worker_conn,
            content="old truth whose physical write may overlap a newer truth commit",
            updated_at="2026-07-20T00:00:00+00:00",
        )

    manifest = bootstrap_legacy_generation(
        worker_conn,
        identity=identity,
        storage_path="vector-generations/causal-order",
    )
    generation_id = str(manifest["generation_id"])
    enqueue_vector_event(
        worker_conn,
        event_key=f"old-{old_operation}",
        generation_id=generation_id,
        memory_id="memory-1",
        operation=old_operation,
        payload={"updated_at": "2026-07-20T00:00:00+00:00"},
    )
    worker_conn.commit()

    initial = (
        {
            "memory-1": {
                "id": "memory-1",
                "content": "pre-delete physical row",
            }
        }
        if old_operation == "delete"
        else {}
    )
    store = _BlockingStore(initial=initial, conn=worker_conn)
    db_lock = threading.RLock()
    writer_attempted = threading.Event()
    writer_done = threading.Event()
    worker_errors: list[BaseException] = []
    writer_errors: list[BaseException] = []
    worker_result: dict[str, int] = {}

    def worker() -> None:
        try:
            worker_result.update(
                _replay(worker_conn, store, generation_id, db_lock)
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            worker_errors.append(exc)

    def writer() -> None:
        conn = _connect(db_path)
        try:
            writer_attempted.set()
            with db_lock:
                if old_operation == "delete":
                    _store_memory(
                        conn,
                        content="new truth committed after the old physical delete",
                        updated_at="2026-07-20T00:00:01+00:00",
                    )
                else:
                    conn.execute("DELETE FROM memories WHERE id = ?", ("memory-1",))
                    enqueue_current_vector_event(
                        conn,
                        memory_id="memory-1",
                        operation="delete",
                        updated_at="2026-07-20T00:00:01+00:00",
                        reason="newer delete committed during old upsert replay",
                    )
                conn.commit()
            store.order.append("truth-commit")
        except BaseException as exc:  # pragma: no cover - asserted below
            writer_errors.append(exc)
        finally:
            conn.close()
            writer_done.set()

    worker_thread = threading.Thread(target=worker, name=f"old-{old_operation}-worker")
    writer_thread = threading.Thread(target=writer, name=f"new-{old_operation}-writer")
    worker_thread.start()
    assert store.mutation_started.wait(timeout=3)
    writer_thread.start()
    assert writer_attempted.wait(timeout=3)

    try:
        writer_committed_while_old_physical_mutation_blocked = writer_done.wait(timeout=0.3)
    finally:
        store.allow_mutation.set()
        worker_thread.join(timeout=5)
        writer_thread.join(timeout=5)

    assert not worker_thread.is_alive()
    assert not writer_thread.is_alive()
    assert worker_errors == []
    assert writer_errors == []
    assert writer_committed_while_old_physical_mutation_blocked is True
    assert worker_result == {"claimed": 1, "completed": 1, "failed": 0}
    assert store.order[-2:] == ["truth-commit", f"physical-{old_operation}"]
    assert store.transaction_states == [False]

    rows = worker_conn.execute(
        """
        SELECT operation, status
        FROM vector_outbox
        WHERE generation_id=? AND memory_id='memory-1'
        ORDER BY id
        """,
        (generation_id,),
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (old_operation, "completed"),
        ("upsert" if old_operation == "delete" else "delete", "pending"),
    ]

    store.mutation_started.clear()
    store.allow_mutation.set()
    replayed = replay_committed_vector_events(
        worker_conn,
        generation_id=generation_id,
        vector_store=store,
        embedder=_Embedder(),
        vector_text=lambda summary, content: f"{summary}\n{content}".strip(),
        should_index_row=lambda _target, _metadata: True,
        default_scope_id="scope-a",
        db_lock=db_lock,
        mutation_context=nullcontext,
        limit=1,
        worker_id="worker-new",
    )
    assert replayed == {"claimed": 1, "completed": 1, "failed": 0}
    return store, worker_conn


def test_processing_delete_allows_newer_upsert_truth_commit_while_backend_blocks(
    tmp_path: Path,
    _identity: GenerationIdentity,
) -> None:
    store, conn = _run_race(tmp_path, _identity, old_operation="delete")
    try:
        row = conn.execute(
            "SELECT content FROM memories WHERE id='memory-1'"
        ).fetchone()
        assert row is not None
        assert "new truth" in str(row["content"])
        assert store.records["memory-1"]["content"] == str(row["content"])
    finally:
        conn.close()


def test_processing_upsert_allows_newer_delete_truth_commit_while_backend_blocks(
    tmp_path: Path,
    _identity: GenerationIdentity,
) -> None:
    store, conn = _run_race(tmp_path, _identity, old_operation="upsert")
    try:
        assert (
            conn.execute("SELECT 1 FROM memories WHERE id='memory-1'").fetchone()
            is None
        )
        assert "memory-1" not in store.records
    finally:
        conn.close()


@pytest.mark.parametrize("old_operation", ["delete", "upsert"])
def test_already_committed_newer_event_makes_old_claim_a_physical_no_op(
    tmp_path: Path,
    _identity: GenerationIdentity,
    old_operation: str,
) -> None:
    """A higher event id supersedes a stale claimed operation in both directions."""

    db_path = tmp_path / f"precommitted-{old_operation}.sqlite3"
    conn = _connect(db_path)
    ensure_schema(conn)
    if old_operation == "upsert":
        _store_memory(
            conn,
            content="old truth",
            updated_at="2026-07-20T00:00:00+00:00",
        )
    manifest = bootstrap_legacy_generation(
        conn,
        identity=_identity,
        storage_path="vector-generations/precommitted-causal-order",
    )
    generation_id = str(manifest["generation_id"])
    enqueue_vector_event(
        conn,
        event_key=f"claimed-{old_operation}",
        generation_id=generation_id,
        memory_id="memory-1",
        operation=old_operation,
        payload={"updated_at": "2026-07-20T00:00:00+00:00"},
    )
    conn.commit()
    claimed = claim_vector_events(
        conn,
        generation_id=generation_id,
        worker_id="expired-worker",
        limit=1,
        lease_seconds=0,
    )
    conn.commit()
    assert len(claimed) == 1

    if old_operation == "delete":
        _store_memory(
            conn,
            content="new truth",
            updated_at="2026-07-20T00:00:01+00:00",
        )
    else:
        conn.execute("DELETE FROM memories WHERE id='memory-1'")
        enqueue_current_vector_event(
            conn,
            memory_id="memory-1",
            operation="delete",
            updated_at="2026-07-20T00:00:01+00:00",
            reason="newer committed delete",
        )
    conn.commit()
    conn.execute(
        "UPDATE vector_outbox SET updated_at=? WHERE id=?",
        ("2000-01-01T00:00:00+00:00", int(claimed[0]["id"])),
    )
    conn.commit()

    class NoMutationStore:
        def delete_by_ids(self, _ids: list[str]) -> None:
            raise AssertionError("superseded delete reached the physical store")

        def upsert_records(self, _rows: list[dict[str, Any]]) -> None:
            raise AssertionError("superseded upsert reached the physical store")

    result = replay_committed_vector_events(
        conn,
        generation_id=generation_id,
        vector_store=NoMutationStore(),
        embedder=_Embedder(),
        vector_text=lambda summary, content: f"{summary}\n{content}".strip(),
        should_index_row=lambda _target, _metadata: True,
        default_scope_id="scope-a",
        db_lock=threading.RLock(),
        mutation_context=nullcontext,
        limit=1,
        worker_id="replacement-worker",
    )
    assert result == {"claimed": 1, "completed": 1, "failed": 0}
    rows = conn.execute(
        """
        SELECT operation, status
        FROM vector_outbox
        WHERE generation_id=? AND memory_id='memory-1'
        ORDER BY id
        """,
        (generation_id,),
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (old_operation, "completed"),
        ("upsert" if old_operation == "delete" else "delete", "pending"),
    ]
    conn.close()


def test_same_timestamp_recreation_gets_a_new_intent_after_processing_delete(
    tmp_path: Path,
    _identity: GenerationIdentity,
) -> None:
    """A reused truth timestamp must not collide with a historical event key."""

    db_path = tmp_path / "same-timestamp-recreation.sqlite3"
    conn = _connect(db_path)
    try:
        ensure_schema(conn)
        manifest = bootstrap_legacy_generation(
            conn,
            identity=_identity,
            storage_path="vector-generations/same-timestamp-recreation",
        )
        generation_id = str(manifest["generation_id"])
        original_timestamp = "2026-07-20T00:00:00+00:00"
        _store_memory(
            conn,
            content="truth before delete",
            updated_at=original_timestamp,
        )
        conn.commit()

        store = _RecordingStore()
        first = replay_committed_vector_events(
            conn,
            generation_id=generation_id,
            vector_store=store,
            embedder=_Embedder(),
            vector_text=lambda summary, content: f"{summary}\n{content}".strip(),
            should_index_row=lambda _target, _metadata: True,
            default_scope_id="scope-a",
            db_lock=threading.RLock(),
            mutation_context=nullcontext,
            limit=1,
            worker_id="initial-upsert",
        )
        assert first == {"claimed": 1, "completed": 1, "failed": 0}

        conn.execute("DELETE FROM memories WHERE id='memory-1'")
        enqueue_current_vector_event(
            conn,
            memory_id="memory-1",
            operation="delete",
            updated_at="2026-07-20T00:00:01+00:00",
            reason="delete before same-timestamp recreation",
        )
        conn.commit()
        claimed = claim_vector_events(
            conn,
            generation_id=generation_id,
            worker_id="expired-delete",
            limit=1,
        )
        conn.commit()
        assert len(claimed) == 1
        assert claimed[0]["operation"] == "delete"

        _store_memory(
            conn,
            content="recreated truth with reused timestamp",
            updated_at=original_timestamp,
        )
        conn.commit()
        rows = conn.execute(
            """
            SELECT id, operation, status
            FROM vector_outbox
            WHERE generation_id=? AND memory_id='memory-1'
            ORDER BY id
            """,
            (generation_id,),
        ).fetchall()
        assert [(row["operation"], row["status"]) for row in rows] == [
            ("upsert", "completed"),
            ("delete", "processing"),
            ("upsert", "pending"),
        ]

        conn.execute(
            "UPDATE vector_outbox SET updated_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", int(claimed[0]["id"])),
        )
        conn.commit()
        calls_before_stale_replay = list(store.calls)
        stale = replay_committed_vector_events(
            conn,
            generation_id=generation_id,
            vector_store=store,
            embedder=_Embedder(),
            vector_text=lambda summary, content: f"{summary}\n{content}".strip(),
            should_index_row=lambda _target, _metadata: True,
            default_scope_id="scope-a",
            db_lock=threading.RLock(),
            mutation_context=nullcontext,
            limit=1,
            worker_id="replacement-delete",
        )
        assert stale == {"claimed": 1, "completed": 1, "failed": 0}
        assert store.calls == calls_before_stale_replay

        latest = replay_committed_vector_events(
            conn,
            generation_id=generation_id,
            vector_store=store,
            embedder=_Embedder(),
            vector_text=lambda summary, content: f"{summary}\n{content}".strip(),
            should_index_row=lambda _target, _metadata: True,
            default_scope_id="scope-a",
            db_lock=threading.RLock(),
            mutation_context=nullcontext,
            limit=1,
            worker_id="latest-upsert",
        )
        assert latest == {"claimed": 1, "completed": 1, "failed": 0}
        assert store.records["memory-1"]["content"] == (
            "recreated truth with reused timestamp"
        )
    finally:
        conn.close()


def test_snapshot_drift_reembeds_only_outside_the_sqlite_writer_fence(
    tmp_path: Path,
    _identity: GenerationIdentity,
) -> None:
    """Defensive truth revalidation must never perform remote work under BEGIN IMMEDIATE."""

    db_path = tmp_path / "embedding-fence.sqlite3"
    conn = _connect(db_path)
    writer_conn = _connect(db_path)
    try:
        ensure_schema(conn)
        manifest = bootstrap_legacy_generation(
            conn,
            identity=_identity,
            storage_path="vector-generations/embedding-fence",
        )
        generation_id = str(manifest["generation_id"])
        _store_memory(
            conn,
            content="truth before defensive snapshot drift",
            updated_at="2026-07-20T00:00:00+00:00",
        )
        conn.commit()

        class DriftingEmbedder:
            def __init__(self) -> None:
                self.transaction_states: list[bool] = []

            def embed(self, text: str) -> list[float]:
                self.transaction_states.append(conn.in_transaction)
                if len(self.transaction_states) == 1:
                    writer_conn.execute(
                        """
                        UPDATE memories
                        SET content=?, summary=?, updated_at=?
                        WHERE id='memory-1'
                        """,
                        (
                            "truth after defensive snapshot drift",
                            "drifted summary",
                            "2026-07-20T00:00:01+00:00",
                        ),
                    )
                    writer_conn.commit()
                return [float(len(text)), 1.0]

        embedder = DriftingEmbedder()
        store = _RecordingStore()
        result = replay_committed_vector_events(
            conn,
            generation_id=generation_id,
            vector_store=store,
            embedder=embedder,
            vector_text=lambda summary, content: f"{summary}\n{content}".strip(),
            should_index_row=lambda _target, _metadata: True,
            default_scope_id="scope-a",
            db_lock=threading.RLock(),
            mutation_context=nullcontext,
            limit=1,
            worker_id="snapshot-drift-worker",
        )
        assert result == {"claimed": 1, "completed": 1, "failed": 0}
        assert embedder.transaction_states == [False, False]
        assert store.records["memory-1"]["content"] == (
            "truth after defensive snapshot drift"
        )
    finally:
        writer_conn.close()
        conn.close()


def test_targeted_replay_bypasses_unrelated_older_backlog(
    tmp_path: Path,
    _identity: GenerationIdentity,
) -> None:
    conn = _connect(tmp_path / "targeted.sqlite3")
    try:
        ensure_schema(conn)
        manifest = bootstrap_legacy_generation(
            conn,
            identity=_identity,
            storage_path="vector-generations/targeted",
        )
        generation_id = str(manifest["generation_id"])
        _store_memory(
            conn,
            content="older unrelated backlog",
            updated_at="2026-08-10T00:00:00+00:00",
        )
        store_row(
            conn,
            memory_id="memory-target",
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
            target="memory",
            content="targeted journal memory",
            metadata="{}",
            allow_duplicate=True,
            timestamp="2026-08-10T00:00:01+00:00",
        )
        conn.commit()
        rows = conn.execute(
            "SELECT id, memory_id FROM vector_outbox ORDER BY id"
        ).fetchall()
        target_event_id = int(
            next(row["id"] for row in rows if row["memory_id"] == "memory-target")
        )
        store = _RecordingStore()

        result = replay_committed_vector_events(
            conn,
            generation_id=generation_id,
            vector_store=store,
            embedder=_Embedder(),
            vector_text=lambda summary, content: f"{summary}\n{content}".strip(),
            should_index_row=lambda _target, _metadata: True,
            default_scope_id="scope-a",
            mutation_context=nullcontext,
            event_ids=[target_event_id],
            limit=1,
        )

        assert result == {"claimed": 1, "completed": 1, "failed": 0}
        assert store.calls == [("upsert", "memory-target")]
        statuses = {
            str(row["memory_id"]): str(row["status"])
            for row in conn.execute(
                "SELECT memory_id, status FROM vector_outbox ORDER BY id"
            ).fetchall()
        }
        assert statuses == {"memory-1": "pending", "memory-target": "completed"}
    finally:
        conn.close()
