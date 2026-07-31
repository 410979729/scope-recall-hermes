"""Cross-platform contracts for paste-ready manual recovery commands."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scope_recall.recovery_commands import (
    quote_argument,
    restore_file_command,
    restore_symlink_command,
    restore_tree_command,
)


def test_windows_recovery_commands_use_powershell_and_literal_paths():
    home = Path(r"C:\Temp\A Home")
    backup = Path(r"C:\Temp\B Backup\config.yaml")

    file_command = restore_file_command(
        home / "config.yaml",
        backup_path=backup,
        preexisting=True,
        platform="nt",
    )
    absent_command = restore_file_command(
        home / "scope-recall" / "memory.sqlite3",
        backup_path=None,
        preexisting=False,
        platform="nt",
    )
    tree_command = restore_tree_command(
        home / "plugins" / "scope-recall",
        backup_path=Path(r"C:\Temp\B Backup\scope-recall"),
        preexisting=True,
        platform="nt",
    )

    for command in (file_command, absent_command, tree_command):
        assert command.startswith("powershell.exe -NoProfile -NonInteractive -Command ")
        assert "rm -" not in command
        assert "cp " not in command
    assert "Copy-Item" in file_command
    assert "Remove-Item" in absent_command
    assert "-Recurse" in tree_command
    assert "'C:\\Temp\\A Home\\config.yaml'" in file_command


def test_windows_symlink_recovery_is_one_powershell_command():
    command = restore_symlink_command(
        Path(r"C:\Temp\Home\config.yaml"),
        link_target=r"..\external config.yaml",
        target_path=Path(r"C:\Temp\external config.yaml"),
        target_backup_path=Path(r"C:\Temp\backup config.yaml"),
        platform="nt",
    )

    assert command.count("powershell.exe") == 1
    assert "New-Item -ItemType SymbolicLink" in command
    assert "Copy-Item" in command
    assert "rm -" not in command
    assert "ln -s" not in command


def test_posix_recovery_commands_preserve_existing_shell_contract():
    path = Path("/tmp/home with space/config.yaml")
    backup = Path("/tmp/backup with space/config.yaml")

    assert restore_file_command(
        path,
        backup_path=backup,
        preexisting=True,
        platform="posix",
    ).startswith("cp ")
    assert restore_file_command(
        path,
        backup_path=None,
        preexisting=False,
        platform="posix",
    ).startswith("rm -f ")
    assert restore_tree_command(
        path.parent,
        backup_path=backup.parent,
        preexisting=True,
        platform="posix",
    ).startswith("rm -rf ")
    assert "ln -s" in restore_symlink_command(
        path,
        link_target="../external.yaml",
        target_path=Path("/tmp/external.yaml"),
        target_backup_path=backup,
        platform="posix",
    )
    assert quote_argument(
        "/tmp/home with space/config.yaml", platform="posix"
    ) == "'/tmp/home with space/config.yaml'"


def test_windows_cli_argument_quoting_handles_spaces():
    quoted = quote_argument(Path(r"C:\Temp\A Home"), platform="nt")

    assert quoted.startswith('"')
    assert quoted.endswith('"')
    assert "A Home" in quoted

@pytest.mark.skipif(os.name != "nt", reason="PowerShell recovery smoke is Windows-specific")
def test_windows_file_and_tree_recovery_commands_execute(tmp_path):
    backup_file = tmp_path / "backup files" / "config.yaml"
    backup_file.parent.mkdir(parents=True)
    backup_file.write_text("provider: scope-recall\n", encoding="utf-8")
    destination_file = tmp_path / "restored home" / "config.yaml"

    file_command = restore_file_command(
        destination_file,
        backup_path=backup_file,
        preexisting=True,
        platform="nt",
    )
    file_result = subprocess.run(
        file_command,
        shell=True,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert file_result.returncode == 0, file_result.stderr + file_result.stdout
    assert destination_file.read_text(encoding="utf-8") == "provider: scope-recall\n"

    backup_tree = tmp_path / "backup tree" / "scope-recall"
    (backup_tree / "nested").mkdir(parents=True)
    (backup_tree / "nested" / "marker.txt").write_text("ready\n", encoding="utf-8")
    destination_tree = tmp_path / "restored plugins" / "scope-recall"

    tree_command = restore_tree_command(
        destination_tree,
        backup_path=backup_tree,
        preexisting=True,
        platform="nt",
    )
    tree_result = subprocess.run(
        tree_command,
        shell=True,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert tree_result.returncode == 0, tree_result.stderr + tree_result.stdout
    assert (destination_tree / "nested" / "marker.txt").read_text(encoding="utf-8") == "ready\n"
