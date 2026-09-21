"""An object reached by relation never outranks a direct hit.

Expansion scored every related object by its place in its own seed's list, so the first
object related to the thirteenth hit scored 1/62: above every direct hit but the first.
Nothing showed it while an episode's lineage was copied onto each of its revisions,
because the rows of one episode at dozens of old revisions spent the relation bound
without becoming candidates.  Writing that lineage once freed the bound, the related
objects arrived, and over one instance's real questions the reply that had answered each
was in the top five for 20 of 30 where it had been for 28.  With a related object held
below the hit it was reached from it is 28 again, on the same stores, with fact recall,
no-match, supersession and question recall unchanged.  On a second instance the freed
bound had answered one question of 25 that 3.1.0 missed; weighing gives that one back, so
both instances recall what 3.1.0 recalled.
"""
from __future__ import annotations

import time

from scope_recall.core.recall_policy import rrf_score
from scope_recall.core.retrieval import CandidateRef, SearchContext
from tests.contract import test_v11_claims as claims
from tests.v11_support import recall_request

#: The store fixture, under the name pytest injects it by.
app = claims.app

STATEMENTS = (("TEST-alpha", "配色", "蓝色"), ("TEST-beta", "主题", "深色"), ("TEST-gamma", "字体", "宋体"))


def _hits_with_claims(core, ctx):
    """Three sources a query hit directly, in rank order, each with one accepted claim derived from it."""
    seeds = []
    for rank, (subject, predicate, value) in enumerate(STATEMENTS, start=1):
        source = claims.capture(core, ctx, f"{subject} {predicate} {value}。", key=f"TEST-relation-rank/{rank}",
                                when=f"2026-09-0{rank}T12:00:00Z")
        claims.accept(core, ctx, claims.draft(source, value, subject=subject, predicate=predicate))
        seeds.append(CandidateRef("event", source.ref, source.revision, "lexical", rank=rank, lexical_score=2.0,
                                  fusion_score=rrf_score((rank,), k=60)))
    return tuple(seeds)


def _expanded(core, ctx, seeds):
    context = SearchContext.from_request(recall_request(query="TEST 配色 主题 字体", mode="current", max_items=6), ctx,
                                         now=core.clock.utc_now(), deadline=time.monotonic() + 30)
    with core.storage.read(ctx) as tx:
        return core.recall_pipeline._expand(tx, context, seeds, [])


def test_a_related_object_scores_below_every_direct_hit(app):
    core, ctx = app
    seeds = _hits_with_claims(core, ctx)
    expanded = _expanded(core, ctx, seeds)

    related = [candidate for candidate in expanded if candidate.source == "relation"]
    assert {candidate.kind for candidate in related} == {"claim"}, "the claims derived from the hits were reached"
    assert len(related) >= len(seeds)
    weakest_hit = min(seed.fusion_score for seed in seeds)
    assert max(candidate.fusion_score for candidate in related) < weakest_hit, \
        "the first object related to the last hit used to score 1/62, above that hit's own 1/63"


def test_a_related_object_is_weighed_against_the_hit_it_was_reached_from(app):
    from scope_recall.core.recall import RELATION_WEIGHT

    core, ctx = app
    seeds = _hits_with_claims(core, ctx)
    related = [candidate for candidate in _expanded(core, ctx, seeds) if candidate.source == "relation"]

    assert 0 < RELATION_WEIGHT <= 0.5, "at 0.5 a related object sorts after every hit of the default pool of 48"
    best_hit = max(seed.fusion_score for seed in seeds)
    assert all(0 < candidate.fusion_score <= best_hit * RELATION_WEIGHT for candidate in related)
    assert len({round(candidate.fusion_score, 9) for candidate in related}) > 1, \
        "what the better hit led to still sorts ahead of what a weaker hit led to"
