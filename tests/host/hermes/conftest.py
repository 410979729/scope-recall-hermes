"""Shared offline Hermes adapter fixtures."""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest
from scope_recall.adapters.hermes import ScopeRecallHermesAdapter, install_hermes_scope_recall, kept_captures

from tests.store_clock import StoreMeteredClock


@dataclass
class FixedClock:
    now = "2026-09-06T12:00:00Z"
    _mono = 100.0

    def utc_now(self) -> str:
        return self.now

    def monotonic(self) -> float:
        return time.monotonic() if isinstance(time.monotonic, StoreMeteredClock) else self._mono


@pytest.fixture
def hermes_home(tmp_path):
    home = tmp_path / "TEST-P11-hermes-home"
    home.mkdir()
    return home


@pytest.fixture
def initialize_kwargs(hermes_home):
    return {
        "hermes_home": str(hermes_home),
        "platform": "cli",
        "agent_context": "primary",
        "agent_identity": "TEST-agent",
        "agent_workspace": "TEST-workspace",
        "user_id": "TEST-user",
        "parent_session_id": "",
    }


@pytest.fixture
def installed_core(hermes_home, initialize_kwargs):
    """Install a synthetic core sharing the admission clock with its adapter."""
    clock = FixedClock()
    _binding, core = install_hermes_scope_recall(
        hermes_home,
        agent_id=initialize_kwargs["agent_identity"],
        platform=initialize_kwargs["platform"],
        user_id=initialize_kwargs["user_id"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        test_mode=False,
        clock=clock,
    )
    return core, clock


@pytest.fixture
def kept_capture_seconds(monkeypatch):
    """Real seconds spent writing kept captures to disk so far, as a function to call.

    A shutdown writes each capture it could not store to its own file and syncs it at the file system's speed, after
    its store budget; no store transaction meters that time, so a shutdown budget check leaves it out.
    """
    write, spent = kept_captures.keep, []

    def timed(*args, **kwargs):
        began = time.perf_counter()
        try:
            return write(*args, **kwargs)
        finally:
            spent.append(time.perf_counter() - began)

    monkeypatch.setattr(kept_captures, "keep", timed)
    return lambda: sum(spent)


@pytest.fixture
def adapter(installed_core, initialize_kwargs):
    core, clock = installed_core
    provider = ScopeRecallHermesAdapter(core=core, clock=clock)
    provider.initialize("TEST-session-1", **initialize_kwargs)
    yield provider, clock
    provider.shutdown()
