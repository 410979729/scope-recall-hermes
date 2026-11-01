"""Contracts for corpus-weighted admission experiments and exact cached queries."""

import json
import sqlite3

import pytest
from admission_variants import ReplayPolicy, SnapshotWeights
from cache import RequestCache
from replay import CachedQueries
from scope_recall.core.retrieval import CandidateRef


def _statistics(path):
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE source_events(source_id INTEGER PRIMARY KEY);"
            "CREATE TABLE lexical_terms(term_id INTEGER PRIMARY KEY,term TEXT);"
            "CREATE TABLE lexical_postings(term_id INTEGER,source_id INTEGER);"
        )
        connection.executemany("INSERT INTO source_events VALUES(?)", [(i,) for i in range(6)])
        connection.executemany("INSERT INTO lexical_terms VALUES(?,?)", [(1, "alpha"), (2, "beta")])
        connection.executemany("INSERT INTO lexical_postings VALUES(?,?)", [(1, i) for i in range(6)] + [(2, 0)])
    return SnapshotWeights(path)


@pytest.mark.parametrize("scheme,expected", [("A", True), ("B", False), ("C", True), ("D", True)])
def test_unseen_term_strategies_keep_the_same_matched_term_minimum(tmp_path, scheme, expected):
    with _statistics(tmp_path / "truth.sqlite3") as statistics:
        policy = ReplayPolicy(vector_threshold=0.653, scheme=scheme, statistics=statistics)
        query = "alpha beta gamma delta epsilon zeta eta theta"
        candidate = CandidateRef(
            "event", "event-TEST", 1, "lexical", lexical_score=2, matched_query_terms=("alpha", "beta")
        )
        assert policy.automatic_admission(candidate, query) is expected
        single = CandidateRef("event", "event-TEST", 1, "lexical", lexical_score=1, matched_query_terms=("alpha",))
        assert not policy.automatic_admission(single, query)


def test_median_and_seen_only_weights_handle_empty_corpus_terms(tmp_path):
    with _statistics(tmp_path / "truth.sqlite3") as statistics:
        assert set(statistics.weights("C", "gamma delta").values()) == {0.0}
        assert set(statistics.weights("D", "gamma delta").values()) == {0.0}
        weights = statistics.weights("D", "alpha beta gamma")
        assert weights["gamma"] == pytest.approx((weights["alpha"] + weights["beta"]) / 2)


def test_exact_model_query_cache_uses_primary_keys_and_refuses_misses(tmp_path):
    cache = RequestCache(tmp_path / "cache.sqlite3")
    model = {"model": "TEST-first"}
    request = {"text": "TEST question", "encoding": "query"}
    cache.call("query_embedding", request, model, {}, lambda: [1.0, 0.0])
    cache.call("query_embedding", request, {"model": "TEST-second"}, {}, lambda: [0.0, 1.0])
    queries = CachedQueries(cache.path, model=model)
    assert queries.embed_query("TEST question") == (1.0, 0.0)
    with pytest.raises(ValueError, match="cache_miss"):
        queries.embed_query("TEST absent")


def test_weighted_coverage_accepts_single_pass_term_iterables():
    from admission_variants import coverage

    assert coverage(iter(("rare", "common")), {"rare"}, {"rare": 3.0, "common": 1.0}) == 0.75


@pytest.mark.parametrize("scheme", ["A", "B", "C", "D"])
@pytest.mark.parametrize(
    "common,question",
    [
        ("United States", "How many days did I spend on camping trips in the United States this year?"),
        ("\u7f8e\u56fd\u65c5\u884c", "\u7f8e\u56fd\u65c5\u884c\u9732\u8425\u5929\u6570\u8ba1\u5212\u8bb0\u5f55"),
    ],
)
def test_variants_measure_common_word_noise_in_both_languages(tmp_path, scheme, common, question):
    from test_recall_admission import _app, _capture
    from v11_support import recall_request

    core, context, _ = _app(tmp_path)
    for index in range(24):
        _capture(core, context, f"TEST-common-{index}", f"{common} ordinary discussion")
    _capture(core, context, "TEST-rare", question)
    with sqlite3.connect(core.storage.path) as connection:
        connection.execute("UPDATE source_events SET suppressed=1 WHERE source_event_key='TEST-rare'")
    with SnapshotWeights(core.storage.path) as statistics:
        policy = core.recall_pipeline.policy
        core.recall_pipeline.policy = ReplayPolicy.from_policy(policy, scheme, statistics, policy.vector_threshold)
        packet = core.recall_packet(context, recall_request(query=question, mode="auto", budget_tokens=4096))
    assert bool(packet["items"]) is (scheme == "A")
