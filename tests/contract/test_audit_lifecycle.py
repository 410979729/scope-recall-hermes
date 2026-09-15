"""Synthetic authorization and restore races; no host or model calls."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core.restore import InstallationMaintenance, begin_restore, export_deletion_ledger, ledger_digest, replay_deletion_ledger
from test_v11_deletion import app, capture, initial, request, authorize, sqlite_backup
from v11_support import source_event


@pytest.mark.parametrize("prefix", [
    "不要忘记 ", "别忘记 ", "不能删除 ", "不必忘掉 ",
    "客户原文：删除 ", "如果成功就删除 ", "举例：清除 ",
    "never forget ", "must not delete ", "don't forget ",
])
def test_negated_reported_or_conditional_text_cannot_authorize_forget(app, prefix):
    core, ctx = app
    item, source = initial(core, ctx)
    capture(core, ctx, prefix + source.ref)
    before = core.storage.path.read_bytes()
    with pytest.raises(ContractError, match="forget_not_authorized"):
        core.forget(ctx, request(source))
    assert core.storage.path.read_bytes() == before
    assert core.current_claim(ctx, item.ref) is not None
    assert core.source(ctx, source.ref, 1) is not None


def test_restore_rejects_a_checkpoint_superseded_by_a_new_capture(app):
    core, ctx = app
    authority = InstallationMaintenance(ctx)
    digest = ledger_digest(export_deletion_ledger(core.storage, authority))
    capture(core, ctx, "TEST newly committed source after checkpoint")
    with pytest.raises(ContractError, match="checkpoint_changed"):
        begin_restore(core.storage, authority, expected_ledger_sha256=digest)
    assert not (ctx.binding.data_directory / "restore-required.json").exists()


def test_restore_waits_for_existing_writer_then_rechecks_checkpoint(app, monkeypatch):
    core, ctx = app
    authority = InstallationMaintenance(ctx)
    digest = ledger_digest(export_deletion_ledger(core.storage, authority))
    attempt = threading.Event()
    actual_open = core.storage._open

    def tracked_open(*args, **kwargs):
        if threading.current_thread().name.startswith("TEST-restorer"):
            attempt.set()
        return actual_open(*args, **kwargs)

    monkeypatch.setattr(core.storage, "_open", tracked_open)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="TEST-restorer") as pool:
        with core.storage.write(ctx) as tx:
            future = pool.submit(begin_restore, core.storage, authority, expected_ledger_sha256=digest)
            assert attempt.wait(2)
            assert not (ctx.binding.data_directory / "restore-required.json").exists()
            tx._check(write=True).execute("UPDATE instance_meta SET memory_epoch=memory_epoch+1")
        with pytest.raises(ContractError, match="checkpoint_changed"):
            future.result(timeout=3)
    assert not (ctx.binding.data_directory / "restore-required.json").exists()


def test_writer_preopened_before_restore_cannot_cross_the_new_fence(app, monkeypatch):
    core, ctx = app
    authority = InstallationMaintenance(ctx)
    digest = ledger_digest(export_deletion_ledger(core.storage, authority))
    opened, resume = threading.Event(), threading.Event()
    actual_open = core.storage._open

    def paused_open(*args, **kwargs):
        conn = actual_open(*args, **kwargs)
        if threading.current_thread().name.startswith("TEST-preopened"):
            opened.set()
            assert resume.wait(3)
        return conn

    monkeypatch.setattr(core.storage, "_open", paused_open)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="TEST-preopened") as pool:
        future = pool.submit(core.record_event, ctx, source_event(content="TEST must remain uncaptured"),
                             scope_id="TEST-scope", remaining_seconds=10)
        try:
            assert opened.wait(2)
            begin_restore(core.storage, authority, expected_ledger_sha256=digest)
        finally:
            resume.set()
        with pytest.raises(ContractError, match="RESTORE_UNVERIFIED"):
            future.result(timeout=3)


def test_restored_attachments_and_vectors_require_fresh_physical_purge(app, tmp_path):
    from test_v11_episodes import artifact

    core, ctx = app
    item, source, _ = artifact(core, ctx, tmp_path)
    blob_path = ctx.binding.data_directory / item.blob.relative_path
    original_bytes = blob_path.read_bytes()
    snapshot = tmp_path / "TEST-before-delete.sqlite3"
    sqlite_backup(core.storage.path, snapshot)

    class Purge:
        calls = 0

        def purge_active(self, operation_id, *, receipt, remaining_seconds):
            self.calls += 1
            assert receipt["read_blocked"] and receipt["physical_members"]
            return True

    port = Purge()
    authorize(core, ctx, source)
    deleted = core.forget(ctx, request(source), remaining_seconds=10)
    assert core.drain_worker(ctx, purge=port, remaining_seconds=10).completed >= 1
    assert not blob_path.exists()
    authority = InstallationMaintenance(ctx)
    ledger = export_deletion_ledger(core.storage, authority)
    begin_restore(core.storage, authority, expected_ledger_sha256=ledger_digest(ledger))
    sqlite_backup(snapshot, core.storage.path)
    blob_path.write_bytes(original_bytes)  # Simulate restoring the old retained backup.
    replay_deletion_ledger(core.storage, authority, ledger)
    assert core.source(ctx, source.ref, 1) is None and core.artifact(ctx, item.ref, 1) is None
    with core.storage.read(ctx) as tx:
        receipt = tx.deletions.receipt(deleted["operation_id"])
        assert not receipt["active_content_removed"]
        assert receipt["layers"]["vector_active"] == "inventory_pending"
        assert receipt["layers"]["attachments"] == "inventory_pending"
        assert tx._check().execute("SELECT count(*) FROM work_items WHERE work_type='purge' AND state='pending'").fetchone()[0] == 1
    result = core.drain_worker(ctx, purge=port, remaining_seconds=10)
    assert result.completed >= 1 and port.calls == 2 and not blob_path.exists()
