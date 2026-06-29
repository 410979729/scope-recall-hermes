from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DESCRIPTION_OVERRIDES = {
    "auto_recall": "Enable automatic recall injection at turn start.",
    "auto_capture": "Capture eligible conversation turns into Scope Recall.",
    "journal.max_entries_per_digest": "Maximum journal entries a digest run may review before dynamic backlog expansion.",
    "journal.backlog_fail_entries": "Doctor failure threshold for unprocessed journal backlog.",
    "retrieval.mode": "Recall mode: lexical, vector, or hybrid.",
    "retrieval.relation_rerank_enabled": "Enable small relation-graph rerank bonuses after primary recall scoring.",
    "retrieval.vector_only_min_score": "Minimum score for vector-only candidates to survive recall filtering.",
    "vector.enabled": "Enable the rebuildable vector companion index.",
    "vector.backend": "Vector companion backend used for semantic recall.",
    "vector.embedder.api_key_env": "Environment variable names that may hold the embedding API key.",
    "experience.enabled": "Enable reusable Experience playbook surfaces.",
    "forgetting.hard_delete_sensitive": "Allow sensitive-data cleanup paths to hard-delete when explicitly invoked.",
}

_HIGH_RISK_PREFIXES = (
    "vector.embedder.api_key_env",
    "capture_llm.api_key_env",
    "forgetting.hard_delete_sensitive",
    "secret_index_tools_enabled",
)
_MEDIUM_RISK_PREFIXES = (
    "journal.",
    "retrieval.",
    "vector.",
    "experience.",
    "forgetting.",
    "shared_pool.",
    "identity.",
)
_RESTART_PREFIXES = (
    "vector.",
    "journal.",
    "tool_schema_",
    "maintenance_tools_enabled",
    "secret_index_tools_enabled",
    "experience.",
)
_CHOICES = {
    "tool_schema_profile": ["compact", "standard"],
    "retrieval.mode": ["lexical", "vector", "hybrid"],
    "retrieval.include_general": ["never", "same-scope", "always"],
    "retrieval.metric": ["cosine", "dot", "l2"],
    "retrieval.fusion_strategy": ["rrf", "weighted"],
    "retrieval.relation_contradiction_mode": ["surface", "suppress", "penalize"],
    "vector.backend": ["lancedb", "sqlite-bruteforce"],
    "vector.fallback_backend": ["sqlite-bruteforce", "disabled"],
    "vector.embedder.provider": ["openai-compatible", "openai", "sentence-transformers", "local-hash"],
    "vector.sync_mode": ["incremental", "rebuild"],
    "journal.extractor": ["llm", "heuristic"],
    "curated_memory.mode": ["single-user", "shared"],
}


def packaged_config_path() -> Path:
    return Path(__file__).resolve().parent / "config.json"


def load_packaged_config() -> dict[str, Any]:
    path = packaged_config_path()
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return "string"


def _description(key: str) -> str:
    if key in _DESCRIPTION_OVERRIDES:
        return _DESCRIPTION_OVERRIDES[key]
    group = key.split(".", 1)[0]
    return f"Scope Recall configuration key `{key}` in the `{group}` group."


def _risk(key: str) -> str:
    if any(key == prefix or key.startswith(f"{prefix}.") for prefix in _HIGH_RISK_PREFIXES):
        return "high"
    if any(key == prefix.rstrip(".") or key.startswith(prefix) for prefix in _MEDIUM_RISK_PREFIXES):
        return "medium"
    return "low"


def _restart_required(key: str) -> bool:
    return any(key == prefix.rstrip(".") or key.startswith(prefix) for prefix in _RESTART_PREFIXES)


def _flatten(value: Any, prefix: str = "") -> list[dict[str, Any]]:
    if isinstance(value, dict):
        rows: list[dict[str, Any]] = []
        for key in sorted(value):
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(_flatten(value[key], child_prefix))
        return rows
    row: dict[str, Any] = {
        "key": prefix,
        "type": _value_type(value),
        "default": value,
        "description": _description(prefix),
        "risk": _risk(prefix),
        "restart_required": _restart_required(prefix),
    }
    if prefix in _CHOICES:
        row["choices"] = list(_CHOICES[prefix])
    return [row]


def build_config_registry(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return _flatten(config if config is not None else load_packaged_config())


def render_configuration_markdown(registry: list[dict[str, Any]] | None = None) -> str:
    rows = registry if registry is not None else build_config_registry()
    lines = [
        "# Scope Recall Configuration Reference",
        "",
        "This file is generated from the packaged `config.json` registry. It lists every supported leaf key, its default value, risk level, and whether a Hermes restart/reload is normally required.",
        "",
    ]
    current_group = ""
    for entry in rows:
        key = str(entry["key"])
        group = key.split(".", 1)[0]
        if group != current_group:
            current_group = group
            lines.extend(["", f"## `{group}`", ""])
        default = json.dumps(entry.get("default"), ensure_ascii=False, sort_keys=True)
        choices = entry.get("choices")
        choices_text = f"; choices: `{', '.join(map(str, choices))}`" if choices else ""
        restart = "yes" if entry.get("restart_required") else "no"
        lines.append(f"- `{key}` ({entry.get('type')}; risk: `{entry.get('risk')}`; restart_required: `{restart}`{choices_text}) — {entry.get('description')} Default: `{default}`")
    lines.append("")
    return "\n".join(lines)
