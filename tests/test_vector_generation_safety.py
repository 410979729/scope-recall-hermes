"""Adversarial safety tests for vector generations and non-destructive runtime behavior."""

from __future__ import annotations

import json
import sqlite3
import threading
from types import SimpleNamespace
from typing import Any

import pytest

import scope_recall.vector_runtime as vector_runtime_module
import scope_recall.vector_store as vector_store_module
from _scope_recall_public_memory_port import attach_public_truth_ports
from scope_recall import memory_ops
from scope_recall.doctor_vector import vector_generation_report
from scope_recall.embedders import LocalHashEmbedder
from scope_recall.provider import MemoryProvider
from scope_recall.sql_store import ensure_schema
from scope_recall.sqlite_vector_store import SQLiteBruteForceVectorStore
from scope_recall.truth_connection import probe_truth_database_header
from scope_recall.vector_generation import (
    GenerationCompatibilityError,
    GenerationIdentity,
    activate_generation,
    bootstrap_legacy_generation,
    claim_vector_events,
    complete_vector_event,
    current_generation,
    enqueue_vector_event,
    ensure_vector_generation_schema,
    fail_vector_event,
    finish_migration_receipt,
    generation_health_report,
    generation_manifest,
    prune_completed_vector_outbox,
    register_generation,
    retire_ready_generation,
    start_migration_receipt,
    validate_generation_compatibility,
)
from scope_recall.vector_runtime import (
    _open_vector_store,
    _prune_completed_outbox,
    _truth_header_preflight,
    replay_vector_outbox,
    setup_vector_layer,
)
from scope_recall.vector_migration import build_vector_generation
from scope_recall.vector_store import LanceVectorStore


def test_live_truth_preflight_never_raw_opens_the_pager_file(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("CREATE TABLE sentinel(value TEXT)")
    conn.commit()
    provider = SimpleNamespace(
        _db_path=db_path,
        _lock=threading.RLock(),
        _require_conn=lambda: conn,
    )

    def forbid_raw_open(*_args, **_kwargs):
        raise AssertionError("live SQLite pager files must not be raw-opened")

    monkeypatch.setattr(type(db_path), "open", forbid_raw_open)
    try:
        assert _truth_header_preflight(provider) is None
    finally:
        conn.close()


def test_offline_truth_header_probe_requires_quiesced_declaration(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE sentinel(value TEXT)")
    conn.commit()
    conn.close()

    refused = probe_truth_database_header(db_path)
    assert refused["ok"] is False
    assert refused["status"] == "unsafe_live_probe_refused"

    allowed = probe_truth_database_header(db_path, connections_quiesced=True)
    assert allowed == {"ok": True, "status": "ok"}


def test_live_truth_preflight_fails_closed_on_pager_database_error():
    class BrokenConnection:
        @staticmethod
        def execute(_statement):
            raise sqlite3.DatabaseError("file is not a database")

    provider = SimpleNamespace(
        _lock=threading.RLock(),
        _require_conn=lambda: BrokenConnection(),
    )

    result = _truth_header_preflight(provider)

    assert result is not None
    assert result["status"] == "failed"
    assert result["header_status"] == "corrupt_or_unreadable"
    assert result["probe_method"] == "sqlite_connection"
    assert "file is not a database" not in result["error"]


def _sentinel_row(dimensions: int) -> dict[str, object]:
    return {
        "id": "sentinel-3072",
        "scope_id": "scope-a",
        "source": "test",
        "target": "memory",
        "content": "healthy primary index sentinel",
        "summary": "healthy primary index sentinel",
        "updated_at": "2026-07-10T00:00:00+00:00",
        "vector": [0.0] * dimensions,
    }


def test_dimension_mismatch_never_drops_healthy_lancedb_table(tmp_path):
    vector_dir = tmp_path / "lancedb"
    original = LanceVectorStore(vector_dir, table_name="memories", dimensions=3072)
    original.open()
    original.upsert_records([_sentinel_row(3072)])
    original.close()

    provider = SimpleNamespace(
        _storage_dir=tmp_path,
        _vector_config={"backend": "lancedb", "table_name": "memories"},
        _retrieval_config={"metric": "cosine"},
        _vector_backend="lancedb",
        _vector_store=None,
        _vector_message="",
    )

    try:
        _open_vector_store(provider, dimensions=256)
    except RuntimeError:
        # A compatibility failure is the expected safe behavior. The durable
        # assertion below proves it did not mutate the existing generation.
        pass
    finally:
        if provider._vector_store is not None:
            provider._vector_store.close()

    reopened = LanceVectorStore(vector_dir, table_name="memories", dimensions=3072)
    reopened.open()
    try:
        assert reopened.dimensions == 3072
        assert reopened.count_rows() == 1
        assert reopened.list_ids() == ["sentinel-3072"]
    finally:
        reopened.close()


def _identity(*, model: str = "gemini-embedding-001", dimensions: int = 3072) -> GenerationIdentity:
    return GenerationIdentity(
        backend="lancedb",
        provider="openai-compatible",
        model=model,
        dimensions=dimensions,
        metric="cosine",
        prompt_profile="retrieval-v1",
        table_name="memories",
    )


@pytest.mark.parametrize(
    "storage_path",
    [
        "/" + "home/operator/private/generation",
        "../escape",
        "vector-generations/../escape",
        "C:" + "\\Users\\operator\\private\\generation",
        "\\\\" + "server\\share\\generation",
    ],
)
def test_generation_registration_rejects_invalid_storage_paths_before_persistence(storage_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_vector_generation_schema(conn)

    with pytest.raises(GenerationCompatibilityError, match="storage_path"):
        register_generation(
            conn,
            generation_id="gen-invalid-path",
            identity=_identity(),
            storage_path=storage_path,
            status="ready",
        )

    assert conn.execute("SELECT COUNT(*) FROM vector_generations").fetchone()[0] == 0


@pytest.mark.parametrize("storage_path", [".", "vector-generations/gen-safe", "./vector-generations/gen-safe"])
def test_generation_registration_accepts_safe_relative_storage_paths(storage_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_vector_generation_schema(conn)

    manifest = register_generation(
        conn,
        generation_id="gen-safe-" + str(abs(hash(storage_path))),
        identity=_identity(),
        storage_path=storage_path,
        status="ready",
    )

    assert manifest["storage_path"] == storage_path


@pytest.mark.parametrize("legacy_path", ["/" + "home/operator/private/legacy", "../legacy-escape"])
def test_generation_health_marks_legacy_invalid_storage_paths(legacy_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_vector_generation_schema(conn)
    register_generation(
        conn,
        generation_id="gen-legacy-invalid-path",
        identity=_identity(),
        storage_path="vector-generations/gen-legacy-invalid-path",
        status="active",
    )
    conn.execute(
        "INSERT INTO vector_generation_state(key, value, updated_at) VALUES (?, ?, ?)",
        ("current_generation", "gen-legacy-invalid-path", "2026-07-10T00:00:00+00:00"),
    )
    conn.execute(
        "UPDATE vector_generations SET storage_path = ? WHERE generation_id = ?",
        (legacy_path, "gen-legacy-invalid-path"),
    )

    rendered = json.dumps(generation_health_report(conn), ensure_ascii=False)
    assert legacy_path not in rendered
    assert "[INVALID_STORAGE_PATH]" in rendered


def test_setup_vector_layer_does_not_leak_orphan_generation_invalid_path(monkeypatch, tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE memories (
            id TEXT PRIMARY KEY, scope_id TEXT, source TEXT, target TEXT,
            content TEXT, summary TEXT, updated_at TEXT, metadata TEXT
        );
        CREATE TABLE memory_entities (memory_id TEXT, entity TEXT);
        CREATE TABLE memory_feedback (memory_id TEXT);
        """
    )
    ensure_vector_generation_schema(conn)
    identity = GenerationIdentity(
        backend="sqlite-bruteforce",
        provider="local-hash",
        model="hash-v1",
        dimensions=8,
        metric="cosine",
        prompt_profile="default-v1",
        table_name="memories",
    )
    generation_id = f"legacy-{identity.fingerprint[:16]}"
    register_generation(
        conn,
        generation_id=generation_id,
        identity=identity,
        storage_path=".",
        status="active",
    )
    raw_path = "/" + "home/operator/private/legacy-generation"
    conn.execute(
        "UPDATE vector_generations SET storage_path = ? WHERE generation_id = ?",
        (raw_path, generation_id),
    )
    conn.execute("DELETE FROM vector_generation_state")

    class Provider:
        name = "scope-recall"
        _storage_dir = tmp_path
        _vector_config = {
            "enabled": True,
            "backend": "sqlite-bruteforce",
            "table_name": "memories",
            "embedder": {"provider": "local-hash", "model": "hash-v1", "dimensions": 8},
        }
        _retrieval_config = {"metric": "cosine"}
        _lock = threading.RLock()
        _vector_lock = threading.RLock()
        _scope_id = "scope-a"
        _shared_scope_id = "shared-a"
        _shared_pool_scope_id = ""
        _accessible_scope_ids = ("scope-a", "shared-a")
        _vector_status = ""
        _vector_message = ""
        _vector_store = None
        _db_path = None
        _hermes_home = tmp_path
        _migration_info = {}

        def _require_conn(self):
            return conn

        @staticmethod
        def _vector_text(summary, content):
            return summary or content

        @staticmethod
        def _memory_isolated_for_scope():
            return False

    provider = attach_public_truth_ports(Provider())
    setup_vector_layer(provider)
    monkeypatch.setattr(memory_ops, "graph_relation_stats", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(memory_ops, "iter_curated_entries", lambda *_args, **_kwargs: [])

    prompt = MemoryProvider.system_prompt_block(provider)
    status = memory_ops.stats_payload(provider)
    rendered_status = json.dumps(status, ensure_ascii=False)

    assert provider._vector_status == "degraded"
    assert raw_path not in provider._vector_message
    assert raw_path not in prompt
    assert raw_path not in rendered_status
    assert "current_pointer" in provider._vector_message
    assert provider._vector_message not in prompt
    assert len(provider._vector_message) <= 300


def test_native_dependency_status_redacts_defensive_exception_path(monkeypatch):
    raw_path = "/" + "home/operator/private/native-probe"

    def fail_probe(*_args, **_kwargs):
        raise RuntimeError("probe failed at " + raw_path)

    monkeypatch.setattr(vector_store_module, "_NATIVE_VECTOR_PROBE", None)
    monkeypatch.setattr(vector_store_module.subprocess, "run", fail_probe)

    status = vector_store_module.native_vector_dependency_status()

    assert raw_path not in str(status.get("stderr") or "")
    assert "[REDACTED_PATH]" in str(status.get("stderr") or "")


def test_active_backend_failure_redacts_internal_stats_and_warning_surfaces(monkeypatch, tmp_path, caplog):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE memories (
            id TEXT PRIMARY KEY, scope_id TEXT, source TEXT, target TEXT,
            content TEXT, summary TEXT, updated_at TEXT, metadata TEXT
        );
        CREATE TABLE memory_entities (memory_id TEXT, entity TEXT);
        CREATE TABLE memory_feedback (memory_id TEXT);
        """
    )
    raw_path = "/" + "home/operator/private/native-fallback"

    class UnavailableLanceStore:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def is_available():
            return False

        @staticmethod
        def close():
            return None

    class Provider:
        name = "scope-recall"
        _storage_dir = tmp_path
        _vector_storage_dir = tmp_path
        _vector_config = {
            "enabled": True,
            "backend": "lancedb",
            "fallback_backend": "sqlite-bruteforce",
            "table_name": "memories",
        }
        _retrieval_config = {"metric": "cosine"}
        _lock = threading.RLock()
        _vector_lock = threading.RLock()
        _scope_id = "scope-a"
        _shared_scope_id = "shared-a"
        _shared_pool_scope_id = ""
        _accessible_scope_ids = ("scope-a", "shared-a")
        _vector_enabled = True
        _vector_ready = False
        _vector_status = "degraded"
        _vector_backend = "lancedb"
        _vector_message = ""
        _vector_store = None
        _vector_row_count = 0
        _vector_unique_id_count = 0
        _vector_duplicate_row_count = 0
        _embedder = None
        _db_path = None
        _hermes_home = tmp_path
        _migration_info = {}

        def _require_conn(self):
            return conn

        @staticmethod
        def _memory_isolated_for_scope():
            return False

    monkeypatch.setattr(vector_runtime_module, "LanceVectorStore", UnavailableLanceStore)
    monkeypatch.setattr(
        vector_runtime_module,
        "native_vector_dependency_status",
        lambda: {
            "safe": False,
            "returncode": None,
            "stdout": "",
            "stderr": "probe failed at " + raw_path,
        },
    )
    monkeypatch.setattr(memory_ops, "graph_relation_stats", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(memory_ops, "iter_curated_entries", lambda *_args, **_kwargs: [])
    caplog.set_level("WARNING", logger=vector_runtime_module.__name__)

    provider = attach_public_truth_ports(Provider())
    with pytest.raises(RuntimeError) as error:
        vector_runtime_module._open_vector_store(provider, dimensions=8)
    vector_runtime_module._mark_vector_startup_degraded(provider, error.value)
    stats = memory_ops.stats_payload(provider)
    prompt = MemoryProvider.system_prompt_block(provider)

    rendered_stats = json.dumps(stats, ensure_ascii=False)
    assert provider._vector_backend == "lancedb"
    assert provider._vector_store is None
    assert raw_path not in provider._vector_message
    assert raw_path not in rendered_stats
    assert raw_path not in caplog.text
    assert raw_path not in prompt
    assert "[REDACTED_PATH]" in provider._vector_message
    assert len(provider._vector_message) <= 300


def test_generation_schema_upgrade_adds_instruction_contract_columns():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE vector_generations (
            generation_id TEXT PRIMARY KEY,
            backend TEXT NOT NULL,
            storage_path TEXT NOT NULL,
            table_name TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            metric TEXT NOT NULL,
            prompt_profile TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            identity_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            activated_at TEXT NOT NULL DEFAULT '',
            row_count INTEGER NOT NULL DEFAULT 0,
            unique_id_count INTEGER NOT NULL DEFAULT 0,
            source_hash TEXT NOT NULL DEFAULT '',
            config_hash TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    ensure_vector_generation_schema(conn)
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(vector_generations)")}
    assert {"document_prefix", "query_prefix", "request_dimensions"} <= columns


def test_generation_activation_is_cas_and_rollback_keeps_old_generation(tmp_path):
    storage = tmp_path / "scope-recall"
    storage.mkdir()
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    old_identity = GenerationIdentity(
        backend="sqlite-bruteforce",
        provider="local-hash",
        model="hash-v1",
        dimensions=16,
        table_name="memories",
    )
    old_store = SQLiteBruteForceVectorStore(
        storage / "vector.sqlite3",
        table_name="memories",
        dimensions=16,
    )
    old_store.open()
    old_store.close()
    old = bootstrap_legacy_generation(conn, identity=old_identity, row_count=0)
    conn.commit()

    new_identity = GenerationIdentity(
        backend="sqlite-bruteforce",
        provider="local-hash",
        model="hash-v2",
        dimensions=16,
        table_name="memories",
    )
    built = build_vector_generation(
        storage,
        conn,
        generation_id="gen-embedding-2",
        identity=new_identity,
        embedder=LocalHashEmbedder(dimensions=16, model="hash-v2"),
        index_general=False,
        activate=True,
        expected_current=str(old["generation_id"]),
    )
    assert built["status"] == "activated"
    assert current_generation(conn)["generation_id"] == "gen-embedding-2"

    with pytest.raises(GenerationCompatibilityError, match="CAS conflict"):
        activate_generation(
            conn,
            old["generation_id"],
            expected_current="stale-browser-version",
            storage_dir=storage,
        )
    assert current_generation(conn)["generation_id"] == "gen-embedding-2"

    rolled_back = activate_generation(
        conn,
        old["generation_id"],
        expected_current="gen-embedding-2",
        storage_dir=storage,
    )
    assert rolled_back["generation_id"] == old["generation_id"]
    assert current_generation(conn)["generation_id"] == old["generation_id"]
    conn.close()


def test_generation_identity_requires_same_embedding_space():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_vector_generation_schema(conn)
    manifest = register_generation(
        conn,
        generation_id="gen-001",
        identity=_identity(),
        storage_path=".",
        status="ready",
    )
    validate_generation_compatibility(manifest, _identity())
    with pytest.raises(GenerationCompatibilityError, match="model"):
        validate_generation_compatibility(manifest, _identity(model="gemini-embedding-2"))
    with pytest.raises(GenerationCompatibilityError, match="dimensions"):
        validate_generation_compatibility(manifest, _identity(dimensions=256))
    for field, value in (
        ("document_prefix", "document: "),
        ("query_prefix", "query: "),
        ("request_dimensions", True),
    ):
        changed = _identity()
        changed_payload = changed.canonical()
        changed_payload[field] = value
        with pytest.raises(GenerationCompatibilityError, match=field):
            validate_generation_compatibility(manifest, GenerationIdentity(**changed_payload))


def test_generation_identity_preserves_prefix_trailing_whitespace_on_idempotent_refresh():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_vector_generation_schema(conn)
    payload = _identity(model="gemini-embedding-2").canonical()
    payload.update(
        {
            "prompt_profile": "retrieval-v1",
            "document_prefix": "Represent this document for retrieval: ",
            "query_prefix": "Represent this query for retrieval: ",
            "request_dimensions": True,
        }
    )
    identity = GenerationIdentity(**payload)

    manifest = register_generation(
        conn,
        generation_id="gen-prefix-whitespace",
        identity=identity,
        storage_path="vector-generations/gen-prefix-whitespace",
        status="building",
    )
    refreshed = register_generation(
        conn,
        generation_id="gen-prefix-whitespace",
        identity=identity,
        storage_path="vector-generations/gen-prefix-whitespace",
        status="ready",
    )

    assert manifest["document_prefix"].endswith(" ")
    assert manifest["query_prefix"].endswith(" ")
    assert refreshed["status"] == "ready"
    validate_generation_compatibility(refreshed, identity)


def test_generation_fingerprint_changes_with_actual_instruction_contract():
    base = _identity()
    for field, value in (
        ("document_prefix", "document: "),
        ("query_prefix", "query: "),
        ("request_dimensions", True),
    ):
        payload = base.canonical()
        payload[field] = value
        assert GenerationIdentity(**payload).fingerprint != base.fingerprint


def test_vector_outbox_is_idempotent_and_claims_once():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_vector_generation_schema(conn)
    first = enqueue_vector_event(
        conn,
        event_key="memory-1:upsert:v1",
        generation_id="gen-001",
        memory_id="memory-1",
        operation="upsert",
        payload={"updated_at": "v1"},
    )
    duplicate = enqueue_vector_event(
        conn,
        event_key="memory-1:upsert:v1",
        generation_id="gen-001",
        memory_id="memory-1",
        operation="upsert",
        payload={"updated_at": "v1"},
    )
    assert duplicate["id"] == first["id"]
    assert conn.execute("SELECT COUNT(*) FROM vector_outbox").fetchone()[0] == 1

    claimed = claim_vector_events(conn, generation_id="gen-001", worker_id="worker-a", limit=10)
    assert [row["id"] for row in claimed] == [first["id"]]
    assert claim_vector_events(conn, generation_id="gen-001", worker_id="worker-b", limit=10) == []

    complete_vector_event(conn, first["id"], worker_id="worker-a")
    assert conn.execute("SELECT status FROM vector_outbox WHERE id = ?", (first["id"],)).fetchone()[0] == "completed"


def test_vector_outbox_coalesces_opposite_unprocessed_intent() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_vector_generation_schema(conn)
    enqueue_vector_event(
        conn,
        event_key="memory-coalesce:upsert:v1",
        generation_id="gen-001",
        memory_id="memory-coalesce",
        operation="upsert",
    )
    enqueue_vector_event(
        conn,
        event_key="memory-coalesce:delete:v2",
        generation_id="gen-001",
        memory_id="memory-coalesce",
        operation="delete",
    )
    assert [
        tuple(row)
        for row in conn.execute(
            "SELECT operation, status FROM vector_outbox WHERE memory_id = ?",
            ("memory-coalesce",),
        ).fetchall()
    ] == [("delete", "pending")]

    enqueue_vector_event(
        conn,
        event_key="memory-coalesce:upsert:v3",
        generation_id="gen-001",
        memory_id="memory-coalesce",
        operation="upsert",
    )
    assert [
        tuple(row)
        for row in conn.execute(
            "SELECT operation, status FROM vector_outbox WHERE memory_id = ?",
            ("memory-coalesce",),
        ).fetchall()
    ] == [("upsert", "pending")]


def test_vector_outbox_reclaims_expired_worker_lease_with_cas():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_vector_generation_schema(conn)
    event = enqueue_vector_event(
        conn,
        event_key="lease-event",
        generation_id="gen-001",
        memory_id="memory-lease",
        operation="delete",
        timestamp="2026-07-10T00:00:00+00:00",
    )
    first = claim_vector_events(
        conn,
        generation_id="gen-001",
        worker_id="worker-a",
        lease_seconds=300,
        timestamp="2026-07-10T00:10:00+00:00",
    )
    assert [row["id"] for row in first] == [event["id"]]
    assert claim_vector_events(
        conn,
        generation_id="gen-001",
        worker_id="worker-b",
        lease_seconds=300,
        timestamp="2026-07-10T00:14:59+00:00",
    ) == []
    reclaimed = claim_vector_events(
        conn,
        generation_id="gen-001",
        worker_id="worker-b",
        lease_seconds=300,
        timestamp="2026-07-10T00:15:01+00:00",
    )
    assert [row["id"] for row in reclaimed] == [event["id"]]
    assert reclaimed[0]["attempts"] == 2
    with pytest.raises(GenerationCompatibilityError, match="completion CAS conflict"):
        complete_vector_event(conn, event["id"], worker_id="worker-a")
    complete_vector_event(conn, event["id"], worker_id="worker-b")


def test_vector_outbox_dead_letters_after_bounded_failures():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_vector_generation_schema(conn)
    event = enqueue_vector_event(
        conn,
        event_key="dead-letter-event",
        generation_id="gen-001",
        memory_id="memory-dead-letter",
        operation="upsert",
        timestamp="2026-07-10T00:00:00+00:00",
    )
    for attempt in range(2):
        worker = f"worker-{attempt}"
        at = f"2026-07-10T00:0{attempt + 1}:00+00:00"
        claimed = claim_vector_events(
            conn,
            generation_id="gen-001",
            worker_id=worker,
            timestamp=at,
        )
        assert [row["id"] for row in claimed] == [event["id"]]
        fail_vector_event(
            conn,
            event["id"],
            worker_id=worker,
            error="bounded failure",
            max_attempts=2,
            timestamp=at,
        )
    status = conn.execute(
        "SELECT status, attempts, completed_at FROM vector_outbox WHERE id = ?",
        (event["id"],),
    ).fetchone()
    assert tuple(status[:2]) == ("dead_letter", 2)
    assert status["completed_at"]
    assert claim_vector_events(
        conn,
        generation_id="gen-001",
        worker_id="worker-late",
        timestamp="2026-07-11T00:00:00+00:00",
    ) == []


def test_vector_generation_durable_error_helpers_redact_at_storage_boundary():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_vector_generation_schema(conn)
    secret = "sk-" + "durable-helper-secret"
    private_path = "/home/" + "operator/private.txt"
    raw_error = f"api_key={secret} failed while reading {private_path}"

    manifest = register_generation(
        conn,
        generation_id="gen-redaction",
        identity=_identity(),
        storage_path="vector-generations/gen-redaction",
        status="failed",
        error=raw_error,
    )
    start_migration_receipt(
        conn,
        receipt_id="receipt-redaction",
        generation_id="gen-redaction",
    )
    receipt = finish_migration_receipt(
        conn,
        "receipt-redaction",
        status="failed",
        error=raw_error,
    )
    event = enqueue_vector_event(
        conn,
        event_key="outbox-redaction",
        generation_id="gen-redaction",
        memory_id="memory-redaction",
        operation="delete",
    )
    claimed = claim_vector_events(
        conn,
        generation_id="gen-redaction",
        worker_id="worker-redaction",
    )
    assert [row["id"] for row in claimed] == [event["id"]]
    fail_vector_event(
        conn,
        event["id"],
        worker_id="worker-redaction",
        error=raw_error,
        max_attempts=1,
    )
    outbox_error = conn.execute(
        "SELECT last_error FROM vector_outbox WHERE id = ?",
        (event["id"],),
    ).fetchone()[0]

    for stored in (manifest["error"], receipt["error"], outbox_error):
        assert secret not in stored
        assert private_path not in stored
        assert "[REDACTED" in stored


def test_vector_generation_mapping_helpers_redact_nested_keys_and_values_at_storage_boundary():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_vector_generation_schema(conn)
    secret = "sk-" + ("A" * 24)
    private_path = "/home/" + "operator/private/vector.json"
    nested = {
        "api_key": secret,
        "child": [{"path": private_path, "token": secret}],
    }

    manifest = register_generation(
        conn,
        generation_id="gen-mapping-redaction",
        identity=_identity(),
        storage_path="vector-generations/gen-mapping-redaction",
        status="active",
        metadata=nested,
    )
    conn.execute(
        "INSERT INTO vector_generation_state(key, value, updated_at) VALUES (?, ?, ?)",
        ("current_generation", "gen-mapping-redaction", "2026-07-10T00:00:00+00:00"),
    )
    started = start_migration_receipt(
        conn,
        receipt_id="receipt-mapping-redaction",
        generation_id="gen-mapping-redaction",
        details=nested,
    )
    finished = finish_migration_receipt(
        conn,
        "receipt-mapping-redaction",
        status="ready",
        details=nested,
    )

    for stored in (manifest["metadata"], started["details"], finished["details"]):
        assert secret not in stored
        assert private_path not in stored
        assert "[REDACTED" in stored

    # Health output must remain safe even if an older caller inserted an
    # unsanitized manifest before this storage-boundary hardening existed.
    conn.execute(
        "UPDATE vector_generations SET metadata = ? WHERE generation_id = ?",
        (json.dumps(nested), "gen-mapping-redaction"),
    )
    health = generation_health_report(conn)
    rendered = json.dumps(health, ensure_ascii=False)
    assert secret not in rendered
    assert private_path not in rendered
    assert "[REDACTED" in rendered


def _insert_outbox_row(
    conn: sqlite3.Connection,
    *,
    event_key: str,
    generation_id: str,
    status: str,
    completed_at: str,
) -> None:
    timestamp = completed_at or "2026-01-01T00:00:00+00:00"
    conn.execute(
        """
        INSERT INTO vector_outbox(
            event_key, generation_id, memory_id, operation, payload, status,
            available_at, created_at, updated_at, completed_at
        ) VALUES (?, ?, ?, 'upsert', '{}', ?, ?, ?, ?, ?)
        """,
        (
            event_key,
            generation_id,
            f"memory-{event_key}",
            status,
            timestamp,
            timestamp,
            timestamp,
            completed_at,
        ),
    )


def test_completed_vector_outbox_pruning_is_bounded_terminal_only_and_caller_owned():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_vector_generation_schema(conn)
    for index in range(5):
        _insert_outbox_row(
            conn,
            event_key=f"gen-a-completed-{index}",
            generation_id="gen-a",
            status="completed",
            completed_at=f"2026-01-0{index + 1}T00:00:00+00:00",
        )
    for index in range(2):
        _insert_outbox_row(
            conn,
            event_key=f"gen-b-completed-{index}",
            generation_id="gen-b",
            status="completed",
            completed_at=f"2026-01-0{index + 1}T00:00:00+00:00",
        )
    _insert_outbox_row(
        conn,
        event_key="gen-a-retry",
        generation_id="gen-a",
        status="retry",
        completed_at="",
    )
    _insert_outbox_row(
        conn,
        event_key="gen-a-dead-letter",
        generation_id="gen-a",
        status="dead_letter",
        completed_at="2026-01-01T00:00:00+00:00",
    )
    conn.commit()

    receipt = prune_completed_vector_outbox(
        conn,
        retention_days=30,
        keep_per_generation=2,
        timestamp="2026-03-01T00:00:00+00:00",
    )

    assert receipt == {
        "enabled": True,
        "retention_days": 30,
        "keep_per_generation": 2,
        "generations_scanned": 2,
        "deleted": 3,
        "completed_remaining": 4,
    }
    assert conn.in_transaction is True
    assert conn.execute(
        "SELECT COUNT(*) FROM vector_outbox WHERE status = 'retry'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM vector_outbox WHERE status = 'dead_letter'"
    ).fetchone()[0] == 1

    conn.rollback()
    assert conn.execute(
        "SELECT COUNT(*) FROM vector_outbox WHERE status = 'completed'"
    ).fetchone()[0] == 7


def test_completed_vector_outbox_pruning_can_be_disabled_without_mutation():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_vector_generation_schema(conn)
    _insert_outbox_row(
        conn,
        event_key="disabled-old-completed",
        generation_id="gen-a",
        status="completed",
        completed_at="2020-01-01T00:00:00+00:00",
    )
    conn.commit()

    receipt = prune_completed_vector_outbox(
        conn,
        retention_days=0,
        keep_per_generation=0,
        timestamp="2026-03-01T00:00:00+00:00",
    )

    assert receipt["enabled"] is False
    assert receipt["deleted"] == 0
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM vector_outbox").fetchone()[0] == 1


def test_vector_outbox_retention_coalesces_transient_lock_without_in_pass_retry(monkeypatch):
    """Issue #47 contract: retention never fights a live writer inside a pass.

    The pre-fix behavior slept 50ms and retried in-pass, which cannot outwait
    a long digest transaction and produced one WARNING per idle tick.
    Retention now skips quietly and the rate-limited next pass covers it.
    """

    import scope_recall.vector_runtime as vector_runtime_module

    conn = sqlite3.connect(":memory:")
    provider = type(
        "Provider",
        (),
        {"_lock": __import__("threading").RLock()},
    )()
    calls = {"count": 0}

    def locked_prune(_conn, *, retention_days, keep_per_generation):
        calls["count"] += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        vector_runtime_module,
        "prune_completed_vector_outbox",
        locked_prune,
    )

    receipt = _prune_completed_outbox(
        provider,
        conn,
        retention_days=30,
        keep_per_generation=5000,
    )

    assert calls["count"] == 1, "lock contention must not trigger an in-pass retry"
    assert receipt["status"] == "skipped_contention"
    assert receipt["deleted"] == 0
    assert receipt["consecutive_skips"] == 1
    assert conn.in_transaction is False

    # A later uncontended pass succeeds and clears the skip streak.
    monkeypatch.setattr(
        vector_runtime_module,
        "prune_completed_vector_outbox",
        lambda _conn, *, retention_days, keep_per_generation: {
            "enabled": True,
            "retention_days": retention_days,
            "keep_per_generation": keep_per_generation,
            "generations_scanned": 1,
            "deleted": 2,
            "completed_remaining": 3,
        },
    )
    receipt = _prune_completed_outbox(
        provider,
        conn,
        retention_days=30,
        keep_per_generation=5000,
    )
    assert receipt["status"] == "pruned"
    assert receipt["deleted"] == 2
    assert provider._outbox_retention_contention_skips == 0
    assert conn.in_transaction is False
    conn.close()


def test_vector_outbox_payload_is_allowlisted_and_redacted_at_storage_boundary():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_vector_generation_schema(conn)
    secret = "sk-" + "outbox-payload-secret"
    private_path = "/home/" + "operator/outbox-private.txt"

    event = enqueue_vector_event(
        conn,
        event_key="outbox-payload-boundary",
        generation_id="gen-001",
        memory_id="memory-payload",
        operation="upsert",
        payload={
            "updated_at": "2026-07-11T00:00:00+00:00",
            "reason": f"api_key={secret} failed at {private_path}",
            "content": secret,
            "nested": {"path": private_path},
        },
    )
    stored = json.loads(str(event["payload"]))

    assert set(stored) == {"updated_at", "reason"}
    assert stored["updated_at"] == "2026-07-11T00:00:00+00:00"
    assert secret not in stored["reason"]
    assert private_path not in stored["reason"]
    assert "[REDACTED" in stored["reason"]


def test_doctor_fails_closed_on_vector_outbox_dead_letter(tmp_path):
    storage = tmp_path / "scope-recall"
    storage.mkdir()
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_vector_generation_schema(conn)
    identity = _identity()
    generation = bootstrap_legacy_generation(conn, identity=identity, row_count=0)
    event = enqueue_vector_event(
        conn,
        event_key="doctor-dead-letter",
        generation_id=str(generation["generation_id"]),
        memory_id="memory-doctor-dead",
        operation="delete",
        timestamp="2026-07-10T00:00:00+00:00",
    )
    claimed = claim_vector_events(
        conn,
        generation_id=str(generation["generation_id"]),
        worker_id="doctor-test-worker",
        timestamp="2026-07-10T00:01:00+00:00",
    )
    assert [row["id"] for row in claimed] == [event["id"]]
    fail_vector_event(
        conn,
        event["id"],
        worker_id="doctor-test-worker",
        error="permanent failure",
        max_attempts=1,
        timestamp="2026-07-10T00:01:00+00:00",
    )
    conn.commit()
    conn.close()

    payload, check, recommendations = vector_generation_report(
        tmp_path,
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
    assert payload["status"] == "outbox_dead_letter"
    assert payload["outbox_dead_letters"] == 1
    assert check["ok"] is False
    assert any("dead-letter" in failure for failure in check["failures"])
    assert any("requeue" in item for item in recommendations)


def test_replay_failure_does_not_claim_unattempted_events():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE memories (
            id TEXT PRIMARY KEY, scope_id TEXT, source TEXT, target TEXT,
            content TEXT, summary TEXT, updated_at TEXT, metadata TEXT
        )
        """
    )
    ensure_vector_generation_schema(conn)
    for index in range(2):
        enqueue_vector_event(
            conn,
            event_key=f"replay-failure-{index}",
            generation_id="gen-001",
            memory_id=f"memory-{index}",
            operation="delete",
            timestamp="2026-07-10T00:00:00+00:00",
        )
    conn.commit()

    class FailingStore:
        def delete_by_ids(self, _ids):
            raise RuntimeError("injected store outage")

        def audit_counts(self):
            return {"physical_rows": 0, "unique_ids": 0, "duplicate_rows": 0}

    provider = SimpleNamespace(
        _vector_generation_id="gen-001",
        _vector_store=FailingStore(),
        _embedder=object(),
        _lock=threading.RLock(),
        _vector_lock=threading.RLock(),
        _require_conn=lambda: conn,
    )
    result = replay_vector_outbox(provider, limit=2)

    assert result == {"claimed": 1, "completed": 0, "failed": 1}
    statuses = [
        str(row[0])
        for row in conn.execute("SELECT status FROM vector_outbox ORDER BY id")
    ]
    assert statuses == ["retry", "pending"]


def test_replay_outbox_deletes_candidate_instead_of_upserting_it():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE memories (
            id TEXT PRIMARY KEY, scope_id TEXT, source TEXT, target TEXT,
            content TEXT, summary TEXT, updated_at TEXT, metadata TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "candidate-1",
            "scope-a",
            "event-digest",
            "memory",
            "candidate content",
            "candidate summary",
            "2026-07-11T00:00:00+00:00",
            '{"lifecycle":"candidate"}',
        ),
    )
    ensure_vector_generation_schema(conn)
    enqueue_vector_event(
        conn,
        event_key="candidate-replay",
        generation_id="gen-001",
        memory_id="candidate-1",
        operation="upsert",
        timestamp="2026-07-11T00:00:00+00:00",
    )
    conn.commit()

    class Store:
        def __init__(self):
            self.records = {"candidate-1": {"id": "candidate-1"}}
            self.deleted = []

        def delete_by_ids(self, ids):
            self.deleted.extend(ids)
            for memory_id in ids:
                self.records.pop(memory_id, None)

        def upsert_records(self, rows):
            for row in rows:
                self.records[str(row["id"])] = dict(row)

        def audit_counts(self):
            return {"physical_rows": len(self.records), "unique_ids": len(self.records), "duplicate_rows": 0}

    class Embedder:
        @staticmethod
        def embed(_text):
            return [1.0, 0.0]

    store = Store()
    provider = SimpleNamespace(
        _vector_generation_id="gen-001",
        _vector_store=store,
        _embedder=Embedder(),
        _vector_config={"index_general": False},
        _lock=threading.RLock(),
        _vector_lock=threading.RLock(),
        _scope_id="scope-a",
        _vector_row_count=0,
        _vector_unique_id_count=0,
        _vector_duplicate_row_count=0,
        _vector_status="ready",
        _vector_message="",
        _require_conn=lambda: conn,
        _vector_text=lambda summary, content: summary or content,
    )

    assert replay_vector_outbox(provider) == {"claimed": 1, "completed": 1, "failed": 0}
    assert store.records == {}
    assert store.deleted == ["candidate-1"]


def test_fresh_setup_may_create_real_empty_generation_with_available_fallback(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("SCOPE_RECALL_TEST_MISSING_EMBED_KEY", raising=False)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    class Provider:
        _storage_dir = tmp_path
        _vector_config = {
            "enabled": True,
            "backend": "sqlite-bruteforce",
            "fallback_backend": "sqlite-bruteforce",
            "table_name": "memories",
            "embedder": {
                "provider": "openai-compatible",
                "model": "gemini-embedding-001",
                "dimensions": 3072,
                "api_key_env": ["SCOPE_RECALL_TEST_MISSING_EMBED_KEY"],
            },
            "fallback_embedder": {
                "provider": "local-hash",
                "model": "hash-v1",
                "dimensions": 16,
            },
        }
        _retrieval_config = {"metric": "cosine"}
        _lock = threading.RLock()
        _vector_lock = threading.RLock()
        _scope_id = "scope-a"
        _vector_status = ""
        _vector_message = ""
        _vector_backend = ""
        _embedder: Any = None
        _vector_store: Any = None

        def _require_conn(self):
            return conn

        @staticmethod
        def _vector_text(summary, content):
            return summary or content

    provider = Provider()
    setup_vector_layer(provider)

    manifest = current_generation(conn)
    assert provider._vector_status == "ready"
    assert provider._embedder.provider == "local-hash"
    assert provider._vector_store.count_rows() == 0
    assert manifest is not None
    assert manifest["backend"] == "sqlite-bruteforce"
    assert manifest["provider"] == "local-hash"
    assert manifest["model"] == "hash-v1"
    assert manifest["dimensions"] == 16
    provider._vector_store.close()
    conn.close()


def test_active_fallback_generation_remains_authoritative_when_primary_returns(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "available-test-key")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    store = SQLiteBruteForceVectorStore(
        tmp_path / "vector.sqlite3", table_name="memories", dimensions=16
    )
    store.open()
    store.close()
    bootstrap_legacy_generation(
        conn,
        identity=GenerationIdentity(
            backend="sqlite-bruteforce",
            provider="local-hash",
            model="hash-v1",
            dimensions=16,
            metric="cosine",
            table_name="memories",
        ),
        row_count=0,
    )

    class Provider:
        _storage_dir = tmp_path
        _vector_config = {
            "enabled": True,
            "backend": "lancedb",
            "fallback_backend": "sqlite-bruteforce",
            "table_name": "memories",
            "embedder": {
                "provider": "openai-compatible",
                "model": "gemini-embedding-001",
                "dimensions": 3072,
                "api_key_env": ["SCOPE_RECALL_TEST_EMBED_KEY"],
            },
            "fallback_embedder": {
                "provider": "local-hash",
                "model": "hash-v1",
                "dimensions": 16,
            },
        }
        _retrieval_config = {"metric": "cosine"}
        _lock = threading.RLock()
        _vector_lock = threading.RLock()
        _scope_id = "scope-a"
        _vector_status = ""
        _vector_message = ""
        _vector_backend = ""
        _embedder: Any = None
        _vector_store: Any = None

        def _require_conn(self):
            return conn

        @staticmethod
        def _vector_text(summary, content):
            return summary or content

    provider = Provider()
    setup_vector_layer(provider)

    assert provider._vector_status == "ready"
    assert provider._vector_backend == "sqlite-bruteforce"
    assert provider._embedder.provider == "local-hash"
    assert "fallback generation" in provider._vector_message
    provider._vector_store.close()
    conn.close()


def _runtime_identity_provider(
    conn: sqlite3.Connection,
    storage_dir,
    *,
    embedder_config: dict[str, Any],
    fallback_embedder_config: dict[str, Any] | None = None,
) -> SimpleNamespace:
    vector_config = {
        "enabled": True,
        "backend": "sqlite-bruteforce",
        "fallback_backend": "sqlite-bruteforce",
        "table_name": "memories",
        "embedder": dict(embedder_config),
        "fallback_embedder": dict(fallback_embedder_config or {}),
    }
    return SimpleNamespace(
        _storage_dir=storage_dir,
        _vector_config=vector_config,
        _retrieval_config={"metric": "cosine"},
        _config={"vector": vector_config, "retrieval": {"metric": "cosine"}},
        _lock=threading.RLock(),
        _vector_lock=threading.RLock(),
        _scope_id="scope-a",
        _vector_store=None,
        _require_conn=lambda: conn,
        _vector_text=lambda summary, content: summary or content,
    )


def _register_empty_runtime_generation(
    conn: sqlite3.Connection,
    storage_dir,
    *,
    model: str,
    dimensions: int,
) -> None:
    store = SQLiteBruteForceVectorStore(
        storage_dir / "vector.sqlite3",
        table_name="memories",
        dimensions=dimensions,
    )
    store.open()
    store.close()
    bootstrap_legacy_generation(
        conn,
        identity=GenerationIdentity(
            backend="sqlite-bruteforce",
            provider="audit",
            model=model,
            dimensions=dimensions,
            metric="cosine",
            table_name="memories",
        ),
        row_count=0,
        storage_path=".",
    )
    conn.commit()


def test_runtime_probes_readiness_before_matching_actual_embedding_dimensions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Runtime must match the manifest against dimensions learned during model load."""

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _register_empty_runtime_generation(
        conn,
        tmp_path,
        model="dynamic-model",
        dimensions=7,
    )

    class DynamicDimensionsEmbedder:
        provider = "audit"
        model = "dynamic-model"

        def __init__(self) -> None:
            self.dimensions = 3

        @staticmethod
        def is_available() -> bool:
            return True

        def probe_readiness(self) -> None:
            self.dimensions = 7

    monkeypatch.setattr(
        vector_runtime_module,
        "build_embedder",
        lambda _config: DynamicDimensionsEmbedder(),
    )
    provider = _runtime_identity_provider(
        conn,
        tmp_path,
        embedder_config={
            "provider": "audit",
            "model": "dynamic-model",
            "dimensions": 3,
        },
    )

    setup_vector_layer(provider)

    try:
        assert provider._vector_status == "ready"
        assert provider._embedder.dimensions == 7
    finally:
        if provider._vector_store is not None:
            provider._vector_store.close()
        conn.close()


def test_runtime_skips_readiness_for_provisionally_different_space_primary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """A provider/model mismatch must not load an unused primary model."""

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _register_empty_runtime_generation(
        conn,
        tmp_path,
        model="active-model",
        dimensions=7,
    )
    readiness_calls: list[str] = []

    class ModelEmbedder:
        provider = "audit"
        dimensions = 7

        def __init__(self, model: str) -> None:
            self.model = model

        @staticmethod
        def is_available() -> bool:
            return True

        def probe_readiness(self) -> None:
            readiness_calls.append(self.model)

    monkeypatch.setattr(
        vector_runtime_module,
        "build_embedder",
        lambda config: ModelEmbedder(str(config.get("model") or "")),
    )
    provider = _runtime_identity_provider(
        conn,
        tmp_path,
        embedder_config={
            "provider": "audit",
            "model": "unused-primary-model",
            "dimensions": 7,
        },
        fallback_embedder_config={
            "provider": "audit",
            "model": "active-model",
            "dimensions": 7,
        },
    )

    setup_vector_layer(provider)

    try:
        assert provider._vector_status == "ready"
        assert provider._embedder.model == "active-model"
        assert readiness_calls == ["active-model"]
    finally:
        if provider._vector_store is not None:
            provider._vector_store.close()
        conn.close()


def test_runtime_tries_same_identity_fallback_after_primary_readiness_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Device-specific primary failure must not hide a usable equivalent fallback."""

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _register_empty_runtime_generation(
        conn,
        tmp_path,
        model="shared-model",
        dimensions=7,
    )
    readiness_calls: list[str] = []

    class EquivalentEmbedder:
        provider = "audit"
        model = "shared-model"
        dimensions = 7

        def __init__(self, device: str) -> None:
            self.device = device

        @staticmethod
        def is_available() -> bool:
            return True

        def probe_readiness(self) -> None:
            readiness_calls.append(self.device)
            if self.device == "broken-device":
                raise RuntimeError("synthetic device initialization failure")

    monkeypatch.setattr(
        vector_runtime_module,
        "build_embedder",
        lambda config: EquivalentEmbedder(str(config.get("device") or "")),
    )
    provider = _runtime_identity_provider(
        conn,
        tmp_path,
        embedder_config={
            "provider": "audit",
            "model": "shared-model",
            "dimensions": 7,
            "device": "broken-device",
        },
        fallback_embedder_config={
            "provider": "audit",
            "model": "shared-model",
            "dimensions": 7,
            "device": "working-device",
        },
    )

    setup_vector_layer(provider)

    try:
        assert provider._vector_status == "ready"
        assert provider._embedder.device == "working-device"
        assert readiness_calls == ["broken-device", "working-device"]
    finally:
        if provider._vector_store is not None:
            provider._vector_store.close()
        conn.close()


def test_setup_never_uses_different_space_fallback_for_active_generation(monkeypatch, tmp_path):
    monkeypatch.delenv("SCOPE_RECALL_TEST_MISSING_EMBED_KEY", raising=False)
    vector_dir = tmp_path / "lancedb"
    store = LanceVectorStore(vector_dir, table_name="memories", dimensions=3072)
    store.open()
    store.upsert_records([_sentinel_row(3072)])
    store.close()

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_vector_generation_schema(conn)
    bootstrap_legacy_generation(
        conn,
        identity=GenerationIdentity(
            backend="lancedb",
            provider="openai-compatible",
            model="gemini-embedding-001",
            dimensions=3072,
            metric="cosine",
            prompt_profile="default-v1",
            table_name="memories",
        ),
        row_count=1,
    )

    class Provider:
        _storage_dir = tmp_path
        _vector_config = {
            "enabled": True,
            "backend": "lancedb",
            "table_name": "memories",
            "embedder": {
                "provider": "openai-compatible",
                "model": "gemini-embedding-001",
                "dimensions": 3072,
                "api_key_env": ["SCOPE_RECALL_TEST_MISSING_EMBED_KEY"],
            },
            "fallback_embedder": {"provider": "local-hash", "model": "hash-v1", "dimensions": 256},
        }
        _retrieval_config = {"metric": "cosine"}
        _lock = threading.RLock()
        _vector_lock = threading.RLock()
        _scope_id = "scope-a"
        _vector_status = ""
        _vector_message = ""
        _vector_store = None

        def _require_conn(self):
            return conn

        @staticmethod
        def _vector_text(summary, content):
            return summary or content

    provider = Provider()
    setup_vector_layer(provider)

    assert provider._vector_status == "degraded"
    assert "different embedding space" in provider._vector_message
    assert provider._vector_store is None
    reopened = LanceVectorStore(vector_dir, table_name="memories", dimensions=3072)
    reopened.open()
    try:
        assert reopened.count_rows() == 1
        assert reopened.list_ids() == ["sentinel-3072"]
    finally:
        reopened.close()
        conn.close()



def test_ready_generation_retirement_is_cas_protected_and_preserves_physical_storage(tmp_path):
    storage = tmp_path / "scope-recall"
    target = storage / "vector-generations" / "gen-stale"
    target.mkdir(parents=True)
    sentinel = target / "sentinel.bin"
    sentinel.write_bytes(b"retain-me")
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_vector_generation_schema(conn)
    identity = _identity()
    active = bootstrap_legacy_generation(conn, identity=identity, row_count=0)
    register_generation(
        conn,
        generation_id="gen-stale",
        identity=identity,
        storage_path="vector-generations/gen-stale",
        status="ready",
        metadata={"existing": "preserved"},
    )
    conn.commit()

    retired = retire_ready_generation(
        conn,
        "gen-stale",
        expected_current=str(active["generation_id"]),
        reason="source cohort is stale",
        timestamp="2026-07-18T00:00:00+00:00",
    )

    assert conn.in_transaction is True
    assert retired["status"] == "retired"
    assert current_generation(conn)["generation_id"] == active["generation_id"]
    metadata = json.loads(str(retired["metadata"]))
    assert metadata["existing"] == "preserved"
    assert metadata["retirement"] == {
        "at": "2026-07-18T00:00:00+00:00",
        "previous_status": "ready",
        "reason": "source cohort is stale",
    }
    assert sentinel.read_bytes() == b"retain-me"
    conn.rollback()
    assert generation_manifest(conn, "gen-stale")["status"] == "ready"
    conn.close()



def _retirement_fixture() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_vector_generation_schema(conn)
    identity = _identity()
    register_generation(
        conn,
        generation_id="gen-active",
        identity=identity,
        storage_path=".",
        status="active",
    )
    conn.execute(
        "INSERT INTO vector_generation_state(key, value, updated_at) VALUES (?, ?, ?)",
        ("current_generation", "gen-active", "2026-07-18T00:00:00+00:00"),
    )
    register_generation(
        conn,
        generation_id="gen-ready",
        identity=identity,
        storage_path="vector-generations/gen-ready",
        status="ready",
    )
    register_generation(
        conn,
        generation_id="gen-failed",
        identity=identity,
        storage_path="vector-generations/gen-failed",
        status="failed",
    )
    conn.commit()
    return conn



@pytest.mark.parametrize(
    ("target_id", "expected_current", "pattern"),
    [
        ("gen-active", "gen-active", "current generation"),
        ("gen-ready", "stale-browser-pointer", "CAS conflict"),
        ("gen-failed", "gen-active", "expected 'ready'"),
    ],
)
def test_generation_retirement_refuses_current_stale_cas_and_non_ready(
    target_id,
    expected_current,
    pattern,
):
    conn = _retirement_fixture()

    with pytest.raises(GenerationCompatibilityError, match=pattern):
        retire_ready_generation(
            conn,
            target_id,
            expected_current=expected_current,
            reason="operator test",
        )

    assert conn.in_transaction is False
    assert current_generation(conn)["generation_id"] == "gen-active"
    assert generation_manifest(conn, "gen-ready")["status"] == "ready"
    assert generation_manifest(conn, "gen-failed")["status"] == "failed"
    conn.close()
