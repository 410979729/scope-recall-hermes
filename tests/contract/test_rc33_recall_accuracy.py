"""rc33 recall accuracy: dated items, questions that do not answer themselves, facts first.

Each case comes from the rc32 field report or the recall benchmark over alpha and
beta: the newest test report dated a day early, "阿乙当前是什么模型" answered with
"你现在是什么模型呀", and facts reachable only through relation expansion.
"""
from __future__ import annotations

from tests.contract.test_v11_claims import Clock, accept, app, capture, draft, initial, revise_request  # noqa: F401  (fixture)
from tests.v11_support import recall_request


def _packet(core, ctx, query, *, mode="current", max_items=6):
    return core.recall_packet(ctx, recall_request(query=query, mode=mode, max_items=max_items),
                              deadline_seconds=30, background_without_evidence=False)


def _item(packet, ref):
    return next(item for item in packet["items"] if item["ref"] == ref)


# -- when was it said -----------------------------------------------------------

def test_event_items_say_when_they_were_said(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 发布窗口定在周五晚上。", when="2026-09-02T08:30:00Z")
    packet = _packet(core, ctx, "TEST-project 发布窗口")
    assert _item(packet, source.ref)["occurred_at"] == source.event["occurred_at"]


def test_claim_items_are_dated_by_their_newest_evidence(app):
    core, ctx = app
    item, source = initial(core, ctx, value="蓝色", when="2026-09-01T12:00:00Z")
    packet = _packet(core, ctx, "TEST-project 配色")
    claim = _item(packet, item.ref)
    assert claim["kind"] == "claim" and claim["occurred_at"] == source.event["occurred_at"]


def test_a_rekeyed_capture_with_an_inherited_time_is_dated_by_its_write(app):
    """Before rc33 the Hermes adapter copied the time of the unrelated message a
    restarted gateway's reused turn number already named.  The stored rows are
    never rewritten, so recall dates the copy by when it was written."""
    core, ctx = app
    core.clock.now = "2026-09-01T13:15:04Z"
    capture(core, ctx, "TEST-project 整理整个文件夹。", key="TEST-turn-8", when="2026-09-01T13:15:04Z")
    core.clock.now = "2026-09-02T11:08:21Z"
    copied = capture(core, ctx, "TEST-project 召回测试报告。", key="TEST-turn-8#rekey:0123456789abcdef",
                     when="2026-09-01T13:15:04Z")
    own = capture(core, ctx, "TEST-project 召回测试复查。", key="TEST-turn-9#rekey:fedcba9876543210",
                  when="2026-09-02T11:00:00Z")
    packet = _packet(core, ctx, "TEST-project 召回测试")
    assert _item(packet, copied.ref)["occurred_at"] == "2026-09-02T11:08:21Z"
    assert _item(packet, own.ref)["occurred_at"] == own.event["occurred_at"], "a re-key alone changes nothing"


# -- a question is not its own answer --------------------------------------------

def test_an_earlier_question_does_not_outrank_the_answer(app):
    """"阿乙当前是什么模型" returned "你现在是什么模型呀" first on beta.

    The index holds overlapping bigrams, so 是什么 contributed 是什, 什么 and
    么模 -- terms every earlier question shares and no answer does.
    """
    core, ctx = app
    question = capture(core, ctx, "你现在是什么模型呀", when="2026-09-03T10:00:00Z")
    answer = capture(core, ctx, "阿乙当前模型切换为 grok-4.5。", when="2026-09-02T10:00:00Z")
    for mode in ("current", "auto"):
        items = _packet(core, ctx, "阿乙当前是什么模型", mode=mode)["items"]
        # The newer question may still come along as conversation context; it
        # must not stand in front of the answer.
        assert [item["ref"] for item in items][:1] == [answer.ref], mode
        assert question.ref != items[0]["ref"], mode


def test_earlier_questions_do_not_crowd_out_the_answer(app):
    """Asked "你身上已经用上了新版的scoperecall插件了吗？" again, alpha returned
    five earlier questions like it and not the reply that answered one: they
    share every content word with the query and are short enough to rank first."""
    core, ctx = app
    answer = capture(core, ctx, "确认了，我身上已经用上新版 scoperecall 插件：网关和后台 worker 都在运行新版，"
                                "召回测试也都通过，旧版已经替换。", origin="assistant_visible",
                     when="2026-09-02T10:05:00Z")
    questions = [capture(core, ctx, text, when=f"2026-09-0{day}T09:00:00Z") for day, text in (
        (3, "你身上已经用上了新版的scoperecall插件了吗？"),
        (4, "你已经安装了最新版的scoperecall插件了吗"),
        (5, "新版scoperecall插件你用上没？"),
        (6, "这个新版的scoperecall插件，你身上用上了吗"),
    )]
    for mode in ("current", "auto"):
        refs = [item["ref"] for item in _packet(core, ctx, "你身上已经用上了新版的scoperecall插件了吗？", mode=mode)["items"]]
        assert refs[:1] == [answer.ref], mode
    assert {question.ref for question in questions} & set(refs), "questions stay available as context"


def test_what_counts_as_only_asking():
    from scope_recall.core.recall_policy import asks_without_answering

    for text in ("你目前有记忆债务没？", "dlss到底是什么", "5个实例都升级完了吗", "What do you see in this image?"):
        assert asks_without_answering(text), text
    for text in ("去看下阿戊怎么了", "不管什么情况都要先备份", "名称错了，是joy广州服务器",
                 "确认了？" + "我身上已经用上新版插件，网关和 worker 都在运行新版。" * 5):
        assert not asks_without_answering(text), text


def _bury(core, ctx, count=60):
    """Newer talk about the same subject, enough to fill the lexical pool."""
    for n in range(count):
        capture(core, ctx, f"TEST-project 配色方案讨论记录 {n} 号。", when=f"2026-09-02T{n // 60:02d}:{n % 60:02d}:30Z")


# -- facts are searched as facts -------------------------------------------------

def test_a_fact_is_found_when_its_evidence_is_buried_under_newer_talk(app):
    """Claims used to reach recall only through relation expansion out of an
    event retrieved first; 19 of alpha's 29 missed benchmark facts were never
    reached.  Sixty newer messages now outrank the fact's own evidence."""
    core, ctx = app
    item, _source = initial(core, ctx, value="蓝色", when="2026-09-01T12:00:00Z")
    _bury(core, ctx)
    for mode in ("current", "auto"):
        refs = [entry["ref"] for entry in _packet(core, ctx, "TEST-project 配色", mode=mode)["items"]]
        assert item.ref in refs[:3], mode


def test_the_current_value_leads_the_value_it_replaced(app):
    core, ctx = app
    item, _ = initial(core, ctx, value="蓝色", when="2026-09-01T12:00:00Z")
    correction = capture(core, ctx, "Please correct TEST-project 配色: 银色。", when="2026-09-03T12:00:00Z")
    core.revise(ctx, revise_request(item, correction), remaining_seconds=10)
    _bury(core, ctx)
    items = _packet(core, ctx, "TEST-project 配色")["items"]
    current = [n for n, entry in enumerate(items) if entry["kind"] == "claim" and "银色" in entry["content"]]
    older = [n for n, entry in enumerate(items) if "蓝色" in entry["content"]]
    assert current and current[0] < 3
    assert all(n > current[0] for n in older)


def test_naming_only_the_subject_does_not_bring_its_other_facts(app):
    core, ctx = app
    item, _ = initial(core, ctx, value="蓝色")
    refs = [entry["ref"] for entry in _packet(core, ctx, "TEST-project 发布窗口")["items"]]
    assert item.ref not in refs


def test_the_claim_channel_names_its_candidates(app):
    import time

    from scope_recall.core.retrieval import SearchContext
    from scope_recall.core.retrieval_storage import RetrievalStorage

    core, ctx = app
    item, _ = initial(core, ctx, value="蓝色")

    def search(query):
        return SearchContext.from_request(recall_request(query=query, mode="current"), ctx,
                                          now=core.clock.utc_now(), deadline=time.monotonic() + 30)

    with core.storage.read(ctx) as tx:
        found = RetrievalStorage().claims(tx, search("TEST-project 配色"), limit=8)
        missed = RetrievalStorage().claims(tx, search("TEST-project 发布窗口"), limit=8)
    assert [(c.kind, c.ref, c.revision, c.source) for c in found] == [("claim", item.ref, item.revision, "claim_lexical")]
    assert set(found[0].matched_query_terms) == {"test-project", "配色"}
    assert missed == ()


def test_asking_words_are_not_evidence_terms():
    from scope_recall.core.recall_policy import meaningful_query_terms

    assert set(meaningful_query_terms("阿乙当前是什么模型")) == {"阿乙", "姬当", "当前", "前是", "模型"}
    assert set(meaningful_query_terms("Scope Recall 的整理模型怎么配置？")) >= {"scope", "recall", "整理", "模型", "配置"}
    assert not {"怎么", "么配"} & set(meaningful_query_terms("Scope Recall 的整理模型怎么配置？"))
    assert set(meaningful_query_terms("which model does beta use")) == {"model", "beta", "use"}
