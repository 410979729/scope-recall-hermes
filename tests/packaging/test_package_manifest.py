"""The version and the wheel module list must be derived, not retyped.

A hand-copied allowlist misses new modules, and version strings drift apart.
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
    from scope_recall.maintenance.install_common import manifest_version

    assert inventory.semver_version(version) == manifest_version(version)


def test_post_release_versions_are_not_reintroduced():
    """``.postN`` is not something this toolchain can normalise.

    ``manifest_version`` only rewrites the ``X.Y.ZrcN`` prefix, so a post
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
    """The version the README names is the package's: the first thing a reader checks is the one thing nothing
    else verifies."""
    version = inventory.source_version(REPO_ROOT)
    readme = _read("README.md")
    assert f"`{version}`" in readme, f"README.md does not mention {version}; update it when bumping _version.py"


def test_the_readme_does_not_still_advertise_an_older_candidate() -> None:
    version = inventory.source_version(REPO_ROOT)
    stale = [line.strip() for line in _read("README.md").splitlines() if "3.1.0rc" in line and version not in line]
    assert not stale, f"README.md still names an older candidate: {stale}"


def test_the_readme_installs_the_vector_extra_and_no_older_release() -> None:
    """The README's install commands carry ``[lancedb]`` and pin no older release: whoever follows the first page
    (PyPI shows it too) gets the current release and recall with vectors."""
    import re

    version = inventory.source_version(REPO_ROOT)
    readme = _read("README.md")
    installs = [line.strip() for line in readme.splitlines() if "pip install" in line and "hermes-scope-recall" in line]
    assert installs, "README.md has no install command"
    assert all("[lancedb" in line for line in installs), f"an install without the vector extra: {installs}"
    # Unpinned, pip keeps an older install that satisfies the name and only adds the extra.
    unpinned = [line for line in installs if "==" not in line]
    assert all(" -U " in line or " --upgrade " in line for line in unpinned), f"no upgrade: {unpinned}"
    pins = re.findall(r"hermes-scope-recall(?:\[[^\]]*\])?==([0-9][^\s\"'`]*)", readme)
    assert all(pin == version for pin in pins), f"README.md pins {pins}; this is {version}"
    # The 3.1.0 notes hold the 2.0.x migration; any other release link must be this one or "latest".
    tags = re.findall(r"releases/tag/v([0-9][^\s)\"'`]*)", readme)
    assert all(tag in {version, "3.1.0"} for tag in tags), f"README.md links releases {tags}"


def test_the_changelog_has_an_entry_for_this_version() -> None:
    version = inventory.source_version(REPO_ROOT)
    assert version in _read("CHANGELOG.md"), (
        f"CHANGELOG.md has no entry for {version}; a shipped version with no notes is a version nobody can review"
    )


# --- one version string, one tree --------------------------------------------


def _git(*args):
    import pathlib
    import subprocess

    root = pathlib.Path(__file__).resolve().parents[2]
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    return done.returncode, done.stdout.strip()


def test_a_tagged_version_is_not_reused_for_a_different_tree():
    """A version that already names a tree may not name a second one: a behaviour change committed under a tagged
    and installed version leaves the repository and the live instance running different code behind the
    identical string, with nothing on the instance to tell them apart."""
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
        "%s already names a different tree. Bump the version, or move the tag if it was never published." % tag
    )
