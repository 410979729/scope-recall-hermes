"""Claude Code host: a plugin with the hooks, the MCP stdio server and the memory
skill, for a Claude Code attached to a shared store.

Claude Code has no store of its own here: ``scope-recall attach --host
claude-code`` makes its home an entry first, and this installer only writes the
plugin that runs the Codex adapter for it.  A plugin directory under
``~/.claude/skills/`` loads in every session of that user, the desktop app's
included; ``claude plugin disable <name>@skills-dir`` stops it.  Claude Code
runs hook commands through a shell (Git Bash, or PowerShell without it), so the
command is kept to words neither shell reinterprets.
"""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from scope_recall.adapters.codex.config import CodexConfigError, load_shared_client
from scope_recall.adapters.hermes.installation import attachment_path

from .install_common import SKILLS, InstallError, InstallPlan, _json_dump, _manifest_version, _require_file

HOST = "claude-code"
#: The events recorded, and how long Claude Code waits for each.  A prompt waits for its
#: recall (the entry's ``hook_processing_seconds``, at most 6 s) after the interpreter starts.
HOOK_TIMEOUTS = {"UserPromptSubmit": 15, "Stop": 10, "SessionEnd": 10}
#: The skills a Claude Code session gets.  ``scope-recall-setup`` stays out: installing and
#: upgrading the fleet is an operator's procedure, not something a coding session is asked.
CLAUDE_CODE_SKILLS = ("scope-recall-memory",)
_SHELL_WORD = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")


def data_dir(instance_root: Path) -> Path:
    return attachment_path(instance_root).parent


def config_path(instance_root: Path) -> Path:
    return attachment_path(instance_root)


def instance_wrapper_files(instance_root: Path) -> tuple[Path, ...]:
    return ()


def validate_options(agent_workspace: str | None, env_file: Path | str | None) -> tuple[str, Path | None]:
    """Claude Code starts hooks and the MCP server with its own environment, so the
    installer may hand them a credential file, as Codex's does."""
    if agent_workspace is not None and str(agent_workspace).strip():
        raise InstallError("agent_workspace is not used for Claude Code installation")
    if env_file is None or str(env_file).strip() == "":
        return "", None
    return "", _require_file(Path(env_file), "env_file")


def validate_local_platforms(values: object) -> tuple[str, ...]:
    if values:
        raise InstallError("local_platform is only used for Hermes installation")
    return ()


def unapproved_local_platforms(plan: InstallPlan) -> tuple[str, ...]:
    return ()


def approve_local_platforms(plan: InstallPlan) -> None:
    return None


def _argv(plan: InstallPlan, module: str) -> list[str]:
    argv = [plan.python_executable.as_posix(), "-I", "-B", "-m", f"scope_recall.adapters.codex.{module}",
            "--home", plan.instance_root.as_posix(), "--host", HOST]
    if plan.env_file is not None:
        argv += ["--env-file", plan.env_file.as_posix()]
    return argv


def _hook_command(plan: InstallPlan) -> str:
    argv = _argv(plan, "hook_entry")
    unsafe = [part for part in argv if not _SHELL_WORD.fullmatch(part)]
    if unsafe:
        raise InstallError("Claude Code runs a hook through a shell: keep the interpreter, home and env file on paths "
                           f"of ASCII letters, digits and ._-/: only (not {unsafe[0]!r})")
    return " ".join(argv)


def _hooks_json(plan: InstallPlan) -> dict[str, Any]:
    command = _hook_command(plan)
    return {"hooks": {event: [{"hooks": [{"type": "command", "command": command, "timeout": timeout}]}]
                      for event, timeout in sorted(HOOK_TIMEOUTS.items())}}


def _mcp_json(plan: InstallPlan) -> dict[str, Any]:
    argv = _argv(plan, "mcp_entry")
    return {"mcpServers": {"scope-recall": {"type": "stdio", "command": argv[0], "args": argv[1:]}}}


def _plugin_json(plugin_name: str) -> dict[str, Any]:
    return {
        "name": plugin_name,
        "version": _manifest_version(),
        "description": "Scope Recall: this machine's shared memory store, in Claude Code",
        "author": {"name": "Local developer"},
        "hooks": "./hooks/hooks.json",
        "mcpServers": "./.mcp.json",
    }


def planned_files(plan: InstallPlan) -> dict[Path, str | bytes]:
    return {
        plan.target_plugin_dir / ".claude-plugin" / "plugin.json": _json_dump(_plugin_json(plan.target_plugin_dir.name)),
        plan.target_plugin_dir / "hooks" / "hooks.json": _json_dump(_hooks_json(plan)),
        plan.target_plugin_dir / ".mcp.json": _json_dump(_mcp_json(plan)),
        **{plan.target_plugin_dir / "skills" / name / "SKILL.md": SKILLS[name].read_text(encoding="utf-8")
           for name in CLAUDE_CODE_SKILLS},
    }


def foreign_instance_entries(instance_root: Path) -> list[str]:
    """A home this installer is asked to create: Claude Code's is only ever an attached one."""
    return [f"{instance_root} is not attached to a shared store; run scope-recall attach --host claude-code first"]


def initialize_instance(plan: InstallPlan) -> str:
    raise InstallError("Claude Code joins a shared store: attach its home first (scope-recall attach --host claude-code)")


def _bound(instance_root: Path):
    try:
        return load_shared_client(instance_root, HOST)
    except CodexConfigError as exc:
        raise InstallError(f"existing Claude Code binding is unusable: {exc}") from exc


def installation_id(instance_root: Path) -> str:
    return _bound(instance_root).installation_id


def validate_reuse(plan: InstallPlan) -> None:
    config = _bound(plan.instance_root)
    if config.agent_id != plan.agent_id:
        raise InstallError("existing Claude Code entry agent_id mismatch: the store's is " + config.agent_id)
    if config.test_mode != plan.test_mode:
        raise InstallError(
            "existing Claude Code entry test_mode mismatch: "
            f"stored={config.test_mode}, requested={plan.test_mode}"
        )


def purge_identity(instance_root: Path) -> tuple[Path, str, str, Path]:
    raise InstallError("an entry of a shared store is never purged from its home; detach it instead")
