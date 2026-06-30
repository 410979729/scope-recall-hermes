"""Tests that operator runbook commands and safety wording stay synchronized with code.

They treat human documentation as part of the release surface."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


REQUIRED_TOPICS = [
    "安装/升级/回滚",
    "日常 health check",
    "journal backlog drain",
    "candidate review",
    "playbook review",
    "vector repair",
    "governance cleanup",
    "backup/restore",
    "release checklist",
    "cross-profile rollout",
]

REQUIRED_COMMANDS = [
    "hermes-scope-recall install",
    "hermes-scope-recall verify",
    "hermes-scope-recall doctor",
    "hermes-scope-recall dashboard",
    "hermes-scope-recall journal digest",
    "hermes-scope-recall candidates report",
    "hermes-scope-recall playbooks list",
    "hermes-scope-recall playbooks review",
    "hermes-scope-recall playbooks dedupe",
    "hermes-scope-recall playbooks promote",
    "hermes-scope-recall playbooks quarantine",
    "hermes-scope-recall playbooks supersede",
    "hermes-scope-recall vector repair",
    "hermes-scope-recall governance cleanup",
    "hermes-scope-recall governance rollback",
    "hermes-scope-recall migrate status",
    "hermes-scope-recall migrate apply",
    "hermes-scope-recall migrate openclaw-import",
    "hermes-scope-recall rollout profiles",
    "python -m pip install -e '.[dev,all]'",
    "python -m pytest -q",
    "scripts/check.release.py --allow-dirty",
]


def test_operator_runbook_covers_required_operational_paths():
    path = ROOT / "docs" / "operator-runbook.md"
    text = path.read_text(encoding="utf-8")

    for topic in REQUIRED_TOPICS:
        assert topic in text
    for command in REQUIRED_COMMANDS:
        assert command in text
    assert "SQLite online backup" in text
    assert "database is locked" in text
    assert "rollback-needed" in text
    assert "do not hard-delete" in text


def test_readme_links_operator_runbook_and_configuration_reference():
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "docs/operator-runbook.md" in text
    assert "docs/configuration.md" in text
