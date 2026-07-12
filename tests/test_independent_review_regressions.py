"""Executable regressions for independent release-review findings.

These tests exercise destructive boundaries directly rather than inferring safety
from happy-path release checks.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from scope_recall.capture_filters import sanitize_capture_text
from scope_recall.doctor_vector import vector_report
from scope_recall.embedders import LocalHashEmbedder
from scope_recall.lifecycle_service import hard_delete_memories
from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.sqlite_vector_store import SQLiteBruteForceVectorStore
from scope_recall.vector_generation import (
    GenerationCompatibilityError,
    GenerationIdentity,
    bootstrap_legacy_generation,
    ensure_vector_generation_schema,
    register_generation,
)
from scope_recall.vector_migration import build_vector_generation
from scope_recall.vector_runtime import cleanup_persisted_vector_companions, sync_vector_index
from scope_recall.vector_store import VectorStoreCompatibilityError
import scope_recall.vector_repair as vector_repair


def _identity(*, dimensions: int = 8) -> GenerationIdentity:
    return GenerationIdentity(
        backend="sqlite-bruteforce",
        provider="local-hash",
        model="hash-v1",
        dimensions=dimensions,
        metric="cosine",
        prompt_profile="default-v1",
        table_name="memories",
    )


def _store_memory(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    lifecycle: str = "active",
    target: str = "memory",
    content: str | None = None,
) -> None:
    store_row(
        conn,
        memory_id=memory_id,
        scope_id="scope-a",
        platform="test",
        user_id="operator",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="test-agent",
        agent_workspace="test-workspace",
        session_id="review-regression",
        source="fixture",
        target=target,
        content=content or f"independent review fixture {memory_id}",
        metadata={"lifecycle": lifecycle},
        allow_duplicate=True,
    )
    row = conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
    metadata = json.loads(str(row[0] or "{}"))
    metadata["lifecycle"] = lifecycle
    conn.execute(
        "UPDATE memories SET metadata = ? WHERE id = ?",
        (json.dumps(metadata, sort_keys=True), memory_id),
    )


def _vector_record(memory_id: str, *, dimensions: int = 8, target: str = "memory") -> dict[str, object]:
    return {
        "id": memory_id,
        "scope_id": "scope-a",
        "source": "fixture",
        "target": target,
        "content": f"independent review fixture {memory_id}",
        "summary": f"independent review fixture {memory_id}",
        "updated_at": "2026-07-10T00:00:00+00:00",
        "vector": [0.25] * dimensions,
    }


def _active_generation_home(tmp_path: Path) -> tuple[Path, Path, sqlite3.Connection, GenerationIdentity]:
    hermes_home = tmp_path / "hermes-home"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _store_memory(conn, "active-row")
    identity = _identity()
    ensure_vector_generation_schema(conn)
    register_generation(
        conn,
        generation_id="gen-active",
        identity=identity,
        storage_path="vector-generations/gen-active",
        status="active",
        row_count=1,
        unique_id_count=1,
    )
    conn.execute(
        "INSERT INTO vector_generation_state(key, value, updated_at) VALUES (?, ?, ?)",
        ("current_generation", "gen-active", "2026-07-10T00:00:00+00:00"),
    )
    conn.commit()
    generation_path = storage / "vector-generations" / "gen-active" / "vector.sqlite3"
    store = SQLiteBruteForceVectorStore(generation_path, table_name="memories", dimensions=identity.dimensions)
    store.open()
    store.upsert_records([_vector_record("active-row", dimensions=identity.dimensions)])
    store.close()
    return hermes_home, generation_path, conn, identity


def _sqlite_ids(path: Path, *, dimensions: int = 8) -> list[str]:
    store = SQLiteBruteForceVectorStore(path, table_name="memories", dimensions=dimensions)
    store.open()
    try:
        return store.list_ids()
    finally:
        store.close()


def test_sqlite_dimension_mismatch_is_non_destructive(tmp_path: Path) -> None:
    path = tmp_path / "vector.sqlite3"
    original = SQLiteBruteForceVectorStore(path, table_name="memories", dimensions=2)
    original.open()
    original.upsert_records([_vector_record("sentinel", dimensions=2)])
    original.close()

    incompatible = SQLiteBruteForceVectorStore(path, table_name="memories", dimensions=3)
    with pytest.raises(VectorStoreCompatibilityError, match="dimension"):
        incompatible.open()
    incompatible.close()

    assert _sqlite_ids(path, dimensions=2) == ["sentinel"]


def test_candidate_cleanup_uses_active_generation_storage_path(tmp_path: Path) -> None:
    hermes_home, generation_path, conn, identity = _active_generation_home(tmp_path)
    conn.close()

    result = cleanup_persisted_vector_companions(
        hermes_home / "scope-recall",
        memory_ids=["active-row"],
        vector_config={
            "enabled": True,
            "backend": "sqlite-bruteforce",
            "table_name": "memories",
            "embedder": {"dimensions": identity.dimensions},
        },
        retrieval_config={"metric": "cosine"},
    )

    assert result["status"] == "ok"
    assert result["deleted"] == 1
    assert _sqlite_ids(generation_path, dimensions=identity.dimensions) == []


def test_doctor_inspects_active_generation_storage_path(tmp_path: Path) -> None:
    hermes_home, generation_path, conn, identity = _active_generation_home(tmp_path)
    conn.close()

    payload, check, _recommendations = vector_report(
        hermes_home,
        expected_embedder={
            "provider": identity.provider,
            "model": identity.model,
            "dimensions": identity.dimensions,
            "metric": identity.metric,
            "prompt_profile": identity.prompt_profile,
            "document_prefix": identity.document_prefix,
            "query_prefix": identity.query_prefix,
            "request_dimensions": identity.request_dimensions,
            "available": True,
        },
        backend=identity.backend,
    )

    assert check["ok"] is True
    assert Path(str(payload["path"])) == generation_path
    assert payload["row_count"] == 1


def test_vector_sync_excludes_candidate_and_in_progress_rows(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _store_memory(conn, "active-row", lifecycle="active")
    _store_memory(conn, "candidate-row", lifecycle="candidate")
    _store_memory(conn, "in-progress-row", lifecycle="in_progress")
    conn.commit()

    store = SQLiteBruteForceVectorStore(tmp_path / "vector.sqlite3", table_name="memories", dimensions=8)
    store.open()
    provider = SimpleNamespace(
        _vector_store=store,
        _embedder=LocalHashEmbedder(dimensions=8, model="hash-v1"),
        _vector_config={"index_general": False},
        _retrieval_config={},
        _lock=threading.RLock(),
        _vector_lock=threading.RLock(),
        _vector_row_count=0,
        _vector_unique_id_count=0,
        _vector_duplicate_row_count=0,
        _require_conn=lambda: conn,
        _vector_text=lambda summary, content: summary or content,
    )
    try:
        assert sync_vector_index(provider) == 1
        assert store.list_ids() == ["active-row"]
    finally:
        store.close()
        conn.close()


class _FailFirstCommitConnection:
    def __init__(self, raw: sqlite3.Connection) -> None:
        self.raw = raw
        self.fail_next_commit = True

    @property
    def in_transaction(self) -> bool:
        return self.raw.in_transaction

    def execute(self, *args, **kwargs):
        return self.raw.execute(*args, **kwargs)

    def commit(self) -> None:
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise sqlite3.OperationalError("injected final hard-delete commit failure")
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()


def test_hard_delete_final_commit_failure_rolls_back_before_vector_side_effect(tmp_path: Path) -> None:
    raw = sqlite3.connect(tmp_path / "memory.sqlite3")
    raw.row_factory = sqlite3.Row
    ensure_schema(raw)
    _store_memory(raw, "subject")
    bootstrap_legacy_generation(raw, identity=_identity(), row_count=1)
    raw.commit()
    conn = _FailFirstCommitConnection(raw)
    vector_calls: list[list[str]] = []

    with pytest.raises(sqlite3.OperationalError, match="injected final hard-delete commit failure"):
        hard_delete_memories(
            conn,  # type: ignore[arg-type]
            memory_ids=["subject"],
            vector_delete=lambda ids: vector_calls.append(list(ids)),
            actor="test",
            reason="commit failure regression",
            batch_id="commit-failure",
        )

    assert raw.execute("SELECT COUNT(*) FROM memories WHERE id = 'subject'").fetchone()[0] == 1
    assert raw.execute("SELECT COUNT(*) FROM vector_outbox WHERE memory_id = 'subject'").fetchone()[0] == 0
    assert raw.execute("SELECT COUNT(*) FROM governance_audit_events WHERE target_id = 'subject'").fetchone()[0] == 0
    assert vector_calls == []
    raw.close()


@pytest.mark.parametrize("width", [48, 60, 63])
@pytest.mark.parametrize("escaped", [False, True])
def test_folded_data_url_short_base64_lines_are_removed(width: int, escaped: bool) -> None:
    fold = "\\n" if escaped else "\n"
    raw = f"before data:image/png;base64,{'A' * width}{fold}{'B' * width}; after"

    sanitized = sanitize_capture_text(raw)

    assert "data:image" not in sanitized
    assert "A" * width not in sanitized
    assert "B" * width not in sanitized
    assert "before" in sanitized
    assert "; after" in sanitized


def test_folded_data_url_preserves_non_base64_prose_line() -> None:
    raw = f"before data:image/png;base64,{'A' * 60}\nKeep this sentence after the payload."

    sanitized = sanitize_capture_text(raw)

    assert "A" * 60 not in sanitized
    assert "Keep this sentence after the payload." in sanitized


@pytest.mark.parametrize(
    ("field", "value"),
    [("target", "project"), ("summary", "summary changed without updated_at")],
)
def test_ready_generation_activation_detects_vector_contract_drift_without_updated_at(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    storage = tmp_path / "scope-recall"
    storage.mkdir()
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _store_memory(conn, "source-row")
    identity = _identity(dimensions=8)
    old = bootstrap_legacy_generation(conn, identity=identity, row_count=0)
    conn.commit()

    build_vector_generation(
        storage,
        conn,
        generation_id="gen-ready",
        identity=identity,
        embedder=LocalHashEmbedder(dimensions=8, model="hash-v1"),
        index_general=False,
        activate=False,
        expected_current=old["generation_id"],
    )
    conn.execute(f"UPDATE memories SET {field} = ? WHERE id = 'source-row'", (value,))
    conn.commit()

    with pytest.raises(GenerationCompatibilityError, match="source snapshot is stale"):
        build_vector_generation(
            storage,
            conn,
            generation_id="gen-ready",
            identity=identity,
            embedder=LocalHashEmbedder(dimensions=8, model="hash-v1"),
            index_general=False,
            activate=True,
            activate_existing_ready=True,
            expected_current=old["generation_id"],
        )
    conn.close()


def test_hidden_vector_repair_aborts_before_delete_when_truth_drifts_after_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_home = tmp_path / "hermes-home"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    truth_path = storage / "memory.sqlite3"
    conn = sqlite3.connect(truth_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _store_memory(conn, "hidden-row", lifecycle="archived")
    conn.commit()
    conn.close()

    vector_path = storage / "vector.sqlite3"
    store = SQLiteBruteForceVectorStore(vector_path, table_name="memories", dimensions=8)
    store.open()
    store.upsert_records([_vector_record("hidden-row", dimensions=8)])
    store.close()

    original_backup = vector_repair._backup_companion

    def backup_then_change_truth(item, root):
        receipt = original_backup(item, root)
        mutator = sqlite3.connect(truth_path)
        row = mutator.execute("SELECT metadata FROM memories WHERE id = 'hidden-row'").fetchone()
        metadata = json.loads(str(row[0] or "{}"))
        metadata["lifecycle"] = "active"
        mutator.execute(
            "UPDATE memories SET metadata = ?, summary = ? WHERE id = 'hidden-row'",
            (json.dumps(metadata, sort_keys=True), "truth changed after backup"),
        )
        mutator.commit()
        mutator.close()
        return receipt

    monkeypatch.setattr(vector_repair, "_backup_companion", backup_then_change_truth)

    result = vector_repair.repair_hidden_vector_companions(
        hermes_home,
        apply=True,
        quiescent_confirmed=True,
    )

    assert result["ok"] is False
    assert result["status"] == "blocked_truth_drift"
    assert result["deleted"] == 0
    assert _sqlite_ids(vector_path, dimensions=8) == ["hidden-row"]
