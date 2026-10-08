"""Shared offline Codex hook fixtures."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from scope_recall.adapters.clients import CodexHookHandler, install_codex_scope_recall


@pytest.fixture(scope="session")
def past_the_parser():
    """``past_the_parser(prefix, suffix)``: a payload with a list nested past what this interpreter's JSON parser
    takes between them.  Where the parser gives up depends on the interpreter: 1,000 levels on Python 3.11, 3,000 on
    3.12 for Windows, 10,000 elsewhere, the stack itself on 3.14.  Nested 1,200 levels, a payload was refused only on
    3.11 and parsed elsewhere, so CI (3.12) never reached the refusal it tested (rc11)."""
    depth = 1000
    while True:
        try:
            json.loads("[" * depth + "]" * depth)
        except RecursionError:
            break
        depth *= 2
        if depth > 1 << 15:
            pytest.skip("this interpreter's JSON parser takes any nesting a hook's 64 kB can hold")

    def nested(prefix: bytes = b"", suffix: bytes = b"") -> bytes:
        return prefix + b"[" * depth + b"]" * depth + suffix

    return nested


@dataclass
class FixedClock:
    now = "2026-09-06T12:00:00Z"
    _mono = 100.0

    def utc_now(self) -> str:
        return self.now

    def monotonic(self) -> float:
        return self._mono


@pytest.fixture
def project_root(tmp_path) -> Path:
    root = tmp_path / "TEST-project"
    root.mkdir()
    return root


@pytest.fixture
def installed(project_root, tmp_path):
    clock = FixedClock()
    config, core = install_codex_scope_recall(tmp_path / "install", project_root=project_root, clock=clock)
    return config, core, clock, project_root


@pytest.fixture
def handler(installed):
    config, core, clock, project_root = installed
    return CodexHookHandler(config, core=core, clock=clock), project_root, config
