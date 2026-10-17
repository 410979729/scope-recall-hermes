"""Shared offline Codex hook fixtures."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from scope_recall.adapters.clients import CodexHookHandler, install_codex_scope_recall
from scope_recall.adapters.clients.handler import SystemHookClock
from scope_recall.core.composition import SystemClock


@pytest.fixture(scope="session")
def past_the_parser():
    """``past_the_parser(prefix, suffix)``: a payload with a list nested past what this interpreter's JSON parser
    takes between them.  Where the parser gives up depends on the interpreter: 1,000 levels on Python 3.11, 3,000 on
    3.12 for Windows, 10,000 elsewhere, the stack itself on 3.14.  Nested 1,200 levels, a payload is refused only on
    3.11 and parsed elsewhere, so CI (3.12) would never reach the refusal it tests."""
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


# A hook's budget runs on its clock (``SystemHookClock`` unless a test gives it another), and one write on a busy
# Windows runner can take seconds: a prompt's capture that took 6.5 s left a 6 s hook no time to ask its server.  These
# keep a test's outcome from depending on how long the runner's store takes.


@pytest.fixture
def still_clocks(monkeypatch):
    """Every hook's clock stands still, and so does that of every ``MemoryCore`` given none: no hook runs out of its
    budget and no recall out of its time.  For a test of what a hook stores and answers, not of when its budget ends."""
    now = time.monotonic()
    monkeypatch.setattr(SystemHookClock, "monotonic", lambda _clock: now)
    monkeypatch.setattr(SystemClock, "monotonic", lambda _clock: now)


@pytest.fixture
def storing_takes_no_hook_time(monkeypatch):
    """A hook's clock stands still while the hook stores what it was sent, and runs otherwise: for a test of how a hook
    spends its budget on the entry's server.  Returns a list of one item, the seconds the hooks have spent storing."""
    stored = [0.0]
    capture = CodexHookHandler.capture

    def timed(self, *args, **kwargs):
        started = time.monotonic()
        try:
            return capture(self, *args, **kwargs)
        finally:
            stored[0] += time.monotonic() - started

    monkeypatch.setattr(CodexHookHandler, "capture", timed)
    monkeypatch.setattr(SystemHookClock, "monotonic", lambda _clock: time.monotonic() - stored[0])
    return stored


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
