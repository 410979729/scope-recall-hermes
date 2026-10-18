"""Shared offline Codex hook fixtures."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from scope_recall.adapters.clients import CodexHookHandler, install_codex_scope_recall
from scope_recall.adapters.clients.handler import SystemHookClock


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


@pytest.fixture
def still_clocks(metered_store):
    """Exclude physical store latency from a semantic test, on the clock every admission check reads.

    The hook, core and durable inbox share this clock. Non-store waits still advance, so a failed operation can
    time out; budget tests charge explicit store costs through the same fixture.
    """
    return metered_store(0.0)


@pytest.fixture
def storing_takes_no_hook_time(monkeypatch):
    """A hook's clock stands still while the hook stores what it was sent, and runs otherwise: for a test of what a
    hook does with the entry's server once it has stored, not of how long a host waits for it."""
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


@pytest.fixture
def project_root(tmp_path) -> Path:
    root = tmp_path / "TEST-project"
    root.mkdir()
    return root


@pytest.fixture
def installed(project_root, tmp_path, still_clocks):
    """Build a semantic install while preserving explicit hook-clock advances.

    The inbox's clock excludes physical SQL time; ``FixedClock._mono`` controls the hook's
    deadline. Lease and non-SQL waits retain their real cost, and budget tests can replace the
    zero transaction charge through ``metered_store(cost)``.
    """
    clock = FixedClock()
    config, core = install_codex_scope_recall(tmp_path / "install", project_root=project_root, clock=clock)
    return config, core, clock, project_root


@pytest.fixture
def handler(installed):
    config, core, clock, project_root = installed
    return CodexHookHandler(config, core=core, clock=clock), project_root, config
