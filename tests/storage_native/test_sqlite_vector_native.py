"""Native import and a reader/writer pair use local synthetic vectors only."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from scope_recall.vector.imports import import_page
from scope_recall.vector.store import build_vector_store


def test_lance_import_streams_existing_values_without_embedding(tmp_path):
    lancedb = pytest.importorskip("lancedb")
    pa = pytest.importorskip("pyarrow")
    rows = [
        dict(
            id=f"TEST:{i}",
            scope_id="TEST-scope",
            source=f"TEST:{i}",
            target="{}",
            content="",
            summary="",
            updated_at="",
            vector=[float(i + 1)] * 8,
        )
        for i in range(7)
    ]
    db = lancedb.connect(str(tmp_path / "lancedb"))
    schema = pa.schema(
        [*(pa.field(k, pa.string()) for k in rows[0] if k != "vector"), pa.field("vector", pa.list_(pa.float32(), 8))]
    )
    db.create_table("vectors", data=pa.Table.from_pylist(rows, schema=schema))
    store = build_vector_store("lancedb", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    store.open()
    try:
        for _ in range(15):
            result = import_page(store, visible=lambda row: True, max_rows=2)
            if result["outcome"] == "complete":
                break
        assert result["outcome"] == "complete"
        assert store.list_ids() == [row["id"] for row in rows]
        assert store.list_records()["TEST:3"]["vector"] == [4.0] * 8
        assert (tmp_path / "lancedb").is_dir()
    finally:
        store.close()


def test_installed_worker_can_publish_without_loading_extension(tmp_path):
    """The isolated child imports the installed wheel, not the checkout's package alias."""
    publisher = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    publisher.open()
    try:
        publisher.upsert_records(
            [
                dict(
                    id="TEST:old",
                    scope_id="TEST:scope",
                    source="TEST:old",
                    target="{}",
                    content="",
                    summary="",
                    updated_at="",
                    vector=[0.0, 1.0] + [0.0] * 6,
                )
            ]
        )
        assert publisher.maintain_index()["outcome"] == "indexed"
    finally:
        publisher.close()
    # Raw publications and mapped reads share the companion without native extensions.
    code = """import json,sys
from pathlib import Path
from scope_recall.vector.store import build_vector_store
sys.modules["sqlite_vec"]=None
store=build_vector_store('sqlite-vec',storage_dir=Path(sys.argv[1]),table_name='vectors',dimensions=8)
store.open()
store.upsert_records([dict(id='TEST:new',scope_id='TEST:scope',source='TEST:new',target='{}',content='',summary='',updated_at='',vector=[1.0]*8)])
print(json.dumps(store.diagnostics()))
store.close()
"""
    done = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(tmp_path)], capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["engine"] == "numpy-exact"
    reader = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    reader.open_existing()
    try:
        assert reader.search([1.0] * 8, scope_id="TEST:scope", limit=1)[0]["id"] == "TEST:new"
    finally:
        reader.close()


def test_a_hundred_scope_filter_keeps_every_partition(tmp_path):
    store = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    scopes = [f"TEST:scope-{i}" for i in range(100)]
    store.open()
    try:
        store.upsert_records(
            [
                dict(
                    id=scope,
                    scope_id=scope,
                    source=scope,
                    target="{}",
                    content="",
                    summary="",
                    updated_at="",
                    vector=[1.0] * 8,
                )
                for scope in scopes
            ]
        )
        assert {row["scope_id"] for row in store.search_scopes([1.0] * 8, scope_ids=scopes, limit=100)} == set(scopes)
        store.maintain_index()
        assert {row["scope_id"] for row in store.search_scopes([1.0] * 8, scope_ids=scopes, limit=100)} == set(scopes)
    finally:
        store.close()


def test_public_benchmark_subprocess_keeps_installed_application_package():
    """Importing benchmark helpers must preserve the isolated child's installed runtime."""
    helpers = Path(__file__).resolve().parents[1] / "public_bench"
    code = """import json,sys
from pathlib import Path
import scope_recall
before=Path(scope_recall.__file__).resolve()
assert 'site-packages' in before.parts, 'TEST installed runtime required'
sys.path.insert(0,sys.argv[1])
import lab
assert Path(scope_recall.__file__).resolve()==before
print(json.dumps({'installed':True}))
"""
    done = subprocess.run(
        [sys.executable, "-I", "-B", "-X", "utf8", "-c", code, str(helpers)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"installed": True}
