"""Shared offline Hermes adapter fixtures."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from scope_recall.adapters.hermes import ScopeRecallHermesAdapter, install_hermes_scope_recall


@dataclass
class FixedClock:
    now = "2026-09-06T12:00:00Z"
    _mono = 100.0

    def utc_now(self) -> str:
        return self.now

    def monotonic(self) -> float:
        return self._mono


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
def adapter(installed_core, initialize_kwargs):
    core, clock = installed_core
    provider = ScopeRecallHermesAdapter(core=core, clock=clock)
    provider.initialize("TEST-session-1", **initialize_kwargs)
    yield provider, clock
    provider.shutdown()
