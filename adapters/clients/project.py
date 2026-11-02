"""Resolve linked worktrees against registered project roots without invoking Git."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def _pointer(path: Path, *, prefix: str = "") -> Path:
    """Read one bounded path line relative to its containing directory."""
    with path.open("r", encoding="utf-8-sig") as stream:
        value = stream.read(4097).strip()
    if not value or len(value) > 4096 or "\n" in value or "\r" in value or not value.startswith(prefix):
        raise ValueError("invalid_repository_pointer")
    target = Path(value[len(prefix) :].strip())
    return (path.parent / target).resolve(strict=True)


def registered_worktree(cwd: str, roots: Mapping[str, str]) -> tuple[str, str] | None:
    """Return the registered root and scope after all forward and reverse pointers agree.

    A checkout copy has no such relationship. Its remote URL cannot grant access. The nearest repository boundary
    stops the search, including an invalid boundary, so a nested unregistered repository cannot borrow a parent.
    """
    directory = Path(cwd)
    try:
        for candidate in (directory, *directory.parents):
            gitfile = candidate / ".git"
            if not gitfile.exists():
                continue
            if not gitfile.is_file():
                return None
            admin = _pointer(gitfile, prefix="gitdir: ")
            common = _pointer(admin / "commondir")
            if not admin.is_dir() or admin.parent != common / "worktrees":
                return None
            backlink = _pointer(admin / "gitdir")
            if backlink.parent != candidate.resolve(strict=True) or backlink != gitfile.resolve(strict=True):
                return None
            for root, scope in roots.items():
                registered = Path(root) / ".git"
                if registered.is_dir() and os.path.normcase(str(registered.resolve(strict=True))) == os.path.normcase(
                    str(common)
                ):
                    return root, scope
            return None
    except (OSError, ValueError, RuntimeError):
        return None
    return None
