from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/benchmark.temporal_scale.py"


def test_temporal_scale_benchmark_small_smoke() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--json",
            "--sizes",
            "2000,5000",
            "--rounds",
            "2",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "scope-recall.temporal-scale.v2"
    assert payload["candidate_version"] == "2.0.1"
    assert payload["sizes"] == [2000, 5000]
    assert payload["rounds_per_query"] == 2
    assert payload["live_database_used"] is False
    assert payload["scan_caps"] == {
        "current_indexed_candidates": 1000,
        "slot": 1000,
        "history": 1000,
    }
    assert payload["passed"] is True
    assert all(item["passed"] is True for item in payload["scenarios"])
    assert all(
        item["checks"]["memory_filtered_current_p99_within_threshold"] is True
        for item in payload["scenarios"]
    )
    assert all(
        item["checks"]["fts_row_count_exact"] is True
        and item["checks"]["natural_language_top1_correct"] is True
        and item["checks"]["natural_language_candidates_complete"] is True
        and item["checks"]["natural_language_current_p99_within_threshold"] is True
        and item["checks"]["indexed_candidate_overflow_explicit"] is True
        for item in payload["scenarios"]
    )
