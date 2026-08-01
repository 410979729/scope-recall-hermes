"""Pytest contract for the standalone capture-LLM manual probe."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_capture_llm_manual_probe_runs_without_collection_side_effects() -> None:
    """The manual probe must execute explicitly and report all checks passing."""

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "manual_capture_llm.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        check=False,
    )
    output = completed.stdout.decode("utf-8", errors="replace")
    error = completed.stderr.decode("utf-8", errors="replace")

    assert completed.returncode == 0, error or output
    assert "Results:" in output
    assert "0 failed" in output
    assert "ALL TESTS PASSED" in output
