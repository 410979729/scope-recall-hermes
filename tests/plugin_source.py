"""Test-only plugin install and Windows capability helpers.

Production installers must not import this module. Native Windows without
Developer Mode cannot create symlinks; the copy fallback is a first-class
fixture path and must compare source bytes, not absolute symlink identity.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

FORCE_COPY_PLUGIN_ENV = "SCOPE_RECALL_TEST_FORCE_COPY_PLUGIN"
ASSUME_NO_SYMLINK_ENV = "SCOPE_RECALL_TEST_ASSUME_NO_SYMLINK"

_COPY_IGNORE = shutil.ignore_patterns(
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    ".hermes-agent-src",
    "build",
    "dist",
    "*.egg-info",
    ".venv",
)


def env_flag(name: str) -> bool:
    """Return whether a truthy test-control environment flag is set."""

    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def assume_no_symlink_privilege() -> bool:
    """Return whether this process is simulating a no-symlink Windows lane."""

    return env_flag(ASSUME_NO_SYMLINK_ENV)


def probe_symlink_privilege(directory: Path) -> bool:
    """Probe whether this account can create a file symlink in ``directory``.

    The probe is the only Windows skip gate for symlink-specific tests. A
    forced no-symlink lane returns False without creating a link so CI can
    exercise ordinary file and copy-fallback paths.
    """

    if assume_no_symlink_privilege():
        return False
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ".scope-recall-symlink-probe-target"
    link = directory / ".scope-recall-symlink-probe-link"
    if link.exists() or link.is_symlink():
        link.unlink()
    target.write_text("probe\n", encoding="utf-8")
    try:
        try:
            link.symlink_to(target.name)
        except OSError:
            return False
        return link.is_symlink()
    finally:
        try:
            link.unlink()
        except OSError:
            pass
        try:
            target.unlink()
        except OSError:
            pass


def require_symlink_privilege(directory: Path) -> None:
    """Skip only the calling symlink-specific test after a live probe fails."""

    if probe_symlink_privilege(directory):
        return
    pytest.skip("symlink privilege unavailable after probe")


def assert_same_source(left: Path, right: Path, *, label: str = "source") -> None:
    """Assert two paths carry the same bytes.

    Copy-fallback fixtures resolve to different absolute paths than the
    workspace tree. Requiring ``Path.resolve()`` or ``samefile`` identity
    would fail on standard non-admin Windows.
    """

    left_path = Path(left)
    right_path = Path(right)
    assert left_path.is_file(), f"{label} missing: {left_path}"
    assert right_path.is_file(), f"{label} missing: {right_path}"
    try:
        if os.path.samefile(left_path, right_path):
            return
    except OSError:
        pass
    assert left_path.read_bytes() == right_path.read_bytes(), (
        f"{label} content identity mismatch: {left_path} vs {right_path}"
    )


def install_plugin_tree(plugin_dir: Path, repo_root: Path) -> str:
    """Install the plugin tree via symlink, or copy when that is impossible.

    Returns ``symlink``, ``copy``, or ``existing``. Callers must validate
    content identity, not that ``plugin_dir`` is a symlink to ``repo_root``.
    """

    plugin_dir = Path(plugin_dir)
    repo_root = Path(repo_root)
    plugin_dir.parent.mkdir(parents=True, exist_ok=True)
    if plugin_dir.exists():
        return "existing"
    if not env_flag(FORCE_COPY_PLUGIN_ENV) and not assume_no_symlink_privilege():
        try:
            plugin_dir.symlink_to(repo_root, target_is_directory=True)
            return "symlink"
        except OSError:
            pass
    shutil.copytree(repo_root, plugin_dir, ignore=_COPY_IGNORE)
    return "copy"
