"""Generate paste-ready manual recovery commands for the current operating system.

Receipts must not tell a Windows operator to run POSIX ``rm``/``cp`` commands.
The helpers accept an optional platform name so both branches remain testable on
all CI hosts; production callers omit it and use ``os.name``.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import PureWindowsPath


def _platform_name(platform: str | None) -> str:
    return str(platform or os.name).strip().lower()


def quote_argument(value: str | os.PathLike[str], *, platform: str | None = None) -> str:
    """Quote one CLI argument for the selected host shell convention."""

    text = str(value)
    if _platform_name(platform) == "nt":
        return subprocess.list2cmdline([text])
    return shlex.quote(text)


def _powershell_literal(value: str | os.PathLike[str]) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _windows_path(value: str | os.PathLike[str]) -> PureWindowsPath:
    """Interpret a path with Windows semantics regardless of the current host."""

    return PureWindowsPath(str(value))


def _powershell(statements: list[str]) -> str:
    script = "; ".join(statement for statement in statements if statement)
    return (
        "powershell.exe -NoProfile -NonInteractive -Command "
        + subprocess.list2cmdline([script])
    )


def restore_file_command(
    path: str | os.PathLike[str],
    *,
    backup_path: str | os.PathLike[str] | None,
    preexisting: bool,
    platform: str | None = None,
) -> str:
    """Return a command that restores or removes one ordinary file."""

    selected = _platform_name(platform)
    if backup_path is None and preexisting:
        return ""
    if selected == "nt":
        windows_path = _windows_path(path)
        destination = _powershell_literal(windows_path)
        if backup_path is None:
            return _powershell(
                [
                    f"Remove-Item -LiteralPath {destination} -Force -ErrorAction SilentlyContinue"
                ]
            )
        parent = _powershell_literal(windows_path.parent)
        source = _powershell_literal(_windows_path(backup_path))
        return _powershell(
            [
                f"New-Item -ItemType Directory -Path {parent} -Force | Out-Null",
                f"Copy-Item -LiteralPath {source} -Destination {destination} -Force",
            ]
        )
    quoted_path = shlex.quote(str(path))
    if backup_path is None:
        return f"rm -f {quoted_path}"
    return f"cp {shlex.quote(str(backup_path))} {quoted_path}"


def restore_symlink_command(
    path: str | os.PathLike[str],
    *,
    link_target: str,
    target_path: str | os.PathLike[str],
    target_backup_path: str | os.PathLike[str] | None,
    platform: str | None = None,
) -> str:
    """Return a command that restores a symlink and its dereferenced file target."""

    selected = _platform_name(platform)
    if selected == "nt":
        statements: list[str] = []
        windows_path = _windows_path(path)
        windows_target_path = _windows_path(target_path)
        target = _powershell_literal(windows_target_path)
        if target_backup_path is None:
            statements.append(
                f"Remove-Item -LiteralPath {target} -Force -ErrorAction SilentlyContinue"
            )
        else:
            statements.extend(
                [
                    f"New-Item -ItemType Directory -Path {_powershell_literal(windows_target_path.parent)} -Force | Out-Null",
                    f"Copy-Item -LiteralPath {_powershell_literal(_windows_path(target_backup_path))} -Destination {target} -Force",
                ]
            )
        link = _powershell_literal(windows_path)
        statements.extend(
            [
                f"Remove-Item -LiteralPath {link} -Force -ErrorAction SilentlyContinue",
                f"New-Item -ItemType Directory -Path {_powershell_literal(windows_path.parent)} -Force | Out-Null",
                f"New-Item -ItemType SymbolicLink -Path {link} -Target {_powershell_literal(_windows_path(link_target))} -Force | Out-Null",
            ]
        )
        return _powershell(statements)

    quoted_target = shlex.quote(str(target_path))
    target_restore = (
        f"cp {shlex.quote(str(target_backup_path))} {quoted_target}"
        if target_backup_path is not None
        else f"rm -f {quoted_target}"
    )
    link_restore = (
        f"rm -f {shlex.quote(str(path))} && "
        f"ln -s {shlex.quote(link_target)} {shlex.quote(str(path))}"
    )
    return f"{target_restore} && {link_restore}"


def restore_tree_command(
    path: str | os.PathLike[str],
    *,
    backup_path: str | os.PathLike[str] | None,
    preexisting: bool,
    platform: str | None = None,
) -> str:
    """Return a command that restores or removes a directory tree."""

    selected = _platform_name(platform)
    if backup_path is None and preexisting:
        return ""
    if selected == "nt":
        windows_path = _windows_path(path)
        destination = _powershell_literal(windows_path)
        statements = [
            f"Remove-Item -LiteralPath {destination} -Recurse -Force -ErrorAction SilentlyContinue"
        ]
        if backup_path is not None:
            statements.extend(
                [
                    f"New-Item -ItemType Directory -Path {_powershell_literal(windows_path.parent)} -Force | Out-Null",
                    f"Copy-Item -LiteralPath {_powershell_literal(_windows_path(backup_path))} -Destination {destination} -Recurse -Force",
                ]
            )
        return _powershell(statements)

    quoted_path = shlex.quote(str(path))
    if backup_path is None:
        return f"rm -rf {quoted_path}"
    return (
        f"rm -rf {quoted_path} && "
        f"cp -a {shlex.quote(str(backup_path))} {quoted_path}"
    )
