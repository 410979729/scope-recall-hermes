"""Shared pytest configuration for Scope Recall tests.

The fixtures keep source-tree imports and Hermes-runtime compatibility predictable across local, CI, and release-gate runs."""

from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

from plugin_source import install_plugin_tree

_TEST_HERMES_HOME = tempfile.TemporaryDirectory(prefix="scope.recall.test-home.")


def _install_plugin() -> Path:
    """Expose the workspace plugin to Hermes, using copy when symlink is denied."""

    repo_root = Path(__file__).resolve().parents[1]
    plugin_dir = Path(_TEST_HERMES_HOME.name) / "plugins" / "scope-recall"
    install_plugin_tree(plugin_dir, repo_root)
    return repo_root


def _register_package_alias(repo_root: Path) -> None:
    package_name = "scope_recall"
    if package_name in sys.modules:
        return
    package = types.ModuleType(package_name)
    package.__path__ = [str(repo_root)]
    sys.modules[package_name] = package


_REPO_ROOT = _install_plugin()
_register_package_alias(_REPO_ROOT)
os.environ["HERMES_HOME"] = _TEST_HERMES_HOME.name


@pytest.fixture(autouse=True)
def _isolate_posix_truth_hardening_cache():
    """Reset process-local POSIX hardening records around every test."""

    import scope_recall.truth_connection as truth_connection

    reset = getattr(truth_connection, "_reset_posix_hardening_cache_for_tests", None)
    if callable(reset):
        reset()
    yield
    if callable(reset):
        reset()
