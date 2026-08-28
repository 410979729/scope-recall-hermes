"""Fail-closed HOME and active-Hermes execution boundary contracts."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.execution_boundary import (
    ACTIVE_HERMES_WRITE_REFUSED,
    ISOLATED_BOUNDARY_WRITE_REFUSED,
    REAL_HOME_WRITE_REFUSED,
    ExecutionBoundaryError,
    validate_execution_boundary,
)


def test_isolated_test_environment_declares_every_write_surface() -> None:
    hermes_home = Path(os.environ["HERMES_HOME"]).resolve()
    active = Path(os.environ["SCOPE_RECALL_ACTIVE_HERMES_HOME"]).resolve()
    targets = {
        key: Path(os.environ[key]).resolve()
        for key in (
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "TEMP",
            "TMP",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "HERMES_HOME",
            "SCOPE_RECALL_DB",
            "SCOPE_RECALL_LOG_DIR",
            "SCOPE_RECALL_LEASE_DIR",
            "SCOPE_RECALL_PLUGIN_DIR",
        )
    }
    root = hermes_home.parent

    receipt = validate_execution_boundary(
        isolated_root=root,
        targets=targets,
        active_hermes_home=active,
    )

    assert receipt["active_instance_touched"] is False
    assert receipt["all_targets_within_isolated_root"] is True


def test_real_home_target_is_refused_before_write(tmp_path: Path) -> None:
    real_home = tmp_path / "real-home"
    isolated = tmp_path / "isolated"

    with pytest.raises(ExecutionBoundaryError) as raised:
        validate_execution_boundary(
            isolated_root=isolated,
            targets={"HERMES_HOME": real_home},
            active_hermes_home=tmp_path / "active",
            real_home=real_home,
        )

    assert raised.value.code == REAL_HOME_WRITE_REFUSED


def test_active_hermes_and_plugin_descendants_are_refused(tmp_path: Path) -> None:
    active = tmp_path / "active"
    isolated = tmp_path / "isolated"

    with pytest.raises(ExecutionBoundaryError) as raised:
        validate_execution_boundary(
            isolated_root=isolated,
            targets={"SCOPE_RECALL_DB": active / "scope-recall" / "memory.sqlite3"},
            active_hermes_home=active,
            real_home=tmp_path / "real-home",
        )

    assert raised.value.code == ACTIVE_HERMES_WRITE_REFUSED


def test_target_outside_unique_isolated_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ExecutionBoundaryError) as raised:
        validate_execution_boundary(
            isolated_root=tmp_path / "isolated",
            targets={"logs": tmp_path / "other" / "logs"},
            active_hermes_home=tmp_path / "active",
            real_home=tmp_path / "real-home",
        )

    assert raised.value.code == ISOLATED_BOUNDARY_WRITE_REFUSED
