"""Automatic injection needs channel evidence and a prompt that needs memory."""

import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from scope_recall.core.auto_recall import automatic_recall_skip_reason
from scope_recall.core.recall_policy import RecallPolicy, rrf_score
from scope_recall.core.retrieval import CandidateRef, SearchContext

from tests.contract.test_autonomous_context import app, task
from tests.contract.test_recall_admission import _app, _candidate, _capture, recall
from tests.contract.test_recall_dating_and_order import _say
from tests.v11_support import recall_request


def test_one_common_identifier_is_below_floor_but_explicit_recall_keeps_it(tmp_path):
    core, ctx, _ = _app(tmp_path)
    source = _capture(core, ctx, "one-word", "2a invoice archive")
    result = core.recall_packet(ctx, recall_request(query="2a telescope lens"))
    assert result["items"] == []
    assert "auto_relevance_below_floor:lexical:1" in result["gaps"]
    explicit = recall(core, ctx, query="2a telescope lens", mode="current")
    assert source.ref in {item.ref for item in explicit.items}
    explicit_auto = core.recall_packet(
        ctx, recall_request(query="2a telescope lens"), background_without_evidence=False
    )
    assert source.ref in {item["ref"] for item in explicit_auto["items"]}
    assert not any(gap.startswith("auto_relevance_below_floor:") for gap in explicit_auto["gaps"])


def test_explicit_auto_lookup_can_search_a_mid_session_continuation(tmp_path):
    core, ctx, vectors = _app(tmp_path)
    source = _capture(core, ctx, "earlier-task", "continue the synthetic poster")
    assert not core.recall_packet(ctx, recall_request(query="continue"))["items"]
    assert not vectors.calls
    explicit = core.recall_packet(ctx, recall_request(query="continue"), background_without_evidence=False)
    assert source.ref in {item["ref"] for item in explicit["items"]}
    assert vectors.calls


def test_strong_channel_and_exact_reference_can_qualify_a_weak_lexical_hit(tmp_path, monkeypatch):
    core, ctx, vectors = _app(tmp_path)
    source = _capture(core, ctx, "weak-seed", "2a invoice archive")
    vectors.candidates = (_candidate(source),)
    assert source.ref in {item.ref for item in recall(core, ctx, query="2a telescope lens").items}
    vectors.candidates = ()
    monkeypatch.setattr(
        core.recall_pipeline.storage_reader, "related", lambda *a, **k: pytest.fail("weak seed expanded")
    )
    assert not recall(core, ctx, query="2a telescope lens").items
    monkeypatch.undo()
    exact = recall(core, ctx, query="2a telescope lens", focus_refs=(f"{source.ref}@1",))
    assert source.ref in {item.ref for item in exact.items}


@pytest.mark.parametrize(
    "query,content",
    [("window", "window pigeon"), ("window bird", "window bird pigeon"), ("我家窗外有什么", "我窗外有只鸽子")],
)
def test_short_questions_retain_matching_terms(tmp_path, query, content):
    core, ctx, _ = _app(tmp_path)
    source = _capture(core, ctx, "short", content)
    result = recall(core, ctx, query=query)
    assert source.ref in {item.ref for item in result.items}


def test_each_lexical_answer_qualifies_independently_of_an_echo(tmp_path):
    core, ctx, _ = _app(tmp_path)
    query = "What is the orchard sprinkler pressure?"
    answer = _capture(core, ctx, "pressure-answer", "The sprinkler pressure is forty psi.")
    assert answer.ref in {item["ref"] for item in core.recall_packet(ctx, recall_request(query=query))["items"]}
    echo = _capture(core, replace(ctx, session_id="TEST-earlier-question"), "pressure-echo", query)
    result = core.recall_packet(ctx, recall_request(query=query))
    assert answer.ref in {item["ref"] for item in result["items"]}
    assert echo.ref not in {item["ref"] for item in result["items"]}


def test_a_weak_long_message_cannot_lift_its_reply_above_a_strong_answer(tmp_path, monkeypatch):
    core, ctx, vectors = _app(tmp_path)
    document = _say(
        core,
        ctx,
        "orchard sprinkler " + "合成文档内容。" * 1000,
        origin="human_direct",
        role="user",
        when="2026-09-01T09:00:00Z",
        key="TEST-manual",
    )
    reply = _say(
        core,
        ctx,
        "First I check where each rule file is.",
        origin="assistant_visible",
        role="assistant",
        when="2026-09-01T09:00:12Z",
        key="TEST-manual-reply",
    )
    answer = _capture(core, replace(ctx, session_id="TEST-pressure-answer"), "strong-answer", "Set it to forty psi.")
    seed = CandidateRef(
        "event", document.ref, 1, "lexical", rank=11, lexical_score=2.0, matched_query_terms=("orchard", "sprinkler")
    )
    monkeypatch.setattr(core.recall_pipeline.storage_reader, "lexical", lambda *a, **k: (seed,))
    leading = [
        _capture(
            core,
            replace(ctx, session_id=f"TEST-leading-{index}"),
            f"leading-{index}",
            f"Synthetic direct evidence {index}.",
        )
        for index in range(4)
    ]
    vectors.candidates = (*(_candidate(source) for source in leading), _candidate(answer, score=0.84))
    reader = replace(ctx, session_id="TEST-pressure-reader")
    result = core.recall_packet(reader, recall_request(query="orchard sprinkler operating pressure", max_items=5))
    refs = {item["ref"] for item in result["items"]}
    assert answer.ref in refs, result
    assert reply.ref not in refs, result


@pytest.mark.parametrize("path", ["ordinary", "echo", "shared_echo"])
def test_turn_reply_score_inherits_only_an_ordinary_seed_bound(tmp_path, monkeypatch, path):
    core, ctx, _ = _app(tmp_path)
    question = _say(
        core,
        ctx,
        "synthetic orchard sprinkler manual",
        origin="human_direct",
        role="user",
        when="2026-09-01T09:00:00Z",
        key="TEST-score-question",
    )
    reply = _say(
        core,
        ctx,
        "The valve is beside the gate.",
        origin="assistant_visible",
        role="assistant",
        when="2026-09-01T09:00:12Z",
        key="TEST-score-reply",
    )
    seed = CandidateRef("event", question.ref, 1, "lexical", rank=11, fusion_score=rrf_score((11,)))
    seeds, echoes = ((seed,), ()) if path == "ordinary" else ((), (seed,))
    if path == "shared_echo":
        seeds = (replace(seed, ref="TEST-ordinary-seed", rank=3, fusion_score=rrf_score((3,))),)
        monkeypatch.setattr(
            core.recall_pipeline.storage_reader,
            "turn_replies",
            lambda *args: (CandidateRef("event", reply.ref, 1, "recent_raw", rank=1),),
        )
    search = SearchContext.from_request(
        recall_request(query="orchard sprinkler operating pressure"), ctx, now=core.clock.utc_now(), deadline=102.0
    )
    with core.storage.read(ctx) as tx:
        expanded = core.recall_pipeline._expand(tx, search, seeds, [], echoes=echoes)
    candidate = next(candidate for candidate in expanded if candidate.ref == reply.ref)
    assert candidate.fusion_score == (seed.fusion_score if path == "ordinary" else rrf_score((2,)))


def test_multi_topic_question_retains_each_lexical_answer(tmp_path):
    core, ctx, _ = _app(tmp_path)
    queue = _capture(core, ctx, "queue", "Onyx 自研的队列换成了托管队列。")
    audit = _capture(core, ctx, "audit", "Onyx 的管理接口开启了审计日志。")
    result = recall(core, ctx, query="替换 Onyx 自研的队列和给 Onyx 管理接口开审计日志，我们先做的是哪个？")
    assert {queue.ref, audit.ref} <= {item.ref for item in result.items}


def test_attachment_terms_do_not_dilute_an_independently_matched_question(tmp_path):
    core, ctx, _ = _app(tmp_path)
    source = _capture(core, ctx, "project-owner", "TEST 白鹭项目的负责人是 KZ-42。")
    attachment = "".join(chr(0x4E00 + index) for index in range(180))
    result = recall(core, ctx, query=f"白鹭项目的负责人 KZ-42 是谁？下面是附件：\n{attachment}")
    assert source.ref in {item.ref for item in result.items}


def test_vector_floor_is_independent_of_lexical_coverage_and_admission(tmp_path):
    core, ctx, vectors = _app(tmp_path)
    source = _capture(core, ctx, "semantic", "quiet aviary")
    vectors.candidates = (_candidate(source, score=0.95),)
    assert source.ref in {item.ref for item in recall(core, ctx, query="violet lantern").items}
    core.recall_pipeline.policy = RecallPolicy(vector_threshold=0.8, auto_vector_threshold=0.9)
    vectors.candidates = (_candidate(source, score=0.85),)
    result = core.recall_packet(ctx, recall_request(query="violet lantern"))
    assert result["items"] == []
    assert "auto_relevance_below_floor:vector:1" in result["gaps"]
    explicit_auto = core.recall_packet(ctx, recall_request(query="violet lantern"), background_without_evidence=False)
    assert source.ref in {item["ref"] for item in explicit_auto["items"]}
    assert source.ref in {item.ref for item in recall(core, ctx, query="violet lantern", mode="current").items}


@pytest.mark.parametrize(
    "query",
    [
        "继续",
        "好的",
        "按你推荐的办",
        "删除了吗",
        "codex在开始跑了吗",
        "太棒了！",
        "Thanks!",
        "go ahead",
        "Is it done?",
        "Great job, continue!",
        "Continue, thanks",
        "继续，谢谢",
        "Thanks, continue",
        "继续，谢谢，进度如何？",
        "可以，按照你说的设置",
        "好，按你建议的来",
        "按你推荐的改",
        "按照你刚才说的弄吧",
        "删除了吗\n",
        "删除了吗\r\n  ",
    ],
)
def test_mid_session_conversation_starts_no_embedding_or_search(tmp_path, monkeypatch, query):
    core, ctx, vectors = _app(tmp_path)
    _capture(core, ctx, "earlier", "Please prepare a synthetic poster.")
    current = _capture(core, ctx, "current", query)
    monkeypatch.setattr(vectors, "prefetch_query", lambda *a, **k: pytest.fail("embedding requested"), raising=False)
    monkeypatch.setattr(core.recall_pipeline.storage_reader, "lexical", lambda *a, **k: pytest.fail("search requested"))
    result = core.recall_packet(ctx, recall_request(query=query), current_source_refs=(f"{current.ref}@1",))
    assert result["items"] == []
    assert any(gap.startswith("auto_recall_skipped:") for gap in result["gaps"])
    assert vectors.calls == []


def test_first_prompt_continues_grounded_task_after_current_prompt_capture(app):
    core, ctx = app
    ctx = replace(ctx, task_anchor="TEST-active-task")
    episode = task(core, ctx, "TEST 海报排版还未完成")
    fresh = replace(ctx, session_id="TEST-new-session", task_anchor=None)
    _capture(core, fresh, "notice", "A synthetic assistant notice.", origin="assistant_visible")
    current = _capture(core, fresh, "first", "继续")
    result = core.recall_packet(fresh, recall_request(query="继续"), current_source_refs=(f"{current.ref}@1",))
    assert episode.ref in {item["ref"] for item in result["items"]}
    assert not any(gap.startswith("auto_recall_skipped:") for gap in result["gaps"])


@pytest.mark.parametrize(
    "query",
    [
        "我是谁",
        "我以前删除了吗",
        "我喜欢什么",
        "Who am I?",
        "What did we delete?",
        "继续查找我的偏好",
        "Is Orchard running?",
        "Thanks, what is my preferred editor?",
        "Continue, thanks, and check the orchard pressure",
        "继续，谢谢，检查喷头压力",
        "按你说的设置喷头压力",
        "可以，按你建议的改配置文件",
    ],
)
def test_owner_past_and_named_topic_questions_are_not_skipped(tmp_path, query):
    core, ctx, _ = _app(tmp_path)
    _capture(core, ctx, "earlier", "synthetic session context")
    with core.storage.read(ctx) as tx:
        assert automatic_recall_skip_reason(query, tx=tx, context=ctx) is None


@pytest.mark.parametrize("query,reason", [("", "blank_prompt"), ("  ", "blank_prompt"), (" /help", "slash_command")])
def test_input_only_skips_need_no_session(query, reason):
    assert automatic_recall_skip_reason(query) == reason


def test_a_fresh_session_checks_only_a_bounded_indexed_scope_window(tmp_path):
    _, ctx, _ = _app(tmp_path)
    with sqlite3.connect(":memory:") as conn:
        conn.execute("""CREATE TABLE source_events(scope_id, occurred_at, event_id, source_revision,
                     session_id, entry_id, role, source_original_origin, origin,
                     project_id, branch_id, read_blocked)""")
        conn.execute("""CREATE INDEX source_scope_time
                     ON source_events(scope_id,occurred_at,event_id,source_revision)""")
        conn.executemany(
            "INSERT INTO source_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                (
                    "TEST-scope",
                    str(index).zfill(8),
                    str(index),
                    1,
                    "TEST-other-session",
                    "local",
                    "user",
                    None,
                    "human_direct",
                    None,
                    None,
                    0,
                )
                for index in range(10000)
            ),
        )
        steps = []
        conn.set_progress_handler(lambda: steps.append(1) or 0, 100)
        tx = SimpleNamespace(_check=lambda: conn)
        assert automatic_recall_skip_reason("继续", tx=tx, context=ctx) is None
        assert len(steps) < 150, "a first prompt must not scan every earlier human message in its scope"


def test_session_probe_obeys_the_deadline_and_releases_its_handler(tmp_path):
    from scope_recall.contracts import ContractError

    core, ctx, _ = _app(tmp_path)
    _capture(core, ctx, "prior", "Prepare a synthetic poster.")
    with core.storage.read(ctx) as tx:
        with pytest.raises(ContractError, match="DEADLINE_EXCEEDED"):
            automatic_recall_skip_reason("继续", tx=tx, context=ctx, deadline=99.0, clock=core.clock)
        assert tx._check().execute("SELECT 1").fetchone()[0] == 1


def test_session_probe_interrupts_an_inflight_statement(tmp_path):
    from scope_recall.contracts import ContractError

    _, ctx, _ = _app(tmp_path)
    ticks = iter(range(100))
    with sqlite3.connect(":memory:") as conn:

        class SlowProbe:
            set_progress_handler = conn.set_progress_handler

            def execute(self, *args):
                return conn.execute("""WITH RECURSIVE rows(n) AS (VALUES(1) UNION ALL
                                    SELECT n+1 FROM rows WHERE n<100000) SELECT sum(n) FROM rows""")

        tx = SimpleNamespace(_check=lambda: SlowProbe())
        with pytest.raises(ContractError, match="DEADLINE_EXCEEDED"):
            automatic_recall_skip_reason(
                "继续", tx=tx, context=ctx, deadline=3.0, clock=SimpleNamespace(monotonic=lambda: next(ticks))
            )
        assert SlowProbe().execute().fetchone()[0] == 5000050000
