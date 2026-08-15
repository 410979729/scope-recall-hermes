"""Regression tests for the reproducible LoCoMo benchmark runner."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    path = ROOT / "scripts" / "locomo_benchmark_lib.py"
    spec = importlib.util.spec_from_file_location("locomo_benchmark_lib", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_runner_module():
    path = ROOT / "scripts" / "benchmark.locomo.py"
    spec = importlib.util.spec_from_file_location("benchmark_locomo", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _benchmark_question() -> dict:
    return {
        "question_id": "sample-1:q0000",
        "sample_id": "sample-1",
        "question_index": 0,
        "question": "What is the canonical question?",
        "gold_answer": "canonical answer",
        "gold_evidence_ids": [],
        "category_id": 1,
        "category": "multi-hop",
        "current_date": "",
    }


def _retrieval_artifact(question: dict) -> dict:
    return {
        **question,
        "query_variants": [question["question"]],
        "retrieval_latency_seconds": 0.1,
        "results": [],
        "retrieval_metrics": {},
        "funnel_trace": {},
        "evidence_set_trace": {},
    }


def _scored_artifact(question: dict) -> dict:
    return {
        **question,
        "attempt_round": 1,
        "answer_model": "answerer",
        "judge_model": "judge",
        "evidence_mode": "retrieved",
        "retrieval_metrics": {},
        "query_variants": [question["question"]],
        "started_at": "2026-08-10T00:00:00+00:00",
        "status": "scored",
        "correct": True,
        "predicted_answer": "canonical answer",
        "judge_label": "CORRECT",
        "answer_latency_seconds": 0.1,
        "judge_latency_seconds": 0.1,
        "completed_at": "2026-08-10T00:00:01+00:00",
    }


def test_runner_requires_explicit_external_paths(tmp_path, monkeypatch) -> None:
    module = _load_runner_module()
    run_dir = tmp_path / "run"

    monkeypatch.setattr(
        sys,
        "argv",
        ["benchmark.locomo.py", "--run-dir", str(run_dir)],
    )
    with pytest.raises(SystemExit) as exc_info:
        module.parse_args()
    assert exc_info.value.code == 2

    dataset = tmp_path / "locomo.json"
    hermes_root = tmp_path / "hermes-agent"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark.locomo.py",
            "--dataset",
            str(dataset),
            "--run-dir",
            str(run_dir),
            "--hermes-agent-root",
            str(hermes_root),
            "--phase",
            "retrieve",
        ],
    )
    args = module.parse_args()
    assert args.dataset == dataset
    assert args.hermes_agent_root == hermes_root
    assert args.auth_path is None
    with pytest.raises(ValueError, match="outside"):
        module.external_run_directory(ROOT / "benchmark-output")
    assert module.external_run_directory(run_dir) == run_dir.resolve()

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "hermes-tianji" not in source
    source_separator = "\\" * 2
    forbidden_root = source_separator.join(("E:", "Agents", "runtime"))
    assert forbidden_root not in source


def test_model_route_fingerprint_binds_nonsecret_identity_without_leaking(tmp_path) -> None:
    module = _load_runner_module()
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "credential_pool": {
                    "openai-codex": [
                        {
                            "access_token": "secret-token-alpha",
                            "base_url": "https://private.example/codex",
                            "account_id": "account-alpha",
                            "email": "owner@example.test",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    client = module.CodexModelClient(auth)
    first = client.route_fingerprint()
    serialized = json.dumps(first, sort_keys=True)

    assert first["credential_identity_fields"] == ["account_id", "email"]
    assert "secret-token-alpha" not in serialized
    assert "private.example" not in serialized
    assert "owner@example.test" not in serialized

    payload = json.loads(auth.read_text(encoding="utf-8"))
    payload["credential_pool"]["openai-codex"][0]["account_id"] = "account-beta"
    auth.write_text(json.dumps(payload), encoding="utf-8")
    second = client.route_fingerprint()
    assert first["credential_identity_sha256"] != second["credential_identity_sha256"]

    candidate_sha256 = "d" * 64
    manifest = {
        "source": {"candidate_sha256": candidate_sha256},
        "models": {"answerer": "answerer", "judge": "judge", "query_planner": ""},
        "model_execution": {
            "timeout_seconds": 90.0,
            "max_attempts": 6,
            "workers": 8,
            "model_rounds": 3,
        },
    }
    receipt = {
        "schema_version": module.MODEL_ROUTE_SCHEMA_VERSION,
        "created_at": "2026-08-10T00:00:00+00:00",
        "source_candidate_sha256": candidate_sha256,
        "models": dict(manifest["models"]),
        "timeout_seconds": 90.0,
        "max_attempts": 6,
        "workers": 8,
        "model_rounds": 3,
        "route": second,
    }
    assert module.model_route_receipt_is_valid(receipt, manifest) is True
    forged = {**receipt, "route": {**second, "credential_identity_sha256": None}}
    assert module.model_route_receipt_is_valid(forged, manifest) is False
    worker_drift = {**receipt, "workers": 9}
    assert module.model_route_receipt_is_valid(worker_drift, manifest) is False
    round_drift = {**receipt, "model_rounds": 4}
    assert module.model_route_receipt_is_valid(round_drift, manifest) is False
    for field in ("max_attempts", "workers", "model_rounds"):
        typed_drift = {**receipt, field: str(receipt[field])}
        assert module.model_route_receipt_is_valid(typed_drift, manifest) is False
    timeout_type_drift = {**receipt, "timeout_seconds": "90.0"}
    assert module.model_route_receipt_is_valid(timeout_type_drift, manifest) is False


def test_model_client_rejects_call_time_route_identity_drift(
    tmp_path, monkeypatch
) -> None:
    module = _load_runner_module()
    auth = tmp_path / "auth.json"
    entry = {
        "access_token": "token-a",
        "base_url": "https://route-a.example/codex",
        "account_id": "account-a",
    }
    auth.write_text(
        json.dumps({"credential_pool": {"openai-codex": [entry]}}),
        encoding="utf-8",
    )
    unguarded = module.CodexModelClient(auth, max_attempts=1)
    expected_route = unguarded.route_fingerprint()
    guarded = module.CodexModelClient(
        auth,
        max_attempts=1,
        expected_route=expected_route,
    )
    entry["access_token"] = "token-b"
    entry["base_url"] = "https://route-b.example/codex"
    entry["account_id"] = "account-b"
    auth.write_text(
        json.dumps({"credential_pool": {"openai-codex": [entry]}}),
        encoding="utf-8",
    )
    model_called = False

    def fake_model_call(*_args, **_kwargs):
        nonlocal model_called
        model_called = True
        return "ANSWER: should not run"

    from scope_recall import nightly_llm

    monkeypatch.setattr(nightly_llm, "call_codex_responses_llm", fake_model_call)
    with pytest.raises(RuntimeError, match="route identity changed"):
        guarded.complete(model="answerer", system="system", user="user")
    assert model_called is False


def test_model_client_allows_token_refresh_when_route_identity_is_stable(
    tmp_path, monkeypatch
) -> None:
    module = _load_runner_module()
    auth = tmp_path / "auth.json"
    entry = {
        "access_token": "token-a",
        "base_url": "https://route.example/codex",
        "account_id": "account-a",
    }
    auth.write_text(
        json.dumps({"credential_pool": {"openai-codex": [entry]}}),
        encoding="utf-8",
    )
    expected_route = module.CodexModelClient(auth).route_fingerprint()
    client = module.CodexModelClient(
        auth,
        max_attempts=1,
        expected_route=expected_route,
    )
    entry["access_token"] = "token-b"
    auth.write_text(
        json.dumps({"credential_pool": {"openai-codex": [entry]}}),
        encoding="utf-8",
    )

    from scope_recall import nightly_llm

    monkeypatch.setattr(
        nightly_llm,
        "call_codex_responses_llm",
        lambda *_args, **_kwargs: "ANSWER: refreshed",
    )
    assert client.complete(model="answerer", system="system", user="user") == (
        "ANSWER: refreshed"
    )


def test_route_drift_aborts_planner_and_evaluator_instead_of_falling_back(
    tmp_path, monkeypatch
) -> None:
    module = _load_runner_module()
    question = _benchmark_question()

    class DriftingClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def complete(self, **_kwargs):
            raise module.ModelRouteDriftError("model route identity changed after preflight")

    monkeypatch.setattr(module, "CodexModelClient", DriftingClient)
    monkeypatch.setattr(module, "ensure_model_route_receipt", lambda _args: {})
    planner_args = SimpleNamespace(
        run_dir=tmp_path / "planner",
        query_planner_model="planner",
        no_query_variants=False,
        planner_categories="1,2,3",
        auth_path=tmp_path / "auth.json",
        model_timeout=90.0,
        workers=1,
    )
    planner_args.run_dir.mkdir()
    with pytest.raises(module.ModelRouteDriftError):
        module.prepare_query_plans(questions=[question], args=planner_args)

    evaluation_args = SimpleNamespace(
        answer_model="answerer",
        judge_model="judge",
        evidence_mode="retrieved",
        evidence_max_chars=1000,
    )
    with pytest.raises(module.ModelRouteDriftError):
        module.evaluate_question(
            question=_retrieval_artifact(question),
            memory_map={},
            record_index={},
            client=DriftingClient(),
            args=evaluation_args,
            attempt_round=1,
        )


def test_manifest_and_report_do_not_persist_machine_paths(tmp_path) -> None:
    module = _load_runner_module()
    dataset = tmp_path / "locomo.json"
    dataset.write_text("[]\n", encoding="utf-8")
    run_dir = tmp_path / "private" / "run"
    run_dir.mkdir(parents=True)
    args = SimpleNamespace(
        run_dir=run_dir,
        dataset=dataset,
        answer_model="answerer",
        judge_model="judge",
        query_planner_model="",
        categories="1,2,3,4",
        samples="",
        per_category=0,
        max_questions=0,
        ingest_mode="chunks",
        chunk_size=4,
        chunk_overlap=1,
        embedder="local-hash",
        no_query_variants=False,
        planner_categories="1,2,3",
        retrieval_limit=20,
        evidence_max_chars=50000,
        evidence_mode="retrieved",
        sample_workers=1,
        auth_path=None,
        model_timeout=90.0,
        hermes_agent_root=tmp_path / "hermes-agent-source",
        workers=8,
        model_rounds=3,
    )
    dependency_epoch = {
        "head": "hermes-head",
        "head_tree": "0" * 40,
        "dirty": False,
        "index_count": 1,
        "index_sha256": "1" * 64,
        "tracked_count": 1,
        "tracked_worktree_sha256": "2" * 64,
        "untracked_count": 0,
        "untracked_sha256": "3" * 64,
        "candidate_sha256": "4" * 64,
    }
    # The manifest must persist a path-free identity for the Hermes dependency,
    # never the machine-local source root itself.
    setattr(module, "dependency_source_epoch", lambda _root: dependency_epoch)

    manifest = module.ensure_manifest(
        args=args,
        dataset=[],
        questions=[],
        config={},
    )
    report = module.build_report(questions=[], args=args)
    serialized = json.dumps({"manifest": manifest, "report": report})

    assert manifest["dataset"]["name"] == "locomo.json"
    assert "path" not in manifest["dataset"]
    assert manifest["dependencies"]["hermes_agent"] == dependency_epoch
    assert manifest["model_execution"]["workers"] == 8
    assert manifest["model_execution"]["model_rounds"] == 3
    assert "run_dir" not in report
    assert str(tmp_path) not in serialized

    args.model_timeout = 91.0
    with pytest.raises(RuntimeError, match="manifest does not match"):
        module.ensure_manifest(
            args=args,
            dataset=[],
            questions=[],
            config={},
        )

    args.model_timeout = 90.0
    args.workers = 9
    with pytest.raises(RuntimeError, match="manifest does not match"):
        module.ensure_manifest(
            args=args,
            dataset=[],
            questions=[],
            config={},
        )

    args.workers = 8
    args.model_rounds = 4
    with pytest.raises(RuntimeError, match="manifest does not match"):
        module.ensure_manifest(
            args=args,
            dataset=[],
            questions=[],
            config={},
        )

    args.model_rounds = 3
    dependency_epoch["candidate_sha256"] = "5" * 64
    with pytest.raises(RuntimeError, match="manifest does not match"):
        module.ensure_manifest(
            args=args,
            dataset=[],
            questions=[],
            config={},
        )


def test_official_comparability_requires_canonical_retrieved_complete_run(
    monkeypatch,
) -> None:
    module = _load_runner_module()
    category_counts = {1: 2, 2: 1, 3: 1, 4: 1}
    questions = [
        {"question_id": f"q{index}", "category_id": category}
        for index, category in enumerate((1, 1, 2, 3, 4))
    ]
    question_hash = module.hashlib.sha256(
        "\n".join(row["question_id"] for row in questions).encode("utf-8")
    ).hexdigest()
    monkeypatch.setattr(module, "CANONICAL_LOCOMO_DATASET_SHA256", "dataset-hash")
    monkeypatch.setattr(module, "CANONICAL_LOCOMO_QUESTION_IDS_SHA256", question_hash)
    monkeypatch.setattr(module, "CANONICAL_LOCOMO_CATEGORY_COUNTS", category_counts)
    monkeypatch.setattr(module, "CANONICAL_LOCOMO_RETRIEVAL_METRIC_QUESTIONS", 4)
    source_epoch = {
        "head": "1" * 40,
        "head_tree": "2" * 40,
        "dirty": True,
        "index_count": 10,
        "index_sha256": "3" * 64,
        "tracked_count": 10,
        "tracked_worktree_sha256": "4" * 64,
        "untracked_count": 1,
        "untracked_sha256": "5" * 64,
        "candidate_sha256": "6" * 64,
    }
    manifest = {
        "source": source_epoch,
        "dataset": {"sha256": "dataset-hash", "sample_count": 10},
        "question_count": len(questions),
        "question_ids_sha256": question_hash,
        "strategy": {"evidence_mode": "retrieved"},
        "dependencies": {
            "hermes_agent": {
                **source_epoch,
                "candidate_sha256": "a" * 64,
            }
        },
        "model_execution": {
            "timeout_seconds": 90.0,
            "max_attempts": module.MODEL_MAX_ATTEMPTS,
            "workers": 8,
            "model_rounds": 3,
        },
    }
    score = {
        "complete": True,
        "artifact_rows_validated": True,
        "expected_questions": len(questions),
        "scored_questions": len(questions),
        "invalid_questions": 0,
        "missing_questions": 0,
        "unexpected_questions": 0,
    }
    retrieval_rows = [
        {"question_id": row["question_id"]} for row in questions
    ]
    summary = {"50": {"questions": 4}}

    checks = module.official_comparability_checks(
        questions=questions,
        manifest=manifest,
        score=score,
        retrieval_rows=retrieval_rows,
        retrieval_summary=summary,
        model_route_receipt_valid=True,
        retrieval_artifacts_valid=True,
        result_artifacts_valid=True,
        query_plan_artifacts_valid=True,
    )
    assert all(checks.values())

    missing_source = dict(manifest)
    missing_source.pop("source")
    source_checks = module.official_comparability_checks(
        questions=questions,
        manifest=missing_source,
        score=score,
        retrieval_rows=retrieval_rows,
        retrieval_summary=summary,
        model_route_receipt_valid=True,
        retrieval_artifacts_valid=True,
        result_artifacts_valid=True,
        query_plan_artifacts_valid=True,
    )
    assert source_checks["source_epoch_bound"] is False
    assert not all(source_checks.values())

    oracle_manifest = {**manifest, "strategy": {"evidence_mode": "oracle"}}
    oracle_checks = module.official_comparability_checks(
        questions=questions,
        manifest=oracle_manifest,
        score=score,
        retrieval_rows=retrieval_rows,
        retrieval_summary=summary,
        model_route_receipt_valid=True,
        retrieval_artifacts_valid=True,
        result_artifacts_valid=True,
        query_plan_artifacts_valid=True,
    )
    assert oracle_checks["retrieved_evidence_only"] is False
    assert not all(oracle_checks.values())

    incomplete_checks = module.official_comparability_checks(
        questions=questions,
        manifest=manifest,
        score=score,
        retrieval_rows=retrieval_rows[:-1],
        retrieval_summary=summary,
        model_route_receipt_valid=True,
        retrieval_artifacts_valid=True,
        result_artifacts_valid=True,
        query_plan_artifacts_valid=True,
    )
    assert incomplete_checks["retrieval_rows_complete"] is False
    assert not all(incomplete_checks.values())

    missing_dependency = dict(manifest)
    missing_dependency.pop("dependencies")
    dependency_checks = module.official_comparability_checks(
        questions=questions,
        manifest=missing_dependency,
        score=score,
        retrieval_rows=retrieval_rows,
        retrieval_summary=summary,
        model_route_receipt_valid=True,
        retrieval_artifacts_valid=True,
        result_artifacts_valid=True,
        query_plan_artifacts_valid=True,
    )
    assert dependency_checks["hermes_dependency_bound"] is False
    assert not all(dependency_checks.values())

    incomplete_execution = {
        **manifest,
        "model_execution": {
            "timeout_seconds": 90.0,
            "max_attempts": module.MODEL_MAX_ATTEMPTS,
        },
    }
    execution_checks = module.official_comparability_checks(
        questions=questions,
        manifest=incomplete_execution,
        score=score,
        retrieval_rows=retrieval_rows,
        retrieval_summary=summary,
        model_route_receipt_valid=True,
        retrieval_artifacts_valid=True,
        result_artifacts_valid=True,
        query_plan_artifacts_valid=True,
    )
    assert execution_checks["execution_contract_bound"] is False
    assert not all(execution_checks.values())

    typed_execution = {
        **manifest,
        "model_execution": {**manifest["model_execution"], "workers": "8"},
    }
    typed_checks = module.official_comparability_checks(
        questions=questions,
        manifest=typed_execution,
        score=score,
        retrieval_rows=retrieval_rows,
        retrieval_summary=summary,
        model_route_receipt_valid=True,
        retrieval_artifacts_valid=True,
        result_artifacts_valid=True,
        query_plan_artifacts_valid=True,
    )
    assert typed_checks["execution_contract_bound"] is False
    assert not all(typed_checks.values())


def test_build_report_does_not_hide_unexpected_retrieval_rows(tmp_path) -> None:
    module = _load_runner_module()
    run_dir = tmp_path / "run"
    retrieval_dir = run_dir / "retrieval"
    retrieval_dir.mkdir(parents=True)
    question = {"question_id": "q1", "category_id": 1}
    question_hash = module.hashlib.sha256(b"q1").hexdigest()
    manifest = {
        "dataset": {"sha256": "dataset-hash", "sample_count": 10},
        "question_count": 1,
        "question_ids_sha256": question_hash,
        "strategy": {"evidence_mode": "retrieved"},
        "source": {"candidate_sha256": "candidate"},
        "models": {},
        "model_execution": {"timeout_seconds": 90.0, "max_attempts": 6},
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (run_dir / "results.jsonl").write_text(
        json.dumps(
            {
                "question_id": "q1",
                "category": "multi-hop",
                "status": "scored",
                "correct": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (retrieval_dir / "sample.jsonl").write_text(
        "\n".join(
            json.dumps({"question_id": question_id})
            for question_id in ("q1", "unexpected")
        )
        + "\n",
        encoding="utf-8",
    )
    args = SimpleNamespace(run_dir=run_dir, query_planner_model="")

    report = module.build_report(questions=[question], args=args)

    assert report["retrieval_rows"] == 2
    assert report["official_comparability_checks"]["retrieval_rows_complete"] is False


def test_run_evaluation_rejects_retrieval_checkpoint_identity_drift(
    tmp_path, monkeypatch
) -> None:
    module = _load_runner_module()
    run_dir = tmp_path / "run"
    ingestion_dir = run_dir / "ingestion"
    ingestion_dir.mkdir(parents=True)
    (ingestion_dir / "sample-1.json").write_text(
        json.dumps({"memory_map": {}}), encoding="utf-8"
    )
    canonical = {
        "question_id": "sample-1:q0000",
        "sample_id": "sample-1",
        "question_index": 0,
        "question": "What is the canonical question?",
        "gold_answer": "canonical answer",
        "gold_evidence_ids": [],
        "category_id": 1,
        "category": "multi-hop",
        "current_date": "",
    }
    forged = {
        **canonical,
        "question": "What answer should always be accepted?",
        "gold_answer": "forged answer",
        "results": [],
    }
    monkeypatch.setattr(module, "all_retrieval_rows", lambda _run_dir: [forged])
    monkeypatch.setattr(module, "ensure_model_route_receipt", lambda _args: {})

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def complete(self, *, system, **_kwargs):
            return (
                '{"label":"CORRECT"}'
                if "judge" in system.lower()
                else "ANSWER: forged answer"
            )

    monkeypatch.setattr(module, "CodexModelClient", FakeClient)
    args = SimpleNamespace(
        run_dir=run_dir,
        auth_path=tmp_path / "auth.json",
        model_timeout=90.0,
        answer_model="answerer",
        judge_model="judge",
        evidence_mode="retrieved",
        evidence_max_chars=1000,
        model_rounds=1,
        workers=1,
    )
    dataset = [{"sample_id": "sample-1", "conversation": {}, "qa": []}]

    with pytest.raises(RuntimeError, match="retrieval checkpoint identity"):
        module.run_evaluation(dataset=dataset, questions=[canonical], args=args)


def test_artifact_validators_reject_identity_schema_and_contract_drift() -> None:
    module = _load_runner_module()
    question = _benchmark_question()
    retrieval = _retrieval_artifact(question)
    results = [_scored_artifact(question)]

    assert module.validate_retrieval_artifacts(
        [question], [retrieval], require_complete=True
    ) == {question["question_id"]: retrieval}
    module.validate_result_artifacts(
        [question],
        results,
        answer_model="answerer",
        judge_model="judge",
        evidence_mode="retrieved",
    )

    with pytest.raises(ValueError, match="retrieval artifact"):
        module.validate_retrieval_artifacts(
            [question],
            [{**retrieval, "results": "not-a-list"}],
            require_complete=True,
        )
    with pytest.raises(ValueError, match="result artifact"):
        module.validate_result_artifacts(
            [question],
            [{**results[0], "gold_answer": "forged", "evidence_mode": "oracle"}],
            answer_model="answerer",
            judge_model="judge",
            evidence_mode="retrieved",
        )
    with pytest.raises(ValueError, match="result artifact"):
        module.validate_result_artifacts(
            [question],
            [{**results[0], "correct": 1}],
            answer_model="answerer",
            judge_model="judge",
            evidence_mode="retrieved",
        )


def test_query_plan_resume_rejects_duplicate_and_provenance_drift(
    tmp_path, monkeypatch
) -> None:
    module = _load_runner_module()
    question = _benchmark_question()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    forged = {
        "question_id": question["question_id"],
        "sample_id": "wrong-sample",
        "category_id": 4,
        "model": "old-model",
        "model_valid": True,
        "fallback_used": False,
        "variants": ["FORGED GOLD ANSWER"],
        "error": "",
        "latency_seconds": 0.1,
        "completed_at": "2026-08-10T00:00:00+00:00",
    }
    (run_dir / "query-plans.jsonl").write_text(
        json.dumps(forged) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(module, "ensure_model_route_receipt", lambda _args: {})
    args = SimpleNamespace(
        run_dir=run_dir,
        query_planner_model="current-model",
        no_query_variants=False,
        planner_categories="1,2,3",
        auth_path=tmp_path / "auth.json",
        model_timeout=90.0,
        workers=1,
    )

    with pytest.raises(ValueError, match="query-plan artifact"):
        module.prepare_query_plans(questions=[question], args=args)


def test_evaluation_resume_rejects_forged_minimal_scored_result(
    tmp_path, monkeypatch
) -> None:
    module = _load_runner_module()
    question = _benchmark_question()
    run_dir = tmp_path / "run"
    ingestion_dir = run_dir / "ingestion"
    ingestion_dir.mkdir(parents=True)
    (ingestion_dir / "sample-1.json").write_text(
        json.dumps({"memory_map": {}}), encoding="utf-8"
    )
    (run_dir / "results.jsonl").write_text(
        json.dumps(
            {
                "question_id": question["question_id"],
                "status": "scored",
                "correct": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        module,
        "all_retrieval_rows",
        lambda _run_dir: [_retrieval_artifact(question)],
    )
    monkeypatch.setattr(module, "ensure_model_route_receipt", lambda _args: {})
    args = SimpleNamespace(
        run_dir=run_dir,
        auth_path=tmp_path / "auth.json",
        model_timeout=90.0,
        answer_model="answerer",
        judge_model="judge",
        evidence_mode="retrieved",
        evidence_max_chars=1000,
        model_rounds=1,
        workers=1,
    )
    dataset = [{"sample_id": "sample-1", "conversation": {}, "qa": []}]

    with pytest.raises(ValueError, match="result artifact"):
        module.run_evaluation(dataset=dataset, questions=[question], args=args)


def _init_source_epoch_repo(tmp_path: Path, *, attributes: str = "") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=repo, check=True)
    if attributes:
        (repo / ".gitattributes").write_text(attributes, encoding="utf-8", newline="\n")
    (repo / "tracked.py").write_bytes(b"value = 'A'\n")
    subprocess.run(["git", "add", "--all"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    return repo


def test_source_epoch_binds_index_even_when_worktree_masks_staged_content(tmp_path) -> None:
    module = _load_runner_module()
    repo = _init_source_epoch_repo(tmp_path)
    clean = module.dependency_source_epoch(repo)

    (repo / "tracked.py").write_bytes(b"value = 'B'\n")
    subprocess.run(["git", "add", "tracked.py"], cwd=repo, check=True)
    (repo / "tracked.py").write_bytes(b"value = 'A'\n")
    masked = module.dependency_source_epoch(repo)

    assert clean["index_sha256"] != masked["index_sha256"]
    assert clean["tracked_worktree_sha256"] == masked["tracked_worktree_sha256"]
    assert clean["candidate_sha256"] != masked["candidate_sha256"]


def test_source_epoch_binds_raw_tracked_bytes_despite_eol_normalization(tmp_path) -> None:
    module = _load_runner_module()
    repo = _init_source_epoch_repo(tmp_path, attributes="*.py text eol=lf\n")
    lf_epoch = module.dependency_source_epoch(repo)

    (repo / "tracked.py").write_bytes(b"value = 'A'" + bytes([13, 10]))
    crlf_epoch = module.dependency_source_epoch(repo)

    assert lf_epoch["tracked_worktree_sha256"] != crlf_epoch["tracked_worktree_sha256"]
    assert lf_epoch["candidate_sha256"] != crlf_epoch["candidate_sha256"]


def test_source_epoch_is_independent_of_local_diff_rendering_config(tmp_path) -> None:
    module = _load_runner_module()
    repo = _init_source_epoch_repo(tmp_path)
    (repo / "tracked.py").write_bytes(b"value = 'changed'\n")
    before = module.dependency_source_epoch(repo)

    subprocess.run(["git", "config", "diff.noprefix", "true"], cwd=repo, check=True)
    after = module.dependency_source_epoch(repo)

    assert before == after


def test_benchmark_stats_summary_drops_runtime_paths_and_messages() -> None:
    module = _load_module()
    summary = module.summarize_provider_stats(
        {
            "provider": "scope-recall",
            "total_memories": 12,
            "shared_scope_memories": 12,
            "db_path": r"C:\\private\\memory.sqlite3",
            "background_writer": {
                "thread_alive": True,
                "failed_writes": 0,
                "unreported_failures": 0,
                "last_error_type": "",
            },
            "vector": {
                "enabled": True,
                "ready": True,
                "status": "ready",
                "backend": "lancedb",
                "row_count": 12,
                "unique_id_count": 12,
                "duplicate_row_count": 0,
                "path": r"C:\\private\\lancedb",
                "message": "opened C:\\private\\lancedb",
                "embedder": {
                    "provider": "local-hash",
                    "model": "hash-v1",
                    "dimensions": 256,
                },
            },
        }
    )

    assert summary["total_memories"] == 12
    assert summary["vector"]["row_count"] == 12
    assert summary["vector"]["embedder"]["model"] == "hash-v1"
    serialized = json.dumps(summary)
    assert "db_path" not in summary
    assert "path" not in summary["vector"]
    assert "message" not in summary["vector"]
    assert "C:\\\\private" not in serialized


def test_ingestion_receipt_validation_fails_closed_on_home_drift() -> None:
    module = _load_module()
    receipt = {
        "stored_memories": 12,
        "stats": {
            "total_memories": 12,
            "vector": {
                "enabled": True,
                "ready": True,
                "backend": "lancedb",
                "row_count": 12,
                "unique_id_count": 12,
                "embedder": {
                    "provider": "local-hash",
                    "model": "hash-v1",
                    "dimensions": 256,
                },
            },
        },
    }
    healthy = {
        "total_memories": 12,
        "vector": {
            "enabled": True,
            "ready": True,
            "backend": "lancedb",
            "row_count": 12,
            "unique_id_count": 12,
            "embedder": {
                "provider": "local-hash",
                "model": "hash-v1",
                "dimensions": 256,
            },
        },
    }
    module.validate_ingestion_receipt(receipt, healthy)

    drifted = dict(healthy)
    drifted["vector"] = {**healthy["vector"], "row_count": 0, "unique_id_count": 0}
    with pytest.raises(RuntimeError, match="vector row count"):
        module.validate_ingestion_receipt(receipt, drifted)


def test_concurrent_heartbeat_writes_are_serialized_on_windows(
    tmp_path, monkeypatch
) -> None:
    module = _load_runner_module()
    original_replace = module.os.replace
    state_lock = threading.Lock()
    active_heartbeat_replaces = 0

    def collision_sensitive_replace(source, destination):
        nonlocal active_heartbeat_replaces
        if Path(destination).name != "heartbeat.json":
            return original_replace(source, destination)
        with state_lock:
            if active_heartbeat_replaces:
                raise PermissionError(5, "simulated Windows replace collision")
            active_heartbeat_replaces += 1
        try:
            time.sleep(0.01)
            return original_replace(source, destination)
        finally:
            with state_lock:
                active_heartbeat_replaces -= 1

    monkeypatch.setattr(module.os, "replace", collision_sensitive_replace)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(
                lambda index: module.write_heartbeat(tmp_path, {"index": index}),
                range(20),
            )
        )

    assert (tmp_path / "heartbeat.json").is_file()


def test_parse_judgment_keeps_unparseable_output_invalid_instead_of_wrong() -> None:
    module = _load_module()

    assert module.parse_judgment('{"label":"CORRECT"}') is True
    assert module.parse_judgment('{"label":"WRONG"}') is False
    assert module.parse_judgment("CORRECT") is True
    assert module.parse_judgment("WRONG") is False
    assert module.parse_judgment("The reference says CORRECT but prediction is WRONG") is None
    assert module.parse_judgment("INCORRECT") is None
    assert module.parse_judgment('{"label":"CORRECT"}\nWRONG') is None
    assert module.parse_judgment('{"label":"correct"}') is None
    assert module.parse_judgment('{"label":"CORRECT","reason":"looks right"}') is None
    assert module.parse_judgment('{"label":"CORRECT","label":"WRONG"}') is None
    assert module.parse_judgment("correct") is None
    assert module.parse_judgment("upstream OAuth 403") is None
    assert module.parse_judgment("") is None


def test_conversation_records_preserve_visual_context_and_evidence_ids() -> None:
    module = _load_module()
    conversation = {
        "conversation": {
            "session_1_date_time": "1:00 pm on 8 May, 2023",
            "session_1": [
                {
                    "dia_id": "D1:3",
                    "speaker": "Alice",
                    "text": "Look at this.",
                    "blip_caption": "a red bicycle beside a lake",
                    "query": "What color is the bicycle?",
                }
            ],
        }
    }

    records = module.conversation_records(conversation)

    assert records == [
        {
            "dia_id": "D1:3",
            "session": "session_1",
            "event_time": "1:00 pm on 8 May, 2023",
            "speaker": "Alice",
            "text": "Look at this.",
            "visual_context": "a red bicycle beside a lake",
            "visual_query": "What color is the bicycle?",
        }
    ]


def test_resume_only_treats_scored_questions_as_done() -> None:
    module = _load_module()
    rows = [
        {"question_id": "q1", "status": "scored", "correct": True},
        {"question_id": "q2", "status": "invalid_judge"},
        {"question_id": "q3", "status": "invalid_answerer"},
    ]

    assert module.completed_question_ids(rows) == {"q1"}


def test_score_report_separates_coverage_from_accuracy() -> None:
    module = _load_module()
    rows = [
        {"question_id": "q1", "category": "single-hop", "status": "scored", "correct": True},
        {"question_id": "q2", "category": "single-hop", "status": "scored", "correct": False},
        {"question_id": "q3", "category": "temporal", "status": "invalid_judge"},
    ]

    unvalidated = module.score_results(
        rows,
        expected_question_ids={"q1", "q2", "q3"},
    )
    report = module.score_results(
        rows,
        expected_question_ids={"q1", "q2", "q3"},
        artifacts_validated=True,
    )

    assert unvalidated["complete"] is False
    assert report["expected_questions"] == 3
    assert report["scored_questions"] == 2
    assert report["invalid_questions"] == 1
    assert report["coverage"] == 2 / 3
    assert report["accuracy"] == 0.5
    assert report["complete"] is False
    assert report["categories"]["single-hop"]["accuracy"] == 0.5
    assert report["categories"]["temporal"]["scored"] == 0


def test_managed_provider_always_shuts_down_after_failure() -> None:
    module = _load_module()

    class FakeProvider:
        initialized = False
        shutdown_called = False

        def initialize(self, **kwargs):
            self.initialized = kwargs == {"session_id": "bench"}

        def shutdown(self, *, timeout):
            self.shutdown_called = timeout == 10.0

    provider = FakeProvider()
    try:
        with module.managed_provider(provider, session_id="bench"):
            assert provider.initialized is True
            raise RuntimeError("boom")
    except RuntimeError as exc:
        assert str(exc) == "boom"

    assert provider.shutdown_called is True


def test_store_record_requires_a_real_memory_id() -> None:
    module = _load_module()
    record = {
        "dia_id": "D1:3",
        "session": "session_1",
        "event_time": "1:00 pm on 8 May, 2023",
        "speaker": "Alice",
        "text": "Look at this.",
        "visual_context": "a red bicycle beside a lake",
        "visual_query": "What color is the bicycle?",
    }

    class FakeProvider:
        def __init__(self, response):
            self.response = response
            self.calls = []

        def handle_tool_call(self, tool_name, args):
            self.calls.append((tool_name, args))
            return self.response

    provider = FakeProvider('{"stored":true,"id":"memory-1"}')
    assert module.store_record(provider, record) == "memory-1"
    tool_name, args = provider.calls[0]
    assert tool_name == "scope_recall_store"
    assert args["target"] == "memory"
    assert "a red bicycle beside a lake" in args["content"]
    assert "D1:3" in args["content"]

    broken = FakeProvider('{"stored":false,"id":"","error":"locked"}')
    try:
        module.store_record(broken, record)
    except RuntimeError as exc:
        assert "failed to store LoCoMo evidence D1:3" in str(exc)
    else:
        raise AssertionError("a missing store receipt must fail the ingestion run")


def test_answer_and_judge_prompts_encode_official_reasoning_rules() -> None:
    module = _load_module()

    answer_prompt = module.build_answer_prompt(
        question="How many concerts did Alice and Bob attend?",
        category="multi-hop",
        evidence="[1] Alice attended one concert.\n[2] Bob attended two concerts.",
        current_date="10 May 2023",
    )
    assert "enumerate" in answer_prompt.lower()
    assert "multiple evidence" in answer_prompt.lower()
    assert "intersection" in answer_prompt.lower()
    assert "ANSWER:" in answer_prompt

    temporal_prompt = module.build_answer_prompt(
        question="In which month's game did Alice score?",
        category="temporal",
        evidence="[event_time=16 July] Alice: last month's game was my high score.",
        current_date="16 July 2023",
    )
    assert "when the message was sent" in temporal_prompt.lower()
    assert "referenced event" in temporal_prompt.lower()

    open_prompt = module.build_answer_prompt(
        question="What kind of institution is MIT?",
        category="open-domain",
        evidence="[1] Alice studied at MIT.",
        current_date="10 May 2023",
    )
    assert "general world knowledge" in open_prompt.lower()
    assert "every constraint" in open_prompt.lower()

    judge_prompt = module.build_judge_prompt(
        question="When did Alice arrive?",
        gold_answer="13 August",
        predicted_answer="13 August 2023",
    )
    assert "semantic equivalence" in judge_prompt.lower()
    assert '"label":"CORRECT"' in judge_prompt
    assert "not provided" in judge_prompt.lower()


def test_open_domain_answer_prompt_requires_hedged_inference_language() -> None:
    module = _load_module()
    prompt = module.build_answer_prompt(
        question="Would Caroline still want to pursue counseling?",
        category="open-domain",
        evidence="[1] Caroline found counseling support helpful.",
        current_date="13 October 2023",
    )
    lowered = prompt.lower()
    assert "hedged" in lowered
    assert "likely" in lowered
    assert "do not answer unknown when the evidence supports a direction" in lowered


def test_question_rows_have_stable_ids_and_gold_evidence() -> None:
    module = _load_module()
    sample = {
        "sample_id": "conv-7",
        "conversation": {
            "session_1_date_time": "8 May 2023",
            "session_1": [],
        },
        "qa": [
            {
                "question": "When did Alice arrive?",
                "answer": "8 May 2023",
                "evidence": ["D1:3"],
                "category": 2,
            }
        ],
    }

    rows = module.question_rows([sample])

    assert rows == [
        {
            "question_id": "conv-7:q0000",
            "sample_id": "conv-7",
            "question_index": 0,
            "question": "When did Alice arrive?",
            "gold_answer": "8 May 2023",
            "gold_evidence_ids": ["D1:3"],
            "category_id": 2,
            "category": "temporal",
            "current_date": "8 May 2023",
        }
    ]


def test_query_variants_cover_each_named_subject_without_model_calls() -> None:
    module = _load_module()

    variants = module.build_query_variants(
        "How many concerts did Alice and Bob attend?",
        category="multi-hop",
    )

    assert "Alice concerts attend" in variants
    assert "Bob concerts attend" in variants
    assert len(variants) <= 7


def test_retrieval_metrics_map_chunks_back_to_gold_dialogue_ids() -> None:
    module = _load_module()
    results = [
        {"id": "m1", "content": "first"},
        {"id": "m2", "content": "second"},
    ]
    memory_map = {
        "m1": {"dia_ids": ["D1:3"], "event_order": 1},
        "m2": {"dia_ids": ["D2:1", "D2:2"], "event_order": 2},
    }

    metrics = module.retrieval_metrics(
        results,
        gold_evidence_ids=["D1:3", "D2:2"],
        memory_map=memory_map,
        cutoffs=(1, 2),
    )

    assert metrics["1"] == {
        "retrieved_evidence_ids": ["D1:3"],
        "any_recall": True,
        "all_recall": False,
        "recall_fraction": 0.5,
    }
    assert metrics["2"]["all_recall"] is True
    assert metrics["2"]["recall_fraction"] == 1.0


def test_format_evidence_retains_retrieval_rank_and_temporal_provenance() -> None:
    module = _load_module()
    results = [
        {"id": "m2", "content": "later event", "score": 0.8},
        {"id": "m1", "content": "earlier event", "score": 0.9},
    ]
    memory_map = {
        "m1": {"dia_ids": ["D1:3"], "event_order": 1},
        "m2": {"dia_ids": ["D2:1"], "event_order": 2},
    }

    formatted = module.format_evidence(
        results,
        memory_map=memory_map,
        max_chars=1000,
        chronological=True,
    )

    assert formatted.index("earlier event") < formatted.index("later event")
    assert "retrieval_rank=2" in formatted
    assert "evidence_ids=D1:3" in formatted


def test_format_evidence_applies_budget_before_chronological_sort() -> None:
    module = _load_module()
    results = [
        {"id": "high-late", "content": "high late " + "x" * 60, "score": 0.9},
        {"id": "high-middle", "content": "high middle " + "y" * 60, "score": 0.8},
        {"id": "low-early", "content": "low early " + "z" * 60, "score": 0.1},
    ]
    memory_map = {
        "high-late": {"dia_ids": ["D3:1"], "event_order": 3},
        "high-middle": {"dia_ids": ["D2:1"], "event_order": 2},
        "low-early": {"dia_ids": ["D1:1"], "event_order": 1},
    }

    formatted = module.format_evidence(
        results,
        memory_map=memory_map,
        max_chars=300,
        chronological=True,
    )

    assert "high late" in formatted
    assert "high middle" in formatted
    assert "low early" not in formatted
    assert formatted.index("high middle") < formatted.index("high late")


def test_format_evidence_marks_event_time_as_message_send_time() -> None:
    module = _load_module()
    results = [{"id": "m1", "content": "Melanie: I painted this a while ago.", "score": 0.9}]
    memory_map = {
        "m1": {"dia_ids": ["D1:12"], "event_order": 1, "event_time": "1:56 pm on 8 May, 2023"},
    }
    formatted = module.format_evidence(
        results,
        memory_map=memory_map,
        max_chars=1000,
        chronological=True,
    )
    lowered = formatted.lower()
    assert "event_time" in lowered
    assert "sent" in lowered
    assert "not when the event happened" in lowered or "not necessarily when" in lowered


def test_session_chunks_never_cross_session_boundaries() -> None:
    module = _load_module()
    records = [
        {
            "dia_id": f"D1:{index}",
            "session": "session_1",
            "event_time": "8 May 2023",
            "speaker": "Alice",
            "text": f"turn {index}",
            "visual_context": "",
            "visual_query": "",
        }
        for index in range(1, 5)
    ]
    records.append(
        {
            "dia_id": "D2:1",
            "session": "session_2",
            "event_time": "9 May 2023",
            "speaker": "Bob",
            "text": "new session",
            "visual_context": "",
            "visual_query": "",
        }
    )

    chunks = module.session_chunks(records, chunk_size=3, overlap=1)

    assert [chunk["dia_ids"] for chunk in chunks] == [
        ["D1:1", "D1:2", "D1:3"],
        ["D1:3", "D1:4"],
        ["D2:1"],
    ]
    assert chunks[0]["chunk_id"] == "session_1:chunk:0000"
    assert chunks[2]["session"] == "session_2"
    assert "new session" in chunks[2]["content"]


def test_extract_answer_rejects_empty_or_reasoning_only_output() -> None:
    module = _load_module()

    assert module.extract_answer("ANSWER: 8 May 2023") == "8 May 2023"
    assert module.extract_answer("Some preface\nANSWER: Alice and Bob") == "Alice and Bob"
    assert module.extract_answer("ANSWER:") is None
    assert module.extract_answer("I cannot comply") is None


def test_stratified_question_sample_round_robins_across_conversations() -> None:
    module = _load_module()
    rows = [
        {"question_id": "a1", "sample_id": "a", "category_id": 1},
        {"question_id": "a2", "sample_id": "a", "category_id": 1},
        {"question_id": "b1", "sample_id": "b", "category_id": 1},
        {"question_id": "a3", "sample_id": "a", "category_id": 2},
        {"question_id": "b2", "sample_id": "b", "category_id": 2},
    ]

    selected = module.stratified_question_sample(rows, per_category=2)

    assert [row["question_id"] for row in selected] == ["a1", "b1", "a3", "b2"]


def test_query_planner_prompt_and_parser_are_bounded_and_answer_free() -> None:
    module = _load_module()
    prompt = module.build_query_planner_prompt(
        question="What martial arts has John done?",
        category="multi-hop",
    )

    assert "do not answer" in prompt.lower()
    assert "one query per subject" in prompt.lower()
    assert "concrete evidence terms" in prompt.lower()
    assert "hypothesis queries" in prompt.lower()

    parsed = module.parse_query_plan(
        '```json\n{"queries":["John combat sports training", "What martial arts has John done?", "John belts"]}\n```',
        primary_query="What martial arts has John done?",
        max_variants=2,
    )
    assert parsed == ["John combat sports training", "John belts"]
    assert module.parse_query_plan("upstream error", primary_query="q") == []
