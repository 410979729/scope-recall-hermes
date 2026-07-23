"""Fresh vector bootstrap selection contract tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import scope_recall.vector_bootstrap as vector_bootstrap
from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.vector_generation import current_generation


class _FakeEmbedder:
    def __init__(self, config: dict[str, object]) -> None:
        self.provider = str(config.get("provider") or "fake")
        self.model = str(config.get("model") or "fake-model")
        raw_dimensions = config.get("dimensions")
        self.dimensions = int(str(raw_dimensions or 8))
        self._available = bool(config.get("available", True))
        self._raise_probe = bool(config.get("raise_probe", False))

    def is_available(self) -> bool:
        if self._raise_probe:
            raise RuntimeError("embedder probe failed")
        return self._available


class _FakeStore:
    def __init__(
        self,
        backend: str,
        available: bool,
        counts: dict[str, int] | None = None,
    ) -> None:
        self.backend = backend
        self._available = available
        self._counts = counts or {
            "physical_rows": 0,
            "unique_ids": 0,
            "duplicate_rows": 0,
        }

    def is_available(self) -> bool:
        return self._available

    @staticmethod
    def open() -> None:
        return None

    @staticmethod
    def open_existing() -> None:
        return None

    @staticmethod
    def close() -> None:
        return None

    def audit_counts(self) -> dict[str, int]:
        return dict(self._counts)


def _truth() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


@pytest.mark.parametrize(
    (
        "primary_backend_available",
        "primary_embedder_available",
        "fallback_configured",
        "expected_selection",
        "expected_backend",
        "expected_provider",
    ),
    [
        (True, True, True, "primary", "lancedb", "openai-compatible"),
        (False, True, True, "fallback", "sqlite-bruteforce", "local-hash"),
        (True, False, True, "fallback", "sqlite-bruteforce", "local-hash"),
        (False, True, False, "none", "", ""),
        (True, False, False, "none", "", ""),
    ],
)
def test_fresh_bootstrap_selects_primary_first_and_fails_closed_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    primary_backend_available: bool,
    primary_embedder_available: bool,
    fallback_configured: bool,
    expected_selection: str,
    expected_backend: str,
    expected_provider: str,
) -> None:
    backend_availability = {
        "lancedb": primary_backend_available,
        "sqlite-bruteforce": True,
    }
    monkeypatch.setattr(
        vector_bootstrap,
        "build_embedder",
        lambda config: _FakeEmbedder(dict(config)),
    )
    monkeypatch.setattr(
        vector_bootstrap,
        "build_vector_store",
        lambda backend, **_kwargs: _FakeStore(
            str(backend), backend_availability[str(backend)]
        ),
    )

    fallback_backend = "sqlite-bruteforce" if fallback_configured else ""
    fallback_embedder: dict[str, object] = (
        {
            "provider": "local-hash",
            "model": "hash-v1",
            "dimensions": 8,
            "available": True,
        }
        if fallback_configured
        else {}
    )
    config = {
        "vector": {
            "enabled": True,
            "backend": "lancedb",
            "fallback_backend": fallback_backend,
            "table_name": "memories",
            "embedder": {
                "provider": "openai-compatible",
                "model": "gemini-embedding-001",
                "dimensions": 3072,
                "available": primary_embedder_available,
            },
            "fallback_embedder": fallback_embedder,
        },
        "retrieval": {"metric": "cosine"},
    }
    conn = _truth()
    try:
        receipt = vector_bootstrap.bootstrap_fresh_vector_companion(
            tmp_path,
            config,
            truth_conn=conn,
        )
        manifest = current_generation(conn)
    finally:
        conn.close()

    assert receipt["selection"] == expected_selection
    if expected_selection == "none":
        assert receipt["status"] == "unavailable"
        assert manifest is None
        return

    assert receipt["status"] == "ready"
    assert receipt["backend"] == expected_backend
    assert receipt["provider"] == expected_provider
    assert manifest is not None
    assert manifest["backend"] == expected_backend
    assert manifest["provider"] == expected_provider
    metadata = json.loads(str(manifest["metadata"] or "{}"))
    assert metadata["selection"] == expected_selection
    assert metadata["provenance"] == "fresh-setup-bootstrap"


def test_missing_manifest_rejects_nonempty_companion_when_truth_is_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Orphan companion rows must never become the current truth generation."""

    counts = {
        "lancedb": {
            "physical_rows": 0,
            "unique_ids": 0,
            "duplicate_rows": 0,
        },
        "sqlite-bruteforce": {
            "physical_rows": 17,
            "unique_ids": 17,
            "duplicate_rows": 0,
        },
    }
    monkeypatch.setattr(
        vector_bootstrap,
        "build_embedder",
        lambda config: _FakeEmbedder(dict(config)),
    )
    monkeypatch.setattr(
        vector_bootstrap,
        "build_vector_store",
        lambda backend, **_kwargs: _FakeStore(
            str(backend), True, counts[str(backend)]
        ),
    )
    config = {
        "vector": {
            "enabled": True,
            "backend": "lancedb",
            "fallback_backend": "sqlite-bruteforce",
            "table_name": "memories",
            "embedder": {
                "provider": "openai-compatible",
                "model": "primary-embedding",
                "dimensions": 12,
            },
            "fallback_embedder": {
                "provider": "local-hash",
                "model": "legacy-fallback-embedding",
                "dimensions": 8,
            },
        },
        "retrieval": {"metric": "cosine"},
    }
    conn = _truth()
    try:
        receipt = vector_bootstrap.bootstrap_fresh_vector_companion(
            tmp_path,
            config,
            truth_conn=conn,
        )
        manifest = current_generation(conn)
    finally:
        conn.close()

    assert receipt["status"] == "unavailable"
    assert receipt["selection"] == "none"
    assert str(receipt["reason"]).startswith(
        "manifest_missing_nonempty_legacy_companion"
    )
    assert manifest is None


def test_missing_manifest_rejects_nonempty_companion_without_embedding_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Matching row counts cannot prove which embedding space produced vectors."""

    counts = {
        "lancedb": {
            "physical_rows": 0,
            "unique_ids": 0,
            "duplicate_rows": 0,
        },
        "sqlite-bruteforce": {
            "physical_rows": 1,
            "unique_ids": 1,
            "duplicate_rows": 0,
        },
    }
    monkeypatch.setattr(
        vector_bootstrap,
        "build_embedder",
        lambda config: _FakeEmbedder(dict(config)),
    )
    monkeypatch.setattr(
        vector_bootstrap,
        "build_vector_store",
        lambda backend, **_kwargs: _FakeStore(
            str(backend), True, counts[str(backend)]
        ),
    )
    conn = _truth()
    _store_truth_fixture(conn)
    try:
        receipt = vector_bootstrap.bootstrap_fresh_vector_companion(
            tmp_path,
            _legacy_matrix_config(),
            truth_conn=conn,
        )
        manifest = current_generation(conn)
    finally:
        conn.close()

    assert receipt["status"] == "unavailable"
    assert receipt["selection"] == "none"
    assert str(receipt["reason"]).startswith(
        "manifest_missing_nonempty_legacy_companion"
    )
    assert manifest is None


def _legacy_matrix_config(
    *, primary_available: bool = True, primary_raise_probe: bool = False
) -> dict[str, object]:
    return {
        "vector": {
            "enabled": True,
            "backend": "lancedb",
            "fallback_backend": "sqlite-bruteforce",
            "table_name": "memories",
            "embedder": {
                "provider": "openai-compatible",
                "model": "primary-embedding",
                "dimensions": 12,
                "available": primary_available,
                "raise_probe": primary_raise_probe,
            },
            "fallback_embedder": {
                "provider": "local-hash",
                "model": "legacy-fallback-embedding",
                "dimensions": 8,
                "available": True,
            },
        },
        "retrieval": {"metric": "cosine"},
    }


def _store_truth_fixture(conn: sqlite3.Connection) -> None:
    store_row(
        conn,
        memory_id="truth-1",
        scope_id="scope-a",
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="legacy-matrix",
        source="tool-store",
        target="project",
        content="Existing truth row without a generation manifest.",
        metadata='{"memory_type":"factual"}',
        allow_duplicate=True,
    )
    conn.commit()


@pytest.mark.parametrize(
    ("primary_rows", "fallback_rows", "primary_available", "truth_rows", "reason_prefix"),
    [
        (7, 9, True, 0, "manifest_missing_nonempty_legacy_companion"),
        (7, 0, False, 0, "manifest_missing_nonempty_legacy_companion"),
        (0, 0, True, 1, "manifest_missing_truth_nonempty_companions_empty"),
    ],
)
def test_missing_manifest_fails_closed_for_ambiguous_or_nonfresh_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    primary_rows: int,
    fallback_rows: int,
    primary_available: bool,
    truth_rows: int,
    reason_prefix: str,
) -> None:
    counts = {
        "lancedb": {
            "physical_rows": primary_rows,
            "unique_ids": primary_rows,
            "duplicate_rows": 0,
        },
        "sqlite-bruteforce": {
            "physical_rows": fallback_rows,
            "unique_ids": fallback_rows,
            "duplicate_rows": 0,
        },
    }
    monkeypatch.setattr(
        vector_bootstrap,
        "build_embedder",
        lambda config: _FakeEmbedder(dict(config)),
    )
    monkeypatch.setattr(
        vector_bootstrap,
        "build_vector_store",
        lambda backend, **_kwargs: _FakeStore(
            str(backend), True, counts[str(backend)]
        ),
    )
    conn = _truth()
    if truth_rows:
        _store_truth_fixture(conn)
    try:
        receipt = vector_bootstrap.bootstrap_fresh_vector_companion(
            tmp_path,
            _legacy_matrix_config(primary_available=primary_available),
            truth_conn=conn,
        )
        manifest = current_generation(conn)
    finally:
        conn.close()

    assert receipt["status"] == "unavailable"
    assert receipt["selection"] == "none"
    assert str(receipt["reason"]).startswith(reason_prefix)
    assert manifest is None


def test_missing_manifest_rejects_nonempty_primary_without_identity_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    counts = {
        "lancedb": {
            "physical_rows": 11,
            "unique_ids": 11,
            "duplicate_rows": 0,
        },
        "sqlite-bruteforce": {
            "physical_rows": 0,
            "unique_ids": 0,
            "duplicate_rows": 0,
        },
    }
    monkeypatch.setattr(
        vector_bootstrap,
        "build_embedder",
        lambda config: _FakeEmbedder(dict(config)),
    )
    monkeypatch.setattr(
        vector_bootstrap,
        "build_vector_store",
        lambda backend, **_kwargs: _FakeStore(
            str(backend), True, counts[str(backend)]
        ),
    )
    conn = _truth()
    try:
        receipt = vector_bootstrap.bootstrap_fresh_vector_companion(
            tmp_path,
            _legacy_matrix_config(),
            truth_conn=conn,
        )
        manifest = current_generation(conn)
    finally:
        conn.close()

    assert receipt["status"] == "unavailable"
    assert receipt["selection"] == "none"
    assert str(receipt["reason"]).startswith(
        "manifest_missing_nonempty_legacy_companion"
    )
    assert manifest is None


def test_primary_embedder_probe_exception_uses_fallback_when_store_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    counts = {
        backend: {"physical_rows": 0, "unique_ids": 0, "duplicate_rows": 0}
        for backend in ("lancedb", "sqlite-bruteforce")
    }
    monkeypatch.setattr(
        vector_bootstrap,
        "build_embedder",
        lambda config: _FakeEmbedder(dict(config)),
    )
    monkeypatch.setattr(
        vector_bootstrap,
        "build_vector_store",
        lambda backend, **_kwargs: _FakeStore(
            str(backend), True, counts[str(backend)]
        ),
    )
    conn = _truth()
    try:
        receipt = vector_bootstrap.bootstrap_fresh_vector_companion(
            tmp_path,
            _legacy_matrix_config(primary_raise_probe=True),
            truth_conn=conn,
        )
    finally:
        conn.close()

    assert receipt["status"] == "ready"
    assert receipt["selection"] == "fallback"
    assert receipt["backend"] == "sqlite-bruteforce"
