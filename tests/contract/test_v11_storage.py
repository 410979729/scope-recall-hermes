"""Real SQLite contract tests, including transaction ownership fault injection.

Sources are synthetic. These tests do not claim semantic model/host acceptance.
"""
from dataclasses import replace
import hashlib
import importlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
import time

import pytest

from scope_recall.contracts import ContractError, InstanceBinding, TrustedContext
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.schema import SCHEMA_VERSION
from scope_recall.core.storage import SQLiteStorage
import scope_recall.core.storage as storage_module
from v11_support import context, source_event


NOW = "2026-09-06T06:00:00Z"


@pytest.fixture
def store(tmp_path):
    ctx = context(tmp_path / "TEST-target")
    storage = SQLiteStorage(ctx.binding)
    storage.initialize()
    return storage, ctx


def put(tx, key="TEST-source/1", **values):
    return tx.put_source(source_event(source_event_key=key, **values), scope_id="TEST-scope", persisted_at=NOW)


def snapshot(storage, ctx):
    with storage.read(ctx) as tx:
        return tx.status()


class InjectedFailure(RuntimeError):
    pass


class FaultConnection:
    """Test-only proxy around a real production-factory SQLite connection."""
    def __init__(self, conn, *, operation=None, occurrence=1, rollback_fail=False, close_fail=False):
        self.conn = conn
        self.operation = operation
        self.occurrence = occurrence
        self.rollback_fail = rollback_fail
        self.close_fail = close_fail
        self.trace = []
        self.closed = False

    @property
    def in_transaction(self):
        return self.conn.in_transaction

    def execute(self, sql, parameters=()):
        self.trace.append(sql)
        if self.operation and sql.startswith(self.operation):
            self.occurrence -= 1
            if self.occurrence == 0:
                raise InjectedFailure(self.operation)
        return self.conn.execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.conn.executemany(sql, parameters)

    def commit(self):
        self.trace.append("COMMIT")
        if self.operation == "COMMIT":
            raise InjectedFailure("commit failed before commit")
        return self.conn.commit()

    def rollback(self):
        self.trace.append("ROLLBACK")
        if self.rollback_fail:
            raise InjectedFailure("rollback failed")
        return self.conn.rollback()

    def close(self):
        self.trace.append("CLOSE")
        if self.close_fail:
            raise InjectedFailure("close failed")
        self.conn.close()
        self.closed = True


def inject(monkeypatch, **faults):
    actual = storage_module.connect_truth_database
    opened = []
    def factory(*args, **kwargs):
        conn = FaultConnection(actual(*args, **kwargs), **faults)
        opened.append(conn)
        return conn
    monkeypatch.setattr(storage_module, "connect_truth_database", factory)
    return actual, opened


def test_import_and_composition_have_no_storage_host_or_model_side_effects(tmp_path, monkeypatch):
    ctx = context(tmp_path / "TEST-not-created")
    def forbidden(*args, **kwargs):
        raise AssertionError("constructor/import opened storage")
    monkeypatch.setattr(storage_module, "connect_truth_database", forbidden)
    before = set(sys.modules)
    importlib.reload(importlib.import_module("scope_recall.core.composition"))
    core = MemoryCore(CoreConfig(ctx.binding))
    assert core.config.binding == ctx.binding
    assert not ctx.binding.data_directory.exists()
    loaded = set(sys.modules) - before
    assert not any(name.startswith(("hermes", "gateway", "run_agent", "lancedb", "torch", "scope_recall.provider")) for name in loaded)


def test_independent_initialize_and_exact_restricted_roundtrip(store):
    storage, ctx = store
    assert storage.initialize().schema_version == SCHEMA_VERSION
    text = "\n  TEST  原始文字、H100/H200 与文档.svg\n"
    with storage.write(ctx) as tx:
        written = put(tx, content=text)
        tx.enqueue_source(written.ref, written.revision, work_type="consolidate", available_at=NOW)
    with storage.read(ctx) as tx:
        saved = tx.source(written.ref, 1)
        assert saved.event["content"] == text
        assert saved.event["origin"] == "human_direct"
        assert saved.content_sha256 == hashlib.sha256(text.encode()).hexdigest()
        assert tx.status().sources == tx.status().pending_work == 1
        assert tx.status().memory_epoch == 1
    assert not hasattr(tx, "connection") and not hasattr(tx, "execute") and not hasattr(tx, "commit")
    with pytest.raises(ContractError, match="transaction_closed"):
        tx.source(written.ref, 1)


def test_scopes_filtered_and_foreign_binding_refused(tmp_path):
    binding = InstanceBinding("TEST-agent", "TEST-install", tmp_path / "TEST-scopes", frozenset({"TEST-private", "TEST-group"}), True)
    storage = SQLiteStorage(binding)
    storage.initialize()
    private = TrustedContext(binding, "TEST-session", frozenset({"TEST-private"}), "human_direct")
    group = replace(private, allowed_scope_ids=frozenset({"TEST-group"}))
    with storage.write(private) as tx:
        row = tx.put_source(source_event(), scope_id="TEST-private", persisted_at=NOW)
    with storage.read(group) as tx:
        assert tx.source(row.ref, 1) is None
        assert tx.status().sources == 0
    with storage.write(group) as tx:
        with pytest.raises(ContractError, match="ACCESS_DENIED"):
            tx.put_source(source_event(), scope_id="TEST-private", persisted_at=NOW)
    stranger = replace(private, binding=replace(binding, installation_id="TEST-other"))
    with pytest.raises(ContractError, match="IDENTITY_UNBOUND"), storage.read(stranger):
        pass


@pytest.mark.parametrize("field", ["project_id", "branch_id"])
def test_context_filter_precedes_source_search_limit_and_status(store, field):
    storage, ctx = store
    owner = replace(ctx, project_id="TEST-A", branch_id="TEST-main")
    other = replace(owner, **{field: "TEST-other"})
    with storage.write(owner) as tx:
        foreign = put(tx, "TEST-foreign", content="TEST-only-owner 内容")
        tx.index_source(foreign.ref, 1)
        tx.enqueue_source(foreign.ref, 1, work_type="embed", available_at=NOW)
    with storage.read(other) as tx:
        assert tx.status().sources == tx.status().pending_work == 0
        assert tx.source(foreign.ref, 1) is None
        assert tx.search_sources("TEST-only-owner", history=True) == ()
    with storage.write(other) as tx:
        local = put(tx, "TEST-local", content="TEST-only-owner 当前项目也包含此词")
        tx.index_source(local.ref, 1)
    with storage.read(other) as tx:
        assert [r.ref for r in tx.search_sources("TEST-only-owner", limit=1)] == [local.ref]
        assert tx.status().sources == 1 and tx.status().pending_work == 0


def test_global_sources_remain_readable_without_exposing_project_sources(store):
    storage, ctx = store
    global_ctx = replace(ctx, project_id=None, branch_id=None)
    with storage.write(global_ctx) as tx:
        source = put(tx)
        tx.enqueue_source(source.ref, 1, work_type="embed", available_at=NOW)
    with storage.read(replace(ctx, project_id="TEST-A", branch_id="TEST-main")) as tx:
        assert tx.source(source.ref, 1) is not None
        assert tx.status().sources == tx.status().pending_work == 1


@pytest.mark.parametrize("change", ["agent", "installation", "test_mode", "scopes", "copy"])
def test_identity_drift_and_directory_copy_do_not_inherit_authority(store, tmp_path, change):
    storage, ctx = store
    values = {"agent": {"agent_id": "TEST-other"}, "installation": {"installation_id": "TEST-other"},
              "test_mode": {"test_mode": False}, "scopes": {"scope_ids": frozenset({"TEST-new-scope"})}}
    if change == "copy":
        destination = tmp_path / "TEST-copied"
        shutil.copytree(storage.binding.data_directory, destination)
        altered = replace(ctx.binding, data_directory=destination)
    else:
        altered = replace(ctx.binding, **values[change])
    replacement = SQLiteStorage(altered)
    with pytest.raises(ContractError, match="IDENTITY_UNBOUND"):
        replacement.initialize()
    assert snapshot(storage, ctx).sources == 0


def test_directory_and_binding_cannot_be_reassigned(store):
    storage, ctx = store
    with pytest.raises(AttributeError):
        storage.path = Path("TEST-other")
    with pytest.raises(AttributeError):
        storage.binding = ctx.binding


def test_read_only_and_foreign_key_guard_are_real_sqlite(store, monkeypatch):
    storage, ctx = store
    actual, opened = inject(monkeypatch)
    with storage.read(ctx) as tx:
        assert opened[-1].conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert opened[-1].conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            opened[-1].conn.execute("UPDATE instance_meta SET memory_epoch=999")
        with pytest.raises(ContractError, match="read_only"):
            put(tx)
    assert snapshot(storage, ctx).memory_epoch == 0
    with storage.write(ctx):
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            opened[-1].conn.execute("INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,available_at) VALUES ('embed','TEST',1,'UNAUTHORIZED',?)", (NOW,))


def test_unknown_schema_and_old_store_never_auto_upgrade(store, tmp_path):
    storage, ctx = store
    conn = sqlite3.connect(storage.path)
    conn.execute("PRAGMA user_version=9999")
    conn.close()
    before = storage.path.read_bytes()
    for action in (storage.initialize, lambda: snapshot(storage, ctx)):
        with pytest.raises(ContractError, match="SCHEMA_UNSUPPORTED"):
            action()
    assert storage.path.read_bytes() == before
    target = tmp_path / "TEST-legacy"
    target.mkdir()
    old = sqlite3.connect(target / "memory.sqlite3")
    old.execute("CREATE TABLE memories(id TEXT PRIMARY KEY,content TEXT)")
    old.execute("INSERT INTO memories VALUES ('TEST-old','TEST keep unchanged')")
    old.commit(); old.close()
    legacy = SQLiteStorage(replace(ctx.binding, data_directory=target))
    before = legacy.path.read_bytes()
    with pytest.raises(ContractError, match="SCHEMA_UNSUPPORTED"):
        legacy.initialize()
    assert legacy.path.read_bytes() == before


def test_missing_database_read_does_not_create_it(tmp_path):
    ctx = context(tmp_path / "TEST-absent")
    with pytest.raises(sqlite3.OperationalError), SQLiteStorage(ctx.binding).read(ctx):
        pass
    assert not ctx.binding.data_directory.exists()


def test_owned_failure_rolls_back_source_work_and_epoch(store):
    storage, ctx = store
    with pytest.raises(InjectedFailure, match="application"):
        with storage.write(ctx) as tx:
            row = put(tx)
            tx.enqueue_source(row.ref, 1, work_type="embed", available_at=NOW)
            raise InjectedFailure("application")
    status = snapshot(storage, ctx)
    assert status.sources == status.pending_work == status.memory_epoch == 0
    with storage.write(ctx) as tx:
        put(tx)
    assert snapshot(storage, ctx).sources == 1


@pytest.mark.parametrize("point", ["BEGIN IMMEDIATE", "INSERT INTO source_events", "INSERT INTO work_items", "COMMIT"])
def test_owned_transaction_failures_discard_connection_and_allow_next_write(store, monkeypatch, point):
    storage, ctx = store
    actual, opened = inject(monkeypatch, operation=point)
    with pytest.raises(InjectedFailure):
        with storage.write(ctx) as tx:
            row = put(tx)
            tx.enqueue_source(row.ref, 1, work_type="consolidate", available_at=NOW)
    assert opened[-1].closed
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[-1].conn.execute("SELECT 1")
    monkeypatch.setattr(storage_module, "connect_truth_database", actual)
    status = snapshot(storage, ctx)
    assert status.sources == status.memory_epoch == status.pending_work == 0
    with storage.write(ctx) as tx:
        put(tx, "TEST-next")
    assert snapshot(storage, ctx).sources == 1


def test_borrowed_failure_rolls_back_only_nested_changes(store):
    storage, ctx = store
    with storage.write(ctx) as tx:
        first = put(tx, "TEST-outer-1")
        with pytest.raises(InjectedFailure):
            with tx.savepoint() as borrowed:
                second = put(borrowed, "TEST-inner")
                borrowed.enqueue_source(second.ref, 1, work_type="embed", available_at=NOW)
                raise InjectedFailure("inner")
        assert tx.source(first.ref, 1)
        assert tx.source(second.ref, 1) is None
        put(tx, "TEST-outer-2")
    status = snapshot(storage, ctx)
    assert status.sources == status.memory_epoch == 2
    assert status.pending_work == 0


def test_commit_failure_after_successful_borrow_never_releases_savepoint_twice(store, monkeypatch):
    storage, ctx = store
    actual, opened = inject(monkeypatch, operation="COMMIT")
    with pytest.raises(InjectedFailure, match="commit"):
        with storage.write(ctx) as tx:
            with tx.savepoint():
                put(tx)
    assert sum(s.startswith("RELEASE SAVEPOINT") for s in opened[-1].trace) == 1
    assert not any(s.startswith("ROLLBACK TO") for s in opened[-1].trace)
    monkeypatch.setattr(storage_module, "connect_truth_database", actual)
    assert snapshot(storage, ctx).sources == 0


@pytest.mark.parametrize("point", ["SAVEPOINT core_1", "RELEASE SAVEPOINT core_1"])
def test_savepoint_begin_and_release_failure_can_be_handled_by_owner(store, monkeypatch, point):
    storage, ctx = store
    actual, opened = inject(monkeypatch, operation=point)
    with storage.write(ctx) as tx:
        put(tx, "TEST-outer")
        with pytest.raises(InjectedFailure):
            with tx.savepoint():
                put(tx, "TEST-inner")
        put(tx, "TEST-after")
    monkeypatch.setattr(storage_module, "connect_truth_database", actual)
    assert snapshot(storage, ctx).sources == 2
    if point.startswith("SAVEPOINT"):
        assert not any(s.startswith(("ROLLBACK TO", "RELEASE")) for s in opened[-1].trace)


def test_savepoint_cleanup_failure_poisoned_owner_cannot_commit(store, monkeypatch):
    storage, ctx = store
    actual, opened = inject(monkeypatch, operation="ROLLBACK TO SAVEPOINT")
    with pytest.raises(ContractError, match="transaction_closed"):
        with storage.write(ctx) as tx:
            put(tx, "TEST-outer")
            with pytest.raises(InjectedFailure, match="original") as error:
                with tx.savepoint():
                    put(tx, "TEST-inner")
                    raise InjectedFailure("original")
            assert isinstance(error.value.__cause__, InjectedFailure)
            assert "cleanup failed" in error.value.__notes__[0]
    assert "COMMIT" not in opened[-1].trace
    monkeypatch.setattr(storage_module, "connect_truth_database", actual)
    assert snapshot(storage, ctx).sources == 0
    with storage.write(ctx) as tx:
        put(tx, "TEST-healthy")


def test_rollback_failure_keeps_primary_error_and_close_discards_transaction(store, monkeypatch):
    storage, ctx = store
    actual, opened = inject(monkeypatch, rollback_fail=True)
    with pytest.raises(InjectedFailure, match="primary") as error:
        with storage.write(ctx) as tx:
            put(tx)
            raise InjectedFailure("primary")
    assert error.value.__cause__.args == ("rollback failed",)
    assert opened[-1].closed
    monkeypatch.setattr(storage_module, "connect_truth_database", actual)
    assert snapshot(storage, ctx).sources == 0
    with storage.write(ctx) as tx:
        put(tx, "TEST-next")


def test_close_failure_is_retained_and_retried_before_another_open(store, monkeypatch):
    storage, ctx = store
    actual, opened = inject(monkeypatch, close_fail=True)
    with pytest.raises(InjectedFailure, match="primary") as error:
        with storage.write(ctx) as tx:
            put(tx)
            raise InjectedFailure("primary")
    assert error.value.cleanup_errors[0].args == ("close failed",)
    with pytest.raises(ContractError, match="connection_cleanup"):
        snapshot(storage, ctx)
    assert len(opened) == 1
    opened[0].close_fail = False
    monkeypatch.setattr(storage_module, "connect_truth_database", actual)
    assert snapshot(storage, ctx).sources == 0
    assert opened[0].closed
    with storage.write(ctx) as tx:
        put(tx, "TEST-next")


def test_remaining_budget_bounds_busy_wait_and_zero_refuses_before_open(store, monkeypatch):
    storage, ctx = store
    actual, opened = inject(monkeypatch)
    with storage.write(ctx, remaining_seconds=0.037):
        assert opened[-1].conn.execute("PRAGMA busy_timeout").fetchone()[0] <= 37
    count = len(opened)
    for remaining in (0, -1, float("nan"), float("inf"), True):
        with pytest.raises(ContractError, match="DEADLINE_EXCEEDED"), storage.write(ctx, remaining_seconds=remaining):
            pass
    assert len(opened) == count


def test_source_query_uses_authorized_identity_index(store, monkeypatch):
    storage, ctx = store
    actual, opened = inject(monkeypatch)
    with storage.read(ctx):
        plan = opened[-1].conn.execute("EXPLAIN QUERY PLAN SELECT * FROM source_events WHERE event_id=? AND source_revision=? AND scope_id=? AND read_blocked=0", ("TEST",1,"TEST-scope")).fetchall()
        assert any("USING INDEX" in row[3] for row in plan)


def test_source_replay_and_versions_preserve_occurrences(store):
    storage, ctx = store
    with storage.write(ctx) as tx:
        first = put(tx)
        assert put(tx, recorded_at=NOW).disposition == "duplicate"
        other = put(tx, "TEST-second-occurrence")
        assert other.ref != first.ref
        newer = put(tx, source_revision=2, content="TEST updated source")
        assert newer.ref == first.ref and newer.revision == 2
        with pytest.raises(ContractError, match="VERSION_CONFLICT"):
            put(tx, content="TEST conflicting replay")
    with storage.read(ctx) as tx:
        assert tx.source(first.ref, 1).event["content"] == source_event()["content"]
        assert tx.source(first.ref, 2).event["content"] == "TEST updated source"
        assert tx.status().sources == 3


def test_test_dataset_is_rejected_by_production_binding(tmp_path):
    ctx = context(tmp_path / "TEST-production-mode", test_mode=False)
    storage = SQLiteStorage(ctx.binding)
    storage.initialize()
    with storage.write(ctx) as tx:
        with pytest.raises(ContractError, match="dataset_id"):
            put(tx, dataset_id="SYNTHETIC_TEST_ONLY")
    assert snapshot(storage, ctx).sources == 0


def test_a_long_read_does_not_block_the_writer(store):
    """Under the rollback journal a two-second read left a writer "database is
    locked" after its whole timeout, so any operator query could fail a worker
    pass.  The store runs in WAL mode, where readers and the writer coexist."""
    storage, ctx = store
    with sqlite3.connect(storage.path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    holding = threading.Event()

    def long_read():
        with storage.read(ctx) as tx:
            tx.status()
            holding.set()
            time.sleep(1.5)

    reader = threading.Thread(target=long_read)
    reader.start()
    assert holding.wait(5)
    started = time.monotonic()
    with storage.write(ctx) as tx:
        put(tx)
    assert time.monotonic() - started < 1.0
    reader.join()
    assert snapshot(storage, ctx).sources == 1


def test_a_heavy_upgrade_waits_for_a_caller_with_the_budget():
    """The 1109 step rebuilds the lexical index: 95 s for 5.2 million rows on a
    1.4 GB store.  A hook's few seconds cannot carry that; the worker's pass
    and the installer can, and a small store is brought forward by anyone."""
    from scope_recall.core.storage import HEAVY_UPGRADE_BYTES, HEAVY_UPGRADE_SECONDS, upgrade_fits
    assert upgrade_fits(HEAVY_UPGRADE_BYTES * 10, None)
    assert upgrade_fits(HEAVY_UPGRADE_BYTES * 10, HEAVY_UPGRADE_SECONDS)
    assert not upgrade_fits(HEAVY_UPGRADE_BYTES * 10, 6.0)
    assert upgrade_fits(HEAVY_UPGRADE_BYTES - 1, 1.0)


def test_source_versions_carry_an_integer_identity_and_the_lexical_index_names_them_by_it(store):
    storage, ctx = store
    with storage.write(ctx) as tx:
        first = put(tx, key="TEST-id/1", content="TEST 蓝色 identity one")
        second = put(tx, key="TEST-id/2", content="TEST 蓝色 identity two")
        tx.index_source(first.ref, first.revision)
        tx.index_source(second.ref, second.revision)
    with sqlite3.connect(storage.path) as conn:
        ids = [row[0] for row in conn.execute("SELECT source_id FROM source_events ORDER BY source_id")]
        assert ids == [1, 2]
        shared = conn.execute(
            """SELECT count(*) FROM lexical_terms t JOIN lexical_postings p ON p.term_id=t.term_id
               WHERE t.term=(SELECT term FROM lexical_terms WHERE term LIKE '%蓝色%' LIMIT 1)""").fetchone()[0]
        assert shared == 2, "one term row, one posting per source"
    with storage.read(ctx) as tx:
        assert tx.source_projection_status(first.ref, first.revision)[0] == "ready"
        assert [s.ref for s in tx.search_sources("identity")] == [second.ref, first.ref] or len(tx.search_sources("identity")) == 2
