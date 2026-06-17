from __future__ import annotations

import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

_TEST_HERMES_HOME = tempfile.TemporaryDirectory(prefix="scope.recall.test-home.")


def _install_plugin() -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    plugin_dir = Path(_TEST_HERMES_HOME.name) / "plugins" / "scope-recall"
    plugin_dir.parent.mkdir(parents=True, exist_ok=True)
    if not plugin_dir.exists():
        try:
            plugin_dir.symlink_to(repo_root, target_is_directory=True)
        except OSError:
            shutil.copytree(repo_root, plugin_dir)
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
