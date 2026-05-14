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
    "capture_assistant": True,
    "query_char_limit": 1000,
    "min_capture_length": 10,
    "enable_tools": True,
    "retrieval": {
        "mode": "hybrid",
        "lexical_weight": 0.45,
        "vector_weight": 0.55,
        "candidate_pool": 12,
        "min_score": 0.18,
        "vector_min_score": 0.12,
        "metric": "cosine",
    },
    "vector": {
        "enabled": True,
        "backend": "lancedb",
        "table_name": "memories",
        "top_k": 8,
        "sync_mode": "incremental",
        "embedder": {
            "provider": "openai-compatible",
            "dimensions": 3072,
            "model": "gemini-embedding-001",
            "api_key_env": ["OPENAI_API_KEY", "GOOGLE_API_KEY"],
            "base_url_env": ["GEMINI_BASE_URL", "OPENAI_BASE_URL"],
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
