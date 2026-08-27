"""Read-only Doctor report for optional extension boundaries."""

from __future__ import annotations

from typing import Any, Mapping

try:
    from .extension_boundary import (
        extension_boundary_status,
        validate_extension_boundaries,
    )
except ImportError:  # pragma: no cover - direct source-script fallback
    from extension_boundary import (  # type: ignore
        extension_boundary_status,
        validate_extension_boundaries,
    )


def extension_report(
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    errors = list(validate_extension_boundaries())
    extensions = extension_boundary_status(config)
    payload = {
        "schema_version": "scope-recall.extension-boundaries.v1",
        "extensions": extensions,
        "core_operational_when_disabled": not errors,
        "truth_authority": "sqlite",
        "global_scheduler_count_added": 0,
        "content_free": True,
    }
    check = {
        "ok": not errors,
        "errors": errors,
        "extension_count": len(extensions),
    }
    recommendations = (
        ["Repair the extension boundary registry before enabling optional capabilities."]
        if errors
        else []
    )
    return payload, check, recommendations


__all__ = ["extension_report"]
