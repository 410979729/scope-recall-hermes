"""The remote client's installer: the plugin that sends a client's hooks and MCP calls to its entry's server, or for
WorkBuddy those hooks and that server merged into WorkBuddy's own files (``remote_client install``).  The installed
entry ``scope_recall.adapters.codex.remote_client`` sends ``install`` here and every other command to the client
(``adapters/clients/remote_client.py``)."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..adapters.clients.remote_client import HOOK_TIMEOUTS, RemoteClientError, absolute_path, load_client_config
from . import install_workbuddy as workbuddy
from .install_claude_code import SHELL_WORD
from .install_common import SKILLS, InstallError, manifest_version


def _hook_argv(config: dict[str, Any]) -> list[str]:
    return [
        Path(sys.executable).as_posix(),
        "-I",
        "-B",
        "-m",
        "scope_recall.adapters.codex.remote_client",
        "--config",
        config["config"].as_posix(),
    ]


def plugin_files(config: dict[str, Any], plugin_dir: Path) -> dict[Path, str]:
    """The plugin that sends this client's hooks and MCP calls to its entry's server."""
    host = config["host"]
    if host == "workbuddy":
        raise RemoteClientError(
            "a WorkBuddy client has no plugin: install merges its hooks and server into "
            "WorkBuddy's own settings (workbuddy_files)"
        )
    token = config["token_file"].read_text(encoding="utf-8").strip()
    argv = _hook_argv(config)
    mcp_url = f"{config['url']}/mcp"
    auth = {"Authorization": f"Bearer {token}"}
    skill = SKILLS["scope-recall-memory"].read_text(encoding="utf-8")
    dump = lambda value: json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"  # noqa: E731
    if host == "claude-code":
        unsafe = [part for part in argv if not SHELL_WORD.fullmatch(part)]
        if unsafe:
            raise RemoteClientError(
                "Claude Code runs a hook through a shell: keep the interpreter and client.json "
                f"on paths of ASCII letters, digits and ._-/: only (not {unsafe[0]!r})"
            )
        command = " ".join(argv)
        hooks = {
            "hooks": {
                event: [{"hooks": [{"type": "command", "command": command, "timeout": timeout}]}]
                for event, timeout in sorted(HOOK_TIMEOUTS[host].items())
            }
        }
        return {
            plugin_dir / ".claude-plugin" / "plugin.json": dump(
                {
                    "name": plugin_dir.name,
                    "version": manifest_version(),
                    "author": {"name": "Local developer"},
                    "description": "Scope Recall: a shared memory store on another machine, in Claude Code",
                    "hooks": "./hooks/hooks.json",
                    "mcpServers": "./.mcp.json",
                }
            ),
            plugin_dir / "hooks" / "hooks.json": dump(hooks),
            plugin_dir / ".mcp.json": dump(
                {"mcpServers": {"scope-recall": {"type": "http", "url": mcp_url, "headers": auth}}}
            ),
            plugin_dir / "skills" / "scope-recall-memory" / "SKILL.md": skill,
        }
    cmd = plugin_dir / "hooks" / "scope-recall-hook.cmd"
    windows = (
        "@echo off\r\nchcp 65001 >nul\r\n" + " ".join(f'"{part}"' for part in argv) + "\r\nexit /b %ERRORLEVEL%\r\n"
    )
    hooks = {
        "hooks": {
            event: [
                {
                    "hooks": [
                        {"type": "command", "command": shlex.join(argv), "commandWindows": str(cmd), "timeout": timeout}
                    ]
                }
            ]
            for event, timeout in sorted(HOOK_TIMEOUTS[host].items())
        }
    }
    return {
        plugin_dir / ".codex-plugin" / "plugin.json": dump(
            {
                "name": plugin_dir.name,
                "version": manifest_version().replace("rc", "-rc."),
                "author": {"name": "Local developer"},
                "mcpServers": "./.mcp.json",
                "description": "Scope Recall: a shared memory store on another machine, in Codex",
                "interface": {
                    "displayName": "Scope Recall",
                    "shortDescription": "Use Scope Recall in Codex.",
                    "category": "Productivity",
                    "capabilities": [],
                    "developerName": "Local developer",
                },
            }
        ),
        plugin_dir / "hooks" / "hooks.json": dump(hooks),
        cmd: windows,
        plugin_dir / ".mcp.json": dump({"mcpServers": {"scope-recall": {"url": mcp_url, "http_headers": auth}}}),
        plugin_dir / "skills" / "scope-recall-memory" / "SKILL.md": skill,
    }


def workbuddy_files(config: dict[str, Any], home: Path) -> dict[Path, bytes]:
    """WorkBuddy's own settings.json and mcp.json in ``home``, with this client's hooks and MCP server merged in by the
    local installer's rules (``maintenance/install_workbuddy.py``): the files that change, as they are to be written.

    The hooks are this client's when they run it with this ``client.json``; another Scope Recall hook (a local entry's,
    or another client's) is refused, since WorkBuddy would run both.  The server ``scope-recall`` is this client's when
    it names this client's server.
    """
    argv = _hook_argv(config)
    token = config["token_file"].read_text(encoding="utf-8").strip()
    server = {
        "type": "http",
        "url": f"{config['url']}/mcp",
        "headers": {"Authorization": f"Bearer {token}"},
        "description": workbuddy.SERVER_DESCRIPTION,
    }

    def this_client(parts: list[str]) -> bool:
        return "scope_recall.adapters.codex.remote_client" in parts and workbuddy.same_path(
            workbuddy.option(parts, "--config"), config["config"]
        )

    def this_server(value: object) -> bool:
        return isinstance(value, dict) and value.get("url") == server["url"]

    changed = {}
    try:
        command = (
            " ".join(
                [
                    workbuddy.quoted(Path(argv[0]), "interpreter"),
                    *argv[1:-1],
                    workbuddy.quoted(config["config"], "client.json"),
                ]
            )
            + workbuddy.FAIL_OPEN
        )
        for name in (workbuddy.SETTINGS_FILENAME, workbuddy.MCP_FILENAME):
            value, raw = workbuddy.read_config(home / name)
            merged = (
                workbuddy.with_hooks(value, command, HOOK_TIMEOUTS["workbuddy"], this_client)
                if name == workbuddy.SETTINGS_FILENAME
                else workbuddy.with_server(value, server, this_server)
            )
            if raw is None or merged != value:
                changed[home / name] = workbuddy.encode_config(merged, raw)
    except InstallError as exc:
        raise RemoteClientError(str(exc)) from None
    return changed


def install(config: dict[str, Any], plugin_dir: Path) -> dict[str, list[str]]:
    """Write the plugin, or for WorkBuddy merge into its own files after a copy of each goes to ``backups``."""
    if not config["token_file"].exists():
        raise RemoteClientError("no token yet: run remote_client token first")
    files: dict[Path, str] | dict[Path, bytes]
    backups = []
    if config["host"] == "workbuddy":
        if not plugin_dir.is_dir():
            raise RemoteClientError(
                f"{plugin_dir} does not exist: name WorkBuddy's home (~/.workbuddy), or start WorkBuddy once"
            )
        files = workbuddy_files(config, plugin_dir)
        kept = config["state_dir"] / "backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        for path in files:
            if path.is_file():
                kept.mkdir(parents=True, exist_ok=True)
                backups.append(shutil.copy2(path, kept / path.name))
    else:
        files = plugin_files(config, plugin_dir)
    written = []
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_name(path.name + ".tmp")
        if isinstance(content, bytes):
            pending.write_bytes(content)
        else:
            pending.write_text(content, encoding="utf-8", newline="")
        os.replace(pending, path)
        written.append(str(path))
    config["state_dir"].mkdir(parents=True, exist_ok=True)
    return {"written": written, "backups": [str(path) for path in backups]}


def main(argv: list[str]) -> int:
    """``remote_client install --config <client.json> --plugin-dir <dir>``: write the plugin, print what was written."""
    parser = argparse.ArgumentParser(prog="scope-recall-remote-client")
    parser.add_argument("--config", required=True)
    parser.add_argument("--plugin-dir", required=True)
    parsed = parser.parse_args(argv)
    try:
        config = load_client_config(absolute_path(parsed.config, "config"))
        print(json.dumps(install(config, absolute_path(parsed.plugin_dir, "plugin_dir")), ensure_ascii=False))
        return 0
    except RemoteClientError as exc:
        raise SystemExit(str(exc)) from None
