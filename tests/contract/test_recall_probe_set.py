"""The probe set itself has to be well formed, even where it cannot be run.

``tests/eval/recall_probes.py`` scores recall against a real corpus, which no
gate has.  What a gate *can* check is that the set stays honest: every question
distinct, every predicate a valid regex that is narrower than "mentions a
word", and the abstention questions genuinely absent from the answerable set.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parents[1] / "eval"
if str(EVAL) not in sys.path:
    sys.path.insert(0, str(EVAL))

from recall_probes import (  # noqa: E402
    ANSWERABLE,
    GOLD,
    UNANSWERABLE,
    mean_reciprocal_rank,
    score,
)


def test_the_set_is_not_empty_and_covers_both_outcomes():
    assert len(ANSWERABLE) >= 8 and len(UNANSWERABLE) >= 3


def test_every_question_is_asked_once():
    questions = [query for query, _ in ANSWERABLE] + list(UNANSWERABLE)
    assert len(set(questions)) == len(questions)


def test_no_question_is_both_answerable_and_not():
    assert not {query for query, _ in ANSWERABLE} & set(UNANSWERABLE)


@pytest.mark.parametrize("query,pattern", ANSWERABLE)
def test_every_predicate_compiles_and_is_specific(query, pattern):
    compiled = re.compile(pattern)
    assert compiled.pattern
    # A bare common word would match an audit dump quoting other events, which
    # is the false positive that once made a worse ranking look better.
    assert len(re.sub(r"[\^$.|?*+()\[\]{}]", "", pattern)) >= 8, query


def test_scoring_reports_the_first_answer_or_nothing():
    class _Item:
        def __init__(self, content):
            self.content = content

    pattern = re.compile("答案")
    assert score([_Item("无关"), _Item("这里有答案")], pattern) == 2
    assert score([_Item("无关")], pattern) is None
    assert score([], pattern) is None


def test_mrr_matches_the_published_figure_shape():
    assert mean_reciprocal_rank([1, 1, 1, 1, 1, 1, 2, 6]) == pytest.approx(0.833, abs=0.001)
    assert mean_reciprocal_rank([None, None]) == 0.0
    assert mean_reciprocal_rank([]) == 0.0
