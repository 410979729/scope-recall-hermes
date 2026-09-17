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
