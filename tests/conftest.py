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

from scripts.execution_boundary import (
    ambient_active_hermes_home,
    validate_execution_boundary,
)

_DECLARED_REAL_HOME = str(os.environ.get("SCOPE_RECALL_REAL_HOME") or "").strip()
_REAL_HOME = Path(_DECLARED_REAL_HOME or Path.home()).resolve(strict=False)
_ACTIVE_HERMES_HOME = ambient_active_hermes_home(real_home=_REAL_HOME)
_DECLARED_BOUNDARY_PARENT = str(
    os.environ.get("SCOPE_RECALL_TEST_BOUNDARY_PARENT") or ""
).strip()
_TEST_BOUNDARY_PARENT = Path(
    _DECLARED_BOUNDARY_PARENT or tempfile.gettempdir()
).resolve(strict=False)
_TEST_BOUNDARY_PARENT.mkdir(parents=True, exist_ok=True)
os.environ["SCOPE_RECALL_TEST_BOUNDARY_PARENT"] = str(_TEST_BOUNDARY_PARENT)
_TEST_BOUNDARY = tempfile.TemporaryDirectory(
    prefix="scope.recall.test-boundary.",
    dir=_TEST_BOUNDARY_PARENT,
)
_TEST_ROOT = Path(_TEST_BOUNDARY.name)
_TEST_HERMES_HOME = _TEST_ROOT / "hermes-home"
_TEST_TEMP = _TEST_ROOT / "temp"
_TEST_TARGETS = {
    "HOME": _TEST_ROOT / "user-home",
    "USERPROFILE": _TEST_ROOT / "user-home",
    "APPDATA": _TEST_ROOT / "appdata",
    "LOCALAPPDATA": _TEST_ROOT / "local-appdata",
    "TEMP": _TEST_TEMP,
    "TMP": _TEST_TEMP,
    "XDG_CONFIG_HOME": _TEST_ROOT / "xdg-config",
    "XDG_CACHE_HOME": _TEST_ROOT / "xdg-cache",
    "HERMES_HOME": _TEST_HERMES_HOME,
    "SCOPE_RECALL_DB": _TEST_ROOT / "truth" / "memory.sqlite3",
    "SCOPE_RECALL_LOG_DIR": _TEST_ROOT / "logs",
    "SCOPE_RECALL_LEASE_DIR": _TEST_ROOT / "leases",
    "SCOPE_RECALL_PLUGIN_DIR": _TEST_HERMES_HOME / "plugins" / "scope-recall",
}
validate_execution_boundary(
    isolated_root=_TEST_ROOT,
    targets=_TEST_TARGETS,
    active_hermes_home=_ACTIVE_HERMES_HOME,
    real_home=_REAL_HOME,
)
for _name, _target in _TEST_TARGETS.items():
    if _name in {"SCOPE_RECALL_DB", "SCOPE_RECALL_PLUGIN_DIR"}:
        _target.parent.mkdir(parents=True, exist_ok=True)
    else:
        _target.mkdir(parents=True, exist_ok=True)
os.environ.update({key: str(value) for key, value in _TEST_TARGETS.items()})
os.environ["SCOPE_RECALL_ACTIVE_HERMES_HOME"] = str(_ACTIVE_HERMES_HOME)
os.environ["SCOPE_RECALL_REAL_HOME"] = str(_REAL_HOME)
tempfile.tempdir = str(_TEST_TEMP)


def _install_plugin() -> Path:
    """Expose the workspace plugin to Hermes, using copy when symlink is denied."""

    repo_root = Path(__file__).resolve().parents[1]
    plugin_dir = _TEST_HERMES_HOME / "plugins" / "scope-recall"
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
