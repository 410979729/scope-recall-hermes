"""Behavioral contracts for GitHub Release notes extraction."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "extract_changelog_release_notes.py"


def _module():
    spec = importlib.util.spec_from_file_location("scope_recall_release_notes", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extract_release_notes_returns_only_target_body():
    module = _module()
    changelog = """# Changelog

## [Unreleased]

## [1.8.7] - 2026-08-03

Cumulative release body.

### Fixed
- The actual fix.

## [1.8.6] - 2026-08-01

Older release body.
"""

    notes = module.extract_release_notes(changelog, "1.8.7")

    assert notes == "Cumulative release body.\n\n### Fixed\n- The actual fix.\n"
    assert "2026-08-03" not in notes
    assert "1.8.6" not in notes


@pytest.mark.parametrize(
    ("opening_fence", "closing_fence"),
    [("```markdown", "```"), ("~~~~markdown", "~~~~")],
)
def test_extract_release_notes_ignores_fenced_code_headings(
    opening_fence: str,
    closing_fence: str,
):
    module = _module()
    changelog = f"""# Changelog

{opening_fence}
## [1.8.7] - 2099-01-01
Fake fenced notes.
{closing_fence}

## [1.8.7] - 2026-08-03

Real release notes.

## [1.8.6] - 2026-08-01

Older notes.
"""

    assert module.extract_release_notes(changelog, "1.8.7") == "Real release notes.\n"


def test_extract_release_notes_rejects_duplicate_version_sections():
    module = _module()
    changelog = """## [1.8.7] - 2026-08-03
First body.
## [1.8.7] - 2026-08-04
Second body.
"""

    with pytest.raises(ValueError, match="exactly one"):
        module.extract_release_notes(changelog, "1.8.7")


def test_release_workflow_uses_checked_release_notes_extractor():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "python .github/scripts/extract_changelog_release_notes.py" in workflow
    assert '--version "${RELEASE_TAG#v}"' in workflow
    assert "--output release-notes.md" in workflow
    assert "text.split(marker" not in workflow


def test_release_notes_cli_writes_clean_markdown(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    output = tmp_path / "release-notes.md"
    changelog.write_text(
        "# Changelog\n\n## [1.8.7] - 2026-08-03\n\nPublic notes.\n\n## [1.8.6] - 2026-08-01\n\nOld notes.\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--version",
            "1.8.7",
            "--changelog",
            str(changelog),
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == "Public notes.\n"
