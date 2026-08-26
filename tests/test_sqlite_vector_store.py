"""Tests for the SQLite brute-force vector companion implementation.

They keep dependency-free vector behavior aligned with the LanceDB companion contract."""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import scope_recall.sqlite_vector_store as sqlite_vector_store_module
import scope_recall.vector_migration as vector_migration_module
from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.sqlite_vector_store import SQLiteBruteForceVectorStore
from scope_recall.vector_generation import (
    GenerationIdentity,
    bootstrap_legacy_generation,
    current_generation,
)
from scope_recall.vector_migration import build_vector_generation
from scope_recall.vector_runtime import mark_vector_needs_repair, setup_vector_layer
from scope_recall.vector_store import VectorStoreCompatibilityError


class RuntimeProvider:
    def __init__(self, tmp_path):
        self._storage_dir = tmp_path / "scope-recall"
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        ensure_schema(self._conn)
        self._lock = threading.RLock()
        self._vector_config = {
            "enabled": True,
            "backend": "sqlite-bruteforce",
            "table_name": "memories",
            "index_general": False,
            "top_k": 4,
            "embedder": {"provider": "local-debug", "dimensions": 16, "model": "debug-hash-v1"},
        }
        self._retrieval_config = {"metric": "cosine", "vector_min_score": 0.0}
        self._vector_backend = ""
        self._vector_ready = False
        self._vector_status = "disabled"
        self._vector_message = ""
        self._vector_row_count = 0
        self._vector_unique_id_count = 0
        self._vector_duplicate_row_count = 0
        self._embedder = None
        self._vector_store = None
        self._scope_id = "scope-a"
        self._accessible_scope_ids = ["scope-a"]

    def _require_conn(self):
        return self._conn

    def _vector_text(self, summary, content):
        return f"{summary}\n{content}".strip()


class TwoDimensionalEmbedder:
    @staticmethod
    def embed_texts(texts):
        return [[1.0, 0.0] for _ in texts]


def _generation_identity():
    return GenerationIdentity(
        backend="sqlite-bruteforce",
        provider="local-hash",
        model="hash-v1",
        dimensions=2,
        table_name="memories",
    )


def _generation_connection(storage):
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    store_row(
        conn,
        memory_id="memory-1",
        scope_id="scope-a",
        platform="cli",
        user_id="joy",
        chat_id="",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="session",
        source="tool-store",
        target="memory",
        content="A READY generation must not have SQLite sidecars.",
    )
    conn.commit()
    return conn


@pytest.mark.skipif(
    not hasattr(os, "fchmod"),
    reason="Windows ACL inheritance has no POSIX descriptor-mode equivalent",
)
def test_mutable_sqlite_vector_store_enforces_owner_only_file_mode(tmp_path):
    db_path = tmp_path / "vector.sqlite3"
    previous_umask = os.umask(0)
    try:
        store = SQLiteBruteForceVectorStore(
            db_path,
            table_name="memories",
            dimensions=2,
            metric="cosine",
        )
        store.open()
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
        for sidecar_path in store._sidecar_paths():
            if sidecar_path.exists():
                assert stat.S_IMODE(sidecar_path.stat().st_mode) == 0o600
        store.close()
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600

    db_path.chmod(0o644)
    updater = SQLiteBruteForceVectorStore(
        db_path,
        table_name="memories",
        dimensions=2,
        metric="cosine",
    )
    updater.open_existing_for_update()
    updater.close()

    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_mutable_sqlite_vector_store_opens_without_posix_fchmod(monkeypatch, tmp_path):
    """The dependency-free fallback must remain usable on Windows CPython."""

    monkeypatch.delattr(sqlite_vector_store_module.os, "fchmod", raising=False)
    db_path = tmp_path / "vector.sqlite3"
    store = SQLiteBruteForceVectorStore(
        db_path,
        table_name="memories",
        dimensions=2,
        metric="cosine",
    )

    try:
        store.open()
        assert store.is_available() is True
        assert db_path.is_file()
    finally:
        store.close()


def test_sqlite_bruteforce_store_upsert_search_repair(tmp_path):
    store = SQLiteBruteForceVectorStore(tmp_path / "vector.sqlite3", table_name="memories", dimensions=2, metric="cosine")
    store.open()
    try:
        store.upsert_records(
            [
                {
                    "id": "a",
                    "scope_id": "scope-a",
                    "source": "tool-store",
                    "target": "memory",
                    "content": "alpha memory",
                    "summary": "alpha",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "vector": [1.0, 0.0],
                },
                {
                    "id": "b",
                    "scope_id": "scope-a",
                    "source": "tool-store",
                    "target": "memory",
                    "content": "beta memory",
                    "summary": "beta",
                    "updated_at": "2026-01-02T00:00:00+00:00",
                    "vector": [0.0, 1.0],
                },
            ]
        )

        assert store.count_rows() == 2
        assert store.audit_counts() == {"physical_rows": 2, "unique_ids": 2, "duplicate_rows": 0, "duplicate_ids": 0}
        assert store.search([1.0, 0.0], scope_id="scope-a", limit=1)[0]["id"] == "a"
        assert store.contains_id("a") is True
        assert store.contains_id("missing") is False

        repaired = store.repair_records({"a": {"updated_at": "2026-01-01T00:00:00+00:00"}})
        assert repaired == 1
        assert store.list_ids() == ["a"]
    finally:
        store.close()


def test_sqlite_bruteforce_equal_distance_prefers_newer_then_id(tmp_path):
    store = SQLiteBruteForceVectorStore(
        tmp_path / "vector.sqlite3",
        table_name="memories",
        dimensions=2,
        metric="cosine",
    )
    store.open()
    try:
        store.upsert_records(
            [
                {
                    "id": "older",
                    "scope_id": "scope-a",
                    "source": "tool-store",
                    "target": "memory",
                    "content": "older equal-distance memory",
                    "summary": "older",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "vector": [1.0, 0.0],
                },
                {
                    "id": "newer-b",
                    "scope_id": "scope-a",
                    "source": "tool-store",
                    "target": "memory",
                    "content": "newer equal-distance memory b",
                    "summary": "newer b",
                    "updated_at": "2026-02-01T00:00:00+00:00",
                    "vector": [1.0, 0.0],
                },
                {
                    "id": "newer-a",
                    "scope_id": "scope-a",
                    "source": "tool-store",
                    "target": "memory",
                    "content": "newer equal-distance memory a",
                    "summary": "newer a",
                    "updated_at": "2026-02-01T00:00:00+00:00",
                    "vector": [1.0, 0.0],
                },
            ]
        )

        rows = store.search([1.0, 0.0], scope_id="scope-a", limit=3)

        assert [row["id"] for row in rows] == ["newer-a", "newer-b", "older"]
    finally:
        store.close()


def test_sqlite_bruteforce_store_is_safe_for_background_threads(tmp_path):
    store = SQLiteBruteForceVectorStore(tmp_path / "vector.sqlite3", table_name="memories", dimensions=2, metric="cosine")
    store.open()
    try:
        store.upsert_records(
            [
                {
                    "id": "a",
                    "scope_id": "scope-a",
                    "source": "tool-store",
                    "target": "memory",
                    "content": "alpha memory",
                    "summary": "alpha",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "vector": [1.0, 0.0],
                }
            ]
        )

        def worker() -> str:
            rows = store.search([1.0, 0.0], scope_id="scope-a", limit=1)
            store.upsert_records(
                [
                    {
                        "id": "b",
                        "scope_id": "scope-a",
                        "source": "tool-store",
                        "target": "memory",
                        "content": "beta memory",
                        "summary": "beta",
                        "updated_at": "2026-01-02T00:00:00+00:00",
                        "vector": [0.0, 1.0],
                    }
                ]
            )
            return str(rows[0]["id"])

        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(worker).result() == "a"
        assert store.list_ids() == ["a", "b"]
    finally:
        store.close()


def test_open_existing_is_strictly_read_only_and_creates_no_sidecars(tmp_path):
    db_path = tmp_path / "vector.sqlite3"
    writer = SQLiteBruteForceVectorStore(
        db_path,
        table_name="memories",
        dimensions=2,
        metric="cosine",
    )
    writer.open()
    writer.upsert_records(
        [
            {
                "id": "a",
                "scope_id": "scope-a",
                "source": "tool-store",
                "target": "memory",
                "content": "immutable generation",
                "summary": "immutable",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "vector": [1.0, 0.0],
            }
        ]
    )
    writer.close()
    assert not db_path.with_name(f"{db_path.name}-wal").exists()
    assert not db_path.with_name(f"{db_path.name}-shm").exists()
    before = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in tmp_path.iterdir()
        if path.is_file()
    }

    reader = SQLiteBruteForceVectorStore(
        db_path,
        table_name="memories",
        dimensions=2,
        metric="cosine",
    )
    reader.open_existing()
    try:
        assert reader.list_ids() == ["a"]
        assert reader.audit_counts()["physical_rows"] == 1
    finally:
        reader.close()

    after = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in tmp_path.iterdir()
        if path.is_file()
    }
    assert after == before


def test_sqlite_bruteforce_seal_truncates_wal_before_immutable_open(tmp_path):
    db_path = tmp_path / "vector.sqlite3"
    writer = SQLiteBruteForceVectorStore(
        db_path,
        table_name="memories",
        dimensions=2,
        metric="cosine",
    )
    writer.open()
    assert writer._conn is not None
    writer._conn.execute("PRAGMA wal_autocheckpoint=0")
    writer.upsert_records(
        [
            {
                "id": "sealed",
                "scope_id": "scope-a",
                "source": "tool-store",
                "target": "memory",
                "content": "sealed immutable generation",
                "summary": "sealed",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "vector": [1.0, 0.0],
            }
        ]
    )

    try:
        checkpoint = writer.seal()
        assert checkpoint == {"busy": 0, "log": 0, "checkpointed": 0}
        assert not db_path.with_name(f"{db_path.name}-wal").exists()
        assert not db_path.with_name(f"{db_path.name}-shm").exists()

        reader = SQLiteBruteForceVectorStore(
            db_path,
            table_name="memories",
            dimensions=2,
            metric="cosine",
        )
        reader.open_existing()
        try:
            assert reader.list_ids() == ["sealed"]
        finally:
            reader.close()
    finally:
        writer.close()


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_open_existing_rejects_any_sqlite_sidecar(suffix, tmp_path):
    db_path = tmp_path / "private-vector-name.sqlite3"
    writer = SQLiteBruteForceVectorStore(
        db_path,
        table_name="memories",
        dimensions=2,
        metric="cosine",
    )
    writer.open()
    writer.close()
    sidecar = db_path.with_name(f"{db_path.name}{suffix}")
    sidecar.write_bytes(b"")

    reader = SQLiteBruteForceVectorStore(
        db_path,
        table_name="memories",
        dimensions=2,
        metric="cosine",
    )
    with pytest.raises(VectorStoreCompatibilityError, match="sidecar") as raised:
        reader.open_existing()

    assert db_path.name not in str(raised.value)
    assert suffix in str(raised.value)
    assert reader._conn is None
    assert sidecar.is_file()


def test_open_existing_rejects_uncheckpointed_wal_with_private_residue(tmp_path):
    db_path = tmp_path / "vector.sqlite3"
    initial = SQLiteBruteForceVectorStore(
        db_path,
        table_name="memories",
        dimensions=2,
        metric="cosine",
    )
    initial.open()
    initial.upsert_records(
        [
            {
                "id": "base",
                "scope_id": "scope-a",
                "source": "tool-store",
                "target": "memory",
                "content": "checkpointed base row",
                "summary": "base",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "vector": [1.0, 0.0],
            }
        ]
    )
    initial.close()

    private_sentinel = "PRIVATE-WAL-SENTINEL-UNBOUND"
    writer = SQLiteBruteForceVectorStore(
        db_path,
        table_name="memories",
        dimensions=2,
        metric="cosine",
    )
    writer.open()
    assert writer._conn is not None
    writer._conn.execute("PRAGMA wal_autocheckpoint=0")
    writer.upsert_records(
        [
            {
                "id": "wal-only",
                "scope_id": "scope-a",
                "source": "tool-store",
                "target": "memory",
                "content": private_sentinel,
                "summary": "private wal residue",
                "updated_at": "2026-01-02T00:00:00+00:00",
                "vector": [0.0, 1.0],
            }
        ]
    )
    wal_path = db_path.with_name(f"{db_path.name}-wal")
    shm_path = db_path.with_name(f"{db_path.name}-shm")
    assert wal_path.is_file()
    assert shm_path.is_file()
    assert private_sentinel.encode("utf-8") in wal_path.read_bytes()

    reader = SQLiteBruteForceVectorStore(
        db_path,
        table_name="memories",
        dimensions=2,
        metric="cosine",
    )
    try:
        with pytest.raises(VectorStoreCompatibilityError, match="sidecar"):
            reader.open_existing()
        assert reader._conn is None
    finally:
        reader.close()
        writer.close()


def test_open_existing_rejects_sidecar_created_during_connect(monkeypatch, tmp_path):
    db_path = tmp_path / "vector.sqlite3"
    writer = SQLiteBruteForceVectorStore(
        db_path,
        table_name="memories",
        dimensions=2,
        metric="cosine",
    )
    writer.open()
    writer.close()
    sidecar = db_path.with_name(f"{db_path.name}-wal")
    real_connect = sqlite_vector_store_module.sqlite3.connect

    def racing_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        sidecar.write_bytes(b"injected-during-connect")
        return conn

    monkeypatch.setattr(sqlite_vector_store_module.sqlite3, "connect", racing_connect)
    reader = SQLiteBruteForceVectorStore(
        db_path,
        table_name="memories",
        dimensions=2,
        metric="cosine",
    )

    with pytest.raises(VectorStoreCompatibilityError, match="sidecar"):
        reader.open_existing()

    assert reader._conn is None
    assert sidecar.is_file()


def test_sqlite_bruteforce_seal_rejects_checkpointed_but_pinned_sidecars(tmp_path):
    db_path = tmp_path / "vector.sqlite3"
    private_sentinel = "PRIVATE-PINNED-WAL-SENTINEL"
    writer = SQLiteBruteForceVectorStore(
        db_path,
        table_name="memories",
        dimensions=2,
        metric="cosine",
    )
    writer.open()
    assert writer._conn is not None
    writer._conn.execute("PRAGMA wal_autocheckpoint=0")
    writer.upsert_records(
        [
            {
                "id": "pinned",
                "scope_id": "scope-a",
                "source": "tool-store",
                "target": "memory",
                "content": private_sentinel,
                "summary": "pinned",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "vector": [1.0, 0.0],
            }
        ]
    )

    pinned_reader = sqlite3.connect(db_path)
    pinned_reader.execute("BEGIN")
    assert pinned_reader.execute("SELECT COUNT(*) FROM vector_records").fetchone()[0] == 1
    passive = tuple(int(value) for value in writer._conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
    assert passive[0] == 0
    assert passive[1] > 0
    assert passive[1] == passive[2]

    try:
        with pytest.raises(
            VectorStoreCompatibilityError,
            match=r"busy=\d+, log=\d+, checkpointed=\d+",
        ):
            writer.seal()
        assert writer._conn is None
        wal_path = db_path.with_name(f"{db_path.name}-wal")
        shm_path = db_path.with_name(f"{db_path.name}-shm")
        assert wal_path.is_file()
        assert shm_path.is_file()
        assert private_sentinel.encode("utf-8") in wal_path.read_bytes()

        immutable_reader = SQLiteBruteForceVectorStore(
            db_path,
            table_name="memories",
            dimensions=2,
            metric="cosine",
        )
        with pytest.raises(VectorStoreCompatibilityError, match="sidecar"):
            immutable_reader.open_existing()
        assert immutable_reader._conn is None
    finally:
        pinned_reader.rollback()
        pinned_reader.close()
        writer.close()


def test_sqlite_bruteforce_seal_preserves_checkpoint_error_over_close_error(tmp_path):
    class CheckpointAndCloseFailure:
        in_transaction = False

        @staticmethod
        def execute(statement):
            if statement == "PRAGMA busy_timeout=0":
                return None
            raise sqlite3.OperationalError("injected checkpoint failure")

        @staticmethod
        def close():
            raise OSError("injected close failure")

    store = SQLiteBruteForceVectorStore(
        tmp_path / "vector.sqlite3",
        table_name="memories",
        dimensions=2,
        metric="cosine",
    )
    store._conn = CheckpointAndCloseFailure()

    with pytest.raises(
        VectorStoreCompatibilityError,
        match="checkpoint execution failed",
    ) as raised:
        store.seal()

    assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
    assert store._conn is None


def test_sqlite_bruteforce_seal_reports_close_error_after_clean_checkpoint(tmp_path):
    class CheckpointResult:
        @staticmethod
        def fetchone():
            return (0, 0, 0)

    class CloseFailure:
        in_transaction = False

        @staticmethod
        def execute(statement):
            if statement == "PRAGMA wal_checkpoint(TRUNCATE)":
                return CheckpointResult()
            return None

        @staticmethod
        def close():
            raise OSError("injected close failure")

    store = SQLiteBruteForceVectorStore(
        tmp_path / "vector.sqlite3",
        table_name="memories",
        dimensions=2,
        metric="cosine",
    )
    store._conn = CloseFailure()

    with pytest.raises(
        VectorStoreCompatibilityError,
        match="could not close",
    ) as raised:
        store.seal()

    assert isinstance(raised.value.__cause__, OSError)
    assert store._conn is None


def test_generation_build_with_pinned_reader_fails_without_preflight_receipt(
    monkeypatch,
    tmp_path,
):
    storage = tmp_path / "scope-recall"
    storage.mkdir()
    conn = _generation_connection(storage)
    pinned_reader = None

    class PinnedSealStore(SQLiteBruteForceVectorStore):
        def seal(self):
            nonlocal pinned_reader
            assert self._conn is not None
            pinned_reader = sqlite3.connect(self.db_path)
            pinned_reader.execute("BEGIN")
            assert pinned_reader.execute("SELECT COUNT(*) FROM vector_records").fetchone()[0] == 1
            passive = self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            assert passive[0] == 0
            assert passive[1] == passive[2]
            return super().seal()

    def build_store(
        _backend,
        *,
        storage_dir,
        table_name,
        dimensions,
        metric="cosine",
        config=None,
    ):
        del config
        return PinnedSealStore(
            storage_dir / "vector.sqlite3",
            table_name=table_name,
            dimensions=dimensions,
            metric=metric,
        )

    monkeypatch.setattr(vector_migration_module, "build_vector_store", build_store)
    generation_id = "gen-pinned-reader"
    generation_root = storage / "vector-generations" / generation_id
    try:
        with pytest.raises(RuntimeError, match=r"busy=\d+, log=\d+, checkpointed=\d+"):
            build_vector_generation(
                storage,
                conn,
                generation_id=generation_id,
                identity=_generation_identity(),
                embedder=TwoDimensionalEmbedder(),
                index_general=False,
                expected_current="",
            )

        manifest = conn.execute(
            "SELECT status FROM vector_generations WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        receipt = conn.execute(
            "SELECT status FROM vector_migration_receipts WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        assert manifest is not None and manifest["status"] == "failed"
        assert receipt is not None and receipt["status"] == "failed"
        assert not (generation_root / ".generation-preflight.json").exists()
        assert (generation_root / "vector.sqlite3-wal").is_file()
        assert (generation_root / "vector.sqlite3-shm").is_file()
    finally:
        if pinned_reader is not None:
            pinned_reader.rollback()
            pinned_reader.close()
        conn.close()


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_generation_build_fails_closed_if_sidecar_appears_after_seal(
    monkeypatch,
    suffix,
    tmp_path,
):
    storage = tmp_path / "scope-recall"
    storage.mkdir()
    conn = _generation_connection(storage)

    class PostSealSidecarStore(SQLiteBruteForceVectorStore):
        def seal(self):
            checkpoint = super().seal()
            sidecar = self.db_path.with_name(f"{self.db_path.name}{suffix}")
            sidecar.write_bytes(b"injected-after-seal")
            return checkpoint

    def build_store(
        _backend,
        *,
        storage_dir,
        table_name,
        dimensions,
        metric="cosine",
        config=None,
    ):
        del config
        return PostSealSidecarStore(
            storage_dir / "vector.sqlite3",
            table_name=table_name,
            dimensions=dimensions,
            metric=metric,
        )

    monkeypatch.setattr(vector_migration_module, "build_vector_store", build_store)
    generation_id = f"gen-post-seal{suffix}"
    generation_root = storage / "vector-generations" / generation_id
    try:
        with pytest.raises(RuntimeError, match="sidecar"):
            build_vector_generation(
                storage,
                conn,
                generation_id=generation_id,
                identity=_generation_identity(),
                embedder=TwoDimensionalEmbedder(),
                index_general=False,
                expected_current="",
            )

        manifest = conn.execute(
            "SELECT status FROM vector_generations WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        receipt = conn.execute(
            "SELECT status FROM vector_migration_receipts WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        assert manifest is not None and manifest["status"] == "failed"
        assert receipt is not None and receipt["status"] == "failed"
        assert not (generation_root / ".generation-preflight.json").exists()
        assert (generation_root / f"vector.sqlite3{suffix}").is_file()
    finally:
        conn.close()


def test_generation_ready_publication_rechecks_for_sidecars(monkeypatch, tmp_path):
    storage = tmp_path / "scope-recall"
    storage.mkdir()
    conn = _generation_connection(storage)
    generation_id = "gen-ready-race"
    generation_root = storage / "vector-generations" / generation_id
    sidecar = generation_root / "vector.sqlite3-wal"
    real_register_generation = vector_migration_module.register_generation

    def racing_register_generation(*args, **kwargs):
        manifest = real_register_generation(*args, **kwargs)
        if kwargs.get("status") == "ready":
            sidecar.write_bytes(b"injected-during-ready-registration")
        return manifest

    monkeypatch.setattr(
        vector_migration_module,
        "register_generation",
        racing_register_generation,
    )
    try:
        with pytest.raises(RuntimeError, match="sidecar"):
            build_vector_generation(
                storage,
                conn,
                generation_id=generation_id,
                identity=_generation_identity(),
                embedder=TwoDimensionalEmbedder(),
                index_general=False,
                expected_current="",
            )

        manifest = conn.execute(
            "SELECT status FROM vector_generations WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        receipt = conn.execute(
            "SELECT status FROM vector_migration_receipts WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        assert manifest is not None and manifest["status"] == "failed"
        assert receipt is not None and receipt["status"] == "failed"
        assert not (generation_root / ".generation-preflight.json").exists()
        assert sidecar.is_file()
    finally:
        conn.close()


def test_generation_ready_has_no_sidecars_and_immutable_readback_is_complete(tmp_path):
    storage = tmp_path / "scope-recall"
    storage.mkdir()
    conn = _generation_connection(storage)
    generation_id = "gen-clean-ready"
    generation_root = storage / "vector-generations" / generation_id
    db_path = generation_root / "vector.sqlite3"
    try:
        result = build_vector_generation(
            storage,
            conn,
            generation_id=generation_id,
            identity=_generation_identity(),
            embedder=TwoDimensionalEmbedder(),
            index_general=False,
            expected_current="",
        )

        manifest = conn.execute(
            "SELECT status, row_count, unique_id_count FROM vector_generations WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        assert result["status"] == "ready"
        assert manifest is not None and tuple(manifest) == ("ready", 1, 1)
        assert (generation_root / ".generation-preflight.json").is_file()
        assert not db_path.with_name(f"{db_path.name}-wal").exists()
        assert not db_path.with_name(f"{db_path.name}-shm").exists()

        reader = SQLiteBruteForceVectorStore(
            db_path,
            table_name="memories",
            dimensions=2,
            metric="cosine",
        )
        reader.open_existing()
        try:
            assert reader.list_ids() == ["memory-1"]
            assert reader.audit_counts()["physical_rows"] == 1
        finally:
            reader.close()
    finally:
        conn.close()


def test_setup_vector_layer_fails_closed_for_manifestless_nonempty_truth(tmp_path):
    provider = RuntimeProvider(tmp_path)
    store_row(
        provider._conn,
        memory_id="memory-orphan-boundary",
        scope_id="scope-a",
        platform="cli",
        user_id="joy",
        chat_id="",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="session",
        source="tool-store",
        target="memory",
        content="Manifestless truth requires an explicit vector generation migration.",
    )

    setup_vector_layer(provider)

    try:
        assert provider._vector_ready is False
        assert provider._vector_status == "needs_repair"
        assert provider._vector_reason_code == "generation_incomplete"
        assert provider._vector_auto_recoverable is False
        assert provider._vector_repair_required is True
        assert "explicit_migration_required" in provider._vector_message
        assert provider._vector_store is None
        assert current_generation(provider._conn) is None
        assert not (provider._storage_dir / "vector.sqlite3").exists()
    finally:
        provider._conn.close()


def test_active_manifest_missing_physical_store_is_not_recreated(tmp_path):
    provider = RuntimeProvider(tmp_path)
    bootstrap_legacy_generation(
        provider._conn,
        identity=GenerationIdentity(
            backend="sqlite-bruteforce",
            provider="local-debug",
            model="debug-hash-v1",
            dimensions=16,
            metric="cosine",
            table_name="memories",
        ),
        row_count=0,
        storage_path=".",
    )
    provider._conn.commit()
    vector_path = provider._storage_dir / "vector.sqlite3"
    assert not vector_path.exists()

    setup_vector_layer(provider)

    try:
        assert provider._vector_ready is False
        assert provider._vector_status == "degraded"
        assert "physical storage is missing" in provider._vector_message
        assert provider._vector_store is None
        assert not vector_path.exists()
    finally:
        provider._conn.close()


def test_setup_vector_layer_can_use_sqlite_bruteforce_without_lancedb(tmp_path):
    provider = RuntimeProvider(tmp_path)
    setup_vector_layer(provider)
    assert provider._vector_ready is True
    assert provider._vector_row_count == 0
    store_row(
        provider._conn,
        memory_id="memory-1",
        scope_id="scope-a",
        platform="cli",
        user_id="joy",
        chat_id="",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="session",
        source="tool-store",
        target="memory",
        content="SQLite brute force vector backend supports non AVX CPUs.",
    )

    setup_vector_layer(provider)

    try:
        assert provider._vector_ready is True
        assert provider._vector_status == "ready"
        assert provider._vector_store is not None
        assert provider._embedder is not None
        store = provider._vector_store
        embedder = provider._embedder
        assert store.backend == "sqlite-bruteforce"
        assert provider._vector_row_count == 1
        rows = store.search(embedder.embed("non AVX vector backend"), scope_id="scope-a", limit=3)
        assert rows and rows[0]["id"] == "memory-1"
    finally:
        if provider._vector_store is not None:
            provider._vector_store.close()
        provider._conn.close()


def test_setup_vector_layer_recovers_from_needs_repair_with_complete_sqlite_meta(tmp_path):
    provider = RuntimeProvider(tmp_path)
    setup_vector_layer(provider)
    assert provider._vector_ready is True
    store_row(
        provider._conn,
        memory_id="memory-1",
        scope_id="scope-a",
        platform="cli",
        user_id="joy",
        chat_id="",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="session",
        source="tool-store",
        target="memory",
        content="Existing vector metadata should recover after needs_repair.",
    )

    setup_vector_layer(provider)
    old_store = provider._vector_store
    assert old_store is not None
    assert provider._vector_status == "ready"
    mark_vector_needs_repair(provider, "background thread failed")
    assert provider._vector_status == "needs_repair"
    assert provider._vector_ready is False

    setup_vector_layer(provider)

    try:
        assert old_store._conn is None
        assert provider._vector_ready is True
        assert provider._vector_status == "ready"
        assert provider._vector_store is not None
        assert provider._vector_store is not old_store
        assert provider._embedder is not None
        assert provider._vector_row_count == 1
        rows = provider._vector_store.search(provider._embedder.embed("vector metadata recover"), scope_id="scope-a", limit=3)
        assert rows and rows[0]["id"] == "memory-1"
    finally:
        if provider._vector_store is not None:
            provider._vector_store.close()
        provider._conn.close()
