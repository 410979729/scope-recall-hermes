"""Canonical Scope Recall 2.0 tool profiles and content-free schema budgets."""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

CANONICAL_TOOL_PROFILES = (
    "core",
    "compatibility",
    "maintenance",
    "developer",
    "extension",
)

TOOL_PROFILE_ALIASES = {
    "compact": "core",
    "standard": "compatibility",
    "legacy": "compatibility",
    "compat": "compatibility",
}

SUPPORTED_TOOL_PROFILES = (*CANONICAL_TOOL_PROFILES, *TOOL_PROFILE_ALIASES)


def normalize_tool_profile(value: object) -> str:
    """Return one canonical profile; unknown values fail closed to ``core``."""

    raw = str(value or "core").strip().lower().replace("-", "_")
    canonical = TOOL_PROFILE_ALIASES.get(raw, raw)
    return canonical if canonical in CANONICAL_TOOL_PROFILES else "core"


def schema_budget(schemas: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Measure the exact content-free schema surface sent to a model."""

    material = [dict(schema) for schema in schemas]
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    schema_chars = len(encoded)
    return {
        "tool_count": len(material),
        "schema_chars": schema_chars,
        "estimated_schema_tokens": (schema_chars + 3) // 4,
    }


__all__ = [
    "CANONICAL_TOOL_PROFILES",
    "SUPPORTED_TOOL_PROFILES",
    "TOOL_PROFILE_ALIASES",
    "normalize_tool_profile",
    "schema_budget",
]
