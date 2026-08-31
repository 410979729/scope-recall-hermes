"""Focused tests for deterministic 2.0.1 hotfix release evidence runners."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
NEGATIVE_SCRIPT = PLUGIN_ROOT / "scripts" / "benchmark.negative_retrieval.py"
CANDIDATE_SCRIPT = PLUGIN_ROOT / "scripts" / "rehearse.candidate_isolation.py"
NEGATIVE_FIXTURE = PLUGIN_ROOT / "benchmarks" / "NEGATIVE_RETRIEVAL_BENCHMARK.json"
CANDIDATE_FIXTURE = PLUGIN_ROOT / "benchmarks" / "CANDIDATE_ISOLATION_REHEARSAL.json"


def _load_script(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_script(path: Path) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, str(path)],
        cwd=PLUGIN_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return json.loads(result.stdout)


def test_negative_retrieval_runner_recomputes_frozen_evidence() -> None:
    module = _load_script("negative_retrieval_evidence", NEGATIVE_SCRIPT)
    expected = json.loads(NEGATIVE_FIXTURE.read_text(encoding="utf-8"))

    recomputed = module.build_negative_retrieval_evidence()

    assert recomputed == expected
    assert recomputed["negative_nonempty_count"] == 0
    assert recomputed["positive_hit_rate"] == 1.0
    assert _run_script(NEGATIVE_SCRIPT) == expected


def test_candidate_isolation_runner_recomputes_frozen_evidence() -> None:
    module = _load_script("candidate_isolation_evidence", CANDIDATE_SCRIPT)
    expected = json.loads(CANDIDATE_FIXTURE.read_text(encoding="utf-8"))

    recomputed = module.build_candidate_isolation_evidence()

    assert recomputed == expected
    assert recomputed["wrapper_insert_count"] == 0
    assert recomputed["candidate_ordinary_leak_count"] == 0
    assert recomputed["unreviewed_auto_promote_count"] == 0
    assert recomputed["explicit_profile_visible_count"] == 1
    assert recomputed["explicit_review_visible_count"] == 1
    assert recomputed["read_total_changes_delta"] == 0
    assert _run_script(CANDIDATE_SCRIPT) == expected
