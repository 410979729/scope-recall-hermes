from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

try:
    from .capture_filters import contains_secret_like_text as _contains_secret_like_text
    from .capture_filters import redact_secret_like_text as _redact_secret_like_text
    from .capture_filters import sanitize_report_text as _sanitize_report_text
except ImportError:  # pragma: no cover - keeps the standalone doctor script usable from source checkouts
    def _contains_secret_like_text(text: Any) -> bool:
        value = "" if text is None else str(text)
        return bool(
            re.search(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", value)
            or re.search(
                r"(?:api[_ \t-]?key|token|secret|password|passwd|credential(?:[_ \t-]?[a-z0-9_]+)?|private[_ \t-]?key)"
                r"(?:[ \t]*(?::|=|是)[ \t]*|[ \t]+is[ \t]+)[^\s]+",
                value,
                flags=re.IGNORECASE,
            )
            or re.search(r"s" r"k-[A-Za-z0-9][A-Za-z0-9_-]{18,}", value)
            or re.search(r"g" r"h[pousr]_[A-Za-z0-9_]{20,}", value)
            or re.search(r"bea" r"rer\s+[A-Za-z0-9._\-~+/=]{16,}", value, flags=re.IGNORECASE)
        )

    def _redact_secret_like_text(text: Any) -> str:
        value = "" if text is None else str(text)
        value = re.sub(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            "[REDACTED_SECRET]",
            value,
        )
        value = re.sub(
            r"(?:api[_ \t-]?key|token|secret|password|passwd|credential(?:[_ \t-]?[a-z0-9_]+)?|private[_ \t-]?key)"
            r"(?:[ \t]*(?::|=|是)[ \t]*|[ \t]+is[ \t]+)[^\s]+",
            "[REDACTED_SECRET]",
            value,
            flags=re.IGNORECASE,
        )
        value = re.sub(r"s" r"k-[A-Za-z0-9][A-Za-z0-9_-]{18,}", "[REDACTED_SECRET]", value)
        value = re.sub(r"g" r"h[pousr]_[A-Za-z0-9_]{20,}", "[REDACTED_SECRET]", value)
        value = re.sub(r"bea" r"rer\s+[A-Za-z0-9._\-~+/=]{16,}", "[REDACTED_SECRET]", value, flags=re.IGNORECASE)
        return value

    def _sanitize_report_text(text: Any) -> str:
        return _redact_secret_like_text(text)


def contains_secret_like_text(text: Any) -> bool:
    return _contains_secret_like_text(text)


def redact_secret_like_text(text: Any) -> str:
    return _redact_secret_like_text(text)


def sanitize_report_text(text: Any) -> str:
    return _sanitize_report_text(text)

def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def plugin_yaml_version(text: str) -> str:
    match = re.search(r"^version:\s*([^\s#]+)", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_profile_dotenv(hermes_home: Path) -> set[str]:
    env_path = hermes_home / ".env"
    loaded: set[str] = set()
    if not env_path.exists():
        return loaded
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if not key or not value:
            continue
        loaded.add(key)
    return loaded


def load_runtime_config(source_root: Path, hermes_home: Path) -> dict[str, Any]:
    profile_env_keys = load_profile_dotenv(hermes_home)
    config: dict[str, Any] = {}
    for path in (source_root / "config.json", hermes_home / "scope-recall" / "config.json"):
        if not path.exists():
            continue
        try:
            raw = json.loads(read_text(path))
        except Exception:
            continue
        if isinstance(raw, dict):
            config = deep_merge(config, raw)
    if profile_env_keys:
        config["_profile_env_keys"] = sorted(profile_env_keys)
    return config


def coerce_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def embedder_config_available(config: dict[str, Any], *, profile_env_keys: set[str] | None = None) -> bool:
    profile_env_keys = profile_env_keys or set()
    provider = str(config.get("provider") or "local-hash").strip().lower()
    if provider in {"local-hash", "local-debug"}:
        return True
    if provider in {"openai-compatible", "openai"}:
        if coerce_list(config.get("api_key")):
            return True
        return any(
            os.getenv(name, "").strip() or name in profile_env_keys
            for name in coerce_list(config.get("api_key_env") or "OPENAI_API_KEY")
        )
    if provider in {"sentence-transformers", "local-model", "local-embedding", "huggingface"}:
        try:
            import importlib.util

            return importlib.util.find_spec("sentence_transformers") is not None
        except Exception:
            return False
    return True


def expected_embedder_from_config(config: dict[str, Any]) -> dict[str, Any]:
    raw_vector = config.get("vector")
    vector_config: dict[str, Any] = raw_vector if isinstance(raw_vector, dict) else {}
    if vector_config.get("enabled") is False:
        return {}
    raw_primary = vector_config.get("embedder")
    raw_fallback = vector_config.get("fallback_embedder")
    primary: dict[str, Any] = raw_primary if isinstance(raw_primary, dict) else {}
    fallback: dict[str, Any] = raw_fallback if isinstance(raw_fallback, dict) else {}
    profile_env_keys = set(coerce_list(config.get("_profile_env_keys")))
    source = "embedder"
    selected: dict[str, Any] = dict(primary)
    if selected and not embedder_config_available(selected, profile_env_keys=profile_env_keys) and fallback and embedder_config_available(fallback, profile_env_keys=profile_env_keys):
        selected = dict(fallback)
        source = "fallback_embedder"
    if not selected:
        return {}
    return {
        "source": source,
        "provider": str(selected.get("provider") or ""),
        "model": str(selected.get("model") or ""),
        "dimensions": int(selected.get("dimensions") or 0),
    }


def vector_enabled_from_config(config: dict[str, Any]) -> bool:
    raw_vector = config.get("vector")
    vector_config: dict[str, Any] = raw_vector if isinstance(raw_vector, dict) else {}
    return vector_config.get("enabled") is not False


def vector_backend_from_config(config: dict[str, Any]) -> str:
    raw_vector = config.get("vector")
    vector_config: dict[str, Any] = raw_vector if isinstance(raw_vector, dict) else {}
    backend = str(vector_config.get("backend") or "lancedb").strip().lower()
    return "sqlite-bruteforce" if backend == "sqlite" else backend


def _lifecycle_visible_clause(alias: str = "m") -> str:
    lifecycle_expr = f"LOWER(COALESCE(CASE WHEN json_valid({alias}.metadata) THEN json_extract({alias}.metadata, '$.lifecycle') ELSE '' END, ''))"
    return f"{lifecycle_expr} NOT IN ('archived','superseded','obsolete','rejected')"
