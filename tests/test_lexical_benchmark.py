"""Installed lexical CJK benchmark script contract."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark.lexical_cjk.py"


def _load_benchmark_module():
    module_name = "scope_recall_lexical_benchmark_contract_test"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_lexical_latency_contract_treats_absolute_p95_as_a_cross_host_target():
    benchmark = _load_benchmark_module()

    contract = benchmark.evaluate_latency_contract(
        legacy_p95_ms=91.063128,
        shadow_p95_ms=128.146864,
        release_contract=True,
    )

    assert 1.40 < contract["latency_ratio"] < 1.41
    assert contract["latency_ratio_budget"] == 4.0
    assert contract["shadow_p95_target_ms"] == 100.0
    assert contract["target_misses"] == ["shadow_p95"]
    assert contract["failures"] == []


def test_lexical_latency_contract_still_rejects_relative_regressions():
    benchmark = _load_benchmark_module()

    contract = benchmark.evaluate_latency_contract(
        legacy_p95_ms=20.0,
        shadow_p95_ms=100.1,
        release_contract=True,
    )

    assert contract["latency_ratio"] > 5.0
    assert contract["target_misses"] == ["shadow_p95"]
    assert contract["failures"] == ["latency_ratio"]


def test_lexical_latency_contract_fails_closed_on_invalid_measurements():
    benchmark = _load_benchmark_module()

    invalid_measurements = (
        (float("nan"), 20.0),
        (20.0, float("inf")),
        (-1.0, 20.0),
    )
    for legacy_p95_ms, shadow_p95_ms in invalid_measurements:
        contract = benchmark.evaluate_latency_contract(
            legacy_p95_ms=legacy_p95_ms,
            shadow_p95_ms=shadow_p95_ms,
            release_contract=True,
        )

        assert contract["latency_ratio"] is None
        assert contract["failures"] == ["invalid_latency"]


def test_lexical_latency_contract_accepts_exact_release_and_smoke_boundaries():
    benchmark = _load_benchmark_module()

    release_contract = benchmark.evaluate_latency_contract(
        legacy_p95_ms=25.0,
        shadow_p95_ms=100.0,
        release_contract=True,
    )
    smoke_contract = benchmark.evaluate_latency_contract(
        legacy_p95_ms=10.0,
        shadow_p95_ms=100.0,
        release_contract=False,
    )

    assert release_contract["latency_ratio"] == 4.0
    assert release_contract["target_misses"] == []
    assert release_contract["failures"] == []
    assert smoke_contract["latency_ratio"] == 10.0
    assert smoke_contract["target_misses"] == []
    assert smoke_contract["failures"] == []


def test_lexical_latency_contract_uses_the_published_six_decimal_values():
    benchmark = _load_benchmark_module()

    rounded_target = benchmark.evaluate_latency_contract(
        legacy_p95_ms=100.0,
        shadow_p95_ms=100.0000004,
        release_contract=True,
    )
    rounded_ratio = benchmark.evaluate_latency_contract(
        legacy_p95_ms=0.5007888341411246,
        shadow_p95_ms=0.41068645620639826,
        release_contract=True,
    )

    assert rounded_target["latency_ratio"] == 1.0
    assert rounded_target["target_misses"] == []
    assert rounded_target["failures"] == []
    assert rounded_ratio["latency_ratio"] == 0.410686 / 0.500789
    assert rounded_ratio["failures"] == []


def test_lexical_cjk_benchmark_reports_quality_latency_and_growth():
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            str(SCRIPT),
            "--json",
            "--rows",
            "500",
            "--rounds",
            "3",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == "scope-recall.lexical-cjk-benchmark.v2"
    assert payload["passed"] is True
    assert payload["rows"] == 500
    assert payload["rounds"] == 3
    assert payload["cjk_expected_found"] == payload["cjk_queries"] == 3
    assert payload["english_regressions"] == 0
    assert payload["max_result_count"] <= payload["limit"] == 10
    assert payload["shadow_p95_ms"] >= 0.0
    assert payload["legacy_p95_ms"] >= 0.0
    assert payload["shadow_to_legacy_p95_ratio"] >= 0.0
    assert payload["page_growth_ratio"] >= 1.0
    assert payload["failures"] == []
