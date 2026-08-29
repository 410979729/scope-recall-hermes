"""Regression tests for release-blocking lifecycle and vector invariants."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from typing import Any

import pytest

from _scope_recall_public_memory_port import attach_public_truth_ports
from scope_recall.lifecycle_registry import (
    BENCHMARK_MARK_LIFECYCLE,
    MEMORY_CLEANUP_ARCHIVE,
    MEMORY_CLEANUP_RESTORE,
)
from scope_recall.lifecycle_service import hard_delete_memories, transition_memory_lifecycle
import scope_recall.memory_ops as memory_ops
from scope_recall.memory_ops import archive_memories, dedupe_memories
from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.vector_generation import (
    GenerationIdentity,
    bootstrap_legacy_generation,
    current_generation,
    enqueue_vector_event,
)
import scope_recall.vector_runtime as vector_runtime


def _conn(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _add_memory(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    scope_id: str = "scope-a",
    content: str = "same",
    memory_type: str = "factual",
) -> None:
    store_row(
        conn,
        memory_id=memory_id,
        scope_id=scope_id,
        platform="test",
        user_id="test-user",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="test-agent",
        agent_workspace="hermes",
        session_id="release-blocker-regression",
        source="fixture",
        target="memory",
        content=content,
        metadata={"lifecycle": "promoted", "memory_type": memory_type},
        allow_duplicate=True,
    )


def _register_generation(conn: sqlite3.Connection, *, backend: str = "lancedb") -> None:
    bootstrap_legacy_generation(
        conn,
        identity=GenerationIdentity(
            backend=backend,
            provider="local-hash",
            model="hash-v1",
            dimensions=16,
            metric="cosine",
            prompt_profile="default-v1",
        ),
        row_count=0,
    )
    conn.commit()


class _MemoryProvider:
    def __init__(self, conn: sqlite3.Connection, *, vector_store=None) -> None:
        self._conn = conn
        self._lock = threading.RLock()
        self._vector_lock = threading.RLock()
        self._vector_store = vector_store
        self._embedder = None
        self._vector_enabled = True
        self._vector_ready = vector_store is not None
        self._vector_status = "ready" if vector_store is not None else "needs_repair"
        self._vector_message = ""
        self._writable_scope_ids = ["scope-a"]
        self._accessible_scope_ids = ["scope-a"]
        attach_public_truth_ports(self)

    def _require_conn(self) -> sqlite3.Connection:
        return self._conn


def test_dedupe_without_generation_fails_closed_and_preserves_truth(tmp_path):
    conn = _conn(tmp_path / "memory.sqlite3")
    _add_memory(conn, "dupe-a")
    _add_memory(conn, "dupe-b")
    conn.commit()

    with pytest.raises(RuntimeError, match="durable vector delete outbox"):
        dedupe_memories(_MemoryProvider(conn), dry_run=False, scope_only=False)

    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM governance_audit_events").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM vector_outbox").fetchone()[0] == 0


def test_dedupe_degraded_never_initialized_vector_has_no_outbox_precondition(tmp_path):
    conn = _conn(tmp_path / "memory.sqlite3")
    _add_memory(conn, "dupe-a")
    _add_memory(conn, "dupe-b")
    conn.commit()
    provider = _MemoryProvider(conn)
    provider._vector_status = "degraded"

    result = dedupe_memories(provider, dry_run=False, scope_only=False)

    assert result["ok"] is True
    assert result["deleted"] == 1
    assert result["vector_status"] == "not_required"
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM vector_outbox").fetchone()[0] == 0


def test_vector_delete_requirement_reads_generation_when_runtime_is_disabled(tmp_path):
    conn = _conn(tmp_path / "memory.sqlite3")
    _add_memory(conn, "dupe-a")
    _add_memory(conn, "dupe-b")
    _register_generation(conn)
    provider = _MemoryProvider(conn)
    provider._vector_enabled = False
    provider._vector_status = "disabled"

    result = dedupe_memories(provider, dry_run=False, scope_only=False)

    assert result["deleted"] == 1
    assert result["vector_pending"] is True
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM vector_outbox WHERE operation = 'delete'").fetchone()[0] == 1


def test_vector_delete_requirement_fails_closed_for_store_without_generation(tmp_path):
    conn = _conn(tmp_path / "memory.sqlite3")
    provider = _MemoryProvider(conn, vector_store=object())

    assert vector_runtime.vector_delete_intent_required(provider) is True


def test_dedupe_degraded_store_uses_durable_outbox_without_silent_zero(tmp_path):
    conn = _conn(tmp_path / "memory.sqlite3")
    _add_memory(conn, "dupe-a")
    _add_memory(conn, "dupe-b")
    _register_generation(conn)

    result = dedupe_memories(_MemoryProvider(conn), dry_run=False, scope_only=False)

    assert result["ok"] is True
    assert result["deleted"] == 1
    assert result["vector_status"] == "pending"
    assert result["vector_pending"] is True
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    outbox = conn.execute("SELECT operation, status FROM vector_outbox").fetchone()
    assert tuple(outbox) == ("delete", "pending")


def test_dedupe_preserves_identical_text_with_distinct_memory_types(tmp_path):
    """Equal text is not a duplicate when the durable semantic type differs."""

    conn = _conn(tmp_path / "memory.sqlite3")
    _add_memory(conn, "fact", content="Same durable text.", memory_type="factual")
    _add_memory(conn, "procedure", content="Same durable text.", memory_type="procedure")
    conn.commit()

    result = dedupe_memories(_MemoryProvider(conn), dry_run=True, scope_only=False)

    assert result["duplicate_groups"] == 0
    assert result["duplicates"] == 0
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2


def _archive_fixture(tmp_path: Path, *memory_ids: str, generation: bool = True):
    conn = _conn(tmp_path / "archive.sqlite3")
    for memory_id in memory_ids:
        _add_memory(conn, memory_id, content=f"archive {memory_id}")
    if generation:
        _register_generation(conn)
    conn.commit()
    return conn, _MemoryProvider(conn)


def _complete_exact_events(conn: sqlite3.Connection, event_ids: list[int]) -> dict[str, int]:
    for event_id in event_ids:
        conn.execute(
            "UPDATE vector_outbox SET status='completed', completed_at=updated_at WHERE id=?",
            (event_id,),
        )
    conn.commit()
    return {"claimed": len(event_ids), "completed": len(event_ids), "failed": 0}


def _set_exact_event_status(
    conn: sqlite3.Connection,
    event_ids: list[int],
    status: str,
) -> dict[str, int]:
    for event_id in event_ids:
        conn.execute(
            "UPDATE vector_outbox SET status=? WHERE id=?",
            (status, event_id),
        )
    conn.commit()
    return {
        "claimed": len(event_ids),
        "completed": 0,
        "failed": len(event_ids),
    }


def test_hard_delete_collects_exact_vector_outbox_keys(tmp_path):
    conn, _provider = _archive_fixture(tmp_path, "hard-delete-key")

    result = hard_delete_memories(
        conn,
        memory_ids=["hard-delete-key"],
        scope_ids=["scope-a"],
        require_vector_delete=True,
        actor="test",
        reason="exact key regression",
    )

    row = conn.execute(
        "SELECT event_key FROM vector_outbox WHERE memory_id='hard-delete-key'"
    ).fetchone()
    assert row is not None
    assert result["vector_outbox_keys"] == [str(row["event_key"])]
    assert result["outbox_enqueued"] == 1


def test_hard_delete_zero_generation_has_no_vector_debt(tmp_path):
    conn, provider = _archive_fixture(
        tmp_path,
        "hard-delete-no-generation",
        generation=False,
    )
    provider._vector_status = "degraded"
    try:
        result = memory_ops.delete_memories_result(
            provider, ["hard-delete-no-generation"]
        )

        assert result.deleted_count == 1
        assert result.vector_outbox_keys == ()
        assert result.vector_pending is False
        assert result.companion_erasure_pending is False
        assert conn.execute("SELECT COUNT(*) FROM vector_outbox").fetchone()[0] == 0
    finally:
        conn.close()


def test_hard_delete_store_unavailable_reports_exact_pending(tmp_path):
    conn, provider = _archive_fixture(tmp_path, "hard-delete-unavailable")

    result = memory_ops.delete_memories_result(provider, ["hard-delete-unavailable"])

    assert result.vector_pending is True
    assert result.companion_erasure_pending is True
    assert len(result.vector_outbox_keys) == 1
    assert conn.execute(
        "SELECT status FROM vector_outbox WHERE event_key=?",
        (result.vector_outbox_keys[0],),
    ).fetchone()[0] == "pending"


def test_hard_delete_unrelated_completed_backlog_does_not_clear_target_event(
    tmp_path,
    monkeypatch,
):
    conn, provider = _archive_fixture(tmp_path, "hard-delete-target")
    generation_id = str(current_generation(conn)["generation_id"])
    unrelated = enqueue_vector_event(
        conn,
        event_key="hard-delete-unrelated-completed",
        generation_id=generation_id,
        memory_id="unrelated",
        operation="delete",
    )
    conn.execute(
        "UPDATE vector_outbox SET status='completed', completed_at=updated_at WHERE id=?",
        (int(unrelated["id"]),),
    )
    conn.commit()
    monkeypatch.setattr(
        vector_runtime,
        "replay_vector_outbox_events",
        lambda _provider, *, event_ids: {
            "claimed": 200,
            "completed": 200,
            "failed": 0,
        },
    )

    result = memory_ops.delete_memories_result(provider, ["hard-delete-target"])

    assert result.vector_pending is True
    assert result.companion_erasure_pending is True


def test_hard_delete_exact_target_completed_clears_pending(tmp_path, monkeypatch):
    conn, provider = _archive_fixture(tmp_path, "hard-delete-complete")
    monkeypatch.setattr(
        vector_runtime,
        "replay_vector_outbox_events",
        lambda _provider, *, event_ids: _complete_exact_events(conn, list(event_ids)),
    )

    result = memory_ops.delete_memories_result(provider, ["hard-delete-complete"])

    assert result.vector_pending is False
    assert result.companion_erasure_pending is False


def test_hard_delete_exact_target_retry_reports_pending(tmp_path, monkeypatch):
    conn, provider = _archive_fixture(tmp_path, "hard-delete-retry")
    monkeypatch.setattr(
        vector_runtime,
        "replay_vector_outbox_events",
        lambda _provider, *, event_ids: _set_exact_event_status(
            conn, list(event_ids), "retry"
        ),
    )

    result = memory_ops.delete_memories_result(provider, ["hard-delete-retry"])

    assert result.vector_pending is True
    assert conn.execute(
        "SELECT status FROM vector_outbox WHERE event_key=?",
        (result.vector_outbox_keys[0],),
    ).fetchone()[0] == "retry"


def test_hard_delete_exact_target_dead_letter_requires_repair(tmp_path, monkeypatch):
    _conn_handle, provider = _archive_fixture(tmp_path, "hard-delete-dead-letter")
    conn = provider._require_conn()
    monkeypatch.setattr(
        vector_runtime,
        "replay_vector_outbox_events",
        lambda _provider, *, event_ids: _set_exact_event_status(
            conn, list(event_ids), "dead_letter"
        ),
    )

    result = memory_ops.delete_memories_result(provider, ["hard-delete-dead-letter"])

    assert result.vector_pending is True
    assert provider._vector_status == "needs_repair"
    assert provider._vector_reason_code == "exact_outbox_dead_letter"


def test_hard_delete_missing_exact_event_fails_closed_pending(tmp_path, monkeypatch):
    conn, provider = _archive_fixture(tmp_path, "hard-delete-missing")

    def remove_events(_provider, *, event_ids):
        for event_id in event_ids:
            conn.execute("DELETE FROM vector_outbox WHERE id=?", (int(event_id),))
        conn.commit()
        return {"claimed": 0, "completed": 0, "failed": 0}

    monkeypatch.setattr(vector_runtime, "replay_vector_outbox_events", remove_events)

    result = memory_ops.delete_memories_result(provider, ["hard-delete-missing"])

    assert result.vector_pending is True
    assert result.companion_erasure_pending is True


def test_hard_delete_multi_id_one_pending_reports_pending(tmp_path, monkeypatch):
    conn, provider = _archive_fixture(
        tmp_path,
        "hard-delete-first",
        "hard-delete-second",
    )

    def complete_first(_provider, *, event_ids):
        return _complete_exact_events(conn, [int(event_ids[0])])

    monkeypatch.setattr(vector_runtime, "replay_vector_outbox_events", complete_first)

    result = memory_ops.delete_memories_result(
        provider,
        ["hard-delete-first", "hard-delete-second"],
    )

    assert result.deleted_count == 2
    assert result.vector_pending is True


def test_dedupe_uses_exact_delete_event_status(tmp_path, monkeypatch):
    conn = _conn(tmp_path / "dedupe-exact.sqlite3")
    _add_memory(conn, "dupe-a")
    _add_memory(conn, "dupe-b")
    _register_generation(conn)
    provider = _MemoryProvider(conn)
    monkeypatch.setattr(
        vector_runtime,
        "replay_vector_outbox_events",
        lambda _provider, *, event_ids: {
            "claimed": 200,
            "completed": 200,
            "failed": 0,
        },
    )

    result = dedupe_memories(provider, dry_run=False, scope_only=False)

    assert result["deleted"] == 1
    assert result["vector_pending"] is True


def test_archive_collects_exact_vector_outbox_keys(tmp_path):
    conn, provider = _archive_fixture(tmp_path, "archive-a", "archive-b")

    result = memory_ops._archive_memories_truth(provider, ["archive-a", "archive-b"])

    rows = conn.execute(
        "SELECT event_key FROM vector_outbox WHERE memory_id IN ('archive-a', 'archive-b') ORDER BY id"
    ).fetchall()
    assert result["outbox_enqueued"] == 2
    assert result["vector_outbox_keys"] == [str(row[0]) for row in rows]


def test_archive_no_generation_has_no_companion_debt(tmp_path):
    conn, provider = _archive_fixture(tmp_path, "archive-no-generation", generation=False)

    result = archive_memories(provider, ["archive-no-generation"])

    assert result["vector_outbox_keys"] == []
    assert result["outbox_enqueued"] == 0
    assert result["vector_pending"] is False
    assert result["companion_erasure_pending"] is False
    assert conn.execute("SELECT COUNT(*) FROM vector_outbox").fetchone()[0] == 0


def test_archive_generation_present_but_store_unavailable_reports_pending(tmp_path):
    _conn_handle, provider = _archive_fixture(tmp_path, "archive-store-unavailable")

    result = archive_memories(provider, ["archive-store-unavailable"])

    assert result["vector_replay"] == {"claimed": 0, "completed": 0, "failed": 0}
    assert result["vector_outbox_status_counts"] == {"pending": 1}
    assert result["vector_pending"] is True
    assert result["companion_erasure_pending"] is True


def test_archive_zero_claim_replay_does_not_clear_exact_pending_event(tmp_path, monkeypatch):
    _conn_handle, provider = _archive_fixture(tmp_path, "archive-zero-claim")
    monkeypatch.setattr(
        vector_runtime,
        "replay_vector_outbox_events",
        lambda _provider, *, event_ids: {"claimed": 0, "completed": 0, "failed": 0},
    )

    result = archive_memories(provider, ["archive-zero-claim"])

    assert result["vector_outbox_status_counts"] == {"pending": 1}
    assert result["vector_pending"] is True


def test_archive_unrelated_completed_backlog_does_not_clear_target_event(tmp_path, monkeypatch):
    conn, provider = _archive_fixture(tmp_path, "archive-target")
    generation_id = str(current_generation(conn)["generation_id"])
    unrelated = enqueue_vector_event(
        conn,
        event_key="unrelated-completed",
        generation_id=generation_id,
        memory_id="unrelated",
        operation="delete",
    )
    conn.execute(
        "UPDATE vector_outbox SET status='completed', completed_at=updated_at WHERE id=?",
        (int(unrelated["id"]),),
    )
    conn.commit()
    monkeypatch.setattr(
        vector_runtime,
        "replay_vector_outbox_events",
        lambda _provider, *, event_ids: {"claimed": 1, "completed": 1, "failed": 0},
    )

    result = archive_memories(provider, ["archive-target"])

    assert result["vector_pending"] is True
    assert result["vector_outbox_status_counts"] == {"pending": 1}


def test_archive_exact_event_completion_clears_pending(tmp_path, monkeypatch):
    conn, provider = _archive_fixture(tmp_path, "archive-complete")
    monkeypatch.setattr(
        vector_runtime,
        "replay_vector_outbox_events",
        lambda _provider, *, event_ids: _complete_exact_events(conn, list(event_ids)),
    )

    result = archive_memories(provider, ["archive-complete"])

    assert result["vector_outbox_status_counts"] == {"completed": 1}
    assert result["vector_pending"] is False
    assert result["companion_erasure_pending"] is False


def test_archive_multi_id_one_pending_reports_pending(tmp_path, monkeypatch):
    conn, provider = _archive_fixture(tmp_path, "archive-first", "archive-second")

    def complete_first(_provider, *, event_ids):
        return _complete_exact_events(conn, [int(event_ids[0])])

    monkeypatch.setattr(vector_runtime, "replay_vector_outbox_events", complete_first)

    result = archive_memories(provider, ["archive-first", "archive-second"])

    assert result["vector_outbox_status_counts"] == {"completed": 1, "pending": 1}
    assert result["vector_pending"] is True


def test_archive_already_archived_no_change_adds_no_event(tmp_path):
    conn, provider = _archive_fixture(tmp_path, "archive-already")
    metadata = json.loads(
        conn.execute("SELECT metadata FROM memories WHERE id='archive-already'").fetchone()[0]
    )
    metadata["lifecycle"] = "archived"
    conn.execute(
        "UPDATE memories SET metadata=? WHERE id='archive-already'",
        (json.dumps(metadata, sort_keys=True),),
    )
    conn.commit()

    result = archive_memories(provider, ["archive-already"])

    assert result["archived"] == 0
    assert result["vector_outbox_keys"] == []
    assert conn.execute("SELECT COUNT(*) FROM vector_outbox").fetchone()[0] == 0


def test_archive_dead_letter_reports_needs_repair(tmp_path, monkeypatch):
    conn, provider = _archive_fixture(tmp_path, "archive-dead-letter")
    provider._vector_status = "ready"

    def dead_letter(_provider, *, event_ids):
        conn.execute(
            "UPDATE vector_outbox SET status='dead_letter' WHERE id=?",
            (int(event_ids[0]),),
        )
        conn.commit()
        return {"claimed": 1, "completed": 0, "failed": 1}

    monkeypatch.setattr(vector_runtime, "replay_vector_outbox_events", dead_letter)

    result = archive_memories(provider, ["archive-dead-letter"])

    assert result["vector_pending"] is True
    assert result["vector_outbox_status_counts"] == {"dead_letter": 1}
    assert provider._vector_status == "needs_repair"


def test_archive_replay_exception_reports_companion_pending(tmp_path, monkeypatch):
    _conn_handle, provider = _archive_fixture(tmp_path, "archive-replay-error")

    def fail_replay(_provider, *, event_ids):
        del event_ids
        raise RuntimeError("isolated replay failure")

    monkeypatch.setattr(vector_runtime, "replay_vector_outbox_events", fail_replay)

    result = archive_memories(provider, ["archive-replay-error"])

    assert result["vector_pending"] is True
    assert result["companion_erasure_pending"] is True
    assert result["vector_replay"]["failed"] == 1


class _VectorProvider:
    def __init__(self, conn: sqlite3.Connection, storage_dir: Path) -> None:
        self._conn = conn
        self._storage_dir = storage_dir
        self._lock = threading.RLock()
        self._vector_lock = threading.RLock()
        self._vector_config = {
            "enabled": True,
            "backend": "lancedb",
            "fallback_backend": "sqlite-bruteforce",
            "table_name": "memories",
            "embedder": {"provider": "local-hash", "model": "hash-v1", "dimensions": 16},
            "fallback_embedder": {
                "provider": "local-hash",
                "model": "hash-v1",
                "dimensions": 16,
            },
        }
        self._retrieval_config = {"metric": "cosine"}
        self._scope_id = "scope-a"
        self._vector_store: Any = None
        self._vector_backend = "lancedb"
        self._vector_ready = False
        self._vector_status = "disabled"
        self._vector_message = ""
        self._vector_generation_id = ""
        self._vector_storage_dir = storage_dir
        self._embedder: Any = None

    def _require_conn(self) -> sqlite3.Connection:
        return self._conn

    @staticmethod
    def _vector_text(summary: str, content: str) -> str:
        return f"{summary}\n{content}"


def test_lancedb_fallback_registers_actual_sqlite_generation(tmp_path, monkeypatch):
    conn = _conn(tmp_path / "memory.sqlite3")
    provider = _VectorProvider(conn, tmp_path)
    monkeypatch.setattr(vector_runtime.LanceVectorStore, "is_available", lambda self: False)

    vector_runtime.setup_vector_layer(provider)

    manifest = current_generation(conn)
    assert provider._vector_status == "ready"
    assert provider._vector_backend == "sqlite-bruteforce"
    assert manifest is not None
    assert manifest["backend"] == "sqlite-bruteforce"
    assert manifest["storage_path"] == "."
    assert Path(provider._vector_store.db_path) == tmp_path / "vector.sqlite3"
    provider._vector_store.close()


def test_corrupt_truth_header_degrades_vector_startup(tmp_path, monkeypatch):
    """A failed live pager probe must keep the vector companion non-ready."""

    conn = _conn(tmp_path / "memory.sqlite3")
    provider = _VectorProvider(conn, tmp_path)
    monkeypatch.setattr(
        vector_runtime,
        "probe_truth_database_connection",
        lambda _conn: {
            "ok": False,
            "status": "corrupt_or_unreadable",
            "error": "SQLite truth database probe failed",
        },
    )
    monkeypatch.setattr(vector_runtime.LanceVectorStore, "is_available", lambda self: False)

    vector_runtime.setup_vector_layer(provider)

    assert provider._vector_ready is False
    assert provider._vector_status == "degraded"
    assert provider._vector_store is None
    assert provider._vector_reconciliation["status"] == "failed"
    assert provider._vector_reconciliation["failed"] == 1
    assert "probe" in provider._vector_message.lower()
    conn.close()


@pytest.mark.parametrize("lancedb_recovers", [False, True], ids=["still-unavailable", "recovered"])
def test_active_sqlite_fallback_generation_reopens_without_implicit_backend_switch(
    tmp_path,
    monkeypatch,
    lancedb_recovers,
):
    conn = _conn(tmp_path / "memory.sqlite3")
    first = _VectorProvider(conn, tmp_path)
    monkeypatch.setattr(vector_runtime.LanceVectorStore, "is_available", lambda self: False)

    vector_runtime.setup_vector_layer(first)

    initial_manifest = current_generation(conn)
    assert initial_manifest is not None
    assert initial_manifest["backend"] == "sqlite-bruteforce"
    generation_id = str(initial_manifest["generation_id"])
    first._vector_store.close()

    _add_memory(conn, "restart-outbox", content="replay on the active fallback generation")
    enqueue_vector_event(
        conn,
        event_key=f"{generation_id}:upsert:restart-outbox",
        generation_id=generation_id,
        memory_id="restart-outbox",
        operation="upsert",
    )
    conn.commit()

    monkeypatch.setattr(
        vector_runtime.LanceVectorStore,
        "is_available",
        lambda self: lancedb_recovers,
    )
    if lancedb_recovers:
        monkeypatch.setattr(
            vector_runtime.LanceVectorStore,
            "open",
            lambda self: (_ for _ in ()).throw(AssertionError("active fallback must not switch backend implicitly")),
        )
    restarted = _VectorProvider(conn, tmp_path)

    vector_runtime.setup_vector_layer(restarted)

    manifest = current_generation(conn)
    outbox = conn.execute(
        "SELECT generation_id, status FROM vector_outbox WHERE memory_id = ?",
        ("restart-outbox",),
    ).fetchone()
    assert restarted._vector_ready is True
    assert restarted._vector_status == "ready"
    assert restarted._vector_backend == "sqlite-bruteforce"
    assert restarted._vector_generation_id == generation_id
    assert manifest is not None
    assert manifest["generation_id"] == generation_id
    assert manifest["backend"] == "sqlite-bruteforce"
    assert Path(restarted._vector_store.db_path) == tmp_path / "vector.sqlite3"
    assert tuple(outbox) == (generation_id, "completed")
    restarted._vector_store.close()


def test_active_fallback_generation_requires_explicit_fallback_configuration(tmp_path):
    conn = _conn(tmp_path / "memory.sqlite3")
    _register_generation(conn, backend="sqlite-bruteforce")
    provider = _VectorProvider(conn, tmp_path)
    provider._vector_config.pop("fallback_backend")

    vector_runtime.setup_vector_layer(provider)

    manifest = current_generation(conn)
    assert provider._vector_ready is False
    assert provider._vector_status == "needs_repair"
    assert provider._vector_reason_code == "identity_mismatch"
    assert provider._vector_auto_recoverable is False
    assert provider._vector_repair_required is True
    assert provider._vector_store is None
    assert manifest is not None and manifest["backend"] == "sqlite-bruteforce"
    assert not (tmp_path / "vector.sqlite3").exists()


def test_existing_lancedb_manifest_rejects_sqlite_fallback(tmp_path, monkeypatch):
    conn = _conn(tmp_path / "memory.sqlite3")
    _register_generation(conn, backend="lancedb")
    provider = _VectorProvider(conn, tmp_path)
    monkeypatch.setattr(vector_runtime.LanceVectorStore, "is_available", lambda self: False)

    vector_runtime.setup_vector_layer(provider)

    manifest = current_generation(conn)
    assert provider._vector_ready is False
    assert provider._vector_status != "ready"
    assert provider._vector_store is None
    assert manifest is not None and manifest["backend"] == "lancedb"
    assert not (tmp_path / "vector.sqlite3").exists()


def test_hard_delete_durable_intent_is_the_only_ordinary_vector_side_effect(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = _conn(db_path)
    _add_memory(conn, "subject", content="durable callback observation")
    _register_generation(conn)
    direct_calls: list[list[str]] = []

    def observe(ids: list[str]) -> None:
        direct_calls.append(list(ids))

    result = hard_delete_memories(
        conn,
        memory_ids=["subject"],
        scope_ids=["scope-a"],
        vector_delete=observe,
        require_vector_delete=True,
        actor="test",
        reason="durable callback observation",
    )

    assert direct_calls == []
    assert result["durable"] is True
    assert result["vector_status"] == "pending"
    observer = sqlite3.connect(db_path)
    try:
        assert observer.execute(
            "SELECT COUNT(*) FROM governance_audit_events WHERE target_id='subject'"
        ).fetchone()[0] == 1
        assert observer.execute(
            "SELECT COUNT(*) FROM vector_outbox WHERE memory_id='subject' AND status='pending'"
        ).fetchone()[0] == 1
        assert observer.execute("SELECT COUNT(*) FROM memories WHERE id='subject'").fetchone()[0] == 0
    finally:
        observer.close()


def test_hard_delete_ignores_process_exit_callback_and_leaves_durable_delete_intent(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = _conn(db_path)
    _add_memory(conn, "subject", content="process exit crash window")
    _register_generation(conn)
    conn.close()
    child = """
import os, sqlite3, sys
from scope_recall.lifecycle_service import hard_delete_memories
path = sys.argv[1]
conn = sqlite3.connect(path)
conn.row_factory = sqlite3.Row
hard_delete_memories(
    conn,
    memory_ids=['subject'],
    scope_ids=['scope-a'],
    vector_delete=lambda _ids: os._exit(91),
    require_vector_delete=True,
    actor='crash-test',
    reason='intent must survive os._exit',
)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path.cwd()) + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run([sys.executable, "-c", child, str(db_path)], env=env, check=False)
    assert completed.returncode == 0

    observer = sqlite3.connect(db_path)
    assert observer.execute("SELECT COUNT(*) FROM memories WHERE id='subject'").fetchone()[0] == 0
    assert observer.execute(
        "SELECT COUNT(*) FROM governance_audit_events WHERE target_id='subject'"
    ).fetchone()[0] == 1
    outbox = observer.execute(
        "SELECT operation, status FROM vector_outbox WHERE memory_id='subject'"
    ).fetchone()
    assert tuple(outbox) == ("delete", "pending")


def _relation_fixture(tmp_path: Path, *, peer_scope: str = "scope-a") -> tuple[sqlite3.Connection, dict[str, object]]:
    conn = _conn(tmp_path / "memory.sqlite3")
    _add_memory(conn, "subject", content="relation subject")
    _add_memory(conn, "peer", scope_id=peer_scope, content="relation peer")
    _register_generation(conn)
    relation: dict[str, object] = {
        "source_memory_id": "subject",
        "target_memory_id": "peer",
        "relation_type": "supports",
        "confidence": 0.9,
        "note": "rollback fixture",
        "created_at": "2026-07-12T00:00:00+00:00",
    }
    conn.execute(
        """
        INSERT INTO memory_relations(
            source_memory_id, target_memory_id, relation_type, confidence, note, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        tuple(relation.values()),
    )
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    transition_memory_lifecycle(
        conn,
        memory_id="subject",
        lifecycle="archived",
        actor="test",
        reason="archive before rollback",
        operation_id=MEMORY_CLEANUP_ARCHIVE,
    )
    conn.commit()
    return conn, relation


def _restore_subject(conn: sqlite3.Connection, relation: dict[str, object]) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    result = transition_memory_lifecycle(
        conn,
        memory_id="subject",
        lifecycle="promoted",
        restore_relations=[relation],
        actor="test",
        reason="rollback relation",
        operation_id=MEMORY_CLEANUP_RESTORE,
    )
    conn.commit()
    return result


@pytest.mark.parametrize("peer_lifecycle", ["archived", "rejected"])
def test_relation_rollback_skips_hidden_peer(tmp_path, peer_lifecycle):
    conn, relation = _relation_fixture(tmp_path)
    conn.execute("BEGIN IMMEDIATE")
    transition_memory_lifecycle(
        conn,
        memory_id="peer",
        lifecycle=peer_lifecycle,
        actor="test",
        reason="hide peer",
        operation_id=(
            MEMORY_CLEANUP_ARCHIVE
            if peer_lifecycle == "archived"
            else BENCHMARK_MARK_LIFECYCLE
        ),
    )
    conn.commit()

    result = _restore_subject(conn, relation)

    assert result["relation_restore"]["restored"] == 0
    assert result["relation_restore"]["skipped"][0]["reason"] == "hidden_target"
    assert conn.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0] == 0
    audit_after = json.loads(
        conn.execute(
            "SELECT after_json FROM governance_audit_events WHERE action='rollback_soft_archive'"
        ).fetchone()[0]
    )
    assert audit_after["relation_restore"]["skipped"][0]["reason"] == "hidden_target"


def test_relation_rollback_skips_deleted_peer(tmp_path):
    conn, relation = _relation_fixture(tmp_path)
    conn.execute("DELETE FROM memories WHERE id='peer'")
    conn.commit()

    result = _restore_subject(conn, relation)

    assert result["relation_restore"]["skipped"][0]["reason"] == "missing_endpoint"
    assert conn.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0] == 0


def test_relation_rollback_skips_cross_scope_peer(tmp_path):
    conn, relation = _relation_fixture(tmp_path, peer_scope="scope-b")

    result = _restore_subject(conn, relation)

    assert result["relation_restore"]["skipped"][0]["reason"] == "cross_scope"
    assert conn.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0] == 0


def test_relation_rollback_skips_existing_contradiction(tmp_path):
    conn, relation = _relation_fixture(tmp_path)
    conn.execute(
        """
        INSERT INTO memory_relations(
            source_memory_id, target_memory_id, relation_type, confidence, note, created_at
        ) VALUES ('peer', 'subject', 'contradicts', 0.8, 'new evidence', '2026-07-12T01:00:00+00:00')
        """
    )
    conn.commit()

    result = _restore_subject(conn, relation)

    assert result["relation_restore"]["skipped"][0]["reason"] == "contradiction_conflict"
    rows = conn.execute("SELECT relation_type FROM memory_relations ORDER BY relation_type").fetchall()
    assert [row[0] for row in rows] == ["contradicts"]


def test_relation_rollback_rejects_unrelated_endpoints(tmp_path):
    """Restoring one memory cannot create an edge between two other memories."""

    conn, _relation = _relation_fixture(tmp_path)
    _add_memory(conn, "other-a", content="unrelated source")
    _add_memory(conn, "other-b", content="unrelated target")
    conn.commit()
    unrelated = {
        "source_memory_id": "other-a",
        "target_memory_id": "other-b",
        "relation_type": "supports",
        "confidence": 0.8,
        "note": "must not be restored through subject rollback",
        "created_at": "2026-07-12T02:00:00+00:00",
    }

    result = _restore_subject(conn, unrelated)

    assert result["relation_restore"]["restored"] == 0
    assert result["relation_restore"]["skipped"][0]["reason"] == "unrelated_endpoint"
    assert conn.execute(
        "SELECT COUNT(*) FROM memory_relations "
        "WHERE source_memory_id='other-a' AND target_memory_id='other-b'"
    ).fetchone()[0] == 0
