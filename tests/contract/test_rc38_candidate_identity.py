"""A candidate's identity is not the model's to rewrite, and not its to lose a verdict over.

Replayed against the real model on alpha's terminally failed evaluations, every rejected
name was the candidate's own, written differently: ``embedding_retry.py`` came back as
``embedding_retry.py 全文`` from the document's heading, a subject holding ``\\"看图\\"`` came
back with plain quotes, and a predicate of a whole clause came back as its first word with
the rest in ``value_text``.  One candidate had been refused four times over its predicate,
each refusal a model call.  What the candidate is was recorded before the call; the verdict
decides whether the evidence supports it, with what value and on which quote.
"""
from __future__ import annotations

from scope_recall.core.candidate_lifecycle import candidate_name_matches
from tests.contract.test_r1_candidate_lifecycle import (  # noqa: F401  (fixture)
    Evaluator,
    _candidate,
    _candidate_rows,
    _finish_source_work,
    app,
)


def _verdict(core, ctx, **changes):
    """Run one evaluation whose proposal differs from the candidate as ``changes`` says."""
    saved, _source, proposal, _registration = _candidate(core, ctx)
    _finish_source_work(core)
    evaluator = Evaluator({**proposal, **changes})
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    _lifecycle, evaluations, work = _candidate_rows(core)
    return core.current_claim(ctx, saved.ref), evaluations[0], work[0]


def test_a_predicate_shortened_to_its_first_word_still_settles_the_candidate(app):
    core, ctx = app
    current, evaluation, work = _verdict(core, ctx, predicate="property")
    assert work["state"] == "done" and evaluation["state"] == "resolved"
    assert current.state == "active" and current.payload["predicate"] == "property-blue"


def test_a_subject_that_picked_up_a_heading_still_settles_the_candidate(app):
    core, ctx = app
    current, evaluation, work = _verdict(core, ctx, subject="entity-blue 全文")
    assert work["state"] == "done" and evaluation["state"] == "resolved"
    assert current.payload["subject"] == "entity-blue"


def test_a_name_quoted_by_the_model_is_the_same_name(app):
    core, ctx = app
    current, evaluation, _work = _verdict(core, ctx, subject='「entity-blue」')
    assert evaluation["state"] == "resolved" and current.payload["subject"] == "entity-blue"


def test_a_different_name_is_still_refused(app):
    """Rejected, which is one feedback retry away from terminal; never recorded."""
    core, ctx = app
    current, evaluation, work = _verdict(core, ctx, subject="entity-red")
    assert work["state"] != "done" and evaluation["state"] != "resolved"
    assert current is None, "the candidate stays a proposal; nothing is recorded as a fact"


def test_a_different_kind_is_still_refused(app):
    """A kind is one of a fixed set: there is nothing to write differently."""
    core, ctx = app
    current, evaluation, work = _verdict(core, ctx, kind="fact")
    assert work["state"] != "done" and evaluation["state"] != "resolved"
    assert current is None


def test_what_counts_as_the_same_name():
    same = (
        ("embedding_retry.py", "embedding_retry.py 全文"),
        ('无 vision_analyze 工具时要\\"看图\\"（截图识别）', '无 vision_analyze 工具时要"看图"（截图识别）'),
        ("prefer PYTHONDONTWRITEBYTECODE=1 to avoid __pycache__", "prefer"),
        ("host_adapter", "HOST_ADAPTER"),
        ("配色", "**配色**"),
    )
    different = (
        ("kimi-k3", "ollama-cloud-provider"),
        ("entity-blue", "entity-red"),
        ("rc28", "rc29"),
        ("host_adapter", ""),
        ("", "host_adapter"),
        ("predicate", None),
    )
    for expected, proposed in same:
        assert candidate_name_matches(expected, proposed), (expected, proposed)
    for expected, proposed in different:
        assert not candidate_name_matches(expected, proposed), (expected, proposed)
