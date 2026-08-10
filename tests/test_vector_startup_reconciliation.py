"""Bounded, crash-recoverable vector startup reconciliation contracts."""

from __future__ import annotations

import sqlite3
import threading
import inspect
from pathlib import Path
from typing import Any

import pytest

import scope_recall.capture as capture
import scope_recall.vector_reconciliation as vector_reconciliation
from scope_recall.doctor_vector import vector_generation_report
from scope_recall.sql_store import ensure_schema
from scope_recall.vector_generation import (
    GenerationIdentity,
    bootstrap_legacy_generation,
    enqueue_vector_event,
)
from scope_recall.vector_reconciliation import (
    prepare_vector_reconciliation_page,
    vector_reconciliation_state,
)
from scope_recall.vector_runtime import (
    run_bounded_vector_reconciliation,
    setup_vector_layer,
)


class _BoundedStore:
    """Physical store double that fails on every full-enumeration API."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []

    def upsert_records(self, rows: list[dict[str, Any]]) -> None:
        assert len(rows) == 1
        for row in rows:
            memory_id = str(row["id"])
            self.records[memory_id] = dict(row)
            self.calls.append(("upsert", memory_id))

    def delete_by_ids(self, ids: list[str]) -> None:
        assert len(ids) == 1
        for memory_id in ids:
            self.records.pop(memory_id, None)
            self.calls.append(("delete", memory_id))

    def count_rows(self) -> int:
        return len(self.records)

    def list_records(self) -> dict[str, dict[str, Any]]:
        raise AssertionError("ordinary startup called full vector list_records")

    def list_ids(self) -> list[str]:
        raise AssertionError("ordinary startup called full vector list_ids")

    def audit_counts(self) -> dict[str, int]:
        raise AssertionError("ordinary startup called full vector audit_counts")


class _Embedder:
    dimensions = 2
    provider = "fixture"
    model = "bounded-v1"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [float(len(text)), 1.0]


class _Provider:
    def __init__(
        self,
        conn: sqlite3.Connection,
        generation_id: str,
        *,
        page_size: int,
        outbox_limit: int,
        store: _BoundedStore | None = None,
        embedder: _Embedder | None = None,
    ) -> None:
        self._conn = conn
        self._lock = threading.RLock()
        self._vector_lock = self._lock
        self._vector_store = store or _BoundedStore()
        self._embedder = embedder or _Embedder()
        self._vector_generation_id = generation_id
        self._vector_config = {
            "index_general": False,
            "startup_reconcile_page_size": page_size,
            "startup_outbox_limit": outbox_limit,
            "startup_reconcile_interval_seconds": 86_400,
        }
        self._vector_status = "ready"
        self._vector_ready = True
        self._vector_message = ""
        self._vector_row_count = 0
        self._vector_unique_id_count = 0
        self._vector_duplicate_row_count = 0
        self._scope_id = "scope-vector"
        self._storage_dir = None

    def _require_conn(self) -> sqlite3.Connection:
        return self._conn

    @staticmethod
    def _vector_text(summary: str, content: str) -> str:
        return f"{summary}\n{content}".strip()


def _seed_truth(conn: sqlite3.Connection, row_count: int) -> None:
    timestamp = "2026-07-21T00:00:00+00:00"
    conn.executemany(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES(?, 'scope-vector', 'fixture', 'memory', ?, ?, ?, ?, '{}')
        """,
        (
            (
                f"memory-{index:06d}",
                f"durable truth {index}",
                f"truth {index}",
                timestamp,
                timestamp,
            )
            for index in range(row_count)
        ),
    )
    conn.commit()


def _generation(conn: sqlite3.Connection) -> str:
    manifest = bootstrap_legacy_generation(
        conn,
        identity=GenerationIdentity(
            backend="sqlite",
            provider="fixture",
            model="bounded-v1",
            dimensions=2,
        ),
        storage_path="vector-generations/bounded-startup",
    )
    conn.commit()
    return str(manifest["generation_id"])


def test_setup_entrypoint_contains_no_full_reconciliation_or_audit_call() -> None:
    """Static guard against reintroducing cardinality-bound startup helpers."""

    source = inspect.getsource(setup_vector_layer)
    assert "sync_vector_index(" not in source
    assert "refresh_vector_audit(" not in source
    assert "list_records(" not in source
    assert "list_ids(" not in source


@pytest.mark.parametrize("row_count", (10_000, 100_000))
def test_startup_reconciliation_touches_only_one_twenty_five_row_page(
    tmp_path: Path,
    row_count: int,
) -> None:
    """Dynamic RB-6 counterexample: startup cost is independent of truth size."""

    db_path = tmp_path / f"bounded-{row_count}.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _seed_truth(conn, row_count)
    generation_id = _generation(conn)
    store = _BoundedStore()
    embedder = _Embedder()
    provider = _Provider(
        conn,
        generation_id,
        page_size=25,
        outbox_limit=25,
        store=store,
        embedder=embedder,
    )

    result = run_bounded_vector_reconciliation(provider)

    assert result["planned"] == 25
    assert result["completed"] == 25
    assert result["failed"] == 0
    assert result["status"] == "running"
    assert len(embedder.calls) == 25
    assert len(store.records) == 25
    watermark = vector_reconciliation_state(conn, generation_id=generation_id)
    assert watermark is not None
    assert watermark["status"] == "running"
    assert int(watermark["processed_rows"]) == 25
    assert watermark["cursor_memory_id"] == "memory-000024"

    # A new provider instance resumes the durable cursor instead of starting at 0.
    resumed = _Provider(
        conn,
        generation_id,
        page_size=25,
        outbox_limit=25,
        store=store,
        embedder=embedder,
    )
    second = run_bounded_vector_reconciliation(resumed)
    assert second["planned"] == 25
    assert second["completed"] == 25
    watermark = vector_reconciliation_state(conn, generation_id=generation_id)
    assert watermark is not None
    assert int(watermark["processed_rows"]) == 50
    assert watermark["cursor_memory_id"] == "memory-000049"
    assert len(embedder.calls) == 50
    conn.close()


def test_existing_outbox_backlog_blocks_truth_page_and_watermark_advance() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _seed_truth(conn, 5)
    generation_id = _generation(conn)
    for index in range(3):
        enqueue_vector_event(
            conn,
            event_key=f"urgent-delete-{index}",
            generation_id=generation_id,
            memory_id=f"missing-{index}",
            operation="delete",
            payload={"reason": "pre-existing durable debt"},
        )
    conn.commit()
    store = _BoundedStore()
    provider = _Provider(
        conn,
        generation_id,
        page_size=2,
        outbox_limit=1,
        store=store,
    )

    result = run_bounded_vector_reconciliation(provider)

    assert result["completed"] == 1
    assert result["planned"] == 0
    assert result["status"] == "outbox_pending"
    assert result["replayable"] == 2
    assert store.calls == [("delete", "missing-0")]
    assert vector_reconciliation_state(conn, generation_id=generation_id) is None
    conn.close()


def test_ready_startup_prunes_only_old_completed_outbox_history() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _seed_truth(conn, 0)
    generation_id = _generation(conn)
    conn.executemany(
        """
        INSERT INTO vector_outbox(
            event_key, generation_id, memory_id, operation, payload, status,
            available_at, created_at, updated_at, completed_at
        ) VALUES (?, ?, ?, 'delete', '{}', 'completed', ?, ?, ?, ?)
        """,
        (
            (
                f"old-completed-{index}",
                generation_id,
                f"missing-{index}",
                "2020-01-01T00:00:00+00:00",
                "2020-01-01T00:00:00+00:00",
                "2020-01-01T00:00:00+00:00",
                "2020-01-01T00:00:00+00:00",
            )
            for index in range(5)
        ),
    )
    conn.commit()
    provider = _Provider(
        conn,
        generation_id,
        page_size=2,
        outbox_limit=2,
    )
    provider._vector_config.update(
        {
            "outbox_completed_retention_days": 30,
            "outbox_completed_keep_per_generation": 2,
        }
    )

    result = run_bounded_vector_reconciliation(provider)

    assert result["status"] == "completed"
    assert result["outbox_retention"]["status"] == "pruned"
    assert result["outbox_retention"]["deleted"] == 3
    assert conn.execute(
        "SELECT COUNT(*) FROM vector_outbox WHERE status='completed'"
    ).fetchone()[0] == 2
    conn.close()


def test_page_outbox_and_watermark_roll_back_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _seed_truth(conn, 3)
    generation_id = _generation(conn)
    original = vector_reconciliation.enqueue_vector_event
    calls = 0

    def fail_second(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected page planning failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(vector_reconciliation, "enqueue_vector_event", fail_second)
    with pytest.raises(RuntimeError, match="injected page planning failure"):
        prepare_vector_reconciliation_page(
            conn,
            generation_id=generation_id,
            should_index_row=lambda _target, _metadata: True,
            page_size=2,
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM vector_outbox WHERE generation_id=?",
        (generation_id,),
    ).fetchone()[0] == 0
    assert vector_reconciliation_state(conn, generation_id=generation_id) is None

    monkeypatch.setattr(vector_reconciliation, "enqueue_vector_event", original)
    recovered = prepare_vector_reconciliation_page(
        conn,
        generation_id=generation_id,
        should_index_row=lambda _target, _metadata: True,
        page_size=2,
    )
    assert recovered["planned"] == 2
    state = vector_reconciliation_state(conn, generation_id=generation_id)
    assert state is not None
    assert int(state["processed_rows"]) == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM vector_outbox WHERE generation_id=?",
        (generation_id,),
    ).fetchone()[0] == 2
    conn.close()


def test_doctor_reports_running_and_failed_reconciliation_watermark(
    tmp_path: Path,
) -> None:
    db_dir = tmp_path / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _seed_truth(conn, 3)
    generation_id = _generation(conn)
    prepared = prepare_vector_reconciliation_page(
        conn,
        generation_id=generation_id,
        should_index_row=lambda _target, _metadata: True,
        page_size=1,
    )
    assert prepared["status"] == "running"
    conn.execute(
        "UPDATE vector_generations SET backend='sqlite-bruteforce' WHERE generation_id=?",
        (generation_id,),
    )
    conn.execute(
        "UPDATE vector_outbox SET status='completed', completed_at='2026-07-21T00:00:01+00:00' WHERE generation_id=?",
        (generation_id,),
    )
    conn.commit()
    expected = {
        "available": True,
        "provider": "fixture",
        "model": "bounded-v1",
        "dimensions": 2,
        "metric": "cosine",
        "prompt_profile": "default-v1",
        "document_prefix": "",
        "query_prefix": "",
        "request_dimensions": False,
    }

    payload, check, recommendations = vector_generation_report(
        tmp_path,
        expected_embedder=expected,
        backend="sqlite",
    )
    assert check["ok"] is True, check
    assert payload["reconciliation"]["status"] == "running"
    assert payload["reconciliation"]["cursor_memory_id"] == "memory-000000"
    assert any("cycle is in progress" in item for item in recommendations)

    conn.execute(
        """
        UPDATE vector_reconciliation_state
        SET status='failed', last_error='token=abcdefghijklmnopqrstuvwxyz'
        WHERE generation_id=?
        """,
        (generation_id,),
    )
    conn.commit()
    payload, check, recommendations = vector_generation_report(
        tmp_path,
        expected_embedder=expected,
        backend="sqlite",
    )
    assert payload["status"] == "reconciliation_failed"
    assert payload["reconciliation"]["status"] == "failed"
    assert "abcdefghijklmnopqrstuvwxyz" not in payload["reconciliation"]["last_error"]
    assert check["ok"] is False
    assert any("watermark is in failed state" in item for item in check["failures"])
    assert any("resume the bounded cycle" in item for item in recommendations)
    conn.close()


def test_bounded_reconciliation_updates_active_manifest_cardinality() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _seed_truth(conn, 1)
    generation_id = _generation(conn)
    provider = _Provider(
        conn,
        generation_id,
        page_size=1,
        outbox_limit=1,
    )

    result = run_bounded_vector_reconciliation(provider)

    assert result["completed"] == 1
    manifest = conn.execute(
        "SELECT row_count, unique_id_count FROM vector_generations WHERE generation_id=?",
        (generation_id,),
    ).fetchone()
    assert dict(manifest) == {"row_count": 1, "unique_id_count": 1}
    conn.close()


def test_startup_reconcile_disabled_skips_outbox_and_truth_planning(
    tmp_path: Path, monkeypatch
) -> None:
    """Explicit disable must not touch outbox replay or truth watermark planning."""

    conn = sqlite3.connect(tmp_path / "disabled.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _seed_truth(conn, 3)
    generation_id = _generation(conn)
    provider = _Provider(conn, generation_id, page_size=2, outbox_limit=2)
    provider._db_path = tmp_path / "disabled.sqlite3"
    provider._storage_dir = tmp_path
    provider._vector_config["startup_reconcile_enabled"] = False

    calls: list[str] = []

    def boom_replay(*args, **kwargs):
        del args, kwargs
        calls.append("replay")
        raise AssertionError("disabled reconciliation must not replay outbox")

    def boom_prepare(*args, **kwargs):
        del args, kwargs
        calls.append("prepare")
        raise AssertionError("disabled reconciliation must not plan truth pages")

    monkeypatch.setattr(
        "scope_recall.vector_runtime.replay_vector_outbox", boom_replay
    )
    monkeypatch.setattr(
        "scope_recall.vector_runtime.prepare_vector_reconciliation_page", boom_prepare
    )

    result = run_bounded_vector_reconciliation(provider)

    assert result["status"] == "disabled"
    assert result["claimed"] == 0
    assert result["completed"] == 0
    assert result["failed"] == 0
    assert result["planned"] == 0
    assert calls == []
    assert vector_reconciliation_state(conn, generation_id=generation_id) is None
    backlog = conn.execute(
        "SELECT COUNT(*) FROM vector_outbox WHERE generation_id=?",
        (generation_id,),
    ).fetchone()[0]
    assert backlog == 0
    assert not (tmp_path / ".vector-mutation.lock").exists()
    conn.close()


def test_corrupt_sqlite_header_blocks_reconciliation_without_outbox_writes(
    tmp_path: Path, monkeypatch
) -> None:
    """Live pager probe must fail closed before outbox/truth reconciliation work."""

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    generation_id = _generation(conn)
    provider = _Provider(conn, generation_id, page_size=2, outbox_limit=2)
    provider._storage_dir = tmp_path
    monkeypatch.setattr(
        "scope_recall.vector_runtime.probe_truth_database_connection",
        lambda _conn: {
            "ok": False,
            "status": "corrupt_or_unreadable",
            "error": "SQLite truth database probe failed",
        },
    )

    calls: list[str] = []

    def boom_replay(*args, **kwargs):
        del args, kwargs
        calls.append("replay")
        raise AssertionError("corrupt header must not replay outbox")

    monkeypatch.setattr(
        "scope_recall.vector_runtime.replay_vector_outbox", boom_replay
    )

    result = run_bounded_vector_reconciliation(provider)

    assert result["status"] == "failed"
    assert result["failed"] == 1
    assert "probe" in str(result.get("error") or "").lower()
    assert result["claimed"] == 0
    assert result["planned"] == 0
    assert calls == []
    conn.close()


def test_corrupt_sqlite_header_warns_background_maintenance(
    tmp_path: Path, caplog, monkeypatch
) -> None:
    """The writer must surface a failed pager preflight instead of staying silent."""

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    generation_id = _generation(conn)
    provider = _Provider(conn, generation_id, page_size=2, outbox_limit=2)
    provider._storage_dir = tmp_path
    provider._config = {"relation_extraction_enabled": True}
    monkeypatch.setattr(
        "scope_recall.vector_runtime.probe_truth_database_connection",
        lambda _conn: {
            "ok": False,
            "status": "corrupt_or_unreadable",
            "error": "SQLite truth database probe failed",
        },
    )
    caplog.set_level("WARNING", logger=capture.__name__)

    capture._drain_relation_rebuild_debt(provider)

    assert any(
        "bounded vector maintenance failed" in record.getMessage()
        for record in caplog.records
    )
    conn.close()


def test_default_startup_reconcile_enabled_still_plans_truth_page(tmp_path: Path) -> None:
    """Default contract remains enabled so ordinary startups still reconcile."""

    conn = sqlite3.connect(tmp_path / "default-enabled.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _seed_truth(conn, 2)
    generation_id = _generation(conn)
    provider = _Provider(conn, generation_id, page_size=2, outbox_limit=2)
    provider._db_path = tmp_path / "default-enabled.sqlite3"
    provider._storage_dir = tmp_path
    # intentionally omit startup_reconcile_enabled

    result = run_bounded_vector_reconciliation(provider)

    assert result["status"] != "disabled"
    assert int(result["planned"]) == 2
    conn.close()
