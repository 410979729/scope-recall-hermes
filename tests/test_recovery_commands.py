"""Cross-platform contracts for paste-ready manual recovery commands."""

from __future__ import annotations

import locale
import os
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from scope_recall.recovery_commands import (
    quote_argument,
    restore_file_command,
    restore_symlink_command,
    restore_tree_command,
)

# cmd.exe / PowerShell 5.1 on Chinese Windows write OEM/ANSI bytes (CP936).
# Release-gate pytest inherits PYTHONUTF8=1, so text=True decodes as UTF-8,
# kills the stderr reader thread, and leaves stderr=None.
_WINDOWS_SUBPROCESS_ENCODINGS = ("utf-8", "oem", "cp936", "gbk", "cp1252")


def _decode_windows_subprocess_bytes(payload: bytes | None) -> str:
    """Decode localized Windows process output without dropping the stream."""

    if not payload:
        return ""
    encodings: list[str] = []
    preferred = locale.getpreferredencoding(False)
    for name in ("utf-8", preferred, *_WINDOWS_SUBPROCESS_ENCODINGS):
        if name and name not in encodings:
            encodings.append(name)
    for name in encodings:
        try:
            return payload.decode(name)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _windows_recovery_command_env() -> dict[str, str]:
    """Keep paste-ready ``powershell.exe`` resolvable on a stripped native PATH."""

    env = os.environ.copy()
    powershell_home = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
    )
    if not powershell_home.is_dir():
        return env
    extra = str(powershell_home)
    updated = False
    for key in ("Path", "PATH"):
        if key not in env:
            continue
        parts = [part for part in env[key].split(os.pathsep) if part]
        if extra not in parts:
            env[key] = extra + os.pathsep + env[key]
        updated = True
    if not updated:
        env["Path"] = extra
    return env


def _run_recovery_command(command: str) -> subprocess.CompletedProcess[str]:
    """Execute one paste-ready recovery command with bytes-safe diagnostics."""

    result = subprocess.run(
        command,
        shell=True,
        text=False,
        capture_output=True,
        timeout=30,
        env=_windows_recovery_command_env() if os.name == "nt" else None,
    )
    return subprocess.CompletedProcess(
        args=result.args,
        returncode=result.returncode,
        stdout=_decode_windows_subprocess_bytes(result.stdout),
        stderr=_decode_windows_subprocess_bytes(result.stderr),
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


def test_windows_recovery_normalizes_paths_created_on_a_posix_host():
    file_command = restore_file_command(
        PurePosixPath(r"C:\Temp\A Home/config.yaml"),
        backup_path=PurePosixPath(r"C:\Temp\B Backup/config.yaml"),
        preexisting=True,
        platform="nt",
    )
    tree_command = restore_tree_command(
        PurePosixPath(r"C:\Temp\A Home/plugins/scope-recall"),
        backup_path=PurePosixPath(r"C:\Temp\B Backup/scope-recall"),
        preexisting=True,
        platform="nt",
    )
    symlink_command = restore_symlink_command(
        PurePosixPath(r"C:\Temp\A Home/config.yaml"),
        link_target=r"..\external config.yaml",
        target_path=PurePosixPath(r"C:\Temp/external config.yaml"),
        target_backup_path=PurePosixPath(r"C:\Temp\B Backup/external config.yaml"),
        platform="nt",
    )

    assert "'C:\\Temp\\A Home\\config.yaml'" in file_command
    assert "'C:\\Temp\\A Home\\plugins\\scope-recall'" in tree_command
    assert "'C:\\Temp\\external config.yaml'" in symlink_command
    for command in (file_command, tree_command, symlink_command):
        assert r"C:\Temp\A Home/" not in command
        assert r"C:\Temp\B Backup/" not in command


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
    file_result = _run_recovery_command(file_command)
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
    tree_result = _run_recovery_command(tree_command)
    assert tree_result.returncode == 0, tree_result.stderr + tree_result.stdout
    assert (destination_tree / "nested" / "marker.txt").read_text(encoding="utf-8") == "ready\n"


def test_windows_recovery_diagnostics_survive_localized_cmd_stderr():
    """GBK cmd.exe diagnostics must stay usable under UTF-8 pytest."""

    payload = (
        "'powershell.exe' 不是内部或外部命令，也不是可运行的程序或批处理文件。\r\n"
    ).encode("gbk")
    assert payload[17] == 0xB2
    with pytest.raises(UnicodeDecodeError):
        payload.decode("utf-8")

    decoded = _decode_windows_subprocess_bytes(payload)
    diagnostic = _decode_windows_subprocess_bytes(None) + decoded
    assert "powershell.exe" in diagnostic
    assert "不是内部或外部命令" in diagnostic
