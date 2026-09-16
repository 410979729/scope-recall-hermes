"""P08 recall admission and coverage contracts over isolated SQLite.

These tests intentionally exercise the core recall entry point with a small
deterministic vector port.  The vector port only proposes candidates; SQLite
remains the authority for scope, version, deletion, and temporal eligibility.
"""

from __future__ import annotations

from dataclasses import replace
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pytest

from scope_recall.contracts import ContractError, InstanceBinding, TrustedContext
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.events import lexical_terms, query_terms
from scope_recall.core.recall_policy import RecallPolicy, SPACE_ID
from scope_recall.core.retrieval import CandidateRef, CollectionQuery, SearchContext
from scope_recall.core.recall_packet import canonical_render_json
from scope_recall.core.retrieval_storage import scope_digest
from v11_support import context, recall_request, source_event


class FixedClock:
    now = "2026-09-06T12:00:00Z"

    def utc_now(self) -> str:
        return self.now

    def monotonic(self) -> float:
        # A stable clock makes deadline propagation assertions deterministic.
        return 100.0


@dataclass
class DeterministicVectors:
    candidates: tuple[CandidateRef, ...] = ()

    def __post_init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def search(self, context: SearchContext, *, limit: int, remaining_seconds: float) -> tuple[CandidateRef, ...]:
        self.calls.append({"query": context.query, "limit": limit, "remaining_seconds": remaining_seconds, "context": context})
        return self.candidates[:limit]


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _item_ref(item: Any) -> str:
    return str(_field(item, "ref", _field(item, "id", "")))


def _item_key(item: Any) -> tuple[str, int]:
    return _item_ref(item), int(_field(item, "revision", 1))


def _items(result: Any) -> tuple[Any, ...]:
    return tuple(getattr(result, "items", ()))


def recall(core: MemoryCore, ctx: TrustedContext, *, query: str, mode: str = "auto", max_items: int = 6,
           budget_tokens: int = 1200, as_of: str | None = None, current_source_refs: tuple[str, ...] = (),
           deadline_seconds: float = 2.0, focus_refs: tuple[str, ...] = ()) -> Any:
    values: dict[str, Any] = {"query": query, "mode": mode, "max_items": max_items, "budget_tokens": budget_tokens}
    if as_of is not None:
        values["as_of"] = as_of
    if focus_refs:
        values["focus_refs"] = list(focus_refs)
    return core.recall(ctx, recall_request(**values), current_source_refs=current_source_refs, deadline_seconds=deadline_seconds)


def _capture(core: MemoryCore, ctx: TrustedContext, key: str, content: str, *, revision: int = 1,
             occurred_at: str | None = "2026-09-05T12:00:00Z", origin: str = "human_direct") -> Any:
    event = source_event(
        source_event_key=key,
        source_revision=revision,
        origin=origin,
        role="tool" if origin == "tool_observation" else "user",
        content=content,
        occurred_at=occurred_at,
        recorded_at="2026-09-06T12:00:00Z",
        time_precision="instant" if occurred_at is not None else "unknown",
    )
    receipt = core.record_event(replace(ctx, actor_origin=origin), event,
                                scope_id=next(iter(ctx.allowed_scope_ids)), remaining_seconds=10)
    assert receipt.durability == "persisted"
    return core.source(ctx, receipt.event_refs[0].ref, revision)


def _app(tmp_path: Path, *, vectors: DeterministicVectors | None = None) -> tuple[MemoryCore, TrustedContext, DeterministicVectors]:
    ctx = context(tmp_path / "TEST-p08")
    vector_port = vectors or DeterministicVectors()
    core = MemoryCore(CoreConfig(ctx.binding), clock=FixedClock(), vectors=vector_port, retrieval_policy=RecallPolicy(vector_threshold=0.8))
    core.initialize()
    return core, ctx, vector_port


def _candidate(source: Any, *, score: float = 0.95, space: str = SPACE_ID) -> CandidateRef:
    return CandidateRef("event", source.ref, source.revision, "vector", vector_score=score, vector_id=f"TEST-vector:{source.ref}@{source.revision}", embedding_space=space)


def _claim_candidate(version: Any, *, score: float = 0.95) -> CandidateRef:
    return CandidateRef("claim", version.ref, version.revision, "vector", vector_score=score,
                        vector_id=f"TEST-vector:{version.ref}@{version.revision}", embedding_space=SPACE_ID)


def _claim_proposal(source: Any, *, value: str, kind: str = "fact", predicate: str = "配置",
                    conditions: list[str] | None = None, valid_from: str | None = None,
                    valid_to: str | None = None, procedure: dict[str, Any] | None = None) -> dict[str, Any]:
    proposal: dict[str, Any] = {
        "kind": kind,
        "subject": "TEST-project",
        "predicate": predicate,
        "value_text": value,
        "conditions": list(conditions or []),
        "statement_kind": "assertion",
        "valid_from": source.event["occurred_at"] if valid_from is None else valid_from,
        "valid_to": valid_to,
        "evidence_spans": [{"source_ref": source.ref, "source_revision": source.revision, "quote": source.event["content"]}],
    }
    if procedure is not None:
        proposal["procedure"] = procedure
    return proposal


def _accept_claim(core: MemoryCore, ctx: TrustedContext, proposal: dict[str, Any]) -> Any:
    refs = [f"{span['source_ref']}@{span['source_revision']}" for span in proposal["evidence_spans"]]
    procedure = proposal.get("procedure") or {}
    refs.extend(ref for ref in procedure.get("counterexample_refs", ()) if ref not in refs)
    return core.accept_claim_proposals(
        ctx,
        {"protocol_version": "1.1", "source_refs": list(dict.fromkeys(refs)), "claim_proposals": [proposal],
         "resume_proposals": [], "reference_proposals": []},
        scope_id="TEST-scope", remaining_seconds=10,
    ).items[0]


def _accept_relation_claim(core: MemoryCore, ctx: TrustedContext, root: Any, peer: Any, index: int) -> Any:
    root_ref = f"{root.ref}@{root.revision}"
    peer_ref = f"{peer.ref}@{peer.revision}"
    proposal = {
        "kind": "fact",
        "subject": f"TEST-relation-{index}",
        "predicate": "connected_to",
        "value_text": peer.ref,
        "conditions": [],
        "statement_kind": "assertion",
        "valid_from": root.event["occurred_at"],
        "valid_to": None,
        "evidence_spans": [
            {"source_ref": root.ref, "source_revision": root.revision, "quote": root.event["content"]},
            {"source_ref": peer.ref, "source_revision": peer.revision, "quote": peer.event["content"]},
        ],
    }
    return core.accept_claim_proposals(
        ctx,
        {"protocol_version": "1.1", "source_refs": [root_ref, peer_ref], "claim_proposals": [proposal], "resume_proposals": [], "reference_proposals": []},
        scope_id="TEST-scope",
        remaining_seconds=10,
    )


def test_P08_vector_semantic_match_is_admitted_without_lexical_overlap(tmp_path):
    core, ctx, vectors = _app(tmp_path)
    source_text = "海报四周压低明度，核心图案保留高光。"
    query = "用什么设计手法吸引注意焦点？"
    assert set(lexical_terms(source_text)).isdisjoint(query_terms(query))
    source = _capture(core, ctx, "TEST-M18/source", source_text)
    vectors.candidates = (_candidate(source),)
    before = core.storage.path.read_bytes()

    result = recall(core, replace(ctx, session_id="TEST-fresh"), query=query)

    assert result.items
    assert source.ref in {_item_ref(item) for item in result.items}
    assert next(item for item in result.items if _item_ref(item) == source.ref).basis in {"direct_report", "observed", "derived_summary"}
    assert core.storage.path.read_bytes() == before
    assert vectors.calls and vectors.calls[0]["query"] == query


def test_P08_high_score_candidates_still_no_match_after_authority_hydration(tmp_path):
    binding = InstanceBinding(
        "TEST-agent", "TEST-installation", (tmp_path / "TEST-p08").resolve(), frozenset({"TEST-scope", "TEST-private"}), True
    )
    ctx = TrustedContext(binding, "TEST-session", frozenset({"TEST-scope"}), "human_direct", project_id="TEST-project", branch_id="main")
    vectors = DeterministicVectors()
    core = MemoryCore(CoreConfig(binding), clock=FixedClock(), vectors=vectors, retrieval_policy=RecallPolicy(vector_threshold=0.8))
    core.initialize()

    current = _capture(core, ctx, "TEST-stale", "旧版本的无关记录。", revision=1)
    _capture(core, ctx, "TEST-stale", "更新后的无关记录。", revision=2)
    private_ctx = replace(ctx, allowed_scope_ids=frozenset({"TEST-private"}), session_id="TEST-private-session")
    permission = _capture(core, private_ctx, "TEST-private", "私有范围的无关记录。")
    outside = _capture(core, replace(ctx, branch_id="other"), "TEST-outside", "另一个分支的无关记录。")
    deleted = _capture(core, ctx, "TEST-deleted", "已经删除的无关记录。")
    oldspace = _capture(core, ctx, "TEST-old-space", "旧向量空间的无关记录。")
    _capture(core, ctx, "TEST-delete-proof", f"删除 {deleted.ref}")
    core.forget(ctx, {"protocol_version": "1.1", "target_refs": [deleted.ref], "mode": "delete", "expected_revisions": {deleted.ref: 1}}, remaining_seconds=10)

    vectors.candidates = (
        _candidate(current),
        _candidate(permission),
        _candidate(outside),
        _candidate(deleted),
        CandidateRef("event", oldspace.ref, oldspace.revision, "vector", vector_score=0.999, vector_id=f"TEST-vector:{oldspace.ref}@1", embedding_space="OLD-space"),
    )
    result = recall(core, ctx, query="量子生物学。")

    assert result.items == ()
    assert result.answerability_hint == "unknown"


def test_P08_runtime_source_is_excluded_but_identical_history_remains(tmp_path):
    core, ctx, vectors = _app(tmp_path)
    historical = _capture(core, ctx, "TEST-history", "我们那次把周边元素淡化，使中间主体更醒目。")
    fresh = _capture(core, replace(ctx, session_id="TEST-live"), "TEST-live-query", "我们那次把周边元素淡化，使中间主体更醒目。")
    vectors.candidates = (_candidate(fresh), _candidate(historical, score=0.90))

    result = recall(core, replace(ctx, session_id="TEST-live"), query="怎样把周边元素淡化，让中间主体更醒目？", current_source_refs=(f"{fresh.ref}@{fresh.revision}",))
    refs = [_item_ref(item) for item in result.items]
    assert fresh.ref not in refs
    assert historical.ref in refs


def test_P08_identifier_mismatch_is_rejected_but_explicit_comparison_admits_both(tmp_path):
    core, ctx, vectors = _app(tmp_path)
    h100 = _capture(core, ctx, "TEST-H100", "TEST项目使用 H100 方案。")
    h200 = _capture(core, ctx, "TEST-H200", "TEST项目使用 H200 方案。")
    vectors.candidates = (_candidate(h200), _candidate(h100, score=0.90))

    mismatch = recall(core, ctx, query="TEST项目 H100 方案的记录是什么？")
    assert h200.ref not in {_item_ref(item) for item in mismatch.items}
    assert h100.ref in {_item_ref(item) for item in mismatch.items}

    comparison = recall(core, ctx, query="比较 TEST 项目的 H100 和 H200 方案。")
    comparison_refs = {_item_ref(item) for item in comparison.items}
    assert {h100.ref, h200.ref} <= comparison_refs


def test_P08_current_history_and_as_of_filter_vector_candidates(tmp_path):
    core, ctx, vectors = _app(tmp_path)
    old = _capture(core, ctx, "TEST-temporal", "TEST设置是旧版银色。", revision=1, occurred_at="2026-09-01T12:00:00Z")
    current = _capture(core, ctx, "TEST-temporal", "TEST设置是新版金色。", revision=2, occurred_at="2026-09-05T12:00:00Z")
    vectors.candidates = (_candidate(current), _candidate(old, score=0.90))

    current_packet = recall(core, ctx, query="TEST设置", mode="current")
    history_packet = recall(core, ctx, query="TEST设置", mode="history")
    as_of_packet = recall(core, ctx, query="TEST设置", mode="as_of", as_of="2026-09-02T00:00:00Z")

    current_keys = {_item_key(item) for item in current_packet.items}
    history_keys = {_item_key(item) for item in history_packet.items}
    as_of_keys = {_item_key(item) for item in as_of_packet.items}
    assert (current.ref, 2) in current_keys
    assert (old.ref, 1) not in current_keys
    assert {(old.ref, 1), (current.ref, 2)} <= history_keys
    assert (old.ref, 1) in as_of_keys
    assert (current.ref, 2) not in as_of_keys


def test_P08_recall_is_zero_authority_write_and_preserves_epoch(tmp_path):
    core, ctx, vectors = _app(tmp_path)
    source = _capture(core, ctx, "TEST-readonly", "TEST只读召回证据。")
    vectors.candidates = (_candidate(source),)
    epoch = core.status(ctx).memory_epoch
    before = core.storage.path.read_bytes()

    result = recall(core, ctx, query="TEST只读召回证据。")

    assert result.memory_epoch == epoch
    assert core.status(ctx).memory_epoch == epoch
    assert core.storage.path.read_bytes() == before


@pytest.mark.parametrize("mode", ["auto", "current", "history"])
def test_P08_single_deadline_is_forwarded_to_vector_port(tmp_path, mode):
    core, ctx, vectors = _app(tmp_path)
    source = _capture(core, ctx, "TEST-deadline", "TEST截止时间证据。")
    vectors.candidates = (_candidate(source),)

    recall(core, ctx, query="TEST截止时间证据。", mode=mode, deadline_seconds=0.25)

    assert vectors.calls
    assert all(call["remaining_seconds"] <= 0.25 + 1e-6 for call in vectors.calls)
    assert all(call["remaining_seconds"] >= 0 for call in vectors.calls)


def test_P08_collection_reports_partial_then_complete_and_rejects_cursor_replay(tmp_path):
    core, ctx, _vectors = _app(tmp_path)
    for index in range(5):
        _capture(core, ctx, f"TEST-collection/{index}", f"TEST集合候选 {index}。")
    epoch = core.status(ctx).memory_epoch
    query = CollectionQuery(
        "event", page_size=2, memory_epoch=epoch, scope_digest=scope_digest(ctx), mode="current"
    )

    first = core.collection(ctx, query)
    assert len(first.items) == 2
    assert first.coverage == "partial"
    assert first.next_cursor is not None

    with pytest.raises(ContractError, match="cursor"):
        core.collection(
            ctx,
            replace(query, where=(("scope_id", "TEST-scope"),)),
            cursor=first.next_cursor,
        )

    other_context = replace(ctx, project_id="TEST-other")
    other_query = replace(query, scope_digest=scope_digest(other_context))
    with pytest.raises(ContractError, match="cursor"):
        core.collection(other_context, other_query, cursor=first.next_cursor)

    second = core.collection(ctx, query, cursor=first.next_cursor)
    assert {item.ref for item in first.items}.isdisjoint({item.ref for item in second.items})
    assert len(first.items) + len(second.items) == 4
    assert second.coverage in {"partial", "complete_for_query"}

    complete_query = replace(query, page_size=10)
    complete = core.collection(ctx, complete_query)
    assert len(complete.items) == 5
    assert complete.coverage == "complete_for_query"

    _capture(core, ctx, "TEST-collection/epoch", "TEST集合水位变化。")
    with pytest.raises(ContractError, match="epoch"):
        core.collection(ctx, query, cursor=first.next_cursor)


def test_P08_relation_expansion_stays_within_two_hops_and_24_objects(tmp_path):
    core, ctx, vectors = _app(tmp_path)
    root = _capture(core, ctx, "TEST-relation/root", "TEST relation root source.")
    for index in range(30):
        peer = _capture(core, ctx, f"TEST-relation/peer/{index}", f"TEST relation peer {index}.")
        accepted = _accept_relation_claim(core, ctx, root, peer, index)
        assert accepted.items and accepted.items[0].state in {"active", "proposed"}
    vectors.candidates = (_candidate(root),)

    result = recall(core, ctx, query="量子生物学。", max_items=30, deadline_seconds=0.5)

    assert len(result.items) <= 25  # one seed plus the documented 24 related objects
    assert "relation_bound_reached" in result.gaps
    assert vectors.calls and 0 <= vectors.calls[0]["remaining_seconds"] <= 0.5


def test_P08_related_lookup_keeps_evidence_object_kind_isolated(tmp_path):
    """An event candidate must not inherit a claim relation sharing its ref."""
    core, ctx, _vectors = _app(tmp_path)
    root = _capture(core, ctx, "TEST-relation-kind/root", "TEST relation kind root")
    peer = _capture(core, ctx, "TEST-relation-kind/peer", "TEST relation kind peer")
    with core.storage.write(ctx, remaining_seconds=5) as tx:
        tx._check().execute(
            """INSERT INTO evidence_links(
                object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote
            ) VALUES (?,?,?,?,?,?,?)""",
            ("claim", root.ref, root.revision, peer.ref, peer.revision, "supports", peer.event["content"]),
        )
        tx._check().execute(
            """INSERT INTO evidence_links(
                object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote
            ) VALUES (?,?,?,?,?,?,?)""",
            ("event", root.ref, root.revision, root.ref, root.revision, "supports", root.event["content"]),
        )
    candidate = CandidateRef("event", root.ref, root.revision, "exact_ref")
    with core.storage.read(ctx, remaining_seconds=5) as tx:
        related = core.recall_pipeline.storage_reader.related(tx, candidate, limit=24)
    assert all(item.kind != "claim" for item in related)
    assert all(item.ref != peer.ref for item in related)


def test_P08_claim_state_reuses_current_history_and_as_of_semantics(tmp_path):
    core, ctx, vectors = _app(tmp_path)
    proposed_source = _capture(core, ctx, "TEST-claim/proposed", "TEST-project 也许采用紫色配置。",
                               origin="assistant_visible")
    proposed = _accept_claim(core, ctx, _claim_proposal(proposed_source, value="紫色", kind="decision"))
    assert proposed.state == "proposed"

    old_source = _capture(core, ctx, "TEST-claim/temporal/old", "TEST-project 配置旧版银色。",
                          occurred_at="2026-09-01T12:00:00Z")
    old = _accept_claim(core, ctx, _claim_proposal(old_source, value="旧版银色"))
    future_source = _capture(core, ctx, "TEST-claim/temporal/future", "TEST-project 配置未来金色。",
                             occurred_at="2026-09-05T12:00:00Z")
    future = _accept_claim(core, ctx, _claim_proposal(
        future_source, value="未来金色", valid_from="2026-09-10T00:00:00Z"
    ))
    assert future.ref == old.ref and future.revision == old.revision + 1

    # Two sentences on purpose. The first is a plain assertion qualification can
    # promote; the second grounds the end date the proposal carries. Written as
    # one "用白色，直到2026-09-04" clause the entailment gate reads 直到 as a
    # transition, reports polarity unknown and arguments unaligned, and leaves
    # the claim proposed — which made every as_of assertion below unreachable
    # and this whole temporal contract untested. (That gate limitation is real
    # and separate: qualify()'s own comment says dated statements should be
    # carried by their bounded interval, and the check was never taught to.)
    ended_source = _capture(core, ctx, "TEST-claim/ended", "TEST-project 颜色配置是白色。有效期到 2026-09-04。",
                            occurred_at="2026-09-02T12:00:00Z")
    ended = _accept_claim(core, ctx, _claim_proposal(ended_source, value="白色", predicate="颜色配置",
                                                     valid_to="2026-09-04T00:00:00Z"))
    ended_next_source = _capture(core, ctx, "TEST-claim/ended-next", "TEST-project 颜色配置当前黑色。",
                                 occurred_at="2026-09-05T12:00:00Z")
    ended_next = _accept_claim(core, ctx, _claim_proposal(ended_next_source, value="当前黑色", predicate="颜色配置"))
    assert ended_next.ref == ended.ref and ended_next.revision == ended.revision + 1

    vectors.candidates = (_claim_candidate(proposed), _claim_candidate(old), _claim_candidate(future),
                          _claim_candidate(ended), _claim_candidate(ended_next))
    claim_refs = tuple(f"{ref}@{revision}" for ref, revision in (
        (old.ref, old.revision), (future.ref, future.revision),
        (ended.ref, ended.revision), (ended_next.ref, ended_next.revision)))
    proposed_only = recall(core, ctx, query="配置", mode="current", focus_refs=(f"{proposed.ref}@{proposed.revision}",))
    current = recall(core, ctx, query="配置", mode="current", max_items=6, focus_refs=claim_refs)
    history = recall(core, ctx, query="配置", mode="history", max_items=6, focus_refs=claim_refs)
    as_of = recall(core, ctx, query="配置", mode="as_of", as_of="2026-09-03T00:00:00Z", max_items=6, focus_refs=claim_refs)

    current_keys = {_item_key(item) for item in current.items}
    history_keys = {_item_key(item) for item in history.items}
    as_of_keys = {_item_key(item) for item in as_of.items}
    assert (proposed.ref, proposed.revision) not in {_item_key(item) for item in proposed_only.items}
    assert (old.ref, old.revision) in current_keys
    assert (future.ref, future.revision) not in current_keys
    assert (ended.ref, ended.revision) in history_keys
    assert (ended.ref, ended.revision) in as_of_keys
    assert (ended_next.ref, ended_next.revision) not in as_of_keys


def test_P08_procedure_recall_preserves_qualifiers_and_basis(tmp_path):
    core, ctx, vectors = _app(tmp_path)
    source = _capture(core, ctx, "TEST-procedure/accepted",
                      "TEST-project 验收前检查方法已认可，先检查温度，再验收。未安装设备时不适用。",
                      )
    procedure = _accept_claim(core, ctx, _claim_proposal(
        source, value="检查方法", kind="procedure", predicate="验收方法",
        conditions=["验收前"], procedure={
            "conditions": ["验收前"], "non_applicable": ["未安装设备"],
            "method": ["先检查温度", "再验收"], "verification_basis": "user_accepted",
            "counterexample_refs": [],
        }
    ))
    assert procedure.state == "active"
    vectors.candidates = (_claim_candidate(procedure),)
    result = recall(core, ctx, query="验收方法", mode="current")
    item = next(item for item in result.items if item.ref == procedure.ref)
    payload = dict(item.metadata).get("payload_json", "")
    assert all(value in payload for value in ("验收前", "未安装设备", "先检查温度", "再验收", "user_accepted"))
    assert item.basis == "direct_report"


@pytest.mark.parametrize("origin", ["assistant_visible", "memory_reinjection"])
def test_P08_derived_echo_cannot_be_recalled_as_direct_authority(tmp_path, origin):
    core, ctx, vectors = _app(tmp_path)
    _capture(core, ctx, f"TEST-echo/root/{origin}", "TEST-project 配置蓝色。")
    echo = _capture(core, ctx, f"TEST-echo/{origin}", "TEST-project 配置银色。",
                    occurred_at="2026-09-05T12:00:00Z", origin=origin)
    proposal = _claim_proposal(echo, value="银色")
    saved = _accept_claim(core, ctx, proposal)
    assert saved.state == "proposed"
    saved_version = core.claim_history(ctx, saved.ref)[0]
    assert saved_version.basis == "inferred_suggestion"
    vectors.candidates = (_claim_candidate(saved),)
    result = recall(core, ctx, query="配置", mode="auto", focus_refs=(f"{saved.ref}@{saved.revision}",))
    recalled = [item for item in result.items if item.ref == saved.ref]
    assert not recalled
    assert core.claim_history(ctx, saved.ref)[0].state == "proposed"


def test_P08_vector_internal_type_error_is_one_call_and_operational_gap(tmp_path):
    class ExplodingVectors:
        def __init__(self):
            self.calls = 0

        def search(self, context: SearchContext, *, limit: int, remaining_seconds: float):
            self.calls += 1
            raise TypeError("internal vector implementation failure")

    vectors = ExplodingVectors()
    core, ctx, _ = _app(tmp_path, vectors=vectors)
    result = recall(core, ctx, query="TEST vector failure")
    assert vectors.calls == 1
    assert "vector_unavailable" in result.gaps
    assert "vector_error:TypeError" in result.gaps


@pytest.mark.parametrize("candidate_factory", [
    lambda source: _candidate(source, score=0.1),
    lambda source: _candidate(source),
])
def test_P08_normal_vector_rejections_have_no_operational_gap(tmp_path, candidate_factory):
    core, ctx, vectors = _app(tmp_path)
    source = _capture(core, ctx, "TEST-normal-rejection", "TEST普通拒绝证据。")
    if candidate_factory(source).vector_score == 0.95:
        candidates = (_candidate(source),)
        current_source_refs = (f"{source.ref}@{source.revision}",)
    else:
        candidates = (candidate_factory(source),)
        current_source_refs = ()
    vectors.candidates = candidates
    result = recall(core, ctx, query="量子生物学无关主题。", current_source_refs=current_source_refs)
    assert result.items == ()
    assert all(not gap.startswith("vector_") for gap in result.gaps)
    assert "sqlite_unavailable" not in result.gaps


def test_P08_collection_claim_and_episode_versions_filter_state_and_reject_cursor_replay(tmp_path):
    core, ctx, vectors = _app(tmp_path)
    claim_source = _capture(core, ctx, "TEST-collection/claim-old", "TEST-project 配置是旧值。",
                            occurred_at="2026-09-01T12:00:00Z")
    claim = _accept_claim(core, ctx, _claim_proposal(claim_source, value="旧值"))
    claim_new_source = _capture(core, ctx, "TEST-collection/claim-new", "TEST-project 配置是新值。",
                                occurred_at="2026-09-05T12:00:00Z")
    claim_new = _accept_claim(core, ctx, _claim_proposal(claim_new_source, value="新值"))
    assert claim_new.ref == claim.ref and claim_new.revision == 2
    _capture(core, ctx, "TEST-collection/episode-one", "TEST episode first")
    _capture(core, ctx, "TEST-collection/episode-two", "TEST episode second")
    epoch = core.status(ctx).memory_epoch

    current_query = CollectionQuery("claim", where=(("state", "active"),), page_size=20,
                                    memory_epoch=epoch, scope_digest=scope_digest(ctx), mode="current")
    current = core.collection(ctx, current_query)
    current_keys = {_item_key(item) for item in current.items}
    assert (claim.ref, claim_new.revision) in current_keys
    assert (claim.ref, claim.revision) not in current_keys

    history_query = replace(current_query, mode="history")
    history = core.collection(ctx, history_query)
    history_keys = {_item_key(item) for item in history.items}
    assert {(claim.ref, claim.revision), (claim.ref, claim_new.revision)} <= history_keys

    episode = core.episodes(ctx)[0]
    episode_current_query = CollectionQuery("episode", where=(("state", episode.state),), page_size=20,
                                           memory_epoch=epoch, scope_digest=scope_digest(ctx), mode="current")
    episode_current = core.collection(ctx, episode_current_query)
    assert (episode.ref, episode.revision) in {_item_key(item) for item in episode_current.items}
    episode_history = core.collection(ctx, replace(episode_current_query, mode="history"))
    episode_history_keys = {_item_key(item) for item in episode_history.items}
    assert (episode.ref, 1) in episode_history_keys and (episode.ref, episode.revision) in episode_history_keys

    event_page = core.collection(ctx, replace(current_query, object_kind="event", where=(), page_size=1))
    assert event_page.next_cursor is not None
    with pytest.raises(ContractError, match="cursor"):
        core.collection(ctx, replace(current_query, page_size=1), cursor=event_page.next_cursor)

    other_binding = replace(ctx.binding, installation_id="TEST-other-installation")
    other_ctx = replace(ctx, binding=other_binding)
    forged = replace(current_query, scope_digest=scope_digest(ctx), memory_epoch=epoch)
    with pytest.raises(ContractError):
        core.collection(other_ctx, forged, cursor=event_page.next_cursor)


def test_P08_relation_budget_counts_rejected_candidates_and_checks_original_deadline(tmp_path):
    class AdvancingClock(FixedClock):
        def __init__(self):
            self.ticks = 0

        def monotonic(self) -> float:
            self.ticks += 1
            return 100.0 + self.ticks * 0.02

    clock = AdvancingClock()
    ctx = context(tmp_path / "TEST-relation-budget")
    vectors = DeterministicVectors()
    core = MemoryCore(CoreConfig(ctx.binding), clock=clock, vectors=vectors,
                      retrieval_policy=RecallPolicy(vector_threshold=0.8))
    core.initialize()
    root = _capture(core, ctx, "TEST-budget/root", "TEST relation budget root")
    peers = []
    for index in range(30):
        peer = _capture(core, ctx, f"TEST-budget/peer/{index}", f"TEST relation budget peer {index}")
        _accept_relation_claim(core, ctx, root, peer, index)
        peers.append(peer)
    # Block several peers rather than one. Unpromoted proposals now reach recall,
    # so the relation frontier fills with accepted objects and can hit its bound
    # before ever inspecting a single blocked peer — leaving this test asserting
    # nothing. Spreading the blocks keeps a rejection inside any 24-object
    # window, which is the property under test.
    for index, blocked in enumerate(peers[:6]):
        _capture(core, ctx, f"TEST-budget/delete/{index}", f"删除 {blocked.ref}")
        core.forget(ctx, {"protocol_version": "1.1", "target_refs": [blocked.ref], "mode": "delete",
                          "expected_revisions": {blocked.ref: blocked.revision}}, remaining_seconds=10)
    vectors.candidates = (_candidate(root),)
    storage_reader = core.recall_pipeline.storage_reader
    original_related = storage_reader.related
    original_hydrate = storage_reader.hydrate
    examined: list[tuple[str, str, int]] = []
    rejected: list[tuple[str, str, int]] = []

    def related(tx, candidate, *, limit):
        rows = original_related(tx, candidate, limit=limit)
        examined.extend(item.key for item in rows)
        return rows

    def hydrate(tx, candidate, context):
        obj = original_hydrate(tx, candidate, context)
        if candidate.source == "relation" and obj is None:
            rejected.append(candidate.key)
        return obj

    storage_reader.related = related
    storage_reader.hydrate = hydrate
    # Admitting unpromoted proposals means a relation candidate now resolves its
    # evidence instead of bailing at the effective-version check, so hydration
    # costs more per candidate. At 0.25s the deadline cut hydration off before a
    # single rejection was recorded and this test asserted nothing. Keep the
    # deadline finite so the deadline path is still exercised, but give it room
    # to reach the blocked peers.
    result = recall(core, ctx, query="无关查询", max_items=30, deadline_seconds=1.0)
    assert rejected
    assert len(examined) <= 24
    assert "deadline_exceeded_relation" in result.gaps or "relation_bound_reached" in result.gaps


def test_automatic_packet_budget_keeps_later_constraint_and_stays_closed(tmp_path):
    from scope_recall.core.recall_budget import estimate_tokens
    from scope_recall.core.recall_packet import RecallPacketCompiler
    from scope_recall.core.retrieval import AUTOMATIC_PACKET_BUDGET_UNITS, SearchLimits

    assert SearchLimits().budget_tokens == AUTOMATIC_PACKET_BUDGET_UNITS == 4096
    core, ctx, vectors = _app(tmp_path)
    first = _capture(core, ctx, "TEST-hotel-1", "旅店热水恢复时间未定。")
    second = _capture(core, ctx, "TEST-hotel-2", "助手说会跟踪热水恢复进度。", origin="assistant_visible")
    third = _capture(core, ctx, "TEST-hotel-3", "物业明确今晚不能恢复、等明早检查。")
    vectors.candidates = (
        _candidate(first, score=0.99),
        _candidate(second, score=0.98),
        _candidate(third, score=0.97),
    )
    query = "旅店今晚能否用热水"
    default_search = SearchContext.from_request(
        recall_request(query=query, mode="auto", budget_tokens=8000, request_id="TEST-hotel-ceiling"),
        ctx,
        now=FixedClock.now,
        deadline=200.0,
    )
    assert RecallPacketCompiler._effective_limits(default_search).budget_tokens == AUTOMATIC_PACKET_BUDGET_UNITS
    small_search = SearchContext.from_request(
        recall_request(query=query, mode="auto", budget_tokens=1200, request_id="TEST-hotel-explicit"),
        ctx,
        now=FixedClock.now,
        deadline=200.0,
    )
    assert RecallPacketCompiler._effective_limits(small_search).budget_tokens == 1200

    default = core.recall_packet(ctx, recall_request(query=query, mode="auto", request_id="TEST-hotel-default"), deadline_seconds=5)
    default_text = " ".join(item["content"] for item in default["items"])
    assert "今晚不能恢复" in default_text and "明早" in default_text
    assert all(item["evidence_refs"] for item in default["items"])
    assert "budget_token_cap" not in default["gaps"]

    # 1200 estimated units now fit these short CJK records. This measured
    # 500-unit boundary fits two event envelopes, not three, including gaps.
    cap = 500
    capped = core.recall_packet(
        ctx,
        recall_request(query=query, mode="auto", budget_tokens=cap, request_id="TEST-hotel-capped"),
        deadline_seconds=5,
    )
    assert estimate_tokens(canonical_render_json(capped)) <= cap
    assert "budget_token_cap" in capped["gaps"]
    # A budget cut comes off the bottom of the ranking, and what is cut is named.
    #
    # This used to assert that "今晚不能恢复" was dropped, on the assumption that
    # the ranking followed the vector scores above. It does not: that source is
    # the property's definitive answer and it carries "今晚" straight out of the
    # query, so the ranker puts it second. The source ranked below it is the
    # assistant_visible echo — "助手说会跟踪进度" — which carries no information
    # at all. Dropping the answer to keep the echo would be strictly worse
    # memory, and the packet is kept honest by the gap naming what went, not by
    # censoring its sharpest item.
    capped_text = " ".join(item["content"] for item in capped["items"])
    assert "今晚不能恢复" in capped_text, "the decisive answer survives the cut"
    assert "助手说" not in capped_text, "the assistant echo is what gets cut"
    assert any(gap.startswith("budget_") for gap in capped["gaps"])
    assert "expandable" in capped["unmet_needs"], "the reader is told the picture is partial"

    # A budget no valid packet can fit is a caller error, not a packet.
    #
    # The tiny estimate must still reject an envelope that cannot fit;
    # do not silently exceed the cap or fall back to the old byte unit.
    with pytest.raises(ContractError) as exc:
        core.recall_packet(
            ctx,
            recall_request(query=query, mode="auto", budget_tokens=80, request_id="TEST-hotel-80"),
            deadline_seconds=5,
        )
    assert exc.value.field == "budget_tokens"

    # A budget that can hold the envelope but little else still answers, and
    # says what it had to leave out.
    tiny = core.recall_packet(
        ctx,
        recall_request(query=query, mode="auto", budget_tokens=400, request_id="TEST-hotel-400"),
        deadline_seconds=5,
    )
    assert estimate_tokens(canonical_render_json(tiny)) <= 400
    assert len(tiny["items"]) <= len(capped["items"])

    _capture(core, ctx, "TEST-hotel-delete", f"删除 {third.ref}")
    core.forget(
        ctx,
        {"protocol_version": "1.1", "target_refs": [third.ref], "mode": "delete", "expected_revisions": {third.ref: 1}},
        remaining_seconds=10,
    )
    deleted = core.recall_packet(ctx, recall_request(query=query, mode="auto", request_id="TEST-hotel-deleted"), deadline_seconds=5)
    assert "今晚不能恢复" not in " ".join(item["content"] for item in deleted["items"])

    with pytest.raises(ContractError, match="ACCESS_DENIED"):
        replace(ctx, allowed_scope_ids=frozenset({"TEST-other"}))
