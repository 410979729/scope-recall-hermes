"""Broad provider integration tests for lifecycle, scope, tools, recall, vector, journal, and memory operations.

This file protects the Hermes MemoryProvider contract from end-to-end regressions."""

import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from plugins.memory import load_memory_provider
from tools.memory_tool import MemoryStore, memory_tool

from scope_recall.journal import append_journal_entry
from scope_recall.models import RuntimeScope
from scope_recall.scope import build_scope_id


def _write_scope_recall_config(hermes_home, values):
    config_path = hermes_home / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(values, ensure_ascii=False) + "\n", encoding="utf-8")


def _production_experience_promotion_module(plugin):
    """Resolve the promote_experiences module BackgroundWork actually imports."""
    package = plugin.__class__.__module__.rsplit(".", 1)[0]
    return importlib.import_module(f"{package}.experience_promotion")


@pytest.fixture
def provider(tmp_path):
    _write_scope_recall_config(
        tmp_path,
        {
            "vector": {
                "embedder": {"provider": "local-hash", "dimensions": 256, "model": "hash-v1"},
                "fallback_embedder": {"provider": "local-hash", "dimensions": 256, "model": "hash-v1"},
            }
        },
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None, "scope-recall plugin should load from $HERMES_HOME/plugins"
    plugin.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    yield plugin
    plugin.shutdown()


def _assert_sqlite_writer_released(provider):
    with provider._lock:
        assert provider._require_conn().in_transaction is False
    external = sqlite3.connect(provider._db_path, timeout=0.2)
    try:
        external.execute("BEGIN IMMEDIATE")
        external.rollback()
    finally:
        external.close()


def _open_transaction_and_raise(provider, marker: str) -> None:
    conn = provider._require_conn()
    conn.execute("CREATE TABLE IF NOT EXISTS rollback_probe(marker TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO rollback_probe(marker) VALUES (?)", (marker,))
    assert conn.in_transaction
    raise RuntimeError(f"synthetic {marker} failure")


def test_scope_recall_plugin_loads_from_hermes_home_plugins():
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    assert plugin.name == "scope-recall"


def test_scope_recall_plugin_cold_loads_through_hermes_user_loader():
    """Recover from half-initialized modules left by Hermes' eager loader."""

    script = r'''
import importlib.util
import os
from pathlib import Path
import sys
import types

parent = types.ModuleType("_hermes_user_memory")
parent.__path__ = []
sys.modules[parent.__name__] = parent
package_name = "_hermes_user_memory.scope-recall"
plugin_dir = Path(os.environ["HERMES_HOME"]) / "plugins" / "scope-recall"
spec = importlib.util.spec_from_file_location(
    package_name,
    plugin_dir / "__init__.py",
    submodule_search_locations=[str(plugin_dir)],
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[package_name] = module
for submodule in (
    "provider",
    "recall",
    "reflection",
    "reflection_grounding",
    "reflection_llm",
    "reflection_tooling",
    "tooling",
):
    full_name = f"{package_name}.{submodule}"
    sys.modules[full_name] = types.ModuleType(full_name)
spec.loader.exec_module(module)

class Collector:
    provider = None

    def register_memory_provider(self, provider):
        self.provider = provider

collector = Collector()
module.register(collector)
assert collector.provider is not None, "cold loader returned no provider"
assert collector.provider.name == "scope-recall"
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_save_config_bootstraps_empty_sqlite_schema(tmp_path):
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    db_path = tmp_path / "scope-recall" / "memory.sqlite3"
    assert not db_path.exists()

    plugin.save_config({"vector": {"enabled": False}}, str(tmp_path))

    assert db_path.is_file()
    conn = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()
    assert {"memories", "journal_entries", "journal_digest_runs", "memory_journal_sources"} <= tables


def test_save_config_bootstrap_uses_sqlite_busy_timeout(tmp_path, monkeypatch):
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    import scope_recall.truth_connection as truth_connection_module

    real_connect = truth_connection_module.sqlite3.connect
    connect_calls: list[dict[str, object]] = []

    def capturing_connect(database, *args, **kwargs):
        database_path = str(database).split("?", 1)[0].removeprefix("file:")
        connect_calls.append({"database": Path(database_path).name, "timeout": kwargs.get("timeout")})
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(truth_connection_module.sqlite3, "connect", capturing_connect)

    plugin.save_config({"vector": {"enabled": False}}, str(tmp_path))

    memory_connects = [call for call in connect_calls if call["database"] == "memory.sqlite3"]
    assert memory_connects
    assert all(call["timeout"] == 10.0 for call in memory_connects)


def test_initialize_sets_sqlite_busy_timeout(provider):
    with provider._lock:
        timeout_ms = provider._require_conn().execute("PRAGMA busy_timeout").fetchone()[0]

    assert timeout_ms >= 10_000


def test_provider_initial_and_reconnected_writer_keep_foreign_keys_enabled(provider, monkeypatch):
    assert int(provider._require_conn().execute("PRAGMA foreign_keys").fetchone()[0]) == 1

    probes = iter((False, True))
    monkeypatch.setattr(provider, "_sqlite_write_probe", lambda _conn: next(probes))
    receipt = provider._recover_sqlite_connection_after_error("audit forced reconnect")

    assert receipt["reopened"] is True
    assert receipt["recovered"] is True
    assert int(provider._require_conn().execute("PRAGMA foreign_keys").fetchone()[0]) == 1


def test_failed_sqlite_reopen_is_retried_by_next_connection_requirement(
    provider,
    monkeypatch,
):
    live_provider_module = importlib.import_module(provider.__class__.__module__)
    original_connect = live_provider_module.connect_truth_database
    attempts = 0

    def fail_once_then_connect(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("synthetic reopen failure")
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(
        live_provider_module,
        "connect_truth_database",
        fail_once_then_connect,
    )
    monkeypatch.setattr(provider, "_sqlite_write_probe", lambda _conn: False)
    provider._maintenance_stop.set()

    receipt = provider._recover_sqlite_connection_after_error(
        "audit forced failed reconnect"
    )

    assert receipt["recovered"] is False
    assert receipt["reconnect_pending"] is True
    assert provider._conn is None
    reconnected = provider._require_conn()
    assert attempts == 2
    assert int(reconnected.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
    reconnected.execute("BEGIN IMMEDIATE")
    reconnected.rollback()
    provider._maintenance_stop.clear()


def test_rollback_failure_quarantines_provider_connection(provider, monkeypatch):
    original = provider._require_conn()

    class RollbackFailureConnection:
        in_transaction = True

        def __init__(self):
            self.closed = False

        def rollback(self):
            raise sqlite3.OperationalError("synthetic rollback failure")

        def close(self):
            self.closed = True

    failed = RollbackFailureConnection()
    provider._conn = failed
    monkeypatch.setattr(provider, "_open_runtime_connection", lambda: original)

    provider._rollback_conn_after_error("rollback quarantine regression")

    assert failed.closed is True
    assert provider._conn is None
    assert provider._require_conn() is original


def test_provider_store_rolls_back_open_sqlite_transaction(provider, monkeypatch):
    def broken_store_memory_now(provider_arg, **kwargs):
        del kwargs
        _open_transaction_and_raise(provider_arg, "provider-store")

    monkeypatch.setitem(provider._store_now.__globals__, "store_memory_now", broken_store_memory_now)

    with pytest.raises(RuntimeError, match="synthetic provider-store failure"):
        provider._store_now(
            content="rollback regression content for provider store exception",
            source="tool-store",
            target="ops",
            session_id=provider._session_id,
        )

    _assert_sqlite_writer_released(provider)


def test_tool_dispatch_rolls_back_open_sqlite_transaction(provider, monkeypatch):
    def broken_store_now(**kwargs):
        del kwargs
        _open_transaction_and_raise(provider, "tool-dispatch")

    monkeypatch.setattr(provider._composition.tool_port, "store_now", broken_store_now)

    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {"content": "rollback regression content for tool dispatch exception", "target": "ops"},
        )
    )

    assert payload["error"] == "synthetic tool-dispatch failure"
    _assert_sqlite_writer_released(provider)


def test_scope_recall_store_recovers_and_retries_after_sqlite_lock(provider, monkeypatch):
    tool_store = provider._composition.tool_port.store_now
    calls: list[dict[str, object]] = []

    def flaky_store_now(**kwargs):
        calls.append(dict(kwargs))
        if len(calls) == 1:
            conn = provider._require_conn()
            conn.execute("CREATE TABLE IF NOT EXISTS rollback_probe(marker TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO rollback_probe(marker) VALUES ('store-retry')")
            assert conn.in_transaction
            raise sqlite3.OperationalError("database is locked")
        return tool_store(**kwargs)

    monkeypatch.setattr(provider._composition.tool_port, "store_now", flaky_store_now)

    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {"content": "rollback regression content for recoverable sqlite lock", "target": "ops"},
        )
    )

    assert payload["stored"] is True
    assert payload["recovered"] is True
    assert payload["retry_count"] == 1
    assert payload["receipt"]["recovered"] is True
    assert payload["receipt"]["retry_count"] == 1
    assert len(calls) == 2
    assert calls[0] == calls[1]
    _assert_sqlite_writer_released(provider)


def test_scope_recall_store_recovers_peer_provider_dirty_transaction(tmp_path, monkeypatch):
    _write_scope_recall_config(
        tmp_path,
        {
            "vector": {
                "embedder": {"provider": "local-hash", "dimensions": 256, "model": "hash-v1"},
                "fallback_embedder": {"provider": "local-hash", "dimensions": 256, "model": "hash-v1"},
            }
        },
    )
    provider_a = load_memory_provider("scope-recall")
    provider_b = load_memory_provider("scope-recall")
    assert provider_a is not None
    assert provider_b is not None
    assert provider_a is not provider_b
    try:
        for index, plugin in enumerate((provider_a, provider_b), start=1):
            plugin.initialize(
                f"session-peer-{index}",
                hermes_home=str(tmp_path),
                platform="cli",
                agent_context="primary",
                agent_identity="yuheng",
                agent_workspace="hermes",
            )
        # This regression owns the orphan transaction timing.  Suppress and
        # drain unrelated idle maintenance before creating it so the peer is
        # observably idle when recovery begins.
        for plugin in (provider_a, provider_b):
            plugin._maintenance_stop.set()
        for plugin in (provider_a, provider_b):
            with plugin._writer_lifecycle_lock:
                pass
        with provider_a._lock:
            conn_a = provider_a._require_conn()
            conn_a.execute("BEGIN IMMEDIATE")
            conn_a.execute("CREATE TABLE IF NOT EXISTS rollback_probe(marker TEXT PRIMARY KEY)")
            conn_a.execute("INSERT INTO rollback_probe(marker) VALUES ('peer-provider')")
            assert conn_a.in_transaction
        with provider_b._lock:
            provider_b._require_conn().execute("PRAGMA busy_timeout=50")
        original_recover = provider_b._recover_sqlite_connection_after_error
        original_store_now = provider_b._composition.tool_port.store_now
        recovery_reports: list[dict[str, object]] = []
        store_attempts = 0

        def capture_recover(context: str) -> dict[str, object]:
            report = original_recover(context)
            recovery_reports.append(dict(report))
            return report

        def locked_then_store(**kwargs):
            nonlocal store_attempts
            store_attempts += 1
            if store_attempts == 1:
                raise sqlite3.OperationalError("database is locked")
            return original_store_now(**kwargs)

        monkeypatch.setattr(provider_b, "_recover_sqlite_connection_after_error", capture_recover)
        monkeypatch.setattr(provider_b._composition.tool_port, "store_now", locked_then_store)

        payload = json.loads(
            provider_b.handle_tool_call(
                "scope_recall_store",
                {"content": "peer provider sqlite lock recovery regression content", "target": "ops"},
            )
        )

        assert payload["stored"] is True
        assert payload["recovered"] is True
        assert payload["retry_count"] == 1
        assert store_attempts == 2
        assert recovery_reports
        checked = recovery_reports[0]["peer_providers_checked"]
        rollbacks = recovery_reports[0]["peer_rollbacks"]
        assert isinstance(checked, int) and checked >= 1
        assert rollbacks == 1
        with provider_a._lock:
            assert provider_a._require_conn().in_transaction is False
        _assert_sqlite_writer_released(provider_b)
    finally:
        provider_b.shutdown()
        provider_a.shutdown()


def test_sqlite_recovery_rechecks_transiently_busy_peer_before_reopen(
    tmp_path, monkeypatch
):
    _write_scope_recall_config(tmp_path, {"vector": {"enabled": False}})
    provider_a = load_memory_provider("scope-recall")
    provider_b = load_memory_provider("scope-recall")
    assert provider_a is not None
    assert provider_b is not None
    lifecycle_held = threading.Event()
    release_lifecycle = threading.Event()
    lifecycle_released = threading.Event()
    holder: threading.Thread | None = None
    try:
        for index, plugin in enumerate((provider_a, provider_b), start=1):
            plugin.initialize(
                f"session-peer-recheck-{index}",
                hermes_home=str(tmp_path),
                platform="cli",
                agent_context="primary",
                agent_identity="yuheng",
                agent_workspace="hermes",
            )
            plugin._maintenance_stop.set()
        for plugin in (provider_a, provider_b):
            with plugin._writer_lifecycle_lock:
                pass

        with provider_a._lock:
            conn_a = provider_a._require_conn()
            conn_a.execute("BEGIN IMMEDIATE")
            conn_a.execute(
                "CREATE TABLE IF NOT EXISTS peer_recheck_probe(marker TEXT PRIMARY KEY)"
            )
            conn_a.execute("INSERT INTO peer_recheck_probe(marker) VALUES ('orphan')")
            assert conn_a.in_transaction is True
        with provider_b._lock:
            conn_b = provider_b._require_conn()

        def hold_peer_lifecycle() -> None:
            with provider_a._writer_lifecycle_lock:
                lifecycle_held.set()
                release_lifecycle.wait(timeout=5.0)
            lifecycle_released.set()

        holder = threading.Thread(
            target=hold_peer_lifecycle,
            name="peer-recovery-transient-lifecycle",
        )
        holder.start()
        assert lifecycle_held.wait(timeout=2.0)

        original_probe = provider_b._sqlite_write_probe
        probe_calls = 0

        def controlled_probe(conn):
            nonlocal probe_calls
            probe_calls += 1
            if probe_calls == 1:
                release_lifecycle.set()
                assert lifecycle_released.wait(timeout=2.0)
                return False
            return original_probe(conn)

        def unexpected_reopen():
            raise AssertionError("transient peer contention must not reopen SQLite")

        monkeypatch.setattr(provider_b, "_sqlite_write_probe", controlled_probe)
        monkeypatch.setattr(provider_b, "_open_runtime_connection", unexpected_reopen)

        report = provider_b._recover_sqlite_connection_after_error(
            "transient peer lifecycle regression"
        )

        assert report["recovered"] is True
        assert report["reopened"] is False
        assert report["write_probe"] is True
        assert report["peer_recovery_passes"] == 2
        assert report["peer_busy_skipped_total"] == 1
        assert report["peer_busy_skipped"] == 0
        assert report["peer_rollbacks"] == 1
        assert report["peer_rollback_errors"] == 0
        assert report["peer_recovery_deferred"] is False
        assert probe_calls == 2
        with provider_b._lock:
            assert provider_b._require_conn() is conn_b
        with provider_a._lock:
            assert provider_a._require_conn().in_transaction is False
    finally:
        release_lifecycle.set()
        if holder is not None:
            holder.join(timeout=2.0)
        provider_b.shutdown()
        provider_a.shutdown()


def test_peer_recovery_skips_an_active_peer_transaction(tmp_path):
    _write_scope_recall_config(tmp_path, {"vector": {"enabled": False}})
    provider_a = load_memory_provider("scope-recall")
    provider_b = load_memory_provider("scope-recall")
    assert provider_a is not None
    assert provider_b is not None
    lock_held = threading.Event()
    release_peer = threading.Event()
    recovery_done = threading.Event()
    recovery_report: dict[str, object] = {}

    def hold_peer_transaction():
        with provider_a._lock:
            conn = provider_a._require_conn()
            conn.execute("BEGIN IMMEDIATE")
            lock_held.set()
            release_peer.wait(timeout=5.0)
            conn.rollback()

    def recover_other_provider():
        recovery_report.update(
            provider_b._rollback_peer_provider_transactions("busy peer regression")
        )
        recovery_done.set()

    holder = threading.Thread(target=hold_peer_transaction, daemon=True)
    recovery = threading.Thread(target=recover_other_provider, daemon=True)
    try:
        for index, plugin in enumerate((provider_a, provider_b), start=1):
            plugin.initialize(
                f"session-busy-peer-{index}",
                hermes_home=str(tmp_path),
                platform="cli",
                agent_context="primary",
                agent_identity="yuheng",
                agent_workspace="hermes",
            )
        holder.start()
        assert lock_held.wait(timeout=2.0)
        recovery.start()
        assert recovery_done.wait(timeout=0.5), "recovery blocked on an active peer"
        assert recovery_report["peer_rollbacks"] == 0
        assert recovery_report["peer_busy_skipped"] == 1
    finally:
        release_peer.set()
        holder.join(timeout=2.0)
        recovery.join(timeout=2.0)
        provider_b.shutdown()
        provider_a.shutdown()


def test_background_writer_rolls_back_open_sqlite_transaction(provider, monkeypatch):
    calls = []

    def broken_store_now(provider_arg, **kwargs):
        del kwargs
        calls.append("background-writer")
        _open_transaction_and_raise(provider_arg, "background-writer")

    monkeypatch.setitem(provider._writer_thread._target.__globals__, "store_now", broken_store_now)

    provider._write_queue.put(
        {
            "kind": "store",
            "content": "rollback regression content for background writer exception",
            "source": "test",
            "target": "memory",
            "session_id": provider._session_id,
            "metadata": {},
        }
    )

    assert provider.flush(timeout=2.0) is False
    assert calls == ["background-writer"]
    stats = json.loads(provider.handle_tool_call("scope_recall_stats", {}))
    assert stats["background_writer"] == {
        "thread_alive": True,
        "failed_writes": 1,
        "unreported_failures": 0,
        "last_error_type": "RuntimeError",
    }
    # The failed batch was acknowledged by the first flush; a later flush with
    # no new failed writes should recover to a successful receipt.
    assert provider.flush(timeout=2.0) is True
    _assert_sqlite_writer_released(provider)


def test_relation_store_failure_rolls_back_open_sqlite_transaction(provider, monkeypatch):
    calls = []
    memory_ops_globals = provider._store_now.__globals__["store_memory_now"].__globals__

    def broken_relation_sync(conn, **kwargs):
        del kwargs
        calls.append("relation-store")
        conn.execute("CREATE TABLE IF NOT EXISTS rollback_probe(marker TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO rollback_probe(marker) VALUES ('relation-store')")
        assert conn.in_transaction
        raise RuntimeError("synthetic relation-store failure")

    monkeypatch.setitem(provider._config, "relation_extraction_enabled", True)
    monkeypatch.setitem(memory_ops_globals, "sync_extracted_relations_for_memory", broken_relation_sync)
    conn = provider._require_conn()
    before = {
        "memories": int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]),
        "fts": int(conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]),
        "entities": int(conn.execute("SELECT COUNT(*) FROM memory_entities").fetchone()[0]),
        "outbox": int(conn.execute("SELECT COUNT(*) FROM vector_outbox").fetchone()[0]),
    }

    with pytest.raises(RuntimeError, match="synthetic relation-store failure"):
        provider._store_now(
            content="rollback regression content for relation store exception",
            source="tool-store",
            target="ops",
            session_id=provider._session_id,
            allow_duplicate=True,
        )

    after = {
        "memories": int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]),
        "fts": int(conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]),
        "entities": int(conn.execute("SELECT COUNT(*) FROM memory_entities").fetchone()[0]),
        "outbox": int(conn.execute("SELECT COUNT(*) FROM vector_outbox").fetchone()[0]),
    }
    assert after == before
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rollback_probe'"
        ).fetchone()
        is None
    )
    assert calls == ["relation-store"]
    _assert_sqlite_writer_released(provider)


def test_relation_update_failure_rolls_back_open_sqlite_transaction(provider, monkeypatch):
    calls = []
    memory_ops_globals = provider._store_now.__globals__["store_memory_now"].__globals__

    monkeypatch.setitem(provider._config, "relation_extraction_enabled", False)
    memory_id, inserted, outcome = provider._store_now(
        content="rollback regression original content for relation update exception",
        source="tool-store",
        target="ops",
        session_id=provider._session_id,
        allow_duplicate=True,
    )
    assert memory_id
    assert inserted is True
    assert outcome == "stored_relation_sync_disabled"

    def broken_relation_sync(conn, **kwargs):
        del kwargs
        calls.append("relation-update")
        conn.execute("CREATE TABLE IF NOT EXISTS rollback_probe(marker TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO rollback_probe(marker) VALUES ('relation-update')")
        assert conn.in_transaction
        raise RuntimeError("synthetic relation-update failure")

    monkeypatch.setitem(provider._config, "relation_extraction_enabled", True)
    monkeypatch.setitem(memory_ops_globals, "sync_extracted_relations_for_memory", broken_relation_sync)

    updated, summary, updated_at = provider._update_memory(
        memory_id,
        "rollback regression updated content for relation update exception",
        "ops",
    )

    assert updated is False
    assert "synthetic relation-update failure" in summary
    assert updated_at == ""
    assert calls == ["relation-update"]
    with provider._lock:
        conn = provider._require_conn()
        row = conn.execute(
            "SELECT content FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        probe = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rollback_probe'"
        ).fetchone()
    assert row is not None
    assert str(row["content"]) == (
        "rollback regression original content for relation update exception"
    )
    assert probe is None
    _assert_sqlite_writer_released(provider)


def test_save_config_bootstraps_sqlite_vector_meta_for_install_verification(tmp_path, monkeypatch):
    monkeypatch.delenv("SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY", raising=False)
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    vector_db_path = tmp_path / "scope-recall" / "vector.sqlite3"
    assert not vector_db_path.exists()

    plugin.save_config(
        {
            "vector": {
                "enabled": True,
                "backend": "lancedb",
                "fallback_backend": "sqlite-bruteforce",
                "table_name": "memories",
                "embedder": {
                    "provider": "openai-compatible",
                    "model": "gemini-embedding-001",
                    "api_key_env": ["SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY"],
                },
                "fallback_embedder": {"provider": "local-hash", "dimensions": 8, "model": "hash-v1"},
            }
        },
        str(tmp_path),
    )

    assert vector_db_path.is_file()
    conn = sqlite3.connect(vector_db_path)
    try:
        meta = dict(conn.execute("SELECT key, value FROM vector_meta").fetchall())
    finally:
        conn.close()
    assert meta["table_name"] == "memories"
    assert meta["dimensions"] == "8"


def test_save_config_bootstraps_primary_generation_when_backend_and_embedder_are_healthy(
    tmp_path, monkeypatch
):
    """Fresh setup must not pin fallback merely because fallback is configured."""

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    provider_package = plugin.__class__.__module__.rsplit(".", 1)[0]
    vector_bootstrap_module = importlib.import_module(
        f"{provider_package}.vector_bootstrap"
    )
    calls = {"embedder": 0, "store": 0}

    class FakeEmbedder:
        def __init__(self, config):
            self.provider = str(config.get("provider") or "fake")
            self.model = str(config.get("model") or "fake-model")
            self.dimensions = int(config.get("dimensions") or 12)

        @staticmethod
        def is_available():
            return True

    class FakeStore:
        def __init__(self, backend):
            self.backend = backend

        @staticmethod
        def is_available():
            return True

        @staticmethod
        def open():
            return None

        @staticmethod
        def open_existing():
            return None

        @staticmethod
        def close():
            return None

        @staticmethod
        def audit_counts():
            return {"physical_rows": 0, "unique_ids": 0, "duplicate_rows": 0}

    def build_fake_embedder(config):
        calls["embedder"] += 1
        return FakeEmbedder(config)

    def build_fake_store(backend, **_kwargs):
        calls["store"] += 1
        return FakeStore(str(backend))

    monkeypatch.setattr(vector_bootstrap_module, "build_embedder", build_fake_embedder)
    monkeypatch.setattr(vector_bootstrap_module, "build_vector_store", build_fake_store)

    plugin.save_config(
        {
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
                    "model": "fallback-embedding",
                    "dimensions": 8,
                },
            }
        },
        str(tmp_path),
    )
    assert calls["embedder"] >= 2
    assert calls["store"] >= 3

    conn = sqlite3.connect(tmp_path / "scope-recall" / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        generation = conn.execute(
            """
            SELECT g.*
            FROM vector_generation_state AS s
            JOIN vector_generations AS g ON g.generation_id = s.value
            WHERE s.key = 'current_generation'
            """
        ).fetchone()
    finally:
        conn.close()
    assert generation is not None
    assert generation["backend"] == "lancedb"
    assert generation["provider"] == "openai-compatible"
    assert generation["model"] == "primary-embedding"
    assert generation["dimensions"] == 12


def test_tool_journal_content_defaults_to_safe_summary_without_raw_output(provider):
    content = json.dumps(
        {
            "output": "pytest passed with private details",
            "exit_code": 0,
            "error": None,
        }
    )

    journal_content = provider._tool_journal_content({"name": "terminal", "content": content})

    assert journal_content.startswith("Tool execution summary (terminal):")
    assert "Tool execution trace" not in journal_content
    assert "exit_code=0" in journal_content
    assert "output_preview=omitted" in journal_content
    assert "pytest passed" not in journal_content

    assert provider._tool_journal_content({"name": "terminal", "content": "api_key=" + "sk-" + "A" * 24}) == ""

    error_content = json.dumps({"exit_code": 1, "error": "Traceback wrote /home/a/private/project/output.log"})
    error_journal_content = provider._tool_journal_content({"name": "terminal", "content": error_content})
    assert "[REDACTED_PATH]" in error_journal_content
    assert "/home/a/private" not in error_journal_content


def test_tool_journal_content_skips_session_message_dumps(provider):
    huge_session_dump = json.dumps(
        {
            "messages": [
                {
                    "role": "tool",
                    "content": "x" * 200_000,
                }
            ]
        }
    )

    assert (
        provider._tool_journal_content(
            {
                "name": "mcp_hermes_studio_use_hermes_studio_use_session_messages",
                "content": huge_session_dump,
            }
        )
        == ""
    )


def test_append_session_tool_journal_skips_session_message_dumps(provider):
    provider._append_session_tool_journal(
        [
            {
                "role": "tool",
                "name": "mcp_hermes_studio_use_hermes_studio_use_session_messages",
                "content": json.dumps({"messages": [{"role": "tool", "content": "x" * 200_000}]}),
            }
        ]
    )

    with provider._lock:
        row = provider._require_conn().execute("SELECT COUNT(*) FROM journal_entries WHERE role = 'tool'").fetchone()
    assert row[0] == 0


def test_background_journal_digest_recovers_and_retries_one_lock(provider, monkeypatch):
    calls = {"digest": 0, "recovery": 0}
    live_provider_module = importlib.import_module(provider.__class__.__module__)

    def flaky_digest(**_kwargs):
        calls["digest"] += 1
        if calls["digest"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return {"ok": True, "status": "ok", "processed_entries": 1}

    def recover(_context):
        calls["recovery"] += 1
        return {"recovered": True}

    monkeypatch.setattr(live_provider_module, "run_journal_digest", flaky_digest)
    monkeypatch.setattr(provider, "_recover_sqlite_connection_after_error", recover)

    provider._run_background_journal_digest(
        {"extractor": "heuristic", "digest_interval_hours": 2}
    )

    assert calls == {"digest": 2, "recovery": 1}
    assert provider._last_journal_digest_status == "ok"
    assert provider._journal_digest_consecutive_failures == 0


def test_background_journal_digest_does_not_retry_non_lock_error(provider, monkeypatch):
    calls = {"digest": 0, "recovery": 0}
    live_provider_module = importlib.import_module(provider.__class__.__module__)

    def failed_digest(**_kwargs):
        calls["digest"] += 1
        raise RuntimeError("synthetic extractor failure")

    def recover(_context):
        calls["recovery"] += 1
        return {"recovered": True}

    monkeypatch.setattr(live_provider_module, "run_journal_digest", failed_digest)
    monkeypatch.setattr(provider, "_recover_sqlite_connection_after_error", recover)

    provider._run_background_journal_digest(
        {"extractor": "heuristic", "digest_interval_hours": 2}
    )

    assert calls == {"digest": 1, "recovery": 0}
    assert provider._last_journal_digest_status == "error"
    assert provider._journal_digest_consecutive_failures == 1


def test_background_journal_digest_allows_dynamic_limit_when_not_overridden(tmp_path, monkeypatch):
    _write_scope_recall_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "journal": {
                "enabled": True,
                "background_digest_enabled": True,
                "background_digest_synchronous": True,
                "digest_interval_hours": 0.001,
                "max_entries_per_digest": 5,
                "dynamic_max_entries_enabled": True,
                "dynamic_backlog_threshold": 10,
                "max_entries_per_digest_ceiling": 50,
                "extractor": "llm",
            },
        },
    )
    captured = {}

    def fake_digest(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "status": "ok", "processed_entries": 0}

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    monkeypatch.setitem(plugin._run_background_journal_digest.__globals__, "run_journal_digest", fake_digest)
    plugin.initialize(
        "session-background-dynamic-limit",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        plugin._run_background_journal_digest(plugin._journal_config())
    finally:
        plugin.shutdown()

    assert captured["dry_run"] is False
    assert captured["extractor"] == "llm"
    assert captured["limit_entries"] is None


def test_background_digest_auto_promotion_runs_when_enabled(tmp_path, monkeypatch):
    _write_scope_recall_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "journal": {
                "enabled": True,
                "background_digest_enabled": True,
                "background_digest_synchronous": True,
                "digest_interval_hours": 0.001,
                "max_entries_per_digest": 5,
                "extractor": "heuristic",
            },
            "experience": {
                "enabled": True,
                "auto_promotion_enabled": True,
                "auto_promotion_limit_sessions": 7,
                "auto_promote_low_risk": True,
            },
        },
    )
    calls = {"digest": 0, "promote": []}

    def fake_digest(**kwargs):
        calls["digest"] += 1
        assert kwargs["dry_run"] is False
        return {"ok": True, "processed_entries": 1}

    def fake_promote(conn, **kwargs):
        calls["promote"].append(kwargs)
        assert conn is not None
        return {"handbooks_created": 1, "handbooks_promoted": 1}

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    monkeypatch.setitem(plugin._run_background_journal_digest.__globals__, "run_journal_digest", fake_digest)
    monkeypatch.setattr(
        _production_experience_promotion_module(plugin),
        "promote_experiences",
        fake_promote,
    )
    plugin.initialize(
        "session-auto-promotion",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        plugin._run_background_journal_digest(plugin._journal_config())
    finally:
        plugin.shutdown()

    assert calls["digest"] == 1
    assert len(calls["promote"]) == 1
    assert calls["promote"][0]["dry_run"] is False
    assert calls["promote"][0]["limit_sessions"] == 7
    assert calls["promote"][0]["scope_id"]
    assert calls["promote"][0]["accessible_scope_ids"]


def test_background_digest_auto_promotion_creates_playbook_from_journal(tmp_path, monkeypatch):
    _write_scope_recall_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "journal": {
                "enabled": True,
                "background_digest_enabled": True,
                "background_digest_synchronous": True,
                "digest_interval_hours": 0.001,
                "max_entries_per_digest": 5,
                "extractor": "heuristic",
            },
            "experience": {
                "enabled": True,
                "auto_promotion_enabled": True,
                "auto_promotion_limit_sessions": 5,
                "auto_promote_low_risk": True,
            },
        },
    )
    calls = {"digest": 0}

    def fake_digest(**kwargs):
        calls["digest"] += 1
        return {"ok": True, "processed_entries": 3}

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    monkeypatch.setitem(plugin._run_background_journal_digest.__globals__, "run_journal_digest", fake_digest)
    plugin.initialize(
        "session-docs",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        with plugin._lock:
            conn = plugin._require_conn()
            append_journal_entry(
                conn,
                scope=plugin._scope,
                scope_id=plugin._scope_id,
                shared_scope_id=plugin._shared_scope_id,
                session_id="session-docs",
                turn_number=1,
                role="user",
                content="检查 scope-recall 文档链接和发布说明是否一致。",
            )
            append_journal_entry(
                conn,
                scope=plugin._scope,
                scope_id=plugin._scope_id,
                shared_scope_id=plugin._shared_scope_id,
                session_id="session-docs",
                turn_number=2,
                role="tool",
                content="Tool execution trace (terminal): python -m pytest tests/test_release.py -q -> 5 passed; ruff ok; docs smoke ok.",
            )
            append_journal_entry(
                conn,
                scope=plugin._scope,
                scope_id=plugin._scope_id,
                shared_scope_id=plugin._shared_scope_id,
                session_id="session-docs",
                turn_number=3,
                role="assistant",
                content="完成：文档检查通过，测试通过，验证完成。下次可以复用这套检查流程。",
            )
        plugin._run_background_journal_digest(plugin._journal_config())
        with plugin._lock:
            row = plugin._require_conn().execute("SELECT status, title FROM procedural_playbooks").fetchone()
    finally:
        plugin.shutdown()

    assert calls["digest"] == 1
    assert row is not None
    assert row["status"] == "promoted"
    assert "scope-recall" in row["title"].lower()


def test_background_digest_auto_promotion_is_opt_in_by_default(tmp_path, monkeypatch):
    _write_scope_recall_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "journal": {
                "enabled": True,
                "background_digest_enabled": True,
                "background_digest_synchronous": True,
                "digest_interval_hours": 0.001,
                "max_entries_per_digest": 5,
                "extractor": "heuristic",
            },
            "experience": {"enabled": True},
        },
    )
    calls = {"digest": 0, "promote": 0}

    def fake_digest(**kwargs):
        calls["digest"] += 1
        return {"ok": True, "processed_entries": 1}

    def fake_promote(conn, **kwargs):
        calls["promote"] += 1
        return {"handbooks_created": 1}

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    monkeypatch.setitem(plugin._run_background_journal_digest.__globals__, "run_journal_digest", fake_digest)
    monkeypatch.setattr(
        _production_experience_promotion_module(plugin),
        "promote_experiences",
        fake_promote,
    )
    plugin.initialize(
        "session-auto-promotion-default-off",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        plugin._run_background_journal_digest(plugin._journal_config())
    finally:
        plugin.shutdown()

    assert calls["digest"] == 1
    assert calls["promote"] == 0


def test_background_digest_auto_promotion_runs_when_enabled_by_config(tmp_path, monkeypatch):
    _write_scope_recall_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "journal": {
                "enabled": True,
                "background_digest_enabled": True,
                "background_digest_synchronous": True,
                "digest_interval_hours": 0.001,
                "max_entries_per_digest": 5,
                "extractor": "heuristic",
            },
            "experience": {"enabled": True, "auto_promotion_enabled": True},
        },
    )
    calls = {"digest": 0, "promote": 0}

    def fake_digest(**kwargs):
        calls["digest"] += 1
        return {"ok": True, "processed_entries": 1}

    def fake_promote(conn, **kwargs):
        calls["promote"] += 1
        return {"handbooks_created": 1}

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    monkeypatch.setitem(plugin._run_background_journal_digest.__globals__, "run_journal_digest", fake_digest)
    monkeypatch.setattr(
        _production_experience_promotion_module(plugin),
        "promote_experiences",
        fake_promote,
    )
    plugin.initialize(
        "session-auto-promotion-enabled",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        plugin._run_background_journal_digest(plugin._journal_config())
    finally:
        plugin.shutdown()

    assert calls["digest"] == 1
    assert calls["promote"] == 1


def test_profile_tool_schema_is_registered(provider):
    names = {schema["name"] for schema in provider.get_tool_schemas()}

    assert "scope_recall_profile" in names


def test_profile_surface_live_reads_curated_memory_without_sqlite_duplication(tmp_path):
    _write_scope_recall_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "curated_memory": {"mode": "profile-global"},
        },
    )
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir(parents=True, exist_ok=True)
    (memories_dir / "USER.md").write_text("Joy prefers version bumps to include a clear semver rationale.\n", encoding="utf-8")

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-profile-curated",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        payload = json.loads(plugin.handle_tool_call("scope_recall_profile", {"max_chars": 1200}))
        with plugin._lock:
            memory_count = plugin._require_conn().execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    finally:
        plugin.shutdown()

    assert payload["surface"] == "profile"
    assert payload["curated"]["count"] == 1
    assert "semver rationale" in payload["context"]
    assert any(item["source"] == "builtin-curated" for item in payload["sections"]["user"]["items"])
    assert memory_count == 0


def test_profile_surface_respects_gateway_user_isolation_and_multisession_durable_recall(tmp_path):
    _write_scope_recall_config(tmp_path, {"vector": {"enabled": False}})

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-profile-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        chat_id="chat-a",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        stored = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "target": "user",
                    "content": "Joy profile surface test preference: answer release-governance questions with concise evidence tables.",
                },
            )
        )
        assert stored["stored"] is True
    finally:
        plugin.shutdown()

    same_user = load_memory_provider("scope-recall")
    assert same_user is not None
    same_user.initialize(
        "session-profile-b",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        chat_id="chat-b",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        same_payload = json.loads(same_user.handle_tool_call("scope_recall_profile", {"max_chars": 1200}))
    finally:
        same_user.shutdown()

    other_user = load_memory_provider("scope-recall")
    assert other_user is not None
    other_user.initialize(
        "session-profile-c",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="other-user",
        chat_id="chat-c",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        other_payload = json.loads(other_user.handle_tool_call("scope_recall_profile", {"max_chars": 1200}))
    finally:
        other_user.shutdown()

    assert "release-governance questions" in same_payload["context"]
    assert same_payload["sections"]["user"]["count"] == 1
    assert "release-governance questions" not in other_payload["context"]
    assert other_payload["sections"]["user"]["count"] == 0


def test_ordinary_recall_hides_migrated_scratch_lifecycle(provider):
    stored = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {
                "target": "ops",
                "content": "Quasar scratch lifecycle sentinel must never appear in ordinary recall results.",
            },
        )
    )
    assert stored["stored"] is True
    memory_id = stored["id"]
    with provider._lock:
        conn = provider._require_conn()
        row = conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
        metadata = json.loads(str(row["metadata"] or "{}"))
        metadata["lifecycle"] = "scratch"
        conn.execute(
            "UPDATE memories SET metadata = ? WHERE id = ?",
            (json.dumps(metadata, ensure_ascii=False, sort_keys=True), memory_id),
        )
        conn.commit()

    payload = json.loads(provider.handle_tool_call("scope_recall_search", {"query": "quasar scratch lifecycle sentinel", "limit": 10}))
    assert memory_id not in {item["id"] for item in payload["results"]}


def test_profile_surface_defaults_to_promoted_only_and_can_include_candidates(tmp_path):
    _write_scope_recall_config(tmp_path, {"vector": {"enabled": False}})
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-profile-lifecycle",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        chat_id="chat-lifecycle",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        promoted = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "target": "ops",
                    "content": "Profile promoted-only regression: stable deploy workflow uses release gate evidence before rollout.",
                },
            )
        )
        candidate = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "target": "ops",
                    "content": "Temporary profile candidate regression: this provisional rollout note should require include_candidates.",
                },
            )
        )
        assert promoted["stored"] is True
        assert candidate["stored"] is True
        # Older installs may have stable rows without an explicit lifecycle key;
        # promoted-only profile should continue treating those as promoted.
        with plugin._lock:
            conn = plugin._require_conn()
            row = conn.execute("SELECT id, metadata FROM memories WHERE content LIKE ?", ("%stable deploy workflow%",)).fetchone()
            metadata = json.loads(str(row["metadata"] or "{}"))
            metadata.pop("lifecycle", None)
            conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (json.dumps(metadata, ensure_ascii=False, sort_keys=True), row["id"]))
            conn.commit()
        default_payload = json.loads(plugin.handle_tool_call("scope_recall_profile", {"targets": ["ops"], "max_chars": 2000}))
        candidate_payload = json.loads(
            plugin.handle_tool_call("scope_recall_profile", {"targets": ["ops"], "include_candidates": True, "max_chars": 2000})
        )
    finally:
        plugin.shutdown()

    assert default_payload["include_candidates"] is False
    assert "stable deploy workflow" in default_payload["context"]
    assert "provisional rollout note" not in default_payload["context"]
    assert default_payload["sections"]["ops"]["count"] == 1
    assert candidate_payload["include_candidates"] is True
    assert "stable deploy workflow" in candidate_payload["context"]
    assert "provisional rollout note" in candidate_payload["context"]
    assert candidate_payload["sections"]["ops"]["count"] == 2


def test_profile_surface_includes_local_general_only_when_requested(tmp_path):
    _write_scope_recall_config(tmp_path, {"vector": {"enabled": False}})
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-profile-general",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        chat_id="chat-general",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        stored = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "target": "general",
                    "content": "Local profile scratch highlight: this temporary chat context should appear only when general is requested.",
                },
            )
        )
        assert stored["stored"] is True
        default_payload = json.loads(plugin.handle_tool_call("scope_recall_profile", {"max_chars": 1200}))
        general_payload = json.loads(plugin.handle_tool_call("scope_recall_profile", {"include_general": True, "max_chars": 1200}))
    finally:
        plugin.shutdown()

    assert "temporary chat context" not in default_payload["context"]
    assert default_payload["sections"]["general"]["count"] == 0
    assert "temporary chat context" in general_payload["context"]
    assert general_payload["sections"]["general"]["count"] == 1


def test_sync_turn_does_not_store_raw_user_turns_by_default(provider):
    provider.sync_turn(
        [{"type": "text", "text": "We deploy services with uv run after structured gateway messages."}],
        [{"type": "text", "text": "Got it."}],
    )
    provider.flush(timeout=2.0)

    with provider._lock:
        rows = provider._require_conn().execute("SELECT source, target, content FROM memories").fetchall()
        journal_count = provider._require_conn().execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
    assert rows == []
    assert journal_count == 1


def test_sync_turn_endpoint_policy_block_suppresses_capture_fallbacks(provider):
    provider._config.update(
        {
            "min_capture_length": 20,
            "capture_raw_user": True,
            "capture_assistant": True,
            "per_turn_extraction": {"enabled": True},
            "journal": {"enabled": False},
            "capture_llm": {
                "enabled": True,
                "model": "test-model",
                "base_url": "http://198.51.100.10:65535",
                "api_key": "test-key",
                "allow_insecure_endpoint": False,
                "min_user_chars": 1,
                "min_assistant_chars": 1,
            },
        }
    )

    provider.sync_turn(
        "Remember that this ordinary deployment note is long enough for raw capture fallback.",
        "Acknowledged; this assistant response is also long enough for fallback capture.",
    )
    provider.flush(timeout=2.0)

    with provider._lock:
        rows = provider._require_conn().execute(
            "SELECT source, target, content FROM memories ORDER BY created_at"
        ).fetchall()
    assert rows == []


def test_sync_turn_transient_capture_failure_keeps_explicit_raw_fallback(
    provider,
    monkeypatch,
):
    import scope_recall.capture_llm as capture_llm_module

    provider._config.update(
        {
            "min_capture_length": 20,
            "capture_raw_user": True,
            "capture_assistant": False,
            "per_turn_extraction": {"enabled": False},
            "journal": {"enabled": False},
            "capture_llm": {
                "enabled": True,
                "model": "test-model",
                "base_url": "https://api.example.test",
                "api_key": "test-key",
                "min_user_chars": 1,
                "min_assistant_chars": 1,
            },
        }
    )

    def fail_capture(*_args, **_kwargs):
        raise TimeoutError("simulated capture provider timeout")

    monkeypatch.setattr(capture_llm_module, "_call_openai_compatible", fail_capture)

    user_content = "This ordinary message is long enough for the explicitly enabled raw fallback."
    provider.sync_turn(
        user_content,
        "Acknowledged; this response is long enough to enter capture extraction.",
    )
    provider.flush(timeout=2.0)

    with provider._lock:
        rows = provider._require_conn().execute(
            "SELECT source, target, content FROM memories ORDER BY created_at"
        ).fetchall()
    assert [(row["source"], row["target"], row["content"]) for row in rows] == [
        ("turn-user", "general", user_content)
    ]


def test_sync_turn_preserves_long_user_turns_in_journal_chunks(provider):
    marker = "TAIL-MARKER-PROVIDER-LONG-TURN"
    long_user = "用户长任务说明：" + ("不要把长 turn 因为 capture_hard_max_chars 丢弃，要进入 journal chunking。" * 90) + marker

    provider.on_turn_start(7, long_user)
    provider.sync_turn(long_user, "ok")
    provider.flush(timeout=2.0)

    with provider._lock:
        rows = provider._require_conn().execute("SELECT content, metadata FROM journal_entries ORDER BY id").fetchall()
    assert len(rows) >= 2
    assert marker in "".join(row["content"] for row in rows)
    metadata = [json.loads(row["metadata"] or "{}") for row in rows]
    assert all(item.get("original_content_hash") for item in metadata)
    assert [item.get("chunk_index") for item in metadata] == list(range(1, len(rows) + 1))


def test_on_session_end_captures_tool_trace_but_does_not_promote_it_with_heuristic_digest(tmp_path):
    _write_scope_recall_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "journal": {"enabled": True, "digest_on_session_end": True, "extractor": "heuristic", "max_entries_per_digest": 20},
        },
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-tool-trace",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        plugin.on_session_end(
            [
                {
                    "role": "tool",
                    "name": "exec_command",
                    "content": "pytest failed because memory_journal_sources kept orphan links after delete_rows; fix delete_rows cleanup.",
                }
            ]
        )
        with plugin._lock:
            journal_row = plugin._require_conn().execute("SELECT content FROM journal_entries ORDER BY id DESC LIMIT 1").fetchone()
            memory = plugin._require_conn().execute("SELECT content FROM memories WHERE source = 'journal-digest'").fetchone()
        assert journal_row is not None
        assert "Tool execution summary (exec_command)" in journal_row["content"]
        assert "output_preview=omitted" in journal_row["content"]
        assert "orphan links" not in journal_row["content"]
        assert memory is None
    finally:
        plugin.shutdown()


def test_background_journal_digest_runs_after_append_and_respects_interval(tmp_path, monkeypatch):
    _write_scope_recall_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "journal": {
                "enabled": True,
                "background_digest_enabled": True,
                "background_digest_synchronous": True,
                "digest_interval_hours": 1,
                "extractor": "heuristic",
                "max_entries_per_digest": 20,
            },
        },
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize("session-background", hermes_home=str(tmp_path), platform="cli", agent_context="primary", agent_identity="yuheng", agent_workspace="hermes")
    calls = []

    def fake_digest(journal_config, *, digest_fn=None):
        del digest_fn
        calls.append(dict(journal_config))

    monkeypatch.setattr(plugin._background, "run_digest", fake_digest)
    try:
        plugin.sync_turn("用户要求 scope-recall 后台 digest 自动合并 journal evidence，而不是永远暂存。", "ok")
        plugin.sync_turn("用户继续说明：同一小时内不应该重复启动 digest worker。", "ok")
        assert len(calls) == 1

        plugin._last_journal_digest_started -= 3601
        plugin.sync_turn("用户继续说明：超过 digest_interval_hours 后可以再次调度 journal digest。", "ok")
        assert len(calls) == 2
        assert all(call["extractor"] == "heuristic" for call in calls)
    finally:
        plugin.shutdown()


def test_background_journal_digest_is_nonblocking_and_not_duplicated_while_running(tmp_path, monkeypatch):
    _write_scope_recall_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "journal": {
                "enabled": True,
                "background_digest_enabled": True,
                "digest_interval_hours": 1,
                "extractor": "heuristic",
                "max_entries_per_digest": 20,
            },
        },
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize("session-background-async", hermes_home=str(tmp_path), platform="cli", agent_context="primary", agent_identity="yuheng", agent_workspace="hermes")
    started = threading.Event()
    release = threading.Event()
    calls = []

    def fake_digest(journal_config, *, digest_fn=None):
        del digest_fn
        calls.append(dict(journal_config))
        started.set()
        release.wait(timeout=2.0)

    monkeypatch.setattr(plugin._background, "run_digest", fake_digest)
    try:
        before = time.monotonic()
        plugin.sync_turn("用户要求后台 digest 不能阻塞普通 sync_turn 前台路径。", "ok")
        elapsed = time.monotonic() - before
        assert elapsed < 0.5
        assert started.wait(timeout=2.0)

        plugin.sync_turn("worker 仍在运行时不能重复启动第二个 digest。", "ok")
        assert len(calls) == 1
    finally:
        release.set()
        thread = getattr(plugin, "_journal_digest_thread", None)
        if thread is not None:
            thread.join(timeout=2.0)
        plugin.shutdown()


def test_background_journal_digest_failure_does_not_break_foreground_or_advance_watermark(tmp_path, monkeypatch):
    _write_scope_recall_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "journal": {
                "enabled": True,
                "background_digest_enabled": True,
                "background_digest_synchronous": True,
                "digest_interval_hours": 1,
                "extractor": "llm",
                "max_entries_per_digest": 20,
            },
        },
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize("session-background-fail", hermes_home=str(tmp_path), platform="cli", agent_context="primary", agent_identity="yuheng", agent_workspace="hermes")
    provider_module = importlib.import_module(type(plugin).__module__)

    def failing_digest(**kwargs):
        raise RuntimeError("simulated background LLM outage")

    monkeypatch.setattr(provider_module, "run_journal_digest", failing_digest)
    try:
        plugin.sync_turn("后台 LLM digest 失败时，前台 sync_turn 不能失败，也不能消费 journal 水位。", "ok")
        with plugin._lock:
            row = plugin._require_conn().execute("SELECT processed_run_id FROM journal_entries ORDER BY id LIMIT 1").fetchone()
        assert row is not None
        assert row["processed_run_id"] == ""
        assert plugin._background.last_status == "error"
        assert plugin._background.last_error
    finally:
        plugin.shutdown()


def test_sync_turn_accepts_structured_content_when_raw_capture_is_explicitly_enabled(provider):
    provider._config["capture_raw_user"] = True

    provider.sync_turn(
        [{"type": "text", "text": "We deploy services with uv run after structured gateway messages."}],
        [{"type": "text", "text": "Got it."}],
    )
    provider.flush(timeout=2.0)

    provider.on_turn_start(1, "How do structured gateway messages deploy services?")
    result = provider.prefetch("How do structured gateway messages deploy services?")
    assert "uv run" in result.lower()


def test_sync_turn_strips_raw_inline_data_url_without_losing_surrounding_text(provider):
    payload = "A" * 1024
    provider.sync_turn(
        f"请分析截图并保留前文 data:image/jpeg;base64,{payload} 然后记住升级流程后文",
        "Acknowledged.",
    )
    provider.flush(timeout=2.0)

    with provider._lock:
        rows = provider._require_conn().execute("SELECT role, content FROM journal_entries ORDER BY id").fetchall()

    user_rows = [str(row["content"]) for row in rows if row["role"] == "user"]
    assert user_rows == ["请分析截图并保留前文 然后记住升级流程后文"]
    combined = "\n".join(str(row["content"]) for row in rows)
    assert "data:image" not in combined
    assert "inline image" not in combined.lower()
    assert payload not in combined


def test_sync_turn_strips_image_attachment_markers_before_journal(provider):
    provider.sync_turn(
        "现在要我扫码，我去哪扫啊\n\n"
        "[Image attached at: /tmp/hermes-home/image_cache/img_ccf883cb57da.jpg]\n"
        "[inline image/jpeg data omitted]\n"
        "[screenshot]",
        "Use an authenticator app or manual setup key.",
    )
    provider.flush(timeout=2.0)

    with provider._lock:
        rows = provider._require_conn().execute("SELECT role, content FROM journal_entries ORDER BY id").fetchall()

    user_rows = [row["content"] for row in rows if row["role"] == "user"]
    assert user_rows == ["现在要我扫码，我去哪扫啊"]
    combined = "\n".join(row["content"] for row in rows)
    assert "image_cache" not in combined
    assert "inline image" not in combined.lower()
    assert "screenshot" not in combined.lower()


def test_on_pre_compress_stages_sanitized_messages_in_journal_without_direct_memory(provider, monkeypatch):
    digest_calls = []
    monkeypatch.setattr(provider, "_maybe_start_background_journal_digest", lambda: digest_calls.append("scheduled"))

    note = provider.on_pre_compress(
        [
            {
                "role": "user",
                "content": (
                    "We decided scope-recall release workflow uses PyPI Trusted Publishing after 2FA.\n"
                    "[Image attached at: /tmp/hermes-home/image_cache/img_ccf883cb57da.jpg]"
                ),
            },
            {
                "role": "assistant",
                "content": "Implementation note: keep workflow pypi.yml and environment pypi for OIDC publishing.",
            },
        ]
    )

    assert "Scope Recall" in note
    assert "compression" in note.lower()
    assert digest_calls == ["scheduled"]
    with provider._lock:
        rows = provider._require_conn().execute("SELECT role, content, metadata FROM journal_entries ORDER BY id").fetchall()
        memory_count = provider._require_conn().execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert memory_count == 0
    assert [row["role"] for row in rows] == ["user", "assistant"]
    combined = "\n".join(row["content"] for row in rows)
    assert "Trusted Publishing" in combined
    assert "pypi.yml" in combined
    assert "image_cache" not in combined
    metadata = [json.loads(row["metadata"] or "{}") for row in rows]
    assert all(item.get("source") == "pre-compression" for item in metadata)


def test_on_pre_compress_dry_runs_event_candidates_without_memory_write(provider, monkeypatch):
    monkeypatch.setattr(provider, "_maybe_start_background_journal_digest", lambda: None)

    note = provider.on_pre_compress(
        [
            {
                "role": "user",
                "content": "User prefers concise Chinese release reports with exact verification outputs.",
            }
        ]
    )

    assert "Scope Recall" in note
    report = provider._last_event_digest_report
    assert report["events_seen"] == 1
    assert report["candidates_proposed"] == 1
    assert report["write_candidates"] is False
    assert report["dry_run"] is True
    with provider._lock:
        assert provider._require_conn().execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def test_on_pre_compress_writes_event_candidates_only_when_enabled(provider, monkeypatch):
    monkeypatch.setattr(provider, "_maybe_start_background_journal_digest", lambda: None)
    provider._config["event_digest"] = {"enabled": True, "write_candidates": True, "dry_run_log": True}

    note = provider.on_pre_compress(
        [
            {
                "role": "user",
                "content": "User prefers concise Chinese release reports with exact verification outputs.",
            }
        ]
    )

    assert "Scope Recall" in note
    report = provider._last_event_digest_report
    assert report["write_candidates"] is True
    assert report["dry_run"] is False
    assert report["store"]["inserted"] == 1
    assert report["vector_replay"]["completed"] == 1
    with provider._lock:
        conn = provider._require_conn()
        row = conn.execute("SELECT target, source, metadata FROM memories").fetchone()
        audit = conn.execute("SELECT event_type, action, target_id FROM governance_audit_events").fetchone()
        outbox = conn.execute(
            """
            SELECT vector_outbox.status, memories.source AS memory_source
            FROM vector_outbox
            JOIN memories ON memories.id = vector_outbox.memory_id
            WHERE memories.source = 'event-digest'
            """
        ).fetchone()
    metadata = json.loads(row["metadata"])
    assert row["target"] == "user"
    assert row["source"] == "event-digest"
    assert metadata["lifecycle"] == "candidate"
    assert metadata["event_digest"] is True
    assert audit["event_type"] == "event_candidate"
    assert audit["action"] == "insert_candidate"
    assert outbox["status"] == "completed"
    assert outbox["memory_source"] == "event-digest"


def test_event_candidate_write_replays_vector_outbox_after_commit(provider, monkeypatch):
    """Candidate writes must not leave zero-attempt vector events stranded."""

    monkeypatch.setattr(provider, "_maybe_start_background_journal_digest", lambda: None)
    provider._config["event_digest"] = {
        "enabled": True,
        "write_candidates": True,
        "dry_run_log": True,
    }
    pkg = provider.__class__.__module__.rsplit(".", 1)[0]
    live_vector = importlib.import_module(f"{pkg}.vector_runtime")
    replay_calls: list[int] = []

    def record_replay(runtime_provider, *, limit):
        assert runtime_provider is provider
        assert not provider._lock._is_owned(), (
            "vector replay must run after the provider SQLite critical section"
        )
        assert provider._require_conn().in_transaction is False
        replay_calls.append(int(limit))
        return {"claimed": 1, "completed": 1, "failed": 0}

    monkeypatch.setattr(live_vector, "replay_vector_outbox", record_replay)
    monkeypatch.setattr(live_vector, "vector_write_replay_limit", lambda _provider: 17)

    provider.on_pre_compress(
        [
            {
                "role": "user",
                "content": "User prefers concise Chinese release reports with exact verification outputs.",
            }
        ]
    )

    assert replay_calls == [17]


def test_event_candidate_store_holds_provider_sqlite_lock(provider, monkeypatch):
    monkeypatch.setattr(
        provider,
        "_maybe_start_background_journal_digest",
        lambda: None,
    )
    provider._config["event_digest"] = {
        "enabled": True,
        "write_candidates": True,
        "dry_run_log": True,
    }
    pkg = provider.__class__.__module__.rsplit(".", 1)[0]
    live_store = importlib.import_module(f"{pkg}.candidate_store")
    original_store = live_store.store_event_candidates
    assert not provider._lock._is_owned()

    def guarded_store(*args, **kwargs):
        assert provider._lock._is_owned(), (
            "event candidate SQLite access must hold the provider connection lock"
        )
        return original_store(*args, **kwargs)

    monkeypatch.setattr(live_store, "store_event_candidates", guarded_store)

    provider.on_pre_compress(
        [
            {
                "role": "user",
                "content": "User prefers concise Chinese release reports with exact verification outputs.",
            }
        ]
    )


def test_on_pre_compress_rejects_generic_chat_even_when_candidate_writes_enabled(provider, monkeypatch):
    monkeypatch.setattr(provider, "_maybe_start_background_journal_digest", lambda: None)
    provider._config["event_digest"] = {"enabled": True, "write_candidates": True, "dry_run_log": True}

    provider.on_pre_compress(
        [
            {
                "role": "user",
                "content": "Please review the last few messages and explain the conversation so far.",
            }
        ]
    )

    report = provider._last_event_digest_report
    assert report["write_candidates"] is True
    assert report["candidates_proposed"] == 0
    assert report["store"]["inserted"] == 0
    assert report["rejection_reasons"] == {"unclassified_event_candidate": 1}
    with provider._lock:
        assert provider._require_conn().execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def test_on_pre_compress_filters_wrappers_tools_trivial_acks_and_secrets(provider, monkeypatch):
    monkeypatch.setattr(provider, "_maybe_start_background_journal_digest", lambda: None)

    note = provider.on_pre_compress(
        [
            {"role": "system", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] stale wrapper text"},
            {"role": "tool", "content": "Tool output says password=super-secret-value should never be staged."},
            {"role": "user", "content": "api_key=sk-" + "a" * 30},
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "Project Orion decision: keep SQLite truth before rebuilding vector companions."},
        ]
    )

    assert "1" in note
    with provider._lock:
        rows = provider._require_conn().execute("SELECT role, content FROM journal_entries ORDER BY id").fetchall()
        memory_count = provider._require_conn().execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert memory_count == 0
    assert [(row["role"], row["content"]) for row in rows] == [
        ("user", "Project Orion decision: keep SQLite truth before rebuilding vector companions.")
    ]


def test_session_end_tool_trace_sanitizes_attachment_markers_before_journal(provider, monkeypatch):
    monkeypatch.setattr(provider, "_run_session_end_journal_digest", lambda: None)

    provider.on_session_end(
        [
            {
                "role": "tool",
                "name": "browser_vision",
                "content": (
                    "Visual smoke result: login button is visible.\n"
                    "[Image attached at: /tmp/hermes-home/image_cache/img_ccf883cb57da.jpg]\n"
                    "[inline image/jpeg data omitted]\n"
                    "Screenshot evidence captured."
                ),
            }
        ]
    )

    with provider._lock:
        rows = provider._require_conn().execute("SELECT role, content FROM journal_entries ORDER BY id").fetchall()
    assert len(rows) == 1
    assert rows[0]["role"] == "tool"
    assert "Tool execution summary (browser_vision)" in rows[0]["content"]
    assert "output_preview=omitted" in rows[0]["content"]
    assert "login button is visible" not in rows[0]["content"]
    assert "image_cache" not in rows[0]["content"]
    assert "Image attached" not in rows[0]["content"]
    assert "inline image" not in rows[0]["content"].lower()


def test_session_end_tool_trace_filters_low_value_and_secret_outputs(provider, monkeypatch):
    monkeypatch.setattr(provider, "_run_session_end_journal_digest", lambda: None)

    provider.on_session_end(
        [
            {"role": "tool", "name": "todo", "content": '{"todos": [{"content": "temporary checklist"}]}'},
            {"role": "tool", "name": "terminal", "content": "api_key=sk-" + "a" * 30},
            {"role": "tool", "name": "terminal", "content": "Release gate output: 332 tests passed and wheel smoke passed."},
        ]
    )

    with provider._lock:
        rows = provider._require_conn().execute("SELECT role, content FROM journal_entries ORDER BY id").fetchall()
    assert len(rows) == 1
    assert rows[0]["role"] == "tool"
    assert rows[0]["content"].startswith("Tool execution summary (terminal):")
    assert "Release gate output" not in rows[0]["content"]
    assert "output_preview=omitted" in rows[0]["content"]


def test_sync_turn_rejects_context_handoff_payload_from_loaded_config(provider):
    provider.sync_turn(
        "## Active Task\n审计 LanceDB/vector 同步、重复与检索质量\n\n## Remaining Work\n进一步优化内容卫生处理",
        "Got it.",
    )
    provider.flush(timeout=2.0)

    with provider._lock:
        count = provider._require_conn().execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert count == 0


def test_prefetch_uses_current_turn_query_not_previous_prefetch(provider):
    provider._config["capture_raw_user"] = True

    provider.sync_turn(
        "We deploy services with uv run and restart the gateway after model changes.",
        "Got it.",
    )
    provider.flush(timeout=2.0)

    provider.on_turn_start(1, "How do we deploy services after model changes?")
    first = provider.prefetch("How do we deploy services after model changes?")
    assert "uv run" in first.lower()

    provider.queue_prefetch("How do we deploy services after model changes?")
    provider.on_turn_start(2, "你好")
    assert provider.prefetch("你好") == ""

    provider.on_turn_start(3, "Where can I buy groceries tonight?")
    assert provider.prefetch("Where can I buy groceries tonight?") == ""


def test_builtin_memory_write_round_trips_into_current_turn_recall(provider, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = MemoryStore()
    store.load_from_disk()
    payload = json.loads(
        memory_tool(
            action="add",
            target="user",
            content="Joy prefers concise answers with direct problem-first reporting.",
            store=store,
        )
    )
    assert payload["success"] is True

    provider.on_turn_start(1, "What response style does Joy prefer?")
    result = provider.prefetch("What response style does Joy prefer?")
    assert "concise answers" in result.lower()
    assert "problem-first" in result.lower()


def test_short_or_greeting_query_is_gated(provider):
    payload = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Joy prefers concise answers.", "target": "user"})
    )
    assert payload["stored"] is True

    provider.on_turn_start(1, "hi")
    assert provider.prefetch("hi") == ""

    provider.on_turn_start(2, "谢谢")
    assert provider.prefetch("谢谢") == ""


def test_conflicting_new_memory_records_review_relation_without_hiding_old(provider):
    old_payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {"content": "Project Phoenix deploy command is uv run old-deploy.", "target": "ops", "memory_type": "procedure"},
        )
    )
    assert old_payload["stored"] is True
    new_payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {
                "content": "Project Phoenix deploy command is not uv run old-deploy.",
                "target": "ops",
                "memory_type": "procedure",
            },
        )
    )
    assert new_payload["stored"] is True

    with provider._lock:
        old_row = provider._require_conn().execute("SELECT metadata FROM memories WHERE id = ?", (old_payload["id"],)).fetchone()
        new_row = provider._require_conn().execute("SELECT metadata FROM memories WHERE id = ?", (new_payload["id"],)).fetchone()
        contradicts = provider._require_conn().execute(
            "SELECT COUNT(*) FROM memory_relations WHERE relation_type = 'contradicts' AND ((source_memory_id = ? AND target_memory_id = ?) OR (source_memory_id = ? AND target_memory_id = ?))",
            (new_payload["id"], old_payload["id"], old_payload["id"], new_payload["id"]),
        ).fetchone()[0]
        supersedes = provider._require_conn().execute(
            "SELECT COUNT(*) FROM memory_relations WHERE relation_type IN ('supersedes', 'superseded_by') AND (source_memory_id IN (?, ?) OR target_memory_id IN (?, ?))",
            (new_payload["id"], old_payload["id"], new_payload["id"], old_payload["id"]),
        ).fetchone()[0]
    old_meta = json.loads(old_row["metadata"])
    new_meta = json.loads(new_row["metadata"])
    assert old_meta["lifecycle"] == "promoted"
    assert old_meta["needs_conflict_review"] is True
    assert old_meta["conflict_review_ids"] == [new_payload["id"]]
    assert new_meta["conflict_review_ids"] == [old_payload["id"]]
    assert contradicts == 2
    assert supersedes == 0

    results = json.loads(provider.handle_tool_call("scope_recall_search", {"query": "Project Phoenix deploy command", "limit": 5}))
    result_ids = [item["id"] for item in results["results"]]
    assert new_payload["id"] in result_ids
    assert old_payload["id"] in result_ids


def test_update_memory_rebuilds_conflict_review_relations(provider):
    old_payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {"content": "Project Lyra deploy command is uv run deploy-old.", "target": "ops", "memory_type": "procedure"},
        )
    )
    changed_payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {"content": "Project Lyra rollback window is 30 minutes.", "target": "ops", "memory_type": "procedure"},
        )
    )
    assert old_payload["stored"] is True
    assert changed_payload["stored"] is True

    updated = json.loads(
        provider.handle_tool_call(
            "scope_recall_update",
            {
                "id": changed_payload["id"],
                "content": "Project Lyra deploy command is not uv run deploy-old.",
                "target": "ops",
            },
        )
    )
    assert updated["updated"] is True

    with provider._lock:
        conn = provider._require_conn()
        relation_count = conn.execute(
            """
            SELECT COUNT(*) FROM memory_relations
            WHERE relation_type = 'contradicts'
              AND ((source_memory_id = ? AND target_memory_id = ?)
                OR (source_memory_id = ? AND target_memory_id = ?))
            """,
            (changed_payload["id"], old_payload["id"], old_payload["id"], changed_payload["id"]),
        ).fetchone()[0]
        old_meta = json.loads(conn.execute("SELECT metadata FROM memories WHERE id = ?", (old_payload["id"],)).fetchone()["metadata"])
        new_meta = json.loads(conn.execute("SELECT metadata FROM memories WHERE id = ?", (changed_payload["id"],)).fetchone()["metadata"])

    assert relation_count == 2
    assert old_meta["needs_conflict_review"] is True
    assert changed_payload["id"] in old_meta["conflict_review_ids"]
    assert old_payload["id"] in new_meta["conflict_review_ids"]


def test_update_memory_clears_stale_conflict_review_relations(provider):
    old_payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {"content": "Project Cygnus deploy command is uv run deploy-old.", "target": "ops", "memory_type": "procedure"},
        )
    )
    changed_payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {
                "content": "Project Cygnus deploy command is not uv run deploy-old.",
                "target": "ops",
                "memory_type": "procedure",
            },
        )
    )
    assert old_payload["stored"] is True
    assert changed_payload["stored"] is True

    updated = json.loads(
        provider.handle_tool_call(
            "scope_recall_update",
            {
                "id": changed_payload["id"],
                "content": "Project Cygnus rollback window is 30 minutes.",
                "target": "ops",
            },
        )
    )
    assert updated["updated"] is True

    with provider._lock:
        conn = provider._require_conn()
        relation_count = conn.execute(
            """
            SELECT COUNT(*) FROM memory_relations
            WHERE relation_type = 'contradicts'
              AND ((source_memory_id = ? AND target_memory_id = ?)
                OR (source_memory_id = ? AND target_memory_id = ?))
            """,
            (changed_payload["id"], old_payload["id"], old_payload["id"], changed_payload["id"]),
        ).fetchone()[0]
        old_meta = json.loads(conn.execute("SELECT metadata FROM memories WHERE id = ?", (old_payload["id"],)).fetchone()["metadata"])
        new_meta = json.loads(conn.execute("SELECT metadata FROM memories WHERE id = ?", (changed_payload["id"],)).fetchone()["metadata"])

    assert relation_count == 0
    assert changed_payload["id"] not in old_meta.get("conflict_review_ids", [])
    assert old_payload["id"] not in new_meta.get("conflict_review_ids", [])
    assert old_meta.get("needs_conflict_review") is not True
    assert new_meta.get("needs_conflict_review") is not True
    assert "contradicts" not in old_meta.get("relation_types", [])
    assert "contradicts" not in new_meta.get("relation_types", [])


def test_update_memory_preserves_feedback_metadata(provider):
    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {
                "content": "Project Vega release command is uv run release.",
                "target": "ops",
                "memory_type": "procedure",
                "importance": 0.9,
            },
        )
    )
    assert payload["stored"] is True
    feedback = json.loads(provider.handle_tool_call("scope_recall_feedback", {"id": payload["id"], "rating": "helpful"}))
    assert feedback["updated"] is True
    assert feedback["feedback_count"] == 1

    updated = json.loads(
        provider.handle_tool_call(
            "scope_recall_update",
            {
                "id": payload["id"],
                "content": "Project Vega release command is uv run release --prod.",
                "target": "ops",
            },
        )
    )
    assert updated["updated"] is True

    with provider._lock:
        row = provider._require_conn().execute("SELECT metadata FROM memories WHERE id = ?", (payload["id"],)).fetchone()
    metadata = json.loads(row["metadata"])
    assert metadata["feedback_count"] == 1
    assert metadata["helpful_count"] == 1
    assert metadata["trust"] == feedback["trust"]
    assert metadata["importance"] == 0.9
    assert metadata["memory_type"] == "procedure"


def test_governance_reports_dirty_history_candidates_without_mutating_lifecycle(provider):
    provider._config["maintenance_tools_enabled"] = True
    scratch_payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {"content": "Temporary scratch note from a local debugging turn that should stay reviewable, not core memory.", "target": "general"},
        )
    )
    old_payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {"content": "Project Orion deployment uses ./deploy-old.sh.", "target": "ops", "memory_type": "procedure"},
        )
    )
    new_payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {"content": "Project Orion deployment does not use ./deploy-old.sh.", "target": "ops", "memory_type": "procedure"},
        )
    )
    assert scratch_payload["stored"] is True
    assert old_payload["stored"] is True
    assert new_payload["stored"] is True

    payload = json.loads(provider.handle_tool_call("scope_recall_govern", {"dry_run": True, "scope_only": True}))
    candidates = {item["id"]: item for item in payload["review_candidates"]}
    assert scratch_payload["id"] in candidates
    assert "local-scratch" in candidates[scratch_payload["id"]]["reasons"]
    assert old_payload["id"] in candidates
    assert "conflict-review" in candidates[old_payload["id"]]["reasons"]

    with provider._lock:
        old_meta = json.loads(provider._require_conn().execute("SELECT metadata FROM memories WHERE id = ?", (old_payload["id"],)).fetchone()["metadata"])
    assert old_meta["lifecycle"] == "promoted"
    assert old_meta["needs_conflict_review"] is True


def test_dedup_prevents_repeat_injection_within_min_repeated(provider):
    payload = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "The deploy command is uv run app.", "target": "memory"})
    )
    assert payload["stored"] is True

    provider.on_turn_start(1, "What is the deploy command for this service?")
    first = provider.prefetch("What is the deploy command for this service?")
    assert "uv run app" in first.lower()

    provider.on_turn_start(2, "What is the deploy command for this service?")
    second = provider.prefetch("What is the deploy command for this service?")
    assert second == ""

    provider.on_turn_start(9, "What is the deploy command for this service?")
    third = provider.prefetch("What is the deploy command for this service?")
    assert "uv run app" in third.lower()


def test_maintenance_tool_schemas_preload_runtime_config_before_initialize(tmp_path, monkeypatch):
    _write_scope_recall_config(tmp_path, {"maintenance_tools_enabled": True, "vector": {"enabled": False}})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from scope_recall.provider import ScopeRecallMemoryProvider

    plugin = ScopeRecallMemoryProvider()
    names = {schema["name"] for schema in plugin.get_tool_schemas()}

    assert "scope_recall_dedupe" in names
    assert "scope_recall_govern" in names
    assert "scope_recall_repair" in names
    assert "scope_recall_hygiene" in names


def test_cross_platform_identity_mapping_is_opt_in_and_default_keeps_platform_isolation(tmp_path):
    _write_scope_recall_config(tmp_path, {"vector": {"enabled": False}})
    telegram = load_memory_provider("scope-recall")
    cli = load_memory_provider("scope-recall")
    assert telegram is not None
    assert cli is not None
    telegram.initialize(
        "telegram-session",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="9000000001",
        chat_id="chat-a",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    cli.initialize(
        "cli-session",
        hermes_home=str(tmp_path),
        platform="cli",
        user_id="9000000001",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        assert telegram._shared_scope_id != cli._shared_scope_id
        payload = json.loads(
            telegram.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Project Atlas default isolation memory should not cross platform without opt-in.",
                    "target": "project",
                    "memory_type": "project",
                },
            )
        )
        assert payload["stored"] is True
        results = json.loads(cli.handle_tool_call("scope_recall_search", {"query": "default isolation memory cross platform opt-in", "limit": 5}))
        assert payload["id"] not in {item["id"] for item in results["results"]}
    finally:
        telegram.shutdown()
        cli.shutdown()


def test_cross_platform_identity_mapping_unmapped_accounts_remain_isolated(tmp_path):
    _write_scope_recall_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "identity": {
                "cross_platform_shared_scope": True,
                "cli_user_id_fallback": "local",
                "user_aliases": {"telegram:9000000001": "joy"},
            },
        },
    )
    telegram = load_memory_provider("scope-recall")
    cli = load_memory_provider("scope-recall")
    assert telegram is not None
    assert cli is not None
    telegram.initialize(
        "telegram-session",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="9000000001",
        chat_id="chat-a",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    cli.initialize(
        "cli-session",
        hermes_home=str(tmp_path),
        platform="cli",
        user_id="",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        assert telegram._shared_scope_id != cli._shared_scope_id
        payload = json.loads(
            telegram.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Project Atlas mapped telegram row should not leak to an unmapped CLI account.",
                    "target": "project",
                    "memory_type": "project",
                },
            )
        )
        assert payload["stored"] is True
        results = json.loads(cli.handle_tool_call("scope_recall_search", {"query": "unmapped CLI account Project Atlas leak", "limit": 5}))
        assert payload["id"] not in {item["id"] for item in results["results"]}
    finally:
        telegram.shutdown()
        cli.shutdown()


def test_cross_platform_identity_mapping_reads_legacy_platform_shared_rows(tmp_path):
    _write_scope_recall_config(tmp_path, {"vector": {"enabled": False}})
    telegram = load_memory_provider("scope-recall")
    assert telegram is not None
    telegram.initialize(
        "telegram-session",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="9000000001",
        chat_id="chat-a",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        legacy = json.loads(
            telegram.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Project Atlas legacy platform shared row remains readable after identity mapping.",
                    "target": "project",
                    "memory_type": "project",
                },
            )
        )
        assert legacy["stored"] is True
        legacy_shared_scope = telegram._shared_scope_id
    finally:
        telegram.shutdown()

    _write_scope_recall_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "identity": {
                "cross_platform_shared_scope": True,
                "cli_user_id_fallback": "local",
                "user_aliases": {"telegram:9000000001": "joy", "cli:local": "joy"},
            },
        },
    )
    cli = load_memory_provider("scope-recall")
    assert cli is not None
    cli.initialize(
        "cli-session",
        hermes_home=str(tmp_path),
        platform="cli",
        user_id="",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        assert legacy_shared_scope in cli._accessible_scope_ids
        results = json.loads(cli.handle_tool_call("scope_recall_search", {"query": "legacy platform shared row identity mapping", "limit": 5}))
        assert legacy["id"] in {item["id"] for item in results["results"]}
    finally:
        cli.shutdown()


def test_cross_platform_identity_mapping_shares_durable_memory_but_not_local_scratch(tmp_path):
    _write_scope_recall_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "identity": {
                "cross_platform_shared_scope": True,
                "cli_user_id_fallback": "local",
                "user_aliases": {
                    "telegram:9000000001": "joy",
                    "cli:local": "joy",
                    "feishu:ou_xxx": "joy",
                },
            },
        },
    )
    telegram = load_memory_provider("scope-recall")
    cli = load_memory_provider("scope-recall")
    assert telegram is not None
    assert cli is not None
    telegram.initialize(
        "telegram-session",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="9000000001",
        chat_id="chat-a",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    cli.initialize(
        "cli-session",
        hermes_home=str(tmp_path),
        platform="cli",
        user_id="",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        assert telegram._shared_scope_id == cli._shared_scope_id
        assert telegram._scope_id != cli._scope_id
        assert cli._scope.user_id == "local"

        durable = json.loads(
            telegram.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Project Atlas cross-platform durable memory uses the Rust pipeline.",
                    "target": "project",
                    "memory_type": "project",
                },
            )
        )
        scratch = json.loads(
            telegram.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Telegram-only scratch note says Project Atlas temporary codename is Glass Sparrow.",
                    "target": "general",
                    "memory_type": "episodic",
                },
            )
        )
        assert durable["stored"] is True
        assert scratch["stored"] is True
        with telegram._lock:
            metadata = json.loads(
                telegram._require_conn().execute("SELECT metadata FROM memories WHERE id = ?", (durable["id"],)).fetchone()["metadata"]
            )
        assert metadata["canonical_user"] == "joy"
        assert metadata["raw_platform"] == "telegram"
        assert metadata["raw_user_id"] == "9000000001"
        durable_results = json.loads(cli.handle_tool_call("scope_recall_search", {"query": "Project Atlas Rust pipeline", "limit": 5}))
        durable_ids = {item["id"] for item in durable_results["results"]}
        assert durable["id"] in durable_ids
        scratch_results = json.loads(cli.handle_tool_call("scope_recall_search", {"query": "Glass Sparrow temporary codename", "limit": 5}))
        scratch_ids = {item["id"] for item in scratch_results["results"]}
        assert scratch["id"] not in scratch_ids
    finally:
        telegram.shutdown()
        cli.shutdown()


def test_maintenance_tool_schemas_require_operator_config(provider):
    schemas = provider.get_tool_schemas()
    names = {schema["name"] for schema in schemas}
    assert {"scope_recall_context", "scope_recall_memory", "scope_recall_entity"} <= names
    assert "scope_recall_probe" not in names
    assert "scope_recall_related" not in names
    assert "scope_recall_feedback" not in names
    assert "scope_recall_store_secret_index" not in names
    assert "scope_recall_dedupe" not in names
    assert "scope_recall_govern" not in names
    assert "scope_recall_repair" not in names
    assert "scope_recall_export" not in names
    assert "scope_recall_stats" not in names
    store_schema = next(schema for schema in schemas if schema["name"] == "scope_recall_store")
    memory_types = set(store_schema["parameters"]["properties"]["memory_type"]["enum"])
    assert {"workflow", "tool_trace", "summary", "pitfall", "decision"} <= memory_types

    provider._config["maintenance_tools_enabled"] = True
    operator_names = {schema["name"] for schema in provider.get_tool_schemas()}
    assert "scope_recall_dedupe" in operator_names
    assert "scope_recall_govern" in operator_names
    assert "scope_recall_repair" in operator_names

    provider._config["secret_index_tools_enabled"] = True
    secret_names = {schema["name"] for schema in provider.get_tool_schemas()}
    assert "scope_recall_store_secret_index" in secret_names


def test_tool_store_enriches_external_artifact_anchors(provider):
    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {
                "content": "Hermes 官方推荐申请见 https://github.com/NousResearch/hermes-agent/issues/42864，后续要查状态和评论。",
                "target": "project",
                "memory_type": "resource",
            },
        )
    )
    assert payload["stored"] is True

    row = provider._require_conn().execute("SELECT content, metadata FROM memories WHERE id = ?", (payload["id"],)).fetchone()
    assert row is not None
    content = row["content"]
    assert "Artifact anchors:" in content
    assert "NousResearch/hermes-agent#42864" in content
    assert "https://github.com/NousResearch/hermes-agent/issues/42864" in content
    metadata = json.loads(row["metadata"])
    assert metadata["artifacts"][0]["kind"] == "github_issue"
    assert metadata["artifacts"][0]["repo"] == "NousResearch/hermes-agent"
    assert metadata["artifacts"][0]["number"] == 42864

    results = json.loads(provider.handle_tool_call("scope_recall_search", {"query": "#42864 hermes-agent 官方推荐", "limit": 3}))
    assert any(item["id"] == payload["id"] for item in results["results"])


def test_secret_index_tool_stores_vault_ref_without_plaintext_secret(provider):
    provider._config["secret_index_tools_enabled"] = True
    secret_value = "correct-horse-battery-staple-12345"
    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_store_secret_index",
            {
                "label": "LA proxy admin password",
                "secret_type": "password",
                "service": "la-proxy",
                "account": "root",
                "vault_ref": "vault://ops/la-proxy/root-password",
                "secret_value": secret_value,
                "notes": "用于服务器维护，回复时必须脱敏。",
                "tags": ["ops", "credential"],
            },
        )
    )
    assert payload["stored"] is True
    assert payload["secret_value_stored"] is False

    row = provider._require_conn().execute("SELECT content, metadata FROM memories WHERE id = ?", (payload["id"],)).fetchone()
    assert row is not None
    assert secret_value not in row["content"]
    assert "vault://ops/la-proxy/root-password" in row["content"]
    assert "LA proxy admin password" in row["content"]
    metadata = json.loads(row["metadata"])
    assert metadata["sensitivity"] == "secret_reference"
    assert metadata["secret_storage"] == "external-vault-reference"
    assert metadata["secret_value_stored"] is False
    assert metadata["secret_value_sha256_prefix"]

    exported = json.loads(provider.handle_tool_call("scope_recall_export", {"format": "json", "scope_only": True}))
    assert secret_value not in json.dumps(exported, ensure_ascii=False)


def test_secret_index_allows_credential_label_with_api_key_kind(provider):
    provider._config["secret_index_tools_enabled"] = True
    secret_value = "scope-recall-smoke-secret-should-not-persist-12345"
    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_store_secret_index",
            {
                "label": "Scope Recall smoke dummy credential",
                "secret_type": "api_key",
                "service": "scope-recall-smoke",
                "account": "joy-smoke",
                "vault_ref": "vault://smoke/scope-recall/dummy",
                "secret_value": secret_value,
                "notes": "dummy credential for pre-push smoke test; not real",
                "tags": ["smoke", "credential"],
            },
        )
    )
    assert payload["stored"] is True
    assert payload["secret_value_stored"] is False

    row = provider._require_conn().execute("SELECT content, metadata FROM memories WHERE id = ?", (payload["id"],)).fetchone()
    assert row is not None
    assert "Kind: api_key" in row["content"]
    assert secret_value not in row["content"]
    exported = json.loads(provider.handle_tool_call("scope_recall_export", {"format": "json", "scope_only": True}))
    assert secret_value not in json.dumps(exported, ensure_ascii=False)


def test_scope_isolation_uses_user_and_profile(tmp_path):
    p1 = load_memory_provider("scope-recall")
    p2 = load_memory_provider("scope-recall")
    assert p1 is not None and p2 is not None

    p1.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    p2.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="other-user",
    )

    try:
        payload = json.loads(
            p1.handle_tool_call("scope_recall_store", {"content": "Joy likes concise replies.", "target": "user"})
        )
        assert payload["stored"] is True

        p2.on_turn_start(1, "What style does the user like?")
        assert p2.prefetch("What style does the user like?") == ""

        p1.on_turn_start(1, "What style does the user like?")
        assert "concise replies" in p1.prefetch("What style does the user like?").lower()
    finally:
        p1.shutdown()
        p2.shutdown()


def test_permanent_user_memory_crosses_chat_id(tmp_path):
    p1 = load_memory_provider("scope-recall")
    p2 = load_memory_provider("scope-recall")
    assert p1 is not None and p2 is not None

    p1.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-1",
    )
    p2.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-2",
    )

    try:
        payload = json.loads(
            p1.handle_tool_call("scope_recall_store", {"content": "Joy likes concise replies.", "target": "user"})
        )
        assert payload["stored"] is True

        p2.on_turn_start(1, "What style does Joy like?")
        assert "concise replies" in p2.prefetch("What style does Joy like?").lower()

        p1.on_turn_start(1, "What style does Joy like?")
        assert "concise replies" in p1.prefetch("What style does Joy like?").lower()
    finally:
        p1.shutdown()
        p2.shutdown()


def test_permanent_user_memory_crosses_thread_id(tmp_path):
    p1 = load_memory_provider("scope-recall")
    p2 = load_memory_provider("scope-recall")
    assert p1 is not None and p2 is not None

    p1.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-1",
        thread_id="topic-a",
    )
    p2.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-1",
        thread_id="topic-b",
    )

    try:
        payload = json.loads(
            p1.handle_tool_call("scope_recall_store", {"content": "Joy likes concise replies.", "target": "user"})
        )
        assert payload["stored"] is True

        p2.on_turn_start(1, "What style does Joy like?")
        assert "concise replies" in p2.prefetch("What style does Joy like?").lower()

        p1.on_turn_start(1, "What style does Joy like?")
        assert "concise replies" in p1.prefetch("What style does Joy like?").lower()
    finally:
        p1.shutdown()
        p2.shutdown()


def test_permanent_user_memory_crosses_gateway_session_key(tmp_path):
    p1 = load_memory_provider("scope-recall")
    p2 = load_memory_provider("scope-recall")
    assert p1 is not None and p2 is not None

    p1.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        gateway_session_key="telegram:group-1:topic-a",
    )
    p2.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        gateway_session_key="telegram:group-2:topic-a",
    )

    try:
        payload = json.loads(
            p1.handle_tool_call("scope_recall_store", {"content": "Joy likes concise replies.", "target": "user"})
        )
        assert payload["stored"] is True

        p2.on_turn_start(1, "What style does Joy like?")
        assert "concise replies" in p2.prefetch("What style does Joy like?").lower()

        p1.on_turn_start(1, "What style does Joy like?")
        assert "concise replies" in p1.prefetch("What style does Joy like?").lower()
    finally:
        p1.shutdown()
        p2.shutdown()



def test_local_general_memory_stays_in_chat_scope(tmp_path):
    p1 = load_memory_provider("scope-recall")
    p2 = load_memory_provider("scope-recall")
    assert p1 is not None and p2 is not None

    p1.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-1",
    )
    p2.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-2",
    )

    try:
        payload = json.loads(
            p1.handle_tool_call("scope_recall_store", {"content": "Group one temporary scratch note.", "target": "general"})
        )
        assert payload["stored"] is True
        assert payload["scope_mode"] == "local"

        p2.on_turn_start(1, "What was the group one temporary scratch note?")
        assert p2.prefetch("What was the group one temporary scratch note?") == ""

        p1.on_turn_start(1, "What was the group one temporary scratch note?")
        assert "temporary scratch note" in p1.prefetch("What was the group one temporary scratch note?").lower()
    finally:
        p1.shutdown()
        p2.shutdown()


def test_durable_shared_memory_is_visible_but_not_to_another_user(tmp_path):
    owner = load_memory_provider("scope-recall")
    other = load_memory_provider("scope-recall")
    assert owner is not None and other is not None

    owner.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-1",
    )
    other.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="other-user",
        chat_id="group-2",
    )

    try:
        stored = json.loads(owner.handle_tool_call("scope_recall_store", {"content": "Joy project codename is Star Compass.", "target": "project"}))
        assert stored["stored"] is True
        assert stored["scope_mode"] == "shared"

        other.on_turn_start(1, "What is Joy project codename?")
        assert other.prefetch("What is Joy project codename?") == ""

        blocked = json.loads(other.handle_tool_call("scope_recall_update", {"id": stored["id"], "content": "Other user overwrite attempt."}))
        assert blocked["error"]
        row = owner._require_conn().execute("SELECT content FROM memories WHERE id = ?", (stored["id"],)).fetchone()
        assert row is not None
        assert row["content"] == "Joy project codename is Star Compass."
    finally:
        owner.shutdown()
        other.shutdown()


def test_durable_shared_memory_is_not_visible_to_sibling_agent_identity(tmp_path):
    yuheng = load_memory_provider("scope-recall")
    tianshu = load_memory_provider("scope-recall")
    assert yuheng is not None and tianshu is not None

    yuheng.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-1",
    )
    tianshu.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="tianshu",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-2",
    )

    try:
        stored = json.loads(yuheng.handle_tool_call("scope_recall_store", {"content": "Yuheng-only durable note is blue comet.", "target": "memory"}))
        assert stored["stored"] is True
        assert stored["scope_mode"] == "shared"

        tianshu.on_turn_start(1, "What is the Yuheng-only durable note?")
        assert tianshu.prefetch("What is the Yuheng-only durable note?") == ""

        blocked = json.loads(tianshu.handle_tool_call("scope_recall_update", {"id": stored["id"], "content": "Sibling overwrite attempt."}))
        assert blocked["error"]
        row = yuheng._require_conn().execute("SELECT content FROM memories WHERE id = ?", (stored["id"],)).fetchone()
        assert row is not None
        assert row["content"] == "Yuheng-only durable note is blue comet."
    finally:
        yuheng.shutdown()
        tianshu.shutdown()

def test_scope_id_serialization_cannot_collide_via_raw_delimiters():
    embedded_delimiter = RuntimeScope(
        platform="telegram",
        agent_workspace="hermes",
        agent_identity="yuheng",
        user_id="joy|chat:group-1",
    )
    structured_chat = RuntimeScope(
        platform="telegram",
        agent_workspace="hermes",
        agent_identity="yuheng",
        user_id="joy",
        chat_id="group-1",
    )

    assert build_scope_id(embedded_delimiter) != build_scope_id(structured_chat)


def test_budget_caps_number_of_lines(provider):
    for i in range(6):
        payload = json.loads(
            provider.handle_tool_call(
                "scope_recall_store",
                {"content": f"Deploy note {i}: deploy with uv run app and restart gateway step {i}.", "target": "memory"},
            )
        )
        assert payload["stored"] is True

    provider.on_turn_start(1, "How do we deploy the service and restart the gateway?")
    result = provider.prefetch("How do we deploy the service and restart the gateway?")
    recalled_items = json.loads(result.splitlines()[-1])
    assert 1 <= len(recalled_items) <= 3


def test_search_tool_returns_ranked_results(provider):
    payload = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Use uv run app for deploys.", "target": "memory"})
    )
    assert payload["stored"] is True

    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_search",
            {"query": "What command do we use for deploys?", "limit": 3},
        )
    )
    assert payload["count"] >= 1
    assert payload["results"][0]["content"].lower().startswith("use uv run app")
    assert "base_score" in payload["results"][0]
    assert "recency_bonus" in payload["results"][0]


def test_graph_context_and_feedback_tools(provider):
    first = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {
                "content": "Yuheng owns the Scope Recall architecture for Hermes.",
                "target": "project",
                "memory_type": "project",
                "importance": 0.9,
                "entities": ["Yuheng", "Scope Recall", "Hermes"],
                "tags": ["architecture"],
            },
        )
    )
    second = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {
                "content": "Scope Recall deploy command uses uv run app.",
                "target": "ops",
                "memory_type": "procedure",
                "entities": ["Scope Recall", "Hermes"],
            },
        )
    )
    assert first["stored"] is True
    assert second["stored"] is True

    probe = json.loads(provider.handle_tool_call("scope_recall_probe", {"entity": "Scope Recall", "limit": 5}))
    assert probe["entity"] == "scope recall"
    assert probe["count"] == 2
    assert {item["id"] for item in probe["results"]} == {first["id"], second["id"]}

    related = json.loads(provider.handle_tool_call("scope_recall_related", {"entity": "Scope Recall", "limit": 5}))
    related_entities = {item["entity"] for item in related["related"]}
    assert {"hermes", "yuheng"} <= related_entities

    context = json.loads(
        provider.handle_tool_call(
            "scope_recall_context",
            {"query": "How does Yuheng deploy Scope Recall?", "limit": 5, "max_chars": 600},
        )
    )
    assert context["count"] >= 2
    assert "Scope Recall" in context["context"]

    feedback = json.loads(provider.handle_tool_call("scope_recall_feedback", {"id": first["id"], "rating": "helpful"}))
    assert feedback["updated"] is True
    assert feedback["trust"] > 0.5

    search = json.loads(provider.handle_tool_call("scope_recall_search", {"query": "Yuheng Scope Recall architecture", "limit": 5}))
    hit = next(item for item in search["results"] if item["id"] == first["id"])
    assert hit["memory_type"] == "project"
    assert "scope recall" in hit["entities"]
    assert hit["trust"] == feedback["trust"]

    stats = json.loads(provider.handle_tool_call("scope_recall_stats", {}))
    assert stats["scope_entities"] >= 3
    assert stats["scope_feedback_rows"] == 1


def test_replace_in_curated_memory_is_reflected_without_stale_old_entry(provider, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = MemoryStore()
    store.load_from_disk()
    assert json.loads(
        memory_tool(
            action="add",
            target="user",
            content="Joy likes concise replies.",
            store=store,
        )
    )["success"] is True
    assert json.loads(
        memory_tool(
            action="replace",
            target="user",
            old_text="Joy likes concise replies.",
            content="Joy likes concise but warm replies.",
            store=store,
        )
    )["success"] is True

    provider.on_turn_start(1, "What style does Joy like?")
    result = provider.prefetch("What style does Joy like?")
    assert "warm replies" in result.lower()
    assert "joy likes concise replies." not in result.lower()


def test_remove_from_curated_memory_is_reflected(provider, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = MemoryStore()
    store.load_from_disk()
    assert json.loads(
        memory_tool(
            action="add",
            target="user",
            content="Joy likes concise replies.",
            store=store,
        )
    )["success"] is True
    assert json.loads(
        memory_tool(
            action="remove",
            target="user",
            old_text="Joy likes concise replies.",
            store=store,
        )
    )["success"] is True

    provider.on_turn_start(1, "What style does Joy like?")
    assert provider.prefetch("What style does Joy like?") == ""


def test_freshness_companion_failure_is_observable_and_rolls_back_memory(provider, monkeypatch):
    def _fail_freshness(*args, **kwargs):
        raise RuntimeError("synthetic freshness companion failure")

    package = provider.__class__.__module__.rsplit(".", 1)[0]
    sql_store_module = importlib.import_module(f"{package}.sql_store")
    monkeypatch.setattr(
        sql_store_module,
        "upsert_memory_freshness",
        _fail_freshness,
    )
    before = json.loads(provider.handle_tool_call("scope_recall_stats", {}))[
        "total_memories"
    ]
    stored = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {
                "content": "Freshness failure observability sentinel is a factual operational state.",
                "target": "ops",
                "memory_type": "factual",
            },
        )
    )
    after = json.loads(provider.handle_tool_call("scope_recall_stats", {}))[
        "total_memories"
    ]

    assert stored.get("stored") is not True
    assert "freshness" in str(stored).lower()
    assert after == before


def test_on_memory_write_is_observational_noop(provider):
    before_stats = json.loads(provider.handle_tool_call("scope_recall_stats", {}))
    provider.on_memory_write("add", "memory", "Curated memory is read live, not mirrored into SQLite.")
    after_stats = json.loads(provider.handle_tool_call("scope_recall_stats", {}))

    assert after_stats["total_memories"] == before_stats["total_memories"]
    provider.on_turn_start(1, "Is curated memory mirrored into SQLite?")
    assert provider.prefetch("Is curated memory mirrored into SQLite?") == ""



def test_reads_builtin_curated_memory_files(tmp_path):
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir(parents=True, exist_ok=True)
    (memories_dir / "USER.md").write_text(
        "Joy prefers concise answers with direct problem-first reporting.\n",
        encoding="utf-8",
    )

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-curated",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    plugin._config["curated_memory"] = {"mode": "explicit-users", "allowed_user_ids": ["joy"]}
    try:
        plugin.on_turn_start(1, "What response style does Joy prefer?")
        result = plugin.prefetch("What response style does Joy prefer?")
        assert "problem-first" in result.lower()
        stats = json.loads(plugin.handle_tool_call("scope_recall_stats", {}))
        assert stats["curated_memories"] >= 1
    finally:
        plugin.shutdown()


def test_subagent_context_cannot_expose_or_use_tools(tmp_path):
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-sub",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="subagent",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    try:
        assert plugin.get_tool_schemas() == []
        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "subagent should not write this", "target": "memory"},
            )
        )
        assert payload["error"]
    finally:
        plugin.shutdown()


def test_enable_tools_false_hides_and_blocks_tool_execution(tmp_path):
    _write_scope_recall_config(tmp_path, {"enable_tools": False, "vector": {"enabled": False}})
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-tools-disabled",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    try:
        assert plugin.get_tool_schemas() == []
        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "disabled tools must not persist this", "target": "memory"},
            )
        )
        assert payload["error"]
        with plugin._lock:
            assert plugin._require_conn().execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    finally:
        plugin.shutdown()


def test_hybrid_recall_uses_vector_companion_for_semantic_match(provider):
    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {"content": "Deploy services with uv run app.", "target": "memory"},
        )
    )
    assert payload["stored"] is True
    provider.flush(timeout=5.0)

    provider.on_turn_start(1, "What is our rollout command for production releases?")
    result = provider.prefetch("What is our rollout command for production releases?")
    assert "uv run app" in result.lower()


def test_stats_report_vector_state(provider):
    payload = json.loads(provider.handle_tool_call("scope_recall_stats", {}))
    assert payload["provider"] == "scope-recall"
    assert payload["vector"]["enabled"] is True
    assert payload["vector"]["backend"] in {"lancedb", "sqlite-bruteforce"}
    assert payload["vector"]["ready"] is True
    if payload["vector"]["backend"] == "sqlite-bruteforce":
        assert "fallback" in payload["vector"]["message"].lower()


def test_lexical_recall_matches_response_style_aliases(tmp_path):
    config_path = tmp_path / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "retrieval": {"mode": "lexical", "min_score": 0.18},
                "vector": {"enabled": False},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-lexical-style",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    try:
        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Joy likes warm concise replies.", "target": "user"},
            )
        )
        assert payload["stored"] is True
        plugin.flush(timeout=5.0)

        plugin.on_turn_start(1, "What response style should I use?")
        result = plugin.prefetch("What response style should I use?")
        assert "warm concise replies" in result.lower()
    finally:
        plugin.shutdown()



def test_lexical_recall_matches_prod_rollout_aliases(tmp_path):
    config_path = tmp_path / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "retrieval": {"mode": "lexical", "min_score": 0.18},
                "vector": {"enabled": False},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-lexical-deploy",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    try:
        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Production rollout uses uv run app.", "target": "memory"},
            )
        )
        assert payload["stored"] is True
        plugin.flush(timeout=5.0)

        plugin.on_turn_start(1, "How do we deploy prod?")
        result = plugin.prefetch("How do we deploy prod?")
        assert "uv run app" in result.lower()
    finally:
        plugin.shutdown()



def test_recency_aware_ranking_prefers_newer_memory_for_current_queries(tmp_path):
    config_path = tmp_path / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "retrieval": {"mode": "lexical", "min_score": 0.18},
                "vector": {"enabled": False},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-recency-current",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    try:
        old_payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Current production deploy command is uv run app.", "target": "memory"},
            )
        )
        new_payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Current production deploy command is uv run service.", "target": "memory"},
            )
        )
        conn = plugin._require_conn()
        with plugin._lock:
            conn.execute(
                "UPDATE memories SET updated_at = ? WHERE id = ?",
                ("2026-01-01T00:00:00+00:00", old_payload["id"]),
            )
            conn.execute(
                "UPDATE memories SET updated_at = ? WHERE id = ?",
                ("2026-05-01T00:00:00+00:00", new_payload["id"]),
            )
            conn.commit()

        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_search",
                {"query": "What is the current production deploy command?", "limit": 3},
            )
        )
        assert payload["results"][0]["content"] == "Current production deploy command is uv run service."
    finally:
        plugin.shutdown()



def test_recency_aware_ranking_keeps_clearer_match_without_freshness_hint(tmp_path):
    config_path = tmp_path / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "retrieval": {"mode": "lexical", "min_score": 0.18},
                "vector": {"enabled": False},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-recency-nonfresh",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    try:
        old_payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Current production deploy uses uv run app.", "target": "memory"},
            )
        )
        new_payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Production command is uv run service now.", "target": "memory"},
            )
        )
        conn = plugin._require_conn()
        with plugin._lock:
            conn.execute(
                "UPDATE memories SET updated_at = ? WHERE id = ?",
                ("2026-01-01T00:00:00+00:00", old_payload["id"]),
            )
            conn.execute(
                "UPDATE memories SET updated_at = ? WHERE id = ?",
                ("2026-05-01T00:00:00+00:00", new_payload["id"]),
            )
            conn.commit()

        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_search",
                {"query": "How do we deploy prod?", "limit": 3},
            )
        )
        assert payload["results"][0]["content"] == "Current production deploy uses uv run app."
    finally:
        plugin.shutdown()



def test_legacy_tool_aliases_still_work(provider):
    stored = json.loads(provider.handle_tool_call("lancepro_store", {"content": "Use uv run app.", "target": "memory"}))
    assert stored["stored"] is True

    searched = json.loads(provider.handle_tool_call("lancepro_search", {"query": "uv run app", "limit": 3}))
    assert searched["count"] >= 1

    stats = json.loads(provider.handle_tool_call("lancepro_stats", {}))
    assert stats["provider"] == "scope-recall"

def test_store_skips_exact_duplicate_content_in_same_scope(provider):
    first = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Joy prefers concise direct replies.", "target": "user"})
    )
    second = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "  Joy prefers concise direct replies.  ", "target": "user"})
    )

    assert first["stored"] is True
    assert second["stored"] is False
    assert second["duplicate"] is True
    assert second["id"] == first["id"]
    stats = json.loads(provider.handle_tool_call("scope_recall_stats", {}))
    assert stats["total_memories"] == 1


def test_auto_capture_filters_system_maintenance_prompts(provider):
    noisy_prompt = "Review the conversation above and update the skill library. Be ACTIVE — most sessions produce at least one skill update."
    provider.sync_turn(noisy_prompt, "OK")
    provider.flush(timeout=2.0)

    stats = json.loads(provider.handle_tool_call("scope_recall_stats", {}))
    assert stats["total_memories"] == 0


def test_forget_archive_reports_retained_and_reversible(provider):
    payload = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Temporary deploy note should be removed.", "target": "memory"})
    )
    assert payload["stored"] is True
    provider.flush(timeout=5.0)

    result = json.loads(provider.handle_tool_call("scope_recall_forget", {"ids": [payload["id"]]}))
    assert result["archived"] == 1
    assert result["deleted"] == 0
    assert payload["id"] in result["ids"]
    assert result["mode"] == "archive"
    assert result["data_retained"] is True
    assert result["reversible"] is True
    assert result["privacy_purge"] is False
    assert result["mutation_applied"] is True

    provider.on_turn_start(1, "Temporary deploy note")
    assert provider.prefetch("Temporary deploy note") == ""


def test_update_tool_replaces_memory_and_old_fts_is_removed(provider):
    payload = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Production deploy uses uv run old.", "target": "memory"})
    )
    updated = json.loads(
        provider.handle_tool_call(
            "scope_recall_update",
            {"id": payload["id"], "content": "Production deploy uses uv run new.", "target": "ops"},
        )
    )

    assert updated["updated"] is True
    assert updated["id"] == payload["id"]
    old_results = json.loads(provider.handle_tool_call("scope_recall_search", {"query": "uv run old", "limit": 3}))
    assert all("uv run old" not in item["content"].lower() for item in old_results["results"])
    new_results = json.loads(provider.handle_tool_call("scope_recall_search", {"query": "uv run new", "limit": 3}))
    assert new_results["results"][0]["content"] == "Production deploy uses uv run new."
    assert new_results["results"][0]["target"] == "ops"


def test_update_tool_cannot_modify_local_memory_from_another_scope(tmp_path):
    p1 = load_memory_provider("scope-recall")
    p2 = load_memory_provider("scope-recall")
    assert p1 is not None and p2 is not None

    p1.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-1",
    )
    p2.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-2",
    )

    try:
        stored = json.loads(p1.handle_tool_call("scope_recall_store", {"content": "Group one private note.", "target": "general"}))
        blocked = json.loads(
            p2.handle_tool_call("scope_recall_update", {"id": stored["id"], "content": "Group two overwrite attempt."})
        )

        assert blocked["error"]
        row = p1._require_conn().execute("SELECT content FROM memories WHERE id = ?", (stored["id"],)).fetchone()
        assert row is not None
        assert row["content"] == "Group one private note."
    finally:
        p1.shutdown()
        p2.shutdown()



def test_dedupe_tool_dry_run_and_apply_collapses_existing_duplicates(provider):
    first = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Duplicate note for cleanup.", "target": "memory"})
    )
    assert first["stored"] is True
    # Simulate legacy duplicate row bypassing the new write-time dedupe guard.
    with provider._lock:
        provider._store_now(content="Duplicate note for cleanup.", source="legacy-import", target="memory", session_id="legacy", semantic_merge=False)

    blocked = json.loads(provider.handle_tool_call("scope_recall_dedupe", {"dry_run": True}))
    assert blocked["error"]

    provider._config["maintenance_tools_enabled"] = True
    dry = json.loads(provider.handle_tool_call("scope_recall_dedupe", {"dry_run": True}))
    assert dry["duplicate_groups"] == 1
    assert dry["duplicates"] == 1
    assert dry["scope_only"] is True

    applied = json.loads(provider.handle_tool_call("scope_recall_dedupe", {"dry_run": False}))
    assert applied["deleted"] == 1
    stats = json.loads(provider.handle_tool_call("scope_recall_stats", {}))
    assert stats["total_memories"] == 1


def test_dedupe_scope_only_false_requires_operator_mode(tmp_path):
    p1 = load_memory_provider("scope-recall")
    p2 = load_memory_provider("scope-recall")
    assert p1 is not None and p2 is not None

    p1.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-1",
    )
    p2.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-2",
    )

    try:
        for plugin in (p1, p2):
            plugin._store_now(
                content="Duplicate note across scopes.",
                source="legacy-import",
                target="general",
                session_id="legacy",
                semantic_merge=False,
                allow_duplicate=True,
            )
            plugin._store_now(
                content="Duplicate note across scopes.",
                source="legacy-import",
                target="general",
                session_id="legacy",
                semantic_merge=False,
                allow_duplicate=True,
            )

        dry = json.loads(p1.handle_tool_call("scope_recall_dedupe", {"dry_run": True, "scope_only": False}))
        assert dry["error"]

        applied = json.loads(p1.handle_tool_call("scope_recall_dedupe", {"dry_run": False, "scope_only": False}))
        assert applied["error"]
        stats = json.loads(p1.handle_tool_call("scope_recall_stats", {}))
        assert stats["total_memories"] == 4
        assert stats["scope_memories"] == 2
    finally:
        p1.shutdown()
        p2.shutdown()


def test_dedupe_scope_only_false_allowed_only_with_operator_config(tmp_path, monkeypatch):
    monkeypatch.delenv("SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY", raising=False)
    _write_scope_recall_config(tmp_path, {"maintenance_tools_enabled": True})
    p1 = load_memory_provider("scope-recall")
    p2 = load_memory_provider("scope-recall")
    assert p1 is not None and p2 is not None

    p1.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-1",
    )
    p2.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-2",
    )

    try:
        for plugin in (p1, p2):
            for _ in range(2):
                plugin._store_now(
                    content="Operator duplicate note across scopes.",
                    source="legacy-import",
                    target="general",
                    session_id="legacy",
                    semantic_merge=False,
                    allow_duplicate=True,
                )

        dry = json.loads(p1.handle_tool_call("scope_recall_dedupe", {"dry_run": True, "scope_only": False}))
        assert dry["duplicate_groups"] == 2
        assert dry["duplicates"] == 2

        applied = json.loads(p1.handle_tool_call("scope_recall_dedupe", {"dry_run": False, "scope_only": False}))
        assert applied["deleted"] == 2
        stats = json.loads(p1.handle_tool_call("scope_recall_stats", {}))
        assert stats["total_memories"] == 2
        assert stats["scope_memories"] == 1
    finally:
        p1.shutdown()
        p2.shutdown()


def test_vector_search_error_degrades_to_lexical_and_marks_needs_repair(provider, monkeypatch):
    payload = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Lexical fallback remembers gateway restart command.", "target": "ops"})
    )
    assert payload["stored"] is True

    def broken_search(*args, **kwargs):
        raise RuntimeError("missing lance data file")

    monkeypatch.setattr(provider._vector_store, "search", broken_search)
    result = json.loads(provider.handle_tool_call("scope_recall_search", {"query": "gateway restart command", "limit": 3}))

    assert result["count"] >= 1
    assert "gateway restart command" in result["results"][0]["content"]
    stats = json.loads(provider.handle_tool_call("scope_recall_stats", {}))
    assert stats["vector"]["status"] == "needs_repair"


def test_stats_reports_cached_vector_counts_without_full_audit(provider, monkeypatch):
    provider.handle_tool_call("scope_recall_store", {"content": "Stats should use cached vector counts.", "target": "ops"})
    provider.flush(timeout=5.0)
    if provider._vector_store is None:
        pytest.skip("vector store unavailable in this lane")

    def boom(*_args, **_kwargs):
        raise AssertionError("stats/status must not enumerate vectors or audit all IDs")

    monkeypatch.setattr(provider._vector_store, "audit_counts", boom)
    monkeypatch.setattr(provider._vector_store, "list_records", boom)
    monkeypatch.setattr(provider._vector_store, "list_ids", boom)
    monkeypatch.setattr(provider._vector_store, "count_rows", boom)

    stats = json.loads(provider.handle_tool_call("scope_recall_stats", {}))

    assert stats["vector"]["row_count"] == provider._vector_row_count
    assert stats["vector"]["unique_id_count"] == provider._vector_unique_id_count
    assert stats["vector"]["duplicate_row_count"] == provider._vector_duplicate_row_count


def test_repair_tool_rebuilds_vector_companion(provider):
    provider.handle_tool_call("scope_recall_store", {"content": "Repair command should rebuild vector index.", "target": "ops"})
    provider.flush(timeout=5.0)
    provider._vector_ready = False
    provider._vector_status = "needs_repair"
    provider._vector_message = "forced test damage"

    blocked = json.loads(provider.handle_tool_call("scope_recall_repair", {}))
    assert blocked["error"]

    provider._config["maintenance_tools_enabled"] = True
    payload = json.loads(provider.handle_tool_call("scope_recall_repair", {}))

    assert payload["repaired"] is True
    assert payload["vector"]["status"] == "ready"
    assert payload["vector"]["row_count"] == 1

def test_smart_extract_turn_creates_preference_and_fact_memories(provider):
    provider.sync_turn(
        "Joy prefers playful concise replies. The production deploy command is uv run app.",
        "Understood.",
    )
    provider.flush(timeout=5.0)

    empty = json.loads(provider.handle_tool_call("scope_recall_search", {"query": "Joy reply preference", "limit": 5}))
    assert empty["results"] == []
    with provider._lock:
        journal_count = provider._require_conn().execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
    assert journal_count >= 1

    provider._config["per_turn_extraction"] = {"enabled": True}
    provider.sync_turn(
        "Joy prefers playful concise replies. The production deploy command is uv run app.",
        "Understood.",
    )
    provider.flush(timeout=5.0)

    payload = json.loads(provider.handle_tool_call("scope_recall_search", {"query": "Joy reply preference", "limit": 5}))
    assert any(item["target"] == "user" and "playful concise replies" in item["content"] for item in payload["results"])

    payload = json.loads(provider.handle_tool_call("scope_recall_search", {"query": "production deploy command", "limit": 5}))
    assert any(item["target"] == "ops" and "uv run app" in item["content"] for item in payload["results"])


def test_semantic_near_duplicate_store_remains_distinct_by_default(provider):
    first = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Joy prefers concise replies.", "target": "user"})
    )
    second = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Joy likes brief responses.", "target": "user"})
    )

    assert first["stored"] is True
    assert second["stored"] is True
    assert second["merged"] is False
    assert second["id"] != first["id"]

    payload = json.loads(provider.handle_tool_call("scope_recall_search", {"query": "Joy response style", "limit": 5}))
    assert payload["count"] == 2
    contents = {item["content"] for item in payload["results"]}
    assert "Joy prefers concise replies." in contents
    assert "Joy likes brief responses." in contents


def test_semantic_merge_does_not_absorb_confirmed_store_into_candidate(provider):
    first = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Joy prefers concise replies.", "target": "user"})
    )
    row = provider._require_conn().execute("SELECT metadata FROM memories WHERE id = ?", (first["id"],)).fetchone()
    metadata = json.loads(str(row["metadata"] or "{}"))
    metadata["lifecycle"] = "candidate"
    provider._require_conn().execute(
        "UPDATE memories SET metadata = ? WHERE id = ?",
        (json.dumps(metadata, ensure_ascii=False, sort_keys=True), first["id"]),
    )
    provider._require_conn().commit()

    second = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Joy likes brief responses.", "target": "user"})
    )

    assert second["stored"] is True
    assert second.get("merged") is not True
    assert second["id"] != first["id"]
    candidate = provider._require_conn().execute("SELECT content, metadata FROM memories WHERE id = ?", (first["id"],)).fetchone()
    assert json.loads(str(candidate["metadata"]))["lifecycle"] == "candidate"
    assert candidate["content"] == "Joy prefers concise replies."


def test_semantic_merge_does_not_hide_conflicting_memory(provider):
    first = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Joy prefers concise replies.", "target": "user"})
    )
    second = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Joy no longer prefers concise replies.", "target": "user"})
    )

    assert first["stored"] is True
    assert second["stored"] is True
    assert second.get("merged") is not True
    stats = json.loads(provider.handle_tool_call("scope_recall_stats", {}))
    assert stats["total_memories"] == 2



def test_update_tool_cannot_change_shared_memory_into_local_general(tmp_path):
    p1 = load_memory_provider("scope-recall")
    p2 = load_memory_provider("scope-recall")
    assert p1 is not None and p2 is not None

    p1.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-1",
    )
    p2.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-2",
    )

    try:
        stored = json.loads(p1.handle_tool_call("scope_recall_store", {"content": "Joy durable note remains shared.", "target": "memory"}))
        blocked = json.loads(
            p1.handle_tool_call(
                "scope_recall_update",
                {"id": stored["id"], "content": "Temporary group one scratch sentinel.", "target": "general"},
            )
        )

        assert blocked["error"]
        row = p1._require_conn().execute("SELECT target, content, scope_id FROM memories WHERE id = ?", (stored["id"],)).fetchone()
        assert row is not None
        assert row["target"] == "memory"
        assert row["scope_id"] == p1._shared_scope_id

        p2.on_turn_start(1, "Temporary group one scratch sentinel")
        assert p2.prefetch("Temporary group one scratch sentinel") == ""
    finally:
        p1.shutdown()
        p2.shutdown()


def test_merge_tool_rejects_shared_local_scope_mixing(tmp_path):
    p1 = load_memory_provider("scope-recall")
    p2 = load_memory_provider("scope-recall")
    assert p1 is not None and p2 is not None

    p1.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-1",
    )
    p2.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-2",
    )

    try:
        local = json.loads(p1.handle_tool_call("scope_recall_store", {"content": "Group one local merge target.", "target": "general"}))
        shared = json.loads(p2.handle_tool_call("scope_recall_store", {"content": "Shared durable merge source.", "target": "memory"}))
        result = json.loads(p1.handle_tool_call("scope_recall_merge", {"target_id": local["id"], "source_ids": [shared["id"]]}))

        assert result["merged"] is False
        assert result["deleted"] == 0
        assert "shared durable and local scratch" in result["error"]
        with p1._lock:
            local_row = p1._require_conn().execute(
                "SELECT id FROM memories WHERE id = ?", (local["id"],)
            ).fetchone()
        with p2._lock:
            shared_row = p2._require_conn().execute(
                "SELECT id FROM memories WHERE id = ?", (shared["id"],)
            ).fetchone()
        assert local_row is not None
        assert shared_row is not None
    finally:
        p1.shutdown()
        p2.shutdown()

def test_merge_tool_cannot_read_or_delete_local_memory_from_another_scope(tmp_path):
    p1 = load_memory_provider("scope-recall")
    p2 = load_memory_provider("scope-recall")
    assert p1 is not None and p2 is not None

    p1.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-1",
    )
    p2.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-2",
    )

    try:
        a = json.loads(p1.handle_tool_call("scope_recall_store", {"content": "Group one target note.", "target": "general"}))
        b = json.loads(p2.handle_tool_call("scope_recall_store", {"content": "Group two source note.", "target": "general"}))
        result = json.loads(p1.handle_tool_call("scope_recall_merge", {"target_id": a["id"], "source_ids": [b["id"]]}))

        assert result["merged"] is False
        assert result["deleted"] == 0
        assert result["missing_source_ids"] == [b["id"]]
        assert "not accessible" in result["error"]
        with p2._lock:
            other_row = p2._require_conn().execute(
                "SELECT content FROM memories WHERE id = ?", (b["id"],)
            ).fetchone()
        assert other_row is not None
        assert other_row["content"] == "Group two source note."
        with p1._lock:
            own_row = p1._require_conn().execute(
                "SELECT content FROM memories WHERE id = ?", (a["id"],)
            ).fetchone()
        assert own_row is not None
        assert own_row["content"] == "Group one target note."
    finally:
        p1.shutdown()
        p2.shutdown()


def test_merge_tool_rejects_inaccessible_source_even_with_explicit_content(tmp_path):
    p1 = load_memory_provider("scope-recall")
    p2 = load_memory_provider("scope-recall")
    assert p1 is not None and p2 is not None

    p1.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-1",
    )
    p2.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-2",
    )

    try:
        a = json.loads(p1.handle_tool_call("scope_recall_store", {"content": "Group one merge target stays intact.", "target": "general"}))
        b = json.loads(p2.handle_tool_call("scope_recall_store", {"content": "Group two inaccessible source stays intact.", "target": "general"}))
        result = json.loads(
            p1.handle_tool_call(
                "scope_recall_merge",
                {
                    "target_id": a["id"],
                    "source_ids": [b["id"]],
                    "content": "Explicit overwrite must not happen when a source is inaccessible.",
                },
            )
        )

        assert result["merged"] is False
        assert result["deleted"] == 0
        assert result["missing_source_ids"] == [b["id"]]
        with p1._lock:
            own_row = p1._require_conn().execute(
                "SELECT content FROM memories WHERE id = ?", (a["id"],)
            ).fetchone()
        with p2._lock:
            other_row = p2._require_conn().execute(
                "SELECT content FROM memories WHERE id = ?", (b["id"],)
            ).fetchone()
        assert own_row is not None and own_row["content"] == "Group one merge target stays intact."
        assert other_row is not None and other_row["content"] == "Group two inaccessible source stays intact."
    finally:
        p1.shutdown()
        p2.shutdown()


def test_merge_tool_combines_memory_content_and_deletes_source(provider):
    a = json.loads(provider.handle_tool_call("scope_recall_store", {"content": "Joy prefers concise replies.", "target": "user"}))
    b = json.loads(provider.handle_tool_call("scope_recall_store", {"content": "Joy likes warm answers.", "target": "user"}))

    result = json.loads(
        provider.handle_tool_call(
            "scope_recall_merge",
            {"target_id": a["id"], "source_ids": [b["id"]], "content": "Joy prefers concise and warm replies.", "target": "user"},
        )
    )

    assert result["merged"] is True
    assert result["target_id"] == a["id"]
    assert result["deleted"] == 1
    payload = json.loads(provider.handle_tool_call("scope_recall_search", {"query": "warm replies", "limit": 5}))
    assert payload["count"] == 1
    assert payload["results"][0]["id"] == a["id"]
    assert "concise and warm" in payload["results"][0]["content"]


def test_export_tool_returns_jsonl_records(provider):
    stored = json.loads(provider.handle_tool_call("scope_recall_store", {"content": "Exportable deploy memory uses uv run app.", "target": "ops"}))

    payload = json.loads(provider.handle_tool_call("scope_recall_export", {"format": "jsonl"}))

    assert payload["format"] == "jsonl"
    assert payload["count"] >= 1
    lines = [line for line in payload["data"].splitlines() if line.strip()]
    assert lines
    parsed = [json.loads(line) for line in lines]
    assert any(row["id"] == stored["id"] and row["content"] == "Exportable deploy memory uses uv run app." for row in parsed)


def test_export_scope_only_false_requires_operator_mode(tmp_path):
    p1 = load_memory_provider("scope-recall")
    p2 = load_memory_provider("scope-recall")
    assert p1 is not None and p2 is not None

    p1.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-1",
    )
    p2.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-2",
    )

    try:
        own = json.loads(p1.handle_tool_call("scope_recall_store", {"content": "Group one export note.", "target": "general"}))
        other = json.loads(p2.handle_tool_call("scope_recall_store", {"content": "Group two export note.", "target": "general"}))

        scoped = json.loads(p1.handle_tool_call("scope_recall_export", {"format": "json", "scope_only": True}))
        assert scoped["count"] == 1
        assert scoped["data"][0]["id"] == own["id"]

        blocked = json.loads(p1.handle_tool_call("scope_recall_export", {"format": "json", "scope_only": False}))
        assert blocked["error"]
        assert other["id"] not in json.dumps(blocked)
    finally:
        p1.shutdown()
        p2.shutdown()


def test_export_scope_only_false_allowed_only_with_operator_config(tmp_path):
    _write_scope_recall_config(tmp_path, {"maintenance_tools_enabled": True})
    p1 = load_memory_provider("scope-recall")
    p2 = load_memory_provider("scope-recall")
    assert p1 is not None and p2 is not None

    p1.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-1",
    )
    p2.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-2",
    )

    try:
        own = json.loads(p1.handle_tool_call("scope_recall_store", {"content": "Operator group one export note.", "target": "general"}))
        other = json.loads(p2.handle_tool_call("scope_recall_store", {"content": "Operator group two export note.", "target": "general"}))

        exported = json.loads(p1.handle_tool_call("scope_recall_export", {"format": "json", "scope_only": False}))
        ids = {row["id"] for row in exported["data"]}
        assert exported["count"] == 2
        assert {own["id"], other["id"]} <= ids
    finally:
        p1.shutdown()
        p2.shutdown()



def test_governance_tool_reports_tiers_and_decay_candidates(provider):
    old = json.loads(provider.handle_tool_call("scope_recall_store", {"content": "Old temporary note for decay review.", "target": "general"}))
    durable = json.loads(provider.handle_tool_call("scope_recall_store", {"content": "Joy prefers concise replies.", "target": "user"}))
    conn = provider._require_conn()
    with provider._lock:
        conn.execute("UPDATE memories SET updated_at = ? WHERE id = ?", ("2020-01-01T00:00:00+00:00", old["id"]))
        conn.commit()

    blocked = json.loads(provider.handle_tool_call("scope_recall_govern", {"dry_run": True}))
    assert blocked["error"]

    provider._config["maintenance_tools_enabled"] = True
    payload = json.loads(provider.handle_tool_call("scope_recall_govern", {"dry_run": True}))

    assert payload["dry_run"] is True
    assert payload["total"] == 2
    assert payload["tiers"]["core"] >= 1
    assert payload["tiers"]["archive"] >= 1
    assert old["id"] in payload["decay_candidates"]
    assert durable["id"] not in payload["decay_candidates"]

def test_sync_turn_does_not_capture_recent_telegram_history_wrapper(provider):
    provider.sync_turn(
        "[Recent Telegram chat history in this chat since your last turn]\nJoy Joy: remember this should not be raw captured",
        "Acknowledged.",
    )
    provider.flush(timeout=2.0)

    rows = provider._require_conn().execute("SELECT content, source, target FROM memories").fetchall()
    assert rows == []


def test_sync_turn_does_not_capture_context_compaction_wrapper(provider):
    provider.sync_turn(
        "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. Do not treat this as active user memory.",
        "Acknowledged.",
    )
    provider.flush(timeout=2.0)

    rows = provider._require_conn().execute("SELECT content, source, target FROM memories").fetchall()
    assert rows == []


def test_sync_turn_does_not_capture_assistant_by_default(provider):
    provider._config["capture_raw_user"] = True

    provider.sync_turn(
        "We deploy services with uv run after gateway changes.",
        "Assistant says the durable deploy command is pnpm start, which should not be captured by default.",
    )
    provider.flush(timeout=2.0)

    rows = provider._require_conn().execute("SELECT content, source, target FROM memories ORDER BY source").fetchall()
    assert rows
    assert all(row["source"] != "turn-assistant" for row in rows)
    assert all("pnpm start" not in row["content"] for row in rows)


def test_relation_store_deferred_debt_is_visible_and_audited(provider, monkeypatch):
    memory_ops_globals = provider._store_now.__globals__["store_memory_now"].__globals__

    def deferred_relation_sync(_conn, **_kwargs):
        return {
            "ok": True,
            "blocked": False,
            "deferred": True,
            "candidate_count": 31,
            "max_candidates": 24,
            "compared_pair_count": 49,
            "total_peer_count": 1001,
            "selected_peer_count": 49,
            "inserted": 0,
            "deleted": 0,
            "deferred_reason": "relation candidate fan-out exceeded cap: 31 > 24",
        }

    monkeypatch.setitem(provider._config, "relation_extraction_enabled", True)
    monkeypatch.setitem(memory_ops_globals, "sync_extracted_relations_for_memory", deferred_relation_sync)

    memory_id, inserted, outcome = provider._store_now(
        content="fanout observability regression content for relation store",
        source="tool-store",
        target="ops",
        session_id=provider._session_id,
        allow_duplicate=True,
    )

    assert inserted is True
    assert outcome == "stored_relation_sync_deferred"
    with provider._lock:
        audit = provider._require_conn().execute(
            """
            SELECT event_type, action, target_id, after_json, reason
            FROM governance_audit_events
            WHERE target_id = ? AND event_type = 'relation_extraction' AND action = 'sync_deferred'
            """,
            (memory_id,),
        ).fetchone()
    assert audit is not None
    assert audit["target_id"] == memory_id
    assert json.loads(audit["after_json"])["candidate_count"] == 31
    assert "fan-out exceeded cap" in audit["reason"]

    public_result = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {
                "content": "public relation fanout visibility sentinel with a distinct payload",
                "target": "project",
                "memory_type": "project",
                "allow_duplicate": True,
            },
        )
    )
    assert public_result["stored"] is True
    assert public_result["relation_sync_status"] == "deferred"
    assert public_result["receipt"]["relation_sync_status"] == "deferred"


def test_relation_store_disabled_is_visible_in_public_receipt(provider, monkeypatch):
    monkeypatch.setitem(provider._config, "relation_extraction_enabled", False)

    public_result = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {
                "content": "relation extraction disabled receipt sentinel",
                "target": "project",
                "memory_type": "project",
                "allow_duplicate": True,
            },
        )
    )

    assert public_result["stored"] is True
    assert public_result["relation_sync_status"] == "disabled"
    assert public_result["receipt"]["relation_sync_status"] == "disabled"
