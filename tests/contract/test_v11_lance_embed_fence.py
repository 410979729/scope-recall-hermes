"""P10 real native fenced publication tests with fixed vectors only."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import queue
import sqlite3
import time
from threading import Event

import pytest

from test_v11_claims import app, capture
from test_v11_deletion import authorize, request

from scope_recall.adapters.lance import LanceEmbedPort, LancePurgePort
from scope_recall.contracts import ContractError
from scope_recall.vector.process_store import ProcessLanceVectorStore


def _purge_port(store, ctx, spaces=("TEST-p10-space",)):
    return LancePurgePort(store, embedding_spaces=spaces,
                          agent_id=ctx.binding.agent_id,
                          installation_id=ctx.binding.installation_id)


def _physical_delete_receipt(core, ctx, source):
    authorize(core, ctx, source)
    deleted = core.forget(ctx, request(source), remaining_seconds=10)
    with core.storage.read(ctx) as tx:
        return dict(deleted, physical_members=tx.deletions.physical_members(deleted["operation_id"]))


def _native_row(source, ctx, *, revision=None, space="TEST-p10-space", installation_id=None):
    from scope_recall.adapters.lance import LanceVectorRecord, _record_row
    revision = revision or source.revision
    installation_id = installation_id or ctx.binding.installation_id
    return _record_row(LanceVectorRecord(
        "event", source.ref, revision, f"TEST:{source.ref}:{revision}:{space}:{installation_id}",
        space, (0.25, 0.75), source.scope_id, ctx.binding.agent_id,
        installation_id, source.project_id, source.branch_id,
    ))


def test_actual_purge_port_empty_inventory_waits_behind_native_grant(worker_app, tmp_path):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST actual purge empty inventory race")
    _mark_consolidation_done(core)
    item = _claim_embed(core, ctx, clock, "TEST-race-owner")
    epoch = core.status(ctx).memory_epoch
    writer = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    cleaner = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    writer.open(); cleaner.open_existing()
    approved, release, purge_started, purge_finished = Event(), Event(), Event(), Event()
    original_send = writer._send_fence_frame

    def delay_allow(encoded, timeout):
        if b'"approved": true' in encoded:
            approved.set()
            assert release.wait(15)
        original_send(encoded, timeout)

    writer._send_fence_frame = delay_allow
    try:
        port = _port(writer, ctx)
        prepared = port.prepare_source(source, remaining_seconds=20)
        with ThreadPoolExecutor(max_workers=2) as pool:
            publication = pool.submit(lambda: port.publish_source(
                prepared, source=source, lease_token=item.lease_token,
                lease_owner=item.lease_owner,
                lease_guard=lambda: _live_guard(core, ctx, clock, item, epoch), remaining_seconds=20))
            assert approved.wait(15)
            receipt = _physical_delete_receipt(core, ctx, source)

            def purge():
                purge_started.set()
                try:
                    return _purge_port(cleaner, ctx).purge_active(
                        receipt["operation_id"], receipt=receipt, remaining_seconds=20)
                finally:
                    purge_finished.set()

            cleanup = pool.submit(purge)
            assert purge_started.wait(5)
            assert not purge_finished.wait(0.3), "purge ACK overtook the native publication lock"
            release.set()
            publication.result(timeout=20)
            assert cleanup.result(timeout=20) is True
        cleaner.close(); cleaner.open_existing()
        assert cleaner.list_records() == {}
    finally:
        release.set(); writer.close(); cleaner.close()


def test_actual_purge_port_covers_old_revisions_and_spaces_without_other_identity(worker_app, tmp_path):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST vector revision one")
    newer = capture(core, ctx, "TEST vector revision two", key=source.event["source_event_key"], revision=2)
    store = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    store.open()
    try:
        rows = [_native_row(newer, ctx, revision=revision, space=space)
                for revision in (1, 2) for space in ("TEST-p10-space", "TEST-second-space")]
        other = _native_row(newer, ctx, installation_id="TEST-other-installation")
        store.upsert_records(rows + [other])
        receipt = _physical_delete_receipt(core, ctx, newer)
        assert _purge_port(store, ctx, ("TEST-p10-space", "TEST-second-space")).purge_active(
            receipt["operation_id"], receipt=receipt, remaining_seconds=10)
        store.close(); store.open_existing()
        assert set(store.list_records()) == {other["id"]}
    finally:
        store.close()


@pytest.mark.parametrize("corruption", ["json", "partition", "revision"])
def test_actual_purge_port_unknown_metadata_cannot_ack_empty(worker_app, tmp_path, corruption):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST damaged vector inventory")
    row = _native_row(source, ctx)
    if corruption == "json":
        row["target"] = "{broken"
    elif corruption == "partition":
        row["scope_id"] = "TEST-wrong-physical-partition"
    else:
        metadata = json.loads(row["target"])
        metadata["object_revision"] = True
        row["target"] = json.dumps(metadata)
    store = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    store.open()
    try:
        store.upsert_records([row])
        receipt = _physical_delete_receipt(core, ctx, source)
        assert _purge_port(store, ctx).purge_active(
            receipt["operation_id"], receipt=receipt, remaining_seconds=10) is False
        store.close(); store.open_existing()
        assert set(store.list_records()) == {row["id"]}
    finally:
        store.close()


def test_actual_purge_port_native_lock_wait_consumes_request_deadline(worker_app, tmp_path):
    """A caller that stops waiting leaves the healthy helper up and drains its frame later."""
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST purge lock deadline")
    writer = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    cleaner = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    writer.open(); cleaner.open_existing()
    helper = cleaner._process
    entered, release = Event(), Event()

    def guard():
        entered.set()
        assert release.wait(10)
        return False

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            publication = pool.submit(lambda: writer.fenced_upsert_records(
                [_native_row(source, ctx)], guard=guard, remaining_seconds=10))
            assert entered.wait(10)
            receipt = _physical_delete_receipt(core, ctx, source)
            started = time.monotonic()
            assert _purge_port(cleaner, ctx).purge_active(
                receipt["operation_id"], receipt=receipt, remaining_seconds=0.15) is False
            assert time.monotonic() - started < 2
            # Blocked behind another writer's native lock is slow, not broken:
            # the helper is kept and the frame it still owes is parked.
            assert cleaner.requires_reopen is False
            assert cleaner._process is helper and helper is not None and helper.poll() is None
            assert cleaner._pending_response_id is not None
            release.set()
            assert publication.result(timeout=10) is False
        # Without any reopen, the next request drains the owed frame first.
        assert _purge_port(cleaner, ctx).purge_active(
            receipt["operation_id"], receipt=receipt, remaining_seconds=10)
        assert cleaner._pending_response_id is None
        assert cleaner.requires_reopen is False
    finally:
        release.set(); writer.close(); cleaner.close()


def test_actual_purge_port_wedged_helper_is_reaped_after_pending_frame_timeout(worker_app, tmp_path):
    """A frame owed for longer than the helper timeout is a wedged helper, not a slow one."""
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST purge wedged helper")
    writer = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    cleaner = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    writer.open(); cleaner.open_existing()
    cleaner._pending_response_timeout = 0.2
    entered, release = Event(), Event()

    def guard():
        entered.set()
        assert release.wait(10)
        return False

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            publication = pool.submit(lambda: writer.fenced_upsert_records(
                [_native_row(source, ctx)], guard=guard, remaining_seconds=10))
            assert entered.wait(10)
            receipt = _physical_delete_receipt(core, ctx, source)
            port = _purge_port(cleaner, ctx)
            assert port.purge_active(receipt["operation_id"], receipt=receipt, remaining_seconds=0.15) is False
            assert cleaner.requires_reopen is False
            time.sleep(0.3)
            # Still blocked: the parked frame has now outlived the helper
            # timeout, so this request reaps the helper instead of spending
            # its own budget on the same frame.
            started = time.monotonic()
            assert port.purge_active(receipt["operation_id"], receipt=receipt, remaining_seconds=5) is False
            assert time.monotonic() - started < 2
            assert cleaner.requires_reopen is True
            release.set()
            assert publication.result(timeout=10) is False
        cleaner.close(); cleaner.open_existing()
        assert cleaner.requires_reopen is False
        assert _purge_port(cleaner, ctx).purge_active(
            receipt["operation_id"], receipt=receipt, remaining_seconds=10)
    finally:
        release.set(); writer.close(); cleaner.close()


class Clock:
    _now = "2026-09-06T12:00:00Z"
    _mono = 1000.0

    def utc_now(self):
        return self._now

    def monotonic(self):
        return self._mono

    def advance(self, seconds=0.0):
        self._mono += seconds
        current = datetime.fromisoformat(self._now.replace("Z", "+00:00"))
        self._now = (current + timedelta(seconds=seconds)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture
def worker_app(app):
    core, ctx = app
    clock = Clock()
    core.clock = clock
    return core, ctx, clock


class FixedEmbedding:
    def embed_source(self, source, *, remaining_seconds=1.0):
        return (0.25, 0.75)


def _mark_consolidation_done(core):
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='consolidate'")
        conn.commit()


def _claim_embed(core, ctx, clock, owner):
    with core.storage.write(ctx) as tx:
        claimed = tx.work.claim_next(owner, clock.utc_now(), lease_seconds=60, limit=1)
    assert len(claimed) == 1 and claimed[0].work_type == "embed"
    return claimed[0]


def _live_guard(core, ctx, clock, item, epoch):
    with core.storage.read(ctx) as tx:
        if tx.status().memory_epoch != epoch:
            return False
        if not tx.work._verify_lease(item.work_id, item.lease_token, item.lease_owner, now=clock.utc_now()):
            return False
        source = tx.source(item.subject_ref, item.subject_revision)
        if source is None:
            return False
        latest = tx._check().execute(
            "SELECT max(source_revision) FROM source_events WHERE event_id=? AND read_blocked=0",
            (item.subject_ref,),
        ).fetchone()[0]
        return latest == item.subject_revision


def _port(store, ctx):
    return LanceEmbedPort(
        store,
        FixedEmbedding(),
        agent_id=ctx.binding.agent_id,
        installation_id=ctx.binding.installation_id,
        embedding_space="TEST-p10-space",
    )


def test_real_lance_embed_worker_publishes_fixed_vector(worker_app, tmp_path):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST P10 fixed vector source")
    _mark_consolidation_done(core)
    store = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    store.open()
    try:
        receipt = core.drain_worker(ctx, max_items=1, remaining_seconds=20, owner_id="lance-worker", embed=_port(store, ctx))
        assert receipt.completed == 1
        records = store.list_records()
        assert len(records) == 1
        row = next(iter(records.values()))
        metadata = json.loads(row["target"])
        assert metadata["object_ref"] == source.ref
        assert metadata["object_revision"] == source.revision
        assert row["content"] == ""
    finally:
        store.close()


def test_purge_waits_for_granted_native_write_and_removes_active_vector(worker_app, tmp_path):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST P10 purge after granted native write")
    _mark_consolidation_done(core)
    item = _claim_embed(core, ctx, clock, "lance-purge-a")
    epoch = core.status(ctx).memory_epoch
    store = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    store.open()
    try:
        port = _port(store, ctx)
        prepared = port.prepare_source(source, remaining_seconds=10)
        granted = Event()

        def guard():
            granted.set()
            return _live_guard(core, ctx, clock, item, epoch)

        errors = []

        def publish():
            try:
                port.publish_source(prepared, source=source, lease_token=item.lease_token,
                                    lease_owner=item.lease_owner, lease_guard=guard, remaining_seconds=10)
            except Exception as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            publish_future = pool.submit(publish)
            assert granted.wait(15)
            authorize(core, ctx, source)
            deleted = core.forget(ctx, request(source), remaining_seconds=10)
            purged = core.purge_sqlite(ctx, deleted["operation_id"], remaining_seconds=10)
            assert purged["active_content_removed"] is False
            assert purged["layers"]["sqlite_active"] == "removed"
            assert purged["layers"]["vector_active"] == "inventory_pending"
            purge_future = pool.submit(store.delete_by_ids, [prepared.vector_id])
            publish_future.result(timeout=20)
            purge_future.result(timeout=20)
        assert errors == []
        assert store.count_rows() == 0
        with sqlite3.connect(core.storage.path) as conn:
            assert conn.execute("SELECT read_blocked FROM source_events WHERE event_id=? AND source_revision=?",
                                (source.ref, source.revision)).fetchone()[0] == 1
    finally:
        store.close()


def test_worker_purge_acknowledges_active_vector_only_after_native_delete(worker_app, tmp_path):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST P10 worker physical purge")
    _mark_consolidation_done(core)
    store = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    store.open()
    try:
        # The normal worker path owns the publication fence; the fixed vector
        # is only a deterministic native test dependency.
        assert core.drain_worker(
            ctx, max_items=1, remaining_seconds=20, owner_id="purge-embed", embed=_port(store, ctx)
        ).completed == 1
        authorize(core, ctx, source)
        deleted = core.forget(ctx, request(source), remaining_seconds=10)

        result = core.drain_worker(
            ctx,
            max_items=1,
            remaining_seconds=20,
            owner_id="purge-native",
            purge=LancePurgePort(store, agent_id=ctx.binding.agent_id, installation_id=ctx.binding.installation_id, embedding_spaces=("TEST-p10-space",)),
        )
        assert result.completed == 1
        assert result.items[0].state == "done"
        assert store.count_rows() == 0
        with core.storage.read(ctx) as tx:
            receipt = tx.deletions.receipt(deleted["operation_id"])
        assert receipt["active_content_removed"] is True
        assert receipt["layers"]["sqlite_active"] == "removed"
        assert receipt["layers"]["vector_active"] == "removed"
        assert receipt["layers"]["attachments"] == "removed"
    finally:
        store.close()


def test_purge_port_keeps_unknown_embedding_space_pending(worker_app, tmp_path):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST P10 unknown active vector space")
    _mark_consolidation_done(core)
    store = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    store.open()
    try:
        assert core.drain_worker(
            ctx, max_items=1, remaining_seconds=20, owner_id="unknown-space-embed", embed=_port(store, ctx)
        ).completed == 1
        authorize(core, ctx, source)
        deleted = core.forget(ctx, request(source), remaining_seconds=10)
        receipt = core.drain_worker(
            ctx,
            max_items=1,
            remaining_seconds=20,
            owner_id="unknown-space-purge",
            purge=LancePurgePort(store, agent_id=ctx.binding.agent_id, installation_id=ctx.binding.installation_id, embedding_spaces=("TEST-unregistered-space",)),
        )
        assert receipt.retried == 1
        assert store.count_rows() == 1
        with core.storage.read(ctx) as tx:
            current = tx.deletions.receipt(deleted["operation_id"])
        assert current["layers"]["vector_active"] == "inventory_pending"
        assert current["active_content_removed"] is False
    finally:
        store.close()


def test_native_guard_denies_new_revision_before_physical_commit(worker_app, tmp_path):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST P10 old revision")
    _mark_consolidation_done(core)
    item = _claim_embed(core, ctx, clock, "lance-revision")
    epoch = core.status(ctx).memory_epoch
    store = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    store.open()
    try:
        port = _port(store, ctx)
        prepared = port.prepare_source(source, remaining_seconds=10)
        newer = capture(core, ctx, "TEST P10 newer revision", key=source.event["source_event_key"], revision=2)
        assert newer.ref == source.ref and newer.revision == 2
        with pytest.raises(ContractError, match="publication_fence"):
            port.publish_source(prepared, source=source, lease_token=item.lease_token,
                                lease_owner=item.lease_owner,
                                lease_guard=lambda: _live_guard(core, ctx, clock, item, epoch), remaining_seconds=10)
        assert store.count_rows() == 0
    finally:
        store.close()


def test_native_guard_denies_epoch_flip_before_physical_commit(worker_app, tmp_path):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST P10 epoch fence")
    _mark_consolidation_done(core)
    item = _claim_embed(core, ctx, clock, "lance-epoch")
    epoch = core.status(ctx).memory_epoch
    store = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    store.open()
    try:
        port = _port(store, ctx)
        prepared = port.prepare_source(source, remaining_seconds=10)
        with sqlite3.connect(core.storage.path) as conn:
            conn.execute("UPDATE instance_meta SET memory_epoch=memory_epoch+1 WHERE singleton=1")
            conn.commit()
        with pytest.raises(ContractError, match="publication_fence"):
            port.publish_source(prepared, source=source, lease_token=item.lease_token,
                                lease_owner=item.lease_owner,
                                lease_guard=lambda: _live_guard(core, ctx, clock, item, epoch), remaining_seconds=10)
        assert store.count_rows() == 0
    finally:
        store.close()


def test_shared_id_lease_reuse_old_owner_cannot_delete_new_owner_vector(worker_app, tmp_path):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST P10 shared vector id lease reuse")
    _mark_consolidation_done(core)
    item_a = _claim_embed(core, ctx, clock, "lance-owner-a")
    epoch = core.status(ctx).memory_epoch
    store_a = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    store_b = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    store_a.open()
    store_b.open_existing()
    try:
        port_a = _port(store_a, ctx)
        port_b = _port(store_b, ctx)
        prepared = port_a.prepare_source(source, remaining_seconds=10)
        entered = Event()
        release = Event()
        def guard_a():
            entered.set()
            release.wait(15)
            return _live_guard(core, ctx, clock, item_a, epoch)
        errors_a = []
        def publish_a():
            try:
                port_a.publish_source(prepared, source=source, lease_token=item_a.lease_token,
                                      lease_owner=item_a.lease_owner, lease_guard=guard_a, remaining_seconds=20)
            except Exception as exc:
                errors_a.append(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(publish_a)
            assert entered.wait(15)
            clock.advance(61)
            with core.storage.write(ctx) as tx:
                tx.work.release_stale(clock.utc_now())
                claimed_b = tx.work.claim_next("lance-owner-b", clock.utc_now(), lease_seconds=60, limit=1)
            assert len(claimed_b) == 1
            item_b = claimed_b[0]
            future_b = pool.submit(lambda: port_b.publish_source(
                prepared, source=source, lease_token=item_b.lease_token, lease_owner=item_b.lease_owner,
                lease_guard=lambda: _live_guard(core, ctx, clock, item_b, epoch), remaining_seconds=20))
            release.set()
            future_a.result(timeout=20)
            future_b.result(timeout=20)
        assert len(errors_a) == 1 and isinstance(errors_a[0], ContractError)
        assert errors_a[0].code == "VERSION_CONFLICT"
        assert store_b.count_rows() == 1
    finally:
        store_a.close()
        store_b.close()


def test_native_guard_false_is_bounded_no_commit(worker_app, tmp_path):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST P10 bounded guard denial")
    _mark_consolidation_done(core)
    item = _claim_embed(core, ctx, clock, "lance-deny")
    store = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    store.open()
    try:
        port = _port(store, ctx)
        prepared = port.prepare_source(source, remaining_seconds=2)
        with pytest.raises(ContractError, match="publication_fence"):
            port.publish_source(prepared, source=source, lease_token=item.lease_token,
                                lease_owner=item.lease_owner, lease_guard=lambda: False, remaining_seconds=0.5)
        assert store.count_rows() == 0
    finally:
        store.close()


def test_native_helper_eof_before_guard_allow_fails_closed(worker_app, tmp_path):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST P10 helper EOF before grant")
    _mark_consolidation_done(core)
    item = _claim_embed(core, ctx, clock, "lance-eof")
    epoch = core.status(ctx).memory_epoch
    store = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    store.open()
    try:
        port = _port(store, ctx)
        prepared = port.prepare_source(source, remaining_seconds=10)

        def kill_before_reply():
            process = store._process
            assert process is not None
            process.kill()
            return True

        with pytest.raises(RuntimeError, match="fence failed"):
            port.publish_source(
                prepared,
                source=source,
                lease_token=item.lease_token,
                lease_owner=item.lease_owner,
                lease_guard=kill_before_reply,
                remaining_seconds=10,
            )
        assert store.requires_reopen
    finally:
        store.close()
    # The helper died while holding the advisory lock but before approval; a
    # fresh native process sees no publication and can reopen the same table.
    recovered = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    recovered.open_existing()
    try:
        assert recovered.count_rows() == 0
    finally:
        recovered.close()


def test_granted_channel_death_allows_native_commit_then_same_lock_purge(worker_app, tmp_path):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST P10 granted host channel death")
    _mark_consolidation_done(core)
    item = _claim_embed(core, ctx, clock, "lance-granted-death")
    epoch = core.status(ctx).memory_epoch
    store = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    store.open()
    try:
        port = _port(store, ctx)
        prepared = port.prepare_source(source, remaining_seconds=10)
        original_send = store._send_fence_frame
        closed = {"value": False}

        def send_then_close(encoded, timeout):
            original_send(encoded, timeout)
            if b'"approved": true' in encoded and not closed["value"]:
                closed["value"] = True
                stream = store._process.stdin
                if stream is not None:
                    stream.close()

        store._send_fence_frame = send_then_close
        try:
            port.publish_source(
                prepared,
                source=source,
                lease_token=item.lease_token,
                lease_owner=item.lease_owner,
                lease_guard=lambda: _live_guard(core, ctx, clock, item, epoch),
                remaining_seconds=10,
            )
        except RuntimeError:
            # Closing the host channel may lose the terminal response even
            # though the helper was already granted and owns the lock.
            pass
    finally:
        store.close()
    recovered = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    recovered.open_existing()
    try:
        assert recovered.count_rows() == 1
        recovered.delete_by_ids([prepared.vector_id])
        assert recovered.count_rows() == 0
    finally:
        recovered.close()


def test_native_commit_with_lost_ack_reopens_and_retries_physical_purge(worker_app, tmp_path):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST P10 lost native acknowledgement")
    _mark_consolidation_done(core)
    store = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    store.open()
    try:
        assert core.drain_worker(
            ctx, max_items=1, remaining_seconds=20, owner_id="lost-ack-embed", embed=_port(store, ctx)
        ).completed == 1
        authorize(core, ctx, source)
        deleted = core.forget(ctx, request(source), remaining_seconds=10)
        actual = LancePurgePort(store, agent_id=ctx.binding.agent_id, installation_id=ctx.binding.installation_id, embedding_spaces=("TEST-p10-space",))

        class LostAck:
            def purge_active(self, operation_id, *, receipt, remaining_seconds):
                assert actual.purge_active(operation_id, receipt=receipt, remaining_seconds=remaining_seconds)
                raise RuntimeError("TEST lost purge acknowledgement")

        first = core.drain_worker(ctx, max_items=1, remaining_seconds=20, owner_id="lost-ack-purge", purge=LostAck())
        assert first.retried == 1
        clock.advance(4)
        with core.storage.read(ctx) as tx:
            pending = tx.deletions.receipt(deleted["operation_id"])
        assert pending["active_content_removed"] is False
        second = core.drain_worker(ctx, max_items=1, remaining_seconds=20, owner_id="lost-ack-retry", purge=actual)
        assert second.completed == 1
        with core.storage.read(ctx) as tx:
            done = tx.deletions.receipt(deleted["operation_id"])
        assert done["layers"]["vector_active"] == "removed"
        assert done["active_content_removed"] is True
        assert store.count_rows() == 0
    finally:
        store.close()


def test_native_commit_lost_terminal_ack_is_recovered_by_physical_purge(worker_app, tmp_path):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST P10 commit then lost terminal ACK")
    _mark_consolidation_done(core)
    item = _claim_embed(core, ctx, clock, "lance-lost-terminal")
    epoch = core.status(ctx).memory_epoch
    store = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    store.open()
    try:
        port = _port(store, ctx)
        prepared = port.prepare_source(source, remaining_seconds=10)
        base_queue = store._responses

        class DropTerminal:
            def __init__(self, delegate):
                self.delegate = delegate
                self.drop = False

            def get(self, timeout=None):
                if self.drop:
                    # Consume the real terminal response first, so the native
                    # merge is complete before the host loses its ACK.
                    self.delegate.get(timeout=timeout)
                    raise queue.Empty
                return self.delegate.get(timeout=timeout)

        def guard():
            store._responses = DropTerminal(base_queue)
            store._responses.drop = True
            return _live_guard(core, ctx, clock, item, epoch)

        with pytest.raises(RuntimeError, match="fence failed"):
            port.publish_source(
                prepared,
                source=source,
                lease_token=item.lease_token,
                lease_owner=item.lease_owner,
                lease_guard=guard,
                remaining_seconds=10,
            )
    finally:
        store.close()
    recovered = ProcessLanceVectorStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    recovered.open_existing()
    try:
        # The native merge occurred before the host intentionally discarded
        # the terminal frame; physical purge must still remove the row.
        assert recovered.count_rows() == 1
        recovered.delete_by_ids([prepared.vector_id])
        assert recovered.count_rows() == 0
    finally:
        recovered.close()
