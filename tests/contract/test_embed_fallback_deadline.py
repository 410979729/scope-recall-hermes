"""Single-request embedding ports preserve progress within one shared lease and deadline."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from scope_recall.core.failure_policy import CAPACITY_RETRY, PROVIDER_RECHECK, WORK_RETRY
from scope_recall.core.worker import WorkerConfig, drain_worker
from scope_recall.runtime import lance_port
from scope_recall.runtime.models import AuxiliaryModelError
from scope_recall.runtime.vector_port import VectorEmbedPort
from test_claims import app
from test_embed_group_bookkeeping import SteppingClock, _claims, _embed_rows, _sources
from test_private_mode import CountingEmbed


class SingleEmbedding:
    """Each successful request advances only the injected clock."""

    def __init__(self, clock, timeout=False):
        self.clock = clock
        self.budgets = []
        self.successes = 0
        self.timeout = timeout

    def embed_source(self, source, *, remaining_seconds):
        self.budgets.append(remaining_seconds)
        self.clock.seconds += min(4, remaining_seconds)
        if self.timeout and remaining_seconds < 4:
            raise AuxiliaryModelError("timeout")
        self.successes += 1
        return (0.5, 0.5)

    def embed_text(self, text, *, remaining_seconds):
        return self.embed_source(text, remaining_seconds=remaining_seconds)


class FencedWriter:
    def __init__(self):
        self.written = []

    def upsert_fenced_many(self, records, *, guard, remaining_seconds):
        assert remaining_seconds > 0 and guard()
        self.written.extend(records)
        return True


@pytest.mark.parametrize("kind", ["source", "claim"])
@pytest.mark.parametrize("timeout", [False, True])
def test_single_embedding_fallback_publishes_progress_before_shared_deadline(app, monkeypatch, kind, timeout):
    core, ctx = app
    (_sources(core, ctx, 32, tag="fallback") if kind == "source" else _claims(core, ctx, 16))
    clock = SteppingClock()
    core.clock = clock
    monkeypatch.setattr(lance_port, "time", SimpleNamespace(monotonic=clock.monotonic))
    embedding, writer = SingleEmbedding(clock, timeout), FencedWriter()
    port = VectorEmbedPort(
        object(),
        embedding,
        agent_id=ctx.binding.agent_id,
        installation_id=ctx.binding.installation_id,
        embedding_space="TEST-space",
        writer=writer,
    )
    receipt = core.drain_worker(ctx, max_items=64, remaining_seconds=120, owner_id="TEST-fallback", embed=port)
    done = [ref for ref, state, _attempt in _embed_rows(core) if state == "done"]
    assert done and receipt.completed == len(done), "successful vectors were discarded before publication"
    assert len(writer.written) == len(done)
    assert embedding.successes == len(done), "a successful embedding was paid for without durable progress"
    assert any(right < left for left, right in zip(embedding.budgets, embedding.budgets[1:], strict=False)), (
        embedding.budgets
    )
    assert clock.seconds < 120
    assert not [item for item in receipt.items if item.disposition == "stale"]
    assert {state for _ref, state, _attempt in _embed_rows(core)} <= {"pending", "done"}


def test_short_embedding_pass_reserves_a_fraction_of_its_budget(app):
    core, ctx = app
    _sources(core, ctx, 3, tag="short-pass")
    core.clock = SteppingClock()
    port = CountingEmbed()
    receipt = core.drain_worker(ctx, max_items=8, remaining_seconds=1, embed=port)
    assert receipt.completed == len(port.sources) == 3


def test_positive_short_leases_do_not_reclaim_a_group_without_progress(app, monkeypatch):
    from scope_recall.core.work_storage import WorkItems

    core, ctx = app
    _sources(core, ctx, 3, tag="short-lease")
    core.clock = SteppingClock()
    claim = WorkItems.claim_next
    calls = []

    def bounded_claim(self, *args, **kwargs):
        calls.append(1)
        assert len(calls) <= 4, "the same unattempted group was reclaimed without progress"
        return claim(self, *args, **kwargs)

    monkeypatch.setattr(WorkItems, "claim_next", bounded_claim)
    port = CountingEmbed()
    receipt = core.drain_worker(ctx, max_items=8, lease_seconds=1, remaining_seconds=30, embed=port)
    assert receipt.completed == len(port.sources) == 3


@pytest.mark.parametrize("count", [1, 3])
def test_single_and_tail_embedding_keep_a_publication_window_inside_the_lease(app, count):
    core, ctx = app
    _sources(core, ctx, count, tag="single-lease")
    clock = core.clock = SteppingClock()
    clock.seconds = 1.001

    class SlowEmbed(CountingEmbed):
        def prepare_source(self, source, *, remaining_seconds):
            assert 0 < remaining_seconds < 0.1, "the request escaped its effective lease"
            clock.seconds += min(0.12, remaining_seconds)
            return super().prepare_source(source, remaining_seconds=remaining_seconds)

    port = SlowEmbed()
    receipt = drain_worker(
        core.storage,
        clock,
        ctx,
        embed=port,
        remaining_seconds=30,
        config=WorkerConfig("TEST-single-lease", lease_seconds=0.1, max_items=8, embed_batch_limit=2),
    )
    assert receipt.completed == len(port.sources) == count
    assert receipt.stale == 0


@pytest.mark.parametrize("kind", ["source", "claim"])
@pytest.mark.parametrize("error", ["http_429", "http_503", "input_invalid", "credential_missing", "timeout"])
def test_single_fallback_keeps_successful_prefix_and_records_tail_failure(app, monkeypatch, kind, error):
    core, ctx = app
    if kind == "source":
        _sources(core, ctx, 3, tag="prefix-error")
    else:
        _claims(core, ctx, 3)
        with core.storage.write(ctx) as tx:
            tx._check(write=True).execute(
                "UPDATE work_items SET state='done' WHERE work_type='embed' AND subject_ref NOT LIKE 'claim-%'"
            )
    clock = core.clock = SteppingClock()
    monkeypatch.setattr(lance_port, "time", SimpleNamespace(monotonic=clock.monotonic))

    class RefusingEmbedding(SingleEmbedding):
        def embed_source(self, source, *, remaining_seconds):
            if self.successes:
                self.budgets.append(remaining_seconds)
                raise AuxiliaryModelError(error)
            return super().embed_source(source, remaining_seconds=remaining_seconds)

    embedding, writer = RefusingEmbedding(clock), FencedWriter()
    port = VectorEmbedPort(
        object(),
        embedding,
        agent_id=ctx.binding.agent_id,
        installation_id=ctx.binding.installation_id,
        embedding_space="TEST-space",
        writer=writer,
    )
    receipt = core.drain_worker(ctx, max_items=16, remaining_seconds=120, embed=port)
    assert receipt.completed == len(writer.written) == embedding.successes == 1
    assert len(embedding.budgets) == 2, "the refused request type was asked again in the same pass"
    with core.storage.read(ctx) as tx:
        rows = list(
            tx._check().execute("SELECT state,last_error_code,available_at FROM work_items WHERE work_type='embed'")
        )
    assert all(row[0] != "leased" for row in rows)
    tail = [row for row in rows if row[0] != "done"]
    assert tail and all(error in row[1] for row in tail)
    assert sum(row[0] == "failed" for row in tail) == (1 if error == "input_invalid" else 0)
    expected_delay = (
        CAPACITY_RETRY.first
        if error in {"http_429", "http_503"}
        else PROVIDER_RECHECK.first
        if error == "credential_missing"
        else WORK_RETRY.first
    )
    now = datetime.fromisoformat(clock.utc_now().replace("Z", "+00:00"))
    assert all(
        row[0] == "failed"
        or (datetime.fromisoformat(row[2].replace("Z", "+00:00")) - now).total_seconds() == expected_delay
        for row in tail
    )


@pytest.mark.parametrize("kind", ["source", "claim"])
@pytest.mark.parametrize("shape", ["unidentified", "reordered", "whole_group"])
def test_malformed_partial_failure_stops_without_charging_a_member(app, kind, shape):
    from scope_recall.contracts import ContractError
    from scope_recall.core.worker_projection import PartialEmbeddingFailure

    core, ctx = app
    if kind == "source":
        _sources(core, ctx, 3, tag="malformed-prefix")
    else:
        _claims(core, ctx, 3)
        with core.storage.write(ctx) as tx:
            tx._check(write=True).execute(
                "UPDATE work_items SET state='done' WHERE work_type='embed' AND subject_ref NOT LIKE 'claim-%'"
            )
    core.clock = SteppingClock()
    calls = []

    class MalformedPort:
        def prepare_sources(self, subjects, *, remaining_seconds):
            calls.append(len(subjects))
            identified = tuple(
                SimpleNamespace(source_ref=subject.ref, source_revision=subject.revision) for subject in subjects
            )
            prefix = (
                (object(),) if shape == "unidentified" else identified[1::-1] if shape == "reordered" else identified
            )
            raise PartialEmbeddingFailure(prefix, ContractError("INPUT_INVALID"))

        prepare_claims = prepare_sources

        def prepare_source(self, *args, **kwargs):
            pytest.fail("a malformed group was asked again as individual requests")

        prepare_claim = prepare_source

        def publish_source(self, *args, **kwargs):
            pytest.fail("an unverified partial vector was published")

        publish_claim = publish_source
        publish_sources = publish_source
        publish_claims = publish_source

    receipt = core.drain_worker(ctx, max_items=16, remaining_seconds=30, embed=MalformedPort())
    assert calls == [3]
    assert receipt.failed == receipt.retried == receipt.completed == receipt.stale == 0
    assert receipt.deferred == 3
    with core.storage.read(ctx) as tx:
        rows = list(
            tx._check().execute(
                "SELECT state,attempt,last_error_code,available_at FROM work_items "
                "WHERE work_type='embed' AND state<>'done'"
            )
        )
    assert len(rows) == 3
    assert all(row[0] == "pending" and row[1] == 0 for row in rows)
    assert all(row[2] == "DERIVATION_INVALID" and row[3] > core.clock.utc_now() for row in rows)
