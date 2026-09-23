"""P09 RecallPacket compiler contracts over the isolated P08 retrieval pipeline."""
from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import contextmanager
import json
import sqlite3

import pytest

from scope_recall.contracts import ContractError, TrustedContext, validate_payload
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.events import lexical_terms
from scope_recall.core.recall_diagnostics import RecallDiagnostics
from scope_recall.core.recall_budget import estimate_tokens
from scope_recall.core.recall_policy import meaningful_query_terms
from scope_recall.core.recall_packet import (
    RecallPacketCompiler,
    RecallPacketRenderer,
    canonical_render_json,
    compile_recall_packet,
    packet_item,
    prioritize_current_claims,
    render_recall_packet_context,
)
from scope_recall.core.resume_compaction import compact_episode_variants
from scope_recall.core.retrieval import CandidateRef, RetrievedObject, RetrievalResult, SearchContext
from scope_recall.core.retrieval_storage import RetrievalStorage
from tests.contract.test_v11_claims import accept, capture, draft
from tests.v11_support import context, recall_request


@dataclass
class FixedClock:
    now = "2026-09-06T12:00:00Z"
    _mono: float = 100.0

    def utc_now(self) -> str:
        return self.now

    def monotonic(self) -> float:
        return self._mono


class BrokenReadStorage:
    def __init__(self, inner):
        self.inner = inner

    @property
    def path(self):
        return self.inner.path

    def read(self, context, *, remaining_seconds=None):
        raise ContractError("SQLITE_BUSY", "read")


class StaleOnReleaseStorage(RetrievalStorage):
    """Drop the second hydrated object during release recheck."""

    def __init__(self, *, clock=None, stale_ref: str):
        super().__init__(clock=clock)
        self.stale_ref = stale_ref
        self._seen = 0

    def hydrate(self, tx, candidate, context):
        if candidate.ref == self.stale_ref:
            self._seen += 1
            if self._seen > 1:
                return None
        return super().hydrate(tx, candidate, context)


class EpochFlipStorage(RetrievalStorage):
    def __init__(self, *, clock=None):
        super().__init__(clock=clock)
        self.epoch_calls = 0

    def epoch(self, tx):
        self.epoch_calls += 1
        current = tx.status().memory_epoch
        return current if self.epoch_calls == 1 else current + 1


class FailSecondReleaseStorage(RetrievalStorage):
    """Raise during the second compile release recheck."""

    def __init__(self, *, clock=None):
        super().__init__(clock=clock)
        self._release_checks = 0

    def hydrate(self, tx, candidate, context):
        self._release_checks += 1
        if self._release_checks > 1:
            raise ContractError("SQLITE_BUSY", "hydrate")
        return super().hydrate(tx, candidate, context)


class OversizedApplicabilityStorage(RetrievalStorage):
    """Return hydrated objects whose applicability exceeds packet schema."""

    def hydrate(self, tx, candidate, context):
        fresh = super().hydrate(tx, candidate, context)
        if fresh is None:
            return None
        return RetrievedObject(
            fresh.ref,
            fresh.revision,
            fresh.kind,
            fresh.content,
            fresh.origin,
            fresh.temporal_status,
            "适用 " + ("Y" * 3000),
            fresh.evidence_refs,
            fresh.basis,
            fresh.expandable,
            fresh.source_kinds,
            fresh.relation_refs,
            fresh.metadata,
        )


class DeleteOnReleaseBarrierStorage(RetrievalStorage):
    """Independent deletion between search hydrate and compile release recheck."""

    def __init__(self, *, clock=None, delete_callback=None, target_ref: str):
        super().__init__(clock=clock)
        self.delete_callback = delete_callback
        self.target_ref = target_ref
        self._hydrate_counts: dict[str, int] = {}
        self._delete_pending = False

    @contextmanager
    def read(self, context, *, remaining_seconds=None):
        # Let the underlying connection close before waiting for the
        # independent writer.  This makes the barrier a real SQLite commit,
        # rather than a mocked epoch flip inside one read snapshot.
        with super().read(context, remaining_seconds=remaining_seconds) as tx:
            yield tx
        if self._delete_pending and self.delete_callback is not None:
            self._delete_pending = False
            # This runs only after the underlying read connection has closed,
            # establishing the writer commit barrier before the final fence.
            self.delete_callback()

    def hydrate(self, tx, candidate, context):
        count = self._hydrate_counts.get(candidate.ref, 0) + 1
        self._hydrate_counts[candidate.ref] = count
        if candidate.ref == self.target_ref and count >= 2 and self.delete_callback is not None:
            self._delete_pending = True
        return super().hydrate(tx, candidate, context)


class ReleaseBarrierStorage:
    """Wrap the real SQLite storage and commit the writer before the fence."""

    def __init__(self, inner, reader: DeleteOnReleaseBarrierStorage):
        self.inner = inner
        self.reader = reader

    @property
    def path(self):
        return self.inner.path

    @contextmanager
    def read(self, context, *, remaining_seconds=None):
        with self.inner.read(context, remaining_seconds=remaining_seconds) as tx:
            yield tx
        if self.reader._delete_pending and self.reader.delete_callback is not None:
            self.reader._delete_pending = False
            self.reader.delete_callback()

    def __getattr__(self, name):
        return getattr(self.inner, name)


class ExpireAfterReadStorage:
    """Advance the injected clock after initial hydration, before the fence."""

    def __init__(self, inner, clock):
        self.inner = inner
        self.clock = clock
        self.reads = 0

    @property
    def path(self):
        return self.inner.path

    @contextmanager
    def read(self, context, *, remaining_seconds=None):
        with self.inner.read(context, remaining_seconds=remaining_seconds) as tx:
            yield tx
        self.reads += 1
        if self.reads == 1:
            self.clock._mono += 10.0

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _forget_request(source):
    return dict(
        protocol_version="1.1",
        target_refs=[source.ref],
        mode="delete",
        expected_revisions={source.ref: source.revision},
    )


def _authorize_forget(core, ctx, source):
    return capture(
        core,
        ctx,
        f"删除 {source.ref}",
        key="TEST-p09/forget-auth",
        when="2026-09-06T12:00:00Z",
    )


@pytest.fixture
def app(tmp_path):
    ctx = replace(context(tmp_path / "TEST-p09"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=FixedClock())
    core.initialize()
    return core, ctx


def _packet(core: MemoryCore, ctx: TrustedContext, **changes):
    return core.recall_packet(ctx, recall_request(**changes), deadline_seconds=5)


def test_p09_ok_packet_validates_schema_and_preserves_evidence(app):
    core, ctx = app
    source = capture(core, ctx, "P09 keeps H100 as an exact identifier.", key="TEST-p09/ok")
    packet = _packet(core, ctx, query="H100 exact identifier", mode="current", request_id="TEST-p09-ok")
    assert validate_payload("recall_packet", packet) == packet
    assert packet["status"] in {"ok", "partial"}
    assert packet["items"]
    assert packet["items"][0]["evidence_refs"] == [f"{source.ref}@{source.revision}"]
    assert packet["items"][0]["basis"] in {"direct_report", "observed"}
    assert packet["memory_epoch"] == core.status(ctx).memory_epoch
    assert packet["answerability"] in {"supported", "partial", "ambiguous"}
    prepared = core.prepare_recall_render(ctx, packet)
    assert prepared.canonical_text is not None
    assert estimate_tokens(prepared.canonical_text) <= 1200


def test_recall_fences_never_run_full_queue_diagnostics(app, monkeypatch):
    from scope_recall.core.storage import Transaction

    core, ctx = app
    source = capture(core, ctx, "H100 exact source survives cheap fresh fences.", key="TEST-cheap-epoch")
    epoch = core.status(ctx).memory_epoch

    def forbidden_diagnostics(*args, **kwargs):
        raise AssertionError("recall must not scan the source/work backlog for an epoch")

    monkeypatch.setattr(Transaction, "status", forbidden_diagnostics)
    packet = _packet(core, ctx, query="H100", mode="history")
    assert [item["ref"] for item in packet["items"]] == [source.ref]
    assert packet["memory_epoch"] == core.memory_epoch(ctx) == epoch
    with pytest.raises(ContractError, match="ACCESS_DENIED"):
        core.memory_epoch(replace(ctx, allowed_scope_ids=frozenset()))


def test_oversized_first_hit_does_not_consume_only_slot(app):
    core, ctx = app
    huge = capture(core, ctx, "H100 " + "X" * 12000, key="TEST-first-oversized")
    useful = capture(core, ctx, "H100 first launch was at 09:15.", key="TEST-short-evidence")
    packet = _packet(core, ctx, query="H100", mode="history", max_items=1,
                     budget_tokens=1200, focus_refs=[f"{huge.ref}@{huge.revision}"])
    assert [item["ref"] for item in packet["items"]] == [useful.ref]
    assert estimate_tokens(canonical_render_json(packet)) <= 1200


def test_prior_budget_rejection_diagnostics_do_not_block_later_evidence(app):
    core, ctx = app
    oversized = RetrievedObject(
        ref="event-oversized",
        revision=1,
        kind="event",
        content="lower-value oversized candidate " + ("X" * 7000),
        origin="human_direct",
        temporal_status="current",
        applicability="trusted scope",
        evidence_refs=("event-oversized@1",),
        basis="direct_report",
        expandable=True,
        source_kinds=("event",),
    )
    useful = RetrievedObject(
        ref="event-useful",
        revision=1,
        kind="event",
        content="DECISIVE " + ("Y" * 710),
        origin="human_direct",
        temporal_status="current",
        applicability="trusted scope",
        evidence_refs=("event-useful@1",),
        basis="direct_report",
        expandable=True,
        source_kinds=("event",),
    )
    candidates = (
        CandidateRef("event", oversized.ref, 1, "exact_ref", fusion_score=0.99),
        CandidateRef("event", useful.ref, 1, "lexical", fusion_score=0.98),
    )

    class FixedBudgetStorage(RetrievalStorage):
        objects = {oversized.ref: oversized, useful.ref: useful}

        def hydrate(self, tx, candidate, context):
            return self.objects.get(candidate.ref)

    request_id = "TEST-p09-budget-rejection-isolated"
    search = SearchContext.from_request(
        recall_request(
            query="decisive budget evidence",
            mode="history",
            max_items=1,
            budget_tokens=1200,
            request_id=request_id,
        ),
        ctx,
        now=FixedClock.now,
        deadline=200.0,
    )
    result = RetrievalResult(
        candidates,
        (oversized, useful),
        core.status(ctx).memory_epoch,
        (),
        "unknown",
        "supported",
        2,
        2,
        request_id=request_id,
    )
    packet = compile_recall_packet(
        search,
        result,
        core.storage,
        storage_reader=FixedBudgetStorage(clock=FixedClock()),
        diagnostics=core.recall_diagnostics,
        clock=FixedClock(),
    )

    assert [item["ref"] for item in packet["items"]] == [useful.ref]
    assert estimate_tokens(canonical_render_json(packet)) <= 1200
    assert "budget_token_cap" in packet["gaps"]
    assert "expandable" in packet["unmet_needs"]


def test_vector_timeout_leaves_time_for_all_fresh_release_checks(app):
    core, ctx = app
    source = capture(core, ctx, "H100 lexical evidence remains usable.", key="TEST-slow-vector")

    class ExhaustVectorBudget:
        calls = 0

        def search(self, context, *, limit, remaining_seconds):
            self.calls += 1
            core.clock._mono += remaining_seconds
            raise TimeoutError("optional vector request exhausted its allowance")

    class SlowFreshReader(RetrievalStorage):
        checks = 0

        def hydrate(self, tx, candidate, context):
            self.checks += 1
            core.clock._mono += 0.45
            return super().hydrate(tx, candidate, context)

    vector = ExhaustVectorBudget()
    reader = SlowFreshReader(clock=core.clock)
    core.recall_pipeline.vector_port = vector
    core.recall_pipeline.storage_reader = reader
    began = core.clock.monotonic()
    packet = _packet(core, ctx, query="H100", mode="history", max_items=1)
    assert vector.calls == 1
    assert reader.checks >= 3
    assert [item["ref"] for item in packet["items"]] == [source.ref]
    assert "vector_error:TimeoutError" in packet["gaps"]
    assert core.clock.monotonic() - began < 5.0


def test_p09_authority_unavailable_is_not_masked_as_no_match(app):
    core, ctx = app
    source = capture(core, ctx, "P09 authority failure should not become no_match.", key="TEST-p09/unavailable")
    result = RetrievalResult(
        (CandidateRef("event", source.ref, source.revision, "exact_ref"),),
        (),
        core.status(ctx).memory_epoch,
        ("sqlite_unavailable:SQLITE_BUSY",),
        "unknown",
        "unknown",
        1,
        0,
        request_id="TEST-p09-unavailable",
    )
    search = SearchContext.from_request(
        recall_request(query="authority", request_id="TEST-p09-unavailable"),
        ctx,
        now=FixedClock.now,
        deadline=200.0,
    )
    packet = compile_recall_packet(search, result, BrokenReadStorage(core.storage), clock=FixedClock())
    assert packet["status"] == "unavailable"
    assert packet["items"] == []
    assert packet["answerability"] == "unknown"
    assert any(gap.startswith("sqlite_unavailable") for gap in packet["gaps"])


def test_p09_vector_gap_with_lexical_hit_is_partial_not_unavailable(app):
    core, ctx = app
    capture(core, ctx, "P09 lexical fallback for H100 record.", key="TEST-p09/partial")
    packet = _packet(core, ctx, query="H100 fallback", mode="current", request_id="TEST-p09-partial")
    if "vector_unavailable" in packet["gaps"] or packet["coverage"] == "partial":
        assert packet["status"] in {"ok", "partial"}
        assert packet["status"] != "unavailable"
    else:
        assert packet["status"] in {"ok", "no_match", "partial"}


def test_p09_budget_never_slices_item_content(app):
    core, ctx = app
    negation = "不要删除 H100；若已删除则不得恢复。"
    sources = []
    for index in range(4):
        sources.append(capture(core, ctx, f"{negation} #{index}", key=f"TEST-p09/budget/{index}"))
    packet = _packet(core, ctx, query="H100", mode="current", max_items=2, budget_tokens=512, request_id="TEST-p09-budget")
    assert estimate_tokens(canonical_render_json(packet)) <= 512
    for item in packet["items"]:
        assert "不要删除" in item["content"]
        assert "不得恢复" in item["content"]
    assert len(packet["items"]) <= 2


def test_p09_auto_budget_prefers_active_direct_fact_over_its_raw_instruction(app):
    core, ctx = app
    token = "TEST-ARCHIVE-P09-CERULEAN"
    source = capture(
        core,
        ctx,
        "请记住这条公开合成事实：青岚档案的公开项目代号是 "
        f"{token}。收到后助手只能严格回复 ACK，不得复述、引用或改写该代号。",
        key="TEST-p09/active-fact-budget/source",
    )
    proposal = draft(
        source,
        token,
        kind="fact",
        subject="青岚档案的公开项目代号",
        predicate="是",
        statement_kind="assertion",
    )
    proposal["evidence_spans"][0]["quote"] = f"青岚档案的公开项目代号是 {token}。"
    claim = accept(core, ctx, proposal).items[0]
    assistant = capture(
        core,
        ctx,
        "ACK",
        origin="assistant_visible",
        key="TEST-p09/active-fact-budget/ack",
    )
    candidates = (
        CandidateRef("event", source.ref, 1, "lexical", fusion_score=0.03),
        CandidateRef("event", assistant.ref, 1, "vector", vector_score=0.99, fusion_score=0.016),
        CandidateRef("claim", claim.ref, 1, "relation", fusion_score=0.016),
    )
    search = SearchContext.from_request(
        recall_request(
            query="请回答青岚档案的公开项目代号。",
            mode="auto",
            budget_tokens=600,
            request_id="TEST-p09-active-fact-budget",
        ),
        ctx,
        now=FixedClock.now,
        deadline=200.0,
    )
    reader = RetrievalStorage(clock=FixedClock())
    with core.storage.read(ctx, remaining_seconds=5) as tx:
        objects = tuple(reader.hydrate(tx, candidate, search) for candidate in candidates)
        epoch = reader.epoch(tx)
    assert all(obj is not None for obj in objects), objects
    result = RetrievalResult(
        candidates,
        objects,
        epoch,
        (),
        "unknown",
        "supported",
        3,
        3,
        request_id=search.request_id,
    )

    packet = compile_recall_packet(search, result, core.storage, clock=FixedClock())

    assert packet["items"][0]["ref"] == claim.ref
    assert packet["items"][0]["kind"] == "claim"
    assert token in packet["items"][0]["content"]
    assert packet["items"][0]["evidence_refs"] == [f"{source.ref}@1"]
    assert all(item["ref"] not in {source.ref, assistant.ref} for item in packet["items"])
    assert packet["status"] == "partial"
    assert "budget_token_cap" in packet["gaps"]


def test_p09_exact_ref_keeps_explicit_event_ahead_of_related_active_claim(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-P09 explicit 的型号是 H100。", key="TEST-p09/exact-order/source")
    claim = accept(
        core,
        ctx,
        draft(
            source,
            "H100",
            kind="fact",
            subject="TEST-P09 explicit",
            predicate="型号",
            statement_kind="assertion",
        ),
    ).items[0]
    candidates = (
        CandidateRef("event", source.ref, 1, "exact_ref", fusion_score=0.01),
        CandidateRef("claim", claim.ref, 1, "relation", fusion_score=0.02),
    )
    search = SearchContext.from_request(
        recall_request(
            query=f"inspect H100 {source.ref}@1",
            mode="current",
            focus_refs=[f"{source.ref}@1"],
            budget_tokens=1200,
            request_id="TEST-p09-exact-order",
        ),
        ctx,
        now=FixedClock.now,
        deadline=200.0,
    )
    reader = RetrievalStorage(clock=FixedClock())
    with core.storage.read(ctx, remaining_seconds=5) as tx:
        objects = tuple(reader.hydrate(tx, candidate, search) for candidate in candidates)
        epoch = reader.epoch(tx)
    assert all(obj is not None for obj in objects), objects
    result = RetrievalResult(
        candidates,
        objects,
        epoch,
        (),
        "unknown",
        "supported",
        2,
        2,
        request_id=search.request_id,
    )

    packet = compile_recall_packet(search, result, core.storage, clock=FixedClock())

    assert packet["items"][0]["ref"] == source.ref


def test_p09_current_claim_priority_preserves_resume_and_non_event_barriers(app):
    core, ctx = app
    evidence_ref = "event-priority@1"
    event = RetrievedObject(
        "event-priority",
        1,
        "event",
        "TEST-P09 raw H100 event",
        "human_direct",
        "current",
        "trusted scope",
        (evidence_ref,),
        "direct_report",
        True,
        ("event",),
    )
    claim = RetrievedObject(
        "claim-priority",
        1,
        "claim",
        '{"kind":"fact","value_text":"H100"}',
        "human_direct",
        "current",
        "trusted scope",
        (evidence_ref,),
        "direct_report",
        True,
        ("claim",),
        metadata=(("state", "active"),),
    )
    episode = RetrievedObject(
        "episode-priority",
        1,
        "episode",
        '{"goal":{"text":"继续 H100","evidence_refs":["event-priority@1"]}}',
        "derived_summary",
        "current",
        "trusted scope",
        (evidence_ref,),
        "derived_summary",
        True,
        ("episode",),
    )
    artifact = RetrievedObject(
        "artifact-priority",
        1,
        "artifact",
        "TEST-P09 H100 artifact",
        "observed",
        "current",
        "trusted scope",
        (evidence_ref,),
        "observed",
        True,
        ("artifact",),
    )
    event_candidate = CandidateRef("event", event.ref, 1, "lexical")
    claim_candidate = CandidateRef("claim", claim.ref, 1, "relation")
    episode_candidate = CandidateRef("episode", episode.ref, 1, "vector")
    artifact_candidate = CandidateRef("artifact", artifact.ref, 1, "vector")

    resume_search = SearchContext.from_request(
        recall_request(query="继续 H100", mode="auto", request_id="TEST-p09-resume-order"),
        ctx,
        now=FixedClock.now,
        deadline=200.0,
    )
    resume_order = [(episode_candidate, episode), (event_candidate, event), (claim_candidate, claim)]
    assert prioritize_current_claims(resume_search, resume_order) == resume_order

    current_search = SearchContext.from_request(
        recall_request(query="H100", mode="auto", request_id="TEST-p09-artifact-order"),
        ctx,
        now=FixedClock.now,
        deadline=200.0,
    )
    barrier_order = [(event_candidate, event), (artifact_candidate, artifact), (claim_candidate, claim)]
    assert prioritize_current_claims(current_search, barrier_order) == barrier_order


def test_p09_oversized_single_item_reports_expandable_gap_without_text(app):
    core, ctx = app
    huge = "H100 " + "X" * 6000
    source = capture(core, ctx, huge, key="TEST-p09/huge")
    result = core.recall(
        ctx,
        recall_request(query="H100", mode="current", max_items=1, budget_tokens=8000, request_id="TEST-p09-huge"),
        deadline_seconds=5,
    )
    search = SearchContext.from_request(
        recall_request(query="H100", mode="current", max_items=1, budget_tokens=8000, request_id="TEST-p09-huge"),
        ctx,
        now=FixedClock.now,
        deadline=200.0,
    )
    packet = compile_recall_packet(search, result, core.storage, clock=FixedClock())
    assert packet["status"] in {"partial", "no_match"}
    assert not packet["items"] or packet["items"][0]["content"] == huge
    if not packet["items"]:
        assert any("budget_oversized" in gap for gap in packet["gaps"])
        assert any(source.ref in need for need in packet["unmet_needs"])


def test_p09_release_recheck_drops_stale_revision_conservatively(tmp_path):
    ctx = replace(context(tmp_path / "TEST-p09-stale"), project_id="TEST-project", branch_id="TEST-main")
    clock = FixedClock()
    core = MemoryCore(CoreConfig(ctx.binding), clock=clock)
    core.initialize()
    first = capture(core, ctx, "P09 first revision.", key="TEST-p09/stale", revision=1)
    second = capture(core, ctx, "P09 second revision.", key="TEST-p09/stale", revision=2)
    core.recall_pipeline.storage_reader = StaleOnReleaseStorage(clock=clock, stale_ref=second.ref)
    packet = core.recall_packet(
        ctx,
        recall_request(query="P09 second revision", mode="history", request_id="TEST-p09-stale"),
        deadline_seconds=5,
    )
    assert packet["status"] in {"partial", "no_match", "ok"}
    refs = {(item["ref"], item["revision"]) for item in packet["items"]}
    assert (second.ref, second.revision) not in refs or "stale_candidate" in "|".join(packet["gaps"])


class WithdrawnSinceStorage(EpochFlipStorage):
    """The epoch moves between the release reads, and a deletion in the recall's scopes came with it."""

    def retracted_since(self, tx, context, since):
        return True


def _epoch_fenced_packet(core, ctx, reader):
    # The query has to find something, or an emptied packet and an empty one look the same.
    capture(core, ctx, "P09 keeps H100 as an exact identifier.", key="TEST-p09/epoch-fence")
    query = "H100 exact identifier"
    result = core.recall(ctx, recall_request(query=query, mode="current", request_id="TEST-p09-epoch-result"), deadline_seconds=5)
    assert result.items, "the fence has nothing to act on"
    search = SearchContext.from_request(
        recall_request(query=query, mode="current", request_id="TEST-p09-epoch-compile"),
        ctx,
        now=FixedClock.now,
        deadline=200.0,
    )
    return compile_recall_packet(search, result, core.storage, storage_reader=reader, clock=FixedClock())


def test_p09_an_epoch_move_that_withdrew_nothing_keeps_the_verified_items(app):
    """Every capture moves the epoch.  With several entries writing to one store a recall rarely
    finished without a move, and each emptied the packet: on the pilot an agent asked about what the
    owner had just told another one while the worker was writing it down, and got nothing."""
    core, ctx = app
    packet = _epoch_fenced_packet(core, ctx, EpochFlipStorage(clock=FixedClock()))
    assert packet["items"], packet["gaps"]
    assert not any(gap.startswith("epoch_changed") for gap in packet["gaps"]), packet["gaps"]


def test_p09_an_epoch_move_that_withdrew_something_in_scope_still_empties_the_release(app):
    core, ctx = app
    packet = _epoch_fenced_packet(core, ctx, WithdrawnSinceStorage(clock=FixedClock()))
    assert not packet["items"]
    assert "epoch_changed_release" in packet["gaps"]
    assert packet["status"] in {"partial", "no_match"}


def test_p09_auto_compiler_caps_delivery_to_six_items(app):
    core, ctx = app
    for index in range(8):
        capture(core, ctx, f"P09 H100 delivery record {index}.", key=f"TEST-p09/auto-budget/{index}")
    result = core.recall(ctx, recall_request(query="H100 delivery record", mode="history", max_items=30), deadline_seconds=5)
    search = SearchContext.from_request(
        recall_request(query="H100 delivery record", mode="auto", max_items=30, budget_tokens=8000, request_id="TEST-p09-auto-budget"),
        ctx,
        now=FixedClock.now,
        deadline=200.0,
    )
    packet = compile_recall_packet(search, result, core.storage, storage_reader=core.recall_pipeline.storage_reader, clock=FixedClock())
    assert len(packet["items"]) <= 6


def test_p09_duplicate_same_request_injects_once_per_session(app):
    core, ctx = app
    capture(core, ctx, "P09 duplicate request should inject once.", key="TEST-p09/dedupe")
    request = recall_request(query="duplicate request", mode="current", request_id="TEST-p09-dedupe")
    first = core.recall_packet(ctx, request, deadline_seconds=5)
    second = core.recall_packet(ctx, request, deadline_seconds=5)
    assert first["items"] == second["items"]
    assert first["diagnostic_ref"] != second["diagnostic_ref"]
    assert first["diagnostic_ref"] is not None


def test_p09_different_sessions_do_not_share_dedupe_state(app):
    core, ctx = app
    capture(core, ctx, "P09 session isolation.", key="TEST-p09/session")
    request = recall_request(query="session isolation", mode="current", request_id="TEST-p09-session")
    first = core.recall_packet(ctx, request, deadline_seconds=5)
    other = core.recall_packet(replace(ctx, session_id="TEST-other-session"), request, deadline_seconds=5)
    assert first["request_id"] == other["request_id"]
    assert first["diagnostic_ref"] != other["diagnostic_ref"]


def test_p09_diagnostics_channel_is_sanitized_and_bounded(app):
    core, ctx = app
    capture(core, ctx, "P09 diagnostics must not echo raw user content.", key="TEST-p09/diag")
    packet = _packet(core, ctx, query="diagnostics must not echo raw user content", request_id="TEST-p09-diag")
    assert packet["diagnostic_ref"]
    record = core.recall_diagnostics.get(packet["diagnostic_ref"])
    assert record is not None
    public = record.to_public()
    blob = str(public)
    assert "diagnostics must not echo raw user content" not in blob
    assert public["items_delivered"] == len(packet["items"])


def test_p09_compiler_does_not_search_or_write_authority(app):
    core, ctx = app
    capture(core, ctx, "P09 read-only compile.", key="TEST-p09/readonly")
    before = core.storage.path.read_bytes()
    epoch = core.status(ctx).memory_epoch
    packet = _packet(core, ctx, query="read-only compile", mode="current", request_id="TEST-p09-readonly")
    after = core.status(ctx).memory_epoch
    assert core.storage.path.read_bytes() == before
    assert after == epoch
    assert packet["status"] in {"ok", "no_match", "partial"}


def test_p09_journey_implicit_preference_without_explicit_recall_word(app):
    """P09/P13: preference journey uses auto recall without explicit remember wording."""
    core, ctx = app
    capture(core, ctx, "仅 TEST 项目使用白色 UI。", key="TEST-p09/pref")
    packet = _packet(core, ctx, query="继续 TEST 项目界面设计", mode="auto", request_id="TEST-p09-pref")
    if packet["items"]:
        assert any("白色" in item["content"] for item in packet["items"])
    assert packet["status"] in {"ok", "partial", "no_match"}


def test_p09_journey_correction_does_not_resurrect_superseded_value(app):
    """P09/P13: corrected value should dominate older preference in current mode."""
    core, ctx = app
    old = capture(core, ctx, "TEST 项目配色蓝色。", key="TEST-p09/correct", revision=1, when="2026-09-01T12:00:00Z")
    capture(core, ctx, "TEST 项目配色银色。", key="TEST-p09/correct", revision=2, when="2026-09-05T12:00:00Z")
    packet = _packet(core, ctx, query="TEST 项目配色", mode="current", request_id="TEST-p09-correct")
    if packet["items"]:
        contents = " ".join(item["content"] for item in packet["items"])
        assert "银色" in contents or old.ref not in {item["ref"] for item in packet["items"]}


def test_p09_journey_unrelated_topic_stays_isolated(app):
    """P09/P13: unrelated query should not inject the preference memory."""
    core, ctx = app
    capture(core, ctx, "仅 TEST 项目使用白色 UI。", key="TEST-p09/unrelated")
    packet = _packet(core, ctx, query="量子生物学最新论文", mode="auto", request_id="TEST-p09-unrelated")
    assert packet["status"] in {"no_match", "partial"}
    assert not any("白色" in item["content"] for item in packet["items"])


def test_p09_cache_hit_invalid_after_source_delete_recompiles_conservatively(app):
    core, ctx = app
    source = capture(core, ctx, "P09 cache fence delete H100 record.", key="TEST-p09/cache-delete")
    request = recall_request(query="cache fence delete H100", mode="current", request_id="TEST-p09-cache-delete")
    first = core.recall_packet(ctx, request, deadline_seconds=5)
    assert first["items"]
    _authorize_forget(core, ctx, source)
    core.forget(ctx, _forget_request(source), remaining_seconds=10)
    second = core.recall_packet(ctx, request, deadline_seconds=5)
    assert not any(item["ref"] == source.ref for item in second["items"])
    assert second["status"] in {"partial", "no_match"}


def test_p09_cache_hit_return_is_isolated_from_caller_mutation(app):
    core, ctx = app
    capture(core, ctx, "P09 cache mutation isolation.", key="TEST-p09/cache-mutate")
    request = recall_request(query="cache mutation isolation", mode="current", request_id="TEST-p09-cache-mutate")
    first = core.recall_packet(ctx, request, deadline_seconds=5)
    assert first["items"]
    first["items"][0]["content"] = "mutated by caller"
    first["items"][0]["evidence_refs"].clear()
    second = core.recall_packet(ctx, request, deadline_seconds=5)
    assert second["items"][0]["content"] != "mutated by caller"
    assert second["items"][0]["evidence_refs"]
    assert "mutation isolation" in second["items"][0]["content"]


def test_p09_sql_error_after_partial_verify_clears_release_candidates(app):
    core, ctx = app
    capture(core, ctx, "P09 SQL error first H100 record.", key="TEST-p09/sql-first")
    capture(core, ctx, "P09 SQL error second H200 record.", key="TEST-p09/sql-second")
    result = core.recall(
        ctx,
        recall_request(query="SQL error H100 H200", mode="history", request_id="TEST-p09-sql-error"),
        deadline_seconds=5,
    )
    search = SearchContext.from_request(
        recall_request(query="SQL error H100 H200", request_id="TEST-p09-sql-error"),
        ctx,
        now=FixedClock.now,
        deadline=200.0,
    )
    reader = FailSecondReleaseStorage(clock=FixedClock())
    packet = compile_recall_packet(search, result, core.storage, storage_reader=reader, clock=FixedClock())
    assert packet["items"] == []
    assert packet["status"] in {"partial", "unavailable", "no_match"}
    assert any(gap.startswith("sqlite_unavailable") for gap in packet["gaps"])


def test_p09_deletion_barrier_race_drops_stale_text_before_release(tmp_path):
    ctx = replace(context(tmp_path / "TEST-p09-barrier"), project_id="TEST-project", branch_id="TEST-main")
    clock = FixedClock()
    core = MemoryCore(CoreConfig(ctx.binding), clock=clock)
    core.initialize()
    source = capture(core, ctx, "P09 barrier race H100 text.", key="TEST-p09/barrier")
    writer = MemoryCore(CoreConfig(ctx.binding), clock=clock)
    writer.initialize()
    _authorize_forget(writer, ctx, source)

    def delete_source():
        # Use a second Core/SQLite connection so the writer commit is
        # independent of the compiler's read snapshot.
        conn = sqlite3.connect(str(writer.storage.path), timeout=10)
        try:
            conn.execute(
                "UPDATE source_events SET read_blocked=1, suppressed=1 WHERE event_id=? AND source_revision=?",
                (source.ref, source.revision),
            )
            conn.execute("UPDATE instance_meta SET memory_epoch=memory_epoch+1 WHERE singleton=1")
            conn.commit()
        finally:
            conn.close()

    core.recall_pipeline.storage_reader = DeleteOnReleaseBarrierStorage(
        clock=clock,
        delete_callback=delete_source,
        target_ref=source.ref,
    )
    core.storage = ReleaseBarrierStorage(core.storage, core.recall_pipeline.storage_reader)
    packet = core.recall_packet(
        ctx,
        recall_request(query="barrier race H100", mode="current", request_id="TEST-p09-barrier"),
        deadline_seconds=5,
    )
    assert not any("barrier race H100" in item["content"] for item in packet["items"])
    assert packet["status"] in {"partial", "no_match"}
    assert any("stale_candidate" in gap or "epoch_changed" in gap for gap in packet["gaps"])


def test_p09_deadline_expiry_does_not_reuse_cached_ok_packet(app):
    core, ctx = app
    clock = core.clock
    capture(core, ctx, "P09 deadline cache H100 record.", key="TEST-p09/deadline-cache")
    request = recall_request(query="deadline cache H100", mode="current", request_id="TEST-p09-deadline-cache")
    search = SearchContext.from_request(request, ctx, now=clock.now, deadline=clock.monotonic() + 0.5)
    result = core.recall_pipeline.search(search)
    first = compile_recall_packet(search, result, core.storage, storage_reader=core.recall_pipeline.storage_reader, clock=clock)
    assert first["items"]
    clock._mono = search.deadline + 1.0
    second = compile_recall_packet(search, result, core.storage, storage_reader=core.recall_pipeline.storage_reader, clock=clock)
    assert second["status"] == "unavailable"
    assert any("deadline_exceeded" in gap for gap in second["gaps"])
    assert second["items"] == []


def test_p09_deadline_expiry_between_hydrate_and_fence_drops_verified(app):
    core, ctx = app
    clock = core.clock
    capture(core, ctx, "P09 fence clock H100 record.", key="TEST-p09/fence-clock")
    request = recall_request(query="fence clock H100", mode="current", request_id="TEST-p09-fence-clock")
    search = SearchContext.from_request(request, ctx, now=clock.now, deadline=clock.monotonic() + 5.0)
    result = core.recall_pipeline.search(search)
    expiring_storage = ExpireAfterReadStorage(core.storage, clock)
    packet = compile_recall_packet(
        search,
        result,
        expiring_storage,
        storage_reader=core.recall_pipeline.storage_reader,
        clock=clock,
    )
    assert packet["items"] == []
    assert "deadline_exceeded_release_fence" in packet["gaps"]
    assert packet["status"] == "unavailable"


def test_p09_deadline_without_safe_item_is_unavailable_not_no_match(app):
    """An unfinished read is operationally unavailable, not semantic no-match."""
    core, ctx = app
    clock = core.clock
    request = recall_request(query="deadline status distinction", mode="current", request_id="TEST-p09-deadline-status")
    search = SearchContext.from_request(
        request,
        ctx,
        now=clock.now,
        deadline=clock.monotonic() + 0.5,
    )
    clock._mono = search.deadline
    result = RetrievalResult(
        (),
        (),
        core.status(ctx).memory_epoch,
        ("deadline_exceeded_hydrate",),
        "partial",
        "unknown",
        1,
        0,
        request_id=request["request_id"],
    )
    packet = compile_recall_packet(search, result, core.storage, clock=clock)
    assert packet["items"] == []
    assert packet["status"] == "unavailable"
    assert packet["answerability"] == "unknown"
    assert any(gap.startswith("deadline_exceeded") for gap in packet["gaps"])


def test_p09_diagnostics_deque_and_index_evict_together():
    diagnostics = RecallDiagnostics()
    refs: list[str] = []
    for index in range(70):
        refs.append(
            diagnostics.record(
                installation_id="TEST-installation",
                session_id=f"TEST-session-{index}",
                request_id=f"TEST-diag-{index}",
                memory_epoch=1,
                phase="compile",
                status="ok",
                retrieval_gaps=(),
                compile_gaps=(),
                items_delivered=1,
                items_dropped_stale=0,
                items_dropped_budget=0,
                elapsed_ms=1,
                deadline_remaining_ms=1000,
            )
        )
    assert diagnostics.record_count == 64
    assert diagnostics.index_count == 64
    for ref in refs[:6]:
        assert diagnostics.get(ref) is None
    for ref in refs[-3:]:
        assert diagnostics.get(ref) is not None


def test_p09_renderer_state_is_bounded_across_requests():
    renderer = RecallPacketRenderer()
    packet = dict(
        protocol_version="1.1",
        request_id="TEST-request",
        status="ok",
        memory_epoch=1,
        gaps=[],
        diagnostic_ref=None,
        answerability="supported",
        coverage="partial",
        unmet_needs=[],
        items=[{
            "ref": "event-test",
            "revision": 1,
            "kind": "event",
            "content": "bounded renderer data",
            "temporal_status": "current",
            "origin": "human_direct",
            "applicability": "trusted scope",
            "evidence_refs": ["event-test@1"],
            "expandable": True,
            "basis": "direct_report",
        }],
    )
    for index in range(70):
        render_recall_packet_context(
            packet | {"request_id": f"TEST-render-{index}"},
            installation_id="TEST-installation",
            session_id=f"TEST-session-{index}",
            renderer=renderer,
        )
    assert len(renderer._prepared) == 64


class MixedOversizedStorage(RetrievalStorage):
    """Keep one complete item while rejected candidates add bounded gaps."""

    def __init__(self, *, clock=None, keep_ref: str):
        super().__init__(clock=clock)
        self.keep_ref = keep_ref

    def hydrate(self, tx, candidate, context):
        fresh = super().hydrate(tx, candidate, context)
        if fresh is None or fresh.ref == self.keep_ref:
            return fresh
        return RetrievedObject(
            fresh.ref,
            fresh.revision,
            fresh.kind,
            fresh.content,
            fresh.origin,
            fresh.temporal_status,
            "适用 " + ("Y" * 3000),
            fresh.evidence_refs,
            fresh.basis,
            fresh.expandable,
            fresh.source_kinds,
            fresh.relation_refs,
            fresh.metadata,
        )
def test_p09_budget_counts_rendered_metadata_and_drops_whole_unit(app):
    core, ctx = app
    capture(core, ctx, "P09 metadata budget H100 record.", key="TEST-p09/metadata-budget")
    result = core.recall(
        ctx,
        recall_request(query="metadata budget H100", mode="current", request_id="TEST-p09-metadata-budget"),
        deadline_seconds=5,
    )
    search = SearchContext.from_request(
        recall_request(
            query="metadata budget H100",
            mode="current",
            max_items=1,
            budget_tokens=512,
            request_id="TEST-p09-metadata-budget",
        ),
        ctx,
        now=FixedClock.now,
        deadline=200.0,
    )
    reader = OversizedApplicabilityStorage(clock=FixedClock())
    packet = compile_recall_packet(search, result, core.storage, storage_reader=reader, clock=FixedClock())
    assert not packet["items"]
    assert any("budget_oversized" in gap for gap in packet["gaps"])


def test_p09_final_packet_budget_includes_rejection_gaps_and_render_text(app):
    core, ctx = app
    sources = [
        capture(core, ctx, f"P09 CJK budget H100 item {index}。", key=f"TEST-p09/mixed-budget/{index}")
        for index in range(5)
    ]
    result = core.recall(
        ctx,
        recall_request(query="H100", mode="history", max_items=6, budget_tokens=8000, request_id="TEST-p09-mixed-budget"),
        deadline_seconds=5,
    )
    search = SearchContext.from_request(
        recall_request(query="H100", mode="history", max_items=6, budget_tokens=1200, request_id="TEST-p09-mixed-budget"),
        ctx,
        now=FixedClock.now,
        deadline=200.0,
    )
    reader = MixedOversizedStorage(clock=FixedClock(), keep_ref=sources[0].ref)
    packet = compile_recall_packet(search, result, core.storage, storage_reader=reader, clock=FixedClock())
    prepared = core.prepare_recall_render(ctx, packet)
    if prepared.canonical_text is not None:
        assert estimate_tokens(prepared.canonical_text) <= 1200
    assert packet["status"] in {"ok", "partial", "no_match"}


def test_p09_resume_correction_selects_complete_json_under_auto_budget(app):
    """A current correction remains deliverable when full resume bookkeeping is too large."""
    core, ctx = app
    correction = "TEST-P09-JOURNEYS/correction-run/summary.md"
    resume = {
        "episode_ref": "episode-correction",
        "state": "open",
        "goal": {"text": "选择摘要位置", "evidence_refs": ["event-goal@1"]},
        "decisions": [{"text": correction, "evidence_refs": ["event-correction@1"]}],
        "verified_progress": [],
        "open_items": [{"text": "继续整理摘要", "evidence_refs": ["event-next@1"]}],
        "blockers": [],
        "next_step": "继续整理摘要",
        "next_step_basis": "user_requested",
        "artifact_refs": [],
        "source_watermark": "watermark-correction",
        "evidence_refs": ["event-goal@1", "event-correction@1", "event-next@1"],
        # Older bookkeeping is intentionally complete but not needed for this
        # query; field selection may omit it without slicing any JSON value.
        "history": [{"text": "旧摘要位置" + ("-history" * 180), "evidence_refs": ["event-old@1"]}],
    }
    original = json.dumps(resume, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    obj = RetrievedObject(
        "episode-correction", 6, "episode", original, "derived_summary", "current", "trusted",
        tuple(resume["evidence_refs"]), "derived_summary", True, ("episode",),
        metadata=(
            ("source_order", json.dumps([
                ["event-goal", 1, 1], ["event-correction", 1, 2], ["event-next", 1, 3],
            ], separators=(",", ":"))),
            ("source_texts", json.dumps({
                "event-goal@1": "选择摘要位置",
                "event-correction@1": correction,
                "event-next@1": "继续执行修正路径",
            }, ensure_ascii=False, separators=(",", ":"))),
        ),
    )

    class FixedEpisodeStorage(RetrievalStorage):
        def hydrate(self, tx, candidate, context):
            return obj

    result = RetrievalResult(
        (CandidateRef("episode", obj.ref, obj.revision, "lexical"),),
        (obj,),
        core.status(ctx).memory_epoch,
        ("vector_unavailable",),
        "partial",
        "partial",
        1,
        1,
        request_id="TEST-p09-resume-correction",
        unmet_needs=("resume_state",),
    )
    request = recall_request(
        query="继续做摘要，采用哪个位置",
        mode="auto",
        max_items=6,
        budget_tokens=1200,
        request_id="TEST-p09-resume-correction",
    )
    search = SearchContext.from_request(request, ctx, now=FixedClock.now, deadline=200.0)
    packet = compile_recall_packet(
        search,
        result,
        core.storage,
        storage_reader=FixedEpisodeStorage(clock=FixedClock()),
        clock=FixedClock(),
    )
    assert packet["items"]
    selected = json.loads(packet["items"][0]["content"])
    assert selected["decisions"][0]["text"] == correction
    assert selected["decisions"][0]["evidence_refs"] == ["event-correction@1"]
    assert packet["items"][0]["content"] != original
    prepared = core.prepare_recall_render(ctx, packet)
    assert prepared.canonical_text is not None
    assert estimate_tokens(prepared.canonical_text) <= 1200


def test_p09_resume_compact_uses_source_sequence_and_keeps_cross_field_evidence(app):
    """Model list order cannot replace trusted event sequence for correction selection."""
    core, ctx = app
    resume = {
        "episode_ref": "episode-reversed",
        "state": "open",
        "goal": {"text": "旧目标", "evidence_refs": ["event-goal@1"]},
        # Newer correction is deliberately first; the older entry is last.
        "decisions": [
            {"text": "采用最新修正路径", "evidence_refs": ["event-new@1"]},
            {"text": "采用已过时路径", "evidence_refs": ["event-old@1"]},
        ],
        "verified_progress": [
            {"text": "已核验修正依赖", "evidence_refs": ["event-progress@1"]},
        ],
        "open_items": [],
        "next_step": "继续执行修正路径",
        "next_step_basis": "user_requested",
        "evidence_refs": ["event-old@1", "event-new@1", "event-progress@1", "event-next@1"],
        "history": [{"text": "旧记录" + ("-history" * 220), "evidence_refs": ["event-old@1"]}],
    }
    original = json.dumps(resume, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    obj = RetrievedObject(
        "episode-reversed", 4, "episode", original, "derived_summary", "current", "trusted",
        tuple(resume["evidence_refs"]), "derived_summary", True, ("episode",),
        metadata=(
            ("source_order", json.dumps([
                ["event-old", 1, 2], ["event-progress", 1, 9],
                ["event-new", 1, 10], ["event-next", 1, 11],
            ], separators=(",", ":"))),
            ("source_texts", json.dumps({
                "event-old@1": "采用已过时路径",
                "event-progress@1": "已核验修正依赖",
                "event-new@1": "采用最新修正路径",
                "event-next@1": "继续执行修正路径",
            }, ensure_ascii=False, separators=(",", ":"))),
        ),
    )

    class FixedEpisodeStorage(RetrievalStorage):
        def hydrate(self, tx, candidate, context):
            return obj

    result = RetrievalResult(
        (CandidateRef("episode", obj.ref, obj.revision, "lexical"),),
        (obj,),
        core.status(ctx).memory_epoch,
        ("vector_unavailable",),
        "partial", "partial", 1, 1,
        request_id="TEST-p09-reversed-decisions",
        unmet_needs=("resume_state",),
    )
    request = recall_request(
        query="继续执行修正路径，当前进度是什么",
        mode="auto", max_items=6, budget_tokens=1200,
        request_id="TEST-p09-reversed-decisions",
    )
    search = SearchContext.from_request(request, ctx, now=FixedClock.now, deadline=200.0)
    packet = compile_recall_packet(
        search, result, core.storage,
        storage_reader=FixedEpisodeStorage(clock=FixedClock()), clock=FixedClock(),
    )
    assert packet["items"]
    selected = json.loads(packet["items"][0]["content"])
    assert selected["decisions"][0]["text"] == "采用最新修正路径"
    assert selected["verified_progress"][0]["evidence_refs"] == ["event-progress@1"]
    assert selected["next_step_evidence_refs"] == ["event-next@1"]
    assert set(packet["items"][0]["evidence_refs"]) == {
        "event-new@1", "event-progress@1", "event-next@1",
    }
    prepared = core.prepare_recall_render(ctx, packet)
    assert prepared.canonical_text is not None
    assert estimate_tokens(prepared.canonical_text) <= 1200


def test_p09_resume_uses_late_trusted_order_and_text_for_next_step_provenance(app):
    """A late correction past the relation window remains latest, while an older source may support next_step."""
    core, ctx = app
    retained = [f"event-{index:02d}@1" for index in range(1, 31)]
    retained[1] = "event-next-old@1"
    retained[-1] = "event-late-correction@1"
    source_order = [
        [ref.split("@", 1)[0], 1, index]
        for index, ref in enumerate(retained, 1)
    ]
    source_texts = {
        "event-next-old@1": "人类记录：继续执行晚修正，并等待确认。",
        "event-late-correction@1": "人类记录：晚更正已完成，但未提出下一步。",
    }
    resume = {
        "episode_ref": "episode-late-correction",
        "state": "open",
        "decisions": [{
            "text": "采用第30条晚更正",
            "evidence_refs": ["event-late-correction@1"],
        }],
        "verified_progress": [],
        "open_items": [],
        "next_step": "继续执行晚修正",
        "next_step_basis": "user_requested",
        "evidence_refs": retained,
    }
    original = json.dumps(resume, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    obj = RetrievedObject(
        "episode-late-correction", 3, "episode", original, "derived_summary", "current", "trusted",
        tuple(retained), "derived_summary", True, ("episode",),
        metadata=(
            ("source_order", json.dumps(source_order, ensure_ascii=False, separators=(",", ":"))),
            ("source_texts", json.dumps(source_texts, ensure_ascii=False, separators=(",", ":"))),
        ),
    )

    class FixedEpisodeStorage(RetrievalStorage):
        def hydrate(self, tx, candidate, context):
            return obj

    result = RetrievalResult(
        (CandidateRef("episode", obj.ref, obj.revision, "lexical"),),
        (obj,),
        core.status(ctx).memory_epoch,
        ("vector_unavailable",),
        "partial", "partial", 1, 1,
        request_id="TEST-p09-late-resume",
        unmet_needs=("resume_state",),
    )
    request = recall_request(
        query="继续执行晚修正，当前进度是什么",
        mode="auto", max_items=6, budget_tokens=600,
        request_id="TEST-p09-late-resume",
    )
    search = SearchContext.from_request(request, ctx, now=FixedClock.now, deadline=200.0)
    packet = compile_recall_packet(
        search, result, core.storage,
        storage_reader=FixedEpisodeStorage(clock=FixedClock()), clock=FixedClock(),
    )
    assert packet["items"]
    selected = json.loads(packet["items"][0]["content"])
    assert selected["decisions"][0]["text"] == "采用第30条晚更正"
    assert selected["decisions"][0]["evidence_refs"] == ["event-late-correction@1"]
    assert selected["next_step"] == "继续执行晚修正"
    assert selected["next_step_evidence_refs"] == ["event-next-old@1"]
    assert "event-late-correction@1" not in selected["next_step_evidence_refs"]


def test_p09_retrieval_hydrates_retained_late_event_after_relation_window(app):
    """Hydration orders retained refs precisely even after 24 unrelated events."""
    core, ctx = app
    sources = [
        capture(
            core,
            ctx,
            f"第 {index} 条历史事件。",
            key=f"TEST-p09/late-order/{index}",
            when="2026-09-01T12:00:00Z",
        )
        for index in range(1, 31)
    ]
    old_ref = f"{sources[0].ref}@{sources[0].revision}"
    late_ref = f"{sources[-1].ref}@{sources[-1].revision}"
    episode_ref = None
    resume = {
        "episode_ref": episode_ref,
        "state": "open",
        "decisions": [{"text": "第30条晚更正", "evidence_refs": [late_ref]}],
        "verified_progress": [],
        "open_items": [],
        "next_step": "继续检查晚更正",
        "next_step_basis": "user_requested",
        "evidence_refs": [old_ref, late_ref],
    }
    assert len({source.ref for source in sources}) == 30
    with sqlite3.connect(core.storage.path) as conn:
        conn.row_factory = sqlite3.Row
        episode_rows = conn.execute(
            """SELECT e.episode_id,e.current_revision,COUNT(ee.sequence) AS event_count
               FROM episodes e JOIN episode_events ee ON ee.episode_id=e.episode_id
               WHERE e.episode_id=(SELECT episode_id FROM episode_events WHERE source_ref=? LIMIT 1)
                 AND e.scope_id=? AND e.project_id=? AND e.branch_id=?
               GROUP BY e.episode_id,e.current_revision
               HAVING event_count>=30 ORDER BY e.episode_id""",
            (sources[-1].ref, "TEST-scope", "TEST-project", "TEST-main"),
        ).fetchall()
        assert episode_rows
        episode_ref = episode_rows[0]["episode_id"]
        episode_revision = episode_rows[0]["current_revision"]
        conn.execute(
            """UPDATE episode_versions
               SET state=?,resume_json=?,processed_sequence=?
               WHERE episode_id=? AND revision=?""",
            (
                "open", json.dumps(resume, ensure_ascii=False),
                conn.execute("SELECT MAX(sequence) FROM episode_events WHERE episode_id=?", (episode_ref,)).fetchone()[0],
                episode_ref, episode_revision,
            ),
        )
        for source in (sources[0], sources[-1]):
            conn.execute(
                """INSERT OR IGNORE INTO evidence_links(
                    object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote
                ) VALUES ('episode',?,?,?,?,?,?)""",
                (episode_ref, episode_revision, source.ref, source.revision, "derived_from", source.event["content"]),
            )
        assert conn.execute(
            "SELECT count(*) FROM evidence_links WHERE object_kind='episode' AND object_ref=? AND object_revision=?",
            (episode_ref, episode_revision),
        ).fetchone()[0] >= 2
        conn.commit()

    request = recall_request(
        query="继续检查晚更正",
        mode="history",
        request_id="TEST-p09-late-order-hydrate",
    )
    search = SearchContext.from_request(request, ctx, now=FixedClock.now, deadline=200.0)
    candidate = CandidateRef("episode", episode_ref, episode_revision, "exact_ref")
    with core.storage.read(ctx) as tx:
        episode = tx.episodes.get(episode_ref, episode_revision)
        assert episode is not None, (episode_ref, episode_revision)
        evidence = RetrievalStorage(clock=FixedClock())._evidence(tx, "episode", episode_ref, episode_revision, search)
        assert evidence is not None, episode
        obj = RetrievalStorage(clock=FixedClock()).hydrate(tx, candidate, search)
    assert obj is not None, episode
    metadata = dict(obj.metadata)
    order = json.loads(metadata["source_order"])
    assert len(order) == 30
    assert order[0][0] == sources[0].ref
    assert order[-1][0] == sources[-1].ref
    assert order[-1][2] > 24
    texts = json.loads(metadata["source_texts"])
    assert texts[late_ref] == sources[-1].event["content"]


def test_p09_episode_hydration_binds_history_revision_pairs(app, monkeypatch):
    """A retained ref@revision cannot cause unrelated source revisions to be read."""
    core, ctx = app
    sources = [
        capture(
            core,
            ctx,
            f"第 {index} 条版本事件。",
            key=f"TEST-p09/pair-order/{index}",
            when="2026-09-01T12:00:00Z",
        )
        for index in range(1, 26)
    ]
    # Keep the same source_ref while adding a historical version which is
    # deliberately absent from the resume's retained evidence pairs.
    historical = capture(
        core,
        ctx,
        "第 1 条版本事件的历史修订版。",
        key="TEST-p09/pair-order/1",
        revision=2,
        when="2026-09-02T12:00:00Z",
    )
    assert historical.ref == sources[0].ref
    old_ref = f"{sources[0].ref}@1"
    late_ref = f"{sources[-1].ref}@1"
    with sqlite3.connect(core.storage.path) as conn:
        conn.row_factory = sqlite3.Row
        episode_row = conn.execute(
            """SELECT e.episode_id,e.current_revision
               FROM episodes e JOIN episode_events ee ON ee.episode_id=e.episode_id
               WHERE ee.source_ref=? AND e.scope_id=? AND e.project_id=? AND e.branch_id=?
               ORDER BY e.episode_id LIMIT 1""",
            (sources[-1].ref, "TEST-scope", "TEST-project", "TEST-main"),
        ).fetchone()
        assert episode_row is not None
        episode_ref, episode_revision = episode_row["episode_id"], episode_row["current_revision"]
        sequence = conn.execute(
            "SELECT MAX(sequence) FROM episode_events WHERE episode_id=?", (episode_ref,)
        ).fetchone()[0] + 1
        conn.execute(
            """INSERT OR IGNORE INTO episode_events(sequence,episode_id,source_ref,source_revision,membership)
               VALUES (?,?,?,?,?)""",
            (sequence, episode_ref, historical.ref, historical.revision, "anchored"),
        )
        resume = {
            "episode_ref": episode_ref,
            "state": "open",
            "decisions": [{"text": "第25条晚更正", "evidence_refs": [late_ref]}],
            "verified_progress": [],
            "open_items": [],
            "next_step": "继续检查晚更正",
            "next_step_basis": "user_requested",
            "evidence_refs": [old_ref, late_ref],
        }
        conn.execute(
            "UPDATE episode_versions SET resume_json=? WHERE episode_id=? AND revision=?",
            (json.dumps(resume, ensure_ascii=False), episode_ref, episode_revision),
        )
        # Lineage rows sit at the revision each source entered and a revision
        # reads every row at or below it, so the retained set is rebuilt whole.
        conn.execute(
            "DELETE FROM evidence_links WHERE object_kind='episode' AND object_ref=?",
            (episode_ref,),
        )
        for source in (sources[0], sources[-1]):
            conn.execute(
                """INSERT OR IGNORE INTO evidence_links(
                    object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote
                ) VALUES ('episode',?,?,?,?,?,?)""",
                (episode_ref, episode_revision, source.ref, source.revision, "derived_from", source.event["content"]),
            )
        conn.commit()

    traces: list[str] = []
    original_open = core.storage._open

    def traced_open(mode, remaining_seconds=None, *, restoring=False):
        connection = original_open(mode, remaining_seconds, restoring=restoring)
        connection.set_trace_callback(traces.append)
        return connection

    monkeypatch.setattr(core.storage, "_open", traced_open)
    request = recall_request(query="继续检查晚更正", mode="history", request_id="TEST-p09-pair-order")
    search = SearchContext.from_request(request, ctx, now=FixedClock.now, deadline=200.0)
    with core.storage.read(ctx) as tx:
        obj = RetrievalStorage(clock=FixedClock()).hydrate(
            tx, CandidateRef("episode", episode_ref, episode_revision, "exact_ref"), search
        )
    assert obj is not None
    metadata = dict(obj.metadata)
    order = json.loads(metadata["source_order"])
    assert {f"{ref}@{revision}" for ref, revision, _sequence in order} == {old_ref, late_ref}
    texts = json.loads(metadata["source_texts"])
    assert f"{historical.ref}@{historical.revision}" not in texts
    pair_selects = [sql for sql in traces if "SELECT sequence,source_ref,source_revision" in sql]
    assert pair_selects and all("source_ref,source_revision) IN" in sql for sql in pair_selects)


def test_p09_comparison_reduced_to_one_side_becomes_partial_not_supported(app):
    core, ctx = app
    capture(core, ctx, "H100 使用第一套部署配置。", key="TEST-p09/compare-one", revision=1, when="2026-09-01T12:00:00Z")
    capture(core, ctx, "H200 使用第二套部署配置。", key="TEST-p09/compare-two", revision=2, when="2026-09-05T12:00:00Z")
    result = core.recall(
        ctx,
        recall_request(query="比较 H100 和 H200 部署配置", mode="history", request_id="TEST-p09-compare"),
        deadline_seconds=5,
    )
    search = SearchContext.from_request(
        recall_request(
            query="比较 H100 和 H200 部署配置",
            mode="history",
            max_items=1,
            budget_tokens=512,
            request_id="TEST-p09-compare-compile",
        ),
        ctx,
        now=FixedClock.now,
        deadline=200.0,
    )
    packet = compile_recall_packet(search, result, core.storage, clock=FixedClock())
    assert len(packet["items"]) <= 1
    assert packet["answerability"] in {"partial", "unknown"}
    assert "comparison_second_side" in packet["unmet_needs"] or packet["status"] == "partial"


def test_p09_renderer_dedupes_same_installation_session_request(app):
    core, ctx = app
    capture(core, ctx, "P09 renderer dedupe H100 record.", key="TEST-p09/render-dedupe")
    packet = _packet(core, ctx, query="renderer dedupe H100", mode="current", request_id="TEST-p09-render-dedupe")
    first = core.prepare_recall_render(ctx, packet)
    second = core.prepare_recall_render(ctx, packet)
    assert first.render_ref is not None
    assert first.canonical_text is not None
    assert estimate_tokens(first.canonical_text) <= 1200
    assert second.render_ref is None
    assert second.context is None
    assert second.canonical_text is None
    assert first.render_ref is not None
    assert "instruction" not in str(first.context).casefold()
    assert first.context["schema"] == "scope-recall.recall_context/1.1"


def test_p09_renderer_separate_session_produces_distinct_preparation(app):
    core, ctx = app
    capture(core, ctx, "P09 renderer session isolation.", key="TEST-p09/render-session")
    request = recall_request(query="renderer session isolation", mode="current", request_id="TEST-p09-render-session")
    packet = core.recall_packet(ctx, request, deadline_seconds=5)
    first = core.prepare_recall_render(ctx, packet)
    other_ctx = replace(ctx, session_id="TEST-render-other-session")
    second = core.prepare_recall_render(other_ctx, packet)
    assert first.render_ref != second.render_ref
    assert first.context == second.context
    assert first.canonical_text == second.canonical_text


def test_p09_renderer_empty_packet_yields_no_context(app):
    core, ctx = app
    packet = dict(
        protocol_version="1.1",
        request_id="TEST-p09-render-empty",
        status="no_match",
        memory_epoch=None,
        items=[],
        gaps=[],
        diagnostic_ref=None,
        answerability="unknown",
        coverage="unknown",
        unmet_needs=[],
    )
    prepared = render_recall_packet_context(
        packet,
        installation_id=ctx.binding.installation_id,
        session_id=ctx.session_id,
        renderer=RecallPacketRenderer(),
    )
    assert prepared.render_ref is None
    assert prepared.context is None
    assert prepared.canonical_text is None


def test_empty_episode_has_no_compact_object_variant():
    obj = RetrievedObject(
        "episode-empty-compact",
        1,
        "episode",
        '{"notes":"bookkeeping only"}',
        "derived_summary",
        "current",
        "trusted scope",
        ("event-empty@1",),
        "derived_summary",
        True,
        ("episode",),
    )
    assert compact_episode_variants(obj) == ()


def test_supported_episode_compact_is_not_empty_object():
    obj = RetrievedObject(
        "episode-goal-compact",
        1,
        "episode",
        '{"goal":"resume the delivery window","notes":"bookkeeping only"}',
        "derived_summary",
        "current",
        "trusted scope",
        ("event-goal@1",),
        "derived_summary",
        True,
        ("episode",),
    )
    variants = [canonical_render_json(variant) for variant in compact_episode_variants(obj)]
    assert variants
    assert "{}" not in variants
    assert any("resume the delivery window" in item for item in variants)


@pytest.mark.parametrize("budget", [1600, 3200, 4096])
@pytest.mark.parametrize("suffix", ["alpha", "other-identifiers"])
def test_budget_density_keeps_773_byte_hit_over_3398_byte_repeat(app, budget, suffix):
    """The former fit threshold must not let a bulky repeat win a single slot."""
    from scope_recall.core.recall import RetrievalPipeline

    core, ctx = app

    def rendered_bytes(obj):
        return len(canonical_render_json(packet_item(obj)).encode("utf-8"))

    def object_of_size(ref, size, phrase):
        obj = RetrievedObject(ref=ref, revision=1, kind="event", content="x",
                              origin="imported", temporal_status="current", applicability="trusted scope",
                              evidence_refs=(ref + "@1",), basis="observed", expandable=True, source_kinds=("event",))
        remaining = size - rendered_bytes(obj) + 1
        count = remaining // len(phrase.encode("utf-8"))
        content = phrase * count
        content += "x" * (remaining - len(content.encode("utf-8")))
        obj = replace(obj, content=content)
        assert rendered_bytes(obj) == size
        return obj

    large = object_of_size("event-repeat-" + suffix, 3398, "预算重复导入事件。")
    target = object_of_size("event-answer-" + suffix, 773, "预算有效目标命中。")
    pairs = [(CandidateRef("event", obj.ref, 1, "lexical", fusion_score=score), obj)
             for obj, score in ((large, .032), (target, .030))]
    search = SearchContext.from_request(recall_request(query="预算", mode="history", max_items=1,
                                                      budget_tokens=budget, request_id="TEST-density"),
                                        ctx, now=FixedClock.now, deadline=200.0)
    # Exercise the earlier retrieval admission as well as final compilation.
    pipeline = object.__new__(RetrievalPipeline)
    assert pipeline._apply_budget(pairs, search.limits)[0][1].ref == target.ref

    class FixtureStorage(RetrievalStorage):
        def hydrate(self, tx, candidate, context):
            return next(obj for _, obj in pairs if obj.ref == candidate.ref)

    result = RetrievalResult(tuple(c for c, _ in pairs), tuple(o for _, o in pairs),
                             core.status(ctx).memory_epoch, (), "unknown", "supported", 2, 2,
                             request_id="TEST-density")
    packet = compile_recall_packet(search, result, core.storage, storage_reader=FixtureStorage(clock=FixedClock()),
                                   diagnostics=core.recall_diagnostics, clock=FixedClock())
    assert [item["ref"] for item in packet["items"]] == [target.ref]
    rendered = canonical_render_json(packet)
    metrics = core.recall_diagnostics.get(packet["diagnostic_ref"])
    assert metrics.rendered_bytes == len(rendered.encode("utf-8"))
    assert metrics.estimated_tokens == estimate_tokens(rendered) <= budget
    assert metrics.budget_tokens == budget
    print(json.dumps({"budget": budget, "item_bytes": [3398, 773], "target": target.ref,
                      "delivered": [item["ref"] for item in packet["items"]], "metrics": metrics.to_public()}))


@pytest.mark.parametrize("mode", ["auto", "current", "history"])
def test_budget_density_keeps_fusion_order_when_the_whole_run_fits(app, mode):
    """A newer long report that fits beside an older short todo is not demoted for its length.

    Density ordering is for the choice above, when the cap cannot hold both.
    Here both fit, so retrieval and the compiler keep the ranker's order.
    """
    core, ctx = app
    query = "阿乙升级 rc29"
    todo = capture(core, ctx, "阿乙 rc29 升级待办：self-cutover pending，等排空后再切换。",
                   key="TEST-p09/density-fit/todo", when="2026-09-03T08:00:00Z", recorded_at="2026-09-03T08:00:00Z")
    report = capture(core, ctx, "阿乙 rc29 升级收尾报告：排空超时，升级没有开始，现在还是 rc28，旧记忆和设置没动。"
                     + "排空等待记录显示网关在超时前仍有写入者，自动重启器没有先停下，所以升级器按规则放弃。" * 8,
                     key="TEST-p09/density-fit/report", when="2026-09-05T08:00:00Z", recorded_at="2026-09-05T08:00:00Z")
    terms = set(meaningful_query_terms(query))
    assert terms.intersection(lexical_terms(report.event["content"])) == terms.intersection(lexical_terms(todo.event["content"]))
    assert estimate_tokens(todo.event["content"]) < 256 < estimate_tokens(report.event["content"])
    # Another session reads, so only the lexical channel ranks the two.
    reader = replace(ctx, session_id="TEST-p09-density-fit-reader")

    request = recall_request(query=query, mode=mode, request_id=f"TEST-density-fit-{mode}")
    assert [item.ref for item in core.recall(reader, request, deadline_seconds=5).items] == [report.ref, todo.ref]
    assert [item["ref"] for item in _packet(core, reader, **request)["items"]] == [report.ref, todo.ref]

    # One slot is a choice again, and the compact todo still takes it.
    single = dict(request, max_items=1)
    assert [item.ref for item in core.recall(reader, single, deadline_seconds=5).items] == [todo.ref]
    assert [item["ref"] for item in _packet(core, reader, **single)["items"]] == [todo.ref]


def test_cjk_budget_uses_character_classes_and_reports_exact_units(app):
    core, ctx = app
    assert estimate_tokens("汉字中文") == 4
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("漢あ한") == 3
    assert estimate_tokens("汉字abcd") == 3
    sources = [capture(core, ctx, "预算中文记录 " + "保留完整有效证据" * 50 + str(i), key=f"TEST-CJK/{i}")
               for i in range(4)]
    search = SearchContext.from_request(recall_request(query="预算中文记录", mode="history", max_items=4,
                                                       budget_tokens=4096), ctx, now=FixedClock.now, deadline=200.0)
    candidates = tuple(CandidateRef("event", s.ref, 1, "lexical") for s in sources)
    reader = RetrievalStorage(clock=FixedClock())
    with core.storage.read(ctx) as tx:
        objects = tuple(reader.hydrate(tx, candidate, search) for candidate in candidates)
    result = RetrievalResult(candidates, objects, core.status(ctx).memory_epoch, (), "unknown", "supported", 4, 4)
    packet = compile_recall_packet(search, result, core.storage, storage_reader=reader,
                                   diagnostics=core.recall_diagnostics, clock=FixedClock())
    assert {item["ref"] for item in packet["items"]} == {source.ref for source in sources}
    rendered = canonical_render_json(packet)
    byte_count = len(rendered.encode("utf-8"))
    token_count = estimate_tokens(rendered)
    assert byte_count > 4096 >= token_count, "byte cap would discard evidence that fits the token estimate"
    diagnostic = core.recall_diagnostics.get(packet["diagnostic_ref"])
    assert diagnostic.rendered_bytes == byte_count
    assert diagnostic.estimated_tokens == token_count
    assert diagnostic.budget_tokens == 4096
    print(json.dumps({"cjk_delivered": len(packet["items"]), "bytes": byte_count, "estimated_tokens": token_count,
                      "budget_tokens": diagnostic.budget_tokens}))
