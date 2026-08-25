#!/usr/bin/env python3
"""Fetch and verify the immutable Hermes source used by CI and release builds."""

from __future__ import annotations

import os
import json
import re
import signal
import subprocess
import sys
from pathlib import Path


HERMES_REPOSITORY = "https://github.com/NousResearch/hermes-agent.git"
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
COMMAND_TIMEOUT_SECONDS = 180
TERMINATION_GRACE_SECONDS = 10


class CheckoutCommandError(RuntimeError):
    """Structured, secret-free failure for a bounded checkout command."""

    def __init__(
        self,
        error: str,
        command: list[str],
        *,
        timeout_seconds: int,
        returncode: int | None = None,
    ) -> None:
        super().__init__(error)
        self.error = error
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.returncode = returncode

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "command": self.command,
            "error": self.error,
            "timeout_seconds": self.timeout_seconds,
        }
        if self.returncode is not None:
            payload["returncode"] = self.returncode
        return payload


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Force-stop the isolated process group/tree after a hard timeout."""

    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=TERMINATION_GRACE_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        process.kill()


def _run(
    *args: str,
    cwd: Path | None = None,
    capture: bool = False,
    timeout_seconds: int = COMMAND_TIMEOUT_SECONDS,
) -> str:
    command = list(args)
    process = subprocess.Popen(
        list(args),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE,
        start_new_session=os.name != "nt",
        creationflags=(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if os.name == "nt"
            else 0
        ),
    )
    try:
        stdout, _stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        try:
            process.communicate(timeout=TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise CheckoutCommandError(
            "command_timeout",
            command,
            timeout_seconds=timeout_seconds,
        ) from exc
    if process.returncode != 0:
        raise CheckoutCommandError(
            "command_failed",
            command,
            timeout_seconds=timeout_seconds,
            returncode=process.returncode,
        )
    return (stdout or "").strip() if capture else ""


def main() -> int:
    commit = str(os.environ.get("HERMES_AGENT_COMMIT") or "").strip().lower()
    if not FULL_SHA_RE.fullmatch(commit):
        raise SystemExit("HERMES_AGENT_COMMIT must be a full lowercase commit SHA")

    destination = Path(".hermes-agent-src")
    if destination.exists():
        raise SystemExit(f"refusing to reuse existing checkout: {destination}")

    _run("git", "init", "--quiet", str(destination))
    _run("git", "config", "core.longpaths", "true", cwd=destination)
    _run("git", "remote", "add", "origin", HERMES_REPOSITORY, cwd=destination)
    _run(
        "git",
        "fetch",
        "--quiet",
        "--depth=1",
        "--no-tags",
        "origin",
        commit,
        cwd=destination,
    )
    _run("git", "checkout", "--quiet", "--detach", "FETCH_HEAD", cwd=destination)
    actual = _run("git", "rev-parse", "HEAD", cwd=destination, capture=True)
    if actual != commit:
        raise SystemExit(f"Hermes checkout verification failed: expected {commit}, got {actual}")
    checkout_drift = _run(
        "git", "status", "--porcelain=v1", cwd=destination, capture=True
    )
    if checkout_drift:
        raise SystemExit(
            "Hermes checkout verification failed: tracked files were not materialized cleanly"
        )

    print(f"Verified Hermes source commit {actual}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CheckoutCommandError as exc:
        print(json.dumps(exc.as_dict(), sort_keys=True), file=sys.stderr)
        raise SystemExit(1) from None
