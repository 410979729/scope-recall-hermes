"""An ingress capture cannot survive an intervening entry suspension."""

from scope_recall.core.capture import record_event
from scope_recall.core.capture_inbox import enqueue
from scope_recall.core.management import MemoryManagement
from scope_recall.core.restore import InstallationMaintenance

from tests.contract.test_claims import app
from tests.v11_support import source_event


def test_queued_before_pause_is_not_replayed_after_resume(app):
    core, context = app
    token, prepared = enqueue(
        core.storage,
        core.clock,
        context,
        source_event(content="TEST queued before pause"),
        scope_id="TEST-scope",
        host_scope=None,
        remaining_seconds=10,
    )
    owner = MemoryManagement(core, InstallationMaintenance(context))
    owner.set_entry_state("local", "paused", actor="TEST-owner")
    owner.set_entry_state("local", "normal", actor="TEST-owner")
    receipt = record_event(
        core.storage,
        core.clock,
        context,
        {},
        scope_id="TEST-scope",
        remaining_seconds=10,
        _prepared=prepared,
        _inbox_token=token,
    )
    assert receipt.durability == "not_persisted"
    assert "entry_state:paused" in receipt.gaps


def test_worker_replay_preserves_ingress_version(app):
    from scope_recall.core.capture_inbox import replay_inbox

    core, context = app
    token, _prepared = enqueue(
        core.storage,
        core.clock,
        context,
        source_event(content="TEST queued replay"),
        scope_id="TEST-scope",
        host_scope=None,
        remaining_seconds=10,
    )
    assert token
    owner = MemoryManagement(core, InstallationMaintenance(context))
    owner.set_entry_state("local", "read-only", actor="TEST-owner")
    owner.set_entry_state("local", "normal", actor="TEST-owner")
    receipts = replay_inbox(
        core.storage, core.clock, context, authorize=lambda _: context.allowed_scope_ids, remaining_seconds=10
    )
    assert len(receipts) == 1
    assert receipts[0].durability == "not_persisted"
    assert "entry_state:read-only" in receipts[0].gaps
    with core.storage.read(context) as tx:
        assert tx._check().execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 0
        assert tx._check().execute("SELECT count(*) FROM source_events").fetchone()[0] == 0
