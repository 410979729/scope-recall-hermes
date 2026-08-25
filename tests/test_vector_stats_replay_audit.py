"""Spy regressions for P1-08 stats/status and P1-09 ordinary replay audit bounds."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import threading
from typing import Any

import pytest

from _scope_recall_public_memory_port import attach_public_truth_ports
from scope_recall._internal.runtime.vector_view import RuntimeVectorView
from scope_recall.hygiene import build_hygiene_report
from scope_recall.memory_queries import stats_payload
from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.sqlite_vector_store import SQLiteBruteForceVectorStore
from scope_recall.vector_generation import (
    GenerationIdentity,
    bootstrap_legacy_generation,
    enqueue_vector_event,
    ensure_vector_generation_schema,
)
from scope_recall.vector_membership import mark_membership_unready, membership_is_ready
from scope_recall.vector_outbox_replay import replay_committed_vector_events
from scope_recall.vector_runtime import (
    _apply_incremental_vector_counts,
    refresh_vector_audit,
    replay_vector_outbox,
)
from scope_recall.vector_store import (
    HYGIENE_SAMPLE_HARD_LIMIT,
    LanceVectorStore,
    VectorRecord,
    lance_table_contains_id,
    sample_lance_table_metadata,
    sample_vector_metadata,
)


class _ForbiddenEnumerationStore:
    """Companion double that fails closed on every full-enumeration API."""

    def __init__(self, *, physical_rows: int = 0) -> None:
        self.physical_rows = physical_rows
        self.records: dict[str, dict[str, object]] = {}
        self.calls: list[str] = []

    def list_records(self) -> dict[str, dict[str, object]]:
        self.calls.append("list_records")
        raise AssertionError("stats/status/replay must not call list_records")

    def list_ids(self) -> list[str]:
        self.calls.append("list_ids")
        raise AssertionError("ordinary runtime must not call list_ids")

    def audit_counts(self) -> dict[str, int]:
        self.calls.append("audit_counts")
        raise AssertionError("ordinary runtime must not call audit_counts")

    def count_rows(self) -> int:
        self.calls.append("count_rows")
        raise AssertionError("ordinary runtime must not call count_rows")

    id_lookup_indexed = False

    def contains_id(self, memory_id: str) -> bool:
        self.calls.append("contains_id")
        raise AssertionError("ordinary ledger path must not call contains_id")

    def sample_metadata(self, *, limit: int = 200, offset: int = 0) -> list[dict[str, object]]:
        self.calls.append("sample_metadata")
        start = max(0, int(offset or 0))
        return [
            {key: value for key, value in row.items() if key != "vector"}
            for row in list(self.records.values())[start : start + max(0, int(limit))]
        ]

    def upsert_records(self, rows: list[dict[str, object]]) -> None:
        for row in rows:
            memory_id = str(row["id"])
            self.records[memory_id] = dict(row)
        self.physical_rows = len(self.records)
        self.calls.append("upsert")

    def delete_by_ids(self, ids: list[str]) -> None:
        for memory_id in ids:
            self.records.pop(memory_id, None)
        self.physical_rows = len(self.records)
        self.calls.append("delete")


class _Embedder:
    def embed(self, text: str) -> list[float]:
        return [float(len(text or "")), 1.0]


def _truth_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _stats_provider(conn: sqlite3.Connection, store: _ForbiddenEnumerationStore) -> SimpleNamespace:
    provider = SimpleNamespace(
        name="scope-recall",
        _conn=conn,
        _lock=threading.RLock(),
        _vector_lock=threading.RLock(),
        _scope_id="scope-a",
        _shared_scope_id="shared-a",
        _shared_pool_scope_id="",
        _accessible_scope_ids=["scope-a", "shared-a"],
        _writable_scope_ids=["scope-a"],
        _vector_enabled=True,
        _vector_ready=True,
        _vector_status="ready",
        _vector_message="",
        _vector_backend="sqlite-bruteforce",
        _vector_store=store,
        _vector_row_count=4,
        _vector_unique_id_count=4,
        _vector_duplicate_row_count=0,
        _vector_config={"sync_mode": "incremental"},
        _embedder=None,
        _retrieval_config={"mode": "hybrid", "lexical_weight": 0.6, "vector_weight": 0.4},
        _config={},
        _hermes_home=None,
        _db_path="",
        _truth_writer_role="owner",
        _truth_writer_owner={},
        _last_adjudication_report={},
        _shared_pool_enabled=False,
        _shared_pool_write_enabled=False,
        _shared_pool_id="",
        _migration_info={},
        _writer_failed_writes=0,
        _writer_reported_failures=0,
        _writer_last_error_type="",
        _freshness_backfill={},
    )
    provider._require_conn = lambda: conn
    return attach_public_truth_ports(provider)


def test_stats_and_status_sources_do_not_enumerate_vectors() -> None:
    import inspect

    from scope_recall.memory_queries import stats_payload as stats_fn
    from scope_recall.vector_runtime import _replay_vector_outbox_guarded

    status_source = inspect.getsource(RuntimeVectorView.vector_status_view)
    stats_source = inspect.getsource(stats_fn)
    replay_source = inspect.getsource(_replay_vector_outbox_guarded)
    assert "list_records(" not in status_source
    assert "list_records(" not in stats_source
    assert "refresh_vector_audit(" not in stats_source
    assert "audit_counts(" not in stats_source
    assert "count_rows(" not in status_source
    assert "count_rows(" not in stats_source
    assert "count_rows(" not in replay_source
    assert "lance_table_contains_id" not in replay_source
    assert "list_ids(" not in status_source
    assert "list_ids(" not in stats_source
    assert "_refresh_vector_row_count_only" not in replay_source


def test_vector_status_view_never_calls_list_records() -> None:
    store = _ForbiddenEnumerationStore(physical_rows=3)
    adapter = SimpleNamespace(
        _vector_store=store,
        _embedder=None,
        _vector_config={"sync_mode": "incremental", "fallback_embedder": {}},
        _vector_status="ready",
        _vector_row_count=3,
        _vector_unique_id_count=3,
        _vector_duplicate_row_count=0,
        _vector_enabled=True,
        _vector_ready=True,
        _vector_message="",
        _vector_backend="sqlite-bruteforce",
    )

    status = RuntimeVectorView(adapter).vector_status_view()

    assert status["row_count"] == 3
    assert status["unique_id_count"] == 3
    assert status["records"] == {}
    assert "list_records" not in store.calls
    assert "audit_counts" not in store.calls


def test_stats_succeeds_when_list_records_raises() -> None:
    conn = _truth_conn()
    store = _ForbiddenEnumerationStore(physical_rows=4)
    provider = _stats_provider(conn, store)

    payload = stats_payload(provider)

    assert payload["vector"]["row_count"] == 4
    assert payload["vector"]["unique_id_count"] == 4
    assert payload["vector"]["status"] == "ready"
    assert "list_records" not in store.calls
    assert "audit_counts" not in store.calls
    assert "list_ids" not in store.calls
    assert "count_rows" not in store.calls


def test_stats_does_not_duplicate_status_or_full_audit() -> None:
    conn = _truth_conn()
    store = _ForbiddenEnumerationStore(physical_rows=4)
    provider = _stats_provider(conn, store)
    views: list[int] = []
    original = provider.vector_status_view

    def counted_status() -> dict[str, object]:
        views.append(1)
        return original()

    provider.vector_status_view = counted_status

    payload = stats_payload(provider)

    assert payload["vector"]["row_count"] == 4
    assert views == [1]


def test_empty_replay_skips_after_replay_hook() -> None:
    conn = _truth_conn()
    ensure_vector_generation_schema(conn)
    hooks: list[str] = []
    store = _ForbiddenEnumerationStore()

    result = replay_committed_vector_events(
        conn,
        generation_id="gen-empty",
        vector_store=store,
        embedder=_Embedder(),
        vector_text=lambda summary, content: f"{summary}\n{content}".strip(),
        should_index_row=lambda _target, _metadata: True,
        mutation_context=nullcontext,
        after_replay=lambda: hooks.append("audit"),
    )

    assert result == {"claimed": 0, "completed": 0, "failed": 0}
    assert hooks == []
    assert store.calls == []


def test_empty_runtime_replay_skips_explicit_audit_hook() -> None:
    conn = _truth_conn()
    ensure_vector_generation_schema(conn)
    store = _ForbiddenEnumerationStore()
    provider = SimpleNamespace(
        _vector_generation_id="gen-empty",
        _vector_store=store,
        _embedder=_Embedder(),
        _lock=threading.RLock(),
        _vector_lock=threading.RLock(),
        _vector_config={},
        _scope_id="scope-a",
        _vector_row_count=0,
        _vector_unique_id_count=0,
        _vector_duplicate_row_count=0,
        _require_conn=lambda: conn,
        _vector_text=lambda summary, content: f"{summary}\n{content}".strip(),
    )

    result = replay_vector_outbox(provider, refresh_audit_after=True)

    assert result == {"claimed": 0, "completed": 0, "failed": 0}
    assert "audit_counts" not in store.calls
    assert "list_ids" not in store.calls
    assert "list_records" not in store.calls
    assert "count_rows" not in store.calls
    assert "contains_id" not in store.calls


def test_ordinary_replay_updates_counts_without_full_audit() -> None:
    conn = _truth_conn()
    ensure_vector_generation_schema(conn)
    store_row(
        conn,
        memory_id="memory-1",
        scope_id="scope-a",
        platform="test",
        user_id="sample-user",
        chat_id="",
        thread_id="",
        gateway_session_key="",
        agent_identity="sample-agent",
        agent_workspace="test",
        session_id="session-a",
        source="fixture",
        target="memory",
        content="ordinary write content",
        metadata="{}",
        allow_duplicate=True,
        timestamp="2026-08-24T00:00:00+00:00",
        enqueue_vector_intent=False,
    )
    manifest = bootstrap_legacy_generation(
        conn,
        identity=GenerationIdentity(
            backend="sqlite",
            provider="fixture",
            model="hash-v1",
            dimensions=2,
        ),
        storage_path="vector-generations/ordinary-write",
    )
    generation_id = str(manifest["generation_id"])
    enqueue_vector_event(
        conn,
        event_key="ordinary-write",
        generation_id=generation_id,
        memory_id="memory-1",
        operation="upsert",
        timestamp="2026-08-24T00:00:00+00:00",
    )
    conn.commit()
    store = _ForbiddenEnumerationStore()
    provider = SimpleNamespace(
        _vector_generation_id=generation_id,
        _vector_store=store,
        _embedder=_Embedder(),
        _lock=threading.RLock(),
        _vector_lock=threading.RLock(),
        _vector_config={},
        _scope_id="scope-a",
        _vector_row_count=0,
        _vector_unique_id_count=0,
        _vector_duplicate_row_count=0,
        _require_conn=lambda: conn,
        _vector_text=lambda summary, content: f"{summary}\n{content}".strip(),
    )

    result = replay_vector_outbox(provider)

    assert result == {"claimed": 1, "completed": 1, "failed": 0}
    assert "memory-1" in store.records
    assert provider._vector_row_count == 1
    assert provider._vector_unique_id_count == 1
    assert "audit_counts" not in store.calls
    assert "list_ids" not in store.calls
    assert "list_records" not in store.calls
    assert "count_rows" not in store.calls
    assert "contains_id" not in store.calls
    assert _membership_ids(conn, generation_id) == ["memory-1"]
    assert membership_is_ready(conn, generation_id) is True


def _ordinary_replay_fixture(
    conn: sqlite3.Connection,
    store: Any,
    *,
    memory_id: str,
    event_key: str,
    operation: str,
    content: str,
    timestamp: str,
    metadata: str = "{}",
    row_count: int = 0,
    unique_id_count: int = 0,
) -> SimpleNamespace:
    if operation != "delete":
        store_row(
            conn,
            memory_id=memory_id,
            scope_id="scope-a",
            platform="test",
            user_id="sample-user",
            chat_id="",
            thread_id="",
            gateway_session_key="",
            agent_identity="sample-agent",
            agent_workspace="test",
            session_id="session-a",
            source="fixture",
            target="memory",
            content=content,
            metadata=metadata,
            allow_duplicate=True,
            timestamp=timestamp,
            enqueue_vector_intent=False,
        )
    manifest = bootstrap_legacy_generation(
        conn,
        identity=GenerationIdentity(
            backend="sqlite",
            provider="fixture",
            model="hash-v1",
            dimensions=2,
        ),
        storage_path=f"vector-generations/{event_key}",
    )
    generation_id = str(manifest["generation_id"])
    enqueue_vector_event(
        conn,
        event_key=event_key,
        generation_id=generation_id,
        memory_id=memory_id,
        operation=operation,
        timestamp=timestamp,
    )
    conn.commit()
    return SimpleNamespace(
        _vector_generation_id=generation_id,
        _vector_store=store,
        _embedder=_Embedder(),
        _lock=threading.RLock(),
        _vector_lock=threading.RLock(),
        _vector_config={},
        _scope_id="scope-a",
        _vector_row_count=row_count,
        _vector_unique_id_count=unique_id_count,
        _vector_duplicate_row_count=max(0, row_count - unique_id_count),
        _require_conn=lambda: conn,
        _vector_text=lambda summary, content: f"{summary}\n{content}".strip(),
    )


def _assert_no_corpus_scan(store: Any) -> None:
    assert "audit_counts" not in store.calls
    assert "list_ids" not in store.calls
    assert "list_records" not in store.calls
    assert "count_rows" not in store.calls
    assert "contains_id" not in store.calls


def _membership_ids(conn: sqlite3.Connection, generation_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT memory_id
        FROM vector_id_membership
        WHERE generation_id = ?
        ORDER BY memory_id
        """,
        (generation_id,),
    ).fetchall()
    return [str(row["memory_id"]) for row in rows]


def test_insert_update_delete_adjust_cached_counts_without_corpus_scan() -> None:
    conn = _truth_conn()
    ensure_vector_generation_schema(conn)
    store = _ForbiddenEnumerationStore()
    provider = _ordinary_replay_fixture(
        conn,
        store,
        memory_id="memory-1",
        event_key="insert-write",
        operation="upsert",
        content="first insert",
        timestamp="2026-08-24T00:00:00+00:00",
    )

    assert replay_vector_outbox(provider) == {"claimed": 1, "completed": 1, "failed": 0}
    assert provider._vector_row_count == 1
    assert provider._vector_unique_id_count == 1
    _assert_no_corpus_scan(store)

    enqueue_vector_event(
        conn,
        event_key="update-write",
        generation_id=str(provider._vector_generation_id),
        memory_id="memory-1",
        operation="upsert",
        timestamp="2026-08-24T00:00:01+00:00",
    )
    conn.commit()
    store.calls.clear()

    assert replay_vector_outbox(provider) == {"claimed": 1, "completed": 1, "failed": 0}
    assert provider._vector_row_count == 1
    assert provider._vector_unique_id_count == 1
    _assert_no_corpus_scan(store)
    assert _membership_ids(conn, str(provider._vector_generation_id)) == ["memory-1"]

    conn.execute("DELETE FROM memories WHERE id = ?", ("memory-1",))
    enqueue_vector_event(
        conn,
        event_key="delete-write",
        generation_id=str(provider._vector_generation_id),
        memory_id="memory-1",
        operation="delete",
        timestamp="2026-08-24T00:00:02+00:00",
    )
    conn.commit()
    store.calls.clear()

    assert replay_vector_outbox(provider) == {"claimed": 1, "completed": 1, "failed": 0}
    assert "memory-1" not in store.records
    assert provider._vector_row_count == 0
    assert provider._vector_unique_id_count == 0
    _assert_no_corpus_scan(store)
    assert _membership_ids(conn, str(provider._vector_generation_id)) == []


def test_failed_mutation_does_not_adjust_cached_counts() -> None:
    conn = _truth_conn()
    ensure_vector_generation_schema(conn)
    store = _ForbiddenEnumerationStore()

    def boom(_rows: list[dict[str, object]]) -> None:
        store.calls.append("upsert")
        raise RuntimeError("injected upsert failure")

    store.upsert_records = boom  # type: ignore[method-assign]
    provider = _ordinary_replay_fixture(
        conn,
        store,
        memory_id="memory-fail",
        event_key="failed-write",
        operation="upsert",
        content="failed insert",
        timestamp="2026-08-24T00:00:00+00:00",
        row_count=4,
        unique_id_count=3,
    )

    result = replay_vector_outbox(provider)

    assert result == {"claimed": 1, "completed": 0, "failed": 1}
    assert provider._vector_row_count == 4
    assert provider._vector_unique_id_count == 3
    assert provider._vector_duplicate_row_count == 1
    _assert_no_corpus_scan(store)
    assert _membership_ids(conn, str(provider._vector_generation_id)) == []


def test_empty_replay_performs_no_contains_or_count() -> None:
    conn = _truth_conn()
    ensure_vector_generation_schema(conn)
    store = _ForbiddenEnumerationStore()
    provider = SimpleNamespace(
        _vector_generation_id="gen-empty-probe",
        _vector_store=store,
        _embedder=_Embedder(),
        _lock=threading.RLock(),
        _vector_lock=threading.RLock(),
        _vector_config={},
        _scope_id="scope-a",
        _vector_row_count=7,
        _vector_unique_id_count=6,
        _vector_duplicate_row_count=1,
        _require_conn=lambda: conn,
        _vector_text=lambda summary, content: f"{summary}\n{content}".strip(),
    )

    result = replay_vector_outbox(provider)

    assert result == {"claimed": 0, "completed": 0, "failed": 0}
    assert provider._vector_row_count == 7
    assert provider._vector_unique_id_count == 6
    assert store.calls == []


def test_explicit_refresh_vector_audit_matches_store_counts(tmp_path) -> None:
    store = SQLiteBruteForceVectorStore(
        tmp_path / "vector.sqlite3",
        table_name="memories",
        dimensions=2,
        metric="cosine",
    )
    store.open()
    try:
        store.upsert(
            VectorRecord(
                id="doctor-1",
                scope_id="scope-a",
                source="fixture",
                target="memory",
                content="doctor audit row",
                summary="doctor audit",
                updated_at="2026-08-24T00:00:00+00:00",
                vector=[1.0, 0.0],
            )
        )
        store.upsert(
            VectorRecord(
                id="doctor-2",
                scope_id="scope-a",
                source="fixture",
                target="memory",
                content="second doctor audit row",
                summary="second doctor audit",
                updated_at="2026-08-24T00:00:01+00:00",
                vector=[0.0, 1.0],
            )
        )
        conn = _truth_conn()
        ensure_vector_generation_schema(conn)
        manifest = bootstrap_legacy_generation(
            conn,
            identity=GenerationIdentity(
                backend="sqlite",
                provider="fixture",
                model="hash-v1",
                dimensions=2,
            ),
            storage_path="vector-generations/doctor-audit",
        )
        provider = SimpleNamespace(
            _vector_store=store,
            _vector_generation_id=str(manifest["generation_id"]),
            _lock=threading.RLock(),
            _vector_lock=threading.RLock(),
            _vector_row_count=0,
            _vector_unique_id_count=0,
            _vector_duplicate_row_count=0,
            _require_conn=lambda: conn,
        )

        counts = refresh_vector_audit(provider, persist=False)

        assert counts == store.audit_counts()
        assert counts["physical_rows"] == 2
        assert counts["unique_ids"] == 2
        assert provider._vector_row_count == 2
        assert provider._vector_unique_id_count == 2
        sampled = store.sample_metadata(limit=2)
        assert [row["id"] for row in sampled] == ["doctor-1", "doctor-2"]
        assert all("vector" not in row and "vector_json" not in row for row in sampled)
    finally:
        store.close()


def test_refresh_audit_holds_mutation_guard_through_membership_commit(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scope_recall.vector_runtime as vector_runtime

    store = SQLiteBruteForceVectorStore(
        tmp_path / "vector-audit-lock.sqlite3",
        table_name="memories",
        dimensions=2,
        metric="cosine",
    )
    store.open()
    conn = _truth_conn()
    ensure_vector_generation_schema(conn)
    manifest = bootstrap_legacy_generation(
        conn,
        identity=GenerationIdentity(
            backend="sqlite",
            provider="fixture",
            model="hash-v1",
            dimensions=2,
        ),
        storage_path="vector-generations/audit-lock",
    )
    provider = SimpleNamespace(
        _vector_store=store,
        _vector_generation_id=str(manifest["generation_id"]),
        _lock=threading.RLock(),
        _vector_lock=threading.RLock(),
        _vector_row_count=0,
        _vector_unique_id_count=0,
        _vector_duplicate_row_count=0,
        _require_conn=lambda: conn,
    )
    guard_held = False
    original_replace = vector_runtime.replace_generation_membership

    @contextmanager
    def guard(_provider):
        nonlocal guard_held
        guard_held = True
        try:
            yield
        finally:
            guard_held = False

    def replace_while_guarded(*args, **kwargs):
        assert guard_held is True
        return original_replace(*args, **kwargs)

    monkeypatch.setattr(vector_runtime, "_vector_mutation_lock", guard)
    monkeypatch.setattr(
        vector_runtime,
        "replace_generation_membership",
        replace_while_guarded,
    )
    try:
        vector_runtime.refresh_vector_audit(provider, persist=True)
    finally:
        store.close()
        conn.close()


def test_hygiene_sampling_projects_no_vector_and_enforces_hard_limit() -> None:
    class HugeStore:
        def sample_metadata(self, *, limit: int = 200, offset: int = 0) -> list[dict[str, object]]:
            start = max(0, int(offset or 0))
            window = max(0, int(limit))
            rows = []
            for index in range(start, start + window):
                rows.append(
                    {
                        "id": f"memory-{index}",
                        "scope_id": "scope-a",
                        "source": "fixture",
                        "target": "general",
                        "content": f"row {index}",
                        "summary": f"row {index}",
                        "updated_at": "2026-08-24T00:00:00+00:00",
                        "vector": [1.0, 0.0],
                    }
                )
            return rows

        def list_records(self) -> dict[str, dict[str, object]]:
            raise AssertionError("hygiene must not fall back to list_records")

    records = sample_vector_metadata(HugeStore(), limit=10_000)

    assert len(records) == HYGIENE_SAMPLE_HARD_LIMIT
    assert all("vector" not in row for row in records.values())

    conn = _truth_conn()
    report = build_hygiene_report(conn, vector_store=HugeStore(), limit=10_000)
    assert report["general_vector_rows"]["count"] == HYGIENE_SAMPLE_HARD_LIMIT
    assert all("vector" not in item for item in report["general_vector_rows"]["items"])


def _metadata_row(memory_id: str) -> dict[str, object]:
    return {
        "id": memory_id,
        "scope_id": "scope-a",
        "source": "fixture",
        "target": "general",
        "content": memory_id,
        "summary": memory_id,
        "updated_at": "2026-08-24T00:00:00+00:00",
    }


def test_sample_vector_metadata_hard_limit_caps_duplicate_consumption() -> None:
    class DuplicateOverflowStore:
        def __init__(self) -> None:
            self.requested: list[tuple[int, int]] = []
            self.returned = 0

        def sample_metadata(self, *, limit: int = 200, offset: int = 0) -> list[dict[str, object]]:
            window = max(0, int(limit))
            self.requested.append((window, int(offset or 0)))
            rows = []
            for index in range(window):
                rows.append(_metadata_row("dup-a" if index % 2 == 0 else "dup-b"))
            self.returned += len(rows)
            return rows

        def list_records(self) -> dict[str, dict[str, object]]:
            raise AssertionError("hygiene must not fall back to list_records")

    store = DuplicateOverflowStore()
    records = sample_vector_metadata(store, limit=10_000)

    assert set(records) == {"dup-a", "dup-b"}
    assert store.returned <= HYGIENE_SAMPLE_HARD_LIMIT
    assert sum(limit for limit, _offset in store.requested) <= HYGIENE_SAMPLE_HARD_LIMIT
    assert all(limit <= HYGIENE_SAMPLE_HARD_LIMIT for limit, _offset in store.requested)


def test_sample_vector_metadata_slices_oversize_backend_pages() -> None:
    class OversizeStore:
        def sample_metadata(self, *, limit: int = 200, offset: int = 0) -> list[dict[str, object]]:
            start = max(0, int(offset or 0))
            del limit
            return [_metadata_row(f"memory-{index}") for index in range(start, start + 5_000)]

        def list_records(self) -> dict[str, dict[str, object]]:
            raise AssertionError("hygiene must not fall back to list_records")

    records = sample_vector_metadata(OversizeStore(), limit=10_000)

    assert len(records) == HYGIENE_SAMPLE_HARD_LIMIT


class _UnboundedLanceTable:
    def to_list(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
        raise AssertionError("unbounded to_list")

    def to_arrow(self) -> object:
        raise AssertionError("unbounded to_arrow")

    def to_pandas(self) -> object:
        raise AssertionError("unbounded to_pandas")


class _PushdownQuery:
    def __init__(self) -> None:
        self._columns: list[str] | None = None
        self._limit: int | None = None
        self._offset: int | None = None
        self._where: str | None = None

    def select(self, columns: list[str]) -> "_PushdownQuery":
        self._columns = list(columns)
        return self

    def limit(self, value: int) -> "_PushdownQuery":
        self._limit = int(value)
        return self

    def offset(self, value: int) -> "_PushdownQuery":
        self._offset = int(value)
        return self

    def where(self, expression: str, **_kwargs: object) -> "_PushdownQuery":
        self._where = str(expression)
        return self

    def to_list(self) -> list[dict[str, object]]:
        if self._limit is None:
            raise AssertionError("query to_list without limit")
        if not self._columns or "vector" in self._columns:
            raise AssertionError("vector column was projected")
        if self._where:
            return [{"id": "memory-1", "scope_id": "scope-a"}]
        return [
            {
                "id": "memory-1",
                "scope_id": "scope-a",
                "source": "fixture",
                "target": "general",
                "content": "sampled",
                "summary": "sampled",
                "updated_at": "2026-08-24T00:00:00+00:00",
            }
        ]


class _PushdownLanceTable(_UnboundedLanceTable):
    def __init__(self) -> None:
        self.last_query: _PushdownQuery | None = None

    def search(self, query: object = None) -> _PushdownQuery:
        del query
        self.last_query = _PushdownQuery()
        return self.last_query


def test_lance_sample_metadata_fails_closed_without_bounded_query() -> None:
    store = LanceVectorStore(
        Path("unused"),
        table_name="memories",
        dimensions=2,
    )
    store._table = _UnboundedLanceTable()

    assert store.sample_metadata(limit=50, offset=10) == []


def test_lance_sample_metadata_pushes_projection_and_limit() -> None:
    table = _PushdownLanceTable()
    rows = sample_lance_table_metadata(
        table,
        columns=("id", "scope_id", "source", "target", "content", "summary", "updated_at", "vector"),
        limit=25,
        offset=5,
    )

    assert [row["id"] for row in rows] == ["memory-1"]
    assert all("vector" not in row for row in rows)
    query = table.last_query
    assert query is not None
    assert query._limit == 25
    assert query._offset == 5
    assert query._columns is not None
    assert "vector" not in query._columns
    assert "id" in query._columns


def test_lance_store_sample_and_contains_use_pushdown_query() -> None:
    store = LanceVectorStore(
        Path("unused"),
        table_name="memories",
        dimensions=2,
    )
    table = _PushdownLanceTable()
    store._table = table

    sampled = store.sample_metadata(limit=12, offset=3)
    assert [row["id"] for row in sampled] == ["memory-1"]
    assert table.last_query is not None
    assert table.last_query._limit == 12
    assert table.last_query._offset == 3
    assert table.last_query._columns is not None
    assert "vector" not in table.last_query._columns
    assert table.last_query._where is None

    with pytest.raises(RuntimeError, match="indexed id lookup"):
        store.contains_id("memory-1")
    assert table.last_query._where is None
    assert lance_table_contains_id(table, "memory-1") is None
    assert store.id_lookup_indexed is False


class _IndexedPushdownLanceTable(_PushdownLanceTable):
    def list_indices(self) -> list[dict[str, object]]:
        return [{"columns": ["id"], "index_type": "BTREE"}]


class _LanceReplaySpyStore(LanceVectorStore):
    """Physical Lance double: filter APIs exist, but no scalar-index proof."""

    def __init__(self, table: _PushdownLanceTable) -> None:
        super().__init__(Path("unused"), table_name="memories", dimensions=2)
        self._table = table
        self.records: dict[str, dict[str, object]] = {}
        self.calls: list[str] = []

    def upsert_records(self, rows: list[dict[str, object]]) -> None:
        self.calls.append("upsert")
        for row in rows:
            self.records[str(row["id"])] = dict(row)

    def delete_by_ids(self, ids: list[str]) -> None:
        self.calls.append("delete")
        for memory_id in ids:
            self.records.pop(str(memory_id), None)

    def contains_id(self, memory_id: str) -> bool:
        self.calls.append("contains_id")
        found = lance_table_contains_id(self._require_table(), str(memory_id or ""))
        if found is None:
            raise RuntimeError("LanceDB cannot prove an indexed id lookup")
        return found

    def list_ids(self) -> list[str]:
        self.calls.append("list_ids")
        raise AssertionError("ordinary runtime must not call list_ids")

    def list_records(self) -> dict[str, dict[str, object]]:
        self.calls.append("list_records")
        raise AssertionError("ordinary runtime must not call list_records")

    def count_rows(self) -> int:
        self.calls.append("count_rows")
        raise AssertionError("ordinary runtime must not call count_rows")

    def audit_counts(self) -> dict[str, int]:
        self.calls.append("audit_counts")
        raise AssertionError("ordinary runtime must not call audit_counts")


def test_generation_schema_creates_membership_ledger() -> None:
    conn = _truth_conn()
    ensure_vector_generation_schema(conn)
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "vector_id_membership" in tables
    assert "vector_membership_state" in tables


def test_lance_contains_id_uses_filter_only_when_scalar_index_listed() -> None:
    store = LanceVectorStore(
        Path("unused"),
        table_name="memories",
        dimensions=2,
    )
    table = _IndexedPushdownLanceTable()
    store._table = table

    assert store.id_lookup_indexed is True
    assert store.contains_id("memory-1") is True
    assert table.last_query is not None
    assert table.last_query._limit == 1
    assert table.last_query._where is not None
    assert "memory-1" in table.last_query._where


def test_ordinary_lance_replay_uses_membership_not_unindexed_filter() -> None:
    conn = _truth_conn()
    ensure_vector_generation_schema(conn)
    table = _PushdownLanceTable()
    store = _LanceReplaySpyStore(table)
    provider = _ordinary_replay_fixture(
        conn,
        store,
        memory_id="memory-1",
        event_key="lance-ledger-insert",
        operation="upsert",
        content="lance ledger insert",
        timestamp="2026-08-24T00:00:00+00:00",
    )

    assert replay_vector_outbox(provider) == {"claimed": 1, "completed": 1, "failed": 0}
    assert provider._vector_row_count == 1
    assert provider._vector_unique_id_count == 1
    assert "memory-1" in store.records
    assert "contains_id" not in store.calls
    _assert_no_corpus_scan(store)
    assert table.last_query is None
    assert _membership_ids(conn, str(provider._vector_generation_id)) == ["memory-1"]
    assert membership_is_ready(conn, str(provider._vector_generation_id)) is True

    enqueue_vector_event(
        conn,
        event_key="lance-ledger-update",
        generation_id=str(provider._vector_generation_id),
        memory_id="memory-1",
        operation="upsert",
        timestamp="2026-08-24T00:00:01+00:00",
    )
    conn.commit()
    store.calls.clear()

    assert replay_vector_outbox(provider) == {"claimed": 1, "completed": 1, "failed": 0}
    assert provider._vector_row_count == 1
    assert "contains_id" not in store.calls
    _assert_no_corpus_scan(store)
    assert table.last_query is None

    conn.execute("DELETE FROM memories WHERE id = ?", ("memory-1",))
    enqueue_vector_event(
        conn,
        event_key="lance-ledger-delete",
        generation_id=str(provider._vector_generation_id),
        memory_id="memory-1",
        operation="delete",
        timestamp="2026-08-24T00:00:02+00:00",
    )
    conn.commit()
    store.calls.clear()

    assert replay_vector_outbox(provider) == {"claimed": 1, "completed": 1, "failed": 0}
    assert provider._vector_row_count == 0
    assert provider._vector_unique_id_count == 0
    assert "memory-1" not in store.records
    assert "contains_id" not in store.calls
    _assert_no_corpus_scan(store)
    assert table.last_query is None
    assert _membership_ids(conn, str(provider._vector_generation_id)) == []


def test_unindexed_lance_unready_generation_does_not_scan_or_change_counts() -> None:
    conn = _truth_conn()
    ensure_vector_generation_schema(conn)
    table = _PushdownLanceTable()
    store = _LanceReplaySpyStore(table)
    provider = _ordinary_replay_fixture(
        conn,
        store,
        memory_id="memory-stale",
        event_key="lance-unready",
        operation="upsert",
        content="historical row",
        timestamp="2026-08-24T00:00:00+00:00",
        row_count=5,
        unique_id_count=5,
    )
    conn.execute(
        """
        UPDATE vector_generations
        SET row_count = 5, unique_id_count = 5
        WHERE generation_id = ?
        """,
        (str(provider._vector_generation_id),),
    )
    mark_membership_unready(conn, str(provider._vector_generation_id))
    conn.commit()

    assert replay_vector_outbox(provider) == {"claimed": 1, "completed": 1, "failed": 0}
    assert "memory-stale" in store.records
    assert provider._vector_row_count == 5
    assert provider._vector_unique_id_count == 5
    assert "contains_id" not in store.calls
    _assert_no_corpus_scan(store)
    assert table.last_query is None
    assert _membership_ids(conn, str(provider._vector_generation_id)) == []


def test_replay_retry_does_not_double_membership_counts() -> None:
    conn = _truth_conn()
    ensure_vector_generation_schema(conn)
    store = _ForbiddenEnumerationStore()
    provider = _ordinary_replay_fixture(
        conn,
        store,
        memory_id="memory-1",
        event_key="retry-insert",
        operation="upsert",
        content="retry insert",
        timestamp="2026-08-24T00:00:00+00:00",
    )

    assert replay_vector_outbox(provider) == {"claimed": 1, "completed": 1, "failed": 0}
    assert provider._vector_row_count == 1
    assert _membership_ids(conn, str(provider._vector_generation_id)) == ["memory-1"]

    conn.execute(
        """
        UPDATE vector_outbox
        SET status = 'pending', worker_id = '', completed_at = '', last_error = '',
            available_at = '2026-08-24T00:00:00+00:00'
        WHERE generation_id = ? AND memory_id = ?
        """,
        (str(provider._vector_generation_id), "memory-1"),
    )
    conn.commit()
    store.calls.clear()

    assert replay_vector_outbox(provider) == {"claimed": 1, "completed": 1, "failed": 0}
    assert provider._vector_row_count == 1
    assert provider._vector_unique_id_count == 1
    assert _membership_ids(conn, str(provider._vector_generation_id)) == ["memory-1"]
    _assert_no_corpus_scan(store)


def test_cardinality_persist_failure_rolls_back_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _truth_conn()
    ensure_vector_generation_schema(conn)
    store = _ForbiddenEnumerationStore()
    provider = _ordinary_replay_fixture(
        conn,
        store,
        memory_id="memory-1",
        event_key="persist-fail",
        operation="upsert",
        content="persist fail",
        timestamp="2026-08-24T00:00:00+00:00",
    )

    def boom(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("cardinality persist failed")

    monkeypatch.setattr(
        "scope_recall.vector_runtime.update_generation_cardinality",
        boom,
    )
    with pytest.raises(RuntimeError, match="cardinality persist failed"):
        _apply_incremental_vector_counts(
            provider,
            operation="upsert",
            memory_id="memory-1",
            existed=None,
        )

    assert provider._vector_row_count == 0
    assert provider._vector_unique_id_count == 0
    assert _membership_ids(conn, str(provider._vector_generation_id)) == []


def test_repeated_ordinary_writes_do_not_scan_or_filter_lance() -> None:
    conn = _truth_conn()
    ensure_vector_generation_schema(conn)
    table = _PushdownLanceTable()
    store = _LanceReplaySpyStore(table)
    first = _ordinary_replay_fixture(
        conn,
        store,
        memory_id="memory-a",
        event_key="repeat-a",
        operation="upsert",
        content="first repeated write",
        timestamp="2026-08-24T00:00:00+00:00",
    )
    generation_id = str(first._vector_generation_id)
    assert replay_vector_outbox(first) == {"claimed": 1, "completed": 1, "failed": 0}

    for index, memory_id in enumerate(("memory-b", "memory-c"), start=1):
        store_row(
            conn,
            memory_id=memory_id,
            scope_id="scope-a",
            platform="test",
            user_id="sample-user",
            chat_id="",
            thread_id="",
            gateway_session_key="",
            agent_identity="sample-agent",
            agent_workspace="test",
            session_id="session-a",
            source="fixture",
            target="memory",
            content=f"repeated write {memory_id}",
            metadata="{}",
            allow_duplicate=True,
            timestamp=f"2026-08-24T00:00:0{index}+00:00",
            enqueue_vector_intent=False,
        )
        enqueue_vector_event(
            conn,
            event_key=f"repeat-{memory_id}",
            generation_id=generation_id,
            memory_id=memory_id,
            operation="upsert",
            timestamp=f"2026-08-24T00:00:0{index}+00:00",
        )
    conn.commit()
    store.calls.clear()

    assert replay_vector_outbox(first) == {"claimed": 2, "completed": 2, "failed": 0}
    assert first._vector_row_count == 3
    assert first._vector_unique_id_count == 3
    assert set(_membership_ids(conn, generation_id)) == {"memory-a", "memory-b", "memory-c"}
    assert "contains_id" not in store.calls
    _assert_no_corpus_scan(store)
    assert table.last_query is None


def test_explicit_refresh_vector_audit_rebuilds_membership_ledger(tmp_path: Path) -> None:
    store = SQLiteBruteForceVectorStore(
        tmp_path / "vector.sqlite3",
        table_name="memories",
        dimensions=2,
        metric="cosine",
    )
    store.open()
    try:
        store.upsert(
            VectorRecord(
                id="doctor-1",
                scope_id="scope-a",
                source="fixture",
                target="memory",
                content="doctor audit row",
                summary="doctor audit",
                updated_at="2026-08-24T00:00:00+00:00",
                vector=[1.0, 0.0],
            )
        )
        store.upsert(
            VectorRecord(
                id="doctor-2",
                scope_id="scope-a",
                source="fixture",
                target="memory",
                content="second doctor audit row",
                summary="second doctor audit",
                updated_at="2026-08-24T00:00:01+00:00",
                vector=[0.0, 1.0],
            )
        )
        conn = _truth_conn()
        ensure_vector_generation_schema(conn)
        manifest = bootstrap_legacy_generation(
            conn,
            identity=GenerationIdentity(
                backend="sqlite",
                provider="fixture",
                model="hash-v1",
                dimensions=2,
            ),
            storage_path="vector-generations/doctor-membership",
        )
        generation_id = str(manifest["generation_id"])
        provider = SimpleNamespace(
            _vector_store=store,
            _vector_generation_id=generation_id,
            _lock=threading.RLock(),
            _vector_lock=threading.RLock(),
            _vector_row_count=0,
            _vector_unique_id_count=0,
            _vector_duplicate_row_count=0,
            _require_conn=lambda: conn,
        )

        assert _membership_ids(conn, generation_id) == []
        counts = refresh_vector_audit(provider, persist=True)

        assert counts == store.audit_counts()
        assert counts["physical_rows"] == 2
        assert counts["unique_ids"] == 2
        assert provider._vector_row_count == 2
        assert provider._vector_unique_id_count == 2
        assert _membership_ids(conn, generation_id) == ["doctor-1", "doctor-2"]
        assert membership_is_ready(conn, generation_id) is True
    finally:
        store.close()
