from __future__ import annotations

from typing import Any

from .gating import config_bool
from .schemas import (
    SCOPE_RECALL_BENCHMARK_SCHEMA,
    SCOPE_RECALL_CONTEXT_SCHEMA,
    SCOPE_RECALL_DEDUPE_SCHEMA,
    SCOPE_RECALL_ENTITY_SCHEMA,
    SCOPE_RECALL_EXPERIENCE_PREFLIGHT_SCHEMA,
    SCOPE_RECALL_EXPERIENCE_PROMOTE_SCHEMA,
    SCOPE_RECALL_EXPERIENCE_STATS_SCHEMA,
    SCOPE_RECALL_EXPLAIN_SCHEMA,
    SCOPE_RECALL_EXPORT_SCHEMA,
    SCOPE_RECALL_FEEDBACK_SCHEMA,
    SCOPE_RECALL_FORGET_SCHEMA,
    SCOPE_RECALL_FORGETTING_REPORT_SCHEMA,
    SCOPE_RECALL_FORGETTING_RUN_SCHEMA,
    SCOPE_RECALL_GOVERN_SCHEMA,
    SCOPE_RECALL_HYGIENE_SCHEMA,
    SCOPE_RECALL_INSPECT_SCHEMA,
    SCOPE_RECALL_MEMORY_SCHEMA,
    SCOPE_RECALL_MERGE_SCHEMA,
    SCOPE_RECALL_PLAYBOOK_CREATE_SCHEMA,
    SCOPE_RECALL_PLAYBOOK_FEEDBACK_SCHEMA,
    SCOPE_RECALL_PLAYBOOK_INSPECT_SCHEMA,
    SCOPE_RECALL_PLAYBOOK_REVIEW_SCHEMA,
    SCOPE_RECALL_PLAYBOOK_SEARCH_SCHEMA,
    SCOPE_RECALL_PROBE_SCHEMA,
    SCOPE_RECALL_PROFILE_SCHEMA,
    SCOPE_RECALL_REPAIR_SCHEMA,
    SCOPE_RECALL_RELATED_SCHEMA,
    SCOPE_RECALL_SEARCH_SCHEMA,
    SCOPE_RECALL_STATS_SCHEMA,
    SCOPE_RECALL_STORE_SCHEMA,
    SCOPE_RECALL_STORE_SECRET_INDEX_SCHEMA,
    SCOPE_RECALL_UPDATE_SCHEMA,
)


def build_config_schema() -> list[dict[str, Any]]:
    return [
        {
            "key": "auto_recall",
            "description": "Enable current-turn memory recall",
            "default": "true",
            "choices": ["true", "false"],
        },
        {
            "key": "auto_capture",
            "description": "Capture turns into local memory",
            "default": "true",
            "choices": ["true", "false"],
        },
        {
            "key": "capture_llm.enabled",
            "description": "Use LLM to extract user+assistant turns into structured memory (requires API key)",
            "default": "false",
            "choices": ["true", "false"],
        },
        {
            "key": "capture_raw_user",
            "description": "Legacy fallback: store whole user turns as local scratch memory when no structured extraction candidate is found",
            "default": "false",
            "choices": ["true", "false"],
        },
        {
            "key": "capture_llm.model",
            "description": "LLM model for capture extraction (OpenAI-compatible)",
            "default": "gpt-4o-mini",
        },
        {
            "key": "vector.enabled",
            "description": "Enable the rebuildable vector companion layer",
            "default": "true",
            "choices": ["true", "false"],
        },
        {
            "key": "vector.backend",
            "description": "Vector companion backend: LanceDB for ANN search, or sqlite-bruteforce for non-AVX/native-free hosts",
            "default": "lancedb",
            "choices": ["lancedb", "sqlite-bruteforce"],
        },
        {
            "key": "vector.fallback_backend",
            "description": "Safe backend used automatically when LanceDB/PyArrow cannot be imported safely",
            "default": "sqlite-bruteforce",
            "choices": ["sqlite-bruteforce", "disabled"],
        },
        {
            "key": "vector.embedder.provider",
            "description": "Embedding backend for the vector layer (API or local model)",
            "default": "openai-compatible",
            "choices": ["openai-compatible", "openai", "sentence-transformers", "local-hash"],
        },
        {
            "key": "vector.embedder.model",
            "description": "Embedding model name for the selected vector backend",
            "default": "gemini-embedding-001",
        },
        {
            "key": "maintenance_tools_enabled",
            "description": "Enable operator-only maintenance tools such as dedupe, governance, and vector repair",
            "default": "false",
            "choices": ["true", "false"],
        },
    ]


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


def build_tool_schemas(config: dict[str, Any], *, agent_context: str = "primary") -> list[dict[str, Any]]:
    if not config_bool(config, "enable_tools", True):
        return []
    if agent_context != "primary":
        return []

    raw_experience_config = config.get("experience")
    experience_config: dict[str, Any] = dict(raw_experience_config) if isinstance(raw_experience_config, dict) else {}
    experience_enabled = config_bool(experience_config, "enabled", True)
    maintenance_enabled = config_bool(config, "maintenance_tools_enabled", False)
    secret_index_enabled = config_bool(config, "secret_index_tools_enabled", False)
    profile = _schema_profile(config)

    compact_schemas = [
        SCOPE_RECALL_STORE_SCHEMA,
        SCOPE_RECALL_SEARCH_SCHEMA,
        SCOPE_RECALL_CONTEXT_SCHEMA,
        SCOPE_RECALL_PROFILE_SCHEMA,
        SCOPE_RECALL_MEMORY_SCHEMA,
        SCOPE_RECALL_ENTITY_SCHEMA,
    ]
    standard_schemas = [
        SCOPE_RECALL_STORE_SCHEMA,
        SCOPE_RECALL_SEARCH_SCHEMA,
        SCOPE_RECALL_CONTEXT_SCHEMA,
        SCOPE_RECALL_PROFILE_SCHEMA,
        SCOPE_RECALL_PROBE_SCHEMA,
        SCOPE_RECALL_RELATED_SCHEMA,
        SCOPE_RECALL_FEEDBACK_SCHEMA,
        SCOPE_RECALL_FORGET_SCHEMA,
        SCOPE_RECALL_UPDATE_SCHEMA,
        SCOPE_RECALL_MERGE_SCHEMA,
        SCOPE_RECALL_INSPECT_SCHEMA,
        SCOPE_RECALL_EXPLAIN_SCHEMA,
        SCOPE_RECALL_EXPORT_SCHEMA,
        SCOPE_RECALL_STATS_SCHEMA,
        SCOPE_RECALL_BENCHMARK_SCHEMA,
    ]
    schemas = list(standard_schemas if profile == "standard" else compact_schemas)

    schema_by_name = {str(schema["name"]): schema for schema in [*compact_schemas, *standard_schemas]}
    if secret_index_enabled:
        schema_by_name[SCOPE_RECALL_STORE_SECRET_INDEX_SCHEMA["name"]] = SCOPE_RECALL_STORE_SECRET_INDEX_SCHEMA
    experience_schemas = [
        SCOPE_RECALL_PLAYBOOK_SEARCH_SCHEMA,
        SCOPE_RECALL_PLAYBOOK_INSPECT_SCHEMA,
        SCOPE_RECALL_EXPERIENCE_PREFLIGHT_SCHEMA,
        SCOPE_RECALL_PLAYBOOK_FEEDBACK_SCHEMA,
        SCOPE_RECALL_EXPERIENCE_STATS_SCHEMA,
    ]
    if experience_enabled:
        schema_by_name.update({str(schema["name"]): schema for schema in experience_schemas})
        if profile == "standard":
            schemas.extend(experience_schemas)
    maintenance_schemas = [
        SCOPE_RECALL_DEDUPE_SCHEMA,
        SCOPE_RECALL_GOVERN_SCHEMA,
        SCOPE_RECALL_REPAIR_SCHEMA,
        SCOPE_RECALL_HYGIENE_SCHEMA,
        SCOPE_RECALL_PLAYBOOK_CREATE_SCHEMA,
        SCOPE_RECALL_PLAYBOOK_REVIEW_SCHEMA,
        SCOPE_RECALL_EXPERIENCE_PROMOTE_SCHEMA,
        SCOPE_RECALL_FORGETTING_REPORT_SCHEMA,
        SCOPE_RECALL_FORGETTING_RUN_SCHEMA,
    ]
    if experience_enabled and maintenance_enabled:
        schema_by_name.update({str(schema["name"]): schema for schema in maintenance_schemas})
        schemas.extend(maintenance_schemas)

    if secret_index_enabled:
        schemas.append(SCOPE_RECALL_STORE_SECRET_INDEX_SCHEMA)

    seen = {str(schema["name"]) for schema in schemas}
    for name in _extra_tool_names(config.get("tool_schema_extra_tools") or []):
        schema = schema_by_name.get(name)
        if schema is None or name in seen:
            continue
        schemas.append(schema)
        seen.add(name)
    return schemas
