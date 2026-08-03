#!/usr/bin/env python3
"""Fetch and verify the immutable Hermes source used by CI and release builds."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


HERMES_REPOSITORY = "https://github.com/NousResearch/hermes-agent.git"
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _run(*args: str, cwd: Path | None = None, capture: bool = False) -> str:
    result = subprocess.run(
        list(args),
        cwd=cwd,
        check=True,
        text=True,
        capture_output=capture,
    )
    return result.stdout.strip() if capture else ""


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
    raise SystemExit(main())
