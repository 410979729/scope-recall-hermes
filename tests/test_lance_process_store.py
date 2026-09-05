"""Real subprocess crash, timeout, and vector round-trip regressions."""

import importlib.util
import gc
import sqlite3
import subprocess
import sys
import time
import threading
from types import SimpleNamespace
import weakref

import pytest

import scope_recall.lance_process_store as process_store
from scope_recall.vector_store import build_vector_store, VectorStoreCompatibilityError


def _store(tmp_path):
    return process_store.ProcessLanceVectorStore(tmp_path / "lancedb", table_name="memories", dimensions=3)


@pytest.mark.parametrize("program", [
    "import sys,os; sys.stdin.buffer.readline(); os._exit(17)",
    "import sys; sys.stdin.buffer.readline(); print('invalid frame', flush=True)",
])
def test_native_worker_crash_does_not_exit_host_or_retry_mutation(tmp_path, monkeypatch, program):
    monkeypatch.setattr(process_store, "_worker_command", lambda: [sys.executable, "-c", program])
    store = _store(tmp_path)
    with pytest.raises(RuntimeError, match="SQLite truth is intact"):
        store.upsert_records([{"id": "pending"}])
    assert store._process.poll() is not None
    with pytest.raises(RuntimeError, match="closed"):
        store.count_rows()
    store.close()


def test_worker_deadline_includes_blocked_pipe_write(tmp_path, monkeypatch):
    monkeypatch.setattr(process_store, "_worker_command", lambda: [sys.executable, "-c", "import time; time.sleep(30)"])
    store = _store(tmp_path)
    store._request_timeout = 0.2
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="outbox work remains pending"):
        store.upsert_records([{"id": "pending", "content": "x" * 1000000}])
    assert time.monotonic() - started < 10
    assert store._process.poll() is not None
    assert not store._reader.is_alive()
    assert not store._sender.is_alive()


def test_unreferenced_worker_is_reaped_without_host_shutdown(tmp_path, monkeypatch):
    program = "import json,sys,time; r=json.loads(sys.stdin.buffer.readline()); print(json.dumps({'id':r['id'],'ok':True,'result':True}),flush=True); time.sleep(30)"
    monkeypatch.setattr(process_store, "_worker_command", lambda: [sys.executable, "-c", program])
    store = _store(tmp_path)
    assert store.is_available()
    worker = store._process
    reader = store._reader
    reference = weakref.ref(store)
    del store
    gc.collect()
    reader.join(timeout=3)
    assert reference() is None
    assert worker.poll() is not None
    assert not reader.is_alive()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows venv redirector regression")
def test_windows_venv_worker_pid_is_the_actual_interpreter(tmp_path, monkeypatch):
    # On uv/CPython Windows venvs, sys.executable names a launcher that would
    # otherwise create an unowned grandchild holding both anonymous pipes.
    program = "import json,sys,os,time; r=json.loads(sys.stdin.buffer.readline()); print(json.dumps({'id':r['id'],'ok':True,'result':{'pid':os.getpid(),'prefix':sys.prefix}}),flush=True); time.sleep(30)"
    monkeypatch.setattr(process_store, "_worker_command", lambda: [sys.executable, "-c", program])
    store = _store(tmp_path)
    try:
        identity = store.list_records()
        assert identity["pid"] == store._process.pid
        assert identity["prefix"] == sys.prefix
        started = time.monotonic()
        store.close()
        assert time.monotonic() - started < 5
        assert store._process.poll() is not None
        assert not store._reader.is_alive()
        assert not store._sender.is_alive()
    finally:
        store.close()


def _worker_script(setup):
    root = process_store.Path(process_store.__file__).resolve().parent
    return f"""
import sys, types, os
package = types.ModuleType('scope_recall')
package.__path__ = [{str(root)!r}]
sys.modules['scope_recall'] = package
import scope_recall.vector_store as native
from scope_recall._lance_worker import main
{setup}
main()
"""


def test_native_stdout_diagnostics_do_not_corrupt_protocol(tmp_path, monkeypatch):
    script = _worker_script("""
class NoisyStore:
    def __init__(self, *args, **kwargs):
        os.write(1, b'native initialization diagnostic\\n')
    def is_available(self):
        print('Python diagnostic', flush=True)
        os.write(1, b'native operation diagnostic\\n')
        return True
    def close(self):
        pass
native.LanceVectorStore = NoisyStore
""")
    monkeypatch.setattr(process_store, "_worker_command", lambda: [sys.executable, "-c", script])
    store = _store(tmp_path)
    try:
        assert store.is_available()
    finally:
        store.close()


@pytest.mark.parametrize("failure", ["unavailable", "missing", "compatibility"])
def test_runtime_failed_open_closes_private_worker(tmp_path, monkeypatch, failure):
    import scope_recall.vector_runtime as runtime

    closed = []

    def open_existing():
        if failure == "missing":
            raise FileNotFoundError("missing physical storage")
        raise VectorStoreCompatibilityError("wrong dimensions")

    store = SimpleNamespace(
        is_available=lambda: failure != "unavailable",
        open_existing_for_update=open_existing,
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(runtime, "build_vector_store", lambda *_args, **_kwargs: store)
    monkeypatch.setattr(runtime, "native_vector_dependency_status", lambda: {"safe": False})
    provider = SimpleNamespace(
        _storage_dir=tmp_path, _vector_storage_dir=tmp_path,
        _vector_config={}, _retrieval_config={}, _vector_backend="lancedb", _vector_store=None,
    )
    with pytest.raises((RuntimeError, FileNotFoundError)):
        runtime._open_vector_store(provider, dimensions=3)
    assert closed == [True]
    assert provider._vector_store is None


def test_windows_factory_does_not_import_native_libraries(tmp_path, monkeypatch):
    import scope_recall.vector_store as native
    monkeypatch.setattr(native.sys, "platform", "win32")
    monkeypatch.setattr(native, "_optional_lancedb", lambda: pytest.fail("native import in host"))
    store = build_vector_store("lancedb", storage_dir=tmp_path, table_name="memories", dimensions=3)
    assert isinstance(store, process_store.ProcessLanceVectorStore)
    assert store.backend == "lancedb" and store.dimensions == 3
    assert store._process is None
    store.close()


def test_non_windows_python_process_options_preserve_default_launcher(monkeypatch):
    import scope_recall.vector_store as native
    monkeypatch.setattr(native.sys, "platform", "linux")
    assert native._python_subprocess_options() == {}


def test_embedding_module_does_not_eagerly_import_torch():
    root = process_store.Path(process_store.__file__).resolve().parent
    script = f"""
import sys, types, builtins
package = types.ModuleType('scope_recall')
package.__path__ = [{str(root)!r}]
sys.modules['scope_recall'] = package
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name in ('sentence_transformers', 'torch'):
        raise AssertionError('unexpected native model import')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import scope_recall.embedders
assert 'torch' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(importlib.util.find_spec("lancedb") is None, reason="optional LanceDB runtime missing")
def test_real_native_worker_preserves_data_schema_scope_and_replay_idempotency(tmp_path):
    store = _store(tmp_path)
    assert store.is_available()
    row = {"id": "one", "scope_id": "scope-a", "source": "test", "target": "memory",
           "content": "durable fact", "summary": "fact", "updated_at": "2026-09-05",
           "vector": [1.0, 0.0, 0.0]}
    try:
        store.open()
        store.upsert_records([row])
        store.upsert_records([row])
        assert store.count_rows() == 1
        assert store.search([1.0, 0.0, 0.0], scope_id="scope-a", limit=5)[0]["id"] == "one"
        assert store.search([1.0, 0.0, 0.0], scope_id="foreign", limit=5) == []
        assert store.list_ids() == ["one"]
        assert store.list_records()["one"]["content"] == "durable fact"
        assert store.audit_counts()["physical_rows"] == 1
        with pytest.raises(VectorStoreCompatibilityError, match="shadow vector generation"):
            store.repair_records({})
        store.close()
        store.open_existing_for_update()
        assert store.count_rows() == 1
        store.delete_by_ids(["one"])
        assert store.count_rows() == 0
    finally:
        store.close()
    assert store._process.poll() is not None


@pytest.mark.skipif(importlib.util.find_spec("lancedb") is None, reason="optional LanceDB runtime missing")
def test_native_crash_after_physical_commit_leaves_truth_and_retryable_outbox(tmp_path, monkeypatch):
    from scope_recall.embedders import LocalHashEmbedder
    from scope_recall.sql_store import ensure_schema, store_row
    from scope_recall.vector_generation import GenerationIdentity, bootstrap_legacy_generation, enqueue_vector_event
    import scope_recall.vector_runtime as runtime

    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    store_row(
        conn, memory_id="pending", scope_id="scope-a", platform="test", user_id="user",
        chat_id="", thread_id="", gateway_session_key="", agent_identity="test", agent_workspace="test",
        session_id="test", source="test", target="memory", content="durable truth", metadata={},
        allow_duplicate=True, enqueue_vector_intent=False,
    )
    manifest = bootstrap_legacy_generation(
        conn, identity=GenerationIdentity(backend="lancedb", provider="local-hash", model="hash-v1", dimensions=16),
    )
    generation_id = str(manifest["generation_id"])
    enqueue_vector_event(conn, event_key="test", generation_id=generation_id, memory_id="pending", operation="upsert")
    conn.commit()

    original_command = process_store._worker_command
    script = _worker_script("""
original_upsert = native.LanceVectorStore.upsert_records
def crash_after_commit(self, rows):
    original_upsert(self, rows)
    os._exit(17)
native.LanceVectorStore.upsert_records = crash_after_commit
""")
    monkeypatch.setattr(process_store, "_worker_command", lambda: [sys.executable, "-c", script])
    store = process_store.ProcessLanceVectorStore(tmp_path / "lancedb", table_name="memories", dimensions=16)
    provider = SimpleNamespace(
        _storage_dir=tmp_path, _db_path=tmp_path / "memory.sqlite3",
        _vector_generation_id=generation_id, _vector_store=store,
        _embedder=LocalHashEmbedder(dimensions=16),
        _vector_config={"enabled": True, "backend": "lancedb", "embedder": {
            "provider": "local-hash", "model": "hash-v1", "dimensions": 16,
        }},
        _retrieval_config={"metric": "cosine"}, _vector_backend="lancedb",
        _vector_ready=True, _vector_status="ready", _vector_enabled=True,
        _scope_id="scope-a", _vector_text=lambda summary, content: content,
        _lock=threading.RLock(), _vector_lock=threading.RLock(), _require_conn=lambda: conn,
    )
    try:
        store.open()
        assert runtime.replay_vector_outbox(provider, refresh_audit_after=True) == {"claimed": 1, "completed": 0, "failed": 1}
        event = conn.execute("SELECT status, attempts FROM vector_outbox WHERE event_key = 'test'").fetchone()
        assert tuple(event) == ("retry", 1)
        assert conn.execute("SELECT content FROM memories WHERE id = 'pending'").fetchone()[0] == "durable truth"
        # EOF can precede the process wait signal on Windows; fail-closed
        # cleanup may terminate the already exiting worker with a new code.
        assert store._process.poll() is not None
        assert store.requires_reopen is True
        assert provider._vector_ready is False
        assert provider._vector_status == "needs_repair"
        assert provider._vector_usable_for_query is False
        assert provider._vector_reason_code == "native_worker_reopen_required"

        conn.execute("UPDATE vector_outbox SET available_at = '2000-01-01T00:00:00+00:00'")
        conn.commit()
        for _attempt in range(3):
            assert runtime.replay_vector_outbox(provider) == {"claimed": 0, "completed": 0, "failed": 0}
        assert tuple(conn.execute("SELECT status, attempts FROM vector_outbox WHERE event_key = 'test'").fetchone()) == ("retry", 1)

        # The online repair action delegates to setup_vector_layer. It opens
        # the same generation and replays the already committed idempotent row.
        monkeypatch.setattr(process_store, "_worker_command", original_command)
        monkeypatch.setattr(runtime, "build_vector_store", lambda _backend, **kwargs: process_store.ProcessLanceVectorStore(
            kwargs["storage_dir"] / "lancedb", table_name=kwargs["table_name"], dimensions=kwargs["dimensions"],
        ))
        runtime.setup_vector_layer(provider)
        assert provider._vector_ready is True, provider._vector_message
        assert provider._vector_status == "ready"
        assert provider._vector_store.requires_reopen is False
        assert provider._vector_store.count_rows() == 1
        assert conn.execute("SELECT status FROM vector_outbox WHERE event_key = 'test'").fetchone()[0] == "completed"
    finally:
        store.close()
        if provider._vector_store is not None:
            provider._vector_store.close()
        conn.close()
