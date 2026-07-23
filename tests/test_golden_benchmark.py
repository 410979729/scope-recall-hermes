"""Tests for curated benchmark isolation, expected/forbidden IDs, and assets.

They make curated lexical recall regressions part of the release contract without
claiming production or commercial-quality evidence."""

from __future__ import annotations

import builtins
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_curated_fixture_has_regression_scale_and_smoke_profiles_are_labeled() -> None:
    curated = json.loads(
        (ROOT / "benchmarks" / "curated_recall_quality_cases_v2.json").read_text(
            encoding="utf-8"
        )
    )
    cases = curated["cases"]
    assert curated["name"] == "curated_recall_regression_v2"
    assert curated["quality_tier"] == "curated-regression"
    assert "not production or commercial-quality evidence" in curated["description"]
    assert 100 <= len(cases) <= 300
    assert len(curated["setup"]) == 88
    assert all(case.get("expected_labels") for case in cases)
    assert all(case.get("forbidden_labels") for case in cases)
    prefixes = {str(case["name"]).split("-", 1)[0] for case in cases}
    assert {"procedure", "current", "chinese", "scope", "mixed"} <= prefixes

    lexical_smoke = json.loads(
        (ROOT / "benchmarks" / "golden_recall_cases.json").read_text(encoding="utf-8")
    )
    hybrid_smoke = json.loads(
        (ROOT / "benchmarks" / "golden_recall_hybrid_cases.json").read_text(
            encoding="utf-8"
        )
    )
    assert lexical_smoke["name"].startswith("recall_smoke_")
    assert hybrid_smoke["name"].startswith("recall_smoke_")


def _load_benchmark_module_without_plugins(monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
        if name == "plugins.memory" or name.startswith("plugins."):
            raise ModuleNotFoundError("No module named 'plugins'")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    spec = importlib.util.spec_from_file_location("benchmark_golden_without_plugins", ROOT / "scripts" / "benchmark.golden.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_golden_benchmark_import_does_not_require_hermes_plugins_package(monkeypatch, tmp_path):
    module = _load_benchmark_module_without_plugins(monkeypatch)

    assert module.GOLDEN_BENCHMARK_RESPONSE_SCHEMA_VERSION == "golden_benchmark_report.v1"
    try:
        module._load_provider_for_home(tmp_path / "home")
    except RuntimeError as exc:
        assert "scope-recall provider is not available" in str(exc)
    else:  # pragma: no cover - defensive, fake import should force failure
        raise AssertionError("provider loading should report a clean provider-unavailable error")


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

    proc = _run_benchmark(
        "--cases",
        "benchmarks/golden_recall_cases.json",
        "--hermes-home",
        str(live_home),
        "--auto-explain-on-fail",
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["passed"] is True
    assert payload["schema_version"] == "golden_benchmark_report.v1"
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
