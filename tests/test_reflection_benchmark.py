"""Release benchmark contract for grounded reflection."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "benchmarks" / "reflection_cases.json"
SCRIPT = ROOT / "scripts" / "benchmark.reflection.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "scope_recall_reflection_benchmark",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reflection_fixture_covers_grounding_and_fail_closed_cases() -> None:
    payload = json.loads(CASES.read_text(encoding="utf-8"))
    assert {item["id"] for item in payload["valid_responses"]} == {
        "grounded-current",
        "grounded-followup",
        "grounded-role-order",
        "grounded-negative-polarity",
        "grounded-temporal-order",
        "grounded-conditional-modal",
        "grounded-quantifier",
        "grounded-historical",
    }
    assert {item["mutation"] for item in payload["invalid_responses"]} == {
        "unknown_top_level",
        "unknown_statement",
        "markdown_fence",
    }
    assert payload["thresholds"] == {
        "citation_validity_rate": 1.0,
        "observation_inference_separation_rate": 1.0,
        "grounded_candidate_rate": 1.0,
        "max_write_delta": 0,
        "unknown_reference_rejection_rate": 1.0,
        "require_unsupported_answer_rejection": True,
        "require_fenced_json_rejection": True,
        "minimum_valid_response_count": 8,
        "semantic_adversarial_rejection_rate": 1.0,
        "semantic_positive_acceptance_rate": 1.0,
    }


def test_reflection_benchmark_meets_release_thresholds() -> None:
    result = _module().run_benchmark(CASES)
    assert result["passed"] is True
    assert result["metrics"] == {
        "valid_response_count": 8,
        "citation_validity_rate": 1.0,
        "observation_inference_separation_rate": 1.0,
        "grounded_candidate_rate": 1.0,
        "unsupported_answer_rejected": True,
        "semantic_adversarial_case_count": 6,
        "semantic_adversarial_rejection_rate": 1.0,
        "semantic_positive_acceptance_rate": 1.0,
        "write_delta": 0,
        "unknown_reference_rejection_rate": 1.0,
        "fenced_json_rejected": True,
    }
    assert all(item["citations_valid"] for item in result["valid_cases"])
    assert all(item["grounded_candidate"] for item in result["valid_cases"])
    assert all(item["rejected"] for item in result["invalid_cases"])


def test_reflection_benchmark_rejects_incomplete_fixture(tmp_path: Path) -> None:
    payload = json.loads(CASES.read_text(encoding="utf-8"))
    payload["invalid_responses"] = payload["invalid_responses"][:2]
    incomplete = tmp_path / "incomplete-reflection.json"
    incomplete.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="at least three invalid"):
        _module().run_benchmark(incomplete)
