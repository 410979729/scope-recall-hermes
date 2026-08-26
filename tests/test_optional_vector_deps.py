"""Tests for behavior when optional vector dependencies are missing or disabled.

They ensure SQLite-only deployments remain functional."""

from __future__ import annotations

import importlib.abc
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


def test_doctor_vector_report_accepts_configured_sqlite_fallback_when_lancedb_unavailable(monkeypatch, tmp_path):
    from scope_recall.doctor_vector import vector_report
    from scope_recall.sql_store import ensure_schema, store_row
    from scope_recall.sqlite_vector_store import SQLiteBruteForceVectorStore

    hermes_home = tmp_path
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    conn = __import__("sqlite3").connect(storage / "memory.sqlite3")
    conn.row_factory = __import__("sqlite3").Row
    try:
        ensure_schema(conn)
        store_row(
            conn,
            memory_id="mem-1",
            scope_id="scope-a",
            platform="telegram",
            user_id="joy",
            chat_id="dm",
            thread_id="",
            gateway_session_key="",
            agent_identity="yuheng",
            agent_workspace="hermes",
            session_id="session",
            source="fixture",
            target="memory",
            content="fallback vector fixture",
            metadata={},
            allow_duplicate=True,
        )
    finally:
        conn.close()
    store = SQLiteBruteForceVectorStore(storage / "vector.sqlite3", dimensions=2)
    store.open()
    try:
        store.upsert_records(
            [
                {
                    "id": "mem-1",
                    "scope_id": "scope-a",
                    "source": "fixture",
                    "target": "memory",
                    "content": "fallback vector fixture",
                    "summary": "",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "vector": [1.0, 0.0],
                }
            ]
        )
    finally:
        store.close()

    def fail_lancedb(*_args, **_kwargs):
        return {"backend": "lancedb", "status": "needs_repair", "ready": False, "error": "No module named 'lancedb'"}, {"ok": False, "failures": ["LanceDB error: No module named 'lancedb'"]}, ["repair lancedb"]

    monkeypatch.setattr("scope_recall.doctor_vector.lancedb_vector_report", fail_lancedb)

    payload, check, recommendations = vector_report(
        hermes_home,
        expected_embedder={"dimensions": 2},
        backend="lancedb",
        fallback_backend="sqlite-bruteforce",
    )

    assert check == {"ok": True, "failures": []}
    assert payload["state"] == "ready"
    assert payload["status"] == "ready"
    assert payload["reason_code"] == "fallback_ready"
    assert payload["diagnostic_status"] == "fallback_ready"
    assert payload["primary"]["ready"] is False
    assert payload["fallback"]["backend"] == "sqlite-bruteforce"
    assert payload["fallback"]["ready"] is True
    assert any("sqlite-bruteforce fallback" in item for item in recommendations)


def test_doctor_allows_uninitialized_fallback_when_truth_has_no_indexable_rows(
    monkeypatch, tmp_path
):
    import sqlite3

    import scope_recall.doctor_vector as doctor_vector
    from scope_recall.sql_store import ensure_schema

    storage = tmp_path / "scope-recall"
    (storage / "lancedb").mkdir(parents=True)
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    monkeypatch.setattr(
        doctor_vector,
        "lancedb_vector_report",
        lambda *_args, **_kwargs: (
            {
                "backend": "lancedb",
                "status": "needs_repair",
                "ready": False,
                "error": "native probe unsafe",
            },
            {"ok": False, "failures": ["native probe unsafe"]},
            ["repair lancedb"],
        ),
    )

    payload, check, recommendations = doctor_vector._backend_vector_report(
        tmp_path,
        expected_embedder={"dimensions": 2},
        backend="lancedb",
        fallback_backend="sqlite-bruteforce",
    )

    assert check == {"ok": True, "failures": []}
    assert payload["status"] == "fallback_not_initialized"
    assert payload["ready"] is False
    assert payload["fallback"]["status"] == "missing"
    assert any("no indexable truth rows" in item for item in recommendations)


def test_doctor_uses_native_probe_before_importing_lancedb(monkeypatch, tmp_path):
    import builtins
    import scope_recall.doctor_vector as doctor_vector

    storage = tmp_path / "scope-recall"
    (storage / "lancedb").mkdir(parents=True)
    monkeypatch.setattr(
        doctor_vector,
        "native_vector_dependency_status",
        lambda: {"safe": False, "returncode": 132, "stderr": "Illegal instruction"},
    )
    original_import = builtins.__import__
    attempts = []

    def guarded_import(name, *args, **kwargs):
        if name == "lancedb" or name.startswith("lancedb."):
            attempts.append(name)
            raise AssertionError("unsafe in-process lancedb import")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    payload, check, _ = doctor_vector.lancedb_vector_report(tmp_path, expected_embedder={"dimensions": 2})

    assert attempts == []
    assert check["ok"] is False
    assert payload["status"] == "needs_repair"
    assert payload["native_dependency"]["returncode"] == 132


def test_vector_runtime_imports_when_lancedb_and_pyarrow_are_unavailable(tmp_path):
    root = Path(__file__).resolve().parents[1]
    temporary_db = tmp_path / "no-native-vector.sqlite3"
    script = textwrap.dedent(
        f"""
        import importlib.abc
        import sys
        from pathlib import Path

        root = Path({str(root)!r})
        temporary_db = Path({str(temporary_db)!r})
        sys.path.insert(0, str(root.parent))
        sys.path.insert(0, str(root))

        class BlockNativeVectorDeps(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'lancedb' or fullname.startswith('lancedb.') or fullname == 'pyarrow' or fullname.startswith('pyarrow.'):
                    raise ImportError(f'blocked {{fullname}}')
                return None

        sys.meta_path.insert(0, BlockNativeVectorDeps())

        import scope_recall.vector_runtime  # noqa: F401
        from scope_recall.sqlite_vector_store import SQLiteBruteForceVectorStore
        store = SQLiteBruteForceVectorStore(temporary_db, dimensions=2)
        store.open()
        try:
            print(store.backend)
        finally:
            store.close()
            temporary_db.unlink(missing_ok=True)
            Path(str(temporary_db) + '-wal').unlink(missing_ok=True)
            Path(str(temporary_db) + '-shm').unlink(missing_ok=True)
        """
    )
    result = subprocess.run([sys.executable, "-c", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "sqlite-bruteforce"


def test_lancedb_availability_probe_does_not_import_unsafe_native_modules_in_process(monkeypatch):
    import scope_recall.vector_store as vector_store

    class Result:
        returncode = 132
        stderr = "Illegal instruction"
        stdout = ""

    class DetectInProcessImport(importlib.abc.MetaPathFinder):
        attempts = 0

        def find_spec(self, fullname, path=None, target=None):
            if fullname == "lancedb" or fullname.startswith("lancedb.") or fullname == "pyarrow" or fullname.startswith("pyarrow."):
                self.attempts += 1
                raise AssertionError(f"unsafe in-process import attempted for {fullname}")
            return None

    detector = DetectInProcessImport()
    monkeypatch.setattr(vector_store, "_NATIVE_VECTOR_PROBE", None, raising=False)
    monkeypatch.setattr(vector_store, "subprocess", type("SubprocessStub", (), {"run": staticmethod(lambda *args, **kwargs: Result())}), raising=False)
    sys.meta_path.insert(0, detector)
    try:
        store = vector_store.LanceVectorStore(Path("/tmp/scope-recall-test-lancedb"), table_name="memories", dimensions=2)
        assert store.is_available() is False
        assert detector.attempts == 0
        assert vector_store.native_vector_dependency_status()["safe"] is False
        assert vector_store.native_vector_dependency_status()["returncode"] == 132
    finally:
        sys.meta_path.remove(detector)


def test_active_vector_generation_does_not_fallback_when_lancedb_probe_sigills(monkeypatch, tmp_path):
    import scope_recall.vector_store as vector_store
    from scope_recall.vector_runtime import _open_vector_store

    class Result:
        returncode = 132
        stderr = "Illegal instruction"
        stdout = ""

    class Provider:
        _storage_dir = tmp_path
        _vector_config = {"backend": "lancedb", "fallback_backend": "sqlite-bruteforce", "table_name": "memories"}
        _retrieval_config = {"metric": "cosine"}
        _vector_backend = "lancedb"
        _vector_store = None
        _vector_message = ""

    monkeypatch.setattr(vector_store, "_NATIVE_VECTOR_PROBE", None, raising=False)
    monkeypatch.setattr(vector_store, "subprocess", type("SubprocessStub", (), {"run": staticmethod(lambda *args, **kwargs: Result())}), raising=False)
    provider = Provider()

    with pytest.raises(RuntimeError, match="returncode=132"):
        _open_vector_store(provider, dimensions=2)

    assert provider._vector_store is None
    assert provider._vector_backend == "lancedb"
    assert not (tmp_path / "vector.sqlite3").exists()


def test_default_config_does_not_override_an_active_lancedb_generation_when_probe_sigills(monkeypatch, tmp_path):
    import scope_recall.vector_store as vector_store
    from scope_recall.config import DEFAULT_CONFIG
    from scope_recall.vector_runtime import _open_vector_store

    class Result:
        returncode = 132
        stderr = "Illegal instruction"
        stdout = ""

    class Provider:
        _storage_dir = tmp_path
        _vector_config = dict(DEFAULT_CONFIG["vector"])
        _retrieval_config = {"metric": "cosine"}
        _vector_backend = "lancedb"
        _vector_store = None
        _vector_message = ""

    monkeypatch.setattr(vector_store, "_NATIVE_VECTOR_PROBE", None, raising=False)
    monkeypatch.setattr(vector_store, "subprocess", type("SubprocessStub", (), {"run": staticmethod(lambda *args, **kwargs: Result())}), raising=False)
    provider = Provider()

    with pytest.raises(RuntimeError, match="returncode=132"):
        _open_vector_store(provider, dimensions=2)

    assert DEFAULT_CONFIG["vector"]["fallback_backend"] == "sqlite-bruteforce"
    assert provider._vector_store is None
    assert provider._vector_backend == "lancedb"
    assert not (tmp_path / "vector.sqlite3").exists()
