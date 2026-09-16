"""Codex host: the plugin's hooks.json, .mcp.json, plugin.json and Windows hook
launcher, plus the instance binding a receipt-backed uninstall verifies."""
from __future__ import annotations

from pathlib import Path
import shlex
from typing import Any

from scope_recall.adapters.codex.config import CONFIG_FILENAME, install_codex_scope_recall, load_codex_config

from .install_common import (
    BACKUP_DIRNAME,
    RECEIPT_FILENAME,
    SETUP_SKILL,
    InstallError,
    InstallPlan,
    _json_dump,
    _manifest_version,
    _reject_symlink_chain,
    _require_file,
)

CODEX_HOOK_EVENTS = frozenset({"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "Interrupt", "SessionEnd"})
WINDOWS_HOOK_LAUNCHER = "scope-recall-hook.cmd"


def data_dir(instance_root: Path) -> Path:
    return instance_root / "data"


def config_path(instance_root: Path) -> Path:
    return instance_root / CONFIG_FILENAME


def instance_wrapper_files(instance_root: Path) -> tuple[Path, ...]:
    """Codex keeps every wrapper inside the plugin directory."""
    return ()


def validate_options(agent_workspace: str | None, env_file: Path | str | None) -> tuple[str, Path | None]:
    """Codex starts the MCP server and hooks with its own environment, so the
    installer may hand them a credential file; audience workspaces are a Hermes
    concept and are refused here."""
    if agent_workspace is not None and str(agent_workspace).strip():
        raise InstallError("agent_workspace is not used for Codex installation")
    if env_file is None or str(env_file).strip() == "":
        return "", None
    return "", _require_file(Path(env_file), "env_file")


def _hook_argv(python_executable: Path, config: Path, *, env_file: Path | None = None) -> list[str]:
    argv = [str(python_executable), "-I", "-B", "-m", "scope_recall.adapters.codex.hook_entry", "--config", str(config)]
    if env_file is not None:
        argv += ["--env-file", str(env_file)]
    return argv


def _windows_hook_launcher_bytes(python_executable: Path, config: Path, *, env_file: Path | None = None) -> bytes:
    """UTF-8 .cmd so cmd.exe can start python without a PowerShell EncodedCommand tax."""
    argv = _hook_argv(python_executable, config, env_file=env_file)
    quoted = " ".join('"' + part.replace('"', "") + '"' for part in argv)
    return ("@echo off\r\nchcp 65001 >nul\r\n" + quoted + "\r\nexit /b %ERRORLEVEL%\r\n").encode("utf-8")


def _hook_command(
    python_executable: Path,
    config: Path,
    *,
    windows_launcher: Path | None = None,
    write_launcher: bool = True,
    env_file: Path | None = None,
) -> tuple[str, str]:
    """POSIX command line plus the Windows launcher path Codex runs through ``cmd.exe /C``.

    A PowerShell EncodedCommand wrapper costs ~0.5-1.0s and pushes capture past
    the frozen 2s hook timeout; a UTF-8 .cmd next to hooks.json keeps
    Unicode/space paths literal without that tax.
    """
    argv = _hook_argv(python_executable, config, env_file=env_file)
    launcher = windows_launcher if windows_launcher is not None else Path(config).with_name(WINDOWS_HOOK_LAUNCHER)
    if write_launcher:
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_bytes(_windows_hook_launcher_bytes(python_executable, config, env_file=env_file))
    return shlex.join(argv), str(launcher.resolve())


def _hooks_json(python_executable: Path, config: Path, *, windows_launcher: Path, env_file: Path | None = None) -> dict[str, Any]:
    command, command_windows = _hook_command(
        python_executable, config, windows_launcher=windows_launcher, write_launcher=False, env_file=env_file
    )
    hook = {"type": "command", "command": command, "commandWindows": command_windows, "timeout": 2}
    return {"hooks": {event: [{"hooks": [dict(hook)]}] for event in sorted(CODEX_HOOK_EVENTS)}}


def _mcp_json(python_executable: Path, config: Path, workspace: Path, *, env_file: Path | None = None) -> dict[str, Any]:
    args = ["-I", "-B", "-m", "scope_recall.adapters.codex.mcp_entry", "--config", str(config), "--workspace", str(workspace)]
    if env_file is not None:
        # Codex starts the server with its own environment; the key names the
        # runtime config declares are read from this file by the entry itself.
        args += ["--env-file", str(env_file)]
    return {"mcpServers": {"scope-recall": {"command": str(python_executable), "args": args}}}


def _plugin_json(plugin_name: str) -> dict[str, Any]:
    return {
        "name": plugin_name,
        "version": _manifest_version(),
        "description": "Scope Recall local Codex plugin",
        "author": {"name": "Local developer"},
        "interface": {
            "displayName": "Scope Recall",
            "shortDescription": "Use Scope Recall in Codex.",
            "longDescription": "Scope Recall adds a local Codex plugin over the installed core.",
            "developerName": "Local developer",
            "category": "Productivity",
            "capabilities": [],
            "defaultPrompt": "Help me use Scope Recall.",
        },
        "mcpServers": "./.mcp.json",
    }


def planned_files(plan: InstallPlan) -> dict[Path, str | bytes]:
    config = config_path(plan.instance_root)
    launcher = plan.target_plugin_dir / "hooks" / WINDOWS_HOOK_LAUNCHER
    return {
        plan.target_plugin_dir / ".codex-plugin" / "plugin.json": _json_dump(_plugin_json(plan.target_plugin_dir.name)),
        launcher: _windows_hook_launcher_bytes(plan.python_executable, config, env_file=plan.env_file),
        plan.target_plugin_dir / "hooks" / "hooks.json": _json_dump(
            _hooks_json(plan.python_executable, config, windows_launcher=launcher, env_file=plan.env_file)
        ),
        plan.target_plugin_dir / "skills" / "scope-recall-setup" / "SKILL.md": SETUP_SKILL.read_text(encoding="utf-8"),
        plan.target_plugin_dir / ".mcp.json": _json_dump(
            _mcp_json(plan.python_executable, config, plan.project_root, env_file=plan.env_file)
        ),
    }


def foreign_instance_entries(instance_root: Path) -> list[str]:
    """Anything beside the receipt and backups in a not-yet-initialized instance root."""
    if not instance_root.exists():
        return []
    return [str(child) for child in instance_root.iterdir() if child.name not in {RECEIPT_FILENAME, BACKUP_DIRNAME}]


def initialize_instance(plan: InstallPlan) -> str:
    config, _core = install_codex_scope_recall(
        plan.instance_root,
        project_root=plan.project_root,
        agent_id=plan.agent_id,
        allow_owner_private=True,
        test_mode=plan.test_mode,
    )
    return config.installation_id


def installation_id(instance_root: Path) -> str:
    return load_codex_config(config_path(instance_root)).installation_id


def validate_reuse(plan: InstallPlan) -> None:
    config = load_codex_config(config_path(plan.instance_root))
    if config.agent_id != plan.agent_id:
        raise InstallError("existing Codex installation agent_id mismatch")
    if config.test_mode != plan.test_mode:
        raise InstallError(
            "existing Codex installation test_mode mismatch: "
            f"stored={config.test_mode}, requested={plan.test_mode}"
        )


def purge_identity(instance_root: Path) -> tuple[Path, str, str, Path]:
    """Data directory, installation id, agent id and manifest path from the signed config."""
    path = config_path(instance_root)
    config = load_codex_config(path)
    _reject_symlink_chain(config.data_directory)
    _reject_symlink_chain(path)
    return config.data_directory.resolve(), config.installation_id, config.agent_id, path.resolve()
