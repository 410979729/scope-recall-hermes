"""Offline calibration and benchmark policy contracts."""

import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from cache import RequestCache
from cases import Case, Turn
from lab import Lab, benchmark_policy
from ports import Ports
from replay import (
    CachedQueries,
    copy_store,
    measure,
    prepare_vectors,
    shifted_pairs,
    source_metadata,
    store_for,
    summarize,
)
from run import run_case
from scope_recall.core.recall_policy import EMBEDDING_SPACE, RecallPolicy


def test_benchmark_result_records_the_effective_vector_threshold(tmp_path, monkeypatch):
    ports = Ports(RequestCache(tmp_path / "cache.sqlite3"))
    monkeypatch.setattr(Lab, "settle", lambda self, **kwargs: {"status": "complete"})
    turn = Turn("TEST-turn", "TEST-session", "user", "TEST telescope amber", "2020-01-01T00:00:00Z", True)
    case = Case(
        "TEST-question", "single-session-user", "TEST telescope color", turn.at, "amber", (turn.session,), (turn,)
    )
    row = run_case(case, root=tmp_path / "stores", ports=ports, prompts={}, method="scope-recall", retrieval_only=True)
    assert row["vector_threshold"] == 0.7


def test_builtin_benchmark_route_uses_product_default_and_explicit_override():
    route = {key: EMBEDDING_SPACE[key] for key in ("model", "dimensions", "endpoint")}
    route.update(dialect="gemini", credential_env="TEST_UNUSED_KEY")
    assert benchmark_policy({"embedding": route}).vector_threshold == RecallPolicy().vector_threshold == 0.70
    assert benchmark_policy({"embedding": route, "vector_threshold": 0.75}).vector_threshold == 0.75
    assert benchmark_policy({"embedding": {**route, "model": "TEST-other-model"}}).vector_threshold is None


def test_cached_queries_never_fall_back_to_document_vectors_or_network(tmp_path):
    path = tmp_path / "cache.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE responses(request TEXT,response TEXT)")
        for operation, vector in (("query_embedding", [1.0, 0.0]), ("document_embedding", [0.0, 1.0])):
            connection.execute(
                "INSERT INTO responses VALUES(?,?)",
                (
                    json.dumps({"operation": operation, "request": {"text": "TEST question"}}),
                    json.dumps(vector),
                ),
            )
    cache = CachedQueries(path)
    with pytest.raises(ValueError, match="source_embedding_disabled"):
        cache.embed_source(object())
    assert cache.embed_query("TEST question") == (1.0, 0.0)
    with pytest.raises(ValueError, match="cache_miss"):
        cache.embed_query("TEST missing")
    with pytest.raises(ValueError, match="cache_miss"):
        cache.raise_if_missing()


def test_shifted_pairs_are_distinct_and_stay_in_their_shard():
    ids = [f"TEST-{i}" for i in range(6)]
    pairs = shifted_pairs(ids, (0, 1, 2, 3, 4, 6, 7))
    assert len(pairs) == len(set(pairs)) == 24
    assert all(a != b and a in ids and b in ids for a, b in pairs)
    assert len(shifted_pairs(ids, (1, 2, 3, 4))) == 24


def test_store_lookup_reports_unreadable_truth_instead_of_zero_stores(tmp_path, monkeypatch):
    from cache import digest

    root = tmp_path / "shard-0-of-1/stores-TEST" / digest("TEST-question")[:20]
    (root / "truth").mkdir(parents=True)
    (root / "truth/memory.sqlite3").write_bytes(b"SQLite format 3\x00")
    original = Path.open

    def denied(path, *args, **kwargs):
        if path == root / "truth/memory.sqlite3":
            raise PermissionError("TEST denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(PermissionError):
        store_for(tmp_path, "TEST-question")


def test_replay_opens_only_a_copy_and_scores_selected_evidence(tmp_path):
    ports = Ports(RequestCache(tmp_path / "cache.sqlite3"))
    # Cache filenames include generation and scope digests. Keep this fixture
    # short on Windows while remaining under the runner's owned boundary.
    store_root = Path(os.environ["SCOPE_RECALL_TEST_BOUNDARY_PARENT"]) / "replay-stores"
    source = store_root / "original"
    turn = Turn("TEST-turn", "TEST-session", "user", "TEST telescope color amber", "2020-01-01T00:00:00Z", True)
    case = Case(
        "TEST-question", "single-session-user", "TEST telescope color", turn.at, "amber", (turn.session,), (turn,)
    )
    lab = Lab(source, ports, now=turn.at)
    try:
        lab.capture(turn)
        assert lab.settle()["status"] == "complete"
    finally:
        lab.close()
    before = (source / "truth/memory.sqlite3").read_bytes()
    copy_store(source, store_root / "copy")
    replayed = Lab(store_root / "copy", ports, now=case.at, existing=True)
    try:
        prepare_vectors(replayed)
        row = measure(replayed, case, source_metadata(replayed))
        assert row["retrieval"]["turn_recall"] == 1.0
        assert sum(row["channels"].values()) == row["items"]
        assert summarize([row])["all"]["session_recall"] == 1.0
    finally:
        replayed.close()
    assert (source / "truth/memory.sqlite3").read_bytes() == before


@pytest.mark.parametrize("kind,source", [("event", "background"), ("procedure", "claim_lexical")])
def test_replay_counts_final_channels_for_background_and_procedure_items(kind, source):
    from scope_recall.core.retrieval import CandidateRef

    turn = Turn("TEST-turn", "TEST-session", "user", "TEST amber", "2020-01-01T00:00:00Z", True)
    case = Case("TEST-question", "synthetic", "TEST color", turn.at, "amber", (turn.session,), (turn,))
    ref = "event-TEST" if kind == "event" else "claim-TEST"
    candidate = CandidateRef("event" if kind == "event" else "claim", ref, 1, source)
    pipeline = SimpleNamespace(
        _fuse_candidates=lambda candidates, seen: (),
        search=lambda context: SimpleNamespace(candidates=(candidate,)),
    )
    packet = {"items": [{"kind": kind, "ref": ref, "revision": 1}], "gaps": []}

    def recall(question):
        pipeline.search(None)
        return packet, 0.0

    lab = SimpleNamespace(
        core=SimpleNamespace(recall_pipeline=pipeline),
        recall=recall,
        retrieved_sources=lambda packet: ({turn.key}, {turn.session}),
    )
    row = measure(lab, case, {ref: (turn.key, turn.session, turn.role)})
    assert row["channels"] == {source: 1}
