"""Long-path-safe filesystem primitives for Scope Recall rollout operations.

Windows still exposes legacy ``MAX_PATH`` failures through ordinary Python path
strings on hosts where long-path policy is not enabled.  This module performs
filesystem I/O through absolute extended-length paths while preserving ordinary
human-readable :class:`~pathlib.Path` values in plans, receipts, and errors.

Policy remains with callers: installer and rollout code decide *when* a backup,
copy, replacement, or rollback is allowed.  These helpers only prove that a
complete destination tree fits Windows component/extended-path limits before
mutation and execute the requested filesystem operation.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_WINDOWS_PREFIX = "\\\\?\\"
_WINDOWS_UNC_PREFIX = "\\\\?\\UNC\\"
_WINDOWS_MAX_COMPONENT_CHARS = 255
# Leave room below the documented 32,767-WCHAR NT path ceiling for a terminator
# and implementation-specific suffix handling.
_WINDOWS_MAX_EXTENDED_CHARS = 32_760


class PathBudgetError(ValueError):
    """Raised before mutation when a destination cannot be represented safely."""


@dataclass(frozen=True)
class CopyTreePlan:
    """Preflight evidence for one complete source-to-destination tree copy."""

    source: Path
    destination: Path
    entry_count: int
    max_destination_chars: int
    max_destination_path: Path


def _strip_windows_prefix(raw: str) -> str:
    normalized = str(raw)
    upper = normalized.upper()
    if upper.startswith(_WINDOWS_UNC_PREFIX.upper()):
        return "\\\\" + normalized[len(_WINDOWS_UNC_PREFIX) :]
    if upper.startswith(_WINDOWS_PREFIX.upper()):
        return normalized[len(_WINDOWS_PREFIX) :]
    return normalized


def public_path(path: str | os.PathLike[str]) -> Path:
    """Return an absolute ordinary path with any internal Windows prefix removed."""

    raw = _strip_windows_prefix(os.fspath(path))
    return Path(os.path.abspath(os.path.expanduser(raw)))


def io_path(path: str | os.PathLike[str]) -> str:
    """Return an absolute path suitable for filesystem I/O on this platform."""

    ordinary = str(public_path(path))
    if os.name != "nt":
        return ordinary
    if ordinary.startswith("\\\\"):
        return _WINDOWS_UNC_PREFIX + ordinary[2:]
    return _WINDOWS_PREFIX + ordinary


def _windows_char_count(value: str) -> int:
    """Count UTF-16 code units, matching the Windows path API's WCHAR budget."""

    return len(value.encode("utf-16-le")) // 2


def _validate_windows_destination(path: Path) -> int:
    """Validate one absolute destination and return its extended-path length."""

    if os.name != "nt":
        return len(str(path))
    for component in path.parts:
        if component == path.anchor:
            continue
        length = _windows_char_count(component)
        if length > _WINDOWS_MAX_COMPONENT_CHARS:
            raise PathBudgetError(
                "Windows destination component exceeds 255 characters: "
                f"component_length={length}, destination={path}"
            )
    extended_length = _windows_char_count(io_path(path))
    if extended_length > _WINDOWS_MAX_EXTENDED_CHARS:
        raise PathBudgetError(
            "Windows extended destination exceeds the supported path budget: "
            f"path_length={extended_length}, destination={path}"
        )
    return extended_length


def preflight_copy_tree(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    ignore: Callable[[str, list[str]], set[str]] | None = None,
) -> CopyTreePlan:
    """Inspect every destination path before a tree copy mutates the filesystem.

    ``ignore`` follows :func:`shutil.copytree` semantics and is applied during
    planning as well as execution so the evidence describes the tree that will
    actually be copied.
    """

    source_path = public_path(source)
    destination_path = public_path(destination)
    if not os.path.isdir(io_path(source_path)):
        raise NotADirectoryError(f"copy source is not a directory: {source_path}")

    longest_path = destination_path
    longest_length = _validate_windows_destination(destination_path)
    entry_count = 1
    source_io = io_path(source_path)
    for root, directories, files in os.walk(source_io, topdown=True, followlinks=False):
        names = [*directories, *files]
        ignored = set(ignore(root, names) if ignore is not None else ())
        directories[:] = [name for name in directories if name not in ignored]
        relative_root = os.path.relpath(root, source_io)
        destination_root = (
            destination_path
            if relative_root == os.curdir
            else destination_path / Path(relative_root)
        )
        for name in [*directories, *(item for item in files if item not in ignored)]:
            candidate = destination_root / name
            candidate_length = _validate_windows_destination(candidate)
            entry_count += 1
            if candidate_length > longest_length:
                longest_length = candidate_length
                longest_path = candidate

    return CopyTreePlan(
        source=source_path,
        destination=destination_path,
        entry_count=entry_count,
        max_destination_chars=longest_length,
        max_destination_path=longest_path,
    )


def make_dirs(path: str | os.PathLike[str], *, exist_ok: bool = True) -> Path:
    """Create a directory tree through the platform I/O path and return its public path."""

    destination = public_path(path)
    _validate_windows_destination(destination)
    os.makedirs(io_path(destination), exist_ok=exist_ok)
    return destination


def path_exists(path: str | os.PathLike[str]) -> bool:
    """Return whether a filesystem entry exists through long-path-safe I/O."""

    return os.path.exists(io_path(path))


def path_is_file(path: str | os.PathLike[str]) -> bool:
    """Return whether *path* is a regular file through long-path-safe I/O."""

    return os.path.isfile(io_path(path))


def path_is_dir(path: str | os.PathLike[str]) -> bool:
    """Return whether *path* is a directory through long-path-safe I/O."""

    return os.path.isdir(io_path(path))


def path_is_symlink(path: str | os.PathLike[str]) -> bool:
    """Return whether *path* is a symlink/reparse link through safe I/O."""

    return os.path.islink(io_path(path))


def remove_path(
    path: str | os.PathLike[str],
    *,
    missing_ok: bool = False,
    ignore_errors: bool = False,
) -> None:
    """Remove one file, symlink, or directory tree using long-path-safe I/O."""

    target = public_path(path)
    raw = io_path(target)
    try:
        if os.path.islink(raw) or os.path.isfile(raw):
            os.unlink(raw)
        elif os.path.isdir(raw):
            shutil.rmtree(raw)
        elif not missing_ok:
            raise FileNotFoundError(f"path does not exist: {target}")
    except Exception:
        if not ignore_errors:
            raise


def copy_tree(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    ignore: Callable[[str, list[str]], set[str]] | None = None,
    symlinks: bool = False,
    dirs_exist_ok: bool = False,
    copy_function: Callable[..., Any] | None = None,
) -> CopyTreePlan:
    """Preflight and copy a complete tree, removing partial output on failure."""

    plan = preflight_copy_tree(source, destination, ignore=ignore)
    make_dirs(plan.destination.parent, exist_ok=True)
    copier = copy_function or shutil.copy2
    try:
        shutil.copytree(
            io_path(plan.source),
            io_path(plan.destination),
            ignore=ignore,
            symlinks=symlinks,
            dirs_exist_ok=dirs_exist_ok,
            copy_function=copier,
        )
    except Exception:
        remove_path(plan.destination, missing_ok=True, ignore_errors=True)
        raise
    return plan


def copy_file(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    follow_symlinks: bool = False,
) -> Path:
    """Copy one file after destination validation and return its public path."""

    source_path = public_path(source)
    destination_path = public_path(destination)
    _validate_windows_destination(destination_path)
    make_dirs(destination_path.parent, exist_ok=True)
    shutil.copy2(
        io_path(source_path),
        io_path(destination_path),
        follow_symlinks=follow_symlinks,
    )
    return destination_path


def move_path(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
) -> Path:
    """Atomically rename one file/tree after validating its destination tree."""

    source_path = public_path(source)
    destination_path = public_path(destination)
    if os.path.isdir(io_path(source_path)) and not os.path.islink(io_path(source_path)):
        preflight_copy_tree(source_path, destination_path)
    else:
        _validate_windows_destination(destination_path)
    make_dirs(destination_path.parent, exist_ok=True)
    os.replace(io_path(source_path), io_path(destination_path))
    return destination_path
