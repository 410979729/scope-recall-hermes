from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_benchmark(*args: str, hermes_home_env: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if hermes_home_env is not None:
        env["HERMES_HOME"] = str(hermes_home_env)
    else:
        env.pop("HERMES_HOME", None)
    return subprocess.run(
        [sys.executable, "scripts/benchmark.golden.py", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )


def test_golden_benchmark_uses_isolated_home_and_keeps_existing_config_read_only(tmp_path):
    live_home = tmp_path / "live-home"
    live_config = live_home / "scope-recall" / "config.json"
    live_config.parent.mkdir(parents=True)
    original = {"retrieval": {"mode": "lexical", "min_score": 0.42}, "sentinel": "do-not-overwrite"}
    live_config.write_text(json.dumps(original, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    proc = _run_benchmark("--hermes-home", str(live_home), "--auto-explain-on-fail")

    assert proc.returncode == 0, proc.stderr + proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["passed"] is True
    assert payload["source_hermes_home"] == str(live_home.resolve())
    assert Path(payload["hermes_home"]).resolve() != live_home.resolve()
    assert json.loads(live_config.read_text(encoding="utf-8")) == original


def test_golden_benchmark_provider_failure_does_not_write_existing_config(tmp_path):
    empty_home = tmp_path / "empty-home"
    live_config = empty_home / "scope-recall" / "config.json"
    live_config.parent.mkdir(parents=True)
    original = {"sentinel": "provider-failure-must-not-overwrite"}
    live_config.write_text(json.dumps(original, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    proc = _run_benchmark("--hermes-home", str(empty_home), "--overwrite-config", hermes_home_env=empty_home)

    assert proc.returncode != 0
    assert "scope-recall provider is not available" in (proc.stderr + proc.stdout)
    assert json.loads(live_config.read_text(encoding="utf-8")) == original
