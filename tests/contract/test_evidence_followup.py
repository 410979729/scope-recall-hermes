"""P08 evidence qualification, directed follow-up, and collection as_of contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

import pytest
from scope_recall.contracts import ImportProvenance, import_source_fingerprint
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.claims import Qualification
from scope_recall.core.events import lexical_terms
from scope_recall.core.recall_needs import Answers, answering, asks_to_resume, unmet_needs
from scope_recall.core.recall_policy import (
    SPACE_ID,
    RecallPolicy,
    meaningful_query_terms,
    query_is_specific,
)
from scope_recall.core.retrieval import CandidateRef, CollectionQuery, RetrievedObject, SearchContext, SearchLimits
from scope_recall.core.retrieval_storage import RetrievalStorage, scope_digest
from scope_recall.core.source_qualification import in_pasted_text, unquoted

from tests.contract.test_candidate_lifecycle import Evaluator, _finish_source_work
from tests.contract.test_claims import Clock, capture, draft
from tests.v11_support import context, recall_request, source_event


@dataclass
class FollowupDeadlineClock:
    now = "2026-09-06T12:00:00Z"
    _mono: float = 100.0

    def utc_now(self) -> str:
        return self.now

    def monotonic(self) -> float:
        return self._mono

    def expire(self) -> None:
        self._mono += 0.2


@dataclass
class RoundTrackingVectors:
    candidates: tuple[CandidateRef, ...] = ()
    calls: list[dict[str, Any]] | None = None
    clock: FollowupDeadlineClock | None = None
    expire_after_first_call: bool = False

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    def search(self, context: SearchContext, *, limit: int, remaining_seconds: float) -> tuple[CandidateRef, ...]:
        self.calls.append({"query": context.query, "limit": limit, "remaining_seconds": remaining_seconds})
        if self.expire_after_first_call and len(self.calls) == 1 and self.clock is not None:
            self.clock.expire()
        return self.candidates[:limit]


@dataclass
class DirectedFollowupVectors(RoundTrackingVectors):
    h100: Any = None
    h200: Any = None

    def search(self, context: SearchContext, *, limit: int, remaining_seconds: float) -> tuple[CandidateRef, ...]:
        self.calls.append({"query": context.query, "limit": limit, "remaining_seconds": remaining_seconds})
        if len(self.calls) == 1:
            # Rejected-by-dedup work still consumes the first-round vector
            # budget; one reserved slot must remain for the directed query.
            return tuple(_vector(self.h100) for _ in range(min(limit, 47)))
        assert context.query == "h200"
        return (_vector(self.h200),)[:limit]


class LexicalBlockedStorage(RetrievalStorage):
    """Keep SQLite hydration real while forcing the missing comparison side through vector."""

    def lexical(self, tx, context: SearchContext, *, limit: int) -> tuple[CandidateRef, ...]:
        return ()

    def recent(self, tx, context: SearchContext, *, limit: int) -> tuple[CandidateRef, ...]:
        return ()


@dataclass
class CollectionTickClock:
    now = "2026-09-06T12:00:00Z"
    _mono: float = 100.0
    tick: float = 0.1

    def utc_now(self) -> str:
        return self.now

    def monotonic(self) -> float:
        self._mono += self.tick
        return self._mono


class DeadlineAfterExactStorage(RetrievalStorage):
    def __init__(self, source, clock):
        super().__init__(clock=clock)
        self.source = source

    def exact(self, tx, context: SearchContext, *, limit: int) -> tuple[CandidateRef, ...]:
        return (CandidateRef("event", self.source.ref, self.source.revision, "exact_ref"),)

    def lexical(self, tx, context: SearchContext, *, limit: int) -> tuple[CandidateRef, ...]:
        self.clock._mono += 1.0
        return ()

    def recent(self, tx, context: SearchContext, *, limit: int) -> tuple[CandidateRef, ...]:
        return ()


@pytest.fixture
def app(tmp_path):
    ctx = replace(context(tmp_path / "TEST-p08-followup"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    return core, ctx


def _recall(core: MemoryCore, ctx, **changes):
    request = recall_request(**changes)
    return core.recall(ctx, request, deadline_seconds=changes.get("deadline_seconds", 5))


def test_comparison_single_source_explicitly_covers_both_objects(app):
    core, ctx = app
    shared = capture(
        core, ctx, "H100 与 H200 共用同一套部署配置。", key="TEST-p08/shared-root", when="2026-09-05T12:00:00Z"
    )
    result = _recall(core, ctx, query="比较 H100 和 H200 部署配置", mode="history")
    assert "comparison_second_side" not in result.unmet_needs
    assert result.answerability_hint == "supported"
    assert len(result.items) == 1
    assert shared.ref in {item.ref for item in result.items}


def test_comparison_same_side_event_duplicates_stay_partial(app):
    core, ctx = app
    capture(core, ctx, "H100 使用第一套部署配置。", key="TEST-p08/same-side-one", when="2026-09-05T12:00:00Z")
    capture(core, ctx, "H100 仍使用第一套部署配置。", key="TEST-p08/same-side-two", when="2026-09-05T12:01:00Z")
    result = _recall(core, ctx, query="比较 H100 和 H200 部署配置", mode="history")
    assert "comparison_second_side" in result.unmet_needs
    assert result.answerability_hint == "partial"


def test_comparison_counts_a_suffixed_version_and_its_suffix_as_one_side():
    """A release named 3.1.0rc28 also yields the identifier rc28.

    Counted as two sides, a report naming 3.1.0rc28 and a note naming rc28
    would pass for both sides of 3.1.0rc28 versus 3.1.0rc29.
    """

    def event(ref: str, content: str) -> RetrievedObject:
        return RetrievedObject(
            ref, 1, "event", content, "human_direct", "current", "trusted scope", (f"{ref}@1",), "direct_report", True
        )

    note = event("event-a-note", "rc28 安装后网关正常。")
    report = event("event-b-report", "hermes-scope-recall 3.1.0rc28 已安装。")
    dashed = event("event-b-dashed", "网关运行 v3.1.0-rc28。")
    other = event("event-c-other", "hermes-scope-recall 3.1.0rc29 已安装。")
    assert "comparison_second_side" in unmet_needs("比较 3.1.0rc28 和 3.1.0rc29", (report, note))
    assert "comparison_second_side" in unmet_needs("比较 v3.1.0-rc28 和 v3.1.0-rc29", (dashed, note))
    assert "comparison_second_side" not in unmet_needs("比较 3.1.0rc28 和 3.1.0rc29", (report, other))
    # Named by suffix alone, a release is one side whichever form its source uses.
    assert "comparison_second_side" in unmet_needs("比较 rc28 和 rc29", (report, note))
    assert "comparison_second_side" not in unmet_needs("比较 rc28 和 rc29", (dashed, other))


def test_directed_followup_retrieves_second_object_and_stops_at_two_rounds(tmp_path):
    ctx = replace(context(tmp_path / "TEST-p08-followup-rounds"), project_id="TEST-project", branch_id="TEST-main")
    vectors = DirectedFollowupVectors()
    core = MemoryCore(
        CoreConfig(ctx.binding),
        clock=Clock(),
        vectors=vectors,
        retrieval_policy=RecallPolicy(vector_threshold=0.8),
    )
    core.recall_pipeline.storage_reader = LexicalBlockedStorage()
    core.initialize()
    vectors.h100 = capture(core, ctx, "H100 使用第一套部署配置。", key="TEST-p08/f1", when="2026-09-01T12:00:00Z")
    vectors.h200 = capture(core, ctx, "H200 使用第二套部署配置。", key="TEST-p08/f2", when="2026-09-05T12:00:00Z")
    result = core.recall(
        ctx,
        recall_request(query="比较 H100 和 H200 部署配置", mode="history"),
        deadline_seconds=5,
    )
    refs = {item.ref for item in result.items}
    assert vectors.h100.ref in refs
    assert vectors.h200.ref in refs
    assert "comparison_second_side" not in result.unmet_needs
    assert len(vectors.calls) == 2
    assert [call["limit"] for call in vectors.calls] == [47, 1]
    assert vectors.calls[0]["query"] == "比较 H100 和 H200 部署配置"
    assert vectors.calls[1]["query"] == "h200"


def _vector(source, *, score: float = 0.95) -> CandidateRef:
    return CandidateRef(
        "event",
        source.ref,
        source.revision,
        "vector",
        vector_score=score,
        vector_id=f"TEST-vector:{source.ref}@{source.revision}",
        embedding_space=SPACE_ID,
    )


def test_why_without_reason_stays_partial_but_explicit_reason_is_supported(app):
    core, ctx = app
    topic = capture(core, ctx, "TEST-project 采用 H100 方案。", key="TEST-p08/why-topic", when="2026-09-05T12:00:00Z")
    partial = _recall(core, ctx, query="为什么 TEST-project 采用 H100 方案？", mode="history")
    assert "reason_evidence" in partial.unmet_needs
    assert partial.answerability_hint == "partial"

    reason = capture(
        core,
        ctx,
        "因为成本更低，TEST-project 采用 H100 方案。",
        key="TEST-p08/why-reason",
        when="2026-09-05T12:01:00Z",
    )
    supported = _recall(core, ctx, query="为什么 TEST-project 采用 H100 方案？", mode="history")
    contents = {item.content for item in supported.items}
    assert reason.event["content"] in contents or topic.event["content"] in contents
    assert "reason_evidence" not in supported.unmet_needs
    assert supported.answerability_hint == "supported"


@pytest.mark.parametrize(
    "content, query",
    [
        ("H100更换配色的原因未知；H200因为预算不足取消。", "为什么 H100 更换配色？"),
        ("没有证据表明因为颜色而取消 H100。", "为什么 H100 被取消？"),
        ("H100更换配色的原因经过多次核对仍然未知，记录明确保留为未验证状态。", "为什么 H100 更换配色？"),
    ],
)
def test_unrelated_or_negative_reason_does_not_support_why(app, content, query):
    core, ctx = app
    capture(core, ctx, content, key="TEST-p08/why-negative", when="2026-09-05T12:00:00Z")
    result = _recall(core, ctx, query=query, mode="history")
    assert "reason_evidence" in result.unmet_needs
    assert result.answerability_hint == "partial"


def _recalled(
    ref: str,
    content: str,
    *,
    kind: str = "event",
    origin: str = "human_direct",
    state: str = "",
    reason: str = "",
    judgement: str = "",
) -> RetrievedObject:
    pairs = (("state", state), ("qualification_reason", reason), ("judgement", judgement))
    metadata = tuple((key, value) for key, value in pairs if value)
    return RetrievedObject(
        ref,
        1,
        kind,
        content,
        origin,
        "current",
        "trusted scope",
        (f"{ref}@1",),
        "direct_report",
        True,
        metadata=metadata,
    )


def test_an_item_answers_by_naming_what_the_question_names_and_saying_more_of_what_it_asks():
    """Each name a question gives must stand in an item that also says another of its words, in any inflection of
    that word; a question's first word, I, a month, a weekday and a word in capitals name nothing.  A question that
    names nothing is not judged."""
    fact = json.dumps({"kind": "fact", "subject": "Fennel", "predicate": "alert channel", "value_text": "#ops-pager"})
    active = _recalled("claim-fennel", fact, kind="claim", state="active")
    other = _recalled("event-maple", "The Maple alert channel is #night-shift.")
    assert answering("Where are the Fennel alerts posted?", (other, active)) == Answers(True, (active,))
    keys = _recalled("event-keys", "We rotated the Orchard API keys on Monday.")
    assert answering("What did I say about the Orchard API keys on Sep 3?", (keys,)).items == (keys,)
    assert answering("What did I say about the Lantern API keys on Sep 3?", (keys,)) == Answers(True, ())
    assert answering("What's the plan for the Orchard keys?", (keys,)).items == (keys,)
    assets = _recalled("event-assets", "Put the Flint assets behind Akamai for a week.")
    assert answering("What CDNs did we test with Flint's assets?", (assets,)).items == (assets,)
    # A sentence that supposes, and words in quotation marks, are left out; the rest of the message still answers.
    port = "What is the staging database port for Orchard?"
    said = _recalled("event-said", "The Orchard staging database port is 6543. If we split the cluster, it would move.")
    supposed = _recalled("event-supposed", "If we split the cluster, the Orchard staging database port would move.")
    assert answering(port, (said, supposed)).items == (said,)
    labelled = _recalled("event-labelled", "旧架构图上把 Cinder 标成“部署区域 eu-central-1”，但那张图是迁移之前画的。")
    assert answering("Cinder 的部署区域是哪里？", (labelled,)) == Answers(True, ())
    sides = (_recalled("event-h100", "H100 使用第一套部署配置。"), _recalled("event-h200", "H200 使用第二套部署配置。"))
    assert answering("比较 H100 和 H200 部署配置", sides) == Answers(True, sides)
    assert answering("比较 H100 和 H300 部署配置", sides) == Answers(True, ())
    assert answering("我下午喜欢喝什么？", (other,)) == Answers(False, (other,))
    assert answering("what port does redis use?", (said,)) == Answers(False, (said,))


def test_a_question_that_asks_nothing_past_its_names_is_answered_by_any_item_naming_them():
    """A question that asks nothing past its names ("What is Orchard?") asks for whatever is said about them: an item
    that names them answers, whatever else it says.  Holding it to another of the question's words would only meet
    such a question with a false unknown."""
    said = _recalled("event-orchard", "Orchard is the billing service the shop runs on.")
    assert answering("What is Orchard?", (said,)) == Answers(True, (said,))
    assert answering("Orchard 是什么？", (said,)) == Answers(True, (said,))
    assert answering("What is Lantern?", (said,)) == Answers(True, ())


def test_a_request_to_be_told_about_a_named_thing_is_answered_by_any_item_naming_it():
    """A request to be told about something asks for whatever is said about it, as "What is Orchard?" does: the words
    that frame it (tell me, describe, any updates, 介绍一下, 说说, 怎么样, 的情况) ask nothing an answer has to say.
    Asked for Orchard's port inside such a frame, an item answers only by saying port."""
    said = _recalled("event-orchard", "Orchard is the billing service the shop runs on.")
    zh = _recalled("event-orchard-zh", "Orchard 是商店用的计费服务。")
    for request in (
        "Tell me about Orchard",
        "What do you know about Orchard?",
        "Describe Orchard",
        "Explain Orchard to me",
        "Anything on Orchard?",
        "Any updates on Orchard?",
        "Orchard 怎么样？",
        "介绍一下 Orchard",
        "说说 Orchard",
        "讲讲 Orchard 的情况",
        "关于 Orchard 你知道什么？",
    ):
        assert answering(request, (said, zh)) == Answers(True, (said, zh)), request
    assert answering("Tell me about Lantern", (said, zh)) == Answers(True, ())
    port = _recalled("event-port", "Orchard's port is 8443.")
    port_zh = _recalled("event-port-zh", "Orchard 的端口是 8443。")
    for request, answer in (("What do you know about Orchard's port?", port), ("介绍一下 Orchard 的端口", port_zh)):
        assert answering(request, (said, zh)) == Answers(True, ()), request
        assert answering(request, (said, zh, port, port_zh)) == Answers(True, (answer,)), request


def test_a_quoted_clause_is_someone_s_words_and_a_quoted_term_the_writer_s_own():
    """Between quotation marks of any kind, a clause is someone's words, read out as a whole: with an apostrophe in it
    (users'), with a quotation nested in it, single or double, straight or curly.  A single term, a name or a value in
    quotation marks is the writer's own: the marks set off what it names, and their statement still answers.  An
    apostrophe inside a word, after a plural's s or before a decade opens no quotation."""
    question = "What port does Orchard run on?"
    for quoted in (
        "The runbook says 'Orchard runs on port 8443'.",
        "The wiki still has ‘Orchard runs on port 8443’ on its front page.",
        "('Orchard runs on port 8443') was the old note.",
        "The old deck says ‘Orchard's users' port is 8443’, but nobody checked.",
        "The old deck says ‘Orchard runs on ‘production’ port 8443’, but nobody checked.",
        "The old deck says ‘Orchard runs on ‘operations’ port 8443’, but nobody checked.",
        "The old deck says \"Orchard runs on 'production' port 8443\", but nobody checked.",
        "The old deck says ‘Orchard Runs On Port 8443’.",
        "The old deck says 'Orchard uses its users’ port 8443'.",
        'The old deck says "Orchard runs on\nport 8443".',
    ):
        assert answering(question, (_recalled("event-quoted", quoted),)) == Answers(True, ()), quoted
    for said in (
        "Orchard's service runs on port 8443.",
        "We don't run Orchard on 8080; it runs on port 8443.",
        "In the '90s we ran Orchard on port 80.",
        "We set the flag to 'on', and Orchard runs on port 8443 now.",
        "The service key is 'Orchard'; it runs on port 8443.",
        'We call it "Orchard"; it runs on port 8443.',
    ):
        item = _recalled("event-said", said)
        assert answering(question, (item,)) == Answers(True, (item,)), said
    named = _recalled("event-named", "我们的项目叫“Orchard”，端口 8443。")
    assert answering("Orchard 的端口是多少？", (named,)) == Answers(True, (named,))
    blue = _recalled("event-blue", 'We call it "Blue Ocean"; its port is 8443.')
    assert answering("What port does Blue Ocean use?", (blue,)) == Answers(True, (blue,))
    assert unquoted("It's Orchard's 'new' port, the '90s one.") == "It's Orchard's 'new' port, the '90s one."
    assert unquoted("Ops wrote “部署区域 eu-central-1” on it.") == "Ops wrote   on it."
    assert unquoted("‘我不吃辣’") == " "
    assert unquoted("'Orchard runs'") == " "
    for term in ("is", "Orchard", "Blue Ocean", "8443", "host:8443", "v3.1", "eu-west-1"):
        assert unquoted(f'We call it "{term}".') == f'We call it "{term}".'


@pytest.mark.parametrize(
    ("question", "quoted", "own"),
    (
        ("When does Orchard ship?", "Alice said 'Orchard Ships Friday'.", "Orchard ships Friday."),
        ("When does Orchard ship?", "The notice says ‘Orchard Ships Friday’.", "Orchard ships Friday."),
        ("When does Orchard ship?", "The note wrote: ‘Orchard Ships Friday’.", "Orchard ships Friday."),
        ("When does Orchard ship?", "Old notice: ‘Orchard Ships Friday’.", "Orchard ships Friday."),
        ("When does Orchard ship?", "‘Orchard Ships Friday’", "Orchard ships Friday."),
        ("Orchard 什么时候发布？", "Orchard 的旧公告写着：“周五发布”。", "Orchard 周五发布。"),
        ("Orchard 什么时候发布？", "Orchard 的通知是“周五发布”", "Orchard 周五发布。"),
        ("Orchard 什么时候发布？", "Orchard 的旧说明说“周五发布”", "Orchard 周五发布。"),
        ("Orchard 什么时候发布？", "Orchard 的旧说明表示“周五发布”", "Orchard 周五发布。"),
        ("Orchard 什么时候发布？", "Orchard 的旧说明称“周五发布”", "Orchard 周五发布。"),
        ("Orchard 什么时候发布？", "Orchard：“周五发布”", "Orchard 周五发布。"),
    ),
)
def test_reported_clauses_answer_nothing_and_direct_statements_answer(question, quoted, own):
    assert answering(question, (_recalled("event-quoted", quoted),)) == Answers(True, ())
    direct = _recalled("event-own", own)
    assert answering(question, (direct,)) == Answers(True, (direct,))


@pytest.mark.parametrize("term", ("Blue Cypress", "New York City", "果园计划", "Orchard", "8443", "host:8443"))
def test_quoted_names_identifiers_and_values_stay_in_the_writers_statement(term):
    text = f'We call it "{term}".'
    assert unquoted(text) == text
    labelled = f'The "{term}" service has port 8443.'
    assert unquoted(labelled) == labelled


def test_a_capitalised_name_ending_in_s_closes_before_the_writers_sentence():
    """A quoted name that ends in s closes on its own mark.  A later plural (the users' guide) is not that closer."""
    text = "The view from 'The Alps' is nice. I prefer blue. See the users' guide."
    assert "I prefer blue" in unquoted(text)
    assert not in_pasted_text(text, "I prefer blue")


def test_a_character_inside_a_name_does_not_introduce_a_quotation():
    """称 introduces a quotation only as its own verb.  The 称 of 名称 leaves the name in the writer's statement."""
    text = "服务名称是“果园计划”"
    assert unquoted(text) == text


@pytest.mark.parametrize("term", ("Orchard", "8443", "host:8443", "v3.1", "eu-west-1"))
def test_a_source_introducing_a_single_identifier_or_value_does_not_make_it_a_clause(term):
    text = f'Alice said "{term}".'
    assert unquoted(text) == text


@pytest.mark.parametrize(
    ("query", "requested"),
    (
        ("继续努力", False),
        ("继续观察 Orchard", False),
        ("继续观察迁移", False),
        ("Do not continue the Orchard migration", False),
        ("Don't resume the Orchard migration", False),
        ("We will continue the Orchard migration", False),
        ("I can resume the Orchard migration", False),
        ("我们继续做 Orchard 迁移", False),
        ("不要继续做 Orchard 迁移", False),
        ("不能恢复 Orchard 的迁移", False),
        ("Can we not pick up the Orchard migration?", False),
        ("Can we continue using Orchard?", False),
        ("continue using Orchard", False),
        ("Continue Using Orchard", False),
        ("Continue the Orchard migration", True),
        ("Let's continue Orchard", True),
        ("Can we pick up Orchard?", True),
        ("Could you resume the Orchard migration?", True),
        ("Please continue with Orchard", True),
        ("Where were we on Orchard?", True),
        ("继续做 Orchard 迁移", True),
        ("继续搞 Orchard 迁移", True),
        ("接着做 Orchard 迁移", True),
        ("请恢复 Orchard 的迁移", True),
        ("我们可以继续做 Orchard 迁移吗？", True),
        ("继续 H100", True),
        ("继续 TEST 项目", True),
        ("继续当前任务", True),
        ("Continue H100", True),
        ("好的，继续做 Orchard 迁移", True),
        ("OK, continue the Orchard migration", True),
        ("请你继续做 Orchard 迁移", True),
        ("So where were we?", True),
        ("继续吧，把迁移做完", True),
        ("继续上次", True),
        ("恢复上次", True),
    ),
)
def test_resume_requires_a_request_and_keeps_other_query_paths(query, requested):
    assert asks_to_resume(query) is requested
    assert ("resume_state" in unmet_needs(query, ())) is requested


def test_only_a_grounded_task_s_resume_answers_and_only_a_question_that_asks_to_resume():
    """An episode's resume is a summary drawn from messages that answer for themselves: asked a fact, it answers
    nothing; asked to resume, a grounded task's resume answers by naming the task, in its own words.  An artifact's
    label and a reference answer nothing."""
    refs = ["event-moving@1"]
    resume = {
        "goal": {"text": "Moving Orchard to the new cluster", "evidence_refs": refs},
        "open_items": [{"text": "Orchard port is 8443 until the cache moves", "evidence_refs": refs}],
    }
    grounded = _recalled("episode-orchard", json.dumps(resume), kind="episode", origin="derived_summary")
    assert answering("What is Orchard port?", (grounded,)) == Answers(True, ())
    assert answering("Continue the Orchard migration", (grounded,)) == Answers(True, (grounded,))
    assert answering("继续 Orchard 的迁移", (grounded,)) == Answers(True, (grounded,))
    assert answering("Continue the Lantern migration", (grounded,)) == Answers(True, ())
    for practice in ("Should we continue using Orchard?", "继续用 Orchard 吗？", "Should we discontinue Orchard?"):
        assert answering(practice, (grounded,)) == Answers(True, ()), practice
    ungrounded = _recalled(
        "episode-goal", json.dumps({"goal": resume["goal"]}), kind="episode", origin="derived_summary"
    )
    assert answering("Continue the Orchard migration", (ungrounded,)) == Answers(True, ())
    artifact = _recalled("artifact-orchard", "Orchard port diagram", kind="artifact", origin="observed")
    binding = {"mention": "the Orchard port diagram", "resolution": "resolved", "resolved_ref": "artifact-orchard@1"}
    reference = _recalled("reference-orchard", json.dumps(binding), kind="reference", origin="derived_summary")
    for item in (artifact, reference):
        assert answering("What is Orchard port?", (item,)) == Answers(True, ()), item.kind


def test_only_a_request_to_resume_a_task_reads_as_one():
    """Resuming a task is asked in so many words: continue, resume or pick up the task, where were we, 继续 what is to
    go on, 接着做, 恢复上次的.  Continuing a practice asks whether to keep it (continue using Jenkins, 继续用 Jenkins
    吗), and discontinue or restoring a database resumes nothing.  The resume need reads a question as what answers it
    does."""
    for request in (
        "Continue the Orchard migration",
        "Resume the Orchard migration",
        "Pick up the Orchard migration",
        "Where were we on Orchard?",
        "continue",
        "继续",
        "继续 Orchard 的迁移",
        "继续 H100",
        "继续 TEST 项目",
        "继续当前任务",
        "继续上次的工作",
        "接着做",
        "恢复上次的任务",
        "恢复 Orchard 的迁移",
    ):
        assert asks_to_resume(request), request
    for other in (
        "Should we continue using Jenkins?",
        "继续用 Jenkins 吗？",
        "继续使用 Jenkins 吗",
        "Should we discontinue Jenkins?",
        "怎么恢复数据库？",
        "Will the outage continue?",
    ):
        assert not asks_to_resume(other), other
    assert "resume_state" in unmet_needs("继续", ())
    assert "resume_state" not in unmet_needs("继续用 Jenkins 吗？", ())
    assert "resume_state" not in unmet_needs("Should we continue using Jenkins?", ())


@pytest.mark.parametrize(
    ("question", "text"),
    (("show me the summary", "Summary: the deployment ended at noon."), ("说说你的进展", "进展：迁移已完成。")),
)
def test_request_framing_words_still_find_lexical_candidates(app, question, text):
    core, ctx = app
    said = capture(core, ctx, text, key="TEST-framing/message")
    for mode in ("auto", "current"):
        search = SearchContext.from_request(
            recall_request(query=question, mode=mode), ctx, now=core.clock.utc_now(), deadline=100
        )
        with core.storage.read(ctx) as tx:
            found = RetrievalStorage(clock=core.clock).lexical(tx, search, limit=10)
        assert said.ref in {candidate.ref for candidate in found}


@pytest.mark.parametrize(
    ("question", "resume"),
    (
        ("Continue the Jenkins migration", True),
        ("Let's continue Jenkins", True),
        ("Can we pick up Jenkins?", True),
        ("接着做 Jenkins 的迁移", True),
        ("继续搞 Jenkins 迁移", True),
        ("Should we continue using Jenkins?", False),
        ("继续用 Jenkins 吗？", False),
        ("继续努力", False),
        ("Do not continue the Jenkins migration", False),
        ("We will continue the Jenkins migration", False),
        ("我们继续做 Jenkins 迁移", False),
        ("discontinue Jenkins", False),
    ),
)
def test_only_task_resumption_triggers_a_resume_followup(app, question, resume):
    core, ctx = app
    vectors = RoundTrackingVectors()
    core.recall_pipeline.vector_port = vectors
    result = _recall(core, ctx, query=question, mode="current")
    assert ("resume_state" in result.unmet_needs) == resume
    assert len(vectors.calls) == (2 if resume else 1)


def test_a_claim_answers_when_active_or_while_only_its_one_source_awaits_judgement():
    """A proposal held back only by the strength of its one source passed every check of what it is, and said by a
    person it answers until its first judgement reaches it.  One already judged, one held back for what it is (a
    supposition, a subject its quote does not bind, a condition it drops), one drawn from a tool's output and a denial
    still proposed answer nothing; an active denial, the person's own answer, does."""

    def claim(ref: str, value: str = "#ops-pager", *, state: str, reason: str, **changes):
        changes.setdefault("judgement", "awaited" if state == "proposed" else "")
        statement_kind = changes.pop("statement_kind", "assertion")
        payload = {"kind": "fact", "subject": "Fennel", "predicate": "alert channel", "value_text": value}
        content = json.dumps({**payload, "statement_kind": statement_kind})
        return _recalled(ref, content, kind="claim", state=state, reason=reason, **changes)

    question = "Where are the Fennel alerts posted?"
    answers = (
        claim("claim-active", state="active", reason="explicit_scoped_source"),
        claim("claim-unproved", state="proposed", reason="fact_entailment_unproved"),
    )
    held_back = (
        claim("claim-judged", state="proposed", reason="fact_entailment_unproved", judgement=""),
        claim("claim-supposed", state="proposed", reason="statement_not_asserted", statement_kind="hypothetical"),
        claim("claim-unbound", state="proposed", reason="subject_not_bound"),
        claim("claim-unconditioned", state="proposed", reason="condition_not_supported"),
        claim("claim-observed", state="proposed", reason="fact_entailment_unproved", origin="tool_observation"),
        claim("claim-denial", "not #ops-pager", state="proposed", reason="fact_entailment_unproved"),
    )
    for item in answers:
        assert answering(question, (item,)) == Answers(True, (item,)), item.ref
    for item in held_back:
        assert answering(question, (item,)) == Answers(True, ()), item.ref
    own = {
        "kind": "preference",
        "subject": "principal:human:alice",
        "predicate": "drink",
        "value_text": "don't drink coffee",
    }
    answer = _recalled("claim-own", json.dumps(own), kind="claim", state="active", reason="explicit_scoped_source")
    assert answering("Does Alice drink coffee?", (answer,)) == Answers(True, (answer,))


def test_a_proposal_the_evaluator_found_insufficient_no_longer_answers(app):
    """Hydrated while its first judgement has not reached it, a proposal a person's words gave answers; once the
    evaluator has ruled its evidence insufficient it answers nothing, though it stays a proposal."""
    core, ctx = app
    said = capture(core, ctx, "Orchard keeps its staging database on port 6543.", key="TEST-p08/judgement-said")
    proposal = draft(said, "6543", kind="fact", subject="Orchard", predicate="staging database port")
    with core.storage.write(ctx) as tx:
        unproved = Qualification("proposed", "inferred_suggestion", "fact_entailment_unproved")
        saved = tx.claims.append("TEST-scope", proposal, unproved, recorded_at=core.clock.utc_now())
        assert tx.candidates.register(saved.ref, saved.revision, observed_at=core.clock.utc_now()).work_queued
    _finish_source_work(core)
    question = "What is the staging database port for Orchard?"

    def hydrated() -> RetrievedObject:
        body = recall_request(query=question, mode="current")
        search = SearchContext.from_request(body, ctx, now=core.clock.utc_now(), deadline=core.clock.monotonic() + 30)
        with core.storage.read(ctx) as tx:
            found = CandidateRef("claim", saved.ref, saved.revision, "claim_lexical")
            item = RetrievalStorage(clock=core.clock).hydrate(tx, found, search)
        assert item is not None
        return item

    waiting = hydrated()
    assert answering(question, (waiting,)) == Answers(True, (waiting,))
    evaluator = Evaluator()
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    judged = hydrated()
    assert evaluator.calls == 1
    assert dict(judged.metadata)["state"] == "proposed"
    assert answering(question, (judged,)) == Answers(True, ())


def test_a_prompt_that_does_not_read_as_a_question_about_named_things_is_not_judged():
    """A pasted traceback, a code block and a long message hold capitalised words that are not things a question
    names, and a short prompt naming more than two things is a list: none is judged, and every item stays its
    evidence.  The question that ends the long message, asked alone, is judged."""
    item = _recalled("event-region", "We moved the deploy region to eu-west-1 last week.")
    traceback = (
        "Why does the worker keep failing?\n"
        "Traceback (most recent call last):\n"
        '  File "/srv/app/worker.py", line 41, in run\n'
        "    Pipeline.start()\n"
        "RuntimeError: Lantern refused the connection"
    )
    code = "Why does this call fail?\n```python\nclient = OrchardClient(Region.EU)\nclient.sync()\n```"
    prose = (
        "I spent the morning going over the rollout plan and the budget with the team, and we still disagree about "
        "the order of the two steps; what did I say about the Basalt migration?"
    )
    listed = "Do Orchard, Lantern and Basalt share one deploy region?"
    for prompt in (traceback, code, prose, listed):
        assert answering(prompt, (item,)) == Answers(False, (item,)), prompt
    assert answering("What did I say about the Basalt migration?", (item,)) == Answers(True, ())


def test_followup_blocked_after_initial_round_when_shared_deadline_expires(tmp_path):
    ctx = replace(context(tmp_path / "TEST-p08-deadline"), project_id="TEST-project", branch_id="TEST-main")
    clock = FollowupDeadlineClock()
    vectors = RoundTrackingVectors(clock=clock, expire_after_first_call=True)
    core = MemoryCore(
        CoreConfig(ctx.binding),
        clock=clock,
        vectors=vectors,
        retrieval_policy=RecallPolicy(vector_threshold=0.8),
    )
    core.recall_pipeline.storage_reader = LexicalBlockedStorage()
    core.initialize()
    h100 = capture(core, ctx, "H100 使用第一套部署配置。", key="TEST-p08/d1", when="2026-09-01T12:00:00Z")
    capture(core, ctx, "H200 使用第二套部署配置。", key="TEST-p08/d2", when="2026-09-05T12:00:00Z")
    vectors.candidates = (_vector(h100),)

    result = core.recall(
        ctx,
        recall_request(query="比较 H100 和 H200 部署配置", mode="history"),
        deadline_seconds=0.15,
    )
    assert len(vectors.calls) == 1
    assert vectors.calls[0]["query"] == "比较 H100 和 H200 部署配置"
    assert "deadline_exceeded_followup" in result.gaps
    assert "comparison_second_side" in result.unmet_needs


def test_collection_as_of_reports_complete_when_time_slice_is_fully_scanned(app):
    core, ctx = app
    old = capture(core, ctx, "旧版 H100 配置。", key="TEST-p08/asof-col", revision=1, when="2026-09-01T12:00:00Z")
    capture(core, ctx, "新版 H200 配置。", key="TEST-p08/asof-col", revision=2, when="2026-09-05T12:00:00Z")
    epoch = core.status(ctx).memory_epoch
    query = CollectionQuery(
        "event",
        page_size=2,
        memory_epoch=epoch,
        scope_digest=scope_digest(ctx),
        mode="as_of",
        as_of="2026-09-03T00:00:00Z",
    )
    page = core.collection(ctx, query, deadline_seconds=5)
    assert page.coverage == "complete_for_query"
    assert len(page.items) == 1
    assert page.items[0].ref == old.ref
    assert page.items[0].content == "旧版 H100 配置。"
    assert all(item.content != "新版 H200 配置。" for item in page.items)
    assert page.next_cursor is None


def test_collection_page_size_31_uses_collection_budget(app):
    core, ctx = app
    for index in range(31):
        capture(
            core, ctx, f"TEST collection row {index}", key=f"TEST-p08/page31/{index:02d}", when="2026-09-05T12:00:00Z"
        )
    epoch = core.status(ctx).memory_epoch
    query = CollectionQuery("event", page_size=31, memory_epoch=epoch, scope_digest=scope_digest(ctx), mode="current")
    page = core.collection(ctx, query, deadline_seconds=5)
    assert len(page.items) == 31
    assert page.coverage == "complete_for_query"
    assert page.next_cursor is None


def test_collection_scans_bounded_invalid_as_of_candidates_before_valid_tail(tmp_path):
    ctx = replace(context(tmp_path / "TEST-p08-collection-scan"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    for index in range(3):
        capture(core, ctx, f"undated row {index}", key=f"TEST-p08/scan/a{index}", when=None)
    valid = capture(core, ctx, "historical valid tail", key="TEST-p08/scan/zvalid", when="2026-09-01T12:00:00Z")
    epoch = core.status(ctx).memory_epoch
    query = CollectionQuery(
        "event",
        page_size=1,
        memory_epoch=epoch,
        scope_digest=scope_digest(ctx),
        mode="as_of",
        as_of="2026-09-02T00:00:00Z",
    )
    page = core.collection(ctx, query, deadline_seconds=5)
    assert [item.ref for item in page.items] == [valid.ref]
    assert page.coverage == "partial"
    assert page.next_cursor is not None


def test_collection_deadline_returns_bounded_cursor_with_injected_clock(tmp_path):
    clock = CollectionTickClock()
    ctx = replace(context(tmp_path / "TEST-p08-collection-deadline"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=clock)
    core.initialize()
    for index in range(8):
        capture(core, ctx, f"row {index}", key=f"TEST-p08/deadline/{index:02d}", when="2026-09-01T12:00:00Z")
    epoch = core.status(ctx).memory_epoch
    query = CollectionQuery("event", page_size=4, memory_epoch=epoch, scope_digest=scope_digest(ctx), mode="current")
    page = core.collection(ctx, query, deadline_seconds=0.5)
    assert page.coverage == "partial"
    assert page.next_cursor is not None
    assert len(page.items) < 4


def test_deadline_after_exact_still_applies_current_source_exclusion(tmp_path):
    clock = FollowupDeadlineClock()
    ctx = replace(context(tmp_path / "TEST-p08-current-exclusion"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=clock)
    core.initialize()
    source = capture(core, ctx, "current source must not reappear", key="TEST-p08/current", when="2026-09-05T12:00:00Z")
    reader = DeadlineAfterExactStorage(source, clock)
    core.recall_pipeline.storage_reader = reader
    result = core.recall(
        ctx,
        recall_request(query="current source", mode="history"),
        current_source_refs=(f"{source.ref}@{source.revision}",),
        deadline_seconds=0.5,
    )
    assert not result.items
    assert "deadline_exceeded_collect" in result.gaps


def test_native_float32_boundary_is_equal_with_fixed_tolerance(tmp_path):
    ctx = replace(context(tmp_path / "TEST-p08-vector-tolerance"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(
        CoreConfig(ctx.binding),
        clock=Clock(),
        retrieval_policy=RecallPolicy(vector_threshold=0.6462987789473623),
    )
    core.initialize()
    source = capture(core, ctx, "海报保留主体高光。", key="TEST-p08/vector-tolerance", when="2026-09-05T12:00:00Z")

    class BoundaryVector:
        score = 0.6462987065315247

        def search(self, context, *, limit, remaining_seconds):
            return (_vector(source, score=self.score),)

    core.recall_pipeline.vector_port = BoundaryVector()
    result = core.recall(ctx, recall_request(query="如何吸引注意焦点", mode="history"), deadline_seconds=5)
    assert source.ref in {item.ref for item in result.items}

    BoundaryVector.score = 0.6462887789473623
    result = core.recall(ctx, recall_request(query="如何吸引注意焦点", mode="history"), deadline_seconds=5)
    assert source.ref not in {item.ref for item in result.items}


@pytest.mark.parametrize(
    "source_text, query",
    [
        ("图片 文件已上传归档。", "图片 文件 批量 改名"),
        ("录音 文件体积已记录。", "录音 文件 批量 压缩"),
    ],
)
def test_generic_short_lexical_overlap_is_not_admitted(app, source_text, query):
    core, ctx = app
    source = capture(core, ctx, source_text, key="TEST-p08/lexical-negative", when="2026-09-05T12:00:00Z")
    result = _recall(core, ctx, query=query, mode="history")
    assert source.ref not in {item.ref for item in result.items}


def test_compound_chinese_query_requires_overlap_within_one_substantive_clause(app):
    core, ctx = app
    query = "请详细说明海报主体视觉层次变化；补充边缘暗化处理依据"
    complementary = capture(
        core,
        ctx,
        "补充边缘暗化",
        key="TEST-p08/clause-aware/complementary",
        when="2026-09-05T12:01:00Z",
    )
    scattered = capture(
        core,
        replace(ctx, session_id="TEST-scattered-session"),
        "请详细补充边缘",
        key="TEST-p08/clause-aware/scattered",
        when="2026-09-05T12:02:00Z",
    )
    unrelated = capture(
        core,
        replace(ctx, session_id="TEST-unrelated-session"),
        "详细说明",
        key="TEST-p08/clause-aware/unrelated",
        when="2026-09-05T12:03:00Z",
    )
    query_terms = set(meaningful_query_terms(query))
    assert len(query_terms) == 23
    assert len(meaningful_query_terms("补充边缘暗化处理依据")) == 9
    assert len(query_terms.intersection(lexical_terms(complementary.event["content"]))) == 5
    assert len(query_terms.intersection(lexical_terms(scattered.event["content"]))) == 5
    assert len(query_terms.intersection(lexical_terms(unrelated.event["content"]))) == 3

    result = _recall(core, ctx, query=query, mode="history")
    refs = {item.ref for item in result.items}
    assert complementary.ref in refs
    assert scattered.ref not in refs
    assert unrelated.ref not in refs


def test_chinese_clause_fallback_requires_two_substantive_cjk_clauses():
    query = "alpha bravo charlie delta echo foxtrot golf hotel india juliet；补充边缘暗化处理依据"
    matches = lexical_terms("补充边缘暗化")
    assert len(set(meaningful_query_terms(query))) == 19
    assert len(set(meaningful_query_terms(query)).intersection(matches)) == 5
    assert not query_is_specific(
        query,
        matched_terms=5.0,
        matched_query_terms=matches,
    )


def test_lexical_prelimit_keeps_direct_and_imported_evidence_ahead_of_diagnostic_envelopes(app):
    core, ctx = app
    direct = capture(
        core,
        ctx,
        "alpha bravo",
        key="TEST-p08/lexical-priority/direct",
        when="2026-09-05T12:01:00Z",
    )
    imported_event = source_event(
        source_event_key="TEST-p08/lexical-priority/imported",
        origin="imported",
        source_original_origin="human_direct",
        role="user",
        content="alpha bravo",
        occurred_at="2026-09-05T12:02:00Z",
    )
    provenance = ImportProvenance(
        "human_direct",
        "a" * 64,
        frozenset({import_source_fingerprint(imported_event)}),
    )
    imported_receipt = core.record_event(
        replace(ctx, actor_origin="imported", import_provenance=provenance),
        imported_event,
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    imported = core.source(ctx, imported_receipt.event_refs[0].ref, 1)

    diagnostics = []
    for index in range(3):
        receipt = core.record_event(
            replace(ctx, actor_origin="memory_reinjection"),
            source_event(
                source_event_key=f"TEST-p08/lexical-priority/diagnostic-{index}",
                origin="memory_reinjection",
                role="tool",
                content="alpha bravo charlie delta",
                occurred_at=f"2026-09-05T12:0{3 + index}:00Z",
            ),
            scope_id="TEST-scope",
            remaining_seconds=10,
        )
        diagnostics.append(core.source(ctx, receipt.event_refs[0].ref, 1))

    search = SearchContext(
        query="alpha bravo charlie delta",
        mode="history",
        as_of=None,
        focus_refs=(),
        limits=SearchLimits(candidate_pool=3),
        deadline=core.clock.monotonic() + 5.0,
        now=core.clock.utc_now(),
        trusted_context=ctx,
    )
    with core.storage.read(ctx) as tx:
        candidates = RetrievalStorage().lexical(tx, search, limit=3)

    assert [candidate.ref for candidate in candidates] == [
        imported.ref,
        direct.ref,
        diagnostics[-1].ref,
    ]


def test_lexical_specificity_accepts_full_multi_term_match_and_one_term_keyword(app):
    core, ctx = app
    full = capture(core, ctx, "图片 文件 批量 改名完成。", key="TEST-p08/lexical-positive", when="2026-09-05T12:00:00Z")
    keyword = capture(core, ctx, "蓝色。", key="TEST-p08/lexical-keyword", when="2026-09-05T12:01:00Z")
    detailed = _recall(core, ctx, query="图片 文件 批量 改名", mode="history")
    assert full.ref in {item.ref for item in detailed.items}
    short = _recall(core, ctx, query="蓝色", mode="history")
    assert keyword.ref in {item.ref for item in short.items}
