"""Focused regressions for bounded selection; no model or host calls."""

import itertools
import json
import sqlite3
from dataclasses import replace

import pytest
from scope_recall.contracts import TrustedSourcePrincipal
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.background_context import background_candidates
from scope_recall.core.recall import RetrievalPipeline
from scope_recall.core.recall_packet import render_recall_packet_context
from scope_recall.core.retrieval import CandidateRef, RetrievedObject, SearchContext, SearchLimits

from tests.contract.test_claims import Clock, accept, app, capture, draft
from tests.contract.test_shared_store import shared_binding, shared_context
from tests.v11_support import recall_request, source_event


def _search(core, ctx, query):
    return SearchContext.from_request(
        recall_request(query=query), ctx, now=core.clock.utc_now(), deadline=core.clock.monotonic() + 5
    )


def test_old_relevant_preference_survives_busy_recent_window(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 输出格式 Markdown。")
    older = accept(core, ctx, draft(source, "Markdown", kind="preference", predicate="输出格式")).items[0]
    for index in range(26):
        source = capture(core, ctx, f"TEST-project 配色{index} 蓝色。", when="2026-09-03T12:00:00Z")
        accept(core, ctx, draft(source, "蓝色", kind="preference", predicate=f"配色{index}"))
    search = _search(core, ctx, "输出格式如何选择")
    with core.storage.read(ctx) as tx:
        selected = background_candidates(
            tx, search, core.recall_pipeline.storage_reader, core.clock, query_evidence=True
        )
    assert older.ref in {candidate.ref for candidate, _ in selected}
    assert len(selected) <= 2


def test_the_profile_term_query_starts_from_the_claims(app):
    """Left to itself SQLite started from every claim's evidence link and read each linked source's whole
    posting list: 1.3 s of a 2.5 s automatic recall on a shared store of 19,000 claim links and 11 million
    postings, for 2,300 preference and constraint links.  Past the hook's budget, the turn got no memory."""
    core, ctx = app
    source = capture(core, ctx, "TEST-project 输出格式 Markdown。")
    accept(core, ctx, draft(source, "Markdown", kind="preference", predicate="输出格式"))
    statements = []
    with core.storage.read(ctx) as tx:
        connection = tx._check()
        connection.set_trace_callback(statements.append)
        selected = background_candidates(
            tx,
            _search(core, ctx, "输出格式如何选择"),
            core.recall_pipeline.storage_reader,
            core.clock,
            query_evidence=True,
        )
        connection.set_trace_callback(None)
    assert selected
    term_queries = [sql for sql in statements if "COUNT(DISTINCT t.term) AS hits" in sql and "claims c" in sql]
    assert len(term_queries) == 1, statements
    with sqlite3.connect(core.storage.path) as db:
        plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + term_queries[0])]
    assert plan[0].startswith(("SCAN c", "SEARCH c")), plan


def test_matching_condition_selects_exception_instead_of_general_value(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 表达偏好 简洁。")
    general = accept(core, ctx, draft(source, "简洁", kind="preference", predicate="表达偏好")).items[0]
    source = capture(core, ctx, "只在写文案时 TEST-project 表达偏好 详细。")
    exception = accept(
        core, ctx, draft(source, "详细", kind="preference", predicate="表达偏好", conditions=["写文案时"])
    ).items[0]
    search = _search(core, ctx, "现在帮我写文案")
    with core.storage.read(ctx) as tx:
        selected = background_candidates(
            tx, search, core.recall_pipeline.storage_reader, core.clock, query_evidence=True
        )
        ordinary = background_candidates(
            tx,
            replace(search, query="现在不是写文案，只核对报价"),
            core.recall_pipeline.storage_reader,
            core.clock,
            query_evidence=True,
        )
    assert [candidate.ref for candidate, _ in selected] == [exception.ref]
    assert [candidate.ref for candidate, _ in ordinary] == [general.ref]


def _pair(ref, text, root, score=0.016):
    content = json.dumps(dict(subject="TEST-project", predicate="state", value_text=text, conditions=[]))
    obj = RetrievedObject(
        ref, 1, "claim", content, "human_direct", "current", "trusted scope", (root,), "direct_report", True
    )
    return CandidateRef("claim", ref, 1, "lexical", fusion_score=score), obj


def test_conflicting_matching_conditions_do_not_get_arbitrary_precedence(app):
    core, ctx = app
    items = []
    for condition, value in (("写文案时", "详细"), ("用中文时", "简洁")):
        source = capture(core, ctx, f"只在{condition} TEST-project 表达偏好 {value}。")
        items.append(
            accept(
                core, ctx, draft(source, value, kind="preference", predicate="表达偏好", conditions=[condition])
            ).items[0]
        )
    search = _search(core, ctx, "现在用中文写文案")
    with core.storage.read(ctx) as tx:
        selected = background_candidates(
            tx, search, core.recall_pipeline.storage_reader, core.clock, query_evidence=True
        )
    assert not {item.ref for item in items}.intersection(candidate.ref for candidate, _ in selected)


def test_ranking_covers_another_part_of_question_before_duplicate_evidence(app):
    core, ctx = app
    first = _pair("claim-a", "deployment ready", "event-a@1")
    repeat = _pair("claim-b", "deployment ready", "event-a@1")
    other = _pair("claim-c", "rollback ready", "event-c@1", score=0.015)
    search = _search(core, ctx, "deployment rollback")
    selected = core.recall_pipeline._rank_hydrated([first, repeat, other], search)
    assert [candidate.ref for candidate, _ in selected[:2]] == ["claim-a", "claim-c"]
    historical = core.recall_pipeline._rank_hydrated([first, repeat, other], replace(search, mode="history"))
    assert [candidate.ref for candidate, _ in historical] == ["claim-a", "claim-b", "claim-c"]


def test_large_middle_candidate_does_not_hide_a_small_later_candidate():
    first = _pair("claim-a", "short", "event-a@1")
    large = _pair("claim-b", "x" * 3000, "event-b@1")
    last = _pair("claim-c", "short", "event-c@1")
    kept = RetrievalPipeline(None)._apply_budget([first, large, last], SearchLimits(budget_tokens=128))
    assert [candidate.ref for candidate, _ in kept] == ["claim-a", "claim-c"]


def test_oversized_fallback_scans_only_until_enough_safe_items(app, monkeypatch):
    from scope_recall.core import recall

    core, ctx = app
    pairs = [_pair(f"claim-{i}", "x" * 65_000, f"event-{i}@1") for i in range(200)]
    original, scanned = recall.withhold_secret_items, []

    def scan(items, gaps):
        scanned.extend(items)
        return original(items, gaps)

    monkeypatch.setattr(recall, "withhold_secret_items", scan)
    kept = core.recall_pipeline._apply_budget(pairs, SearchLimits(max_items=1, budget_tokens=128))
    assert kept == pairs[:1] and len(scanned) == 1


def test_oversized_fallback_checks_deadline_between_withheld_candidates(app, monkeypatch):
    from scope_recall.core import recall

    core, ctx = app
    pairs = [_pair(f"claim-{i}", "x" * 65_000, f"event-{i}@1") for i in range(3)]
    search, scanned, gaps = _search(core, ctx, "TEST"), [], []
    expired = False

    def scan(items, gaps):
        nonlocal expired
        scanned.extend(items)
        expired = True
        return []

    monkeypatch.setattr(recall, "withhold_secret_items", scan)
    monkeypatch.setattr(core.recall_pipeline, "_remaining", lambda context: 0 if expired else 1)
    kept = core.recall_pipeline._apply_budget(pairs, SearchLimits(max_items=1, budget_tokens=128), gaps, context=search)
    assert kept == [] and len(scanned) == 1 and "deadline_exceeded" in gaps


def restatement_owner(entry, principal, memo):
    """Synthetic entry-owner attestation, separate from each source's speaker kind."""
    if entry not in {"entry-a", "entry-b", "entry-c"}:
        return None
    if principal is None:
        return "TEST-installation-owner"
    if principal.get("resolution") != "verified":
        return None
    if (principal.get("kind"), principal.get("principal_ref")) in {
        ("human", "principal:human:TEST-owner"),
        ("assistant", "principal:assistant:TEST-" + entry),
    }:
        return "TEST-installation-owner"
    return None


def restatement_assistant(context):
    """The entry's normal assistant actor, without impersonating the owner."""
    return replace(
        context,
        actor_origin="assistant_visible",
        source_principal=TrustedSourcePrincipal(
            "assistant", "verified", "principal:assistant:TEST-" + context.entry_id
        ),
    )


@pytest.fixture
def restatement_store(tmp_path):
    """Three independent sessions and entries share one isolated truth store."""
    binding = shared_binding(tmp_path / "TEST-restatement")
    ctx = replace(
        shared_context(binding),
        project_id="TEST-project",
        branch_id="TEST-main",
        source_principal=TrustedSourcePrincipal("human", "verified", "principal:human:TEST-owner"),
        owner_resolver=restatement_owner,
    )
    core = MemoryCore(CoreConfig(binding), clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    with core.storage.write(ctx) as tx:
        for entry in ("entry-a", "entry-b", "entry-c"):
            tx.registry.register_entry(entry, entry, "hermes", now=core.clock.utc_now())
    contexts = [
        replace(ctx, entry_id=entry, session_id=entry + "-session") for entry in ("entry-a", "entry-b", "entry-c")
    ]
    return core, contexts


def _restatement_packet(store, mode, original, echo, *, linked=False, fact=False):
    core, (a, b, c) = store
    source = capture(core, a, original) if original else None
    claim = accept(core, a, draft(source, "蓝色", predicate="配色")).items[0] if fact else None
    refs = [f"{(claim or source).ref}@1"] if linked and source else []
    actor = restatement_assistant(b)
    saved = core.record_event(
        actor,
        source_event(
            source_event_key="TEST-echo",
            content=echo,
            origin="assistant_visible",
            role="assistant",
            evidence_refs=refs,
            occurred_at="2026-09-02T12:00:00Z",
        ),
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    repeat = core.source(b, saved.event_refs[0].ref, 1)
    packet = core.recall_packet(c, recall_request(query="TEST-project 配色", mode=mode), deadline_seconds=10)
    return packet, source, repeat


@pytest.mark.parametrize("mode", ["auto", "current"])
@pytest.mark.parametrize("linked,fact", [(False, False), (True, False), (True, True)])
def test_original_precedes_attributed_cross_entry_restatement(restatement_store, mode, linked, fact):
    original = "TEST-project 配色使用蓝色，图标和背景统一采用同一种蓝色。"
    packet, source, repeat = _restatement_packet(
        restatement_store, mode, original, "你之前说过：" + original, linked=linked, fact=fact
    )
    assert packet["items"]
    assert packet["items"][0]["origin"] == "human_direct", packet
    repeat_item = next(item for item in packet["items"] if item["ref"] == repeat.ref)
    assert repeat_item["basis"] == "derived_summary", packet
    assert any(f"{source.ref}@1" in item["evidence_refs"] for item in packet["items"])


@pytest.mark.parametrize("mode", ["auto", "current"])
@pytest.mark.parametrize("extra", ["印刷交付另用银色专色油墨，并附上纸张色样。", "备用银色。"])
def test_restatement_with_additional_information_is_labelled(restatement_store, mode, extra):
    original = "TEST-project 配色使用蓝色，图标和背景统一采用同一种蓝色。"
    packet, source, repeat = _restatement_packet(
        restatement_store,
        mode,
        original,
        "用户偏好：" + original + extra,
        linked=True,
    )
    refs = [item["ref"] for item in packet["items"]]
    assert refs.index(source.ref) < refs.index(repeat.ref), packet
    item = next(item for item in packet["items"] if item["ref"] == repeat.ref)
    assert "restatement_of" not in item, packet
    assert item["basis"] == "derived_summary", packet
    assert item["entries"] == [{"id": "entry-b", "name": "entry-b"}], packet
    assert "银色" in item["content"]
    rendered = render_recall_packet_context(packet, installation_id="TEST-installation", session_id="TEST-render")
    assert rendered.canonical_text and '"derived_summary"' in rendered.canonical_text
    assert '"restatement_of"' not in rendered.canonical_text
    assert '"entry-b"' in rendered.canonical_text


@pytest.mark.parametrize("mode", ["auto", "current"])
def test_unrelated_assistant_information_keeps_its_rank(restatement_store, mode):
    packet, source, repeat = _restatement_packet(
        restatement_store,
        mode,
        "TEST-project 配色使用蓝色。",
        "TEST-project 配色校验器的运行命令是 palette inspect，输出保存到 TEST-samples。",
    )
    item = next(item for item in packet["items"] if item["ref"] == repeat.ref)
    assert item["basis"] == "observed"


@pytest.mark.parametrize("mode", ["auto", "current"])
def test_assistant_only_information_stays_available(restatement_store, mode):
    packet, _, repeat = _restatement_packet(restatement_store, mode, None, "TEST-project 配色使用蓝色。")
    assert repeat.ref in {item["ref"] for item in packet["items"]}, packet


@pytest.mark.parametrize("mode", ["auto", "current"])
def test_correction_precedes_restatement_of_the_old_value(restatement_store, mode):
    core, (a, b, c) = restatement_store
    original = capture(core, a, "TEST-project 配色使用蓝色，图标和背景统一采用同一种蓝色。")
    claim = accept(core, a, draft(original, "蓝色", predicate="配色")).items[0]
    saved = core.record_event(
        restatement_assistant(b),
        source_event(
            source_event_key="TEST-old-echo",
            content="你之前说过：" + original.event["content"],
            origin="assistant_visible",
            role="assistant",
            evidence_refs=[f"{claim.ref}@1"],
            occurred_at="2026-09-02T12:00:00Z",
        ),
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    correction = capture(core, a, "TEST-project 配色改为银色。", when="2026-09-03T12:00:00Z")
    assert core.current_claim(a, claim.ref).payload["value_text"] == "银色"
    packet = core.recall_packet(c, recall_request(query="TEST-project 配色", mode=mode), deadline_seconds=10)
    items = packet["items"]
    assert "银色" in items[0]["content"], packet
    assert any(f"{correction.ref}@1" in item["evidence_refs"] for item in items), packet
    old_echo = next(item for item in items if item["ref"] == saved.event_refs[0].ref)
    assert old_echo["basis"] == "derived_summary", packet


@pytest.mark.parametrize("mode", ["history", "as_of"])
def test_historical_reads_keep_the_restatement(restatement_store, mode):
    core, (a, b, c) = restatement_store
    original = capture(core, a, "TEST-project 配色使用蓝色，图标和背景统一采用同一种蓝色。")
    saved = core.record_event(
        restatement_assistant(b),
        source_event(
            source_event_key="TEST-history-echo",
            content="你之前说过：" + original.event["content"],
            origin="assistant_visible",
            role="assistant",
        ),
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    request = recall_request(query="TEST-project 配色", mode=mode)
    if mode == "as_of":
        request["as_of"] = "2026-09-06T12:00:00Z"
    packet = core.recall_packet(c, request, deadline_seconds=10)
    assert saved.event_refs[0].ref in {item["ref"] for item in packet["items"]}, packet


@pytest.mark.parametrize("mode", ["auto", "current"])
def test_size_admission_cannot_put_a_long_restatement_first(restatement_store, mode):
    original = "TEST-project 配色使用蓝色，图标和背景统一采用同一种蓝色。" * 20
    packet, source, _ = _restatement_packet(
        restatement_store, mode, original, "用户偏好：" + original + "备用银色。", linked=True
    )
    core, (_, _, c) = restatement_store
    packet = core.recall_packet(
        c, recall_request(query="TEST-project 配色", mode=mode, max_items=1), deadline_seconds=10
    )
    assert packet["items"][0]["ref"] == source.ref, packet
