from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_graph_relation_benchmark_script_passes_and_reports_expected_metrics():
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, "scripts/benchmark.graph_relations.py"],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["benchmark_name"] == "graph_relation_rerank_v1"
    assert payload["passed"] is True
    assert payload["metrics"]["case_count"] == 3
    assert payload["metrics"]["passed_cases"] == 3
    assert payload["metrics"]["improved_cases"] == 1
    assert payload["metrics"]["forbidden_id_violations"] == 0
    by_name = {case["name"]: case for case in payload["cases"]}
    assert by_name["supersedes improves current fact"]["off_order"][:2] == ["older-deploy-command", "newer-deploy-command"]
    assert by_name["supersedes improves current fact"]["on_order"][:2] == ["newer-deploy-command", "older-deploy-command"]
    assert by_name["hidden peers do not leak or rerank"]["relation_evidence_ids"] == []
    assert by_name["explicit zero superseded penalty is respected"]["older_bonus"] == 0.0
