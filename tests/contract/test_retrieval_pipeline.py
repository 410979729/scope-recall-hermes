"""Deterministic P08 core retrieval contracts using synthetic TEST identity."""

import itertools
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.recall_policy import SPACE_ID, RecallPolicy
from scope_recall.core.retrieval import CandidateRef, CollectionQuery, PageCursor, SearchContext
from scope_recall.core.retrieval_storage import scope_digest

from tests.contract.test_claims import Clock, capture
from tests.v11_support import context, recall_request, source_event


@pytest.fixture
def app(tmp_path):
    ctx = replace(context(tmp_path / "TEST-p08"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    return core, ctx


def test_search_context_copies_request_and_trusted_runtime(app):
    core, ctx = app
    request = recall_request(query="TEST query", request_id="TEST-r1")
    search = SearchContext.from_request(request, ctx, now=Clock.now, deadline=999.0)
    request["query"] = "mutated"
    request["focus_refs"] = ["untrusted"]
    assert search.query == "TEST query"
    assert search.focus_refs == ()
    assert search.request_id == "TEST-r1"
    with pytest.raises(FrozenInstanceError):
        search.query = "changed"


def test_lexical_recall_is_read_only_and_returns_source_evidence(app):
    core, ctx = app
    source = capture(core, ctx, "P08 keeps H100 as an exact identifier.", key="TEST-p08/lexical")
    before = core.status(ctx)
    result = core.recall(ctx, recall_request(query="H100 exact identifier", mode="current"), deadline_seconds=5)
    after = core.status(ctx)
    assert [item.ref for item in result.items] == [source.ref]
    assert result.items[0].evidence_refs == (f"{source.ref}@{source.revision}",)
    assert after.memory_epoch == before.memory_epoch
    assert after.pending_work == before.pending_work


def test_semantic_candidate_can_admit_without_lexical_overlap(app):
    core, ctx = app
    source = capture(core, ctx, "海上晨雾项目采用暮光方案。", key="TEST-p08/vector")
    ref = source.ref

    class Vector:
        def search(self, context, *, limit, remaining_seconds):
            return (
                CandidateRef("event", ref, 1, "vector", vector_id="v1", embedding_space=SPACE_ID, vector_score=0.91),
            )

    core.recall_pipeline.vector_port = Vector()
    core.recall_pipeline.policy = RecallPolicy(vector_threshold=0.8)
    result = core.recall(ctx, recall_request(query="完全没有共同词的语义问题", mode="current"), deadline_seconds=5)
    assert ref in {item.ref for item in result.items}
    assert next(item for item in result.items if item.ref == ref).content == "海上晨雾项目采用暮光方案。"


def test_hard_identifiers_remain_distinct_but_comparison_keeps_both(app):
    core, ctx = app
    first = capture(
        core, ctx, "H100 使用第一套部署配置。", key="TEST-p08/hard", revision=1, when="2026-09-01T12:00:00Z"
    )
    second = capture(
        core, ctx, "H200 使用第二套部署配置。", key="TEST-p08/hard", revision=2, when="2026-09-05T12:00:00Z"
    )
    single = core.recall(ctx, recall_request(query="H100 部署配置", mode="current"), deadline_seconds=5)
    comparison = core.recall(
        ctx, recall_request(query="比较 H100 和 H200 部署配置", mode="history"), deadline_seconds=5
    )
    assert [item.content for item in single.items] == []
    assert {item.content for item in comparison.items} == {"H100 使用第一套部署配置。", "H200 使用第二套部署配置。"}


def test_as_of_hydrates_historical_source_before_new_revision(app):
    core, ctx = app
    old = capture(core, ctx, "旧版本 H100 配置。", key="TEST-p08/asof", revision=1, when="2026-09-01T12:00:00Z")
    capture(core, ctx, "新版本 H200 配置。", key="TEST-p08/asof", revision=2, when="2026-09-05T12:00:00Z")
    result = core.recall(
        ctx, recall_request(query="H100 配置", mode="as_of", as_of="2026-09-03T00:00:00Z"), deadline_seconds=5
    )
    assert [item.content for item in result.items] == ["旧版本 H100 配置。"]
    assert result.items[0].revision == old.revision


def test_current_source_receipt_excludes_only_that_source(app):
    core, ctx = app
    source = capture(core, ctx, "本轮刚捕获的 TEST 计划。", key="TEST-p08/current")
    key = f"{source.ref}@{source.revision}"
    result = core.recall(
        ctx, recall_request(query="TEST 计划", mode="current"), current_source_refs=(key,), deadline_seconds=5
    )
    assert result.items == ()


def test_collection_cursor_binds_epoch_scope_and_filters(app):
    core, ctx = app
    capture(core, ctx, "第一条 H100 记录。", key="TEST-p08/c1")
    capture(core, ctx, "第二条 H200 记录。", key="TEST-p08/c2")
    epoch = core.status(ctx).memory_epoch
    query = CollectionQuery("event", page_size=1, memory_epoch=epoch, scope_digest=scope_digest(ctx), mode="current")
    first = core.collection(ctx, query)
    assert first.coverage == "partial"
    assert first.next_cursor is not None
    second = core.collection(ctx, query, cursor=first.next_cursor)
    assert second.coverage == "partial"
    assert second.next_cursor is None
    assert first.items[0].ref != second.items[0].ref
    with pytest.raises(ContractError):
        core.collection(ctx, replace(query, memory_epoch=epoch - 1), cursor=first.next_cursor)


def test_cursor_encoding_is_validated_before_scope_binding(app):
    with pytest.raises(ContractError):
        PageCursor.decode("not-a-cursor")


def test_P08_lexical_skips_terms_too_common_to_separate_anything(app):
    """A term matching most of the corpus costs the most and tells the least.

    Terms that clear 10% document frequency are JSON field names from
    tool-observation envelopes (`tool`, `summary`, `omitted`, `exit_code`, ...),
    together about a tenth of the whole index. A query containing one would walk
    an 18,000-row posting list to learn nothing.
    """
    from scope_recall.core.retrieval_storage import (
        _LEXICAL_DF_FLOOR,
        _discriminating_terms,
    )

    core, ctx = app
    capture(core, ctx, "TEST quarkonium 出现一次。", key="TEST-p08/rare")
    for index in range(_LEXICAL_DF_FLOOR + 2):
        capture(core, ctx, f"TEST boilerplate 第{index}条。", key=f"TEST-p08/common/{index}")

    with core.storage.read(ctx) as tx:
        assert _discriminating_terms(tx, ("quarkonium", "boilerplate")) == ("quarkonium",), (
            "the common term is dropped, the rare one stays"
        )
        # A query made only of common terms must still answer: falling back to
        # the rarest of them beats returning nothing at all.
        assert _discriminating_terms(tx, ("boilerplate",)) == ("boilerplate",)
        # A term the index has never seen has no frequency and is never pruned.
        assert _discriminating_terms(tx, ("neverindexed",)) == ("neverindexed",)
        # A kept term survives however common it is.
        assert _discriminating_terms(tx, ("quarkonium", "boilerplate"), keep=("boilerplate",)) == (
            "quarkonium",
            "boilerplate",
        )


def test_P08_a_long_prompt_searches_its_rarest_terms_within_a_posting_budget(monkeypatch):
    """The lexical statement groups every posting of every kept term: on a large shared store a 2,000-character prompt's
    80 terms can hold 273,000 postings and take 9 s, longer than the prompt's whole recall.  The rarest terms
    are searched, as many as the posting budget allows beyond the first few; a short question is left as it was."""
    from scope_recall.core import retrieval_storage

    monkeypatch.setattr(retrieval_storage, "_LEXICAL_MIN_TERMS", 3)
    monkeypatch.setattr(retrieval_storage, "_LEXICAL_POSTING_BUDGET", 100)
    frequencies = {"a": 10, "b": 20, "c": 30, "d": 40, "e": 50, "f": 5}
    terms = ("a", "b", "c", "d", "e", "f", "unindexed")
    # f, a and b are the three always searched; c still fits (65 postings); d would pass the budget (105).
    assert retrieval_storage._within_posting_budget(terms, frequencies, ()) == ("a", "b", "c", "f", "unindexed")
    # A kept identifier stays whatever it costs, and what it costs counts.
    assert retrieval_storage._within_posting_budget(terms, frequencies, ("e",)) == ("a", "b", "e", "f", "unindexed")
    # A question with no more indexed terms than are always searched is left as it was.
    assert retrieval_storage._within_posting_budget(("d", "e", "unindexed"), frequencies, ()) == ("d", "e", "unindexed")
    # The rarest few are searched even when they alone pass the budget.
    monkeypatch.setattr(retrieval_storage, "_LEXICAL_POSTING_BUDGET", 20)
    assert retrieval_storage._within_posting_budget(terms, frequencies, ()) == ("a", "b", "f", "unindexed")
    # A term one source holds (the prompt's own, stored before its recall) stays but takes no rarest place.
    assert retrieval_storage._within_posting_budget(("only", *terms), {**frequencies, "only": 1}, ()) == (
        "only",
        "a",
        "b",
        "f",
        "unindexed",
    )


def test_P08_a_long_prompt_s_related_memory_is_admitted_with_the_terms_the_budget_left_out(app, monkeypatch):
    """Searched with its rarest terms alone, a long prompt must not count a found memory's matches among those alone,
    or admission, which weighs matches against the whole prompt, turns away a memory sharing its topic words (3 of
    the 12 matches required).  What a found row holds of the terms left out still counts."""
    from scope_recall.core import retrieval_storage

    core, ctx = app
    letters = "abcdefghijklmnopqrstuvwxyz"
    rare = [f"rare{letter}q" for letter in letters]
    topical = [f"topic{letter}z" for letter in letters[:14]]
    query = " ".join(rare + topical)
    prompt = capture(core, ctx, query, key="TEST-p08/budget-admission/prompt")
    target = capture(core, ctx, " ".join(topical) + " answerbody explanation", key="TEST-p08/budget-admission/target")
    for index in range(12):
        capture(
            core,
            ctx,
            " ".join(topical[(index + step) % 14] for step in range(7)) + f" filler{letters[index]}text",
            key=f"TEST-p08/budget-admission/filler/{index}",
        )
    for index in range(4):
        capture(
            core,
            ctx,
            " ".join(rare[index * 3 : (index + 1) * 3]) + f" noise{letters[index]}body",
            key=f"TEST-p08/budget-admission/noise/{index}",
        )
    monkeypatch.setattr(retrieval_storage, "_LEXICAL_POSTING_BUDGET", 60)
    result = core.recall(
        ctx,
        recall_request(query=query, mode="current"),
        current_source_refs=(f"{prompt.ref}@{prompt.revision}",),
        deadline_seconds=10,
    )
    assert target.ref in [item.ref for item in result.items], result.gaps


def test_P08_the_lexical_channel_keeps_its_terms_within_the_posting_budget(app, monkeypatch):
    from scope_recall.core import retrieval_storage

    core, ctx = app
    capture(core, ctx, "TEST quarkonium 出现一次。", key="TEST-p08/budget/rare")
    capture(core, ctx, "TEST quarkonium 又出现一次。", key="TEST-p08/budget/rare-again")
    for index in range(5):
        capture(core, ctx, f"TEST gluonfield 第{index}条。", key=f"TEST-p08/budget/common/{index}")
    monkeypatch.setattr(retrieval_storage, "_LEXICAL_MIN_TERMS", 1)
    monkeypatch.setattr(retrieval_storage, "_LEXICAL_POSTING_BUDGET", 3)
    with core.storage.read(ctx) as tx:
        assert retrieval_storage._discriminating_terms(tx, ("quarkonium", "gluonfield")) == ("quarkonium",)


def test_P08_the_lexical_statement_starts_from_the_query_s_terms_however_many_there_are(app, monkeypatch):
    """A store has no planner statistics, so SQLite weighs the query's terms against the audience's scopes by rule of
    thumb, and past some thirty terms it would start from the scope index instead: every event of the audience read, one
    by one.  On a large shared store a Telegram message of 72 characters would take 21 s that way and its recall
    come back empty at every stage's deadline.  The scope filter never drives the statement, and what it finds is
    the same."""
    from scope_recall.core import retrieval_storage

    core, ctx = app
    letters = "abcdefghijklmnopqrstuvwxyz"
    words = [f"plan{first}{second}term" for first in letters[:8] for second in letters[:8]]
    target = capture(core, ctx, " ".join(words[:12]) + " 计划目标。", key="TEST-p08/plan/target")
    plans = []
    lexical = retrieval_storage.RetrievalStorage.lexical

    class Planned:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            if "GROUP_CONCAT(DISTINCT hex(" in sql:
                plans.append(self._conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()[0][3])
            return self._conn.execute(sql, params)

    def planned(self, tx, context, *, limit):
        check = tx._check
        tx._check = lambda: Planned(check())
        try:
            return lexical(self, tx, context, limit=limit)
        finally:
            tx._check = check

    monkeypatch.setattr(retrieval_storage.RetrievalStorage, "lexical", planned)
    found = {}
    for count in (4, 12, 24, 40, 64):
        result = core.recall(ctx, recall_request(query=" ".join(words[:count]), mode="current"), deadline_seconds=10)
        found[count] = target.ref in [item.ref for item in result.items]
    assert len(plans) >= 5 and all(plan.startswith("SEARCH t ") for plan in plans), plans
    assert found[4] and found[12], found


def test_P08_the_recent_channel_reads_the_pending_queue_not_every_consolidation(app, monkeypatch):
    """The recent channel offers this session's messages still waiting to be consolidated.  With no planner
    statistics SQLite would read every consolidate item by its work type, done ones included, which the queue keeps,
    or with one scope every event of it.  It starts from the pending rows."""
    import time as _time

    from scope_recall.core import retrieval_storage
    from scope_recall.core.retrieval import SearchContext

    core, ctx = app
    waiting = capture(core, ctx, "TEST 值班表明天换人。", key="TEST-p08/recent/waiting")
    done = capture(core, ctx, "TEST 值班表上周换过。", key="TEST-p08/recent/done")
    with core.storage.write(ctx, remaining_seconds=10) as tx:
        for source in (waiting, done):
            tx.sources.enqueue_source(
                source.ref, source.revision, work_type="consolidate", available_at=core.clock.utc_now()
            )
        tx._check().execute("UPDATE work_items SET state='done' WHERE subject_ref=?", (done.ref,))
    plans = []

    class Planned:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            if "JOIN work_items w" in sql:
                plans.append(self._conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()[0][3])
            return self._conn.execute(sql, params)

    context = SearchContext.from_request(
        recall_request(query="TEST 值班表", mode="current"),
        ctx,
        now=core.clock.utc_now(),
        deadline=_time.monotonic() + 30,
    )
    with core.storage.read(ctx) as tx:
        check = tx._check
        monkeypatch.setattr(tx, "_check", lambda: Planned(check()))
        found = [candidate.ref for candidate in retrieval_storage.RetrievalStorage().recent(tx, context, limit=8)]
    assert found == [waiting.ref]
    assert plans and plans[0].startswith("SEARCH w USING INDEX work_ready"), plans


def test_P08_a_candidate_statement_past_the_deadline_is_interrupted(app, monkeypatch):
    """A statement cannot see the recall's deadline: the lexical one can run 18-21 s for a long Telegram message on a
    large shared store, and the recall would come back empty long after its deadline.  Past the deadline a candidate
    statement is interrupted at the next look at the clock, and the recall returns, saying so.  The recall's clock here
    moves with the statement's rows, a microsecond each: its 0.5 s are up after 500,000 rows on any machine, and an
    interrupt that came later than a few looks after would let it run on to a million."""
    import sqlite3

    from scope_recall.core import retrieval_storage

    core, ctx = app
    capture(core, ctx, "TEST 值班表明天换人。", key="TEST-p08/interrupt")
    rows, ended = [0], []

    def counted():
        rows[0] += 1
        return 1

    def slow(self, tx, context, *, limit):
        conn = tx._check()
        conn.create_function("counted", 0, counted)
        try:
            conn.execute(
                "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE counted() LIMIT 60000000) "
                "SELECT count(*) FROM n"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            ended.append(exc.sqlite_errorcode)
            raise
        ended.append("ran to its end")
        return ()

    started = core.clock.monotonic()
    monkeypatch.setattr(core.clock, "monotonic", lambda: started + rows[0] / 1_000_000)
    monkeypatch.setattr(retrieval_storage.RetrievalStorage, "lexical", slow)
    result = core.recall(ctx, recall_request(query="TEST 值班表", mode="current"), deadline_seconds=0.5)
    assert ended == [sqlite3.SQLITE_INTERRUPT]
    assert 500_000 <= rows[0] < 1_000_000, rows
    assert "deadline_exceeded_collect" in result.gaps
    assert not [gap for gap in result.gaps if gap.startswith("sqlite_candidate_error")], result.gaps
    # The interrupt ends with the statement: the next recall's statements run as before.
    monkeypatch.undo()
    assert core.recall(ctx, recall_request(query="TEST 值班表", mode="current"), deadline_seconds=5).items


def test_P08_what_the_channels_before_an_interrupted_one_found_still_answers(app, monkeypatch):
    """The exact ref is found before the lexical statement is interrupted, and is still delivered: the interrupt is
    taken off once the candidate statements end, so hydration's own statements, past the deadline too, run to the
    end.  Left on, the first of them would be interrupted and the recall would come back empty."""
    from scope_recall.core import recall as recall_module
    from scope_recall.core import retrieval_storage

    core, ctx = app
    target = capture(core, ctx, "TEST 值班表明天换人。", key="TEST-p08/interrupt-exact")

    def slow(self, tx, context, *, limit):
        tx._check().execute(
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n LIMIT 60000000) SELECT count(*) FROM n"
        ).fetchone()
        return ()

    hydrate = retrieval_storage.RetrievalStorage.hydrate

    def hydrate_after_a_statement(self, tx, candidate, context):
        # Far more steps than the handler lets pass between two looks at the deadline.
        tx._check().execute(
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n LIMIT 300000) SELECT count(*) FROM n"
        ).fetchone()
        return hydrate(self, tx, candidate, context)

    monkeypatch.setattr(recall_module, "_INTERRUPT_STEPS", 1_000)
    monkeypatch.setattr(retrieval_storage.RetrievalStorage, "lexical", slow)
    monkeypatch.setattr(retrieval_storage.RetrievalStorage, "hydrate", hydrate_after_a_statement)
    request = recall_request(query="TEST 值班表", mode="current", focus_refs=[f"{target.ref}@{target.revision}"])
    result = core.recall(ctx, request, deadline_seconds=0.5)
    assert "deadline_exceeded_collect" in result.gaps, result.gaps
    assert [item.ref for item in result.items] == [target.ref], result.gaps


def test_P08_a_locked_store_past_the_deadline_is_reported_as_the_error(app, monkeypatch):
    """Only the interrupt reads as the deadline: another statement error raised past it is still reported as that
    error."""
    import sqlite3
    import time as _time

    from scope_recall.core import retrieval_storage

    core, ctx = app
    capture(core, ctx, "TEST 值班表明天换人。", key="TEST-p08/locked")

    def locked(self, tx, context, *, limit):
        _time.sleep(0.6)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(retrieval_storage.RetrievalStorage, "lexical", locked)
    result = core.recall(ctx, recall_request(query="TEST 值班表", mode="current"), deadline_seconds=0.5)
    assert "sqlite_candidate_error:OperationalError" in result.gaps, result.gaps


def test_P08_lexical_never_prunes_a_common_hard_identifier(app):
    """Hydration still requires the identifier, so SQL must still search for it.

    With 65 sources naming ``rc28`` the term clears the document-frequency
    floor.  Pruned in favour of the query's rarer (here unindexed) terms, SQL
    would match nothing hydration would admit.
    """
    from scope_recall.core.retrieval_storage import _LEXICAL_DF_FLOOR

    core, ctx = app
    for index in range(_LEXICAL_DF_FLOOR):
        capture(
            core,
            ctx,
            f"rc28 构建日志 第{index}条",
            key=f"TEST-p08/common-identifier/{index}",
            when="2026-09-02T12:00:00Z",
        )
    target = capture(
        core,
        ctx,
        "现在还是 rc28，旧记忆和设置没动。",
        key="TEST-p08/common-identifier/target",
        when="2026-09-03T12:00:00Z",
    )
    reader = replace(ctx, session_id="TEST-p08-common-identifier-reader")
    for mode in ("auto", "current", "history"):
        result = core.recall(reader, recall_request(query="rc28 升级结果", mode=mode), deadline_seconds=5)
        assert result.items and result.items[0].ref == target.ref, mode


def test_P08_lexical_pool_ranks_rows_naming_the_hard_identifier_first(app):
    """Hydration admits only sources naming the query's identifier, so the pool must reach them.

    Sixty sources share six generic query terms but not ``rc28``; the one that
    names it matches fewer terms.  Ranked by hits alone it would fall outside the
    47-row first-round pool and every mode would answer nothing.
    """
    from scope_recall.core.retrieval_storage import RetrievalStorage

    core, ctx = app
    query = "阿乙仍然是 Scope Recall rc28"
    target = capture(
        core,
        ctx,
        "阿乙升级收尾：排空超时，现在还是 rc28，旧记忆和设置没动。",
        key="TEST-p08/identifier-pool/target",
        when="2026-09-01T12:00:00Z",
    )
    for index in range(60):
        capture(
            core,
            ctx,
            f"阿乙仍然是 Scope Recall 的测试对象（记录 {index}）",
            key=f"TEST-p08/identifier-pool/{index}",
            when="2026-09-02T12:00:00Z",
        )
    # Another session reads, so the recent channel cannot supply the target.
    reader = replace(ctx, session_id="TEST-p08-identifier-pool-reader")
    search = SearchContext.from_request(
        recall_request(query=query, mode="history"), reader, now=Clock.now, deadline=core.clock.monotonic() + 5
    )
    with core.storage.read(reader) as tx:
        pool = RetrievalStorage().lexical(tx, search, limit=47)
    assert pool[0].ref == target.ref
    assert pool[0].lexical_score < pool[1].lexical_score, "identifier first, then hits"
    for mode in ("auto", "current", "history"):
        result = core.recall(reader, recall_request(query=query, mode=mode), deadline_seconds=5)
        assert [item.ref for item in result.items] == [target.ref], mode

    # Memory reinjection naming the identifier, and every query term, still ranks last.
    reinjected = core.record_event(
        replace(ctx, actor_origin="memory_reinjection"),
        source_event(
            source_event_key="TEST-p08/identifier-pool/reinjected",
            origin="memory_reinjection",
            role="tool",
            content="召回注入：阿乙仍然是 Scope Recall，现在还是 rc28。",
            occurred_at="2026-09-03T12:00:00Z",
        ),
        scope_id="TEST-scope",
        remaining_seconds=10,
    ).event_refs[0]
    with core.storage.read(reader) as tx:
        pool = RetrievalStorage().lexical(tx, search, limit=100)
    assert (pool[0].ref, pool[-1].ref) == (target.ref, reinjected.ref)


def _keep_candidate_marker(core, ctx, item, *, kind, field, marker):
    """Exercise each independently authoritative marker in a synthetic store."""
    from scope_recall.core.delete_storage import OBJECT_TABLES

    if marker == "both":
        return
    table, key = OBJECT_TABLES[kind]
    with core.storage.write(ctx) as tx:
        conn = tx._check(write=True)
        if marker == "row":
            conn.execute(f"UPDATE object_blocks SET {field}=0 WHERE object_kind=? AND object_ref=?", (kind, item.ref))
        else:
            conn.execute(f"UPDATE {table} SET {field}=0 WHERE {key}=?", (item.ref,))


@pytest.mark.parametrize("mode", ["auto", "current", "method", "history", "as_of"])
@pytest.mark.parametrize("field", ["suppressed", "read_blocked"])
@pytest.mark.parametrize("marker", ["both", "row", "block"])
def test_P08_lexical_pool_applies_read_policy_before_limit(app, mode, field, marker):
    """Blocked sources never consume the limit; suppressed sources remain available to explicit reads."""
    from scope_recall.core.retrieval_storage import RetrievalStorage

    from tests.contract.test_deletion import authorize, request
    from tests.contract.test_memory_admin import management

    core, ctx = app
    core.test_sequence = itertools.count(1)
    live = capture(core, ctx, "orion live memory", key="TEST-p08/suppressed-pool/live", when="2026-09-01T12:00:00Z")
    hidden = capture(
        core, ctx, "orion cobalt hidden memory", key="TEST-p08/suppressed-pool/hidden", when="2026-09-01T12:01:00Z"
    )
    action = "suppress" if field == "suppressed" else "delete"
    authorize(core, ctx, hidden, mode=action)
    core.forget(ctx, request(hidden, mode=action), remaining_seconds=10)
    _keep_candidate_marker(core, ctx, hidden, kind="event", field=field, marker=marker)
    reader = replace(ctx, session_id="TEST-p08-suppressed-pool-reader")

    search = SearchContext.from_request(
        recall_request(query="orion cobalt", mode=mode, **({"as_of": Clock.now} if mode == "as_of" else {})),
        reader,
        now=Clock.now,
        deadline=core.clock.monotonic() + 5,
    )
    with core.storage.read(reader) as tx:
        pool = RetrievalStorage().lexical(tx, search, limit=1)
    assert [c.ref for c in pool] == [live.ref if mode == "auto" or field == "read_blocked" else hidden.ref]
    if mode == "auto" and field == "suppressed":
        owner = management(core, ctx)
        owner.apply(owner.preview([hidden.ref], mode="unmute"))
        with core.storage.read(reader) as tx:
            assert [c.ref for c in RetrievalStorage().lexical(tx, search, limit=1)] == [hidden.ref]


@pytest.mark.parametrize("mode", ["auto", "current", "method", "history", "as_of"])
@pytest.mark.parametrize("field", ["suppressed", "read_blocked"])
@pytest.mark.parametrize("marker", ["both", "row", "block"])
def test_P08_claim_pool_applies_read_policy_before_limit(app, monkeypatch, mode, field, marker):
    """Both marker locations protect the bounded claim scan, and unmuting restores automatic reads."""
    from scope_recall.core import retrieval_storage

    from tests.contract.test_claims import accept, draft
    from tests.contract.test_deletion import authorize, request
    from tests.contract.test_memory_admin import management

    core, ctx = app
    core.test_sequence = itertools.count(1)
    live_source = capture(
        core, ctx, "TEST-project 配色 蓝色。", key="TEST-p08/suppressed-claims/live", when="2026-09-01T12:00:00Z"
    )
    hidden_source = capture(
        core,
        ctx,
        "TEST-project cobalt 状态 配色。",
        key="TEST-p08/suppressed-claims/hidden",
        when="2026-09-01T12:01:00Z",
    )
    live = accept(core, ctx, draft(live_source, "蓝色", subject="TEST-project", predicate="配色")).items[0]
    hidden = accept(
        core,
        ctx,
        draft(hidden_source, "配色", subject="TEST-project cobalt", predicate="状态"),
    ).items[0]
    action = "suppress" if field == "suppressed" else "delete"
    authorize(core, ctx, hidden, mode=action)
    core.forget(ctx, request(hidden, mode=action), remaining_seconds=10)
    _keep_candidate_marker(core, ctx, hidden, kind="claim", field=field, marker=marker)
    monkeypatch.setattr(retrieval_storage, "_CLAIM_SCAN_LIMIT", 1)
    reader = replace(ctx, session_id="TEST-p08-suppressed-claims-reader")

    search = SearchContext.from_request(
        recall_request(
            query="TEST-project 配色 cobalt", mode=mode, **({"as_of": Clock.now} if mode == "as_of" else {})
        ),
        reader,
        now=Clock.now,
        deadline=core.clock.monotonic() + 5,
    )
    with core.storage.read(reader) as tx:
        pool = retrieval_storage.RetrievalStorage().claims(tx, search, limit=1)
    assert [c.ref for c in pool] == [live.ref if mode == "auto" or field == "read_blocked" else hidden.ref]
    if mode == "auto" and field == "suppressed":
        owner = management(core, ctx)
        owner.apply(owner.preview([hidden.ref], mode="unmute"))
        with core.storage.read(reader) as tx:
            assert [c.ref for c in retrieval_storage.RetrievalStorage().claims(tx, search, limit=1)] == [hidden.ref]


# -- Chinese synonyms ------------------------------------------------------------


@contextmanager
def _without_synonyms():
    """The unexpanded lexical path: the same pipeline with an empty synonym table."""
    from scope_recall.core import recall_policy

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(recall_policy, "_SYNONYMS", {})
        yield


def _lexical_pool(core, reader, query, *, mode="history"):
    from scope_recall.core.retrieval_storage import RetrievalStorage

    search = SearchContext.from_request(
        recall_request(query=query, mode=mode), reader, now=Clock.now, deadline=core.clock.monotonic() + 5
    )
    with core.storage.read(reader) as tx:
        return RetrievalStorage().lexical(tx, search, limit=47)


def _recalled(core, reader, query, mode):
    return [item.ref for item in core.recall(reader, recall_request(query=query, mode=mode), deadline_seconds=5).items]


def test_P08_synonym_table_is_short_disjoint_and_one_index_term_per_member():
    from scope_recall.core.events import lexical_terms
    from scope_recall.core.recall_policy import SYNONYM_GROUPS

    members = [member for group in SYNONYM_GROUPS for member in group]
    assert 10 <= len(SYNONYM_GROUPS) <= 20 and all(len(group) >= 2 for group in SYNONYM_GROUPS)
    assert len(members) == len(set(members)), "a word belongs to one group"
    # Two CJK characters make one index term, so a swap lines up bigram for bigram.
    assert all(len(member) == 2 and lexical_terms(member) == (member,) for member in members)
    assert "还是" not in members, "still, but also 'or'"


def test_P08_judged_probe_questions_are_outside_the_synonym_table():
    """The probe set is scored on a corpus no gate has: a table change reaching one must be re-measured."""
    from scope_recall.core.recall_policy import synonym_expansions

    from tests.contract.recall_probes import ANSWERABLE, UNANSWERABLE

    for query in (*(question for question, _pattern in ANSWERABLE), *UNANSWERABLE):
        assert synonym_expansions(query) == {}, query


@pytest.mark.parametrize("mode", ["current", "history"])
def test_P08_lexical_synonym_reaches_a_paraphrase_without_the_query_word(app, mode):
    """ "阿乙装上了吗" shares one of its five terms with "阿乙已经安装好了"; three are needed."""
    core, ctx = app
    target = capture(core, ctx, "阿乙已经安装好了", key="TEST-p08/synonym/installed")
    reader = replace(ctx, session_id="TEST-p08-synonym-reader")
    query = "阿乙装上了吗"
    with _without_synonyms():
        assert _recalled(core, reader, query, mode) == []
    # 安装 and 装好 both stand in for 装上, 好了 for 上了: each query term is
    # credited once, and coverage is still counted against the query's five.
    [candidate] = _lexical_pool(core, reader, query, mode=mode)
    assert (candidate.ref, candidate.lexical_score, candidate.matched_query_terms) == (
        target.ref,
        3.0,
        ("上了", "装上", "阿乙"),
    )
    # The source leads; its capture episode may follow by relation.
    assert _recalled(core, reader, query, mode)[:1] == [target.ref]


def test_P08_lexical_synonym_hit_counts_once_for_the_query_term_it_stands_in_for(app):
    """A source sharing nothing with the query but synonyms of one word is one term, and stays out."""
    core, ctx = app
    one = capture(core, ctx, "新版安装包放在共享盘", key="TEST-p08/synonym-bound/one")
    every = capture(core, ctx, "先安装依赖，再把插件装好", key="TEST-p08/synonym-bound/every")
    reader = replace(ctx, session_id="TEST-p08-synonym-bound-reader")
    query = "阿乙装上了吗"
    pool = {
        candidate.ref: (candidate.lexical_score, candidate.matched_query_terms)
        for candidate in _lexical_pool(core, reader, query)
    }
    assert pool == {one.ref: (1.0, ("装上",)), every.ref: (1.0, ("装上",))}
    for mode in ("current", "history"):
        assert _recalled(core, reader, query, mode) == [], mode


def test_P08_lexical_admission_counts_only_the_query_s_own_terms():
    query = "阿乙装上了吗"
    policy = RecallPolicy(vector_threshold=None)
    credited = CandidateRef(
        "event", "event-TEST", 1, "lexical", lexical_score=3.0, matched_query_terms=("上了", "装上", "阿乙")
    )
    assert policy.lexical_admission(credited, query) == (True, None)
    # The same count reported over synonym terms is one query term, not three.
    inflated = replace(credited, matched_query_terms=("安装", "装上", "装好"))
    assert policy.lexical_admission(inflated, query) == (False, "lexical_insufficient_specificity")


def test_P08_lexical_synonym_ranks_a_paraphrased_release_status_into_the_pool(app):
    """ "依然是 rc28" is answered by "仍然是 rc28", never by rc29, however many rc28 logs compete."""
    core, ctx = app
    query = "阿乙依然是 rc28 吗"
    target = capture(core, ctx, "阿乙仍然是 rc28", key="TEST-p08/synonym-pool/target", when="2026-09-01T12:00:00Z")
    rc29 = capture(core, ctx, "阿乙仍然是 rc29", key="TEST-p08/synonym-pool/rc29", when="2026-09-01T12:00:00Z")
    reader = replace(ctx, session_id="TEST-p08-synonym-pool-reader")
    # rc29 matches every Chinese term through the synonym; the identifier still decides.
    pool = {candidate.ref: candidate.lexical_score for candidate in _lexical_pool(core, reader, query)}
    assert pool == {target.ref: 5.0, rc29.ref: 4.0}
    for mode in ("current", "history"):
        assert _recalled(core, reader, query, mode) == [target.ref], mode

    # Sixty newer rc28 logs share three terms each, as many as the target
    # shares literally: unexpanded, they fill the pool and the answer is lost.
    for index in range(60):
        capture(
            core,
            ctx,
            f"阿乙 rc28 回归记录 {index}：结果当然是通过",
            key=f"TEST-p08/synonym-pool/{index}",
            when="2026-09-02T12:00:00Z",
        )
    with _without_synonyms():
        assert target.ref not in {candidate.ref for candidate in _lexical_pool(core, reader, query)}
        for mode in ("current", "history"):
            assert target.ref not in _recalled(core, reader, query, mode), mode
    assert _lexical_pool(core, reader, query)[0].ref == target.ref
    for mode in ("current", "history"):
        refs = _recalled(core, reader, query, mode)
        assert refs[0] == target.ref and rc29.ref not in refs, mode


def test_P08_lexical_query_without_a_synonym_is_untouched_by_the_table(app):
    from scope_recall.core.recall_policy import synonym_expansions

    core, ctx = app
    for index, text in enumerate(
        (
            "阿乙升级收尾：现在还是 rc28，旧记忆和设置没动。",
            "阿乙仍然是 Scope Recall 的测试对象",
            "网关安装日志：rc28 已就绪",
            "安全审计：阿乙网关日志 rc28 已归档",
        )
    ):
        capture(core, ctx, text, key=f"TEST-p08/synonym-free/{index}", when=f"2026-09-0{index + 1}T12:00:00Z")
    reader = replace(ctx, session_id="TEST-p08-synonym-free-reader")
    for query in ("阿乙升级结果怎么样", "rc28 网关日志", "安全审计归档了吗"):
        assert synonym_expansions(query) == {}, query
        for mode in ("current", "history"):
            request = recall_request(query=query, mode=mode)
            expanded = (_lexical_pool(core, reader, query, mode=mode), core.recall(reader, request, deadline_seconds=5))
            with _without_synonyms():
                base = (_lexical_pool(core, reader, query, mode=mode), core.recall(reader, request, deadline_seconds=5))
            assert expanded[0] and expanded == base, (query, mode)
