"""Shared offline Codex hook fixtures."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from scope_recall.adapters.codex import CodexHookHandler, install_codex_scope_recall


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
