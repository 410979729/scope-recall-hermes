from __future__ import annotations

import importlib.abc
import subprocess
import sys
import textwrap
from pathlib import Path


def test_vector_runtime_imports_when_lancedb_and_pyarrow_are_unavailable():
    root = Path(__file__).resolve().parents[1]
    script = textwrap.dedent(
        f"""
        import importlib.abc
        import sys
        from pathlib import Path

        root = Path({str(root)!r})
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
        store = SQLiteBruteForceVectorStore(root / '.tmp-no-native-vector.sqlite3', dimensions=2)
        store.open()
        try:
            print(store.backend)
        finally:
            store.close()
            (root / '.tmp-no-native-vector.sqlite3').unlink(missing_ok=True)
            (root / '.tmp-no-native-vector.sqlite3-wal').unlink(missing_ok=True)
            (root / '.tmp-no-native-vector.sqlite3-shm').unlink(missing_ok=True)
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


def test_vector_runtime_falls_back_to_sqlite_backend_when_lancedb_probe_sigills(monkeypatch, tmp_path):
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

    _open_vector_store(provider, dimensions=2)

    try:
        assert provider._vector_store.backend == "sqlite-bruteforce"
        assert provider._vector_backend == "sqlite-bruteforce"
        assert "lancedb unavailable" in provider._vector_message.lower()
    finally:
        provider._vector_store.close()
