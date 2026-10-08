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

import re
from pathlib import Path
from typing import Any

from . import install_client
from .install_client import (  # noqa: F401 -- install.py calls these on every host's module
    approve_local_platforms,
    config_path,
    data_dir,
    home_plugin_dir,
    host_config_files,
    instance_wrapper_files,
    purge_identity,
    unapproved_local_platforms,
    unapproved_owner_logins,
    validate_local_platforms,
    validate_owner_logins,
)
from .install_common import SKILLS, InstallError, InstallPlan, json_dump, manifest_version

HOST = "claude-code"
#: The events recorded, and how long Claude Code waits for each.  A prompt waits for its
#: recall (the entry's ``hook_processing_seconds``, at most 6 s) after the interpreter starts.
HOOK_TIMEOUTS = {"UserPromptSubmit": 15, "Stop": 10, "SessionEnd": 10}
#: The skills a Claude Code session gets.  ``scope-recall-setup`` stays out: installing and
#: upgrading the fleet is an operator's procedure, not something a coding session is asked.
CLAUDE_CODE_SKILLS = ("scope-recall-memory",)
SHELL_WORD = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")
_ENTRY = install_client.AttachedEntry(HOST, "Claude Code")
validate_options = _ENTRY.validate_options
foreign_instance_entries = _ENTRY.foreign_instance_entries
initialize_instance = _ENTRY.initialize_instance
installation_id = _ENTRY.installation_id
validate_reuse = _ENTRY.validate_reuse


def _argv(plan: InstallPlan, module: str) -> list[str]:
    argv = [
        plan.python_executable.as_posix(),
        "-I",
        "-B",
        "-m",
        f"scope_recall.adapters.codex.{module}",
        "--home",
        plan.instance_root.as_posix(),
        "--host",
        HOST,
    ]
    if plan.env_file is not None:
        argv += ["--env-file", plan.env_file.as_posix()]
    return argv


def _hook_command(plan: InstallPlan) -> str:
    argv = _argv(plan, "hook_entry")
    unsafe = [part for part in argv if not SHELL_WORD.fullmatch(part)]
    if unsafe:
        raise InstallError(
            "Claude Code runs a hook through a shell: keep the interpreter, home and env file on paths "
            f"of ASCII letters, digits and ._-/: only (not {unsafe[0]!r})"
        )
    return " ".join(argv)


def _hooks_json(plan: InstallPlan) -> dict[str, Any]:
    command = _hook_command(plan)
    return {
        "hooks": {
            event: [{"hooks": [{"type": "command", "command": command, "timeout": timeout}]}]
            for event, timeout in sorted(HOOK_TIMEOUTS.items())
        }
    }


def _mcp_json(plan: InstallPlan) -> dict[str, Any]:
    argv = _argv(plan, "mcp_entry")
    return {"mcpServers": {"scope-recall": {"type": "stdio", "command": argv[0], "args": argv[1:]}}}


def _plugin_json(plugin_name: str) -> dict[str, Any]:
    return {
        "name": plugin_name,
        "version": manifest_version(),
        "description": "Scope Recall: this machine's shared memory store, in Claude Code",
        "author": {"name": "Local developer"},
        "hooks": "./hooks/hooks.json",
        "mcpServers": "./.mcp.json",
    }


def planned_files(plan: InstallPlan) -> dict[Path, str | bytes]:
    return {
        plan.target_plugin_dir / ".claude-plugin" / "plugin.json": json_dump(_plugin_json(plan.target_plugin_dir.name)),
        plan.target_plugin_dir / "hooks" / "hooks.json": json_dump(_hooks_json(plan)),
        plan.target_plugin_dir / ".mcp.json": json_dump(_mcp_json(plan)),
        **{
            plan.target_plugin_dir / "skills" / name / "SKILL.md": SKILLS[name].read_text(encoding="utf-8")
            for name in CLAUDE_CODE_SKILLS
        },
    }
