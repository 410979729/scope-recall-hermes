"""Fail-closed path validation for tests and release-candidate rehearsals."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


REAL_HOME_WRITE_REFUSED = "REAL_HOME_WRITE_REFUSED"
ACTIVE_HERMES_WRITE_REFUSED = "ACTIVE_HERMES_WRITE_REFUSED"
ISOLATED_BOUNDARY_WRITE_REFUSED = "ISOLATED_BOUNDARY_WRITE_REFUSED"


class ExecutionBoundaryError(RuntimeError):
    """Raised before a test/rehearsal target can reach a protected path."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def _resolved(path: str | os.PathLike[str] | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _same_or_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _overlap(left: Path, right: Path) -> bool:
    return _same_or_within(left, right) or _same_or_within(right, left)


def validate_execution_boundary(
    *,
    isolated_root: str | os.PathLike[str] | Path,
    targets: Mapping[str, str | os.PathLike[str] | Path],
    active_hermes_home: str | os.PathLike[str] | Path,
    real_home: str | os.PathLike[str] | Path | None = None,
) -> dict[str, object]:
    """Validate every write target is isolated from real and active homes."""

    root = _resolved(isolated_root)
    declared_real = str(os.environ.get("SCOPE_RECALL_REAL_HOME") or "").strip()
    real = _resolved(real_home or declared_real or Path.home())
    active = _resolved(active_hermes_home)
    active_plugin = active / "plugins" / "scope-recall"
    if root == real:
        raise ExecutionBoundaryError(
            REAL_HOME_WRITE_REFUSED,
            "isolated root resolves to the real user home",
        )
    if _overlap(root, active) or _overlap(root, active_plugin):
        raise ExecutionBoundaryError(
            ACTIVE_HERMES_WRITE_REFUSED,
            "isolated root overlaps the active Hermes boundary",
        )
    normalized: dict[str, Path] = {}
    for name, value in targets.items():
        target = _resolved(value)
        if target == real:
            raise ExecutionBoundaryError(
                REAL_HOME_WRITE_REFUSED,
                f"{name} resolves to the real user home",
            )
        if _overlap(target, active) or _overlap(target, active_plugin):
            raise ExecutionBoundaryError(
                ACTIVE_HERMES_WRITE_REFUSED,
                f"{name} overlaps the active Hermes boundary",
            )
        if not _same_or_within(target, root):
            raise ExecutionBoundaryError(
                ISOLATED_BOUNDARY_WRITE_REFUSED,
                f"{name} is outside the isolated execution root",
            )
        normalized[name] = target
    if not normalized:
        raise ExecutionBoundaryError(
            ISOLATED_BOUNDARY_WRITE_REFUSED,
            "no explicit write targets were supplied",
        )
    return {
        "schema_version": "scope-recall.execution-boundary.v1",
        "hermes_home_kind": "isolated",
        "database_kind": "fixture-or-isolated",
        "active_instance_touched": False,
        "target_names": sorted(normalized),
        "all_targets_within_isolated_root": True,
        "active_boundary_overlap": False,
        "real_home_targeted": False,
    }


def ambient_active_hermes_home(*, real_home: Path | None = None) -> Path:
    """Return the explicitly declared active home or the platform-safe default."""

    declared = str(os.environ.get("SCOPE_RECALL_ACTIVE_HERMES_HOME") or "").strip()
    if declared:
        return _resolved(declared)
    base = _resolved(real_home or Path.home())
    if os.name == "nt":
        local_appdata = str(os.environ.get("LOCALAPPDATA") or "").strip()
        return _resolved(Path(local_appdata) / "hermes") if local_appdata else base / ".hermes"
    return base / ".hermes"
