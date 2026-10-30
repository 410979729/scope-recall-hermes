"""A replayed revise request id answers with its committed receipt, not a second mutation.

The client this covers cannot confirm a write.  It re-sends the same request id
with the ``expected_revision`` the first call already consumed, so without a
receipt it would either duplicate the fact or report ``VERSION_CONFLICT`` —
indistinguishable from a genuine concurrent writer, the one outcome a client must
not silently absorb.
"""

import itertools
from dataclasses import replace

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core import CoreConfig, MemoryCore

from tests.contract.test_claims import Clock, capture, counts, initial, revise_request
from tests.v11_support import context


@pytest.fixture
def app(tmp_path):
    ctx = replace(context(tmp_path / "TEST-revise-replay"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    return core, ctx


def _correction(core, ctx, value, *, when="2026-09-03T12:00:00Z"):
    """An explicit correction: capture applies a bare "replace X with Y" itself."""
    return capture(core, ctx, f"Please correct TEST-project 配色: {value}。", when=when)


def test_replayed_request_id_answers_with_the_committed_receipt(app):
    core, ctx = app
    item, _ = initial(core, ctx, value="H100")
    source = _correction(core, ctx, "H200")
    request = revise_request(item, source, "H200")

    first = core.revise(ctx, request, request_id="TEST-replay-0001", remaining_seconds=10)
    assert first.replayed is False
    after_first = counts(core)
    epoch_after_first = core.status(ctx).memory_epoch
    history_after_first = len(core.claim_history(ctx, item.ref))
    bytes_after_first = core.storage.path.read_bytes()

    replay = core.revise(ctx, request, request_id="TEST-replay-0001", remaining_seconds=10)
    assert replay.replayed is True
    # The archived outcome, not a new one: same claim, same disposition, same state.
    assert replay.items[0].ref == first.items[0].ref
    assert replay.items[0].revision == first.items[0].revision
    assert replay.items[0].disposition == first.items[0].disposition
    assert replay.items[0].state == first.items[0].state
    # Read-only: no second version, no new work, no epoch bump.
    assert counts(core) == after_first
    assert core.status(ctx).memory_epoch == epoch_after_first
    assert len(core.claim_history(ctx, item.ref)) == history_after_first
    assert core.storage.path.read_bytes() == bytes_after_first


def test_same_id_with_a_different_body_is_a_reuse_not_a_replay(app):
    core, ctx = app
    item, _ = initial(core, ctx, value="H100")
    source = _correction(core, ctx, "H200")
    core.revise(ctx, revise_request(item, source, "H200"), request_id="TEST-replay-0002", remaining_seconds=10)
    held = core.claim_history(ctx, item.ref)

    other = revise_request(item, source, "H400")
    with pytest.raises(ContractError) as refused:
        core.revise(ctx, other, request_id="TEST-replay-0002")
    assert refused.value.code == "REQUEST_ID_REUSED"
    assert refused.value.field == "request_id"
    # The refusal wrote nothing either.
    assert core.claim_history(ctx, item.ref) == held


def test_a_fresh_id_is_applied_even_after_a_replay(app):
    """Only the id is idempotent: a genuinely new request still writes."""
    core, ctx = app
    item, _ = initial(core, ctx, value="H100")
    source = _correction(core, ctx, "H200")
    first = core.revise(ctx, revise_request(item, source, "H200"), request_id="TEST-replay-0003")
    core.revise(ctx, revise_request(item, source, "H200"), request_id="TEST-replay-0003")

    later = _correction(core, ctx, "H400", when="2026-09-04T12:00:00Z")
    second = core.revise(
        ctx, revise_request(first.items[0], later, "H400"), request_id="TEST-replay-0004", remaining_seconds=10
    )
    assert second.replayed is False
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "H400"


def test_without_a_request_id_nothing_is_recorded(app):
    """The envelope id a host mints is echo-only; an ordinary call writes no receipt."""
    core, ctx = app
    item, _ = initial(core, ctx, value="H100")
    source = _correction(core, ctx, "H200")
    core.revise(ctx, revise_request(item, source, "H200"))

    import sqlite3

    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT count(*) FROM mutation_receipts").fetchone()[0] == 0
