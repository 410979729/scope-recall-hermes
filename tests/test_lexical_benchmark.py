"""Installed lexical CJK benchmark script contract."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark.lexical_cjk.py"


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
    assert payload["schema_version"] == "scope-recall.lexical-cjk-benchmark.v1"
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
