"""The version and the wheel module list must be derived, not retyped.

Six of the eight packaging failures on 2026-09-13 were new modules missing from
a hand-copied allowlist, plus three version strings that had drifted apart.
These tests make both conditions loud and tell the reader the one command that
fixes them.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from packaging_hooks import module_inventory as inventory  # noqa: E402

_FIX = "python scripts/build.package_manifest.py --write"


def _allowlist() -> dict:
    return json.loads(inventory.ALLOWLIST_PATH.read_text(encoding="utf-8"))


def test_every_reachable_module_is_in_the_wheel_allowlist():
    expected = set(inventory.reachable_modules(REPO_ROOT))
    listed = set(_allowlist()["python_modules"])
    missing = sorted(expected - listed)
    unreachable = sorted(listed - expected)
    assert not missing, f"modules reachable from an entry point but not in the wheel: {missing}\nrun: {_FIX}"
    assert not unreachable, f"modules in the wheel that nothing imports: {unreachable}\nrun: {_FIX}"


def test_the_allowlist_matches_the_generator_exactly():
    assert _allowlist() == inventory.expected_allowlist(REPO_ROOT), f"run: {_FIX}"


def test_declared_packages_cover_every_listed_module():
    listed = _allowlist()
    assert listed["packages"] == inventory.reachable_packages(listed["python_modules"])


def test_one_version_source_and_no_stale_stamps():
    stale = inventory.stale_version_files(REPO_ROOT)
    assert not stale, f"version stamps disagree with _version.py: {stale}\nrun: {_FIX}"
    assert _allowlist()["package_version"] == inventory.source_version(REPO_ROOT)


def test_every_entry_point_exists():
    """A stale entry point silently shrinks the wheel, so it must be an error."""
    missing = [entry for entry in inventory.ENTRY_POINTS if not (REPO_ROOT / entry).is_file()]
    assert not missing, f"entry points named but absent: {missing}"


@pytest.mark.parametrize(
    "version",
    ["3.1.0rc11", "3.1.0rc9", "3.2.0", "3.1.0.dev4", "3.1.0rc10.post17", "4.0.0rc1"],
)
def test_semver_spelling_agrees_with_the_installer(version):
    """The build helper restates the installer's rule; it must not drift from it."""
    from scope_recall.maintenance.install import _manifest_version

    assert inventory.semver_version(version) == _manifest_version(version)


def test_post_release_versions_are_not_reintroduced():
    """``.postN`` is not something this toolchain can normalise.

    ``_manifest_version`` only rewrites the ``X.Y.ZrcN`` prefix, so a post
    segment survives into a plugin manifest as a version no host can parse.
    The project ships ``rcN`` and ``devN``; this keeps that decision visible in
    the place that would otherwise discover it in production.
    """
    assert ".post" not in inventory.source_version(REPO_ROOT)


def test_stamping_a_manifest_preserves_everything_else(tmp_path):
    original = "name: scope-recall\nversion: 1.2.3\ndescription: unchanged\n"
    stamped = inventory.stamped_manifest(original, "9.9.9rc1")
    assert stamped == "name: scope-recall\nversion: 9.9.9rc1\ndescription: unchanged\n"
    with pytest.raises(RuntimeError):
        inventory.stamped_manifest("name: no-version-line\n", "9.9.9rc1")


# --------------------------------------------------------------------------
# The documents a reader checks the version against
# --------------------------------------------------------------------------

def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def test_the_readme_names_the_version_being_shipped() -> None:
    """It said rc10 while the package said rc11: the first thing a reader
    checks was the one thing nothing verified."""
    version = inventory.source_version(REPO_ROOT)
    readme = _read("README.md")
    assert f"`{version}`" in readme, (
        f"README.md does not mention {version}; update it when bumping _version.py")


def test_the_readme_does_not_still_advertise_an_older_candidate() -> None:
    version = inventory.source_version(REPO_ROOT)
    stale = [line.strip() for line in _read("README.md").splitlines()
             if "3.1.0rc" in line and version not in line]
    assert not stale, f"README.md still names an older candidate: {stale}"


def test_the_changelog_has_an_entry_for_this_version() -> None:
    version = inventory.source_version(REPO_ROOT)
    assert version in _read("CHANGELOG.md"), (
        f"CHANGELOG.md has no entry for {version}; a shipped version with no "
        "notes is a version nobody can review")


# --- one version string, one tree --------------------------------------------

def _git(*args):
    import subprocess
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    return done.returncode, done.stdout.strip()


def test_a_tagged_version_is_not_reused_for_a_different_tree():
    """rc23 was tagged and installed, then a behaviour change was committed under
    the same version. For about an hour the repository and the live instance ran
    different code behind the identical string `3.1.0rc23`, and nothing on the
    instance could tell them apart -- the divergence was found by hashing a file.
    A version that already names a tree may not name a second one."""
    from scope_recall._version import __version__

    code, _ = _git("rev-parse", "--git-dir")
    if code != 0:
        import pytest

        pytest.skip("not a git checkout")
    tag = "v" + __version__
    code, tagged = _git("rev-parse", "--verify", "--quiet", tag + "^{tree}")
    if code != 0:
        return  # This version has never been tagged; nothing to contradict.
    _, head_tree = _git("rev-parse", "HEAD^{tree}")
    assert tagged == head_tree, (
        "%s already names a different tree. Bump the version, or move the tag if "
        "it was never published." % tag)
