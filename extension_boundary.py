"""Canonical Scope Recall 2.0 extension boundary registry."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

try:
    from .gating import config_bool
except ImportError:  # pragma: no cover - direct source-script fallback
    from gating import config_bool  # type: ignore


@dataclass(frozen=True)
class ExtensionBoundary:
    name: str
    data_role: str
    enable_path: tuple[str, ...]
    disable_path: tuple[str, ...]
    scheduler_owner: str
    core_startup_required: bool = False
    truth_authority: bool = False


EXTENSION_BOUNDARIES: tuple[ExtensionBoundary, ...] = (
    ExtensionBoundary(
        name="graph",
        data_role="rebuildable relation-graph companion",
        enable_path=(
            "relation_extraction_enabled=true",
            "retrieval.relation_rerank_enabled=true",
        ),
        disable_path=(
            "relation_extraction_enabled=false",
            "retrieval.relation_rerank_enabled=false",
        ),
        scheduler_owner="core-background",
    ),
    ExtensionBoundary(
        name="experience",
        data_role="optional procedural evidence and reuse",
        enable_path=("experience.enabled=true",),
        disable_path=("experience.enabled=false",),
        scheduler_owner="core-background",
    ),
    ExtensionBoundary(
        name="playbook",
        data_role="optional Experience procedure projection",
        enable_path=("experience.enabled=true",),
        disable_path=("experience.enabled=false",),
        scheduler_owner="core-background",
    ),
    ExtensionBoundary(
        name="reflection",
        data_role="optional citation-grounded derived candidate",
        enable_path=("reflection.enabled=true",),
        disable_path=("reflection.enabled=false",),
        scheduler_owner="none",
    ),
    ExtensionBoundary(
        name="external_bridge",
        data_role="explicit export/import interoperability adapter",
        enable_path=("explicit standalone API invocation",),
        disable_path=("no automatic registration or invocation",),
        scheduler_owner="none",
    ),
)


def _nested_mapping(config: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key)
    return dict(value) if isinstance(value, Mapping) else {}


def extension_enabled(config: Mapping[str, Any], name: str) -> bool:
    """Report automatic runtime enablement without importing an extension."""

    if name == "graph":
        retrieval = _nested_mapping(config, "retrieval")
        return config_bool(
            dict(config), "relation_extraction_enabled", True
        ) or config_bool(retrieval, "relation_rerank_enabled", False)
    if name in {"experience", "playbook"}:
        return config_bool(_nested_mapping(config, "experience"), "enabled", True)
    if name == "reflection":
        return config_bool(_nested_mapping(config, "reflection"), "enabled", False)
    if name == "external_bridge":
        return False
    raise KeyError(name)


def extension_boundary_status(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return content-free status for every frozen extension boundary."""

    rows: list[dict[str, Any]] = []
    for boundary in EXTENSION_BOUNDARIES:
        row = asdict(boundary)
        row["enabled"] = extension_enabled(config, boundary.name)
        row["automatic"] = boundary.name != "external_bridge"
        rows.append(row)
    return rows


def validate_extension_boundaries() -> tuple[str, ...]:
    """Prove that no extension is a Core authority or scheduler owner."""

    errors: list[str] = []
    expected = {"graph", "experience", "playbook", "reflection", "external_bridge"}
    names = {boundary.name for boundary in EXTENSION_BOUNDARIES}
    if names != expected or len(names) != len(EXTENSION_BOUNDARIES):
        errors.append("extension registry is incomplete or duplicated")
    for boundary in EXTENSION_BOUNDARIES:
        if boundary.core_startup_required:
            errors.append(f"{boundary.name} is a Core startup prerequisite")
        if boundary.truth_authority:
            errors.append(f"{boundary.name} claims truth authority")
        if boundary.scheduler_owner not in {"none", "core-background"}:
            errors.append(f"{boundary.name} creates an unowned scheduler")
        if not boundary.disable_path:
            errors.append(f"{boundary.name} has no disable path")
    return tuple(errors)


__all__ = [
    "EXTENSION_BOUNDARIES",
    "ExtensionBoundary",
    "extension_boundary_status",
    "extension_enabled",
    "validate_extension_boundaries",
]
