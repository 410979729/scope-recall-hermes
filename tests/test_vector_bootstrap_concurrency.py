"""Concurrent fresh vector bootstrap invariants.

The physical companion can be probed outside SQLite, but manifest registration
and current-pointer publication must leave exactly one active generation even
when independent provider connections race.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import scope_recall.vector_bootstrap as vector_bootstrap
from scope_recall.sql_store import ensure_schema
from scope_recall.vector_generation import GenerationIdentity, bootstrap_fresh_generation


class _FakeEmbedder:
    provider = "audit"

    def __init__(self, config: dict[str, Any]) -> None:
        self.model = str(config["model"])
        self.dimensions = int(config["dimensions"])

    @staticmethod
    def is_available() -> bool:
        return True


class _FakeStore:
    @staticmethod
    def is_available() -> bool:
        return True

    @staticmethod
    def open() -> None:
        return None

    @staticmethod
    def open_existing() -> None:
        raise FileNotFoundError

    @staticmethod
    def close() -> None:
        return None

    @staticmethod
    def audit_counts() -> dict[str, int]:
        return {"physical_rows": 0, "unique_ids": 0, "duplicate_rows": 0}


class _SavepointFailingConnection(sqlite3.Connection):
    """Inject one failure after the helper has acquired its writer transaction."""

    reject_bootstrap_savepoint = False

    def execute(
        self,
        sql: str,
        parameters: Any = (),
    ) -> sqlite3.Cursor:
        if self.reject_bootstrap_savepoint and sql.strip().casefold().startswith(
            "savepoint vector_generation_bootstrap"
        ):
            raise sqlite3.OperationalError("injected savepoint failure")
        return super().execute(sql, parameters)


def _config(model: str, dimensions: int) -> dict[str, Any]:
    return {
        "vector": {
            "enabled": True,
            "backend": "lancedb",
            "table_name": "memories",
            "embedder": {
                "provider": "audit",
                "model": model,
                "dimensions": dimensions,
            },
        },
        "retrieval": {"metric": "cosine"},
    }


def test_concurrent_fresh_bootstrap_serializes_physical_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Concurrent callers publish one manifest and create one companion."""

    db_path = tmp_path / "memory.sqlite3"
    setup = sqlite3.connect(db_path)
    setup.row_factory = sqlite3.Row
    ensure_schema(setup)
    setup.close()

    open_state = {"active": 0, "max_active": 0, "calls": 0}
    open_lock = threading.Lock()

    class _TrackingStore(_FakeStore):
        @staticmethod
        def open() -> None:
            with open_lock:
                open_state["active"] += 1
                open_state["calls"] += 1
                open_state["max_active"] = max(
                    open_state["max_active"],
                    open_state["active"],
                )
            time.sleep(0.05)
            with open_lock:
                open_state["active"] -= 1

    monkeypatch.setattr(
        vector_bootstrap,
        "build_embedder",
        lambda config: _FakeEmbedder(dict(config)),
    )
    monkeypatch.setattr(
        vector_bootstrap,
        "build_vector_store",
        lambda *_args, **_kwargs: _TrackingStore(),
    )
    monkeypatch.setattr(
        vector_bootstrap,
        "vector_companion_presence",
        lambda *_args, **_kwargs: False,
    )
    outcomes: list[dict[str, Any]] = []
    outcome_lock = threading.Lock()
    start = threading.Barrier(2)

    def worker(model: str, dimensions: int) -> None:
        conn = sqlite3.connect(db_path, timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            try:
                start.wait(timeout=10)
                receipt = vector_bootstrap.bootstrap_fresh_vector_companion(
                    tmp_path,
                    _config(model, dimensions),
                    truth_conn=conn,
                )
                outcome: dict[str, Any] = {"model": model, "receipt": receipt}
            except Exception as exc:  # noqa: BLE001 - the invariant covers every failure path
                outcome = {
                    "model": model,
                    "exception": f"{type(exc).__name__}: {exc}",
                }
            # Runtime setup catches bootstrap errors. A later ordinary provider
            # commit must not persist residue from the losing attempt.
            conn.commit()
            with outcome_lock:
                outcomes.append(outcome)
        finally:
            conn.close()

    threads = [
        threading.Thread(target=worker, args=("model-a", 8)),
        threading.Thread(target=worker, args=("model-b", 12)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert all(not thread.is_alive() for thread in threads)
    assert len(outcomes) == 2

    check = sqlite3.connect(db_path)
    check.row_factory = sqlite3.Row
    try:
        manifests = check.execute(
            "SELECT generation_id, status FROM vector_generations ORDER BY generation_id"
        ).fetchall()
        pointer = check.execute(
            "SELECT value FROM vector_generation_state WHERE key='current_generation'"
        ).fetchone()
    finally:
        check.close()

    assert len(manifests) == 1
    assert [row["status"] for row in manifests] == ["active"]
    assert pointer is not None
    assert pointer["value"] == manifests[0]["generation_id"]
    assert sum(
        str(outcome.get("receipt", {}).get("status") or "") == "ready"
        for outcome in outcomes
    ) == 1
    assert sum(
        str(outcome.get("receipt", {}).get("status") or "") == "existing"
        for outcome in outcomes
    ) == 1
    assert open_state == {"active": 0, "max_active": 1, "calls": 1}


def test_owned_bootstrap_transaction_rolls_back_when_savepoint_creation_fails(
    tmp_path: Path,
) -> None:
    """A failure between BEGIN IMMEDIATE and SAVEPOINT must release the lock."""

    conn = sqlite3.connect(
        tmp_path / "savepoint-failure.sqlite3",
        factory=_SavepointFailingConnection,
    )
    assert isinstance(conn, _SavepointFailingConnection)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    conn.commit()
    conn.reject_bootstrap_savepoint = True

    with pytest.raises(sqlite3.OperationalError, match="injected savepoint failure"):
        bootstrap_fresh_generation(
            conn,
            identity=GenerationIdentity(
                backend="lancedb",
                provider="audit",
                model="savepoint-test",
                dimensions=8,
            ),
            storage_path=str(tmp_path / "vectors"),
        )

    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM vector_generations").fetchone()[0] == 0
    conn.close()


def test_existing_companion_path_with_missing_table_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An existing local path is not owned by bootstrap merely because its table is absent."""

    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    companion = tmp_path / "lancedb"
    companion.mkdir(parents=True)
    marker = companion / "preexisting.txt"
    marker.write_text("owner evidence", encoding="utf-8")

    monkeypatch.setattr(
        vector_bootstrap,
        "build_embedder",
        lambda config: _FakeEmbedder(dict(config)),
    )
    monkeypatch.setattr(
        vector_bootstrap,
        "build_vector_store",
        lambda *_args, **_kwargs: _FakeStore(),
    )
    monkeypatch.setattr(
        vector_bootstrap,
        "vector_companion_presence",
        lambda _backend, _storage_dir: True,
    )

    result = vector_bootstrap.bootstrap_fresh_vector_companion(
        tmp_path,
        _config("primary-model", 8),
        truth_conn=conn,
    )

    assert result["status"] == "unavailable"
    assert "existing_companion_table_missing" in str(result["reason"])
    assert marker.read_text(encoding="utf-8") == "owner evidence"
    assert conn.execute("SELECT COUNT(*) FROM vector_generations").fetchone()[0] == 0
    conn.close()


def test_manifestless_companion_is_inventoried_without_embedder_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A named backend cannot hide on-disk state behind an empty embedder block."""

    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    monkeypatch.setattr(
        vector_bootstrap,
        "build_embedder",
        lambda config: _FakeEmbedder(dict(config)),
    )
    monkeypatch.setattr(
        vector_bootstrap,
        "build_vector_store",
        lambda *_args, **_kwargs: _FakeStore(),
    )
    monkeypatch.setattr(
        vector_bootstrap,
        "vector_companion_presence",
        lambda backend, _storage_dir: backend == "sqlite-bruteforce",
    )
    config = _config("primary-model", 8)
    config["vector"]["fallback_backend"] = "sqlite-bruteforce"
    config["vector"]["fallback_embedder"] = {}

    result = vector_bootstrap.bootstrap_fresh_vector_companion(
        tmp_path,
        config,
        truth_conn=conn,
    )

    assert result["status"] == "unavailable"
    assert str(result["reason"]).startswith("legacy_companion_uninspectable:")
    assert "fallback_embedder_not_configured" in str(result["reason"])
    assert conn.execute("SELECT COUNT(*) FROM vector_generations").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_sqlite_sidecar_without_main_is_uninspectable_and_preserved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    suffix: str,
) -> None:
    """A pre-existing SQLite sidecar prevents bootstrap from claiming path ownership."""

    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    conn.commit()
    main_path = tmp_path / "vector.sqlite3"
    sidecar_path = tmp_path / f"vector.sqlite3{suffix}"
    sidecar_path.write_text("preexisting sidecar evidence", encoding="utf-8")
    publish_called = False

    class _SQLiteFilesystemStore(_FakeStore):
        @staticmethod
        def open_existing() -> None:
            if not main_path.exists():
                raise FileNotFoundError

        @staticmethod
        def open() -> None:
            main_path.write_text("created by bootstrap", encoding="utf-8")

    monkeypatch.setattr(
        vector_bootstrap,
        "build_embedder",
        lambda config: _FakeEmbedder(dict(config)),
    )
    monkeypatch.setattr(
        vector_bootstrap,
        "build_vector_store",
        lambda *_args, **_kwargs: _SQLiteFilesystemStore(),
    )

    def fail_publish(*_args, **_kwargs):
        nonlocal publish_called
        publish_called = True
        raise sqlite3.OperationalError("synthetic manifest publish failure")

    monkeypatch.setattr(
        vector_bootstrap,
        "bootstrap_fresh_generation",
        fail_publish,
    )
    config = _config("sqlite-sidecar-model", 8)
    config["vector"]["backend"] = "sqlite-bruteforce"
    config["vector"]["fallback_backend"] = ""
    config["vector"]["fallback_embedder"] = {}

    error: Exception | None = None
    result: dict[str, Any] | None = None
    try:
        result = vector_bootstrap.bootstrap_fresh_vector_companion(
            tmp_path,
            config,
            truth_conn=conn,
        )
    except Exception as exc:  # RED: the old path reaches injected publication.
        error = exc

    assert error is None
    assert result is not None
    assert result["status"] == "unavailable"
    assert "existing_companion_table_missing" in str(result["reason"])
    assert publish_called is False
    assert main_path.exists() is False
    assert sidecar_path.read_text(encoding="utf-8") == "preexisting sidecar evidence"
    assert conn.execute("SELECT COUNT(*) FROM vector_generations").fetchone()[0] == 0
    conn.close()


def test_manifest_publish_failure_removes_new_empty_dynamic_dimension_companion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed SQLite publish must leave a dynamic-dimension bootstrap retryable."""

    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    conn.commit()
    companion = tmp_path / "lancedb"

    class _DynamicEmbedder(_FakeEmbedder):
        def probe_readiness(self) -> None:
            self.dimensions = 7

    class _FilesystemStore(_FakeStore):
        def __init__(self, dimensions: int) -> None:
            self.dimensions = int(dimensions)

        def open(self) -> None:
            companion.mkdir(parents=True, exist_ok=True)
            (companion / "dimensions.txt").write_text(
                str(self.dimensions), encoding="utf-8"
            )

        def open_existing(self) -> None:
            marker = companion / "dimensions.txt"
            if not marker.exists():
                raise FileNotFoundError
            if int(marker.read_text(encoding="utf-8")) != self.dimensions:
                raise RuntimeError("synthetic vector dimension mismatch")

    monkeypatch.setattr(
        vector_bootstrap,
        "build_embedder",
        lambda config: _DynamicEmbedder(dict(config)),
    )
    monkeypatch.setattr(
        vector_bootstrap,
        "build_vector_store",
        lambda _backend, **kwargs: _FilesystemStore(int(kwargs["dimensions"])),
    )
    monkeypatch.setattr(
        vector_bootstrap,
        "vector_companion_presence",
        lambda _backend, _storage_dir: companion.exists(),
    )
    original_bootstrap = vector_bootstrap.bootstrap_fresh_generation

    def fail_publish(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic manifest publish failure")

    monkeypatch.setattr(
        vector_bootstrap,
        "bootstrap_fresh_generation",
        fail_publish,
    )
    with pytest.raises(sqlite3.OperationalError, match="manifest publish failure"):
        vector_bootstrap.bootstrap_fresh_vector_companion(
            tmp_path,
            _config("dynamic-model", 3),
            truth_conn=conn,
        )

    assert companion.exists() is False
    assert conn.execute("SELECT COUNT(*) FROM vector_generations").fetchone()[0] == 0

    monkeypatch.setattr(
        vector_bootstrap,
        "bootstrap_fresh_generation",
        original_bootstrap,
    )
    retried = vector_bootstrap.bootstrap_fresh_vector_companion(
        tmp_path,
        _config("dynamic-model", 3),
        truth_conn=conn,
    )

    assert retried["status"] == "ready"
    assert retried["dimensions"] == 7
    conn.close()


def test_manifest_publish_failure_preserves_preexisting_empty_companion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Compensation owns only a path proven absent before this bootstrap attempt."""

    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    conn.commit()
    companion = tmp_path / "lancedb"
    companion.mkdir(parents=True)
    marker = companion / "owner.txt"
    marker.write_text("preexisting", encoding="utf-8")

    class _PreexistingStore(_FakeStore):
        def open_existing(self) -> None:
            if not marker.exists():
                raise FileNotFoundError

    monkeypatch.setattr(
        vector_bootstrap,
        "build_embedder",
        lambda config: _FakeEmbedder(dict(config)),
    )
    monkeypatch.setattr(
        vector_bootstrap,
        "build_vector_store",
        lambda *_args, **_kwargs: _PreexistingStore(),
    )
    monkeypatch.setattr(
        vector_bootstrap,
        "vector_companion_presence",
        lambda _backend, _storage_dir: True,
    )

    def fail_publish(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic manifest publish failure")

    monkeypatch.setattr(
        vector_bootstrap,
        "bootstrap_fresh_generation",
        fail_publish,
    )

    with pytest.raises(sqlite3.OperationalError, match="manifest publish failure"):
        vector_bootstrap.bootstrap_fresh_vector_companion(
            tmp_path,
            _config("preexisting-model", 8),
            truth_conn=conn,
        )

    assert marker.read_text(encoding="utf-8") == "preexisting"
    assert conn.execute("SELECT COUNT(*) FROM vector_generations").fetchone()[0] == 0
    conn.close()
