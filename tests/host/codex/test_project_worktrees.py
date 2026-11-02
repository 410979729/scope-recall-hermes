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
