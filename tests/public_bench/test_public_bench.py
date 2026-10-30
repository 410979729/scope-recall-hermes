"""Offline contracts for public-data adapters, cache fencing and statistics."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Standalone runners and pytest share these helpers without installing them in
# the product wheel; the explicit test-directory path is established above.
from cache import RequestCache, digest  # noqa: E402
from cases import LOCOMO_CATEGORIES, Case, Turn, fixed_splits, longmemeval  # noqa: E402
from lab import Lab  # noqa: E402
from paired_statistics import holm, paired_binary  # noqa: E402
from ports import Ports, normalized_messages  # noqa: E402
from run import alternative_context  # noqa: E402
from translate import validate  # noqa: E402


def _question():
    turn = Turn(
        "TEST-turn", "TEST-session", "user", "TEST orchard color amber remains stable.", "2020-01-01T00:00:00Z", True
    )
    return Case(
        "TEST-question", "single-session-user", "What color?", "2020-02-01T00:00:00Z", "amber", (turn.session,), (turn,)
    )


def _stub_lab(monkeypatch, *, status="complete"):
    import run

    class StubLab:
        def __init__(self, *args, **kwargs):
            pass

        def capture(self, turn):
            return SimpleNamespace(durability="persisted")

        def settle(self):
            return {"status": status}

        def recall(self, case):
            return {"items": []}, 0

        def retrieved_sources(self, packet):
            return set(), set()

        def close(self):
            pass

    monkeypatch.setattr(run, "Lab", StubLab)


def _data_manifest(root):
    root.mkdir()
    records = {}
    for name, filename in (
        ("longmemeval", "longmemeval_s_cleaned.json"),
        ("longmemeval_judge", "lme-evaluate-qa.py.txt"),
    ):
        path = root / filename
        path.write_text("[]", encoding="utf-8")
        records[name] = {"file": filename, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "revision": "TEST"}
    (root / "datasets-manifest.json").write_text(json.dumps(records), encoding="utf-8")


def _sample(root):
    import sample_store

    root.mkdir()
    original = sample_store.create(root)
    (root / "sample.json").write_text(json.dumps(original), encoding="utf-8")
    return sample_store, Path(original["path"])


def test_split_has_fixed_disjoint_question_ids_and_is_order_independent():
    cases = [Case(str(index), "category", "question", "2020", "gold", (), ()) for index in range(500)]
    splits = fixed_splits(cases)
    assert splits == fixed_splits(list(reversed(cases)))
    sets = [set(splits[name]) for name in ("dev", "regression", "sealed")]
    assert list(map(len, sets)) == [50, 150, 300]
    assert len(set.union(*sets)) == 500
    assert all(not sets[a] & sets[b] for a, b in ((0, 1), (0, 2), (1, 2)))


def test_cache_remaps_refs_and_refuses_uncached_requests(tmp_path):
    cache = RequestCache(tmp_path / "requests.sqlite3")
    request = {
        "sources": [{"ref": "event-one", "revision": 2, "role": "user", "at": "2020", "chunk": 3}],
        "candidate": {"evidence_refs": ["event-one@2"]},
    }
    response = {"source_refs": ["event-one@2"], "quote": "unchanged prose"}
    assert cache.call("propose", request, {"weights": "a"}, {"event-one": "logical-1"}, lambda: response) == response
    second = deepcopy(request)
    second["sources"][0]["ref"] = "event-two"
    second["candidate"]["evidence_refs"] = ["event-two@2"]
    replay = RequestCache(cache.path, only=True)
    assert replay.call("propose", second, {"weights": "a"}, {"event-two": "logical-1"}, None)["source_refs"] == [
        "event-two@2"
    ]
    for field, value in (("role", "assistant"), ("at", "2021"), ("revision", 3), ("chunk", 4)):
        changed = deepcopy(second)
        changed["sources"][0][field] = value
        with pytest.raises(RuntimeError, match="cache_miss"):
            replay.call("propose", changed, {"weights": "a"}, {"event-two": "logical-1"}, None)
    with pytest.raises(RuntimeError, match="cache_miss"):
        replay.call("propose", second, {"weights": "b"}, {"event-two": "logical-1"}, None)


def test_query_embedding_cache_never_reuses_document_encoding(tmp_path):
    ports = Ports(RequestCache(tmp_path / "requests.sqlite3"))
    ports.embedder.embed_text("TEST synthetic text")
    ports.embedder.embed_query("TEST synthetic text")
    assert ports.cache.misses == 2
    assert len(ports.embedder.embed_query("TEST synthetic text")) == 3072
    assert ports.cache.hits == 1


def test_formatted_request_refs_are_normalized_and_source_prose_is_preserved():
    messages = [
        {"role": "system", "content": "TEST system prompt"},
        {"role": "user", "content": json.dumps({"source_ref": "event-one", "content": "event-one@2"})},
    ]
    normalized = normalized_messages(messages, {"event-one": "logical-one"})
    assert normalized[0] == messages[0]
    content = json.loads(normalized[1]["content"])
    assert content == {"source_ref": "logical-one", "content": "event-one@2"}


def test_alternative_baselines_select_their_own_context_without_consolidation(tmp_path):
    ports = Ports(RequestCache(tmp_path / "requests.sqlite3"))
    turns = (
        Turn("a", "s1", "user", "TEST telescope amber", "2020", True),
        Turn("b", "s2", "assistant", "TEST telescope copper", "2021"),
    )
    case = Case("TEST-case", "single-session-user", "TEST telescope", "2022", "amber", ("s1",), turns)
    for method in ("full-context", "naive-rag"):
        context, score, _ = alternative_context(case, ports, method)
        assert "amber" in context and "answer" not in context
        assert score["turn_recall_all"] and score["session_recall_all"]
    assert set(ports.cache.operations) == {"query_embedding", "document_embedding"}


def test_live_routes_require_exact_explicit_approval(tmp_path):
    config = {
        "weights": "TEST",
        "chat": {"endpoint": "http://127.0.0.1:1"},
        "embedding": {"endpoint": "http://127.0.0.1:2"},
    }
    with pytest.raises(ValueError, match="endpoint_not_explicitly_approved"):
        Ports(RequestCache(tmp_path / "requests.sqlite3"), config, approved=(), ledger_root=tmp_path)


def test_longmemeval_keeps_assistant_role_and_answer_labels(tmp_path):
    row = {
        "question_id": "TEST_abs",
        "question_type": "single-session-user",
        "question": "TEST question",
        "question_date": "2023/05/20 (Sat) 03:00",
        "answer": "Unknown",
        "answer_session_ids": [],
        "haystack_dates": ["2023/05/20 (Sat) 02:00"],
        "haystack_session_ids": ["session"],
        "haystack_sessions": [[{"role": "assistant", "content": "TEST answer", "has_answer": True}]],
    }
    path = tmp_path / "synthetic.json"
    path.write_text(json.dumps([row]), encoding="utf-8")
    case = longmemeval(path)[0]
    assert case.category == "abstention"
    assert case.turns[0].role == "assistant" and case.turns[0].answer
    assert LOCOMO_CATEGORIES == {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop", 5: "adversarial"}


def test_translation_validation_preserves_metadata_and_evidence():
    source = {
        "question_id": "TEST",
        "question": "What color?",
        "answer": "Blue",
        "answer_session_ids": ["s"],
        "question_date": "2020",
        "haystack_dates": ["2019"],
        "haystack_session_ids": ["s"],
        "haystack_sessions": [[{"role": "user", "content": "The color is blue.", "has_answer": True}]],
    }
    translated = deepcopy(source)
    translated.update(
        question="什么颜色？",
        answer="蓝色",
        alignments=[{"session_id": "s", "turn_index": 0, "source_quote": "blue", "translated_quote": "蓝色"}],
    )
    translated["haystack_sessions"][0][0]["content"] = "颜色是蓝色。"
    assert validate(source, translated) == []
    changed = deepcopy(translated)
    changed["haystack_sessions"][0][0]["role"] = "assistant"
    assert "turn_metadata:s:0" in validate(source, changed)
    changed = deepcopy(translated)
    changed["alignments"][0]["translated_quote"] = "红色"
    assert "target_alignment:s:0" in validate(source, changed)


def test_paired_statistics_require_identical_ids_and_correct_multiplicity():
    before = {str(index): False for index in range(12)}
    after = dict.fromkeys(before, True)
    result = paired_binary(before, after)
    assert result["delta"] == 1 and result["p_value"] < 0.001
    assert result["delta_ci95"] == [1, 1]
    assert holm({"one": 0.001, "two": 0.04, "three": 0.5}) == {"one": True, "two": False, "three": False}
    with pytest.raises(ValueError, match="same nonempty"):
        paired_binary(before, {})


def test_real_worker_settles_all_fake_stages_and_zero_progress_is_not_terminal(tmp_path, monkeypatch):
    ports = Ports(RequestCache(tmp_path / "requests.sqlite3"))
    store = Lab(tmp_path / "isolated", ports, now="2026-01-01T00:00:00Z")
    try:
        store.capture(
            Turn(
                "TEST-pipeline",
                "TEST-session",
                "user",
                "TEST-orchard color amber remains stable.",
                "2026-01-01T00:00:00Z",
            )
        )
        result = store.settle(max_rounds=32, wall_seconds=20)
        assert result["status"] == "complete"
        from scope_recall.core.claims import Qualification

        source = store.core.source_by_event_key(store.context, "TEST-pipeline", 1)
        proposal = {
            "kind": "fact",
            "subject": "TEST-orchard",
            "predicate": "remains",
            "value_text": "stable",
            "conditions": [],
            "statement_kind": "assertion",
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_to": None,
            "evidence_spans": [{"source_ref": source.ref, "source_revision": 1, "quote": source.event["content"]}],
        }
        with store.core.storage.write(store.context) as transaction:
            saved = transaction.claims.append(
                "public",
                proposal,
                Qualification("proposed", "inferred_suggestion", "TEST-candidate"),
                recorded_at=store.clock.utc_now(),
            )
            transaction.candidates.register(saved.ref, saved.revision, observed_at=store.clock.utc_now())
        result = store.settle(max_rounds=32, wall_seconds=20)
        assert result["status"] == "complete"
        assert any(
            kind == "evaluate_candidate" and state == "done" and count for kind, state, _, count in result["work"]
        )
        store.capture(
            Turn(
                "TEST-stalled",
                "TEST-session",
                "user",
                "TEST-meadow color silver remains stable.",
                "2026-01-02T00:00:00Z",
            )
        )
        from types import SimpleNamespace

        monkeypatch.setattr(store.core, "drain_worker", lambda *a, **kw: SimpleNamespace(processed=0))
        stalled = store.settle(max_rounds=2, wall_seconds=20)
        assert stalled["status"] == "round_limit"
        assert any(state == "pending" for _, state, _, _ in stalled["work"])
        ports.model = None
        assert store.settle(max_rounds=2, wall_seconds=20)["status"] == "unavailable"
    finally:
        store.close()


def test_cache_only_worker_miss_cannot_become_an_unavailable_result(tmp_path):
    ports = Ports(RequestCache(tmp_path / "requests.sqlite3", only=True))
    store = Lab(tmp_path / "store", ports, now=_question().at)
    try:
        store.capture(_question().turns[0])
        with pytest.raises(RuntimeError, match="cache_miss"):
            store.settle(max_rounds=2)
    finally:
        store.close()


def test_cache_only_query_miss_cannot_become_a_retrieval_gap(tmp_path, monkeypatch):
    ports = Ports(RequestCache(tmp_path / "requests.sqlite3", only=True))
    store = Lab(tmp_path / "store", ports, now=_question().at)

    def swallowed_query(*args, **kwargs):
        try:
            ports.embedder.embed_query("TEST uncached query")
        except RuntimeError:
            return {"items": [], "gaps": ["vector_unavailable"]}
        raise AssertionError("an empty cache cannot answer this query")

    monkeypatch.setattr(store.core, "recall_packet", swallowed_query)
    try:
        with pytest.raises(RuntimeError, match="cache_miss"):
            store.recall(_question())
    finally:
        store.close()


def test_cache_only_main_rejects_a_swallowed_miss(tmp_path, monkeypatch):
    import run

    data, output = tmp_path / "data", tmp_path / "output"
    _data_manifest(data)
    monkeypatch.setattr(run, "longmemeval", lambda _: [replace(_question(), id=str(i)) for i in range(500)])
    monkeypatch.setattr(run, "official_prompts", lambda _: {"default": "{}{}{}"})

    def swallowed(case, *, ports, **kwargs):
        try:
            ports.cache.call("query_embedding", {}, {}, {}, None)
        except RuntimeError:
            pass
        return {"id": case.id, "category": case.category, "status": "complete", "judge_correct": None}

    monkeypatch.setattr(run, "run_case", swallowed)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run.py",
            "--data",
            str(data),
            "--output",
            str(output),
            "--fake",
            "--cache-only",
            "--retrieval-only",
            "--limit",
            "1",
        ],
    )
    with pytest.raises(RuntimeError, match="cache_miss"):
        run.main()
    assert not (output / "results.json").exists()


@pytest.mark.parametrize("status", ["failed", "unavailable", "wall_limit", "round_limit"])
def test_unfinished_questions_never_answer_or_judge(tmp_path, monkeypatch, status):
    import run

    _stub_lab(monkeypatch, status=status)
    ports = Ports(RequestCache(tmp_path / "requests.sqlite3"))
    row = run.run_case(
        _question(),
        root=tmp_path,
        ports=ports,
        prompts={"default": "{}{}{}"},
        method="scope-recall",
        retrieval_only=False,
    )
    assert row["answer"] is None and row["judge_correct"] is None
    assert "answer" not in ports.cache.operations and "judge" not in ports.cache.operations
    assert row["status"] == "incomplete"


def test_longmemeval_sorts_sessions_stably_without_reordering_turns(tmp_path):
    row = {
        "question_id": "TEST",
        "question_type": "multi-session",
        "question": "TEST?",
        "question_date": "2023/05/20 (Sat) 03:00",
        "answer": "TEST",
        "answer_session_ids": [],
        "haystack_session_ids": ["late", "early-a", "early-b"],
        "haystack_dates": ["2023/05/20 (Sat) 02:00", "2023/05/19 (Fri) 02:00", "2023/05/19 (Fri) 02:00"],
        "haystack_sessions": [
            [{"role": "user", "content": "late"}],
            [{"role": "assistant", "content": "first"}, {"role": "user", "content": "second"}],
            [{"role": "user", "content": "tie"}],
        ],
    }
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps([row]), encoding="utf-8")
    assert [turn.key for turn in longmemeval(path)[0].turns] == ["early-a:0", "early-a:1", "early-b:0", "late:0"]


def test_settle_checks_the_deadline_after_a_batch_and_passes_remaining_budget(monkeypatch):
    import lab

    current, budgets = [0.0], []
    monkeypatch.setattr(lab.time, "monotonic", lambda: current[0])
    monkeypatch.setattr(lab.time, "perf_counter", lambda: current[0])

    def batch(*args, **kwargs):
        budgets.append(kwargs["remaining_seconds"])
        current[0] += 50
        return SimpleNamespace(processed=1)

    store = object.__new__(Lab)
    store.core, store.context = SimpleNamespace(drain_worker=batch), None
    store.ports = SimpleNamespace(model=None, cache=SimpleNamespace(raise_if_missing=lambda: None))
    store.embed = store.purge = None
    store.state = lambda: {"work": [], "candidates": [], "phases": {}, "next_at": None}
    result = store.settle(wall_seconds=1)
    assert result["status"] == "wall_limit"
    assert 0 < budgets[0] <= 1


def test_lab_deadlines_use_real_time_while_business_dates_are_injected(tmp_path):
    store = Lab(tmp_path / "store", Ports(RequestCache(tmp_path / "cache.sqlite3")), now="2020-01-01T00:00:00Z")
    try:
        assert store.clock.monotonic() > 1
        assert store.clock.utc_now() == "2020-01-01T00:00:00Z"
    finally:
        store.close()


def test_compatibility_fingerprint_checks_actual_source_content(tmp_path):
    sample, path = _sample(tmp_path / "sample")
    before = sample.fingerprint(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE source_events SET content='' WHERE content IS NOT NULL")
    assert sample.fingerprint(path)["source_events"] != before["source_events"]


def test_compatibility_fingerprint_checks_required_work_states(tmp_path):
    sample, path = _sample(tmp_path / "sample")
    before = sample.fingerprint(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE work_items SET state='done' WHERE state='failed'")
    assert sample.fingerprint(path) != before


def test_compatibility_rejects_integrity_errors(tmp_path, monkeypatch):
    sample, _ = _sample(tmp_path / "sample")
    original = sample.fingerprint

    def corrupt(path):
        result = original(path)
        if Path(path).name == "memory.sqlite3":
            result["integrity"] = "TEST-corrupt-page"
        return result

    monkeypatch.setattr(sample, "fingerprint", corrupt)
    assert sample.verify(tmp_path / "sample")["passed"] is False


def test_each_compatibility_verification_uses_the_original_snapshot(tmp_path):
    sample, path = _sample(tmp_path / "sample")
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=1111")
    first = sample.verify(tmp_path / "sample")
    second = sample.verify(tmp_path / "sample")
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1111
    assert first["passed"] and second["passed"]
    assert first["validation_path"] != second["validation_path"]


@pytest.mark.parametrize("method", ["scope-recall", "full-context", "naive-rag"])
def test_question_date_is_in_each_answer_prompt_and_its_cache_key(tmp_path, monkeypatch, method):
    import run

    _stub_lab(monkeypatch)
    ports = Ports(RequestCache(tmp_path / "requests.sqlite3"))
    first = run.run_case(
        _question(), root=tmp_path, ports=ports, prompts={"default": "{}{}{}"}, method=method, retrieval_only=False
    )
    second_case = replace(_question(), at="2020-03-01T00:00:00Z")
    second = run.run_case(
        second_case, root=tmp_path, ports=ports, prompts={"default": "{}{}{}"}, method=method, retrieval_only=False
    )
    with sqlite3.connect(ports.cache.path) as connection:
        requests = [
            row[0]
            for row in connection.execute('SELECT request FROM responses WHERE request LIKE \'%"operation":"answer"%\'')
        ]
    assert any(_question().at in request for request in requests)
    assert any(second_case.at in request for request in requests)
    assert first["answer_prompt_sha256"] != second["answer_prompt_sha256"]


@pytest.mark.parametrize("filename", ["longmemeval_s_cleaned.json", "lme-evaluate-qa.py.txt"])
def test_runner_checks_frozen_input_hashes_before_loading(tmp_path, monkeypatch, filename):
    import run

    data = tmp_path / "data"
    _data_manifest(data)
    (data / filename).write_text("changed", encoding="utf-8")
    monkeypatch.setattr(run, "longmemeval", lambda _: pytest.fail("unverified data reached the adapter"))
    monkeypatch.setattr(sys, "argv", ["run.py", "--data", str(data), "--output", str(tmp_path / "output"), "--fake"])
    with pytest.raises(ValueError, match="sha256"):
        run.main()


def test_downloader_refuses_an_existing_unverified_or_changed_file(tmp_path):
    from download_data import acquire

    path = tmp_path / "corpus.json"
    path.write_text("TEST local content", encoding="utf-8")
    with pytest.raises(ValueError, match="unverified|sha256"):
        acquire("https://example.invalid/corpus.json", path)
    records = {
        "corpus": {
            "file": path.name,
            "url": "https://example.invalid/corpus.json",
            "sha256": hashlib.sha256(b"frozen").hexdigest(),
        }
    }
    (tmp_path / "datasets-manifest.json").write_text(json.dumps(records), encoding="utf-8")
    with pytest.raises(ValueError, match="sha256"):
        acquire(records["corpus"]["url"], path)


def test_local_scores_cover_refusal_and_normalized_fact_answers():
    import scoring

    refusal = scoring.score_local("locomo", "adversarial", "unused distractor", "It is not mentioned.")
    fact = scoring.score_local(
        "factconsolidation",
        "multi-hop",
        json.dumps([["The Hague"], ["Den Haag"]]),
        "Reasoning first\nAnswer: It is THE HAGUE.",
    )
    assert refusal["score"] == 1 and refusal["metric"] == "abstention"
    assert fact["score"] == 1 and fact["metric"] == "substring_exact_match"
    assert scoring.score_local("unknown", "unknown", "gold", "answer")["status"] == "unsupported"


@pytest.mark.parametrize("category", ["single-hop", "temporal", "open-domain", "multi-hop"])
def test_locomo_lexical_scores_or_explicit_missing_dependency(category):
    import scoring

    result = scoring.score_local(
        "locomo", category, "running cats; ignored" if category == "open-domain" else "running cats", "A running cat"
    )
    if importlib.util.find_spec("nltk") is None:
        assert result["status"] == "unsupported" and result["reason"] == "nltk_required"
    else:
        assert result["status"] == "scored" and result["score"] == 1


def test_score_summary_counts_only_selected_complete_questions():
    import scoring

    rows = [
        {
            "id": "one",
            "category": "single-hop",
            "evaluation": {"status": "scored", "metric": "substring_exact_match", "score": 1},
        }
    ]
    result = scoring.summarize(rows, [{"id": "two", "category": "single-hop"}])
    assert result["status"] == "incomplete" and result["score"] is None
    assert result["categories"]["single-hop"]["selected"] == 2
    assert result["categories"]["single-hop"]["scored"] == 1
    assert result["categories"]["single-hop"]["mean_score"] == 1


@pytest.mark.parametrize(
    "field,value",
    [("platform", "different"), ("cpu", "different"), ("logical_cpus", 64), ("physical_memory_bytes", 999)],
)
def test_frozen_performance_rejects_changed_hardware(tmp_path, monkeypatch, field, value):
    import performance

    facts = {
        "platform": "TEST",
        "cpu": "TEST",
        "logical_cpus": 2,
        "physical_memory_bytes": 1000,
        "disk": {"total": 3000, "used": 1000, "free": 2000},
        "observed_at": "first",
    }
    monkeypatch.setattr(performance, "machine", lambda _: deepcopy(facts))
    performance.freeze(tmp_path)
    facts[field] = value
    with pytest.raises(ValueError, match="frozen conditions differ"):
        performance.freeze(tmp_path)


def test_partition_protocol_name_is_neutral():
    cases = [replace(_question(), id=str(i)) for i in range(500)]
    assert fixed_splits(cases)["method"] == "sha256(memory-evaluation-split/1:42:question_id)"


def test_paired_comparison_reads_result_files_and_rejects_different_questions(tmp_path):
    import paired_statistics

    before = {
        "mode": "live",
        "dataset": "longmemeval",
        "models": {},
        "dataset_manifest": {},
        "results": [{"id": str(i), "category": "single-session-user", "judge_correct": False} for i in range(12)],
    }
    after = deepcopy(before)
    for row in after["results"]:
        row["judge_correct"] = True
    paths = [tmp_path / name for name in ("before.json", "after.json")]
    for path, result in zip(paths, (before, after), strict=True):
        path.write_text(json.dumps(result), encoding="utf-8")
    result = paired_statistics.compare_reports(*paths)
    assert result["overall"]["n"] == 12 and result["overall"]["delta"] == 1
    assert result["categories"]["single-session-user"]["delta"] == 1
    after["results"].pop()
    paths[1].write_text(json.dumps(after), encoding="utf-8")
    with pytest.raises(ValueError, match="question"):
        paired_statistics.compare_reports(*paths)


def test_documented_commands_include_cached_splits_and_file_comparison():
    text = (HERE.parents[1] / "docs/benchmarks.md").read_text(encoding="utf-8")
    assert all(
        token in text
        for token in (
            "--cache ",
            "--cache-only",
            "--split regression",
            "--split sealed",
            "paired_statistics.py --before",
        )
    )


def test_incomplete_cli_report_excludes_answers_and_full_scores(tmp_path, monkeypatch):
    import run

    data, output = tmp_path / "data", tmp_path / "output"
    _data_manifest(data)
    _stub_lab(monkeypatch, status="failed")
    monkeypatch.setattr(run, "longmemeval", lambda _: [replace(_question(), id=str(i)) for i in range(500)])
    monkeypatch.setattr(run, "official_prompts", lambda _: {"default": "{}{}{}"})
    monkeypatch.setattr(sys, "argv", ["run.py", "--data", str(data), "--output", str(output), "--fake", "--limit", "1"])
    with pytest.raises(SystemExit) as error:
        run.main()
    assert error.value.code == 1
    report = json.loads((output / "results.json").read_text(encoding="utf-8"))
    assert report["results"] == [] and len(report["incomplete_results"]) == 1
    assert report["scoring"]["status"] == "incomplete" and report["accuracy"] is None
    assert report["cache"]["operations"] == {}


@pytest.mark.parametrize(
    "dataset,category,gold,metric",
    [
        ("locomo", "adversarial", "", "abstention"),
        ("factconsolidation", "single-hop", '["amber"]', "substring_exact_match"),
    ],
)
def test_runner_uses_local_dataset_scoring_without_a_model_judge(tmp_path, dataset, category, gold, metric):
    import run

    ports = Ports(RequestCache(tmp_path / "cache.sqlite3"))
    case = replace(_question(), category=category, gold=gold)
    row = run.run_case(
        case, root=tmp_path, ports=ports, prompts={}, method="full-context", retrieval_only=False, dataset=dataset
    )
    assert row["evaluation"]["status"] == "scored" and row["evaluation"]["metric"] == metric
    assert set(ports.cache.operations) == {"answer"}


def test_missing_lexical_dependency_is_explicitly_unsupported(monkeypatch):
    import scoring

    def missing(_):
        raise ImportError("TEST missing benchmark dependency")

    monkeypatch.setattr(scoring, "_stem_tokens", missing)
    result = scoring.score_local("locomo", "single-hop", "TEST gold", "TEST answer")
    assert result == {"status": "unsupported", "metric": "stemmed_f1", "reason": "nltk_required"}


def test_performance_freeze_allows_only_volatile_observation_changes(tmp_path, monkeypatch):
    import performance

    facts = {
        "platform": "TEST",
        "cpu": "TEST",
        "logical_cpus": 2,
        "physical_memory_bytes": 1000,
        "disk": {"total": 3000, "used": 1000, "free": 2000},
        "observed_at": "first",
    }
    monkeypatch.setattr(performance, "machine", lambda _: deepcopy(facts))
    first = performance.freeze(tmp_path)
    facts.update(observed_at="second", disk={"total": 3000, "used": 1500, "free": 1500})
    assert performance.freeze(tmp_path) == first
    facts["disk"]["total"] += 1
    with pytest.raises(ValueError, match="frozen conditions differ"):
        performance.freeze(tmp_path)


def test_paired_cli_writes_offline_retrieval_comparisons(tmp_path):
    import paired_statistics

    paths = [tmp_path / name for name in ("before.json", "after.json")]
    for path, value in zip(paths, (False, True), strict=True):
        report = {
            "mode": "fake-smoke",
            "results": [
                {"id": "TEST-one", "category": "single-session-user", "retrieval": {"turn_recall_all": value}},
                {"id": "TEST-unlabeled", "category": "abstention", "retrieval": {"turn_recall_all": None}},
            ],
        }
        path.write_text(json.dumps(report), encoding="utf-8")
    output = tmp_path / "comparison.json"
    paired_statistics.main(
        ["--before", str(paths[0]), "--after", str(paths[1]), "--output", str(output), "--metric", "turn_recall_all"]
    )
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["overall"]["n"] == 1 and result["overall"]["delta"] == 1
    assert result["unlabeled_question_ids"] == ["TEST-unlabeled"]
    with pytest.raises(ValueError, match="complete_live"):
        paired_statistics.compare_reports(*paths)


def test_local_scoring_does_not_claim_a_model_judge_prompt(tmp_path, monkeypatch):
    import run

    data, output = tmp_path / "data", tmp_path / "output"
    data.mkdir()
    source = data / "Conflict_Resolution-00000-of-00001.parquet"
    source.write_bytes(b"TEST adapter fixture")
    manifest = {
        "memoryagentbench": {
            "files": [{"file": source.name, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]
        }
    }
    (data / "datasets-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(
        run, "factconsolidation", lambda _: [replace(_question(), category="single-hop", gold='["amber"]')]
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run.py",
            "--data",
            str(data),
            "--output",
            str(output),
            "--dataset",
            "factconsolidation",
            "--method",
            "full-context",
            "--fake",
        ],
    )
    run.main()
    report = json.loads((output / "results.json").read_text(encoding="utf-8"))
    assert report["official_judge_prompts_sha256"] is None
    assert report["results"][0]["evaluation"]["metric"] == "substring_exact_match"
