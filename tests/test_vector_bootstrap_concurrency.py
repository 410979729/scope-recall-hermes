"""Concurrent fresh vector bootstrap invariants.

The physical companion can be probed outside SQLite, but manifest registration
and current-pointer publication must leave exactly one active generation even
when independent provider connections race.
"""

from __future__ import annotations

import sqlite3
import threading
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


def test_concurrent_fresh_bootstrap_rolls_back_losing_active_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A losing pointer CAS must not become durable after a later commit."""

    db_path = tmp_path / "memory.sqlite3"
    setup = sqlite3.connect(db_path)
    setup.row_factory = sqlite3.Row
    ensure_schema(setup)
    setup.close()

    original_bootstrap = vector_bootstrap.bootstrap_fresh_generation
    barrier = threading.Barrier(2)

    def synchronized_bootstrap(*args: Any, **kwargs: Any) -> dict[str, Any]:
        barrier.wait(timeout=10)
        return original_bootstrap(*args, **kwargs)

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
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        vector_bootstrap,
        "bootstrap_fresh_generation",
        synchronized_bootstrap,
    )

    outcomes: list[dict[str, Any]] = []
    outcome_lock = threading.Lock()

    def worker(model: str, dimensions: int) -> None:
        conn = sqlite3.connect(db_path, timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            try:
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
