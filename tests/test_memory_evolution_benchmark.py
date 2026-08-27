"""Release benchmark contract for temporal memory evolution."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "benchmarks" / "memory_evolution_cases.json"
SCRIPT = ROOT / "scripts" / "benchmark.memory_evolution.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "scope_recall_memory_evolution_benchmark",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_memory_evolution_fixture_covers_required_scenarios() -> None:
    payload = json.loads(CASES.read_text(encoding="utf-8"))
    cases = payload["cases"]
    assert len(cases) == 10
    ids = {item["id"] for item in cases}
    assert {
        "moving-city",
        "job-change",
        "preference-change",
        "multi-value-skills",
        "retraction",
        "delayed-ingestion",
        "ambiguous-time",
        "wrong-date-correction",
        "conflicting-source",
        "cross-project-isolation",
    } == ids
    assert payload["thresholds"] == {
        "case_pass_rate": 1.0,
        "max_stale_current_leakage": 0,
        "max_ambiguous_auto_apply": 0,
        "max_scope_leakage": 0,
        "require_idempotent_replay": True,
        "require_evidence_authority_gate": True,
        "require_evidence_polarity_gate": True,
        "require_evidence_subject_binding_gate": True,
        "require_adversarial_evidence_zero_write": True,
        "require_chunk_provenance_gate": True,
        "require_session_char_budget_gate": True,
        "require_journal_atomic_checkpoint": True,
        "require_durable_scope_route": True,
        "require_legacy_general_scope_route": True,
    }


def test_memory_evolution_benchmark_meets_release_thresholds() -> None:
    result = _module().run_benchmark(CASES)
    assert result["passed"] is True
    assert result["metrics"] == {
        "case_count": 10,
        "passed_count": 10,
        "case_pass_rate": 1.0,
        "stale_current_leakage": 0,
        "ambiguous_auto_apply": 0,
        "scope_leakage": 0,
        "idempotent_replay": True,
        "evidence_authority_gate": True,
        "evidence_polarity_gate": True,
        "evidence_subject_binding_gate": True,
        "adversarial_evidence_zero_write": True,
        "chunk_provenance_gate": True,
        "session_char_budget_gate": True,
        "journal_atomic_checkpoint": True,
        "durable_scope_route": True,
        "legacy_general_scope_route": True,
    }
    assert all(item["passed"] for item in result["cases"])


def test_memory_evolution_benchmark_rejects_incomplete_fixture(tmp_path: Path) -> None:
    payload = json.loads(CASES.read_text(encoding="utf-8"))
    payload["cases"] = payload["cases"][:9]
    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="at least ten"):
        _module().run_benchmark(incomplete)


def test_benchmark_process_prefers_checkout_over_stale_installed_package(
    tmp_path: Path,
) -> None:
    stale_root = tmp_path / "stale-site"
    stale_package = stale_root / "scope_recall"
    stale_package.mkdir(parents=True)
    (stale_package / "__init__.py").write_text(
        "__version__ = '0.0.0-stale'\n",
        encoding="utf-8",
    )
    (stale_package / "fact_repository.py").write_text(
        "STALE_PACKAGE = True\n",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(stale_root)

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--json"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["passed"] is True
