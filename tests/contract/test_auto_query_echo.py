"""Automatic recall must not let an old query echo crowd out facts."""

from __future__ import annotations

import itertools
from dataclasses import replace

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core import CoreConfig, MemoryCore

from tests.contract.test_claims import Clock, capture
from tests.v11_support import context, recall_request, slowdown


@pytest.fixture
def app(tmp_path):
    ctx = replace(context(tmp_path / "TEST-p12-query-echo"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    return core, ctx


def _packet(core: MemoryCore, ctx, **changes):
    return core.recall_packet(ctx, recall_request(**changes), deadline_seconds=5)


def _contents(packet):
    return {item["content"] for item in packet["items"]}


def test_auto_query_echoes_do_not_crowd_out_real_fact_but_explicit_modes_keep_them(app):
    core, ctx = app
    # Keep lexical retrieval deterministic: all echoes are newer than the fact,
    # and the long single identifier remains one admissible query term.
    query = "P12-echo-" + ("x" * 220)
    normalized_echo = f"\u3000{query.replace('-', '－')}\u3000"
    for index in range(1, 4):
        capture(
            core,
            ctx,
            normalized_echo if index == 3 else query,
            key=f"TEST-p12/ack/{index}",
            when="2026-09-01T12:00:00Z",
        )
    fact_text = f"{query} fact: staged in vault-sim-42; backup window is 03:20."
    capture(core, ctx, fact_text, key="TEST-p12/fact", when="2026-08-01T12:00:00Z")

    auto = _packet(core, ctx, query=query, mode="auto", request_id="TEST-p12-auto")
    assert fact_text in _contents(auto), auto
    assert query not in _contents(auto)
    assert normalized_echo not in _contents(auto)

    for mode in ("current", "history"):
        explicit = _packet(
            core,
            ctx,
            query=query,
            mode=mode,
            budget_tokens=8000,
            request_id=f"TEST-p12-{mode}",
        )
        assert query in _contents(explicit), (mode, explicit)


@pytest.mark.parametrize(
    "content",
    (
        "P12q-route question: should the archive remain pending?",
        "P12q-route was not approved; keep the archive pending.",
        "P12q-route only after the vault check passes may the archive proceed.",
    ),
)
def test_auto_does_not_broadly_filter_question_negative_or_conditional_facts(app, content):
    core, ctx = app
    capture(core, ctx, content, key="TEST-p12/semantic-variant")

    packet = _packet(core, ctx, query="P12q-route", mode="auto", request_id="TEST-p12-variant")

    assert content in _contents(packet), packet


def test_auto_followup_keeps_original_query_filter(app):
    core, ctx = app
    query = "why P12-followup"
    capture(core, ctx, query, key="TEST-p12/followup-echo", when="2026-09-01T12:00:00Z")
    neutral = "P12-followup why status is pending; no cause recorded."
    capture(core, ctx, neutral, key="TEST-p12/followup-neutral", when="2026-08-01T12:00:00Z")

    packet = _packet(core, ctx, query=query, mode="auto", request_id="TEST-p12-followup")

    # The why query leaves a bounded reason need and exercises the directed
    # followup round; its internal query must not redefine the original echo.
    assert query not in _contents(packet)
    assert neutral in _contents(packet), packet


@pytest.mark.parametrize(
    ("older", "query", "same"),
    (
        ("我家窗外有什么", "我家窗外有什么？", True),
        ("我家窗外有什么？", "我家窗外有什么", True),
        ("  Is P12 done?  ", "Is P12 done?", True),
        ("继续。", "继续", True),
        ("我家窗外有什么", "我家 窗外有什么？", False),
        ("我家窗外有什么", "我家窗外有什么哇", False),
        ("我的航班改到周五早上八点了。", "我的航班改到周五早上八点了？", False),
        ("我的航班" + "改" * 130 + "了。", "我的航班" + "改" * 130 + "了？", False),
        ("你说的是这个？我同意。", "你说的是这个？我同意？", False),
        ("切到分支 Release-2", "切到分支 release-2", False),
        ("继续", "继续?", False),
        ("？", "？", True),
        ("？", "!", False),
    ),
)
def test_an_older_copy_is_the_same_words_whatever_closes_them(older, query, same):
    """Only the closing marks and surrounding spaces may differ, and the two must both end asking or both not: a
    statement asked back as a question is not a copy of it, however long.  The words, their letter
    case included, may not differ."""
    from scope_recall.core.recall_policy import same_message

    assert same_message(older, query) is same


def test_closing_marks_are_read_from_the_end_once():
    """A long run of spaces or marks inside a message costs one pass, not a pattern anchored at the end that tries it
    again from every position of the run, which takes seconds for one candidate: four times the run takes about four
    times as long (``slowdown``)."""
    from scope_recall.core.recall_policy import same_message

    def compared(text):
        return same_message(text, text + "。"), same_message(text, "P12")

    text = "P12" + " ." * 40_000 + "x"
    assert compared(text) == (True, False)
    assert slowdown(compared, "P12" + " ." * 10_000 + "x", text) < 8


# -- what the asking model still holds ----------------------------------------------------------------------------
# A host that keeps the whole session in its model's context (Claude Code until it compacts) says since when; an
# automatic recall then leaves out what stands only on this session's turns since that moment, and other memories
# take the slots.


def _session_facts(core, ctx):
    """This session's fact before and after the moment, and another session's after it."""
    before = capture(core, ctx, "P249 构建机是 QX-17。", key="TEST-p249/before", when="2026-09-01T10:00:00Z")
    after = capture(core, ctx, "P249 部署机是 KZ-42。", key="TEST-p249/after", when="2026-09-01T12:00:00Z")
    other = replace(ctx, session_id="TEST-other-session")
    elsewhere = capture(core, other, "P249 备份机是 VX-9。", key="TEST-p249/elsewhere", when="2026-09-01T12:30:00Z")
    return before, after, elsewhere


def test_automatic_recall_leaves_out_what_the_model_still_holds(app):
    core, ctx = app
    before, after, elsewhere = _session_facts(core, ctx)
    query = "P249 QX-17 KZ-42 VX-9 都是哪台机器？"
    said = {source.event["content"] for source in (before, after, elsewhere)}

    unknown = core.recall_packet(ctx, recall_request(query=query, request_id="TEST-p249-0"), deadline_seconds=5)
    assert _contents(unknown) >= said, "without the host's word every one is recalled, as before"
    compacted = core.recall_packet(
        ctx,
        recall_request(query=query, request_id="TEST-p249-1"),
        deadline_seconds=5,
        in_context_since="2026-09-01T11:00:00+00:00",
    )
    assert _contents(compacted) >= said - {after.event["content"]}, compacted
    assert after.event["content"] not in _contents(compacted), "said since the compaction: the model has it"
    whole = core.recall_packet(
        ctx,
        recall_request(query=query, request_id="TEST-p249-2"),
        deadline_seconds=5,
        in_context_since="2026-09-01T09:00:00Z",
    )
    assert elsewhere.event["content"] in _contents(whole), "another session's is never the model's"
    assert not _contents(whole) & {before.event["content"], after.event["content"]}, whole
    # Asked for, it is answered: only automatic recall leaves anything out.
    for mode in ("current", "history"):
        explicit = core.recall_packet(
            ctx,
            recall_request(query=query, mode=mode, budget_tokens=8000, request_id=f"TEST-p249-{mode}"),
            deadline_seconds=5,
            in_context_since="2026-09-01T09:00:00Z",
        )
        assert after.event["content"] in _contents(explicit), (mode, explicit)


def test_an_item_is_left_out_only_when_all_it_stands_on_is_held(app):
    """A claim or an episode stands on several messages: one of another session, from before the moment, or that cannot
    be read keeps it in.  An item without evidence is kept too."""
    from scope_recall.core.recall import _in_context
    from scope_recall.core.retrieval import RetrievedObject, SearchContext

    core, ctx = app
    before, after, elsewhere = _session_facts(core, ctx)
    search = SearchContext.from_request(
        recall_request(query="P249"),
        ctx,
        now="2026-09-06T12:00:00Z",
        deadline=10**9,
        in_context_since="2026-09-01T11:00:00Z",
    )

    def item(*sources, refs=()):
        evidence = tuple(f"{source.ref}@{source.revision}" for source in sources) + tuple(refs)
        return RetrievedObject("TEST-item", 1, "claim", "P249", "human_direct", "current", "", evidence, "x", True)

    with core.storage.read(ctx, remaining_seconds=5) as tx:
        assert _in_context(tx, item(after), search)
        assert not _in_context(tx, item(after, elsewhere), search), "one of another session"
        assert not _in_context(tx, item(after, before), search), "one from before the compaction"
        assert not _in_context(tx, item(after, refs=("TEST-missing@1",)), search), "one that cannot be read"
        assert not _in_context(tx, item(after, refs=("not a ref",)), search)
        assert not _in_context(tx, item(), search), "no evidence"
        assert not _in_context(tx, item(after), replace(search, in_context_since=None)), "the host said nothing"
    with pytest.raises(ContractError):
        replace(search, in_context_since="yesterday")


def test_what_the_model_holds_still_leads_to_what_is_related_to_it(app):
    """Left out at admission, a message the model holds stopped leading to what is related to it: on the replay of
    361 prompts, claims and procedures drawn partly from before the session's last compaction.  It is set aside,
    not delivered, and still a seed; one reached from it that the model holds leads on in turn and is not delivered
    either."""
    from scope_recall.core.recall import RetrievalPipeline, _Aside
    from scope_recall.core.retrieval import CandidateRef, RetrievedObject, SearchContext

    core, ctx = app
    said = {
        "event-held": (ctx.session_id, "2026-09-01T12:00:00Z"),
        "event-old": (ctx.session_id, "2026-09-01T10:00:00Z"),
        "event-other": ("TEST-other-session", "2026-09-01T12:30:00Z"),
    }
    stands_on = {"event-held": "event-held", "claim-held": "event-held", "claim-old": "event-old"}
    stands_on["claim-other"] = "event-other"

    class Source:
        def __init__(self, ref):
            self.session_id, moment = said[ref]
            self.event = {"occurred_at": moment}

    class Tx:
        def source(self, ref, revision):
            return Source(ref) if ref in said else None

    class Reader:
        links = {"event-held": ["claim-old", "claim-held"], "claim-held": ["claim-other"]}

        def hydrate(self, tx, candidate, context):
            kind = "event" if candidate.ref.startswith("event") else "claim"
            evidence = (f"{stands_on[candidate.ref]}@1",)
            content = f"P249 链：{candidate.ref}"
            return RetrievedObject(candidate.ref, 1, kind, content, "human_direct", "current", "", evidence, "x", True)

        def related(self, tx, candidate, *, limit):
            return tuple(
                CandidateRef("claim", ref, 1, "relation", rank=index)
                for index, ref in enumerate(self.links.get(candidate.ref, ()), 1)
            )

    pipeline = RetrievalPipeline(None, storage_reader=Reader())
    search = SearchContext.from_request(
        recall_request(query="P249 链"),
        ctx,
        now="2026-09-06T12:00:00Z",
        deadline=10**9,
        in_context_since="2026-09-01T11:00:00Z",
    )
    aside = _Aside()
    hydrated: list = []
    seed = CandidateRef("event", "event-held", 1, "lexical", fusion_score=0.9)
    pipeline._hydrate_all(Tx(), hydrated, (seed,), search, [], floor=6, aside=aside)
    assert hydrated == [] and aside.held == [seed], "held, not delivered"
    pipeline._hydrate_related(Tx(), search, hydrated, [], held=tuple(aside.held))
    assert sorted(candidate.ref for candidate, _obj in hydrated) == ["claim-old", "claim-other"]


def test_hits_too_large_for_any_packet_leave_the_owner_s_background_room(tmp_path):
    """An automatic recall whose every hit is larger than any packet would keep six of them, the compiler would refuse
    each, and the packet would come back empty, cutting the owner's background with them.  One stays as the hint
    that it can be expanded.  Leaving out what the model holds makes this likelier."""
    from tests.contract.test_autonomous_context import packet, preference

    ctx = replace(context(tmp_path / "TEST-p249-oversized"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    item, _source = preference(core, ctx)
    for index in range(7):
        capture(core, ctx, f"火星咖啡采购报价 第{index}份：" + "价" * 9000, key=f"TEST-p249/huge/{index}")
    result = packet(core, ctx, request_id="TEST-p249-oversized")
    assert [entry["ref"] for entry in result["items"]] == [item.ref], result["gaps"]
    assert any(need.startswith("expandable") for need in result["unmet_needs"]), "the hint that it can be expanded"


def test_the_owner_s_background_the_model_holds_is_left_out_too(tmp_path):
    """A standing preference the owner stated in this session since its last compaction is in the model's context
    already; background is not admitted like a hit, so it is left out too, not delivered again."""
    from tests.contract.test_autonomous_context import packet, preference

    ctx = replace(context(tmp_path / "TEST-p249-background"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    item, source = preference(core, ctx)  # said at 2026-09-01T12:00:00Z, in this session

    def refs(**changes):
        return [entry["ref"] for entry in packet(core, ctx, **changes)["items"]]

    assert item.ref in refs(request_id="TEST-bg-0")
    held = core.recall_packet(
        ctx,
        recall_request(query="火星咖啡采购报价是什么", request_id="TEST-bg-1"),
        in_context_since="2026-09-01T11:00:00Z",
    )
    assert item.ref not in [entry["ref"] for entry in held["items"]]
    later = core.recall_packet(
        ctx,
        recall_request(query="火星咖啡采购报价是什么", request_id="TEST-bg-2"),
        in_context_since="2026-09-01T13:00:00Z",
    )
    assert item.ref in [entry["ref"] for entry in later["items"]], "said before the compaction"
    other = replace(ctx, session_id="TEST-other-session")
    elsewhere = core.recall_packet(
        other,
        recall_request(query="火星咖啡采购报价是什么", request_id="TEST-bg-3"),
        in_context_since="2026-09-01T11:00:00Z",
    )
    assert item.ref in [entry["ref"] for entry in elsewhere["items"]], "another session's model does not hold it"
