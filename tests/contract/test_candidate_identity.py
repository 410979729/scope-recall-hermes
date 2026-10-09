"""A candidate's identity is not the model's to rewrite, and not its to lose a verdict over.

What the candidate is was recorded before the call; the verdict decides whether the evidence supports it, with what
value and on which quote.  A name the model writes differently -- quoted, escaped, or cut to its first words -- is
the candidate's own and is restored; a name it lengthens, negates or points at something else is refused.
"""

from __future__ import annotations

from scope_recall.core.candidate_lifecycle import candidate_name_matches

from tests.contract.test_candidate_lifecycle import (  # noqa: F401  (fixture)
    Evaluator,
    _candidate,
    _candidate_rows,
    _finish_source_work,
    app,
)


def _verdict(core, ctx, *, candidate_predicate=None, **changes):
    """Run one evaluation whose proposal differs from the candidate as ``changes`` says."""
    saved, _source, proposal, _registration = _candidate(core, ctx, predicate=candidate_predicate)
    _finish_source_work(core)
    evaluator = Evaluator({**proposal, **changes})
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    _lifecycle, evaluations, work = _candidate_rows(core)
    return core.current_claim(ctx, saved.ref), evaluations[0], work[0]


def test_a_predicate_shortened_to_its_first_word_still_settles_the_candidate(app):
    core, ctx = app
    current, evaluation, work = _verdict(core, ctx, candidate_predicate="prefers blue paint", predicate="prefers")
    assert work["state"] == "done" and evaluation["state"] == "resolved"
    assert current.state == "active" and current.payload["predicate"] == "prefers blue paint"


def test_a_predicate_without_its_subject_s_words_still_settles_the_candidate(app):
    """A candidate's predicate that repeats its subject came back without it: the same predicate."""
    core, ctx = app
    current, evaluation, work = _verdict(
        core, ctx, candidate_predicate="entity-blue needs review", predicate="needs review"
    )
    assert work["state"] == "done" and evaluation["state"] == "resolved"
    assert current.payload["predicate"] == "entity-blue needs review"


def test_a_name_turned_into_its_opposite_is_refused_not_restored(app):
    """Restored, the candidate's 吃辣 would be recorded on a verdict the model gave for 不吃辣."""
    core, ctx = app
    current, evaluation, work = _verdict(core, ctx, candidate_predicate="吃辣", predicate="不吃辣")
    assert work["state"] != "done" and evaluation["state"] != "resolved"
    assert current is None


def test_a_name_the_model_lengthened_is_refused(app):
    """The words a model adds to a name may narrow it; the candidate's shorter name is not restored over them."""
    core, ctx = app
    current, evaluation, work = _verdict(core, ctx, subject="entity-blue in staging")
    assert work["state"] != "done" and evaluation["state"] != "resolved"
    assert current is None


def test_a_name_quoted_by_the_model_is_the_same_name(app):
    core, ctx = app
    current, evaluation, _work = _verdict(core, ctx, subject="「entity-blue」")
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
        ('无 vision_analyze 工具时要\\"看图\\"（截图识别）', '无 vision_analyze 工具时要"看图"（截图识别）'),
        ("prefer PYTHONDONTWRITEBYTECODE=1 to avoid __pycache__", "prefer"),
        ("host_adapter", "HOST_ADAPTER"),
        ("配色", "**配色**"),
        ("hourly health check store access", "hourly health check"),
        ("喜欢喝咖啡", "喜欢"),
        ("Issue comments", "issue comments"),
        ("defect", "a defect"),
        ("fixed_by", "is fixed by"),
        ("has access", "have access"),
        ("API KEY", "API"),
        ("live store queries", "live store"),
    )
    different = (
        ("吃辣", "不吃辣"),
        ("允许删除", "不允许删除"),
        ("吃辣", "偶尔吃辣"),
        ("吃辣", "吃辣（偶尔）"),
        ("prod", "nonprod"),
        ("prod", "non-prod"),
        ("allow delete", "do not allow delete"),
        ("allow delete", "allow delete temporarily"),
        ("allow delete", "allow delete in staging"),
        ("不吃辣", "吃辣"),
        ("nonprod", "prod"),
        ("rc28", "rc2"),
        ("gpt-5-mini", "gpt-5"),
        ("吃辣不行", "吃辣"),
        ("allow delete if approved", "allow delete"),
        ("embedding_retry.py", "embedding_retry.py 全文"),
        ("allow delete", "allow delete if approved"),
        ("部署", "部署之前确认"),
        ("fixed_by", "is not fixed by"),
        ("has access", "had access"),
        ("had access", "has access"),
        ("fixed_by", "was fixed by"),
        ("allow delete", "allow delete only on Fridays"),
        ("rc2", "rc28"),
        ("gpt-5", "gpt-5-mini"),
        ("方案", "方案2"),
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
    # A predicate that repeats its subject is the same predicate without it; a negation never falls away with it.
    said = ("issue comments need Joy's OK", "need Joy's OK")
    assert candidate_name_matches(*said, subject="issue comments")
    assert not candidate_name_matches(*said)
    assert not candidate_name_matches("issue comments need Joy's OK", "do not need Joy's OK", subject="issue comments")
    assert not candidate_name_matches("Joy吃辣", "不吃辣", subject="Joy")
    assert not candidate_name_matches("prodigy plan", "igy plan", subject="prod")
