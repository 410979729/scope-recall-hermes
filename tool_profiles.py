"""Canonical Scope Recall 2.0 tool profiles and content-free schema budgets."""

from __future__ import annotations

import hashlib
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

CORE_TOOL_SCHEMA_POLICY = {
    "schema_version": "scope-recall.core-tool-schema-policy.v1",
    "decision": "D-013",
    "profile": "core",
    "expected_tool_count": 6,
    "expected_schema_chars": 9588,
    "maximum_schema_chars": 9600,
    "maximum_estimated_schema_tokens": 2400,
    "canonical_schema_sha256": "d19b08d445c17c265ee216acfe06060714ac1848917d5e7d70aa0dc05edd615d",
}


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


def canonical_schema_sha256(schemas: Iterable[Mapping[str, Any]]) -> str:
    material = [dict(schema) for schema in schemas]
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def core_schema_budget_gate(
    schemas: Iterable[Mapping[str, Any]],
) -> dict[str, object]:
    """Enforce the reviewed D-013 core snapshot, cost ceilings, and digest."""

    material = [dict(schema) for schema in schemas]
    measured = schema_budget(material)
    digest = canonical_schema_sha256(material)
    failures: list[str] = []
    if measured["tool_count"] != CORE_TOOL_SCHEMA_POLICY["expected_tool_count"]:
        failures.append("core tool-count snapshot changed without policy update")
    if measured["schema_chars"] != CORE_TOOL_SCHEMA_POLICY["expected_schema_chars"]:
        failures.append("core schema character snapshot changed without policy update")
    if measured["schema_chars"] > CORE_TOOL_SCHEMA_POLICY["maximum_schema_chars"]:
        failures.append("core schema character ceiling exceeded")
    if (
        measured["estimated_schema_tokens"]
        > CORE_TOOL_SCHEMA_POLICY["maximum_estimated_schema_tokens"]
    ):
        failures.append("core estimated schema-token ceiling exceeded")
    if digest != CORE_TOOL_SCHEMA_POLICY["canonical_schema_sha256"]:
        failures.append("core canonical schema digest changed without policy update")
    return {
        "ok": not failures,
        "profile": "core",
        "decision": CORE_TOOL_SCHEMA_POLICY["decision"],
        "measured": {**measured, "canonical_schema_sha256": digest},
        "policy": dict(CORE_TOOL_SCHEMA_POLICY),
        "failures": failures,
    }


__all__ = [
    "CANONICAL_TOOL_PROFILES",
    "CORE_TOOL_SCHEMA_POLICY",
    "SUPPORTED_TOOL_PROFILES",
    "TOOL_PROFILE_ALIASES",
    "canonical_schema_sha256",
    "core_schema_budget_gate",
    "normalize_tool_profile",
    "schema_budget",
]
