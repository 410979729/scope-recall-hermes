from __future__ import annotations

from pathlib import Path
from typing import Any
import json

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
        "enabled": True,
        "digest_on_session_end": False,
        "background_digest_enabled": True,
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
        "tool_trace_skip_names": ["todo", "skill_view", "skills_list"],
        "tool_trace_hard_max_chars": 4000,
        "tool_trace_max_chars": 1800,
    },
    "per_turn_extraction": {
        "enabled": False,
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
    "maintenance_tools_enabled": False,
    "experience": {
        "enabled": True,
        "prefetch_enabled": False,
        "min_query_chars": 8,
        "direct_reuse_min_confidence": 0.82,
        "allow_risky_direct_reuse": False,
        "packet_max_chars": 1400,
        "auto_promotion_enabled": False,
        "auto_promote_low_risk": True,
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
    "retrieval": {
        "mode": "hybrid",
        "lexical_weight": 0.45,
        "vector_weight": 0.55,
        "candidate_pool": 12,
        "min_score": 0.18,
        "vector_min_score": 0.12,
        "vector_only_min_score": 0.68,
        "include_general": "same-scope",
        "general_weight": 0.35,
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
    },
    "vector": {
        "enabled": True,
        "backend": "lancedb",
        "table_name": "memories",
        "top_k": 8,
        "sync_mode": "incremental",
        "index_general": False,
        "embedder": {
            "provider": "openai-compatible",
            "dimensions": 3072,
            "model": "gemini-embedding-001",
            "api_key_env": ["SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY"],
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        },
        "fallback_embedder": {
            "provider": "local-hash",
            "dimensions": 256,
            "model": "hash-v1",
        },
    },
}


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



def load_runtime_config(plugin_dir: Path, storage_dir: Path) -> dict[str, Any]:
    config: dict[str, Any] = json.loads(json.dumps(DEFAULT_CONFIG))
    for path in (plugin_dir / "config.json", storage_dir / "config.json"):
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(raw, dict):
            config = _deep_merge(config, raw)
    return config



def save_runtime_config(values: dict[str, Any], hermes_home: str) -> None:
    path = Path(hermes_home) / "scope-recall" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_runtime_config(Path(__file__).resolve().parent, path.parent)
    merged = _deep_merge(existing, _expand_dotted_keys(values or {}))
    path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
