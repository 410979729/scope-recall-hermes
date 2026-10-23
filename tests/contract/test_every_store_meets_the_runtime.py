"""Every companion store can be asked what the runtime asks of a store.

The runtime reaches a vector store through two seams: ``LanceIndexWriter`` publishes through
``fenced_upsert_records(rows, guard=, remaining_seconds=)``, and ``LancePurgePort`` purges through
``purge_governed_members(..., remaining_seconds=)``.  ``build_vector_store`` picks the helper-process
store on Windows and the in-process one everywhere else, so a fence or purge test that drives only the
helper-process store, on Windows, leaves three gaps unseen:

- an in-process LanceDB store without ``fenced_upsert_records``: on Linux every publication fails with
  ``fenced_upsert_unsupported`` and the index stays empty;
- a ``purge_governed_members`` that takes ``budget_seconds``, the name the Windows helper uses, and refuses
  the port's ``remaining_seconds`` with a ``TypeError`` the port reads as "not purged": a forget never
  finishes there;
- a SQLite companion with no purge at all.

The first test asks each store class for the two calls by signature.  It needs no native code, so it
runs on every platform and in this tier, which forbids the child process and the socket LanceDB needs
to load.  The behaviour is written once, as the ``check_*`` functions below: this file runs them
against the SQLite companion, and ``tests/storage_native/test_in_process_lance_store.py`` runs them
against the in-process LanceDB store in the native tier.
"""

from __future__ import annotations

import inspect
import os
import sqlite3
import stat
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest
from scope_recall.core.deadline import RequestDeadline, using_request_deadline
from scope_recall.runtime.lance_port import LanceIndexWriter, LancePurgePort, LanceVectorRecord
from scope_recall.vector import VectorStoreCompatibilityError
from scope_recall.vector.process_store import ProcessLanceVectorStore
from scope_recall.vector.sqlite_store import SQLiteBruteForceVectorStore
from scope_recall.vector.store import LanceVectorStore, build_vector_store

SPACE, SCOPE, AGENT, INSTALLATION = "TEST-space", "TEST-scope", "TEST-agent", "TEST-installation"


def _record(
    ref: str,
    vector: tuple[float, ...] = (0.25, 0.75),
    *,
    revision: int = 1,
    scope: str = SCOPE,
    vector_id: str | None = None,
) -> LanceVectorRecord:
    return LanceVectorRecord(
        "event", ref, revision, vector_id or f"TEST:{ref}:{revision}", SPACE, vector, scope, AGENT, INSTALLATION
    )


def _receipt(*refs: str, scope: str = SCOPE) -> dict:
    return {
        "physical_members": [{"kind": "event", "ref": ref} for ref in refs],
        "scope_ids": [scope],
        "project_id": None,
        "branch_id": None,
    }


def _port(store) -> LancePurgePort:
    return LancePurgePort(store, embedding_spaces=[SPACE], agent_id=AGENT, installation_id=INSTALLATION)


@pytest.mark.parametrize("store_class", [LanceVectorStore, ProcessLanceVectorStore, SQLiteBruteForceVectorStore])
def test_each_store_class_takes_the_two_calls_the_runtime_makes(store_class):
    fenced = inspect.signature(store_class.fenced_upsert_records)
    fenced.bind(None, [], guard=lambda: True, remaining_seconds=1.0)
    purge = inspect.signature(store_class.purge_governed_members)
    purge.bind(
        None,
        members=[],
        agent_id=AGENT,
        installation_id=INSTALLATION,
        partitions=[],
        project_id=None,
        branch_id=None,
        remaining_seconds=1.0,
    )
    inspect.signature(store_class.search_scopes).bind(None, [1.0, 0.0], scope_ids=[], limit=1)


@pytest.mark.parametrize("backend", ["lancedb", "sqlite-bruteforce"])
def test_the_store_chosen_for_this_platform_has_both(backend, tmp_path):
    store = build_vector_store(backend, storage_dir=tmp_path, table_name="TEST_vectors", dimensions=2)
    assert callable(getattr(store, "fenced_upsert_records", None)), type(store).__name__
    assert callable(getattr(store, "purge_governed_members", None)), type(store).__name__


# -- the behaviour, for whichever in-process store is handed in ------------------------------------


def check_a_group_is_published_under_one_guard_check_and_a_refusal_writes_nothing(store) -> None:
    checks: list[bool] = []
    writer = LanceIndexWriter(store)
    assert (
        writer.upsert_fenced_many(
            [_record("event-1"), _record("event-2", (0.5, 0.5))],
            guard=lambda: not checks.append(True),
            remaining_seconds=5.0,
        )
        is True
    )
    assert sorted(store.list_ids()) == ["TEST:event-1:1", "TEST:event-2:1"] and checks == [True]
    assert writer.upsert_fenced(_record("event-3"), guard=lambda: False, remaining_seconds=5.0) is False
    assert "TEST:event-3:1" not in store.list_ids()


def check_a_forget_removes_every_revision_of_its_members_and_only_those(store) -> None:
    published = [
        _record("event-1"),
        _record("event-1", (0.3, 0.7), revision=2),
        _record("event-2", (0.5, 0.5)),
        _record("event-1", scope="TEST-other-scope", vector_id="TEST:other-scope:event-1:1"),
    ]
    assert LanceIndexWriter(store).upsert_fenced_many(published, guard=lambda: True, remaining_seconds=5.0) is True

    assert _port(store).purge_active("TEST-operation", receipt=_receipt("event-1"), remaining_seconds=5.0) is True

    assert sorted(store.list_ids()) == ["TEST:event-2:1", "TEST:other-scope:event-1:1"], (
        "both revisions of the member are gone; another member, and the same ref in another scope, are not this forget's"
    )
    assert _port(store).purge_active("TEST-operation", receipt=_receipt("event-1"), remaining_seconds=5.0) is True, (
        "an inventory verified empty is acknowledged"
    )


def check_a_row_that_cannot_be_classified_is_never_acknowledged_as_gone(store) -> None:
    store.upsert_records(
        [
            dict(
                id="TEST:stray",
                scope_id="TEST-partition",
                source="event-9",
                target="not json",
                content="",
                summary="",
                updated_at="",
                vector=[0.1, 0.9],
            )
        ]
    )

    assert _port(store).purge_active("TEST-operation", receipt=_receipt("event-1"), remaining_seconds=5.0) is False
    assert store.list_ids() == ["TEST:stray"], "and nothing is deleted on a guess"


def check_a_row_that_carries_text_is_refused(store) -> None:
    """A companion row is a vector and opaque identities: one with words in ``content`` or ``summary`` is refused,
    fenced or not, before anything is written, so no file of the companion ever holds them."""
    row = dict(
        id="TEST:event-1:1",
        scope_id="TEST-partition",
        source="event-1",
        target="{}",
        content="",
        summary="",
        updated_at="",
        vector=[0.25, 0.75],
    )
    for field in ("content", "summary"):
        with pytest.raises(ValueError, match="carries no text"):
            store.upsert_records([{**row, field: "TEST 紫色钥匙"}])
        with pytest.raises(ValueError, match="carries no text"):
            store.fenced_upsert_records([{**row, field: "TEST 紫色钥匙"}], guard=lambda: True, remaining_seconds=5.0)
    assert store.list_ids() == []


def check_a_search_over_several_partitions_filters_before_it_ranks(store) -> None:
    """An entry holding a hundred scopes asks once, and rows it may not read never crowd out its own.

    Filtering after the nearest rows were chosen would return nothing here: thirty rows of another
    partition sit nearer the query than anything the listed partitions hold.
    """
    from scope_recall.runtime.lance_port import physical_partition_scope_id

    def partition(scope: str) -> str:
        return physical_partition_scope_id(
            agent_id=AGENT,
            installation_id=INSTALLATION,
            embedding_space=SPACE,
            logical_scope_id=scope,
            project_id=None,
            branch_id=None,
        )

    crowd = [_record(f"event-crowd-{index}", (1.0, 0.001 * index), scope="TEST-crowded") for index in range(30)]
    mine = [
        _record("event-near", (0.8, 0.6), scope="TEST-mine-a"),
        _record("event-far", (0.0, 1.0), scope="TEST-mine-b"),
    ]
    assert (
        LanceIndexWriter(store).upsert_fenced_many([*crowd, *mine], guard=lambda: True, remaining_seconds=5.0) is True
    )

    listed = [partition("TEST-mine-a"), partition("TEST-mine-b")]
    rows = store.search_scopes([1.0, 0.0], scope_ids=listed, limit=5)
    assert [row["id"] for row in rows] == ["TEST:event-near:1", "TEST:event-far:1"], (
        "only the listed partitions, nearest first"
    )
    one_at_a_time = [row["id"] for scope in listed for row in store.search([1.0, 0.0], scope_id=scope, limit=5)]
    assert sorted(one_at_a_time) == sorted(row["id"] for row in rows), (
        "one request finds what a request per partition did"
    )
    assert store.search_scopes([1.0, 0.0], scope_ids=[], limit=5) == []


CHECKS = (
    check_a_group_is_published_under_one_guard_check_and_a_refusal_writes_nothing,
    check_a_forget_removes_every_revision_of_its_members_and_only_those,
    check_a_row_that_cannot_be_classified_is_never_acknowledged_as_gone,
    check_a_row_that_carries_text_is_refused,
    check_a_search_over_several_partitions_filters_before_it_ranks,
)


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__.removeprefix("check_"))
def test_the_sqlite_companion(check, tmp_path):
    store = build_vector_store(
        "sqlite-bruteforce", storage_dir=tmp_path, table_name="TEST_vectors", dimensions=2, metric="cosine"
    )
    store.open()
    try:
        check(store)
    finally:
        store.close()


def _sqlite_store(tmp_path, *, dimensions=2):
    return SQLiteBruteForceVectorStore(tmp_path / "vector.sqlite3", table_name="TEST_vectors", dimensions=dimensions)


def _sqlite_rows(start, stop):
    return [
        dict(id=f"TEST:{index:04d}", scope_id=SCOPE, content="", summary="", vector=[0.25, 0.75])
        for index in range(start, stop)
    ]


def test_sqlite_raw_file_descriptors_close_before_its_connection_opens(tmp_path, monkeypatch):
    store = _sqlite_store(tmp_path)
    raw_open = os.open
    paths = {store.db_path, *store._sidecar_paths()}
    calls = []

    def track_open(path, *args, **kwargs):
        if path in paths:
            calls.append((path, store._conn is not None))
        return raw_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", track_open)
    try:
        store.open()
        store.upsert_records(_sqlite_rows(0, 1))
        assert store.count_rows() == 1
        store.delete_by_ids(["TEST:0000"])
        assert store.list_ids() == []
    finally:
        store.close()
    assert calls and all(not connected for _path, connected in calls), calls


def test_sqlite_new_store_raw_opens_only_its_exclusive_creation(tmp_path, monkeypatch):
    store = _sqlite_store(tmp_path)
    raw_open, calls = os.open, []
    paths = {store.db_path, *store._sidecar_paths()}

    def track_open(path, flags, *args, **kwargs):
        if path in paths:
            calls.append((path, flags))
        return raw_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", track_open)
    try:
        store.open()
        assert len(calls) == 1 and calls[0][0] == store.db_path
        assert calls[0][1] & os.O_CREAT and calls[0][1] & os.O_EXCL
    finally:
        store.close()


def test_sqlite_writer_preserves_a_same_process_reader_without_raw_opens(tmp_path, monkeypatch):
    writer, reader = _sqlite_store(tmp_path), _sqlite_store(tmp_path)
    writer.open()
    writer.upsert_records(_sqlite_rows(0, 1))
    writer.close()
    reader.open_existing()
    raw_open, calls = os.open, []
    paths = {writer.db_path, *writer._sidecar_paths()}

    def track_open(path, *args, **kwargs):
        if path in paths:
            calls.append(path)
        return raw_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", track_open)
    try:
        assert all(path.is_file() for path in paths)
        writer.open()
        assert calls == []
        writer.upsert_records(_sqlite_rows(1, 2))
        assert reader.count_rows() == 2
    finally:
        writer.close()
        reader.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_sqlite_existing_database_permissions_are_narrowed_once(tmp_path, monkeypatch):
    store = _sqlite_store(tmp_path)
    store.open()
    store.close()
    store.db_path.chmod(0o644)
    raw_open, calls = os.open, []

    def track_open(path, *args, **kwargs):
        if path == store.db_path:
            calls.append(path)
        return raw_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", track_open)
    try:
        store.open()
        assert stat.S_IMODE(store.db_path.stat().st_mode) == 0o600
        store.close()
        store.open()
        assert calls == [store.db_path]
    finally:
        store.close()


@pytest.mark.parametrize("sidecar", ["wal", "shm"])
def test_sqlite_writer_skips_a_sidecar_missing_at_lstat(tmp_path, monkeypatch, sidecar):
    writer, reader = _sqlite_store(tmp_path), _sqlite_store(tmp_path)
    writer.open()
    writer.close()
    reader.open_existing()
    missing = writer.db_path.with_name(f"{writer.db_path.name}-{sidecar}")
    raw_lstat, raw_open = os.lstat, os.open

    def missing_lstat(path, *args, **kwargs):
        if path == missing:
            raise FileNotFoundError("TEST sidecar disappeared")
        return raw_lstat(path, *args, **kwargs)

    def missing_open(path, *args, **kwargs):
        if path == missing:
            raise FileNotFoundError("TEST sidecar disappeared")
        return raw_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", missing_lstat)
    monkeypatch.setattr(os, "open", missing_open)
    try:
        writer.open()
        assert reader.count_rows() == 0
    finally:
        writer.close()
        reader.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_sqlite_writer_skips_a_sidecar_gone_before_permission_narrowing(tmp_path, monkeypatch):
    writer, reader = _sqlite_store(tmp_path), _sqlite_store(tmp_path)
    writer.open()
    writer.close()
    reader.open_existing()
    missing = writer._sidecar_paths()[0]
    missing.chmod(0o644)
    raw_open = os.open

    def missing_open(path, *args, **kwargs):
        if path == missing:
            raise FileNotFoundError("TEST sidecar disappeared")
        return raw_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", missing_open)
    try:
        writer.open()
        assert reader.count_rows() == 0
    finally:
        writer.close()
        reader.close()


def test_sqlite_existing_open_propagates_path_permission_errors(tmp_path, monkeypatch):
    store = _sqlite_store(tmp_path)
    raw_stat, raw_lstat = os.stat, os.lstat
    path_exists, path_is_symlink = Path.exists, Path.is_symlink

    def deny_stat(path, *args, **kwargs):
        if path == store.db_path:
            raise PermissionError("TEST inaccessible vector store")
        return raw_stat(path, *args, **kwargs)

    def deny_lstat(path, *args, **kwargs):
        if path == store.db_path:
            raise PermissionError("TEST inaccessible vector store")
        return raw_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", deny_stat)
    monkeypatch.setattr(os, "lstat", deny_lstat)
    # Python 3.14's path predicates suppress permission errors; emulate that on older interpreters.
    monkeypatch.setattr(Path, "exists", lambda path: False if path == store.db_path else path_exists(path))
    monkeypatch.setattr(Path, "is_symlink", lambda path: False if path == store.db_path else path_is_symlink(path))
    with pytest.raises(PermissionError, match="TEST inaccessible vector store"):
        store.open_existing()
    assert store._conn is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_sqlite_created_sidecars_have_owner_only_permissions(tmp_path):
    store = _sqlite_store(tmp_path)
    try:
        store.open()
        store.upsert_records(_sqlite_rows(0, 1))
        assert [stat.S_IMODE(path.stat().st_mode) for path in store._sidecar_paths()] == [0o600, 0o600]
    finally:
        store.close()


@pytest.mark.parametrize("deadline_bound", [True, False], ids=["request-deadline", "no-deadline"])
def test_sqlite_thread_lock_wait_obeys_the_request_deadline(tmp_path, deadline_bound):
    store = _sqlite_store(tmp_path)
    store.open()
    started, done = threading.Event(), threading.Event()
    held, release = threading.Event(), threading.Event()
    outcomes = []

    def hold_lock():
        with store._lock:
            held.set()
            release.wait()

    def read():
        started.set()
        try:
            if deadline_bound:
                with using_request_deadline(RequestDeadline.from_budget(1.0)):
                    outcomes.append(store.count_rows())
            else:
                outcomes.append(store.count_rows())
        except TimeoutError as exc:
            outcomes.append(exc)
        finally:
            done.set()

    reader = threading.Thread(target=read)
    holder = threading.Thread(target=hold_lock)
    try:
        holder.start()
        assert held.wait(15.0)
        reader.start()
        assert started.wait(15.0)
        before = time.perf_counter()
        finished_while_held = done.wait(15.0 if deadline_bound else 0.1)
        elapsed = time.perf_counter() - before
        release.set()
        reader.join(15.0)
        assert not reader.is_alive()
        if deadline_bound:
            assert finished_while_held, "request waited for the store lock past its deadline"
            assert elapsed < 15.0
            assert len(outcomes) == 1 and isinstance(outcomes[0], TimeoutError)
            assert "store lock" in str(outcomes[0])
        else:
            assert not finished_while_held
            assert outcomes == [0]
    finally:
        release.set()
        holder.join(15.0)
        reader.join(15.0)
        store.close()


def test_sqlite_existing_open_refuses_a_directory_as_physical_storage(tmp_path):
    store = _sqlite_store(tmp_path)
    store.db_path.mkdir()
    with pytest.raises(VectorStoreCompatibilityError, match="physical storage is not a regular file"):
        store.open_existing()
    assert store._conn is None


def test_sqlite_reader_leaves_sidecars_until_the_last_writer_closes(tmp_path):
    writer, reader = _sqlite_store(tmp_path), _sqlite_store(tmp_path)
    writer.open()
    writer.upsert_records(_sqlite_rows(0, 1))
    writer.close()
    sidecars = writer._sidecar_paths()
    assert not any(path.exists() for path in sidecars)
    try:
        reader.open_existing()
        assert all(path.is_file() for path in sidecars)
    finally:
        reader.close()
    assert all(path.is_file() for path in sidecars)
    try:
        writer.open()
    finally:
        writer.close()
    assert not any(path.exists() for path in sidecars)


@pytest.mark.parametrize("truncate", [False, True], ids=["wal-frames", "empty-wal"])
def test_sqlite_reader_opens_beside_a_writer_and_reads_committed_rows(tmp_path, truncate):
    writer, reader = _sqlite_store(tmp_path), _sqlite_store(tmp_path)
    writer.open()
    try:
        writer.upsert_records(_sqlite_rows(0, 10))
        if truncate:
            writer._require_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        wal, shm = writer._sidecar_paths()
        assert shm.is_file() and (wal.stat().st_size == 0) is truncate
        reader.open_existing()
        assert reader.count_rows() == 10
        writer.upsert_records(_sqlite_rows(10, 15))
        assert reader.count_rows() == 15
        assert len(reader.search_scopes([0.25, 0.75], scope_ids=[SCOPE], limit=20)) == 15
    finally:
        reader.close()
        writer.close()


def test_sqlite_kept_reader_sees_later_commits_deletions_and_checkpoints(tmp_path):
    writer, reader = _sqlite_store(tmp_path), _sqlite_store(tmp_path)
    writer.open()
    writer.upsert_records(_sqlite_rows(0, 10))
    writer.close()
    assert not any(path.exists() for path in writer._sidecar_paths())
    reader.open_existing()
    try:
        assert reader.count_rows() == 10
        writer.open()
        writer.upsert_records(_sqlite_rows(10, 2010))
        writer.delete_by_ids(["TEST:0000", "TEST:0001", "TEST:0002"])
        expected = [row["id"] for row in _sqlite_rows(3, 2010)]
        assert reader.count_rows() == len(expected)
        assert reader.list_ids() == expected
        assert len(reader.search_scopes([0.25, 0.75], scope_ids=[SCOPE], limit=3000)) == len(expected)
        checkpoint = writer._require_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert tuple(checkpoint) == (0, 0, 0)
        writer.close()
        assert reader.count_rows() == len(expected)
        assert reader.list_ids() == expected
        assert (
            sorted(row["id"] for row in reader.search_scopes([0.25, 0.75], scope_ids=[SCOPE], limit=3000)) == expected
        )
    finally:
        reader.close()
        writer.close()


def test_sqlite_existing_open_preserves_storage_and_refuses_creation_and_writes(tmp_path):
    reader, writer = _sqlite_store(tmp_path), _sqlite_store(tmp_path)
    with pytest.raises(FileNotFoundError, match="sqlite-bruteforce physical storage is missing"):
        reader.open_existing()
    assert not list(tmp_path.iterdir())
    writer.open()
    writer.upsert_records(_sqlite_rows(0, 1))
    writer.close()
    before = writer.db_path.read_bytes()
    incompatible = _sqlite_store(tmp_path, dimensions=3)
    with pytest.raises(VectorStoreCompatibilityError, match="identity mismatch"):
        incompatible.open_existing()
    assert incompatible._conn is None
    reader.open_existing()
    try:
        assert reader.count_rows() == 1
        assert reader.list_ids() == ["TEST:0000"]
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.upsert_records(_sqlite_rows(1, 2))
    finally:
        reader.close()
    assert writer.db_path.read_bytes() == before


def test_sqlite_kept_connection_caps_each_lock_wait_to_the_current_request(tmp_path):
    writer, reader = _sqlite_store(tmp_path), _sqlite_store(tmp_path)
    writer.open()
    writer.close()
    # Rollback-journal mode lets the other connection acquire its exclusive lock
    # after validation, without a WAL reader's shared-memory attachment.
    with closing(sqlite3.connect(writer.db_path)) as blocker:
        blocker.execute("PRAGMA journal_mode=DELETE")
        reader.open_existing()
        try:
            assert reader._require_conn().execute("PRAGMA busy_timeout").fetchone()[0] == 30000
            blocker.execute("PRAGMA locking_mode=EXCLUSIVE")
            blocker.execute("BEGIN EXCLUSIVE")
            started = time.perf_counter()
            with using_request_deadline(RequestDeadline.from_budget(1.0)):
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    reader.count_rows()
            assert time.perf_counter() - started < 15.0
            blocker.rollback()
        finally:
            reader.close()


def test_sqlite_statement_timeout_tracks_expiry_and_resets_outside_requests(tmp_path):
    store = _sqlite_store(tmp_path)
    store.open()
    try:
        for budget in (1.0, 0.0):
            with using_request_deadline(RequestDeadline.from_budget(budget)):
                for read in (store.count_rows, store.list_ids, store.list_records, lambda: store.contains_id("TEST:0")):
                    read()
                    timeout = store._require_conn().execute("PRAGMA busy_timeout").fetchone()[0]
                    assert 0 <= timeout <= budget * 1000
        store.count_rows()
        assert store._require_conn().execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    finally:
        store.close()
