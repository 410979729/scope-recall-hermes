"""P2-11: declared Python support must match tested minors."""

from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path

import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CHECK_RELEASE_PATH = PLUGIN_ROOT / "scripts" / "check.release.py"
PYPROJECT = PLUGIN_ROOT / "pyproject.toml"
CI_WORKFLOW = PLUGIN_ROOT / ".github" / "workflows" / "ci.yml"
README = PLUGIN_ROOT / "README.md"
STABILITY = PLUGIN_ROOT / "docs" / "stability.md"

SUPPORTED_MINORS = ("3.11", "3.12")


def _load_release_check_module():
    spec = importlib.util.spec_from_file_location(
        "scope_recall_check_release_python_support",
        CHECK_RELEASE_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_requires_python_is_honest_bounded_range():
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    requires = project["requires-python"]
    classifiers = project["classifiers"]
    assert requires == ">=3.11,<3.13"
    for minor in SUPPORTED_MINORS:
        assert f"Programming Language :: Python :: {minor}" in classifiers
    assert "Programming Language :: Python :: 3.13" not in classifiers
    assert "Programming Language :: Python :: 3.10" not in classifiers


def test_ci_windows_covers_minimum_and_maximum_supported_minors():
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    lanes = workflow["jobs"]["test"]["strategy"]["matrix"]["include"]
    windows = {
        lane["python"]: lane
        for lane in lanes
        if lane.get("os") == "windows-latest" and str(lane.get("name", "")).startswith("windows-full-")
    }
    assert set(windows) == set(SUPPORTED_MINORS)
    for minor, lane in windows.items():
        assert lane["command"] == "python -m pytest -q"
        assert lane["install-target"] == ".[lancedb,dev]"
        assert lane.get("continue-on-error") is None
    assert workflow["jobs"]["test"].get("continue-on-error") is None


def test_public_docs_do_not_claim_untested_python_minors():
    readme = README.read_text(encoding="utf-8")
    stability = STABILITY.read_text(encoding="utf-8")
    assert "Python-3.11%20%7C%203.12" in readme or "Python 3.11 | 3.12" in readme
    assert "3.11 or newer" not in readme
    assert "3.11 or newer" not in stability
    assert re.search(r"Python 3\.11 or 3\.12", readme)
    assert re.search(r"Python 3\.11 or 3\.12", stability)


def test_release_gate_python_support_check_is_clean():
    release_check = _load_release_check_module()
    result = release_check.python_support_check()
    assert result["ok"] is True, result.get("failures")
    assert result["requires_python"] == ">=3.11,<3.13"
    assert list(result["supported_minors"]) == list(SUPPORTED_MINORS)
