"""The information one query term carries in a store (``recall_policy.term_weight``), which the lexical channel's pool
is ranked by.

The weight falls as more of the store holds a term and never reaches nothing, so every matched term counts for
something; a term one source in a thousand holds outweighs two that most sources hold, which is what lets a source
sharing a question's one rare word go before those sharing two of its common ones.
"""

from itertools import pairwise

from scope_recall.core.recall_policy import term_weight


def test_a_term_weighs_less_the_more_sources_hold_it_and_never_nothing():
    weights = [term_weight(frequency, 1000) for frequency in range(1001)]
    assert all(earlier > later for earlier, later in pairwise(weights))
    assert weights[-1] > 0


def test_one_rare_term_outweighs_two_that_many_sources_hold():
    assert term_weight(1, 1000) > 2 * term_weight(100, 1000) > 2 * term_weight(600, 1000)


def test_a_frequency_past_the_corpus_weighs_as_a_term_every_source_holds():
    assert term_weight(1200, 1000) == term_weight(1200, 1200) > 0
