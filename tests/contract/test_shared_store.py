"""A shared store: one store several entries write, identified by a fixed id.

Storage-level only.  What an adapter puts in a source key, and what recall shows
a reader, are tested where they are built.  Sources are synthetic.
"""
from dataclasses import replace
import os
import shutil
import sqlite3
import time

import pytest

from scope_recall.contracts import ContractError, InstanceBinding, MAX_BINDING_SCOPES, MAX_SHARED_SCOPES, TrustedContext
from scope_recall.core import capture_inbox
from scope_recall.core.schema import SCHEMA_VERSION
from scope_recall.core.storage import SQLiteStorage
from v11_support import context, downgrade_store, source_event


NOW = "2026-09-22T20:00:00Z"
LATER = "2026-09-22T21:00:00Z"
SCOPES = frozenset({"TEST-scope", "TEST-group-a"})


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def utc_now(self):
        return self.now

    def monotonic(self):
        return time.monotonic()


def shared_binding(directory, scopes=SCOPES, installation_id="shared-install:TEST-0001"):
    return InstanceBinding("TEST-agent", installation_id, directory, frozenset(scopes), True, "shared")


def shared_context(binding, entry_id=None, scopes=None, session="TEST-session"):
    return TrustedContext(binding, session, frozenset(scopes or binding.scope_ids), "human_direct", entry_id=entry_id)


@pytest.fixture
def shared(tmp_path):
    binding = shared_binding(tmp_path / "TEST-shared")
    storage = SQLiteStorage(binding)
    storage.initialize()
    with storage.write(shared_context(binding)) as tx:
        tx.register_entry("tianshu", "天枢", "hermes", now=NOW)
        tx.register_entry("tianxuan", "天璇", "hermes", now=NOW)
    return storage, binding


def put(storage, ctx, key, scope="TEST-scope", content="TEST 只写给共享库的一句话。"):
    with storage.write(ctx) as tx:
        return tx.put_source(source_event(source_event_key=key, content=content), scope_id=scope, persisted_at=NOW)


def rows(storage):
    with sqlite3.connect(storage.path) as conn:
        return conn.execute("SELECT source_event_key, entry_id FROM source_events ORDER BY source_event_key").fetchall()


def meta(storage, column):
    with sqlite3.connect(storage.path) as conn:
        return conn.execute(f"SELECT {column} FROM instance_meta").fetchone()[0]


# --- a local store is what it was -------------------------------------------------------

def test_a_local_store_marks_every_row_local(tmp_path):
    ctx = context(tmp_path / "TEST-local")
    storage = SQLiteStorage(ctx.binding)
    storage.initialize()
    put(storage, ctx, "TEST-local/1")
    assert rows(storage) == [("TEST-local/1", "local")]
    assert meta(storage, "installation_kind") == "local"
    assert meta(storage, "schema_version") == SCHEMA_VERSION == 1110


def test_a_local_store_refuses_a_source_that_names_an_entry(tmp_path):
    ctx = context(tmp_path / "TEST-local")
    storage = SQLiteStorage(ctx.binding)
    storage.initialize()
    with pytest.raises(ContractError) as exc:
        put(storage, replace(ctx, entry_id="tianshu"), "TEST-local/1")
    assert (exc.value.code, exc.value.field) == ("IDENTITY_UNBOUND", "entry_unexpected")


def test_a_moved_local_store_is_refused_exactly_as_before(tmp_path):
    ctx = context(tmp_path / "TEST-local")
    SQLiteStorage(ctx.binding).initialize()
    shutil.copytree(tmp_path / "TEST-local", tmp_path / "TEST-moved")
    moved = SQLiteStorage(replace(ctx.binding, data_directory=tmp_path / "TEST-moved"))
    with pytest.raises(ContractError) as exc:
        with moved.read(replace(ctx, binding=moved.binding)):
            pass
    assert (exc.value.code, exc.value.field) == ("IDENTITY_UNBOUND", "payload")


def test_a_local_store_is_never_adopted_and_takes_no_entries(tmp_path):
    ctx = context(tmp_path / "TEST-local")
    storage = SQLiteStorage(ctx.binding)
    storage.initialize()
    with pytest.raises(ContractError) as exc:
        storage.adopt()
    assert (exc.value.code, exc.value.field) == ("ACCESS_DENIED", "local_store")
    with storage.write(ctx) as tx:
        for call in (lambda: tx.register_entry("tianshu", "天枢", "hermes", now=NOW),
                     lambda: tx.register_scopes({"TEST-other"})):
            with pytest.raises(ContractError) as exc:
                call()
            assert (exc.value.code, exc.value.field) == ("ACCESS_DENIED", "local_store")
        assert tx.entries() == {}


def test_the_two_kinds_do_not_open_each_other(tmp_path, shared):
    storage, binding = shared
    as_local = replace(binding, installation_kind="local")
    with pytest.raises(ContractError) as exc:
        with SQLiteStorage(as_local).read(shared_context(as_local)):
            pass
    assert (exc.value.code, exc.value.field) == ("IDENTITY_UNBOUND", "installation_kind")
    ctx = context(tmp_path / "TEST-local")
    SQLiteStorage(ctx.binding).initialize()
    as_shared = replace(ctx.binding, installation_kind="shared")
    with pytest.raises(ContractError) as exc:
        with SQLiteStorage(as_shared).read(shared_context(as_shared)):
            pass
    assert (exc.value.code, exc.value.field) == ("IDENTITY_UNBOUND", "installation_kind")


# --- entries -------------------------------------------------------------------------------

def test_each_source_names_the_entry_it_came_in_through(shared):
    storage, binding = shared
    put(storage, shared_context(binding, "tianshu"), "hermes:shared:tianshu:TEST-session:m:1@1")
    put(storage, shared_context(binding, "tianxuan"), "hermes:shared:tianxuan:TEST-session:m:1@1")
    assert rows(storage) == [("hermes:shared:tianshu:TEST-session:m:1@1", "tianshu"),
                             ("hermes:shared:tianxuan:TEST-session:m:1@1", "tianxuan")]


def test_a_capture_records_when_its_entry_was_last_seen(shared):
    storage, binding = shared
    put(storage, shared_context(binding, "tianshu"), "TEST-entry/1")
    with storage.read(shared_context(binding)) as tx:
        seen = tx.entries()
    assert seen["tianshu"] == {"name": "天枢", "host": "hermes", "first_seen": NOW, "last_seen": NOW}
    assert seen["tianxuan"]["last_seen"] == NOW


def test_a_shared_store_refuses_a_source_that_names_no_entry(shared):
    storage, binding = shared
    with pytest.raises(ContractError) as exc:
        put(storage, shared_context(binding), "TEST-nobody/1")
    assert (exc.value.code, exc.value.field) == ("IDENTITY_UNBOUND", "entry_required")
    assert rows(storage) == []


def test_a_shared_store_refuses_an_entry_it_never_registered(shared):
    storage, binding = shared
    with pytest.raises(ContractError) as exc:
        put(storage, shared_context(binding, "yuheng"), "TEST-stranger/1")
    assert (exc.value.code, exc.value.field) == ("IDENTITY_UNBOUND", "entry_unregistered")
    assert rows(storage) == []


def test_renaming_an_entry_keeps_when_it_first_attached(shared):
    storage, binding = shared
    with storage.write(shared_context(binding)) as tx:
        tx.register_entry("tianshu", "天枢二号", "hermes", now=LATER)
        assert tx.entries()["tianshu"] == {"name": "天枢二号", "host": "hermes", "first_seen": NOW, "last_seen": NOW}


@pytest.mark.parametrize("entry_id", ["Tianshu", "t", "1abc", "tian_shu", "x" * 33, ""])
def test_an_entry_id_is_short_lowercase_ascii(entry_id, shared):
    storage, binding = shared
    with pytest.raises(ContractError):
        TrustedContext(binding, "TEST-session", binding.scope_ids, "human_direct", entry_id=entry_id)
    with storage.write(shared_context(binding)) as tx:
        with pytest.raises(ContractError) as exc:
            tx.register_entry(entry_id, "名字", "hermes", now=NOW)
        assert exc.value.field == "entry_id"


# --- scopes --------------------------------------------------------------------------------

def test_an_entry_binds_a_subset_of_the_store_scopes(shared):
    storage, binding = shared
    narrow = replace(binding, scope_ids=frozenset({"TEST-scope"}))
    with SQLiteStorage(narrow).read(shared_context(narrow)) as tx:
        assert tx.status() is not None
    wider = replace(binding, scope_ids=frozenset({"TEST-scope", "TEST-group-b"}))
    with pytest.raises(ContractError) as exc:
        with SQLiteStorage(wider).read(shared_context(wider)):
            pass
    assert (exc.value.code, exc.value.field) == ("IDENTITY_UNBOUND", "scope_binding")


def test_registering_scopes_lets_an_entry_with_them_open_the_store(shared):
    storage, binding = shared
    with storage.write(shared_context(binding)) as tx:
        assert tx.register_scopes({"TEST-group-b", "TEST-scope"}) == 1
        assert tx.register_scopes({"TEST-group-b"}) == 0
    wider = replace(binding, scope_ids=frozenset({"TEST-scope", "TEST-group-b"}))
    with SQLiteStorage(wider).read(shared_context(wider)) as tx:
        assert tx.status() is not None


def test_the_store_never_holds_more_scopes_than_its_worker_can_bind(shared):
    storage, binding = shared
    room = MAX_SHARED_SCOPES - len(SCOPES)
    with storage.write(shared_context(binding)) as tx:
        assert tx.register_scopes({f"TEST-g{i}" for i in range(room)}) == room
        with pytest.raises(ContractError) as exc:
            tx.register_scopes({"TEST-one-too-many"})
    assert (exc.value.code, exc.value.field) == ("INPUT_INVALID", "scope_limit")


def test_a_shared_binding_carries_every_entry_s_scopes_and_a_local_one_what_it_did(tmp_path):
    over_local = frozenset(f"TEST-g{i}" for i in range(MAX_BINDING_SCOPES + 1))
    with pytest.raises(ContractError) as exc:
        InstanceBinding("TEST-agent", "TEST-install", tmp_path, over_local, True)
    assert (exc.value.code, exc.value.field) == ("IDENTITY_UNBOUND", "scope_ids")
    assert shared_binding(tmp_path, over_local).scope_ids == over_local
    with pytest.raises(ContractError):
        shared_binding(tmp_path, frozenset(f"TEST-g{i}" for i in range(MAX_SHARED_SCOPES + 1)))


# --- moving --------------------------------------------------------------------------------

def test_a_copied_shared_store_opens_only_after_adopt(tmp_path, shared):
    storage, binding = shared
    put(storage, shared_context(binding, "tianshu"), "TEST-before-move/1")
    shutil.copytree(tmp_path / "TEST-shared", tmp_path / "TEST-new-machine")
    moved_binding = replace(binding, data_directory=tmp_path / "TEST-new-machine")
    moved = SQLiteStorage(moved_binding)
    with pytest.raises(ContractError) as exc:
        with moved.read(shared_context(moved_binding)):
            pass
    assert (exc.value.code, exc.value.field) == ("IDENTITY_UNBOUND", "store_moved:run_adopt")
    # The store records a directory the way it compares one: absolute, case-folded where the
    # filesystem folds case.
    assert moved.adopt() == os.path.normcase(os.path.abspath(tmp_path / "TEST-shared"))
    put(moved, shared_context(moved_binding, "tianxuan"), "TEST-after-move/1")
    assert rows(moved) == [("TEST-after-move/1", "tianxuan"), ("TEST-before-move/1", "tianshu")]


def test_adopt_refuses_a_store_with_another_id(tmp_path, shared):
    shutil.copytree(tmp_path / "TEST-shared", tmp_path / "TEST-new-machine")
    stranger = shared_binding(tmp_path / "TEST-new-machine", installation_id="shared-install:TEST-9999")
    with pytest.raises(ContractError) as exc:
        SQLiteStorage(stranger).adopt()
    assert (exc.value.code, exc.value.field) == ("IDENTITY_UNBOUND", "payload")


# --- a capture that waited in the inbox ----------------------------------------------------

def test_a_replayed_capture_keeps_the_entry_that_captured_it(shared):
    """The shared worker replays with its own context, which names no entry.  The
    capture is still filed under the entry that made it, not under the replayer."""
    storage, binding = shared
    capturer = shared_context(binding, "tianxuan")
    capture_inbox.enqueue(storage, Clock(), capturer, source_event(source_event_key="TEST-waited/1"),
                          scope_id="TEST-scope", host_scope=None)
    worker = shared_context(binding)
    receipts = capture_inbox.replay_inbox(storage, Clock(), worker, authorize=lambda _: binding.scope_ids)
    assert [r.durability for r in receipts] == ["persisted"]
    assert rows(storage) == [("TEST-waited/1", "tianxuan")]


def test_a_local_inbox_row_is_byte_for_byte_what_it_was(tmp_path):
    ctx = context(tmp_path / "TEST-local")
    assert "entry_id" not in capture_inbox._context_payload(ctx)
    shared_ctx = shared_context(shared_binding(tmp_path / "TEST-shared"), "tianshu")
    assert capture_inbox._context_payload(shared_ctx)["entry_id"] == "tianshu"


# --- upgrade -------------------------------------------------------------------------------

def test_a_1109_store_upgrades_with_every_row_local(tmp_path):
    ctx = context(tmp_path / "TEST-local")
    storage = SQLiteStorage(ctx.binding)
    storage.initialize()
    put(storage, ctx, "TEST-old/1")
    downgrade_store(storage.path, 1109)
    with sqlite3.connect(storage.path) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(source_events)")}
    assert "entry_id" not in columns
    storage.initialize()
    assert rows(storage) == [("TEST-old/1", "local")]
    assert (meta(storage, "installation_kind"), meta(storage, "schema_version")) == ("local", 1110)
    put(storage, ctx, "TEST-new/1")
    assert rows(storage) == [("TEST-new/1", "local"), ("TEST-old/1", "local")]


# --- what a reader is shown ----------------------------------------------------------------

def _core(binding):
    from scope_recall.core import CoreConfig, MemoryCore
    core = MemoryCore(CoreConfig(binding), clock=Clock())
    core.initialize()
    return core


def _record(core, ctx, text, key):
    saved = core.record_event(ctx, source_event(source_event_key=key, content=text), scope_id="TEST-scope",
                              remaining_seconds=10)
    assert saved.durability == "persisted"
    return saved.event_refs[0]


def test_a_shared_recall_says_which_entry_each_item_came_in_through(tmp_path):
    from scope_recall.contracts import validate_payload
    from v11_support import recall_request
    binding = shared_binding(tmp_path / "TEST-shared")
    core = _core(binding)
    with core.storage.write(shared_context(binding)) as tx:
        tx.register_entry("tianshu", "天枢", "hermes", now=NOW)
        tx.register_entry("tianxuan", "天璇", "hermes", now=NOW)
    _record(core, shared_context(binding, "tianxuan"), "天璇记下的 TEST 部署口令是 H100-ZEBRA。", "TEST-recall/1")
    packet = core.recall_packet(shared_context(binding, "tianshu"),
                                recall_request(query="H100-ZEBRA 部署口令", mode="current"), deadline_seconds=5)
    assert packet["items"], packet
    assert packet["items"][0]["entries"] == [{"id": "tianxuan", "name": "天璇"}]
    validate_payload("recall_packet", packet)


def test_a_local_recall_packet_carries_no_entries(tmp_path):
    from v11_support import recall_request
    ctx = context(tmp_path / "TEST-local")
    core = _core(ctx.binding)
    _record(core, ctx, "本地库的 TEST 部署口令是 H100-ZEBRA。", "TEST-recall/1")
    packet = core.recall_packet(ctx, recall_request(query="H100-ZEBRA 部署口令", mode="current"), deadline_seconds=5)
    assert packet["items"] and all("entries" not in item for item in packet["items"])


def test_an_item_with_evidence_from_several_entries_names_each_once(tmp_path):
    """A claim or episode lists every entry behind its evidence: once each, ordered."""
    from scope_recall.core.retrieval_storage import evidence_entries
    binding = shared_binding(tmp_path / "TEST-shared")
    core = _core(binding)
    with core.storage.write(shared_context(binding)) as tx:
        tx.register_entry("tianshu", "天枢", "hermes", now=NOW)
        tx.register_entry("tianxuan", "天璇", "hermes", now=NOW)
    a = _record(core, shared_context(binding, "tianxuan"), "TEST 第一句。", "TEST-many/1")
    b = _record(core, shared_context(binding, "tianshu"), "TEST 第二句。", "TEST-many/2")
    c = _record(core, shared_context(binding, "tianxuan"), "TEST 第三句。", "TEST-many/3")
    refs = [f"{r.ref}@{r.revision}" for r in (a, b, c)] + ["not-a-source-ref"]
    with core.storage.read(shared_context(binding)) as tx:
        assert evidence_entries(tx, refs) == [{"id": "tianshu", "name": "天枢"}, {"id": "tianxuan", "name": "天璇"}]
    local = context(tmp_path / "TEST-local")
    local_core = _core(local.binding)
    d = _record(local_core, local, "TEST 本地一句。", "TEST-many/4")
    with local_core.storage.read(local) as tx:
        assert evidence_entries(tx, [f"{d.ref}@{d.revision}"]) == []


# --- writers in separate processes take turns ----------------------------------------------

def _busy_for(monkeypatch, attempts):
    """Another process holds the writer lease for the next ``attempts`` writable opens."""
    from scope_recall.core import storage as module
    from scope_recall.core.writer_lease import TruthWriterBusyError
    real, calls = module.connect_truth_database, []

    def connect(path, *, mode, **kwargs):
        if mode != "ro":
            calls.append(mode)
            if attempts is None or len(calls) <= attempts:
                raise TruthWriterBusyError(role="truth_connection", scope="other_process")
        return real(path, mode=mode, **kwargs)

    monkeypatch.setattr(module, "connect_truth_database", connect)
    return calls


def test_a_writer_waits_for_another_process_s_turn_to_end(monkeypatch, shared):
    """Soak of 2026-09-22: three entries and a worker, one capture in ten failed at once on a lease
    released milliseconds later, and waited in memory for a retry the process might never reach."""
    storage, binding = shared
    calls = _busy_for(monkeypatch, 3)
    started = time.monotonic()
    saved = put(storage, shared_context(binding, "tianshu"), "TEST-turns/1")
    assert saved.disposition == "inserted" and len(calls) == 4
    assert time.monotonic() - started < 0.5


def test_a_writer_gives_up_when_the_turn_outlasts_its_deadline(monkeypatch, shared):
    from scope_recall.core.writer_lease import TruthWriterBusyError
    storage, binding = shared
    _busy_for(monkeypatch, None)
    started = time.monotonic()
    with pytest.raises(TruthWriterBusyError):
        with storage.write(shared_context(binding, "tianshu"), remaining_seconds=0.1):
            pass
    assert 0.05 <= time.monotonic() - started < 0.5
    with storage.read(shared_context(binding)) as tx:
        assert tx.status() is not None, "a reader never waits for the writer lease"
