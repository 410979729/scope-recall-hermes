from __future__ import annotations

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
