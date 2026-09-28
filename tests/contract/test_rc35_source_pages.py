"""A truncated source trigger always finishes, and one pass finishes many of its pages.

On 2026-09-17, with rc34 on alpha, worker passes still started back to back and
processed nothing: the planner woke for 120 truncated candidate source triggers
and each pass resumed one page.  56 of them named memory read back to the model,
which is never evidence, so every page linked nothing and stayed open; the other
64 owed 1,091 pages of sixteen candidates, at one page a pass.
"""
from __future__ import annotations

from dataclasses import replace
import sqlite3

from scope_recall.core import worker
from scope_recall.core.candidate_lifecycle import SOURCE_MATCH_LIMIT
from scope_recall.core.claims import Qualification
from tests.contract.test_r1_candidate_lifecycle import Evaluator, _finish_source_work
from tests.contract.test_v11_claims import app, capture, draft  # noqa: F401  (fixture)
from tests.v11_support import source_event


def _candidates(core, ctx, count):
    """``count`` proposed candidates, each sharing ``sharedtoken`` with any later source."""
    sources = [capture(core, ctx, f"entity{i} property{i} sharedtoken value{i}。", key=f"TEST-rc35/candidate/{i}")
               for i in range(count)]
    refs = []
    with core.storage.write(ctx) as tx:
        for index, source in enumerate(sources):
            proposal = draft(source, f"sharedtoken value{index}", subject=f"entity{index}", predicate=f"property{index}")
            saved = tx.claims.append("TEST-scope", proposal,
                                     Qualification("proposed", "inferred_suggestion", "TEST_candidate"),
                                     recorded_at=core.clock.utc_now())
            tx.candidates.register(saved.ref, saved.revision, observed_at=core.clock.utc_now())
            refs.append(saved.ref)
    return refs


def _pending_pages(core, ctx):
    with core.storage.read(ctx) as tx:
        return tx.candidates.pending_source_pages()


def _trigger(core, source_ref):
    with sqlite3.connect(core.storage.path) as db:
        return db.execute("SELECT matched_count,truncated FROM candidate_source_triggers WHERE source_ref=?",
                          (source_ref,)).fetchone()


def _linked(core, source_ref):
    with sqlite3.connect(core.storage.path) as db:
        return db.execute("SELECT count(*) FROM candidate_evidence WHERE source_ref=?", (source_ref,)).fetchone()[0]


def test_a_trigger_left_on_memory_read_back_is_closed_without_linking_it(app):
    core, ctx = app
    _candidates(core, ctx, SOURCE_MATCH_LIMIT + 4)
    saved = core.record_event(
        replace(ctx, actor_origin="memory_reinjection"),
        source_event(source_event_key="TEST-rc35/echo", content="sharedtoken 召回结果。",
                     origin="memory_reinjection", role="tool"),
        scope_id="TEST-scope", remaining_seconds=10)
    echo = saved.event_refs[0]
    # Reinjection is admitted source-only today; older code opened a trigger for it.
    assert _trigger(core, echo.ref) is None
    with sqlite3.connect(core.storage.path) as db:
        db.execute("INSERT INTO candidate_source_triggers VALUES (?,?,0,0,1,?)",
                   (echo.ref, echo.revision, core.clock.utc_now()))
    assert _pending_pages(core, ctx) == 1

    with core.storage.write(ctx) as tx:
        assert tx.candidates.resume_source_pages(now=core.clock.utc_now()) == 0

    assert _trigger(core, echo.ref) == (0, 0)
    assert _pending_pages(core, ctx) == 0
    assert _linked(core, echo.ref) == 0


def test_a_page_that_links_nothing_closes_its_trigger(app):
    core, ctx = app
    refs = _candidates(core, ctx, SOURCE_MATCH_LIMIT + 4)
    # Hidden from the version reader but not from the trigger query, so every page
    # lists these candidates and none of them can take the evidence.
    with sqlite3.connect(core.storage.path) as db:
        db.executemany(
            "INSERT INTO restored_absence_blocks VALUES ('claim',?,'TEST-scope','TEST-project','TEST-main',?,'TEST')",
            [(ref, "a" * 64) for ref in refs])

    trigger = capture(core, ctx, "sharedtoken 提供了统一的新证据。", key="TEST-rc35/trigger")

    assert _trigger(core, trigger.ref) == (0, 0)
    assert _pending_pages(core, ctx) == 0


def test_one_pass_finishes_every_page_a_source_owes(app):
    core, ctx = app
    count = SOURCE_MATCH_LIMIT * 3 + 4
    _candidates(core, ctx, count)
    trigger = capture(core, ctx, "sharedtoken 提供了统一的新证据。", key="TEST-rc35/trigger")
    _finish_source_work(core)
    assert _trigger(core, trigger.ref) == (SOURCE_MATCH_LIMIT, 1)

    core.drain_worker(ctx, max_items=32, remaining_seconds=10, consolidation=Evaluator())

    assert _linked(core, trigger.ref) == count
    assert _trigger(core, trigger.ref) == (count, 0)
    assert _pending_pages(core, ctx) == 0


def test_a_page_matches_its_source_outside_the_writer_lease(app, monkeypatch):
    """Matching a source against every candidate that shares a term is most of a page: inside the page's write it
    held the writer lease 0.5-7.4 s a page on the shared store (median 1.3 s), and a hook that waited its second
    for the lease meanwhile lost its capture.  The pass finds the candidates in a read; the write only links."""
    from scope_recall.contracts import ContractError
    from scope_recall.core.candidate_intake import CandidateIntake

    core, ctx = app
    count = SOURCE_MATCH_LIMIT * 3 + 4
    _candidates(core, ctx, count)
    trigger = capture(core, ctx, "sharedtoken 提供了统一的新证据。", key="TEST-rc35/read")
    _finish_source_work(core)
    under_write, real = [], CandidateIntake._candidates_mentioned_by

    def spy(self, source, limit):
        try:
            self._tx._check(write=True)
            under_write.append(True)
        except ContractError:
            under_write.append(False)
        return real(self, source, limit)

    monkeypatch.setattr(CandidateIntake, "_candidates_mentioned_by", spy)
    core.drain_worker(ctx, max_items=32, remaining_seconds=10, consolidation=Evaluator())

    assert under_write and not any(under_write), under_write
    assert _linked(core, trigger.ref) == count
    assert _trigger(core, trigger.ref) == (count, 0)


def test_a_candidate_archived_after_the_page_was_read_takes_no_evidence(app):
    core, ctx = app
    refs = _candidates(core, ctx, SOURCE_MATCH_LIMIT * 2 + 4)
    trigger = capture(core, ctx, "sharedtoken 提供了统一的新证据。", key="TEST-rc35/archived")
    before = _linked(core, trigger.ref)
    with core.storage.read(ctx) as tx:
        page = tx.candidates.next_source_page()
    assert page is not None and page[0] == trigger.ref and page[2]
    archived = page[2][0]
    with sqlite3.connect(core.storage.path) as db:
        db.execute("UPDATE candidate_lifecycle SET processing_state='archived',reason='TEST_archived' "
                   "WHERE candidate_ref=? AND candidate_revision=?", archived)
    with core.storage.write(ctx) as tx:
        linked = tx.candidates.resume_source_pages(now=core.clock.utc_now(), page=page)
    assert linked == min(SOURCE_MATCH_LIMIT, len(page[2])) - 1
    assert _linked(core, trigger.ref) == before + linked
    with sqlite3.connect(core.storage.path) as db:
        assert db.execute("SELECT count(*) FROM candidate_evidence WHERE source_ref=? AND candidate_ref=?",
                          (trigger.ref, archived[0])).fetchone()[0] == 0
    assert archived[0] in refs


def test_the_page_queries_read_the_triggers_first(app):
    """Both page queries hold the page's write, so they must not start from every source of every scope.

    On 2026-09-27 the shared store (8,819 triggers, 192 truncated, 379 scopes) took 2.7 s to count the pending
    pages and 3.5 s to find the next one: SQLite started from ``source_events``, whose scope index it could use,
    and looked up a trigger for every source.  A pass runs up to sixteen pages back to back, so the writer lease
    was held for a minute at a time and every hook that waited its one second for it failed to capture.
    """
    core, ctx = app
    _candidates(core, ctx, SOURCE_MATCH_LIMIT + 4)
    capture(core, ctx, "sharedtoken 提供了统一的新证据。", key="TEST-rc35/plan")
    statements = []
    with core.storage.write(ctx) as tx:
        connection = tx._check(write=True)
        connection.set_trace_callback(statements.append)
        tx.candidates.pending_source_pages()
        tx.candidates.resume_source_pages(now=core.clock.utc_now())
        connection.set_trace_callback(None)
    page_queries = [sql for sql in statements
                    if "candidate_source_triggers t" in sql and sql.lstrip().upper().startswith("SELECT")]
    assert len(page_queries) == 2, statements
    with sqlite3.connect(core.storage.path) as db:
        for sql in page_queries:
            plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql)]
            assert plan[0].startswith("SCAN t"), plan


def test_a_pass_gives_a_waiting_writer_its_turn_between_pages(app, monkeypatch):
    """A page's write ends and the next begins at once; a hook polling for the lease every 10 ms rarely lands in
    that gap.  The pass waits a moment after each page so a capture waits behind at most one page."""
    core, ctx = app
    monkeypatch.setattr(worker, "SOURCE_PAGES_PER_PASS", 3)
    count = SOURCE_MATCH_LIMIT * 4 + 4
    _candidates(core, ctx, count)
    capture(core, ctx, "sharedtoken 提供了统一的新证据。", key="TEST-rc35/turn")
    _finish_source_work(core)
    naps = []
    monkeypatch.setattr(worker.time, "sleep", naps.append)

    core.drain_worker(ctx, max_items=32, remaining_seconds=10, consolidation=Evaluator())

    assert naps.count(worker.PAGE_TURN_SECONDS) == 3, "one turn after each of the pass's three pages"
    assert worker.PAGE_TURN_SECONDS >= 2 * 0.01, "at least two of a waiting writer's lease polls"


def test_a_pass_stops_resuming_pages_once_their_time_is_spent(app, monkeypatch):
    """A page on the shared store took 0.5-7 s under the writer lease (matching a long source against thousands of
    candidates), so sixteen of them still held it for half a minute.  Past ``SOURCE_PAGE_SECONDS`` the pass leaves
    the rest to the next one; the page that was running finishes."""
    core, ctx = app
    monkeypatch.setattr(worker, "SOURCE_PAGE_SECONDS", 0.0)
    count = SOURCE_MATCH_LIMIT * 4 + 4
    _candidates(core, ctx, count)
    trigger = capture(core, ctx, "sharedtoken 提供了统一的新证据。", key="TEST-rc35/time")
    _finish_source_work(core)
    assert _trigger(core, trigger.ref) == (SOURCE_MATCH_LIMIT, 1)

    core.drain_worker(ctx, max_items=32, remaining_seconds=10, consolidation=Evaluator())

    assert _trigger(core, trigger.ref) == (SOURCE_MATCH_LIMIT * 2, 1), "one page, then the time is spent"


def test_a_pass_resumes_at_most_its_page_allowance(app, monkeypatch):
    core, ctx = app
    monkeypatch.setattr(worker, "SOURCE_PAGES_PER_PASS", 2)
    count = SOURCE_MATCH_LIMIT * 4 + 4
    _candidates(core, ctx, count)
    trigger = capture(core, ctx, "sharedtoken 提供了统一的新证据。", key="TEST-rc35/trigger")
    _finish_source_work(core)

    core.drain_worker(ctx, max_items=32, remaining_seconds=10, consolidation=Evaluator())
    assert _trigger(core, trigger.ref) == (SOURCE_MATCH_LIMIT * 3, 1)

    core.drain_worker(ctx, max_items=32, remaining_seconds=10, consolidation=Evaluator())
    assert _trigger(core, trigger.ref) == (count, 0)
    assert _pending_pages(core, ctx) == 0


def test_a_page_another_drain_linked_first_does_not_close_the_trigger(app):
    """Two drains read the same page; the first links it, the second finds every link taken.  Closing on that
    empty page left the candidates past it without this source: the second write finds the next page itself."""
    core, ctx = app
    count = SOURCE_MATCH_LIMIT * 3 + 4
    _candidates(core, ctx, count)
    trigger = capture(core, ctx, "sharedtoken 提供了统一的新证据。", key="TEST-rc35/race")
    with core.storage.read(ctx) as tx:
        first = tx.candidates.next_source_page()
    with core.storage.read(ctx) as tx:
        second = tx.candidates.next_source_page()
    assert first == second
    with core.storage.write(ctx) as tx:
        assert tx.candidates.resume_source_pages(now=core.clock.utc_now(), page=first) == SOURCE_MATCH_LIMIT
    with core.storage.write(ctx) as tx:
        assert tx.candidates.resume_source_pages(now=core.clock.utc_now(), page=second) == SOURCE_MATCH_LIMIT
    assert _trigger(core, trigger.ref) == (SOURCE_MATCH_LIMIT * 3, 1)
