"""Read-only endpoint-policy health checks for configured outbound providers.

The doctor reuses final transport policy and emits only sanitized endpoint
metadata. It never resolves API keys or performs network I/O.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

try:
    from .embedders import resolve_embedder_base_url
    from .http_utils import (
        UnsafeEndpointError,
        chat_completions_endpoint,
        explicit_insecure_endpoint_opt_in,
        redact_sensitive,
        require_safe_endpoint,
        safe_endpoint_display,
    )
    from .nightly_llm import (
        anthropic_messages_endpoint,
        normalize_digest_api_mode,
        resolve_llm_transport_config,
        responses_endpoint,
    )
except ImportError:  # pragma: no cover - standalone doctor source checkout
    from embedders import resolve_embedder_base_url
    from http_utils import (
        UnsafeEndpointError,
        chat_completions_endpoint,
        explicit_insecure_endpoint_opt_in,
        redact_sensitive,
        require_safe_endpoint,
        safe_endpoint_display,
    )
    from nightly_llm import (
        anthropic_messages_endpoint,
        normalize_digest_api_mode,
        resolve_llm_transport_config,
        responses_endpoint,
    )

_RECOMMENDATION = (
    "Fix unsafe Scope Recall provider endpoints before capture, digest, "
    "reflection, or embedding runs."
)
def _feature_enabled(value: object) -> bool:
    """Match compatible feature-flag parsing without weakening endpoint opt-in."""

    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _failure(surface: str, display_url: str, error: Exception) -> dict[str, Any]:
    return {
        "surface": surface,
        "enabled": True,
        "ok": False,
        "endpoint": safe_endpoint_display(display_url, public_path_only=True),
        "error": redact_sensitive(error),
    }


def _success(surface: str, endpoint: str) -> dict[str, Any]:
    return {
        "surface": surface,
        "enabled": True,
        "ok": True,
        "endpoint": safe_endpoint_display(endpoint, public_path_only=True),
        "error": "",
    }


def _chat_surface(surface: str, config: Mapping[str, Any]) -> dict[str, Any]:
    configured_endpoint = str(config.get("endpoint") or config.get("chat_endpoint") or "")
    configured_base_url = str(config.get("base_url") or "https://api.openai.com")
    display_url = configured_endpoint or configured_base_url
    try:
        endpoint = chat_completions_endpoint(
            configured_base_url,
            endpoint=configured_endpoint,
            append_v1=_feature_enabled(config.get("append_v1", True)),
            allow_insecure_endpoint=explicit_insecure_endpoint_opt_in(
                config.get("allow_insecure_endpoint", False)
            ),
        )
    except UnsafeEndpointError as exc:
        return _failure(surface, display_url, exc)
    return _success(surface, endpoint)


def _llm_surface(
    surface: str,
    config: Mapping[str, Any],
    *,
    hermes_home: Path | None = None,
) -> dict[str, Any]:
    """Validate the endpoint shape selected by the configured LLM API mode."""

    if hermes_home is not None:
        config = resolve_llm_transport_config(
            hermes_home,
            SimpleNamespace(
                provider=str(config.get("provider") or config.get("llm_provider") or ""),
                model=str(config.get("model") or ""),
                base_url=str(config.get("base_url") or ""),
                endpoint=str(config.get("endpoint") or config.get("chat_endpoint") or ""),
                append_v1=config.get("append_v1") if "append_v1" in config else None,
                allow_insecure_endpoint=(
                    config.get("allow_insecure_endpoint")
                    if "allow_insecure_endpoint" in config
                    else None
                ),
                api_mode=str(config.get("api_mode") or ""),
            ),
        )
    configured_endpoint = str(config.get("endpoint") or config.get("chat_endpoint") or "")
    configured_base_url = str(config.get("base_url") or "https://api.openai.com")
    provider = str(config.get("provider") or config.get("llm_provider") or "")
    api_mode = normalize_digest_api_mode(
        config.get("api_mode"),
        provider=provider,
        base_url=configured_base_url,
    )
    allow_insecure = explicit_insecure_endpoint_opt_in(
        config.get("allow_insecure_endpoint", False)
    )
    display_url = configured_endpoint or configured_base_url
    try:
        if api_mode == "codex_responses":
            display_url = configured_base_url
            endpoint = responses_endpoint(
                configured_base_url,
                allow_insecure_endpoint=allow_insecure,
            )
        elif api_mode == "anthropic_messages":
            endpoint = anthropic_messages_endpoint(
                configured_base_url,
                endpoint=configured_endpoint,
                allow_insecure_endpoint=allow_insecure,
            )
        else:
            endpoint = chat_completions_endpoint(
                configured_base_url,
                endpoint=configured_endpoint,
                append_v1=_feature_enabled(config.get("append_v1", True)),
                allow_insecure_endpoint=allow_insecure,
            )
    except UnsafeEndpointError as exc:
        return _failure(surface, display_url, exc)
    return _success(surface, endpoint)


def _embedder_surface(
    surface: str,
    config: Mapping[str, Any],
) -> dict[str, Any] | None:
    configured_url = resolve_embedder_base_url(config)
    if configured_url is None:
        return None
    try:
        endpoint = require_safe_endpoint(
            configured_url,
            allow_insecure=explicit_insecure_endpoint_opt_in(
                config.get("allow_insecure_endpoint", False)
            ),
        ).url
    except UnsafeEndpointError as exc:
        return _failure(surface, configured_url, exc)
    return _success(surface, endpoint)


def endpoint_policy_report(
    runtime_config: Mapping[str, Any],
    *,
    hermes_home: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Validate enabled outbound endpoints without credentials or network I/O."""

    surfaces: list[dict[str, Any]] = []

    raw_capture = runtime_config.get("capture_llm")
    capture = raw_capture if isinstance(raw_capture, Mapping) else {}
    if _feature_enabled(capture.get("enabled", False)):
        surfaces.append(_chat_surface("capture_llm", capture))

    raw_journal = runtime_config.get("journal")
    journal = raw_journal if isinstance(raw_journal, Mapping) else {}
    if (
        isinstance(raw_journal, Mapping)
        and _feature_enabled(journal.get("enabled", True))
        and str(journal.get("extractor") or "llm").strip().casefold() == "llm"
    ):
        surfaces.append(_llm_surface("journal", journal, hermes_home=hermes_home))

    raw_reflection = runtime_config.get("reflection")
    reflection = raw_reflection if isinstance(raw_reflection, Mapping) else {}
    if (
        _feature_enabled(reflection.get("enabled", False))
        and str(reflection.get("provider") or "").strip()
        and str(reflection.get("model") or "").strip()
    ):
        surfaces.append(_llm_surface("reflection", reflection, hermes_home=hermes_home))

    raw_vector = runtime_config.get("vector")
    vector = raw_vector if isinstance(raw_vector, Mapping) else {}
    if _feature_enabled(vector.get("enabled", False)):
        for config_key, surface_name in (
            ("embedder", "vector.embedder"),
            ("fallback_embedder", "vector.fallback_embedder"),
        ):
            raw_embedder = vector.get(config_key)
            embedder = raw_embedder if isinstance(raw_embedder, Mapping) else {}
            result = _embedder_surface(surface_name, embedder)
            if result is not None:
                surfaces.append(result)

    invalid = sum(1 for surface in surfaces if not surface["ok"])
    payload = {"surfaces": surfaces}
    check = {"ok": invalid == 0, "checked": len(surfaces), "invalid": invalid}
    recommendations = [_RECOMMENDATION] if invalid else []
    return payload, check, recommendations


__all__ = ["endpoint_policy_report"]
