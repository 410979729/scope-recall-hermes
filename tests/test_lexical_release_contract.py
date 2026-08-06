"""Release and installed-CLI contracts for lexical/deep-path hardening."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from scope_recall import cli

ROOT = Path(__file__).resolve().parents[1]
CHECK_RELEASE = ROOT / "scripts" / "check.release.py"


def _release_module():
    spec = importlib.util.spec_from_file_location(
        "scope_recall_lexical_release_contract",
        CHECK_RELEASE,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_installed_cli_routes_lexical_plan_build_activate_and_rollback():
    assert cli._match_script_command(["lexical", "plan", "--json"]) == (
        "migrate.lexical_index.py",
        ["--json"],
    )
    assert cli._match_script_command(
        ["lexical", "build", "--maintenance-confirmed"]
    ) == (
        "migrate.lexical_index.py",
        ["--apply", "--maintenance-confirmed"],
    )
    assert cli._match_script_command(
        ["lexical", "activate", "--expected-current", "legacy"]
    ) == (
        "migrate.lexical_index.py",
        ["--apply", "--activate", "--expected-current", "legacy"],
    )
    assert cli._match_script_command(
        [
            "lexical",
            "rollback",
            "--expected-current",
            "cjk-trigram-v1",
        ]
    ) == (
        "migrate.lexical_index.py",
        [
            "--apply",
            "--rollback",
            "--expected-current",
            "cjk-trigram-v1",
        ],
    )


def test_release_contract_requires_lexical_and_windows_hardening_sources():
    release = _release_module()
    required = {
        "windows_filesystem.py",
        "lexical_generation.py",
        "lexical_migration.py",
        "lexical_query.py",
        "scripts/migrate.lexical_index.py",
        "scripts/benchmark.lexical_cjk.py",
    }

    assert required <= release.REQUIRED_SOURCE_FILES
    assert {
        "scope_recall/windows_filesystem.py",
        "scope_recall/lexical_generation.py",
        "scope_recall/lexical_migration.py",
        "scope_recall/lexical_query.py",
        "scope_recall/scripts/migrate.lexical_index.py",
        "scope_recall/scripts/benchmark.lexical_cjk.py",
    } <= release.REQUIRED_WHEEL
