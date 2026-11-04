"""Temporary host wiring and independent evidence of capture, injection and answer."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from real_host_io import shell_command, write_json

PROBE = Path(__file__).with_name("real_host_probe.py")
HOSTS = ("claude-code", "codex")


def toml(value):
    """Serialize the small TOML value subset accepted by Codex's -c overrides."""
    if isinstance(value, dict):
        return "{" + ", ".join(json.dumps(k) + " = " + toml(v) for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(toml(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def definition(root, host, home, python):
    """Build a fresh plugin and tap without touching either real host home."""
    plugin = root / (host + "-plugin")
    evidence = root / (host + "-hook-evidence")
    evidence.mkdir(parents=True)
    hook = [
        str(python),
        "-I",
        "-B",
        str(PROBE),
        "hook",
        "--python",
        str(python),
        "--home",
        str(home),
        "--host",
        host,
        "--evidence",
        str(evidence),
    ]
    events = {
        event: [{"hooks": [dict(type="command", command=shell_command(hook), timeout=25)]}]
        for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")
    }
    if os.name == "nt" and host == "codex":
        for groups in events.values():
            groups[0]["hooks"][0]["commandWindows"] = shell_command(hook)
    mcp = dict(
        command=str(python),
        args=["-I", "-B", "-m", "scope_recall.adapters.clients.mcp_entry", "--home", str(home), "--host", host],
    )
    write_json(plugin / "hooks/hooks.json", dict(hooks=events))
    write_json(plugin / ".mcp.json", dict(mcpServers={"scope-recall": mcp}))
    write_json(
        plugin / ".claude-plugin/plugin.json",
        dict(name="test-scope-recall", version="1.0.0", hooks="./hooks/hooks.json", mcpServers="./.mcp.json"),
    )
    result = dict(plugin=str(plugin), evidence=str(evidence), hook_argv=hook, events=events, mcp=mcp)
    write_json(root / (host + "-definition.json"), result)
    return result


class Session:
    """Two host clients sharing one explicitly bound synthetic memory library."""

    def __init__(self, run, root, homes, *, python=None):
        self.run, self.root, self.homes = run, root, homes
        self.python = python or Path(sys.executable)
        root.mkdir(parents=True, exist_ok=True)
        self.definitions = {host: definition(root, host, homes[host], self.python) for host in HOSTS}

    def command(self, host, prompt, session=None):
        """Assemble invocation-only hooks/MCP; the answer prompt contains no expected value."""
        value, args = self.definitions[host], self.run.args
        if args.dry_run:
            return [
                sys.executable,
                "-I",
                "-B",
                str(PROBE),
                "fake-host",
                "--definition",
                str(self.root / (host + "-definition.json")),
                "--project",
                str(self.root),
                "--prompt",
                prompt,
                "--session",
                session or str(uuid4()),
            ]
        if host == "claude-code":
            command = [
                args.claude,
                "-p",
                prompt,
                "--plugin-dir",
                value["plugin"],
                "--mcp-config",
                str(Path(value["plugin"]) / ".mcp.json"),
                "--output-format",
                "json",
                "--setting-sources",
                "",
                "--strict-mcp-config",
                "--tools",
                "",
                "--permission-mode",
                "dontAsk",
            ]
            if session:
                command += ["--resume", session]
            if args.claude_model:
                command += ["--model", args.claude_model]
            return command
        mcp = dict(value["mcp"], env_vars=self.run.credential_names)
        command = [
            args.codex,
            "--no-daemon",
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--dangerously-bypass-hook-trust",
            "--json",
            "-s",
            "workspace-write",
            "-C",
            str(self.root),
            "-c",
            "hooks=" + toml(value["events"]),
            "-c",
            "mcp_servers=" + toml({"scope-recall": mcp}),
            "-c",
            'shell_environment_policy.inherit="all"',
            "-c",
            'approval_policy="never"',
            "-c",
            "features.shell_tool=false",
            "-c",
            "features.apply_patch_freeform=false",
        ]
        if args.codex_model:
            command += ["-m", args.codex_model]
        return command + [prompt]

    def ask(self, host, prompt, *, label, session=None):
        """Run one host and retain only its actual answer and this call's tap evidence."""
        evidence = Path(self.definitions[host]["evidence"])
        before = set(evidence.glob("*.json"))
        result = self.run.children.run(
            self.command(host, prompt, session),
            self.root / (label + ".json"),
            timeout=self.run.args.host_timeout,
            env=None if self.run.args.dry_run else self.run.host_env,
        )
        taps = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(set(evidence.glob("*.json")) - before)]
        answer, identifier = answer_text(host, result["stdout"], fake=self.run.args.dry_run)
        return dict(
            exit_code=result["exit_code"],
            answer=answer,
            session=identifier,
            hooks=taps,
            evidence=str(self.root / (label + ".json")),
            seconds=result["seconds"],
        )


def answer_text(host, output, *, fake=False):
    """Read assistant text only, excluding prompts, tool messages and diagnostics."""
    answers, session = [], None
    for line in output.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        session = row.get("session_id", session)
        if host == "claude-code" or fake:
            if isinstance(row.get("result"), str):
                answers.append(row["result"])
        elif row.get("type") == "item.completed" and row.get("item", {}).get("type") == "agent_message":
            answers.append(row["item"]["text"])
    return "\n".join(answers), session


def injected(reply, expected):
    """Require the expected value in context emitted during this actual host invocation."""
    for row in reply["hooks"]:
        if row["event"] not in {"SessionStart", "UserPromptSubmit"} or row["exit_code"]:
            continue
        try:
            output = json.loads(row["output"])
        except ValueError:
            continue
        text = output.get("hookSpecificOutput", {}).get("additionalContext", "")
        if expected in text or json.dumps(expected, ensure_ascii=True)[1:-1] in text:
            return True
    return False


def recalled(reply, expected):
    """Both automatic injection and the host's answer must contain the held-out fact."""
    return reply["exit_code"] == 0 and injected(reply, expected) and expected in reply["answer"]
