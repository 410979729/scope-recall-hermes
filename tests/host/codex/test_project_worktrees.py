"""Linked worktrees inherit only a fully cross-checked registered repository."""

from pathlib import Path

import pytest
from scope_recall.adapters.clients.identity import resolve_runtime_audience


@pytest.fixture
def linked(installed, tmp_path):
    config, _core, _clock, project = installed
    common = project / ".git"
    admin = common / "worktrees" / "feature"
    admin.mkdir(parents=True)
    worktree = tmp_path / "outside-project"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    (admin / "gitdir").write_text(str(worktree / ".git") + "\n", encoding="utf-8")
    return config, project, worktree, admin


def test_linked_worktree_uses_registered_project_scopes(linked):
    config, project, worktree, _admin = linked
    nested = worktree / "src"
    nested.mkdir()
    audience = resolve_runtime_audience(config, str(nested))
    assert audience == resolve_runtime_audience(config, str(project))


@pytest.mark.parametrize("broken", ["gitfile", "commondir", "backlink", "unregistered", "directory"])
def test_unverified_worktree_is_refused(linked, tmp_path, broken):
    config, _project, worktree, admin = linked
    if broken == "gitfile":
        (worktree / ".git").write_text(f"gitdir: {tmp_path}\n", encoding="utf-8")
    elif broken == "commondir":
        (admin / "commondir").write_text("../../..\n", encoding="utf-8")
    elif broken == "backlink":
        (admin / "gitdir").write_text(str(tmp_path / ".git"), encoding="utf-8")
    elif broken == "unregistered":
        other = tmp_path / "unregistered" / ".git"
        other.mkdir(parents=True)
        (admin / "commondir").write_text(str(other), encoding="utf-8")
    else:
        (worktree / ".git").write_text("gitdir: .\n", encoding="utf-8")
    audience = resolve_runtime_audience(config, str(worktree))
    assert not audience.allowed_scope_ids


def test_borrowed_gitfile_symlink_does_not_grant_project_access(linked, tmp_path):
    config, _project, worktree, _admin = linked
    borrowed = tmp_path / "TEST-borrowed"
    borrowed.mkdir()
    try:
        (borrowed / ".git").symlink_to(worktree / ".git")
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {getattr(exc, 'winerror', exc.errno)}")
    assert not resolve_runtime_audience(config, str(borrowed)).allowed_scope_ids


def test_worktree_directory_junction_preserves_actual_directory_identity(linked, tmp_path):
    import os

    if os.name != "nt":
        pytest.skip("Windows directory junction")
    import _winapi

    config, project, worktree, _admin = linked
    alias = tmp_path / "TEST-worktree-junction"
    _winapi.CreateJunction(str(worktree), str(alias))
    try:
        assert resolve_runtime_audience(config, str(alias)) == resolve_runtime_audience(config, str(project))
    finally:
        alias.rmdir()


def test_borrowed_gitfile_link_semantics_are_refused_without_symlink_privileges(linked, tmp_path, monkeypatch):
    config, _project, worktree, _admin = linked
    borrowed = tmp_path / "TEST-borrowed-link"
    borrowed.mkdir()
    gitfile = borrowed / ".git"
    gitfile.write_text((worktree / ".git").read_text(encoding="utf-8"), encoding="utf-8")
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        return original(worktree / ".git", *args, **kwargs) if path == gitfile else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert not resolve_runtime_audience(config, str(borrowed)).allowed_scope_ids


def test_git_directory_junction_cannot_borrow_registered_repository(linked, tmp_path):
    import os

    if os.name != "nt":
        pytest.skip("Windows directory junction")
    import _winapi

    config, project, _worktree, _admin = linked
    borrowed = tmp_path / "TEST-borrowed-junction"
    borrowed.mkdir()
    alias = borrowed / ".git"
    _winapi.CreateJunction(str(project / ".git"), str(alias))
    try:
        assert not resolve_runtime_audience(config, str(borrowed)).allowed_scope_ids
    finally:
        alias.rmdir()
