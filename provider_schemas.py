"""Tool and configuration schema builders exposed to Hermes.

Schema generation controls the prompt/tool surface, so compact and standard
profiles must stay aligned with dispatcher support. Runtime identity comes from
``tool_runtime_spec``; independent expected-set oracles stay outside this module.
"""

from __future__ import annotations

from typing import Any

from .config_schema import build_config_registry
from .gating import config_bool
from .tool_runtime_spec import TOOL_SPECS, visible_tool_specs


def build_config_schema() -> list[dict[str, Any]]:
    return build_config_registry()


def _schema_profile(config: dict[str, Any]) -> str:
    profile = str(config.get("tool_schema_profile") or "compact").strip().lower().replace("-", "_")
    if profile in {"legacy", "compat", "standard"}:
        return "standard"
    if profile not in {"compact", "standard"}:
        return "compact"
    return profile


def _extra_tool_names(raw_extra_tools: Any) -> list[str]:
    if isinstance(raw_extra_tools, str):
        return [item.strip() for item in raw_extra_tools.split(",")]
    if isinstance(raw_extra_tools, list):
        return [str(item).strip() for item in raw_extra_tools]
    return []


def _flag_set(config: dict[str, Any]) -> set[str]:
    flags: set[str] = set()
    raw_experience = config.get("experience")
    experience_config = dict(raw_experience) if isinstance(raw_experience, dict) else {}
    if config_bool(experience_config, "enabled", True):
        flags.add("experience")
    raw_temporal = config.get("temporal_queries")
    temporal_config = dict(raw_temporal) if isinstance(raw_temporal, dict) else {}
    if config_bool(temporal_config, "enabled", False):
        flags.add("temporal")
    raw_reflection = config.get("reflection")
    reflection_config = dict(raw_reflection) if isinstance(raw_reflection, dict) else {}
    if config_bool(reflection_config, "enabled", False):
        flags.add("reflection")
    if config_bool(config, "maintenance_tools_enabled", False):
        flags.add("maintenance")
    if config_bool(config, "secret_index_tools_enabled", False):
        flags.add("secret_index")
    return flags


def build_tool_schemas(config: dict[str, Any], *, agent_context: str = "primary") -> list[dict[str, Any]]:
    """Build the public tool schema list for compact, standard, and optional maintenance surfaces."""

    if not config_bool(config, "enable_tools", True):
        return []
    if agent_context != "primary":
        return []

    profile = _schema_profile(config)
    flags = _flag_set(config)
    schema_by_name: dict[str, dict[str, Any]] = {}
    for spec in TOOL_SPECS:
        gate = spec.feature_gate
        if gate is None:
            schema_by_name[spec.name] = spec.schema
        elif gate == "experience" and "experience" in flags:
            schema_by_name[spec.name] = spec.schema
        elif gate == "experience+maintenance" and "experience" in flags and "maintenance" in flags:
            schema_by_name[spec.name] = spec.schema
        elif gate in flags:
            schema_by_name[spec.name] = spec.schema
    schemas = [spec.schema for spec in visible_tool_specs(profile=profile, flags=flags)]
    seen = {str(schema["name"]) for schema in schemas}
    for name in _extra_tool_names(config.get("tool_schema_extra_tools") or []):
        schema = schema_by_name.get(name)
        if schema is None or name in seen:
            continue
        schemas.append(schema)
        seen.add(name)
    return schemas
