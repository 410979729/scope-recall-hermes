"""Regression boundaries for private fences, disposable-log durability and diagnostics."""

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event, Thread

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core.management import MemoryManagement
from scope_recall.core.restore import InstallationMaintenance

from tests.contract.test_claims import app, capture
from tests.v11_support import recall_request


@pytest.mark.parametrize("method", ["recall", "recall_packet"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_read_only_interval_opened_and_closed_during_recall_never_logs(app, monkeypatch, method, interrupted):
    core, ctx = app
    capture(core, ctx, "TEST observatory compass material is copper.")
    owner = MemoryManagement(core, InstallationMaintenance(ctx))
    original = core.recall_pipeline.search

    def searched(context):
        result = original(context)
        owner.private_on(target="global", mode="read-only")
        owner.private_off(target="global")
        if interrupted:
            raise ContractError("SQLITE_BUSY", "TEST interrupted")
        return result

    monkeypatch.setattr(core.recall_pipeline, "search", searched)
    if interrupted:
        with pytest.raises(ContractError, match="SQLITE_BUSY"):
            getattr(core, method)(ctx, recall_request(query="TEST observatory compass material"))
    else:
        result = getattr(core, method)(ctx, recall_request(query="TEST observatory compass material"))
        assert result["items"] if isinstance(result, dict) else result.items
    assert core.recall_diagnostics.flush()
    assert core.recall_diagnostics.store.list() == []


def test_uncaptured_query_records_only_its_length(app):
    core, ctx = app
    core.recall_packet(ctx, recall_request(query="TEST absent observatory compass"))
    assert core.recall_diagnostics.flush()
    assert core.recall_diagnostics.store.list()[0]["query"] == {"length": len("TEST absent observatory compass")}


def test_recall_diagnostic_does_not_query_the_work_queue(app, monkeypatch):
    from scope_recall.core.storage import Transaction

    core, ctx = app
    capture(core, ctx, "TEST observatory compass material is copper.")
    original = Transaction._check

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, statement, *args):
            assert "FROM work_items" not in statement, "recall queried the work queue"
            return self.conn.execute(statement, *args)

    monkeypatch.setattr(Transaction, "_check", lambda self, **kw: Connection(original(self, **kw)))
    packet = core.recall_packet(ctx, recall_request(query="TEST observatory compass material"))
    assert packet["items"]


def test_leased_work_does_not_assert_worker_liveness(app):
    core, ctx = app
    capture(core, ctx, "TEST observatory compass material is copper.")
    with core.storage.write(ctx) as tx:
        tx._check(write=True).execute(
            "UPDATE work_items SET state='leased',lease_owner='TEST',lease_until=?", (core.clock.now,)
        )
    core.recall_packet(ctx, recall_request(query="TEST observatory compass material"))
    assert core.recall_diagnostics.flush()
    assert core.recall_diagnostics.store.list()[0]["background"]["worker_running"] is None


def test_management_history_explicitly_disables_open_time_upgrade(app, monkeypatch):
    from scope_recall.maintenance import memory

    core, ctx = app
    monkeypatch.setattr(memory, "store_to_restore", lambda **kw: ctx.binding)
    args = memory.parser().parse_args(["recalls"])
    api = memory.open_management(args, initialize=False)
    assert api.core.storage.upgrade_on_open is False


def test_doctor_history_reports_pending_upgrade_without_initializing(app, monkeypatch):
    from scope_recall.core.storage import SQLiteStorage
    from scope_recall.maintenance.doctor_recall import check_recall_log
    from scope_recall.maintenance.doctor_report import DoctorReport

    core, ctx = app
    core.clock.now = datetime.now(UTC).isoformat()
    core.recall_packet(ctx, recall_request(query="TEST absent observatory", request_id="TEST-upgrade"))
    assert core.recall_diagnostics.flush()
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("PRAGMA user_version=1118")
    monkeypatch.setattr(SQLiteStorage, "initialize", lambda *a, **kw: pytest.fail("doctor upgraded truth"))
    report = DoctorReport(host="hermes", status="ok")
    check_recall_log(report, ctx.binding, request_id="TEST-upgrade")
    assert report.recall_attempt["state"] == "unavailable"


@pytest.mark.parametrize("reason", ["quarantine", "deleted", "muted", "private", "expired"])
def test_compiler_uses_the_retrieval_rejection_reason(app, monkeypatch, reason):
    from scope_recall.core import recall_log_view
    from scope_recall.core.recall_trace import CURRENT_TRACE, RecallTrace
    from scope_recall.core.retrieval import SearchContext

    core, ctx = app
    capture(core, ctx, "TEST observatory compass material is copper.")
    request = recall_request(query="TEST observatory compass material")
    result = core.recall(ctx, request)
    obj = result.items[0]
    candidate = next(c for c in result.candidates if c.key == (obj.kind, obj.ref, obj.revision))
    context = SearchContext.from_request(request, ctx, now=core.clock.utc_now(), deadline=core.clock.monotonic() + 5)
    monkeypatch.setattr(core.recall_packet_compiler.storage_reader, "hydrate", lambda *a: None)
    monkeypatch.setattr(recall_log_view, "rejection_reason", lambda *a: reason)
    trace = RecallTrace(enabled=True, context=context)
    token = CURRENT_TRACE.set(trace)
    try:
        with core.storage.read(ctx) as tx:
            assert core.recall_packet_compiler._recheck(tx, context, candidate, obj, []) is None
        assert trace.candidates[candidate.key]["reason"] == reason
    finally:
        CURRENT_TRACE.reset(token)


def _record(ref, day):
    return dict(
        ref=ref, timestamp=f"2026-09-{day:02d}T12:00:00Z", installation_id="TEST", session_id="TEST", request_id=ref
    )


def test_log_writes_use_wal_with_normal_synchronization(tmp_path):
    from scope_recall.core.recall_log_storage import RecallLogStore

    store = RecallLogStore(tmp_path / "recall.sqlite3")
    with store._open() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1


def test_writer_close_drains_and_stops_the_thread(tmp_path):
    from scope_recall.core.recall_log_storage import RecallLogStore
    from scope_recall.core.recall_log_writer import RecallLogWriter

    writer = RecallLogWriter(RecallLogStore(tmp_path / "recall.sqlite3"))
    writer.submit(_record("TEST-close", 1))
    assert writer.close(timeout=2)
    assert writer.thread is None or not writer.thread.is_alive()
    writer.submit(_record("TEST-after-close", 2))
    assert writer.queue.qsize() == 0


def test_two_connections_cannot_reinsert_an_expired_record(tmp_path):
    from scope_recall.core.recall_log_storage import LogPolicy, RecallLogStore

    path = tmp_path / "recall.sqlite3"
    policy = LogPolicy(retention_days=2)
    first, second = RecallLogStore(path, policy), RecallLogStore(path, policy)
    first.write(_record("TEST-initial", 1))
    arrived, release = Event(), Event()
    original = first._open
    errors = []

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchone(self):
            value = self.cursor.fetchone()
            arrived.set()
            assert release.wait(5)
            return value

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, key):
            return getattr(self.conn, key)

        def execute(self, statement, *args):
            result = self.conn.execute(statement, *args)
            return Cursor(result) if statement.startswith("SELECT max(timestamp)") else result

    first._open = lambda: Connection(original())

    def old_writer():
        try:
            first.write(_record("TEST-old", 1))
        except (OSError, sqlite3.Error):
            errors.append("busy")

    thread = Thread(target=old_writer)
    thread.start()
    busy = False
    try:
        assert arrived.wait(5)
        try:
            second.write(_record("TEST-new", 10))
        except (OSError, sqlite3.Error):
            busy = True
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    if busy:
        second.write(_record("TEST-new", 10))
    assert all(row["timestamp"] >= "2026-09-09" for row in second.list())


def test_private_source_has_a_distinct_rejection_reason(app):
    from scope_recall.core.recall_log_view import rejection_reason
    from scope_recall.core.retrieval import CandidateRef, SearchContext

    core, ctx = app
    source = capture(core, ctx, "TEST observatory compass material is copper.")
    owner = MemoryManagement(core, InstallationMaintenance(ctx))
    owner.private_on(target="session", session=ctx.session_id)
    other = replace(ctx, session_id="TEST-other-reader")
    context = SearchContext.from_request(
        recall_request(query="TEST observatory compass material"),
        other,
        now=core.clock.utc_now(),
        deadline=core.clock.monotonic() + 5,
    )
    with core.storage.read(other) as tx:
        assert rejection_reason(tx, CandidateRef("event", source.ref, 1, "exact_ref"), context) == "private"


def test_published_queue_state_is_scoped_and_bad_snapshots_do_not_change_recall(app):
    core, ctx = app
    capture(core, ctx, "TEST observatory compass material is copper.")
    path = ctx.binding.data_directory / "runtime-worker-status.json"
    path.write_text(
        json.dumps(
            dict(
                installation_id=ctx.binding.installation_id,
                recall_state=dict(
                    observed_at=core.clock.now,
                    truncated=False,
                    groups=[
                        dict(
                            scope_id="TEST-scope",
                            project_id="TEST-project",
                            branch_id=None,
                            pending=7,
                            leased=2,
                            paused=1,
                        ),
                        dict(scope_id="TEST-other", project_id=None, branch_id=None, pending=99, leased=9, paused=0),
                    ],
                ),
            )
        ),
        encoding="utf-8",
    )
    request = recall_request(query="TEST observatory compass material")
    first = core.recall_packet(ctx, request)
    assert core.recall_diagnostics.flush()
    state = core.recall_diagnostics.store.list()[0]["background"]
    assert (state["pending"], state["leased"], state["budget_paused"]) == (7, 2, True)
    assert state["worker_running"] is None
    path.write_text("[]", encoding="utf-8")
    second = core.recall_packet(ctx, request)
    assert second["items"] == first["items"]


def test_shared_policy_and_empty_history_keep_the_expiry_watermark(tmp_path):
    from scope_recall.core.recall_log_storage import LogPolicy, RecallLogStore

    path = tmp_path / "recall.sqlite3"
    policy = LogPolicy(retention_days=2, max_bytes=131072)
    store = RecallLogStore(path, policy)
    store.write(_record("TEST-first", 1))
    RecallLogStore(path).prune("2026-09-10T00:00:00Z")
    store.write(_record("TEST-expired", 1))
    assert store.list() == []
    with pytest.raises(ValueError, match="policy"):
        RecallLogStore(path, LogPolicy(retention_days=30)).write(_record("TEST-conflict", 10))


def test_pinned_reader_does_not_allow_unbounded_wal_growth(tmp_path):
    from scope_recall.core.recall_log_storage import LogPolicy, RecallLogStore

    path = tmp_path / "recall.sqlite3"
    maximum = 131072
    store = RecallLogStore(path, LogPolicy(max_bytes=maximum))
    store.write(_record("TEST-base", 1))
    reader = sqlite3.connect(path, timeout=0)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM attempts").fetchone()
    try:
        try:
            store.write(_record("TEST-one-wal", 2))
        except sqlite3.OperationalError:
            pass
        lengths = []
        for i in range(12):
            with pytest.raises(sqlite3.OperationalError, match="busy"):
                store.write(_record(f"TEST-pinned-{i}", 2))
            lengths.append(sum(p.stat().st_size for p in tmp_path.iterdir() if p.is_file()))
        assert max(lengths) <= maximum
        assert len(set(lengths)) == 1
    finally:
        reader.rollback()
        reader.close()
    store.write(_record("TEST-after-reader", 3))
