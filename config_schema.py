"""Configuration schema metadata used by tools, docs, and release checks.

The schema is descriptive rather than a runtime authority; keep defaults synchronized with config loading and plugin metadata."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DESCRIPTION_OVERRIDES = {
    "auto_recall": "Enable automatic recall injection at turn start.",
    "auto_capture": "Capture eligible conversation turns into Scope Recall.",
    "memory_isolated_chat_ids": "Runtime-only chat identifiers excluded from prompt recall, tools, capture, journal, and digest surfaces.",
    "journal.max_entries_per_digest": "Maximum journal entries a digest run may review before dynamic backlog expansion.",
    "journal.backlog_fail_entries": "Doctor failure threshold for unprocessed journal backlog.",
    "journal.no_insert_fail_streak": "Doctor failure threshold for recent digest runs that processed entries but produced no durable writes for provider/schema/quality-risk reasons.",
    "relation_extraction_enabled": "Enable bounded extraction and maintenance of relation edges during memory mutations and background repair.",
    "relation_extraction_max_pairs": "Maximum comparison budget for one relation extraction operation (1 to 5000 pairs).",
    "relation_sync_neighbor_limit": "Maximum local peers synchronously compared during a foreground memory mutation (1 to 256 peers).",
    "relation_rebuild_chunk_pairs": "Maximum relation pairs processed by one background rebuild chunk (1 to 1000 pairs).",
    "retrieval.mode": "Recall mode: lexical, vector, or hybrid.",
    "retrieval.relation_rerank_enabled": "Enable small relation-graph rerank bonuses after primary recall scoring.",
    "retrieval.vector_only_min_score": "Minimum score for vector-only candidates to survive recall filtering.",
    "vector.enabled": "Enable the rebuildable vector companion index.",
    "vector.backend": "Vector companion backend used for semantic recall.",
    "vector.startup_reconcile_page_size": "Maximum truth rows planned into durable vector outbox events by one startup or background maintenance tick.",
    "vector.startup_outbox_limit": "Maximum durable vector outbox events replayed in one startup or background maintenance phase.",
    "vector.write_outbox_replay_limit": "Maximum durable vector outbox events replayed after one committed memory write so transient backlog converges during normal traffic.",
    "vector.startup_reconcile_interval_seconds": "Minimum delay between completed vector reconciliation cycles; interrupted cycles resume immediately from their durable watermark.",
    "vector.embedder.api_key_env": "Environment variable names that may hold the embedding API key.",
    "vector.embedder.request_dimensions": "Send the configured output dimension to providers that support explicit dimensionality.",
    "vector.embedder.document_prefix": "Optional instruction prefix applied only when embedding indexed documents.",
    "vector.embedder.query_prefix": "Optional instruction prefix applied only when embedding retrieval queries.",
    "vector.embedder.prompt_profile": "Versioned identifier for the query/document instruction profile; changing it requires a new vector generation.",
    "vector.embedder.connection_retry_delays": "Optional bounded delays in seconds for retrying transport-level embedding connection failures (maximum 8 entries, each 0 to 300 seconds). HTTP/API errors are not retried by this schedule.",
    "experience.enabled": "Enable reusable Experience playbook surfaces.",
    "reflection.enabled": "Expose bounded citation-grounded reflection tooling.",
    "reflection.write_candidates": "Allow explicit maintenance-mode reflection calls to store hidden needs_review mental-model candidates.",
    "fact_evolution.enabled": "Enable structured Fact Evolution. This switch is high risk because an already configured apply mode can persist durable memory immediately; resident providers require reload.",
    "fact_evolution.mode": "Fallback Fact Evolution mode. preview is medium risk; auto_apply/reviewed_apply persist durable memory and are high risk. Resident providers require reload.",
    "fact_evolution.nightly_mode": "Nightly lane mode loaded by each scheduled invocation. preview is medium risk; auto_apply is high risk and may persist durable memory.",
    "fact_evolution.journal_mode": "Journal lane mode loaded by each scheduled invocation. preview is medium risk; auto_apply is high risk and may persist durable memory.",
    "fact_evolution.tool_mode": "Resident public tool-lane mode. Caller evidence remains non-authoritative until a runtime-owned evidence registry is available; provider reload is required.",
    "fact_evolution.maintenance_mode": "Explicit maintenance-lane mode. reviewed_apply permits maintenance-gated operator corrections and is high risk; provider reload is required.",
    "forgetting.hard_delete_sensitive": "Allow sensitive-data cleanup paths to hard-delete when explicitly invoked.",
}

_HIGH_RISK_PREFIXES = (
    "vector.embedder.api_key_env",
    "capture_llm.api_key_env",
    "reflection.api_key_env",
    "reflection.write_candidates",
    "forgetting.hard_delete_sensitive",
    "secret_index_tools_enabled",
    "memory_isolated_chat_ids",
)
_MEDIUM_RISK_PREFIXES = (
    "journal.",
    "retrieval.",
    "vector.",
    "experience.",
    "reflection.",
    "forgetting.",
    "shared_pool.",
    "identity.",
    "relation_",
)
_RESTART_PREFIXES = (
    "vector.",
    "journal.",
    "tool_schema_",
    "maintenance_tools_enabled",
    "secret_index_tools_enabled",
    "memory_isolated_chat_ids",
    "experience.",
    "reflection.",
    "relation_",
)
_FACT_EVOLUTION_RISKS = {
    "fact_evolution.enabled": "high",
    "fact_evolution.mode": "high",
    "fact_evolution.nightly_mode": "high",
    "fact_evolution.journal_mode": "high",
    "fact_evolution.tool_mode": "high",
    "fact_evolution.maintenance_mode": "high",
}
_RESTART_OVERRIDES = {
    "fact_evolution.enabled": True,
    "fact_evolution.mode": True,
    "fact_evolution.nightly_mode": False,
    "fact_evolution.journal_mode": False,
    "fact_evolution.tool_mode": True,
    "fact_evolution.maintenance_mode": True,
}
_CHOICES = {
    "tool_schema_profile": ["compact", "standard"],
    "retrieval.mode": ["lexical", "vector", "hybrid"],
    "retrieval.include_general": ["never", "same-scope", "always"],
    "retrieval.metric": ["cosine", "dot", "l2"],
    "retrieval.fusion_strategy": ["rrf", "weighted"],
    "retrieval.relation_contradiction_mode": ["surface", "suppress", "penalize"],
    "vector.backend": ["lancedb", "sqlite-bruteforce", "pgvector"],
    "vector.fallback_backend": ["sqlite-bruteforce", "disabled"],
    "vector.embedder.provider": ["openai-compatible", "openai", "sentence-transformers", "local-hash"],
    "vector.sync_mode": ["incremental", "rebuild"],
    "journal.extractor": ["llm", "heuristic"],
    "reflection.api_mode": [
        "chat_completions",
        "codex_responses",
        "anthropic_messages",
    ],
    "curated_memory.mode": ["single-user", "explicit-users", "profile-global", "disabled"],
    "fact_evolution.mode": ["preview", "auto_apply", "reviewed_apply"],
    "fact_evolution.nightly_mode": ["preview", "auto_apply"],
    "fact_evolution.journal_mode": ["preview", "auto_apply"],
    "fact_evolution.tool_mode": ["preview", "auto_apply", "reviewed_apply"],
    "fact_evolution.maintenance_mode": ["preview", "reviewed_apply"],
}
_CHOICE_RISKS = {
    "fact_evolution.mode": {
        "preview": "medium",
        "auto_apply": "high",
        "reviewed_apply": "high",
    },
    "fact_evolution.nightly_mode": {
        "preview": "medium",
        "auto_apply": "high",
    },
    "fact_evolution.journal_mode": {
        "preview": "medium",
        "auto_apply": "high",
    },
    "fact_evolution.tool_mode": {
        "preview": "medium",
        "auto_apply": "high",
        "reviewed_apply": "high",
    },
    "fact_evolution.maintenance_mode": {
        "preview": "medium",
        "reviewed_apply": "high",
    },
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
    if key in _FACT_EVOLUTION_RISKS:
        return _FACT_EVOLUTION_RISKS[key]
    if any(key == prefix or key.startswith(f"{prefix}.") for prefix in _HIGH_RISK_PREFIXES):
        return "high"
    if any(key == prefix.rstrip(".") or key.startswith(prefix) for prefix in _MEDIUM_RISK_PREFIXES):
        return "medium"
    return "low"


def _restart_required(key: str) -> bool:
    if key in _RESTART_OVERRIDES:
        return _RESTART_OVERRIDES[key]
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
    if prefix in _CHOICE_RISKS:
        row["choice_risks"] = dict(_CHOICE_RISKS[prefix])
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
        choice_risks = entry.get("choice_risks")
        choice_risks_text = (
            "; choice_risks: `"
            + ", ".join(
                f"{choice}={risk}"
                for choice, risk in choice_risks.items()
            )
            + "`"
            if isinstance(choice_risks, dict) and choice_risks
            else ""
        )
        restart = "yes" if entry.get("restart_required") else "no"
        lines.append(f"- `{key}` ({entry.get('type')}; risk: `{entry.get('risk')}`; restart_required: `{restart}`{choices_text}{choice_risks_text}) — {entry.get('description')} Default: `{default}`")
    lines.append("")
    return "\n".join(lines)
