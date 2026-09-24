"""Codex host: the plugin's hooks.json, .mcp.json, plugin.json and Windows hook
launcher, plus the instance binding a receipt-backed uninstall verifies.

A Codex home is either an installation of its own (``codex-installation.json``
and ``data/``) or, once ``scope-recall attach --host codex`` made it one, an
entry of a shared store: its wrappers then name the home, not a config."""
from __future__ import annotations

from pathlib import Path
import shlex
from typing import Any

from scope_recall.adapters.codex.config import (
    CONFIG_FILENAME,
    CodexConfigError,
    install_codex_scope_recall,
    load_codex_config,
    load_shared_client,
)
from scope_recall.adapters.hermes.installation import ATTACHMENT_FILENAME, attachment_path

from .install_common import (
    BACKUP_DIRNAME,
    RECEIPT_FILENAME,
    SKILLS,
    InstallError,
    InstallPlan,
    _json_dump,
    _manifest_version,
    _reject_symlink_chain,
    _require_file,
)

CODEX_HOOK_EVENTS = frozenset({"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "Interrupt", "SessionEnd"})
WINDOWS_HOOK_LAUNCHER = "scope-recall-hook.cmd"


def attached(instance_root: Path) -> bool:
    """Whether this home is an entry of a shared store."""
    return attachment_path(instance_root).is_file()


def data_dir(instance_root: Path) -> Path:
    # An entry keeps its pointer and runtime config here; its memory is the store's.
    return attachment_path(instance_root).parent if attached(instance_root) else instance_root / "data"


def config_path(instance_root: Path) -> Path:
    return attachment_path(instance_root) if attached(instance_root) else instance_root / CONFIG_FILENAME


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


def validate_local_platforms(values: object) -> tuple[str, ...]:
    """A Codex installation has one local user and no audiences to approve."""
    if values:
        raise InstallError("local_platform is only used for Hermes installation")
    return ()


def unapproved_local_platforms(plan: InstallPlan) -> tuple[str, ...]:
    return ()


def approve_local_platforms(plan: InstallPlan) -> None:
    return None


def _binding_argv(config: Path) -> list[str]:
    """How a hook or the MCP server finds its binding: an entry's pointer names the home, else the config."""
    if config.name == ATTACHMENT_FILENAME:
        return ["--home", str(config.parent.parent), "--host", "codex"]
    return ["--config", str(config)]


def _hook_argv(python_executable: Path, config: Path, *, env_file: Path | None = None) -> list[str]:
    argv = [str(python_executable), "-I", "-B", "-m", "scope_recall.adapters.codex.hook_entry", *_binding_argv(config)]
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


def _mcp_json(python_executable: Path, config: Path, workspace: Path | None, *, env_file: Path | None = None) -> dict[str, Any]:
    # An entry's audience is the entry's wherever Codex runs; only an installation of its own maps a workspace.
    mapped = ["--workspace", str(workspace)] if config.name != ATTACHMENT_FILENAME else []
    args = ["-I", "-B", "-m", "scope_recall.adapters.codex.mcp_entry", *_binding_argv(config), *mapped]
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
    if config.name != ATTACHMENT_FILENAME and plan.project_root is None:
        raise InstallError("project_root is required for a Codex installation of its own")
    launcher = plan.target_plugin_dir / "hooks" / WINDOWS_HOOK_LAUNCHER
    return {
        plan.target_plugin_dir / ".codex-plugin" / "plugin.json": _json_dump(_plugin_json(plan.target_plugin_dir.name)),
        launcher: _windows_hook_launcher_bytes(plan.python_executable, config, env_file=plan.env_file),
        plan.target_plugin_dir / "hooks" / "hooks.json": _json_dump(
            _hooks_json(plan.python_executable, config, windows_launcher=launcher, env_file=plan.env_file)
        ),
        **{plan.target_plugin_dir / "skills" / name / "SKILL.md": source.read_text(encoding="utf-8")
           for name, source in SKILLS.items()},
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


def _bound(instance_root: Path):
    try:
        if attached(instance_root):
            return load_shared_client(instance_root, "codex")
        return load_codex_config(config_path(instance_root))
    except CodexConfigError as exc:
        raise InstallError(f"existing Codex binding is unusable: {exc}") from exc


def installation_id(instance_root: Path) -> str:
    return _bound(instance_root).installation_id


def validate_reuse(plan: InstallPlan) -> None:
    config = _bound(plan.instance_root)
    if config.agent_id != plan.agent_id:
        raise InstallError("existing Codex installation agent_id mismatch")
    if config.test_mode != plan.test_mode:
        raise InstallError(
            "existing Codex installation test_mode mismatch: "
            f"stored={config.test_mode}, requested={plan.test_mode}"
        )


def purge_identity(instance_root: Path) -> tuple[Path, str, str, Path]:
    """Data directory, installation id, agent id and manifest path from the signed config."""
    if attached(instance_root):
        raise InstallError("an entry of a shared store is never purged from its home; detach it instead")
    path = config_path(instance_root)
    config = load_codex_config(path)
    _reject_symlink_chain(config.data_directory)
    _reject_symlink_chain(path)
    return config.data_directory.resolve(), config.installation_id, config.agent_id, path.resolve()
