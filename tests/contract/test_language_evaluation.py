"""Complete per-case evidence is required before paired acceptance is calculated."""

import importlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def evaluation(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tests/language_eval"))
    package = importlib.import_module("scope_recall")
    if package.__spec__ is None:
        # Source aliases still need a package reader for resource-based inputs.
        spec = importlib.util.spec_from_file_location(
            "scope_recall", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
        )
        monkeypatch.setattr(package, "__spec__", spec)
    return importlib.import_module("evaluate"), importlib.import_module("acceptance")


def report_pair(evaluation):
    evaluator, acceptance = evaluation
    rows = [
        dict(
            id="TEST-positive",
            gold_active=True,
            active=True,
            promoted_within_1200=True,
            validation_failed=False,
            discarded_proposals=0,
            delay_seconds=10.0,
            candidate_count=0,
            candidates_with_reasons=0,
            states=[dict(state="active", reason="supported", at="2026-09-06T12:00:10Z", current=True)],
        ),
        dict(
            id="TEST-negative",
            gold_active=False,
            active=False,
            promoted_within_1200=False,
            validation_failed=False,
            discarded_proposals=0,
            delay_seconds=None,
            candidate_count=1,
            candidates_with_reasons=1,
            states=[dict(state="proposed", reason="unsupported", at="2026-09-06T12:00:10Z", current=True)],
        ),
    ]
    for row in rows:
        row.update(
            receipts=[dict(type="consolidate", disposition="completed", error=None)],
            model_calls={"extraction": 1},
            prompt_sha256={"extraction": [evaluator.digest("TEST")]},
            operational_errors=[],
            requests_with_clause_removed=0,
        )
    report = dict(
        schema="TEST",
        mode="fake",
        corpus_sha256="TEST-corpus",
        case_ids_sha256=evaluator.digest([r["id"] for r in rows]),
        model_config_sha256="TEST-config",
        protocol_sha256="TEST-protocol",
        evaluator_sha256="TEST-evaluator",
        complete=True,
        subject={"version": "TEST"},
        prompt_sha256={"extraction": [evaluator.digest("TEST")]},
        variants=[dict(clause=None, rows=rows, metrics=evaluator.summarize(rows))],
    )
    old = deepcopy(report)
    old["variants"][0]["rows"][0]["validation_failed"] = True
    old["variants"][0]["rows"][0]["receipts"].append(
        dict(type="consolidate", disposition="retry", error="INPUT_INVALID")
    )
    old["variants"][0]["metrics"] = evaluator.summarize(old["variants"][0]["rows"])
    return old, report


def test_complete_reports_still_use_the_same_acceptance_gates(evaluation):
    before, after = report_pair(evaluation)
    assert evaluation[1].compare(before, after)["accepted"]


@pytest.mark.parametrize(
    "damage", ["empty", "missing_case", "duplicate", "wrong_hash", "wrong_count", "wrong_rate", "wrong_reason_count"]
)
def test_comparison_rejects_missing_or_inconsistent_per_case_evidence(evaluation, damage):
    before, after = report_pair(evaluation)
    for report in (before, after):
        variant = report["variants"][0]
        if damage == "empty":
            variant["rows"] = []
        elif damage == "missing_case":
            variant["rows"].pop()
        elif damage == "duplicate":
            variant["rows"][1]["id"] = variant["rows"][0]["id"]
        elif damage == "wrong_hash":
            report["case_ids_sha256"] = "TEST-wrong"
        elif damage == "wrong_count":
            variant["metrics"]["cases"] = 3
        elif damage == "wrong_rate":
            variant["metrics"]["false_acceptance_rate"] = 0.5
        else:
            variant["metrics"]["candidates_with_reasons"] = 0
    with pytest.raises(ValueError):
        evaluation[1].compare(before, after)


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "gold_active",
        "active",
        "promoted_within_1200",
        "validation_failed",
        "discarded_proposals",
        "delay_seconds",
        "candidate_count",
        "candidates_with_reasons",
        "states",
        "receipts",
        "model_calls",
        "prompt_sha256",
        "operational_errors",
        "requests_with_clause_removed",
    ],
)
def test_comparison_requires_every_per_case_field(evaluation, field):
    before, after = report_pair(evaluation)
    for report in (before, after):
        del report["variants"][0]["rows"][0][field]
    with pytest.raises(ValueError):
        evaluation[1].compare(before, after)


@pytest.mark.parametrize(
    "field,wrong",
    [("active", 1), ("delay_seconds", float("nan")), ("candidate_count", -1), ("discarded_proposals", True)],
)
def test_comparison_rejects_invalid_row_values(evaluation, field, wrong):
    before, after = report_pair(evaluation)
    for report in (before, after):
        report["variants"][0]["rows"][0][field] = wrong
    with pytest.raises(ValueError):
        evaluation[1].compare(before, after)


@pytest.mark.parametrize("discard_count", [0, 1, 2])
def test_successful_work_with_discarded_proposals_counts_as_validation_failure(evaluation, tmp_path, discard_count):
    evaluator, acceptance = evaluation
    recording = dict(
        kind="preference",
        subject="user",
        predicate="prefer",
        value_text="blue",
        conditions=[],
        statement_kind="assertion",
        support="supported",
    )

    class Partial(evaluator.RecordingPort):
        def propose(self, messages, **kwargs):
            value = json.loads(super().propose(messages, **kwargs))
            if "claim_proposals" in value:
                good = value["claim_proposals"][0]
                value["claim_proposals"] += [
                    dict(good, evidence_spans=[dict(good["evidence_spans"][0], quote=f"TEST absent quote {i}")])
                    for i in range(discard_count)
                ]
            return json.dumps(value)

    case = dict(id="TEST-dropped", text="I prefer blue.", origin="human_direct", gold_active=True)
    row = evaluator.run_case(case, tmp_path / "TEST-case", evaluator.PromptPort(Partial(recording), None))
    # Work superseded by a newer revision (an entity projection queued before promotion) is closed as
    # obsolete with a reason code; it is not failed work.
    assert not any(receipt["error"] for receipt in row["receipts"] if receipt["disposition"] != "obsolete")
    assert row["validation_failed"] == bool(discard_count)
    assert row["discarded_proposals"] == discard_count
    summary = evaluator.summarize([row])
    assert summary["validation_failure_rate"] == float(bool(discard_count))
    assert summary["discarded_proposals"] == discard_count


@pytest.mark.parametrize("name", ["consolidation", "candidate", "fragment", "repair"])
def test_prompt_evaluation_accepts_the_production_json_fence(evaluation, name):
    evaluator = importlib.import_module("evaluate_prompts")
    example = json.loads((ROOT / f"prompts/{name}.examples.json").read_text(encoding="utf-8"))[0]
    raw = "```json\n" + json.dumps(example["expected"]) + "\n```"
    assert evaluator.valid_result(name, raw, example["expected"], example["input"])


def test_comparison_rejects_a_discard_hidden_by_a_successful_receipt(evaluation):
    before, after = report_pair(evaluation)
    after["variants"][0]["rows"][0]["discarded_proposals"] = 1
    after["variants"][0]["metrics"] = evaluation[0].summarize(after["variants"][0]["rows"])
    with pytest.raises(ValueError):
        evaluation[1].compare(before, after)
