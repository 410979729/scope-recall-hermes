"""The release's last step can run, and this version has notes to publish.

``.github/scripts/extract_changelog_release_notes.py`` imported a module removed with the
2.x tooling, so the release workflow's final step raised ``ModuleNotFoundError`` at import
time.  No gate ran the script, so nothing noticed for the whole 3.1.0 candidate series.
"""
from __future__ import annotations

import importlib.util
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github" / "scripts" / "extract_changelog_release_notes.py"
CHANGELOG = ROOT / "CHANGELOG.md"


def _module():
    spec = importlib.util.spec_from_file_location("extract_changelog_release_notes", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: a dataclass in the module resolves its own
    # module through sys.modules while the class body runs.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)  # the import that used to fail
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _version() -> str:
    return runpy.run_path(str(ROOT / "_version.py"))["__version__"]


def test_the_extractor_imports_without_a_retired_module():
    assert callable(_module().extract_version_section)


def test_it_returns_one_released_section_from_the_real_changelog():
    notes = _module().extract_version_section(CHANGELOG.read_text(encoding="utf-8"), "2.0.1")
    assert notes.strip() and notes.endswith("\n")
    assert "## [" not in notes, "a section stops at the next release heading"


def test_it_fails_closed_rather_than_publishing_the_wrong_body():
    module = _module()
    changelog = CHANGELOG.read_text(encoding="utf-8")
    for version, reason in (("9.9.9", "missing"), ("3.1.0rc39", "not a release version"), ("", "empty")):
        with pytest.raises(ValueError):
            module.extract_version_section(changelog, version)


def test_a_release_version_has_a_section_to_publish():
    """An rc has nothing to publish yet; a release without notes must not ship."""
    version = _version()
    if "rc" in version:
        assert f"### Scope Recall {version} " in CHANGELOG.read_text(encoding="utf-8"), (
            f"{version} has no changelog entry")
        pytest.skip(f"{version} is a candidate; the release section is written when the final is cut")
    notes = _module().extract_version_section(CHANGELOG.read_text(encoding="utf-8"), version)
    assert notes.strip()


def test_the_script_runs_as_the_workflow_runs_it(tmp_path):
    output = tmp_path / "notes.md"
    done = subprocess.run([sys.executable, str(SCRIPT), "--version", "2.0.1",
                           "--changelog", str(CHANGELOG), "--output", str(output)],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert output.read_text(encoding="utf-8").strip()
