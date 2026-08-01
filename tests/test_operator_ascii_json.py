"""Cross-platform output contracts for audited operator entry points."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "script_name",
    [
        "backfill.freshness.py",
        "requeue.vector_dead_letter.py",
        "recover.activation_lease.py",
    ],
)
def test_operator_missing_database_json_is_ascii_console_safe(
    tmp_path,
    script_name,
):
    """Windows legacy consoles must receive valid JSON, not UnicodeEncodeError."""

    hermes_home = tmp_path / "unicode-profile-玉衡"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "ascii:strict"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / script_name),
            "--hermes-home",
            str(hermes_home),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2, completed.stderr.decode("utf-8", errors="replace")
    rendered = completed.stdout.decode("ascii")
    payload = json.loads(rendered)
    assert payload["status"] == "error"
    assert "玉衡" in payload["path"]
    assert completed.stderr == b""