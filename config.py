"""Runtime configuration loading and persistence for Scope Recall.

Configuration is merged from defaults and Hermes-home state; callers should use typed helpers before making safety-critical decisions."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

_CONFIG_LOAD_ERRORS_KEY = "_config_load_errors"

DEFAULT_CONFIG: dict[str, Any] = {
    "auto_recall": True,
    "auto_capture": True,
    "auto_recall_min_length": 15,
    "auto_recall_min_repeated": 8,
    "auto_recall_max_items": 3,
    "auto_recall_max_chars": 600,
    "auto_recall_per_item_max_chars": 180,
    "max_recall_per_turn": 10,
    "min_score": 0.18,
    "capture_assistant": False,
    "capture_llm": {
        "enabled": False,
        "model": "gpt-4o-mini",
        "base_url": "https://api.openai.com",
        "api_key_env": ["SCOPE_RECALL_CAPTURE_LLM_API_KEY", "OPENAI_API_KEY"],
        "max_tokens_per_turn": 2000,
        "timeout": 15.0,
        "min_user_chars": 20,
        "min_assistant_chars": 30,
    },
    "query_char_limit": 1000,
    "min_capture_length": 40,
    "capture_raw_user": False,
    "journal": {
        "allow_heuristic_fallback": False,
        "allow_session_end_llm": False,
        "enabled": True,
        "digest_on_session_end": False,
        "background_digest_enabled": True,
        "background_digest_synchronous": False,
        "extractor": "llm",
        "digest_interval_hours": 2,
        "retention_days": 0,
        "max_entries_per_digest": 500,
        "dynamic_max_entries_enabled": True,
        "dynamic_backlog_threshold": 2000,
        "max_entries_per_digest_ceiling": 1200,
        "backlog_warn_entries": 500,
        "backlog_fail_entries": 3000,
        "backlog_max_age_hours": 72,
        "no_insert_fail_streak": 3,
        "llm_chunk_chars": 7000,
        "llm_max_session_chars": 16000,
        "llm_retry_delay": 1.0,
        "llm_timeout": 60.0,
        "tool_trace_skip_names": ["todo", "skill_view", "skills_list", "session_messages"],
        "tool_trace_hard_max_chars": 4000,
        "tool_trace_max_chars": 1800,
        "tool_trace_include_output_preview": False,
        "tool_trace_preview_max_chars": 500,
    },
    "per_turn_extraction": {
        "enabled": False,
    },
    "event_digest": {
        "enabled": True,
        "write_candidates": False,
        "dry_run_log": True,
        "max_events_per_turn": 3,
    },
    "capture_hard_max_chars": 2500,
    "capture_skip_patterns": [
        r"^\[Recent Telegram chat history",
        r"^\[CONTEXT COMPACTION",
        r"Earlier turns were compacted into the summary below",
        r"Conversation continues after context compression",
        r"^\[Your active task list was preserved across context compression\]",
        r"^\[IMPORTANT: Background process ",
        r"^## Active Task(?:\n|\r|$)",
        r"^## Remaining Work(?:\n|\r|$)",
        r"^Review the conversation above and update the skill library",
        r"call the memory tool .*output only the raw json",
        r"reply with ok and nothing else",
        r"^\s*you are an ai assistant",
        r"<available_skills>[\s\S]*?</available_skills>",
    ],
    "enable_tools": True,
    "tool_schema_profile": "compact",
    "tool_schema_extra_tools": [],
    "maintenance_tools_enabled": False,
    "secret_index_tools_enabled": False,
    "experience": {
        "enabled": True,
        "prefetch_enabled": True,
        "min_query_chars": 8,
        "direct_reuse_min_confidence": 0.82,
        "allow_risky_direct_reuse": False,
        "packet_max_chars": 1400,
        "auto_promotion_enabled": False,
        "auto_promotion_limit_sessions": 20,
        "auto_promote_low_risk": False,
        "promotion_min_entries": 3,
        "promotion_min_tool_entries": 1,
        "promotion_require_verification": True,
    },
    "forgetting": {
        "enabled": True,
        "soft_archive_default": True,
        "archive_very_short": True,
        "archive_assistant_scratch": True,
        "archive_duplicates": True,
        "hard_delete_sensitive": True,
    },
    "curated_memory": {
        "mode": "single-user",
        "allowed_user_ids": [],
    },
    "shared_pool": {
        "enabled": False,
        "pool_id": "default",
        "write_enabled": False,
        "allowed_targets": ["memory", "project", "ops"],
    },
    "retrieval": {
        "mode": "hybrid",
        "lexical_weight": 0.45,
        "vector_weight": 0.55,
        "candidate_pool": 12,
        "top_k": 5,
        "min_score": 0.18,
        "vector_min_score": 0.12,
        "vector_only_min_score": 0.30,
        "include_general": "same-scope",
        "general_weight": 0.35,
        "general_min_importance": 0.2,
        "entity_scope_filter_enabled": True,
        "metric": "cosine",
        "fusion_strategy": "rrf",
        "bm25_weight": 0.15,
        "rrf_weight": 0.18,
        "rrf_k": 60,
        "rrf_min_signals": 2,
        "rrf_lexical_weight": 1.0,
        "rrf_vector_weight": 1.0,
        "rrf_bm25_weight": 1.0,
        "rrf_curated_weight": 1.25,
        "entity_distance_weight": 0.04,
        "relation_rerank_enabled": False,
        "relation_rerank_weight": 0.04,
        "relation_supersedes_boost": 0.04,
        "relation_supports_boost": 0.04,
        "relation_superseded_penalty": 0.04,
        "relation_contradicts_penalty": 0.0,
        "relation_contradiction_mode": "surface",
        "temporal_decay_enabled": False,
        "temporal_decay_weight": 0.0,
        "temporal_decay_half_life_days": 180.0,
        "temporal_decay_floor": 0.65,
        "temporal_policy_enabled": True,
        "temporal_policy_weights": {
            "durable_fact": 0.25,
            "episodic": 0.8,
            "temporary": 1.0,
            "default": 1.0,
        },
        "temporal_policy_durable_types": [
            "constraint",
            "decision",
            "environment_fact",
            "fact",
            "factual",
            "memory",
            "ops",
            "ops_procedure",
            "preference",
            "procedure",
            "project",
            "project_fact",
            "resource",
            "user_preference",
            "workflow",
        ],
        "temporal_policy_episodic_types": ["episodic", "summary"],
        "temporal_policy_temporary_types": ["scratch", "temporary", "temporary_state", "tool_trace"],
    },
    "vector": {
        "enabled": True,
        "backend": "lancedb",
        "fallback_backend": "sqlite-bruteforce",
        "table_name": "memories",
        "pgvector": {
            "dsn_env": "SCOPE_RECALL_PGVECTOR_DSN",
            "table_name": "scope_recall_vectors",
        },
        "top_k": 8,
        "sync_mode": "incremental",
        "index_general": False,
        "embedder": {
            "provider": "openai-compatible",
            "dimensions": 3072,
            "model": "gemini-embedding-001",
            "api_key_env": ["SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY"],
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "request_dimensions": False,
            "document_prefix": "",
            "query_prefix": "",
            "prompt_profile": "default-v1",
        },
        "fallback_embedder": {
            "provider": "local-hash",
            "dimensions": 256,
            "model": "hash-v1",
        },
    },
}

# Supported compatibility keys accepted in home overrides but intentionally
# absent from runtime defaults. Non-empty alias defaults could mask canonical
# journal keys selected later through ``or`` fallback chains.
CONFIG_SCHEMA_EXTRAS: dict[str, Any] = {
    "journal": {
        "api_key": "",
        "api_key_env": "",
        "api_mode": "chat_completions",
        "base_url": "",
        "chat_endpoint": "",
        "key_env": "",
        "llm_max_attempts": 3,
        "llm_provider": "",
        "llm_retry_attempts": 3,
        "model": "",
        "provider": "",
        "timeout": 60.0,
    }
}
CONFIG_OPEN_MAP_PATHS = frozenset({"identity.user_aliases"})
CONFIG_BOOL_OR_OBJECT_PATHS = frozenset({"curated_memory"})


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged



def _expand_dotted_keys(values: dict[str, Any]) -> dict[str, Any]:
    expanded: dict[str, Any] = {}
    for key, value in (values or {}).items():
        if not isinstance(key, str) or "." not in key:
            if isinstance(value, dict) and isinstance(expanded.get(key), dict):
                expanded[key] = _deep_merge(expanded[key], value)
            else:
                expanded[key] = value
            continue
        cursor = expanded
        parts = [part for part in key.split(".") if part]
        if not parts:
            continue
        for part in parts[:-1]:
            child = cursor.get(part)
            if not isinstance(child, dict):
                child = {}
                cursor[part] = child
            cursor = child
        cursor[parts[-1]] = value
    return expanded



def _config_load_error(path: Path, *, kind: str, message: str) -> dict[str, str]:
    return {"path": str(path), "kind": kind, "message": message}


def _config_type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if value is None:
        return "null"
    return "string"


def _config_value_matches_template(value: Any, expected: Any) -> bool:
    if isinstance(expected, bool):
        if isinstance(value, bool):
            return True
        if isinstance(value, str):
            return value.strip().lower() in {"1", "0", "true", "false", "yes", "no", "on", "off"}
        return False
    if isinstance(expected, int) and not isinstance(expected, bool):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(expected, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(expected, dict):
        return isinstance(value, dict)
    if isinstance(expected, list):
        return isinstance(value, list)
    if expected is None:
        return value is None
    return isinstance(value, type(expected))


def validate_config_override(
    values: dict[str, Any],
    template: dict[str, Any],
    *,
    path: Path,
    prefix: str = "",
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Filter one operator override against the source configuration contract.

    Unknown keys and incompatible leaf types are ignored rather than merged.
    Diagnostics contain key names and type names only; config values are never
    copied into doctor output where they could expose credentials.
    """

    cleaned: dict[str, Any] = {}
    errors: list[dict[str, str]] = []
    expanded = _expand_dotted_keys(values) if not prefix else values
    for key, value in expanded.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        if key not in template:
            errors.append(_config_load_error(path, kind="unknown_key", message=f"unknown config key: {dotted}"))
            continue
        expected = template[key]
        if dotted in CONFIG_BOOL_OR_OBJECT_PATHS and isinstance(value, bool):
            cleaned[key] = value
            continue
        if isinstance(expected, dict) and isinstance(value, dict):
            if dotted in CONFIG_OPEN_MAP_PATHS:
                cleaned[key] = dict(value)
                continue
            child, child_errors = validate_config_override(value, expected, path=path, prefix=dotted)
            if child:
                cleaned[key] = child
            errors.extend(child_errors)
            continue
        if not _config_value_matches_template(value, expected):
            errors.append(
                _config_load_error(
                    path,
                    kind="invalid_type",
                    message=(
                        f"invalid type for {dotted}: expected {_config_type_name(expected)}, "
                        f"got {_config_type_name(value)}"
                    ),
                )
            )
            continue
        cleaned[key] = value
    return cleaned, errors


def load_runtime_config_errors(config: dict[str, Any]) -> list[dict[str, str]]:
    """Return non-fatal runtime config loading diagnostics."""
    raw_errors = config.get(_CONFIG_LOAD_ERRORS_KEY)
    if not isinstance(raw_errors, list):
        return []
    errors: list[dict[str, str]] = []
    for item in raw_errors:
        if not isinstance(item, dict):
            continue
        errors.append(
            {
                "path": str(item.get("path") or ""),
                "kind": str(item.get("kind") or ""),
                "message": str(item.get("message") or ""),
            }
        )
    return errors


def _without_internal_config_keys(config: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of config without private runtime/diagnostic keys."""

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): clean(item) for key, item in value.items() if not str(key).startswith("_")}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return clean(config)


def load_runtime_config(plugin_dir: Path, storage_dir: Path) -> dict[str, Any]:
    config: dict[str, Any] = json.loads(json.dumps(DEFAULT_CONFIG))
    errors: list[dict[str, str]] = []
    for index, path in enumerate((plugin_dir / "config.json", storage_dir / "config.json")):
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(_config_load_error(path, kind="json_decode", message=str(exc)))
            continue
        except OSError as exc:
            errors.append(_config_load_error(path, kind="read_error", message=str(exc)))
            continue
        if isinstance(raw, dict):
            if index == 0:
                # The packaged/source config extends DEFAULT_CONFIG and is the
                # schema authority for installation-specific feature keys.
                config = _deep_merge(config, raw)
            else:
                validation_template = _deep_merge(CONFIG_SCHEMA_EXTRAS, config)
                override, validation_errors = validate_config_override(raw, validation_template, path=path)
                errors.extend(validation_errors)
                config = _deep_merge(config, override)
        else:
            errors.append(_config_load_error(path, kind="non_dict_payload", message="config payload must be a JSON object"))
    if errors:
        config[_CONFIG_LOAD_ERRORS_KEY] = errors
    return config



def save_runtime_config(values: dict[str, Any], hermes_home: str) -> None:
    """Validate and atomically persist one runtime-config update.

    Save and load deliberately share the same schema contract. Invalid dotted
    keys or leaf types reject the whole update so operators never receive a
    false success for values that the next load would discard.
    """

    path = Path(hermes_home) / "scope-recall" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_runtime_config(Path(__file__).resolve().parent, path.parent)
    expanded = _without_internal_config_keys(_expand_dotted_keys(values or {}))
    validation_template = _deep_merge(CONFIG_SCHEMA_EXTRAS, _without_internal_config_keys(existing))
    validated, errors = validate_config_override(expanded, validation_template, path=path)
    if errors:
        messages = "; ".join(str(item.get("message") or item.get("kind") or "invalid config") for item in errors)
        raise ValueError(f"runtime config update rejected: {messages}")

    merged = _deep_merge(_without_internal_config_keys(existing), validated)
    payload = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)
