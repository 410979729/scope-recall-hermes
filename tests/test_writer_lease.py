"""P0-A / issue #39: cross-process truth write ownership.

The lease is an OS-level cooperating-writer contract. SQLite WAL already
allows multi-process readers and a single writer; the corruption class is
two independent processes each opening a writable pager against the same
truth database. Same-process peer providers share one refcounted OS lock
so issue #43 recovery remains possible.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from plugins.memory import load_memory_provider

import writer_lease as writer_lease_module
from writer_lease import (
    ALLOWED_TRUTH_WRITER_ROLES,
    TRUTH_WRITER_LEASE_FILENAME,
    TRUTH_WRITER_LEASE_INFO_FILENAME,
    TruthWriterBusyError,
    TruthWriterLease,
    holding_truth_writer_lease,
    read_truth_writer_owner,
)

READ_ONLY_STATUS = "active_read_only"
_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_auto_adjudication_is_a_recognized_truth_writer_role():
    assert "auto_adjudication" in ALLOWED_TRUTH_WRITER_ROLES


def _provider():
    provider = load_memory_provider("scope-recall")
    assert provider is not None
    return provider


def _write_config(hermes_home: Path, payload: dict) -> None:
    path = hermes_home / "scope-recall" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _initialize(provider, hermes_home: Path, session: str) -> None:
    provider.initialize(
        session,
        hermes_home=str(hermes_home),
        platform="cli",
        user_id="lease-user",
        chat_id="lease-chat",
        agent_identity="tester",
        agent_workspace="hermes",
        agent_context="primary",
    )


@contextlib.contextmanager
def _external_lease_holder(storage_dir: Path, *, role: str = "external-process"):
    storage_dir.mkdir(parents=True, exist_ok=True)
    child_script = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        sys.path.insert(0, {str(_REPO_ROOT)!r})
        from writer_lease import TruthWriterLease
        lease = TruthWriterLease(Path({str(storage_dir)!r}), role={role!r})
        result = lease.acquire()
        print("STATUS:" + result["status"], flush=True)
        sys.stdin.readline()
        lease.release()
        print("RELEASED", flush=True)
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "STATUS:acquired"
        yield child
    finally:
        if child.poll() is None:
            try:
                child.stdin.write("\n")
                child.stdin.close()
                assert child.stdout.readline().strip() == "RELEASED"
                child.wait(timeout=10)
            except Exception:
                child.kill()
                child.wait(timeout=10)


def test_lease_roundtrip_and_reacquisition(tmp_path):
    storage = tmp_path / "scope-recall"
    lease = TruthWriterLease(storage, role="test")
    result = lease.acquire()
    assert result["status"] == "acquired"
    assert lease.acquired is True
    assert (storage / TRUTH_WRITER_LEASE_FILENAME).is_file()
    owner = read_truth_writer_owner(storage)
    assert owner.get("role") == "unknown"
    lease.release()
    assert lease.acquired is False

    again = TruthWriterLease(storage, role="test-2")
    assert again.acquire()["status"] == "acquired"
    again.release()


def test_same_process_leases_share_one_refcounted_lock(tmp_path):
    storage = tmp_path / "scope-recall"
    baseline = len(writer_lease_module._PROCESS_REGISTRY)

    first = TruthWriterLease(storage, role="first")
    second = TruthWriterLease(storage, role="second")
    assert first.acquire()["status"] == "acquired"
    shared = second.acquire()
    assert shared["status"] == "acquired"
    assert shared.get("scope") == "same_process_shared"
    assert len(writer_lease_module._PROCESS_REGISTRY) == baseline + 1

    first.release()
    assert len(writer_lease_module._PROCESS_REGISTRY) == baseline + 1
    second.release()
    assert len(writer_lease_module._PROCESS_REGISTRY) == baseline


def test_connection_first_counts_as_pin_then_shares_provider_holder(tmp_path):
    storage = tmp_path / "scope-recall"
    baseline = len(writer_lease_module._PROCESS_REGISTRY)
    pin = TruthWriterLease(storage, role="truth_connection")
    owner = TruthWriterLease(storage, role="provider")
    try:
        first = pin.acquire()
        assert first["status"] == "acquired"
        state = writer_lease_module._PROCESS_REGISTRY[pin._registry_key]
        assert pin._pin_only is True
        assert state.holders == 0
        assert state.connection_pins == 1
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline + 1

        shared = owner.acquire()
        assert shared["status"] == "acquired"
        assert shared.get("scope") == "same_process_shared"
        assert owner._pin_only is False
        assert state.holders == 1
        assert state.connection_pins == 1

        owner.release()
        assert state.holders == 0
        assert state.connection_pins == 1
        assert pin.acquired is True
        assert _child_acquire_status(storage) == "STATUS:busy"

        pin.release()
        assert pin.acquired is False
        assert pin._registry_key not in writer_lease_module._PROCESS_REGISTRY
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline
        assert _child_acquire_status(storage) == "STATUS:acquired"
    finally:
        if owner.acquired:
            owner.release()
        if pin.acquired:
            pin.release()
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline


def test_acquire_fails_closed_without_os_lock_primitive(tmp_path, monkeypatch):
    monkeypatch.setattr(writer_lease_module, "_fcntl", None)
    monkeypatch.setattr(writer_lease_module, "_msvcrt", None)
    lease = TruthWriterLease(tmp_path / "scope-recall", role="no-primitive")
    busy = lease.acquire()
    assert busy["status"] == "busy"
    assert busy["scope"] == "unsupported_platform"
    assert lease.acquired is False


def test_owner_diagnostics_are_sanitized(tmp_path):
    storage = tmp_path / "scope-recall"
    lease = TruthWriterLease(storage, role="cli")
    assert lease.acquire()["status"] == "acquired"
    owner = read_truth_writer_owner(storage)
    serialized = json.dumps(owner)
    assert owner == {"role": "unknown"}
    assert str(storage) not in serialized
    assert "hostname" not in owner
    assert "pid" not in owner
    assert "username" not in owner
    lease.release()


def test_cross_process_lease_blocks_second_process(tmp_path):
    storage = tmp_path / "scope-recall"
    child_script = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        sys.path.insert(0, {str(_REPO_ROOT)!r})
        from writer_lease import TruthWriterLease
        lease = TruthWriterLease(Path({str(storage)!r}), role="journal_digest")
        result = lease.acquire()
        print("STATUS:" + result["status"], flush=True)
        sys.stdin.readline()
        lease.release()
        print("RELEASED", flush=True)
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "STATUS:acquired"
        mine = TruthWriterLease(storage, role="parent")
        busy = mine.acquire()
        assert busy["status"] == "busy"
        assert busy["scope"] == "cross_process"
        assert busy["owner"].get("role") == "journal_digest"
    finally:
        if child.poll() is None:
            try:
                child.stdin.write("\n")
                child.stdin.close()
                assert child.stdout.readline().strip() == "RELEASED"
                child.wait(timeout=10)
            except Exception:
                child.kill()
                child.wait(timeout=10)

    recovered = mine.acquire()
    assert recovered["status"] == "acquired"
    mine.release()


def test_provider_degrades_to_read_only_under_external_writer(tmp_path):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    storage = tmp_path / "scope-recall"

    seeder = _provider()
    _initialize(seeder, tmp_path, "seed-session")
    store_receipt = json.loads(
        seeder.handle_tool_call(
            "scope_recall_store",
            {
                "content": "The lease pilot project deploys with uv run app.",
                "target": "ops",
            },
        )
    )
    assert store_receipt.get("id")
    seeder.shutdown()

    reader = _provider()
    try:
        with _external_lease_holder(storage, role="external-gateway"):
            _initialize(reader, tmp_path, "reader-session")
            assert reader.runtime_status == READ_ONLY_STATUS
            assert reader._truth_writer_role == "reader"
            assert reader.is_available() is True

            search = json.loads(
                reader.handle_tool_call(
                    "scope_recall_search",
                    {"query": "lease pilot project deploy"},
                )
            )
            assert search.get("count", 0) >= 1

            blocked = json.loads(
                reader.handle_tool_call(
                    "scope_recall_store",
                    {"content": "must not be stored", "target": "ops"},
                )
            )
            assert "truth_writer_busy" in str(blocked.get("error") or "")

            db_path = storage / "memory.sqlite3"
            probe = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
            journal_before = probe.execute(
                "SELECT COUNT(*) FROM journal_entries"
            ).fetchone()[0]
            reader.sync_turn(
                "please remember this reader-mode line for the lease test",
                "acknowledged with a sufficiently long assistant reply for capture",
            )
            assert (
                reader.on_pre_compress(
                    [
                        {
                            "role": "user",
                            "content": "reader-mode compression boundary text",
                        }
                    ]
                )
                == ""
            )
            journal_after = probe.execute(
                "SELECT COUNT(*) FROM journal_entries"
            ).fetchone()[0]
            probe.close()
            assert journal_after == journal_before
            assert "read-only recall mode" in reader.system_prompt_block()

        reader.on_turn_start(2, "probe after external writer exit")
        assert reader._truth_writer_role == "owner"
        assert reader.runtime_status == "active"
        promoted = json.loads(
            reader.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "stored after writer-lease promotion succeeded",
                    "target": "ops",
                },
            )
        )
        assert promoted.get("id")
    finally:
        try:
            reader.shutdown()
        except Exception:
            pass


def test_same_process_peer_providers_both_write(tmp_path):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    first = _provider()
    second = _provider()
    try:
        _initialize(first, tmp_path, "peer-first")
        _initialize(second, tmp_path, "peer-second")
        assert first.runtime_status == "active"
        assert second.runtime_status == "active"
        assert first._truth_writer_role == "owner"
        assert second._truth_writer_role == "owner"
        receipt = json.loads(
            second.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "same-process peer write stays allowed",
                    "target": "ops",
                },
            )
        )
        assert receipt.get("id")
    finally:
        for provider in (first, second):
            try:
                provider.shutdown()
            except Exception:
                pass


def test_failed_writer_startup_releases_the_lease(tmp_path):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    broken = _provider()
    broken._initialize_writer_runtime = lambda: (_ for _ in ()).throw(
        RuntimeError("simulated writer startup failure")
    )
    with pytest.raises(RuntimeError, match="simulated writer startup failure"):
        _initialize(broken, tmp_path, "broken-session")
    assert broken._truth_writer_lease is None

    healthy = _provider()
    try:
        _initialize(healthy, tmp_path, "healthy-session")
        assert healthy.runtime_status == "active"
        assert healthy._truth_writer_role == "owner"
    finally:
        healthy.shutdown()


def test_reader_mode_without_database_stays_graceful(tmp_path):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    storage = tmp_path / "scope-recall"
    provider = _provider()
    try:
        with _external_lease_holder(storage, role="external-holder"):
            _initialize(provider, tmp_path, "no-db-session")
            assert provider.runtime_status == READ_ONLY_STATUS
            assert provider._conn is None
            assert provider.prefetch("anything relevant to recall here") == ""
            provider.sync_turn("captured nothing", "captured nothing either")
    finally:
        try:
            provider.shutdown()
        except Exception:
            pass


def test_abrupt_child_exit_releases_lease_for_immediate_reacquire(tmp_path):
    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True, exist_ok=True)
    child_script = textwrap.dedent(
        f"""
        import os
        import sys
        from pathlib import Path
        sys.path.insert(0, {str(_REPO_ROOT)!r})
        from writer_lease import TruthWriterLease
        lease = TruthWriterLease(Path({str(storage)!r}), role="crash-child")
        result = lease.acquire()
        print("STATUS:" + result["status"], flush=True)
        os._exit(1)
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_script],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "STATUS:acquired"
        child.wait(timeout=10)
        assert child.returncode == 1
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)

    recovered = TruthWriterLease(storage, role="parent")
    assert recovered.acquire()["status"] == "acquired"
    recovered.release()


def test_holding_helper_raises_sanitized_busy_error(tmp_path):
    storage = tmp_path / "scope-recall"
    with _external_lease_holder(storage, role="nightly_digest"):
        with pytest.raises(Exception, match="truth_writer_busy") as captured:
            with holding_truth_writer_lease(storage, role="journal_digest"):
                raise AssertionError("must not enter the writer body")
        assert type(captured.value).__name__ == "TruthWriterBusyError"
        message = str(captured.value)
        assert str(storage) not in message
        assert "pid" not in message.lower()
        assert "hostname" not in message.lower()
        assert captured.value.owner.get("role") == "nightly_digest"


def test_unpublished_escape_hatch_is_removed():
    from scope_recall.config import DEFAULT_CONFIG
    from scope_recall.config_schema import build_config_registry

    packaged = json.loads((_REPO_ROOT / "config.json").read_text(encoding="utf-8"))
    docs = (_REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    registry_keys = {entry["key"] for entry in build_config_registry()}
    assert "truth_writer_lease" not in DEFAULT_CONFIG
    assert "truth_writer_lease" not in packaged
    assert "truth_writer_lease.enabled" not in registry_keys
    assert "truth_writer_lease.enabled" not in docs


def test_save_config_busy_does_not_partially_write_config(tmp_path, monkeypatch):
    import scope_recall.truth_connection as truth_connection

    storage = tmp_path / "scope-recall"
    config_path = storage / "config.json"
    connect_calls: list[str] = []
    real_connect = truth_connection.connect_truth_database

    def tracking_connect(db_path, *args, **kwargs):
        connect_calls.append(str(kwargs.get("mode") or ""))
        return real_connect(db_path, *args, **kwargs)

    monkeypatch.setattr(truth_connection, "connect_truth_database", tracking_connect)
    plugin = _provider()
    with _external_lease_holder(storage, role="external-setup"):
        with pytest.raises(Exception, match="truth_writer_busy") as captured:
            plugin.save_config({"retrieval": {"top_k": 11}}, str(tmp_path))
        assert type(captured.value).__name__ == "TruthWriterBusyError"
        assert not config_path.exists()
        assert "rwc" not in connect_calls
        assert "rw" not in connect_calls


def test_save_config_acquires_lease_before_writable_connect(tmp_path, monkeypatch):
    import scope_recall.provider as provider_module
    import scope_recall.truth_connection as truth_connection

    events: list[str] = []
    real_acquire = TruthWriterLease.acquire
    real_connect = truth_connection.connect_truth_database

    def tracking_acquire(self, *args, **kwargs):
        events.append("acquire")
        return real_acquire(self, *args, **kwargs)

    def tracking_connect(db_path, *args, **kwargs):
        events.append(f"connect:{kwargs.get('mode') or 'default'}")
        return real_connect(db_path, *args, **kwargs)

    plugin = _provider()
    plugin_module = sys.modules[type(plugin).__module__]
    plugin_lease_module = sys.modules[plugin_module.TruthWriterLease.__module__]
    monkeypatch.setattr(TruthWriterLease, "acquire", tracking_acquire)
    monkeypatch.setattr(plugin_lease_module.TruthWriterLease, "acquire", tracking_acquire)
    monkeypatch.setattr(truth_connection, "connect_truth_database", tracking_connect)
    monkeypatch.setattr(provider_module, "connect_truth_database", tracking_connect)
    monkeypatch.setattr(plugin_module, "connect_truth_database", tracking_connect)

    plugin.save_config({"vector": {"enabled": False}}, str(tmp_path))
    assert "acquire" in events
    first_rw = next(
        index
        for index, event in enumerate(events)
        if event.startswith("connect:") and event.split(":", 1)[1] in {"rw", "rwc"}
    )
    assert events.index("acquire") < first_rw


def test_journal_digest_busy_before_writable_connect(tmp_path, monkeypatch):
    import scope_recall.journal as journal_module
    from scope_recall.journal import append_journal_entry, run_journal_digest
    from scope_recall.models import RuntimeScope
    from scope_recall.scope import build_scope_id, build_shared_scope_id
    from scope_recall.sql_store import ensure_schema
    from scope_recall.journal import ensure_journal_schema

    hermes_home = tmp_path
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps({"vector": {"enabled": False}}), encoding="utf-8"
    )
    db_path = storage / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_journal_schema(conn)
    scope = RuntimeScope(
        platform="cli",
        user_id="busy-journal-user",
        chat_id="busy-chat",
        thread_id="",
        gateway_session_key="",
        agent_identity="tester",
        agent_workspace="hermes",
        agent_context="primary",
    )
    entry_id = append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="busy-journal",
        turn_number=1,
        role="user",
        content="这篇日记在 writer lease 被占用时不得被 digest 消费或改写。",
    )
    conn.close()

    connect_calls: list[str] = []
    real_connect = journal_module.connect_truth_database

    def tracking_connect(db_path, *args, **kwargs):
        connect_calls.append(str(kwargs.get("mode") or ""))
        return real_connect(db_path, *args, **kwargs)

    monkeypatch.setattr(journal_module, "connect_truth_database", tracking_connect)
    with _external_lease_holder(storage, role="external-digest"):
        with pytest.raises(Exception, match="truth_writer_busy"):
            run_journal_digest(
                hermes_home=hermes_home,
                extractor="heuristic",
                scope=scope,
                interval_label="busy",
                limit_entries=50,
            )
    assert "rwc" not in connect_calls
    verify = sqlite3.connect(db_path)
    try:
        processed = verify.execute(
            "SELECT processed_run_id FROM journal_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()[0]
    finally:
        verify.close()
    assert processed in {None, ""}


def test_nightly_digest_busy_before_writable_connect(tmp_path, monkeypatch):
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    import scope_recall.nightly_digest as nightly_digest
    from scope_recall.nightly_digest import DigestOptions, run_digest

    hermes_home = tmp_path
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps({"vector": {"enabled": False}}), encoding="utf-8"
    )
    day = date(2026, 8, 14)
    started = datetime(2026, 8, 14, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    state = hermes_home / "state.db"
    state_conn = sqlite3.connect(state)
    try:
        state_conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                user_id TEXT,
                model TEXT,
                title TEXT,
                started_at REAL NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL
            );
            """
        )
        state_conn.execute(
            "INSERT INTO sessions(id, source, user_id, model, title, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("busy-nightly", "cli", "busy-user", "test", "busy nightly", started),
        )
        state_conn.execute(
            "INSERT INTO messages(session_id, role, content, tool_calls, tool_name, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "busy-nightly",
                "user",
                "Nightly digest must not open a writable truth pager while the lease is busy.",
                "",
                "",
                started,
            ),
        )
        state_conn.commit()
    finally:
        state_conn.close()

    connect_calls: list[str] = []
    real_connect = nightly_digest.connect_truth_database

    def tracking_connect(db_path, *args, **kwargs):
        connect_calls.append(str(kwargs.get("mode") or ""))
        return real_connect(db_path, *args, **kwargs)

    monkeypatch.setattr(nightly_digest, "connect_truth_database", tracking_connect)
    with _external_lease_holder(storage, role="external-nightly"):
        with pytest.raises(Exception, match="truth_writer_busy"):
            run_digest(
                DigestOptions(
                    hermes_home=hermes_home,
                    digest_date=day,
                    extractor="heuristic",
                )
            )
    assert "rwc" not in connect_calls
    assert "rw" not in connect_calls


def test_same_process_owner_joins_lease_for_save_config_and_journal(tmp_path):
    from scope_recall.journal import run_journal_digest

    _write_config(tmp_path, {"vector": {"enabled": False}})
    owner = _provider()
    try:
        _initialize(owner, tmp_path, "owner-join")
        owner.save_config({"vector": {"enabled": False}}, str(tmp_path))
        result = run_journal_digest(
            hermes_home=tmp_path,
            extractor="heuristic",
            interval_label="join",
            limit_entries=10,
        )
        assert result.get("ok") is True
        assert owner._truth_writer_role == "owner"
        assert owner._truth_writer_lease is not None
        assert owner._truth_writer_lease.acquired is True
    finally:
        owner.shutdown()


def test_dry_run_digest_does_not_require_writer_lease(tmp_path):
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    from scope_recall.journal import append_journal_entry, run_journal_digest
    from scope_recall.models import RuntimeScope
    from scope_recall.nightly_digest import DigestOptions, run_digest
    from scope_recall.scope import build_scope_id, build_shared_scope_id
    from scope_recall.sql_store import ensure_schema
    from scope_recall.journal import ensure_journal_schema

    hermes_home = tmp_path
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps({"vector": {"enabled": False}}), encoding="utf-8"
    )
    db_path = storage / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_journal_schema(conn)
    scope = RuntimeScope(
        platform="cli",
        user_id="dry-run-user",
        chat_id="dry-run-chat",
        thread_id="",
        gateway_session_key="",
        agent_identity="tester",
        agent_workspace="hermes",
        agent_context="primary",
    )
    append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="dry-run-journal",
        turn_number=1,
        role="user",
        content="Dry-run journal digest must stay read-only even when another process owns the writer lease.",
    )
    conn.close()
    day = date(2026, 8, 14)
    started = datetime(2026, 8, 14, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    state = hermes_home / "state.db"
    state_conn = sqlite3.connect(state)
    try:
        state_conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                user_id TEXT,
                model TEXT,
                title TEXT,
                started_at REAL NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL
            );
            """
        )
        state_conn.execute(
            "INSERT INTO sessions(id, source, user_id, model, title, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("dry-nightly", "cli", "dry-user", "test", "dry nightly", started),
        )
        state_conn.execute(
            "INSERT INTO messages(session_id, role, content, tool_calls, tool_name, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "dry-nightly",
                "user",
                "Dry-run nightly digest must not take the writer lease.",
                "",
                "",
                started,
            ),
        )
        state_conn.commit()
    finally:
        state_conn.close()

    with _external_lease_holder(storage, role="external-writer"):
        journal = run_journal_digest(
            hermes_home=hermes_home,
            extractor="heuristic",
            scope=scope,
            interval_label="dry",
            limit_entries=50,
            dry_run=True,
        )
        nightly = run_digest(
            DigestOptions(
                hermes_home=hermes_home,
                digest_date=day,
                extractor="heuristic",
                dry_run=True,
            )
        )
    assert journal.get("ok") is True
    assert nightly.get("ok") is True


def _sqlite_connection_closed(conn: sqlite3.Connection | None) -> bool:
    if conn is None:
        return True
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


def _cleanup_captured_digest_handles(captured: dict) -> None:
    """Release leaked OS handles so tmp_path teardown cannot hit WinError 32."""

    conn = captured.get("conn")
    if conn is not None and not _sqlite_connection_closed(conn):
        try:
            conn.close()
        except Exception:
            pass
    _release_tracked_leases(captured)


def _child_acquire_status(storage_dir: Path) -> str:
    """Ask a new process whether it can take the writer lease."""

    child_script = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        sys.path.insert(0, {str(_REPO_ROOT)!r})
        from writer_lease import TruthWriterLease
        lease = TruthWriterLease(Path({str(storage_dir)!r}), role="probe")
        result = lease.acquire()
        print("STATUS:" + result["status"], flush=True)
        if result["status"] == "acquired":
            lease.release()
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_script],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = (child.stdout.readline() or "").strip()
        child.wait(timeout=10)
        return line
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


class _TruthCloseRaises:
    """Proxy whose close() fails while the real SQLite connection stays open."""

    def __init__(self, inner: sqlite3.Connection) -> None:
        self._inner = inner

    def close(self) -> None:
        raise sqlite3.OperationalError("injected sqlite close failure")

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def _release_tracked_leases(captured: dict) -> None:
    """Release every captured lease, outer owner last."""

    leases = list(captured.get("leases") or [])
    primary = captured.get("lease")
    if primary is not None and primary not in leases:
        leases.append(primary)
    for lease in reversed(leases):
        if lease is None:
            continue
        try:
            lease.release()
        except Exception:
            pass


def _release_close_failure_handles(captured: dict) -> None:
    """Close the real pager, then drop the retained lease for Windows teardown."""

    real_conn = captured.get("real_conn")
    if isinstance(real_conn, sqlite3.Connection) and not _sqlite_connection_closed(real_conn):
        try:
            real_conn.close()
        except Exception:
            pass
    _release_tracked_leases(captured)


def _track_lease_instances(monkeypatch, *classes):
    captured: dict = {"lease": None, "conn": None, "leases": []}

    for cls in classes:
        original = cls.acquire

        def tracking_acquire(self, *args, _original=original, **kwargs):
            captured["leases"].append(self)
            if captured["lease"] is None:
                captured["lease"] = self
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(cls, "acquire", tracking_acquire)
    return captured


def _seed_nightly_state_db(hermes_home: Path) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    started = datetime(2026, 8, 14, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    state = hermes_home / "state.db"
    state_conn = sqlite3.connect(state)
    try:
        state_conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                user_id TEXT,
                model TEXT,
                title TEXT,
                started_at REAL NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL
            );
            """
        )
        state_conn.execute(
            "INSERT INTO sessions(id, source, user_id, model, title, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("cleanup-nightly", "cli", "cleanup-user", "test", "cleanup nightly", started),
        )
        state_conn.execute(
            "INSERT INTO messages(session_id, role, content, tool_calls, tool_name, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "cleanup-nightly",
                "user",
                "Nightly digest must release the writer lease if setup fails after connect.",
                "",
                "",
                started,
            ),
        )
        state_conn.commit()
    finally:
        state_conn.close()


def _seed_minimal_journal_entry(hermes_home: Path, *, content: str) -> None:
    """Write one unprocessed journal row so digest constructs a vector runtime."""

    from scope_recall.journal import append_journal_entry, ensure_journal_schema
    from scope_recall.models import RuntimeScope
    from scope_recall.scope import build_scope_id, build_shared_scope_id
    from scope_recall.sql_store import ensure_schema

    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True, exist_ok=True)
    db_path = storage / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        ensure_journal_schema(conn)
        scope = RuntimeScope(
            platform="cli",
            user_id="vector-close-user",
            chat_id="vector-close-chat",
            thread_id="",
            gateway_session_key="",
            agent_identity="tester",
            agent_workspace="hermes",
            agent_context="primary",
        )
        append_journal_entry(
            conn,
            scope=scope,
            scope_id=build_scope_id(scope),
            shared_scope_id=build_shared_scope_id(scope),
            session_id="vector-close-journal",
            turn_number=1,
            role="user",
            content=content,
        )
        conn.commit()
    finally:
        conn.close()


def _vector_close_bomb(base_cls: type, message: str) -> type:
    """Subclass a digest vector runtime so only companion close fails."""

    class _VectorCloseBomb(base_cls):
        def close(self) -> None:
            raise RuntimeError(message)

    return _VectorCloseBomb


def test_journal_digest_releases_lease_if_runtime_config_fails_after_connect(
    tmp_path, monkeypatch
):
    import scope_recall.journal as journal_module
    from scope_recall.journal import run_journal_digest

    hermes_home = tmp_path
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps({"vector": {"enabled": False}}), encoding="utf-8"
    )
    baseline = len(writer_lease_module._PROCESS_REGISTRY)
    captured = _track_lease_instances(
        monkeypatch,
        journal_module.TruthWriterLease,
        writer_lease_module.TruthWriterLease,
    )
    real_open = journal_module._open_digest_connection

    def tracking_open(*args, **kwargs):
        conn = real_open(*args, **kwargs)
        captured["conn"] = conn
        return conn

    monkeypatch.setattr(journal_module, "_open_digest_connection", tracking_open)
    monkeypatch.setattr(
        journal_module,
        "_runtime_config",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("injected journal runtime config failure")
        ),
    )

    try:
        with pytest.raises(RuntimeError, match="injected journal runtime config failure"):
            run_journal_digest(
                hermes_home=hermes_home,
                extractor="heuristic",
                interval_label="cleanup",
                limit_entries=10,
            )
        lease = captured["lease"]
        conn = captured["conn"]
        assert lease is not None
        assert conn is not None
        assert lease.acquired is False
        assert _sqlite_connection_closed(conn)
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline
        again = TruthWriterLease(storage, role="provider")
        assert again.acquire()["status"] == "acquired"
        again.release()
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline
    finally:
        _cleanup_captured_digest_handles(captured)


def test_journal_digest_owns_connection_before_authorizer_install(
    tmp_path, monkeypatch
):
    import scope_recall.journal as journal_module
    from scope_recall.journal import run_journal_digest

    hermes_home = tmp_path
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps({"vector": {"enabled": False}}), encoding="utf-8"
    )
    baseline = len(writer_lease_module._PROCESS_REGISTRY)
    captured = _track_lease_instances(
        monkeypatch,
        journal_module.TruthWriterLease,
        writer_lease_module.TruthWriterLease,
    )
    real_connect = journal_module.connect_truth_database

    def tracking_connect(db_path, *args, **kwargs):
        conn = real_connect(db_path, *args, **kwargs)
        mode = str(kwargs.get("mode") or "")
        if mode in {"rw", "rwc"} and str(db_path) != ":memory:":
            captured["conn"] = conn
        return conn

    monkeypatch.setattr(journal_module, "connect_truth_database", tracking_connect)
    monkeypatch.setattr(
        journal_module,
        "install_activation_lease_authorizer",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("injected journal authorizer failure")
        ),
    )

    try:
        with pytest.raises(RuntimeError, match="injected journal authorizer failure"):
            run_journal_digest(
                hermes_home=hermes_home,
                extractor="heuristic",
                interval_label="authorizer",
                limit_entries=10,
            )
        lease = captured["lease"]
        conn = captured["conn"]
        assert lease is not None
        assert conn is not None
        if not _sqlite_connection_closed(conn):
            conn.execute(
                "CREATE TABLE IF NOT EXISTS journal_authorizer_leak_probe(label TEXT)"
            )
            conn.execute(
                "INSERT INTO journal_authorizer_leak_probe(label) VALUES ('live')"
            )
            conn.commit()
            child_during_leak = _child_acquire_status(storage)
            raise AssertionError(
                "opened writable connection remained live after authorizer "
                f"failure; child={child_during_leak}"
            )
        assert lease.acquired is False
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline
        assert _child_acquire_status(storage) == "STATUS:acquired"
    finally:
        _cleanup_captured_digest_handles(captured)


def test_journal_digest_keeps_lease_if_close_fails_after_authorizer(
    tmp_path, monkeypatch
):
    import scope_recall.journal as journal_module
    from scope_recall.journal import run_journal_digest

    hermes_home = tmp_path
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps({"vector": {"enabled": False}}), encoding="utf-8"
    )
    baseline = len(writer_lease_module._PROCESS_REGISTRY)
    captured = _track_lease_instances(
        monkeypatch,
        journal_module.TruthWriterLease,
        writer_lease_module.TruthWriterLease,
    )
    real_connect = journal_module.connect_truth_database

    def wrapping_connect(db_path, *args, **kwargs):
        conn = real_connect(db_path, *args, **kwargs)
        mode = str(kwargs.get("mode") or "")
        if mode in {"rw", "rwc"} and str(db_path) != ":memory:":
            captured["real_conn"] = conn
            proxy = _TruthCloseRaises(conn)
            captured["conn"] = proxy
            return proxy
        return conn

    monkeypatch.setattr(journal_module, "connect_truth_database", wrapping_connect)
    monkeypatch.setattr(
        journal_module,
        "install_activation_lease_authorizer",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("injected journal authorizer failure")
        ),
    )

    try:
        with pytest.raises(
            sqlite3.OperationalError, match="injected sqlite close failure"
        ) as caught:
            run_journal_digest(
                hermes_home=hermes_home,
                extractor="heuristic",
                interval_label="authorizer-close",
                limit_entries=10,
            )
        context = caught.value.__context__
        assert isinstance(context, RuntimeError)
        assert "injected journal authorizer failure" in str(context)
        lease = captured["lease"]
        assert lease is not None
        assert lease.acquired is True
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline + 1
        assert _child_acquire_status(storage) == "STATUS:busy"
    finally:
        _release_close_failure_handles(captured)


def test_dry_run_digest_connection_closes_destination_if_backup_fails(
    tmp_path, monkeypatch
):
    import scope_recall.journal as journal_module

    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True)
    db_path = storage / "memory.sqlite3"
    source = sqlite3.connect(db_path)
    try:
        source.execute("CREATE TABLE dry_run_backup_probe(label TEXT)")
        source.commit()
    finally:
        source.close()

    captured: dict[str, sqlite3.Connection | None] = {"dest": None}
    real_connect = journal_module.connect_truth_database

    class _SourceBackupFails:
        def __init__(self, inner: sqlite3.Connection) -> None:
            self._inner = inner

        def backup(self, target, *args, **kwargs):
            del target, args, kwargs
            raise sqlite3.OperationalError("injected dry-run backup failure")

        def close(self) -> None:
            self._inner.close()

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    def tracking_connect(db_path, *args, **kwargs):
        conn = real_connect(db_path, *args, **kwargs)
        if str(db_path) == ":memory:":
            captured["dest"] = conn
            return conn
        return _SourceBackupFails(conn)

    monkeypatch.setattr(journal_module, "connect_truth_database", tracking_connect)
    try:
        with pytest.raises(
            sqlite3.OperationalError, match="injected dry-run backup failure"
        ):
            journal_module._open_digest_connection(db_path, dry_run=True)
        dest = captured["dest"]
        assert dest is not None
        assert _sqlite_connection_closed(dest)
    finally:
        dest = captured.get("dest")
        if dest is not None and not _sqlite_connection_closed(dest):
            dest.close()


def test_nightly_digest_releases_lease_if_llm_config_fails_after_connect(
    tmp_path, monkeypatch
):
    from datetime import date

    import scope_recall.nightly_digest as nightly_digest
    from scope_recall.nightly_digest import DigestOptions, run_digest

    hermes_home = tmp_path
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps({"vector": {"enabled": False}}), encoding="utf-8"
    )
    _seed_nightly_state_db(hermes_home)
    baseline = len(writer_lease_module._PROCESS_REGISTRY)
    captured = _track_lease_instances(
        monkeypatch,
        nightly_digest.TruthWriterLease,
        writer_lease_module.TruthWriterLease,
    )
    real_connect = nightly_digest.connect_truth_database

    def tracking_connect(db_path, *args, **kwargs):
        conn = real_connect(db_path, *args, **kwargs)
        mode = str(kwargs.get("mode") or "")
        if mode in {"rw", "rwc"} and str(db_path) != ":memory:":
            captured["conn"] = conn
        return conn

    monkeypatch.setattr(nightly_digest, "connect_truth_database", tracking_connect)
    monkeypatch.setattr(
        nightly_digest,
        "resolve_llm_config",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("injected nightly llm config failure")
        ),
    )

    try:
        with pytest.raises(RuntimeError, match="injected nightly llm config failure"):
            run_digest(
                DigestOptions(
                    hermes_home=hermes_home,
                    digest_date=date(2026, 8, 14),
                    extractor="heuristic",
                )
            )
        lease = captured["lease"]
        conn = captured["conn"]
        assert lease is not None
        assert conn is not None
        assert lease.acquired is False
        assert _sqlite_connection_closed(conn)
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline
        again = TruthWriterLease(storage, role="provider")
        assert again.acquire()["status"] == "acquired"
        again.release()
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline
    finally:
        _cleanup_captured_digest_handles(captured)


def test_journal_digest_keeps_lease_if_truth_close_fails_after_connect(
    tmp_path, monkeypatch
):
    import scope_recall.journal as journal_module
    from scope_recall.journal import run_journal_digest

    hermes_home = tmp_path
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps({"vector": {"enabled": False}}), encoding="utf-8"
    )
    baseline = len(writer_lease_module._PROCESS_REGISTRY)
    captured = _track_lease_instances(
        monkeypatch,
        journal_module.TruthWriterLease,
        writer_lease_module.TruthWriterLease,
    )
    real_open = journal_module._open_digest_connection

    def wrapping_open(*args, **kwargs):
        conn = real_open(*args, **kwargs)
        captured["real_conn"] = conn
        proxy = _TruthCloseRaises(conn)
        captured["conn"] = proxy
        return proxy

    monkeypatch.setattr(journal_module, "_open_digest_connection", wrapping_open)
    monkeypatch.setattr(
        journal_module,
        "_runtime_config",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("injected journal runtime config failure")
        ),
    )

    try:
        with pytest.raises(
            sqlite3.OperationalError, match="injected sqlite close failure"
        ) as caught:
            run_journal_digest(
                hermes_home=hermes_home,
                extractor="heuristic",
                interval_label="cleanup",
                limit_entries=10,
            )
        context = caught.value.__context__
        assert isinstance(context, RuntimeError)
        assert "injected journal runtime config failure" in str(context)
        lease = captured["lease"]
        assert lease is not None
        assert lease.acquired is True
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline + 1
        assert _child_acquire_status(storage) == "STATUS:busy"
    finally:
        _release_close_failure_handles(captured)


def test_nightly_digest_keeps_lease_if_truth_close_fails_after_connect(
    tmp_path, monkeypatch
):
    from datetime import date

    import scope_recall.nightly_digest as nightly_digest
    from scope_recall.nightly_digest import DigestOptions, run_digest

    hermes_home = tmp_path
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps({"vector": {"enabled": False}}), encoding="utf-8"
    )
    _seed_nightly_state_db(hermes_home)
    baseline = len(writer_lease_module._PROCESS_REGISTRY)
    captured = _track_lease_instances(
        monkeypatch,
        nightly_digest.TruthWriterLease,
        writer_lease_module.TruthWriterLease,
    )
    real_connect = nightly_digest.connect_truth_database

    def wrapping_connect(db_path, *args, **kwargs):
        conn = real_connect(db_path, *args, **kwargs)
        mode = str(kwargs.get("mode") or "")
        if mode in {"rw", "rwc"} and str(db_path) != ":memory:":
            captured["real_conn"] = conn
            proxy = _TruthCloseRaises(conn)
            captured["conn"] = proxy
            return proxy
        return conn

    monkeypatch.setattr(nightly_digest, "connect_truth_database", wrapping_connect)
    monkeypatch.setattr(
        nightly_digest,
        "resolve_llm_config",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("injected nightly llm config failure")
        ),
    )

    try:
        with pytest.raises(
            sqlite3.OperationalError, match="injected sqlite close failure"
        ) as caught:
            run_digest(
                DigestOptions(
                    hermes_home=hermes_home,
                    digest_date=date(2026, 8, 14),
                    extractor="heuristic",
                )
            )
        context = caught.value.__context__
        assert isinstance(context, RuntimeError)
        assert "injected nightly llm config failure" in str(context)
        lease = captured["lease"]
        assert lease is not None
        assert lease.acquired is True
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline + 1
        assert _child_acquire_status(storage) == "STATUS:busy"
    finally:
        _release_close_failure_handles(captured)


def test_journal_digest_raises_vector_close_after_truth_and_lease_cleanup(
    tmp_path, monkeypatch
):
    import scope_recall.journal as journal_module
    import scope_recall.nightly_digest as nightly_digest
    from scope_recall.journal import run_journal_digest

    hermes_home = tmp_path
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps({"vector": {"enabled": False}}), encoding="utf-8"
    )
    _seed_minimal_journal_entry(
        hermes_home,
        content="Journal digest must surface vector close failure after truth teardown.",
    )
    baseline = len(writer_lease_module._PROCESS_REGISTRY)
    captured = _track_lease_instances(
        monkeypatch,
        journal_module.TruthWriterLease,
        writer_lease_module.TruthWriterLease,
    )
    real_open = journal_module._open_digest_connection

    def tracking_open(*args, **kwargs):
        conn = real_open(*args, **kwargs)
        captured["conn"] = conn
        return conn

    monkeypatch.setattr(journal_module, "_open_digest_connection", tracking_open)
    monkeypatch.setattr(
        nightly_digest,
        "DigestVectorRuntime",
        _vector_close_bomb(
            nightly_digest.DigestVectorRuntime,
            "injected journal vector close failure",
        ),
    )

    try:
        with pytest.raises(RuntimeError, match="injected journal vector close failure"):
            run_journal_digest(
                hermes_home=hermes_home,
                extractor="heuristic",
                interval_label="vector-close",
                limit_entries=10,
            )
        lease = captured["lease"]
        conn = captured["conn"]
        assert lease is not None
        assert conn is not None
        assert lease.acquired is False
        assert _sqlite_connection_closed(conn)
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline
        assert _child_acquire_status(storage) == "STATUS:acquired"
    finally:
        _cleanup_captured_digest_handles(captured)


def test_nightly_digest_raises_vector_close_after_truth_and_lease_cleanup(
    tmp_path, monkeypatch
):
    from datetime import date

    import scope_recall.nightly_digest as nightly_digest
    from scope_recall.nightly_digest import DigestOptions, run_digest

    hermes_home = tmp_path
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps({"vector": {"enabled": False}}), encoding="utf-8"
    )
    _seed_nightly_state_db(hermes_home)
    baseline = len(writer_lease_module._PROCESS_REGISTRY)
    captured = _track_lease_instances(
        monkeypatch,
        nightly_digest.TruthWriterLease,
        writer_lease_module.TruthWriterLease,
    )
    real_connect = nightly_digest.connect_truth_database

    def tracking_connect(db_path, *args, **kwargs):
        conn = real_connect(db_path, *args, **kwargs)
        mode = str(kwargs.get("mode") or "")
        if mode in {"rw", "rwc"} and str(db_path) != ":memory:":
            captured["conn"] = conn
        return conn

    monkeypatch.setattr(nightly_digest, "connect_truth_database", tracking_connect)
    monkeypatch.setattr(
        nightly_digest,
        "DigestVectorRuntime",
        _vector_close_bomb(
            nightly_digest.DigestVectorRuntime,
            "injected nightly vector close failure",
        ),
    )

    try:
        with pytest.raises(RuntimeError, match="injected nightly vector close failure"):
            run_digest(
                DigestOptions(
                    hermes_home=hermes_home,
                    digest_date=date(2026, 8, 14),
                    extractor="heuristic",
                )
            )
        lease = captured["lease"]
        conn = captured["conn"]
        assert lease is not None
        assert conn is not None
        assert lease.acquired is False
        assert _sqlite_connection_closed(conn)
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline
        assert _child_acquire_status(storage) == "STATUS:acquired"
    finally:
        _cleanup_captured_digest_handles(captured)


_ADVERSARIAL_LEASE_ROLE = (
    "C:\\Users\\Administrator\\.ssh\\id_rsa "
    + "ghp_"
    + "abcdefghijklmnopqrstuvwxyz0123456789 "
    + "DESKTOP-HOST1\\admin\r\n\t"
    + ("leakpath" * 80)
)
_ADVERSARIAL_FRAGMENTS = (
    "Users",
    "Administrator",
    "ghp_",
    "DESKTOP-HOST1",
    "id_rsa",
    "leakpath",
    "memory.sqlite3",
)


def _assert_owner_is_sanitized(owner: dict, serialized: str) -> None:
    assert owner == {"role": "unknown"}
    lowered = serialized.lower()
    for fragment in _ADVERSARIAL_FRAGMENTS:
        assert fragment.lower() not in lowered
    assert _ADVERSARIAL_LEASE_ROLE not in serialized


def test_adversarial_sidecar_role_is_never_echoed(tmp_path):
    from scope_recall.memory_ops import stats_payload

    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True)
    info_path = storage / TRUTH_WRITER_LEASE_INFO_FILENAME
    info_path.write_text(
        json.dumps(
            {
                "role": _ADVERSARIAL_LEASE_ROLE,
                "pid": 12345,
                "hostname": "DESKTOP-HOST1",
                "username": "Administrator",
                "db_path": str(storage / "memory.sqlite3"),
                "storage_path": str(storage),
            }
        ),
        encoding="utf-8",
    )

    owner = read_truth_writer_owner(storage)
    _assert_owner_is_sanitized(owner, json.dumps(owner))

    busy = TruthWriterBusyError(
        role=_ADVERSARIAL_LEASE_ROLE,
        owner={
            "role": _ADVERSARIAL_LEASE_ROLE,
            "pid": 12345,
            "hostname": "DESKTOP-HOST1",
        },
    )
    _assert_owner_is_sanitized(busy.owner, json.dumps(busy.owner))
    assert _ADVERSARIAL_LEASE_ROLE not in str(busy)
    assert "12345" not in str(busy)

    _write_config(tmp_path, {"vector": {"enabled": False}})
    seeder = _provider()
    _initialize(seeder, tmp_path, "adversarial-seed")
    seeder.shutdown()
    reader = _provider()
    try:
        with _external_lease_holder(storage, role="provider"):
            info_path.write_text(
                json.dumps({"role": _ADVERSARIAL_LEASE_ROLE, "pid": 99}),
                encoding="utf-8",
            )
            _initialize(reader, tmp_path, "adversarial-reader")
            reader._truth_writer_owner = {
                "role": _ADVERSARIAL_LEASE_ROLE,
                "pid": 99,
                "username": "Administrator",
            }
            stats = stats_payload(reader)
            owner_payload = stats.get("truth_writer", {}).get("owner")
            _assert_owner_is_sanitized(
                owner_payload, json.dumps(stats.get("truth_writer"))
            )
    finally:
        try:
            reader.shutdown()
        except Exception:
            pass


@pytest.mark.parametrize(
    "role",
    ["provider", "save_config", "journal_digest", "nightly_digest", "truth_connection"],
)
def test_production_lease_roles_remain_visible(tmp_path, role):
    storage = tmp_path / "scope-recall"
    lease = TruthWriterLease(storage, role=role)
    assert lease.acquire()["status"] == "acquired"
    try:
        assert read_truth_writer_owner(storage) == {"role": role}
    finally:
        lease.release()


def test_shared_holder_publication_is_race_safe():
    name = writer_lease_module._SHARED_STATE_NAME
    existing = sys.modules.get(name)
    barrier = threading.Barrier(2)
    results: list[tuple[object, object]] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        pair = writer_lease_module._shared_lock_and_registry()
        with lock:
            results.append(pair)

    try:
        sys.modules.pop(name, None)
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
        assert len(results) == 2
        assert results[0][0] is results[1][0]
        assert results[0][1] is results[1][1]
        assert sys.modules[name] is not None
        assert getattr(sys.modules[name], "lock") is results[0][0]
        assert getattr(sys.modules[name], "registry") is results[0][1]
    finally:
        if existing is not None:
            sys.modules[name] = existing


def test_same_process_threads_serialize_os_lock_under_registry_lock(
    tmp_path, monkeypatch
):
    storage = tmp_path / "scope-recall"
    baseline = len(writer_lease_module._PROCESS_REGISTRY)
    os_lock_calls: list[int] = []
    lock_held_during_os: list[bool] = []
    results: list[dict] = []
    leases: list[TruthWriterLease] = []
    result_lock = threading.Lock()
    started = threading.Barrier(2)
    real_try = writer_lease_module._try_lock_exclusive_nonblocking

    def tracking_try(handle):
        lock_held_during_os.append(writer_lease_module._PROCESS_REGISTRY_LOCK.locked())
        os_lock_calls.append(threading.get_ident())
        return real_try(handle)

    monkeypatch.setattr(
        writer_lease_module, "_try_lock_exclusive_nonblocking", tracking_try
    )
    scope_module = sys.modules.get("scope_recall.writer_lease")
    if scope_module is not None:
        monkeypatch.setattr(scope_module, "_try_lock_exclusive_nonblocking", tracking_try)

    def worker() -> None:
        started.wait()
        lease = TruthWriterLease(storage, role="provider")
        result = lease.acquire()
        with result_lock:
            results.append(result)
            leases.append(lease)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()
        assert len(results) == 2
        assert all(item.get("status") == "acquired" for item in results)
        assert lock_held_during_os and all(lock_held_during_os)
        assert len(os_lock_calls) == 1
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline + 1
        state = writer_lease_module._PROCESS_REGISTRY[leases[0]._registry_key]
        assert state.holders == 2
    finally:
        for lease in leases:
            try:
                lease.release()
            except Exception:
                pass
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline


@pytest.mark.skipif(os.name != "nt", reason="Windows path case folding")
def test_windows_case_variant_paths_share_one_registry_owner(tmp_path):
    storage = tmp_path / "ScopeRecall"
    variant = Path(str(storage).swapcase())
    if str(variant) == str(storage):
        pytest.skip("path case did not change")
    baseline = len(writer_lease_module._PROCESS_REGISTRY)
    first = TruthWriterLease(storage, role="provider")
    second = TruthWriterLease(variant, role="provider")
    try:
        assert first.acquire()["status"] == "acquired"
        shared = second.acquire()
        assert shared["status"] == "acquired"
        assert shared.get("scope") == "same_process_shared"
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline + 1
    finally:
        first.release()
        second.release()
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline


@pytest.mark.skipif(os.name != "nt", reason="Windows junction canonicalization")
def test_windows_junction_path_shares_one_registry_owner(tmp_path):
    storage = tmp_path / "scope-recall"
    storage.mkdir()
    junction = tmp_path / "alias-recall"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(storage)],
        check=False,
        capture_output=True,
    )
    if created.returncode != 0 or not junction.exists():
        detail = (created.stderr or created.stdout or b"").decode(
            "utf-8", errors="replace"
        )
        pytest.skip(f"could not create junction: {detail}")
    baseline = len(writer_lease_module._PROCESS_REGISTRY)
    first = TruthWriterLease(storage, role="provider")
    second = TruthWriterLease(junction, role="provider")
    try:
        assert first.acquire()["status"] == "acquired"
        shared = second.acquire()
        assert shared["status"] == "acquired"
        assert shared.get("scope") == "same_process_shared"
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline + 1
    finally:
        first.release()
        second.release()
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline


# Synthetic Win32 forms only. Do not use home/Users/UNC hostnames from this machine.
_LEASE_DRIVE_ORDINARY = r"C:\sr-lease-key\.truth-writer.lease"
_LEASE_DRIVE_EXTENDED = r"\\?\C:\sr-lease-key\.truth-writer.lease"
_LEASE_UNC_ORDINARY = r"\\fileserver\share\sr-lease-key\.truth-writer.lease"
_LEASE_UNC_EXTENDED = r"\\?\UNC\fileserver\share\sr-lease-key\.truth-writer.lease"
_LEASE_UNC_EXTENDED_MIXED = r"\\?\Unc\FileServer\Share\sr-lease-key\.truth-writer.lease"


def test_strip_windows_extended_prefix_joins_drive_and_unc_forms():
    strip = writer_lease_module._strip_windows_extended_prefix
    assert strip(_LEASE_DRIVE_EXTENDED) == _LEASE_DRIVE_ORDINARY
    assert strip(_LEASE_DRIVE_ORDINARY) == _LEASE_DRIVE_ORDINARY
    assert strip(_LEASE_UNC_EXTENDED) == _LEASE_UNC_ORDINARY
    assert strip(_LEASE_UNC_EXTENDED_MIXED) == _LEASE_UNC_ORDINARY.replace(
        "fileserver\\share", "FileServer\\Share"
    )
    assert strip(_LEASE_UNC_ORDINARY) == _LEASE_UNC_ORDINARY
    assert strip("/var/tmp/scope-recall/.truth-writer.lease") == (
        "/var/tmp/scope-recall/.truth-writer.lease"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path namespace")
def test_canonical_registry_key_joins_windows_extended_drive_prefix():
    ordinary = writer_lease_module._canonical_registry_key(Path(_LEASE_DRIVE_ORDINARY))
    extended = writer_lease_module._canonical_registry_key(Path(_LEASE_DRIVE_EXTENDED))
    assert ordinary == extended
    assert ordinary == os.path.normcase(os.path.normpath(_LEASE_DRIVE_ORDINARY))
    assert not ordinary.startswith("\\\\?\\")


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path namespace")
def test_canonical_registry_key_joins_windows_extended_unc_prefix():
    ordinary = writer_lease_module._canonical_registry_key(Path(_LEASE_UNC_ORDINARY))
    extended = writer_lease_module._canonical_registry_key(Path(_LEASE_UNC_EXTENDED))
    mixed = writer_lease_module._canonical_registry_key(Path(_LEASE_UNC_EXTENDED_MIXED))
    assert ordinary == extended == mixed
    assert ordinary == os.path.normcase(os.path.normpath(_LEASE_UNC_ORDINARY))
    assert not ordinary.casefold().startswith("\\\\?\\")


class _PausedHandleClose:
    """Pause the last-holder OS close so same-process reacquire can be observed."""

    def __init__(self, inner, *, in_close: threading.Event, allow_close: threading.Event, info_path: Path) -> None:
        self._inner = inner
        self._in_close = in_close
        self._allow_close = allow_close
        self._info_path = info_path

    def close(self) -> None:
        self._in_close.set()
        assert not self._info_path.exists()
        assert self._allow_close.wait(timeout=5.0)
        self._inner.close()

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class _CloseRaisesKeepLocked:
    """Fail close() while the real locked handle stays open."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def close(self) -> None:
        raise OSError("injected lease handle close failure")

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class _CloseThenRaise:
    """Close the real locked handle, then raise so authority is already gone."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def close(self) -> None:
        self._inner.close()
        raise OSError("injected lease handle close after authority gone")

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def test_last_release_and_same_process_reacquire_are_atomic(tmp_path):
    storage = tmp_path / "scope-recall"
    baseline = len(writer_lease_module._PROCESS_REGISTRY)
    in_close = threading.Event()
    allow_close = threading.Event()
    acquire_done = threading.Event()
    info_path = storage / TRUTH_WRITER_LEASE_INFO_FILENAME

    holder = TruthWriterLease(storage, role="provider")
    acquired = holder.acquire()
    assert acquired["status"] == "acquired"
    assert info_path.is_file()
    assert len(writer_lease_module._PROCESS_REGISTRY) == baseline + 1
    state = writer_lease_module._PROCESS_REGISTRY[holder._registry_key]
    real_handle = state.handle
    state.handle = _PausedHandleClose(
        real_handle,
        in_close=in_close,
        allow_close=allow_close,
        info_path=info_path,
    )

    release_error: list[BaseException] = []

    def releaser() -> None:
        try:
            holder.release()
        except BaseException as exc:
            release_error.append(exc)

    release_thread = threading.Thread(target=releaser, name="lease-last-release")
    release_thread.start()
    assert in_close.wait(timeout=5.0)
    assert not info_path.exists()
    if release_error:
        raise release_error[0]
    state = writer_lease_module._PROCESS_REGISTRY.get(holder._registry_key)
    assert state is not None
    assert state.holders == 1
    assert holder.acquired is True

    result: dict[str, object] = {}
    reacquired: list[TruthWriterLease] = []

    def acquirer() -> None:
        lease = TruthWriterLease(storage, role="provider")
        reacquired.append(lease)
        result["status"] = lease.acquire()
        acquire_done.set()

    acquire_thread = threading.Thread(target=acquirer, name="lease-reacquire")
    acquire_thread.start()
    try:
        finished_during_close = acquire_done.wait(timeout=0.3)
        assert finished_during_close is False
        assert acquire_thread.is_alive()
        allow_close.set()
        acquire_thread.join(timeout=10.0)
        release_thread.join(timeout=10.0)
        assert not acquire_thread.is_alive()
        assert not release_thread.is_alive()
        assert release_error == []
        assert acquire_done.is_set()
        acquired_again = result.get("status")
        assert isinstance(acquired_again, dict)
        assert acquired_again.get("status") == "acquired"
        assert acquired_again.get("scope") != "cross_process"
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline + 1
    finally:
        allow_close.set()
        acquire_thread.join(timeout=10.0)
        release_thread.join(timeout=10.0)
        for lease in reacquired:
            try:
                lease.release()
            except Exception:
                pass
        current = writer_lease_module._PROCESS_REGISTRY.get(holder._registry_key)
        if current is not None and current.handle is not real_handle:
            current.handle = real_handle
        if holder.acquired:
            try:
                holder.release()
            except Exception:
                pass
    assert len(writer_lease_module._PROCESS_REGISTRY) == baseline
    assert holder._registry_key not in writer_lease_module._PROCESS_REGISTRY
    assert holder.acquired is False
    assert all(not lease.acquired for lease in reacquired)


def test_last_release_retains_authority_if_handle_close_fails(tmp_path):
    storage = tmp_path / "scope-recall"
    baseline = len(writer_lease_module._PROCESS_REGISTRY)
    holder = TruthWriterLease(storage, role="provider")
    assert holder.acquire()["status"] == "acquired"
    state = writer_lease_module._PROCESS_REGISTRY[holder._registry_key]
    real_handle = state.handle
    state.handle = _CloseRaisesKeepLocked(real_handle)
    try:
        with pytest.raises(OSError, match="injected lease handle close failure"):
            holder.release()
        assert holder.acquired is True
        retained = writer_lease_module._PROCESS_REGISTRY.get(holder._registry_key)
        assert retained is not None
        assert retained.holders == 1
        assert _child_acquire_status(storage) == "STATUS:busy"
    finally:
        current = writer_lease_module._PROCESS_REGISTRY.get(holder._registry_key)
        if current is not None:
            current.handle = real_handle
        if holder.acquired:
            holder.release()
    assert holder.acquired is False
    assert holder._registry_key not in writer_lease_module._PROCESS_REGISTRY
    assert len(writer_lease_module._PROCESS_REGISTRY) == baseline
    assert _child_acquire_status(storage) == "STATUS:acquired"


def test_last_connection_pin_retains_authority_if_handle_close_fails(tmp_path):
    """A last pin must remain retryable until the OS handle really closes."""

    storage = tmp_path / "scope-recall"
    baseline = len(writer_lease_module._PROCESS_REGISTRY)
    owner = TruthWriterLease(storage, role="provider")
    pin = TruthWriterLease(storage, role="truth_connection")
    assert owner.acquire()["status"] == "acquired"
    assert pin.acquire()["status"] == "acquired"
    state = writer_lease_module._PROCESS_REGISTRY[owner._registry_key]
    assert state.holders == 1
    assert state.connection_pins == 1

    owner.release()
    assert state.holders == 0
    assert state.connection_pins == 1
    real_handle = state.handle
    state.handle = _CloseRaisesKeepLocked(real_handle)
    try:
        with pytest.raises(OSError, match="injected lease handle close failure"):
            pin.release()
        assert pin.acquired is True
        assert pin._pin_only is True
        retained = writer_lease_module._PROCESS_REGISTRY.get(pin._registry_key)
        assert retained is state
        assert retained.holders == 0
        assert retained.connection_pins == 1
        assert _child_acquire_status(storage) == "STATUS:busy"
    finally:
        current = writer_lease_module._PROCESS_REGISTRY.get(pin._registry_key)
        if current is not None:
            current.handle = real_handle
            # Repair the intentionally exposed pre-fix state so RED runs do not
            # leak the process-global lock into later tests.
            if current.connection_pins < 1:
                current.connection_pins = 1
            if not pin.acquired:
                pin._acquired = True
                pin._acquired_pid = os.getpid()
                pin._pin_only = True
        if pin.acquired:
            pin.release()
    assert pin._registry_key not in writer_lease_module._PROCESS_REGISTRY
    assert len(writer_lease_module._PROCESS_REGISTRY) == baseline
    assert _child_acquire_status(storage) == "STATUS:acquired"


def test_last_release_clears_registry_if_handle_closes_then_raises(tmp_path):
    storage = tmp_path / "scope-recall"
    baseline = len(writer_lease_module._PROCESS_REGISTRY)
    holder = TruthWriterLease(storage, role="provider")
    assert holder.acquire()["status"] == "acquired"
    state = writer_lease_module._PROCESS_REGISTRY[holder._registry_key]
    real_handle = state.handle
    state.handle = _CloseThenRaise(real_handle)
    try:
        with pytest.raises(
            OSError, match="injected lease handle close after authority gone"
        ):
            holder.release()
        assert holder.acquired is False
        assert holder._registry_key not in writer_lease_module._PROCESS_REGISTRY
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline
        assert _child_acquire_status(storage) == "STATUS:acquired"
    finally:
        if holder.acquired:
            current = writer_lease_module._PROCESS_REGISTRY.get(holder._registry_key)
            if current is not None:
                current.handle = real_handle
            try:
                holder.release()
            except Exception:
                pass
        elif not real_handle.closed:
            try:
                real_handle.close()
            except Exception:
                pass


def test_provider_authorizer_close_failure_retains_published_connection(
    tmp_path, monkeypatch
):
    import scope_recall.provider as provider_module

    _write_config(tmp_path, {"vector": {"enabled": False}})
    storage = tmp_path / "scope-recall"
    provider = _provider()
    live_module = sys.modules[type(provider).__module__]
    captured: dict = {}
    real_connect = live_module.connect_truth_database

    def wrapping_connect(db_path, *args, **kwargs):
        conn = real_connect(db_path, *args, **kwargs)
        mode = str(kwargs.get("mode") or "")
        if mode in {"rw", "rwc"} and Path(db_path).name == "memory.sqlite3":
            captured["real_conn"] = conn
            proxy = _TruthCloseRaises(conn)
            captured["proxy"] = proxy
            return proxy
        return conn

    for module in (live_module, provider_module):
        monkeypatch.setattr(module, "connect_truth_database", wrapping_connect)
        monkeypatch.setattr(
            module,
            "install_activation_lease_authorizer",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("injected provider authorizer failure")
            ),
        )

    try:
        with pytest.raises(
            sqlite3.OperationalError, match="injected sqlite close failure"
        ) as caught:
            _initialize(provider, tmp_path, "authorizer-close")
        context = caught.value.__context__
        assert isinstance(context, RuntimeError)
        assert "injected provider authorizer failure" in str(context)
        assert provider._conn is captured["proxy"]
        assert provider._truth_writer_role == "unknown"
        assert provider._truth_writer_lease is not None
        assert provider._truth_writer_lease.acquired is True
        assert _child_acquire_status(storage) == "STATUS:busy"
        real_conn = captured["real_conn"]
        real_conn.execute(
            "CREATE TABLE IF NOT EXISTS provider_authorizer_leak_probe(label TEXT)"
        )
        real_conn.execute(
            "INSERT INTO provider_authorizer_leak_probe(label) VALUES ('live')"
        )
        real_conn.commit()
        captured["proxy"].close = real_conn.close
        assert provider._cleanup_failed_writer_initialization() is True
        assert provider._conn is None
        assert provider._truth_writer_lease is None
        assert _child_acquire_status(storage) == "STATUS:acquired"
    finally:
        _release_close_failure_handles(
            {
                "real_conn": captured.get("real_conn"),
                "lease": provider._truth_writer_lease,
            }
        )
        try:
            provider.shutdown()
        except Exception:
            pass


def test_startup_backfill_close_failure_retains_connection_and_authority(
    tmp_path, monkeypatch
):
    import scope_recall.provider as provider_module

    _write_config(tmp_path, {"vector": {"enabled": False}})
    storage = tmp_path / "scope-recall"
    provider = _provider()
    live_module = sys.modules[type(provider).__module__]
    captured: dict = {}
    real_connect = live_module.connect_truth_database

    def wrapping_connect(db_path, *args, **kwargs):
        conn = real_connect(db_path, *args, **kwargs)
        mode = str(kwargs.get("mode") or "")
        if mode in {"rw", "rwc"} and Path(db_path).name == "memory.sqlite3":
            captured["real_conn"] = conn
            proxy = _TruthCloseRaises(conn)
            captured["proxy"] = proxy
            return proxy
        return conn

    for module in (live_module, provider_module):
        monkeypatch.setattr(module, "connect_truth_database", wrapping_connect)
        monkeypatch.setattr(
            module,
            "backfill_untracked_memory_freshness",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                sqlite3.OperationalError("injected freshness backfill failure")
            ),
        )

    try:
        with pytest.raises(
            sqlite3.OperationalError, match="injected sqlite close failure"
        ) as caught:
            _initialize(provider, tmp_path, "backfill-close")
        context = caught.value.__context__
        assert isinstance(context, sqlite3.OperationalError)
        assert "injected freshness backfill failure" in str(context)
        assert provider._conn is captured["proxy"]
        assert provider._truth_writer_role == "unknown"
        assert provider._truth_writer_lease is not None
        assert provider._truth_writer_lease.acquired is True
        assert _child_acquire_status(storage) == "STATUS:busy"
        captured["proxy"].close = captured["real_conn"].close
        assert provider._cleanup_failed_writer_initialization() is True
        assert provider._conn is None
        assert _child_acquire_status(storage) == "STATUS:acquired"
    finally:
        _release_close_failure_handles(
            {
                "real_conn": captured.get("real_conn"),
                "lease": provider._truth_writer_lease,
            }
        )
        try:
            provider.shutdown()
        except Exception:
            pass


def test_quarantine_and_recovery_close_failure_retains_conn_without_reopen(
    tmp_path, monkeypatch
):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    storage = tmp_path / "scope-recall"
    provider = _provider()
    _initialize(provider, tmp_path, "recovery-close")
    real_conn = provider._conn
    assert real_conn is not None
    reopen_calls: list[str] = []
    real_open = provider._open_runtime_connection

    def tracking_open():
        reopen_calls.append("reopen")
        return real_open()

    provider._open_runtime_connection = tracking_open
    monkeypatch.setattr(provider, "_sqlite_write_probe", lambda _conn: False)

    class _CloseFailsUntilRestored:
        def __init__(self, inner: sqlite3.Connection) -> None:
            self._inner = inner
            self.close_calls = 0
            self.fail_close = True

        def close(self) -> None:
            self.close_calls += 1
            if self.fail_close:
                raise sqlite3.OperationalError("injected recovery close failure")
            return self._inner.close()

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    proxy = _CloseFailsUntilRestored(real_conn)
    provider._conn = proxy
    try:
        provider._quarantine_sqlite_connection(proxy, "injected quarantine")
        assert provider._conn is proxy
        assert provider._truth_writer_role == "unknown"
        assert provider._truth_writes_blocked() is True
        assert reopen_calls == []
        assert _child_acquire_status(storage) == "STATUS:busy"

        provider._truth_writer_role = "owner"
        receipt = provider._recover_sqlite_connection_after_error("injected recovery")
        assert provider._conn is proxy
        assert provider._truth_writer_role == "unknown"
        assert provider._truth_writes_blocked() is True
        assert reopen_calls == []
        assert receipt.get("reopened") is not True
        assert _child_acquire_status(storage) == "STATUS:busy"

        proxy.fail_close = False
        provider._quarantine_sqlite_connection(proxy, "retry quarantine")
        assert provider._conn is None
        assert _sqlite_connection_closed(real_conn)
        assert provider._cleanup_failed_writer_initialization() is True
        assert provider._truth_writer_lease is None
        assert _child_acquire_status(storage) == "STATUS:acquired"
    finally:
        proxy.fail_close = False
        if provider._conn is proxy:
            try:
                proxy.close()
            except Exception:
                pass
            provider._conn = None
        try:
            provider.shutdown()
        except Exception:
            pass


def test_programming_error_during_close_retains_published_connection(tmp_path):
    """The exception class alone is not proof that a pager is already closed."""

    _write_config(tmp_path, {"vector": {"enabled": False}})
    storage = tmp_path / "scope-recall"
    provider = _provider()
    _initialize(provider, tmp_path, "programming-close")
    real_conn = provider._conn
    assert real_conn is not None

    class _ProgrammingCloseProxy:
        def __init__(self, inner: sqlite3.Connection) -> None:
            self._inner = inner
            self.fail_close = True

        def close(self) -> None:
            if self.fail_close:
                raise sqlite3.ProgrammingError("injected still-open close failure")
            self._inner.close()

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    proxy = _ProgrammingCloseProxy(real_conn)
    provider._conn = proxy
    try:
        closed = provider._close_published_connection(
            proxy,
            context="injected ProgrammingError close",
            reraise=False,
        )
        assert closed is False
        assert provider._conn is proxy
        assert provider._truth_writer_role == "unknown"
        proxy.execute("CREATE TABLE IF NOT EXISTS retained_close_probe(value TEXT)")
        proxy.execute("INSERT INTO retained_close_probe(value) VALUES ('still-open')")
        proxy.commit()
        assert _child_acquire_status(storage) == "STATUS:busy"
    finally:
        if provider._conn is None:
            provider._conn = proxy
        proxy.fail_close = False
        provider._close_published_connection(
            proxy,
            context="ProgrammingError close retry",
            reraise=False,
        )
        try:
            provider.shutdown()
        except Exception:
            pass
    assert provider._conn is None
    assert _child_acquire_status(storage) == "STATUS:acquired"


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(os, "fork"),
    reason="POSIX-only actual fork",
)
def test_fork_child_fresh_lease_does_not_join_parent_owner(tmp_path):
    storage = tmp_path / "scope-recall"
    parent = TruthWriterLease(storage, role="provider")
    assert parent.acquire()["status"] == "acquired"
    read_fd = write_fd = None
    child_pid = 0
    try:
        read_fd, write_fd = os.pipe()
        child_pid = os.fork()
        if child_pid == 0:
            os.close(read_fd)
            try:
                child = TruthWriterLease(storage, role="provider")
                result = child.acquire()
                payload = json.dumps(
                    {
                        "status": result.get("status"),
                        "scope": result.get("scope"),
                        "acquired": child.acquired,
                    }
                )
                os.write(write_fd, payload.encode("utf-8"))
            except Exception as exc:
                os.write(
                    write_fd,
                    json.dumps({"error": type(exc).__name__, "detail": str(exc)}).encode(
                        "utf-8"
                    ),
                )
            finally:
                os.close(write_fd)
                os._exit(0)
        os.close(write_fd)
        write_fd = None
        chunks: list[bytes] = []
        while True:
            data = os.read(read_fd, 4096)
            if not data:
                break
            chunks.append(data)
        os.close(read_fd)
        read_fd = None
        os.waitpid(child_pid, 0)
        child_pid = 0
        child_result = json.loads(b"".join(chunks).decode("utf-8"))
        assert "error" not in child_result, child_result
        assert child_result.get("status") == "busy"
        assert child_result.get("scope") != "same_process_shared"
        assert child_result.get("acquired") is False
    finally:
        if child_pid:
            try:
                os.waitpid(child_pid, 0)
            except OSError:
                pass
        if write_fd is not None:
            os.close(write_fd)
        if read_fd is not None:
            os.close(read_fd)
        if parent.acquired:
            parent.release()
    assert _child_acquire_status(storage) == "STATUS:acquired"


def _restore_writer_lease_holder(snapshot: dict) -> None:
    holder = sys.modules[writer_lease_module._SHARED_STATE_NAME]
    holder.lock = snapshot["lock"]
    holder.registry = snapshot["registry"]
    holder.pid = snapshot["pid"]
    holder.poisoned = snapshot["poisoned"]
    writer_lease_module._PROCESS_REGISTRY_LOCK = snapshot["lock"]
    writer_lease_module._PROCESS_REGISTRY = snapshot["registry"]
    alias = sys.modules.get("scope_recall.writer_lease")
    if alias is not None:
        alias._PROCESS_REGISTRY_LOCK = snapshot["lock"]
        alias._PROCESS_REGISTRY = snapshot["registry"]


def test_pid_mismatch_closes_inherited_handle_and_does_not_join(tmp_path, monkeypatch):
    storage = tmp_path / "scope-recall"
    holder = sys.modules[writer_lease_module._SHARED_STATE_NAME]
    snapshot = {
        "lock": holder.lock,
        "registry": holder.registry,
        "pid": getattr(holder, "pid", os.getpid()),
        "poisoned": getattr(holder, "poisoned", False),
    }
    closed: list[str] = []
    os_lock_calls: list[int] = []
    real_try = writer_lease_module._try_lock_exclusive_nonblocking

    class _FakeInheritedHandle:
        def close(self) -> None:
            closed.append("inherited")

    def tracking_try(handle):
        os_lock_calls.append(1)
        return real_try(handle)

    monkeypatch.setattr(
        writer_lease_module, "_try_lock_exclusive_nonblocking", tracking_try
    )
    alias = sys.modules.get("scope_recall.writer_lease")
    if alias is not None:
        monkeypatch.setattr(alias, "_try_lock_exclusive_nonblocking", tracking_try)

    probe = TruthWriterLease(storage, role="provider")
    inherited_state = writer_lease_module._ProcessLeaseState(_FakeInheritedHandle())
    inherited_state.holders = 1
    try:
        holder.registry = {probe._registry_key: inherited_state}
        holder.lock = threading.Lock()
        holder.pid = os.getpid() + 7919
        holder.poisoned = False
        writer_lease_module._PROCESS_REGISTRY = holder.registry
        writer_lease_module._PROCESS_REGISTRY_LOCK = holder.lock

        inherited = TruthWriterLease(storage, role="provider")
        inherited._acquired = True
        inherited._acquired_pid = os.getpid() + 7919
        assert inherited.acquired is False

        first_lock = holder.lock
        first_registry = holder.registry
        writer_lease_module._after_fork_in_child()
        writer_lease_module._after_fork_in_child()
        assert holder.lock is not first_lock
        assert holder.registry is not first_registry
        assert holder.lock is writer_lease_module._PROCESS_REGISTRY_LOCK
        assert holder.registry is writer_lease_module._PROCESS_REGISTRY
        assert closed == ["inherited"]

        lease = TruthWriterLease(storage, role="provider")
        result = lease.acquire()
        try:
            assert result["status"] == "acquired"
            assert result.get("scope") != "same_process_shared"
            assert os_lock_calls == [1]
            assert probe._registry_key not in first_registry or inherited_state.holders == 1
        finally:
            if lease.acquired:
                lease.release()
    finally:
        _restore_writer_lease_holder(snapshot)

    boom_closed: list[str] = []

    class _BoomInheritedHandle:
        def close(self) -> None:
            boom_closed.append("boom")
            raise OSError("injected inherited handle close failure")

    try:
        holder.registry = {
            probe._registry_key: writer_lease_module._ProcessLeaseState(
                _BoomInheritedHandle()
            )
        }
        holder.lock = threading.Lock()
        holder.pid = os.getpid() + 4242
        holder.poisoned = False
        writer_lease_module._PROCESS_REGISTRY = holder.registry
        writer_lease_module._PROCESS_REGISTRY_LOCK = holder.lock
        poisoned = TruthWriterLease(storage, role="provider")
        busy = poisoned.acquire()
        assert boom_closed == ["boom"]
        assert busy["status"] == "busy"
        assert busy.get("scope") != "same_process_shared"
        assert getattr(holder, "poisoned", False) is True
        assert poisoned.acquired is False
    finally:
        _restore_writer_lease_holder(snapshot)
