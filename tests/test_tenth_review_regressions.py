"""Regression gates for the tenth independent Scope Recall review.

These tests bind release blockers to behavior instead of a historical dirty-tree
snapshot.  They must stay self-contained and use only temporary SQLite/vector
state.
"""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
import importlib
import sqlite3
import threading
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from scope_recall.truth_connection import connect_truth_database


ROOT = Path(__file__).resolve().parents[1]


def _disable_unrelated_vector_runtime(hermes_home: Path) -> None:
    """Keep relation-only provider tests independent of native vector runtimes."""

    config_path = hermes_home / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        '{"vector":{"enabled":false}}\n',
        encoding="utf-8",
    )


def _vector_row(*, memory_id: str = "memory-1", content: str = "current") -> dict[str, Any]:
    return {
        "id": memory_id,
        "scope_id": "scope-a",
        "source": "fixture",
        "target": "memory",
        "content": content,
        "summary": "",
        "updated_at": "2026-07-19T00:00:00+00:00",
        "vector": [1.0, 0.0],
    }


def test_lancedb_upsert_uses_one_merge_insert_transaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One logical upsert must not expose a delete/add crash window."""

    from scope_recall.vector_store import LanceVectorStore

    calls: list[tuple[str, Any]] = []

    class MergeBuilder:
        def when_matched_update_all(self) -> "MergeBuilder":
            calls.append(("when_matched_update_all", None))
            return self

        def when_not_matched_insert_all(self) -> "MergeBuilder":
            calls.append(("when_not_matched_insert_all", None))
            return self

        def execute(self, rows: list[dict[str, Any]]) -> None:
            calls.append(("execute", rows))

    class Table:
        def merge_insert(self, key: str) -> MergeBuilder:
            calls.append(("merge_insert", key))
            return MergeBuilder()

        def delete(self, _predicate: str) -> None:
            raise AssertionError("upsert must not delete before insert")

        def add(self, _rows: list[dict[str, Any]]) -> None:
            raise AssertionError("upsert must not append outside merge_insert")

    store = object.__new__(LanceVectorStore)
    store._table = Table()
    store._db_path = tmp_path / "fixture.lance"
    monkeypatch.setattr(store, "_ensure_schema_compatible", lambda: None)

    row = _vector_row()
    store.upsert_records([row])

    assert calls == [
        ("merge_insert", "id"),
        ("when_matched_update_all", None),
        ("when_not_matched_insert_all", None),
        ("execute", [row]),
    ]


def test_lancedb_concurrent_handles_do_not_duplicate_ids(tmp_path: Path) -> None:
    """Independent handles share a generation-level physical write lock."""

    from scope_recall.vector_store import LanceVectorStore

    db_path = tmp_path / "concurrent.lance"
    stores = [
        LanceVectorStore(db_path, table_name="memories", dimensions=2)
        for _ in range(8)
    ]
    if not stores[0].is_available():
        pytest.skip("native LanceDB dependency is unavailable")
    for store in stores:
        store.open()

    try:
        barrier = threading.Barrier(len(stores))

        def write(index: int) -> None:
            barrier.wait(timeout=10)
            stores[index].upsert_records(
                [
                    _vector_row(
                        memory_id="same-id",
                        content=f"concurrent-{index}",
                    )
                ]
            )

        with ThreadPoolExecutor(max_workers=len(stores)) as pool:
            futures = [pool.submit(write, index) for index in range(len(stores))]
            for future in futures:
                future.result(timeout=30)

        assert stores[0].audit_counts() == {
            "physical_rows": 1,
            "unique_ids": 1,
            "duplicate_rows": 0,
            "duplicate_ids": 0,
        }
    finally:
        for store in reversed(stores):
            store.close()


def test_vector_doctor_rejects_duplicate_physical_ids_without_truth_db(tmp_path: Path) -> None:
    """Duplicate physical rows are unhealthy even before truth comparison."""

    from scope_recall.doctor_vector import apply_vector_truth_consistency

    payload = {
        "backend": "lancedb",
        "status": "ready",
        "ready": True,
        "row_count": 3,
        "unique_id_count": 2,
    }
    recommendations: list[str] = []

    result = apply_vector_truth_consistency(
        payload,
        hermes_home=tmp_path,
        index_general=False,
        recommendations=recommendations,
        vector_ids=["memory-1", "memory-1", "memory-2"],
    )

    assert result is not None
    checked_payload, check, checked_recommendations = result
    assert checked_payload["status"] == "needs_repair"
    assert checked_payload["ready"] is False
    assert checked_payload["duplicate_rows"] == 1
    assert checked_payload["duplicate_id_count"] == 1
    assert check["ok"] is False
    assert any("duplicate" in item.lower() for item in check["failures"])
    assert any("shadow generation" in item.lower() for item in checked_recommendations)


def test_digest_paths_do_not_import_or_call_direct_vector_upsert() -> None:
    """Truth commits may trigger replay, but never bypass the durable outbox."""

    for relative in ("journal.py", "nightly_digest.py"):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"), filename=relative)
        imported_names: set[str] = set()
        called_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported_names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called_names.add(node.func.id)
        assert "upsert_vector_record" not in imported_names, relative
        assert "upsert_vector_record" not in called_names, relative
        assert "replay_vector_outbox" in imported_names, relative
        assert "replay_vector_outbox" in called_names, relative


def test_shared_vector_replay_claims_committed_outbox_once() -> None:
    """Provider and digest callers share the same durable replay executor."""

    from scope_recall.sql_store import ensure_schema, store_row
    from scope_recall.vector_generation import GenerationIdentity, bootstrap_legacy_generation
    from scope_recall.vector_outbox_replay import replay_committed_vector_events

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    manifest = bootstrap_legacy_generation(
        conn,
        identity=GenerationIdentity(
            backend="lancedb",
            provider="local-hash",
            model="hash-v1",
            dimensions=2,
        ),
        row_count=0,
    )
    store_row(
        conn,
        memory_id="memory-1",
        scope_id="scope-a",
        platform="cli",
        user_id="local",
        chat_id="",
        thread_id="",
        gateway_session_key="",
        agent_identity="default",
        agent_workspace="hermes",
        session_id="session",
        source="fixture",
        target="memory",
        content="committed truth row",
        metadata="{}",
        allow_duplicate=True,
    )
    conn.commit()

    class Store:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []
            self.deleted: list[str] = []

        def upsert_records(self, rows: list[dict[str, Any]]) -> None:
            self.rows.extend(rows)

        def delete_by_ids(self, ids: list[str]) -> None:
            self.deleted.extend(ids)

    class Embedder:
        def embed(self, _text: str) -> list[float]:
            return [1.0, 0.0]

    store = Store()
    result = replay_committed_vector_events(
        conn,
        generation_id=str(manifest["generation_id"]),
        vector_store=store,
        embedder=Embedder(),
        vector_text=lambda summary, content: f"{summary}\n{content}".strip(),
        should_index_row=lambda _target, _metadata: True,
        default_scope_id="scope-a",
        db_lock=threading.RLock(),
        mutation_context=nullcontext,
        limit=10,
    )

    assert result == {"claimed": 1, "completed": 1, "failed": 0}
    assert [row["id"] for row in store.rows] == ["memory-1"]
    assert store.deleted == []
    status = conn.execute(
        "SELECT status, attempts FROM vector_outbox WHERE memory_id='memory-1'"
    ).fetchone()
    assert tuple(status) == ("completed", 1)
    assert replay_committed_vector_events(
        conn,
        generation_id=str(manifest["generation_id"]),
        vector_store=store,
        embedder=Embedder(),
        vector_text=lambda summary, content: f"{summary}\n{content}".strip(),
        should_index_row=lambda _target, _metadata: True,
        default_scope_id="scope-a",
        db_lock=threading.RLock(),
        mutation_context=nullcontext,
        limit=10,
    ) == {"claimed": 0, "completed": 0, "failed": 0}


def _insert_relation_scope_rows(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    focus_id: str,
    peer_count: int,
) -> None:
    rows = [
        (
            focus_id,
            scope_id,
            "Project Atlas deploy depends on Redis service availability.",
            "focus",
            "fixture",
            "project",
            '{"entities":["Project Atlas","Redis service"]}',
            "2026-07-19T00:00:00+00:00",
            "2026-07-19T00:00:00+00:00",
        )
    ]
    rows.extend(
        (
            f"peer-{index:04d}",
            scope_id,
            f"Peer fact {index} for deterministic relation budget coverage.",
            f"peer {index}",
            "fixture",
            "project",
            "{}",
            "2026-07-18T00:00:00+00:00",
            f"2026-07-18T00:{index % 60:02d}:00+00:00",
        )
        for index in range(peer_count)
    )
    conn.executemany(
        """
        INSERT INTO memories(
            id, scope_id, content, summary, source, target, metadata,
            created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.executemany(
        "INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)",
        [(str(row[0]), str(row[2]), str(row[3])) for row in rows],
    )
    from scope_recall.relation_frequency_index import sync_relation_frequency_memory

    for row in rows:
        sync_relation_frequency_memory(conn, str(row[0]))
    conn.commit()


def test_large_focus_sync_is_bounded_and_enqueues_durable_rebuild() -> None:
    from scope_recall.relation_extraction import sync_extracted_relations_for_memory
    from scope_recall.sql_store import ensure_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _insert_relation_scope_rows(
        conn,
        scope_id="large-scope",
        focus_id="focus-large",
        peer_count=1001,
    )

    result = sync_extracted_relations_for_memory(
        conn,
        memory_id="focus-large",
        scope_ids=["large-scope"],
        max_pairs=1000,
        local_peer_limit=8,
        batch_id="tenth-review-bounded",
    )

    assert result["ok"] is True
    assert result["blocked"] is False
    assert result["deferred"] is True
    assert int(result["compared_pairs"]) <= 8
    queued = conn.execute(
        """
        SELECT status, cursor_memory_id
        FROM relation_rebuild_queue
        WHERE scope_id='large-scope' AND focus_memory_id='focus-large'
        """
    ).fetchone()
    assert tuple(queued) == ("pending", "")


def test_large_scope_update_commits_truth_and_relation_debt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.memory import load_memory_provider

    _disable_unrelated_vector_runtime(tmp_path)
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    # This contract calls the synchronous private mutation path directly.  An
    # async capture worker can race to drain its intentionally large relation
    # debt and hold the provider lock across shutdown, obscuring the atomicity
    # assertion with unrelated background scheduling.
    monkeypatch.setitem(
        plugin.initialize.__func__.__globals__, "start_writer", lambda _provider: None
    )
    plugin.initialize(
        "session-tenth-review-large-update",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        plugin._config["relation_extraction_max_pairs"] = 1000
        plugin._config["relation_sync_neighbor_limit"] = 8
        conn = plugin._require_conn()
        with plugin._lock:
            _insert_relation_scope_rows(
                conn,
                scope_id=plugin._scope_id,
                focus_id="focus-update",
                peer_count=1001,
            )

        updated, summary, _updated_at = plugin._update_memory(
            "focus-update",
            "Project Atlas updated truth survives bounded relation debt.",
            "project",
        )

        assert updated is True
        assert "updated truth survives" in summary
        persisted = conn.execute(
            "SELECT content FROM memories WHERE id='focus-update'"
        ).fetchone()
        assert "updated truth survives" in str(persisted[0])
        queued = conn.execute(
            """
            SELECT status
            FROM relation_rebuild_queue
            WHERE focus_memory_id='focus-update'
            """
        ).fetchone()
        assert queued is not None
        assert queued[0] in {"pending", "processing", "completed"}
    finally:
        plugin.shutdown()


def test_store_rolls_back_truth_when_relation_debt_cannot_persist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.memory import load_memory_provider

    _disable_unrelated_vector_runtime(tmp_path)
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    monkeypatch.setitem(
        plugin.initialize.__func__.__globals__, "start_writer", lambda _provider: None
    )
    plugin.initialize(
        "session-tenth-review-store-atomic",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        plugin._config["relation_extraction_max_pairs"] = 1
        conn = plugin._require_conn()
        with plugin._lock:
            _insert_relation_scope_rows(
                conn,
                scope_id=plugin._scope_id,
                focus_id="preexisting-focus",
                peer_count=1,
            )
        before = {
            "memories": int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]),
            "fts": int(conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]),
            "entities": int(
                conn.execute("SELECT COUNT(*) FROM memory_entities").fetchone()[0]
            ),
            "outbox": int(conn.execute("SELECT COUNT(*) FROM vector_outbox").fetchone()[0]),
        }
        store_service = plugin._store_now.__func__.__globals__["store_memory_now"]
        sync_fn = store_service.__globals__["sync_extracted_relations_for_memory"]

        def fail_debt(*_args: Any, **_kwargs: Any) -> int:
            raise RuntimeError("injected relation debt persistence failure")

        monkeypatch.setitem(
            sync_fn.__globals__, "enqueue_relation_rebuild", fail_debt
        )
        with pytest.raises(
            RuntimeError, match="injected relation debt persistence failure"
        ):
            plugin._store_now(
                content="A new Project Atlas truth row that requires deferred relation work.",
                source="tool-store",
                target="project",
                session_id=plugin._session_id,
                allow_duplicate=True,
            )

        after = {
            "memories": int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]),
            "fts": int(conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]),
            "entities": int(
                conn.execute("SELECT COUNT(*) FROM memory_entities").fetchone()[0]
            ),
            "outbox": int(conn.execute("SELECT COUNT(*) FROM vector_outbox").fetchone()[0]),
        }
        assert after == before
    finally:
        plugin.shutdown()


def test_relation_rebuild_worker_recovers_expired_claim_and_converges() -> None:
    from scope_recall.relation_rebuild_queue import (
        claim_relation_rebuild_events,
        drain_relation_rebuild_queue,
        enqueue_relation_rebuild,
        relation_rebuild_queue_report,
    )
    from scope_recall.sql_store import ensure_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _insert_relation_scope_rows(
        conn,
        scope_id="queue-scope",
        focus_id="queue-focus",
        peer_count=5,
    )
    enqueue_relation_rebuild(
        conn,
        scope_id="queue-scope",
        focus_memory_id="queue-focus",
        requested_updated_at="2026-07-19T00:00:00+00:00",
        reason="tenth-review-crash-recovery",
        commit=True,
    )

    claimed = claim_relation_rebuild_events(
        conn,
        worker_id="crashed-worker",
        limit=1,
        lease_seconds=0,
        commit=True,
    )
    assert len(claimed) == 1
    assert claimed[0]["status"] == "processing"

    totals = {"claimed": 0, "chunks_completed": 0, "events_completed": 0, "failed": 0}
    for _ in range(5):
        result = drain_relation_rebuild_queue(
            conn,
            max_events=1,
            pair_limit=2,
            lease_seconds=0,
        )
        for key in totals:
            totals[key] += int(result[key])
        if relation_rebuild_queue_report(conn)["unresolved"] == 0:
            break

    report = relation_rebuild_queue_report(conn)
    assert totals["failed"] == 0
    assert totals["events_completed"] == 1
    assert report["unresolved"] == 0
    assert report["completed"] == 1
    row = conn.execute(
        """
        SELECT status, processed_pairs, cursor_memory_id
        FROM relation_rebuild_queue
        WHERE focus_memory_id='queue-focus'
        """
    ).fetchone()
    assert row["status"] == "completed"
    assert int(row["processed_pairs"]) == 5
    assert row["cursor_memory_id"] == "peer-0004"


def test_sqlite_doctor_surfaces_relation_debt_and_fails_dead_letter(
    tmp_path: Path,
) -> None:
    from scope_recall.doctor_sqlite import sqlite_report
    from scope_recall.relation_rebuild_queue import enqueue_relation_rebuild
    from scope_recall.sql_store import ensure_schema

    db_dir = tmp_path / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = connect_truth_database(db_path, mode="rwc")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _insert_relation_scope_rows(
        conn,
        scope_id="doctor-scope",
        focus_id="doctor-focus",
        peer_count=0,
    )
    enqueue_relation_rebuild(
        conn,
        scope_id="doctor-scope",
        focus_memory_id="doctor-focus",
        requested_updated_at="2026-07-19T00:00:00+00:00",
        reason="doctor visibility fixture",
        commit=True,
    )
    conn.close()

    payload, check, recommendations = sqlite_report(tmp_path)

    debt = payload["relation_rebuild_queue"]
    assert debt["pending"] == 1
    assert debt["unresolved"] == 1
    assert check["ok"] is True
    assert any("Relation rebuild debt" in item for item in recommendations)

    writer = sqlite3.connect(db_path)
    writer.execute(
        """
        UPDATE relation_rebuild_queue
        SET status='dead_letter', failures=5, last_error='fixture failure'
        WHERE focus_memory_id='doctor-focus'
        """
    )
    writer.commit()
    writer.close()

    failed_payload, failed_check, _failed_recommendations = sqlite_report(tmp_path)
    assert failed_payload["relation_rebuild_queue"]["dead_letter"] == 1
    assert failed_check["ok"] is False
    assert any("dead-letter" in item for item in failed_check["failures"])


def test_operator_ledger_recovers_file_written_before_mirror_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scope_recall import operator_ledger
    from scope_recall.doctor_sqlite import sqlite_report
    from scope_recall.sql_store import ensure_schema

    db_path = tmp_path / "scope-recall" / "memory.sqlite3"
    db_path.parent.mkdir(parents=True)
    conn = connect_truth_database(db_path, mode="rwc")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    operator_ledger.record_committed_operator_operation(
        conn,
        operation_id="op_test_receipt_recovery",
        operation_kind="playbook.promote",
        target_ref="pb_test",
        before={"status": "candidate"},
        result={"ok": True, "status": "promoted"},
        backup_path="/redacted/backup.sqlite3",
        request_fingerprint="f" * 64,
        commit=False,
    )
    conn.commit()
    assert operator_ledger.operator_ledger_report(conn)["pending"] == 1
    pending_payload, pending_check, _pending_recommendations = sqlite_report(tmp_path)
    assert pending_payload["operator_ledger"]["pending"] == 1
    assert pending_check["ok"] is True

    original_mark = operator_ledger._mark_receipt_mirrored

    def crash_after_file(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic crash after receipt rename")

    monkeypatch.setattr(operator_ledger, "_mark_receipt_mirrored", crash_after_file)
    with pytest.raises(RuntimeError, match="synthetic crash after receipt rename"):
        operator_ledger.mirror_operator_receipt(
            conn,
            db_path=db_path,
            operation_id="op_test_receipt_recovery",
        )
    receipt_paths = list((db_path.parent / "receipts").glob("*.json"))
    assert len(receipt_paths) == 1
    assert operator_ledger.operator_ledger_report(conn)["pending"] == 1

    monkeypatch.setattr(operator_ledger, "_mark_receipt_mirrored", original_mark)
    recovered = operator_ledger.recover_operator_receipts(
        conn,
        db_path=db_path,
        include_failed=True,
    )
    assert recovered["mirrored"] == 1
    assert recovered["failed"] == 0
    assert len(list((db_path.parent / "receipts").glob("*.json"))) == 1
    report = operator_ledger.operator_ledger_report(conn)
    assert report["pending"] == 0
    assert report["mirrored"] == 1

    conn.execute(
        "UPDATE operator_operations SET receipt_state='failed' WHERE operation_id=?",
        ("op_test_receipt_recovery",),
    )
    conn.commit()
    failed_payload, failed_check, _failed_recommendations = sqlite_report(tmp_path)
    assert failed_payload["operator_ledger"]["failed"] == 1
    assert failed_check["ok"] is False
    repaired_failed = operator_ledger.recover_operator_receipts(
        conn,
        db_path=db_path,
        include_failed=True,
    )
    assert repaired_failed["mirrored"] == 1
    assert len(list((db_path.parent / "receipts").glob("*.json"))) == 1
    conn.close()


def test_operator_receipt_racing_conflict_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator_ledger = importlib.import_module("scope_recall.operator_ledger")

    receipt_path = tmp_path / "receipts" / "playbooks.promote.op_race.json"
    expected = b'{"operation_id":"op_race"}\n'
    forged = b'{"operation_id":"forged"}\n'
    original_exists = Path.exists
    injected = False

    def inject_conflict_between_check_and_publish(path: Path) -> bool:
        nonlocal injected
        if path == receipt_path and not injected:
            injected = True
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(forged)
            # The conflicting file appeared immediately after the caller's
            # existence check but before publication of the temporary file.
            return False
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", inject_conflict_between_check_and_publish)

    with pytest.raises(FileExistsError, match="different evidence"):
        operator_ledger._write_receipt_mirror(receipt_path, expected)

    assert receipt_path.read_bytes() == forged
    assert not list(receipt_path.parent.glob("*.tmp"))
