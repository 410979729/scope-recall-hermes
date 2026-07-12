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

from scope_recall.lifecycle_service import hard_delete_memories, transition_memory_lifecycle
from scope_recall.memory_ops import dedupe_memories
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
        metadata={"lifecycle": "promoted", "memory_type": "factual"},
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
    assert provider._vector_status == "degraded"
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


def test_hard_delete_intent_is_visible_before_vector_callback(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = _conn(db_path)
    _add_memory(conn, "subject", content="durable callback observation")
    _register_generation(conn)
    during: dict[str, int] = {}

    def observe(_ids: list[str]) -> None:
        observer = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            during["audit"] = observer.execute(
                "SELECT COUNT(*) FROM governance_audit_events WHERE target_id='subject'"
            ).fetchone()[0]
            during["outbox"] = observer.execute(
                "SELECT COUNT(*) FROM vector_outbox WHERE memory_id='subject'"
            ).fetchone()[0]
            during["truth"] = observer.execute(
                "SELECT COUNT(*) FROM memories WHERE id='subject'"
            ).fetchone()[0]
        finally:
            observer.close()

    result = hard_delete_memories(
        conn,
        memory_ids=["subject"],
        scope_ids=["scope-a"],
        vector_delete=observe,
        require_vector_delete=True,
        actor="test",
        reason="durable callback observation",
    )

    assert during == {"audit": 1, "outbox": 1, "truth": 0}
    assert result["durable"] is True
    assert result["vector_status"] == "applied_pending_ack"


def test_hard_delete_process_exit_leaves_durable_delete_intent(tmp_path):
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
    assert completed.returncode == 91

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
        event_type="rollback_fixture",
        action="soft_archive",
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
        event_type="rollback_fixture",
        action="rollback_soft_archive",
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
        event_type="rollback_fixture",
        action="hide_peer",
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
