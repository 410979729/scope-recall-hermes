"""Provider resolution and LLM prompt helpers for nightly digest runs.

This module handles provider-specific request details while returning sanitized, bounded digest candidates."""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .http_utils import (
    UnsafeEndpointError,
    chat_completions_endpoint,
    explicit_insecure_endpoint_opt_in,
    redact_sensitive,
    require_safe_endpoint,
    safe_endpoint_display,
    safe_urlopen,
)


class NightlyDigestLLMError(RuntimeError):
    """Retry failure that preserves classification for downstream fallback policy."""

    def __init__(
        self,
        message: str,
        *,
        attempts: int,
        error_kind: str,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.error_kind = error_kind
        self.retryable = retryable


def config_bool_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def normalize_digest_api_mode(value: Any, *, provider: str = "", base_url: str = "") -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "": "",
        "openai": "chat_completions",
        "openai_compatible": "chat_completions",
        "chat": "chat_completions",
        "chat_completion": "chat_completions",
        "chat_completions": "chat_completions",
        "codex": "codex_responses",
        "codex_responses": "codex_responses",
        "responses": "codex_responses",
        "openai_responses": "codex_responses",
        "anthropic": "anthropic_messages",
        "anthropic_messages": "anthropic_messages",
        "messages": "anthropic_messages",
    }
    normalized = aliases.get(raw, raw)
    if normalized:
        return normalized
    provider_l = str(provider or "").strip().lower()
    base_l = str(base_url or "").strip().lower()
    if provider_l == "openai-codex" or ("chatgpt.com" in base_l and "/backend-api/codex" in base_l):
        return "codex_responses"
    return "chat_completions"


def load_dotenv(path: Path) -> dict[str, str]:
    output: dict[str, str] = {}
    if not path.exists():
        return output
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        value = value.strip().strip("'\"")
        output[key.strip()] = value
    return output


def resolve_api_key(raw_value: Any, provider: str, env: dict[str, str]) -> str:
    candidates = ["SCOPE_RECALL_DIGEST_API_KEY"]
    raw = str(raw_value or "").strip()
    env_match = re.fullmatch(r"\$\{([^}]+)\}", raw)
    if env_match:
        candidates.append(env_match.group(1))
    elif raw and re.fullmatch(r"[A-Z][A-Z0-9_]*", raw):
        candidates.append(raw)
    elif raw:
        return raw
    provider_l = str(provider or "").strip().lower()
    if provider:
        candidates.append(f"{provider.upper().replace('-', '_')}_API_KEY")
    # Generic fallback keys are only safe for matching OpenAI-compatible providers.
    # Do not use DEEPSEEK_API_KEY as an implicit credential for openai-codex: it
    # produces valid-looking configuration but sends the wrong bearer token to
    # chatgpt.com/backend-api/codex and turns every journal batch into auth dead letters.
    if provider_l in {"", "deepseek"}:
        candidates.append("DEEPSEEK_API_KEY")
    if provider_l in {"", "openai", "openai-compatible", "openai_compatible"}:
        candidates.append("OPENAI_API_KEY")
    for key in candidates:
        value = env.get(key)
        if value:
            return value
    return ""


def resolve_hermes_credential_pool_token(hermes_home: Path, provider: str) -> str:
    """Return the first OAuth access token for provider from Hermes auth.json.

    This keeps standalone digest scripts aligned with Hermes provider auth instead of
    guessing from unrelated API-key environment variables."""
    provider = str(provider or "").strip()
    if not provider:
        return ""
    auth_path = hermes_home / "auth.json"
    if not auth_path.exists():
        return ""
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    raw_pool = payload.get("credential_pool") if isinstance(payload, dict) else None
    pool = raw_pool.get(provider) if isinstance(raw_pool, dict) else None
    if not isinstance(pool, list):
        return ""
    for item in pool:
        if not isinstance(item, dict):
            continue
        token = str(item.get("access_token") or "").strip()
        if token:
            return token
    return ""


def _dict_child(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    value = mapping.get(key)
    return value if isinstance(value, dict) else {}


def _load_llm_config_layers(
    hermes_home: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Load credential-free Hermes model/provider/nightly routing layers."""

    config_path = hermes_home / "config.yaml"
    cfg: dict[str, Any] = {}
    if config_path.exists():
        try:
            import yaml  # type: ignore

            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            cfg = loaded if isinstance(loaded, dict) else {}
        except Exception:
            cfg = {}
    return (
        _dict_child(cfg, "model"),
        _dict_child(cfg, "providers"),
        _dict_child(cfg, "scope_recall_nightly_digest"),
    )


def _resolve_llm_transport_config_and_layers(
    hermes_home: Path,
    options: Any,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Resolve outbound LLM routing without reading or returning credentials."""

    model_cfg, providers_cfg, nightly_cfg = _load_llm_config_layers(hermes_home)
    provider = str(
        getattr(options, "provider", "")
        or nightly_cfg.get("provider")
        or model_cfg.get("provider")
        or ""
    ).strip()
    provider_cfg = _dict_child(providers_cfg, provider)
    model = getattr(options, "model", "") or str(
        nightly_cfg.get("model")
        or nightly_cfg.get("default_model")
        or provider_cfg.get("default_model")
        or model_cfg.get("model")
        or model_cfg.get("default")
        or model_cfg.get("default_model")
        or "gpt-4o-mini"
    )
    base_url = getattr(options, "base_url", "") or str(
        nightly_cfg.get("base_url")
        or provider_cfg.get("base_url")
        or model_cfg.get("base_url")
        or "https://api.openai.com"
    )
    endpoint = getattr(options, "endpoint", "") or str(
        nightly_cfg.get("endpoint")
        or nightly_cfg.get("chat_endpoint")
        or provider_cfg.get("endpoint")
        or provider_cfg.get("chat_endpoint")
        or model_cfg.get("endpoint")
        or ""
    )
    append_v1_raw = getattr(options, "append_v1", None)
    if append_v1_raw is None:
        append_v1_raw = nightly_cfg.get(
            "append_v1",
            provider_cfg.get("append_v1", model_cfg.get("append_v1", True)),
        )
    allow_insecure_raw = getattr(options, "allow_insecure_endpoint", None)
    if allow_insecure_raw is None:
        allow_insecure_raw = nightly_cfg.get(
            "allow_insecure_endpoint",
            provider_cfg.get(
                "allow_insecure_endpoint",
                model_cfg.get("allow_insecure_endpoint", False),
            ),
        )
    api_mode = normalize_digest_api_mode(
        getattr(options, "api_mode", "")
        or nightly_cfg.get("api_mode")
        or provider_cfg.get("api_mode")
        or model_cfg.get("api_mode"),
        provider=provider,
        base_url=str(base_url or ""),
    )
    transport = {
        "provider": provider,
        "model": str(model or "gpt-4o-mini"),
        "base_url": str(base_url or "https://api.openai.com").rstrip("/"),
        "endpoint": str(endpoint or "").rstrip("/"),
        "append_v1": config_bool_value(append_v1_raw, True),
        "allow_insecure_endpoint": explicit_insecure_endpoint_opt_in(
            allow_insecure_raw
        ),
        "api_mode": api_mode,
    }
    return transport, model_cfg, provider_cfg, nightly_cfg


def resolve_llm_transport_config(
    hermes_home: Path,
    options: Any,
) -> dict[str, Any]:
    """Return the runtime LLM route without reading API keys or auth state."""

    transport, _model_cfg, _provider_cfg, _nightly_cfg = (
        _resolve_llm_transport_config_and_layers(hermes_home, options)
    )
    return transport


def resolve_llm_config(hermes_home: Path, options: Any) -> dict[str, Any]:
    """Resolve runtime LLM routing and credentials for digest/reflection calls."""

    transport, model_cfg, provider_cfg, nightly_cfg = (
        _resolve_llm_transport_config_and_layers(hermes_home, options)
    )
    provider = str(transport.get("provider") or "")
    env = load_dotenv(hermes_home / ".env")
    env.update(os.environ)
    raw_api_key_source = (
        getattr(options, "api_key_env", "")
        or nightly_cfg.get("api_key")
        or nightly_cfg.get("api_key_env")
        or nightly_cfg.get("key_env")
        or provider_cfg.get("api_key")
        or provider_cfg.get("api_key_env")
        or provider_cfg.get("key_env")
        or model_cfg.get("api_key")
    )
    explicit_api_key = str(getattr(options, "api_key", "") or "").strip()
    if explicit_api_key:
        api_key = explicit_api_key
    elif raw_api_key_source:
        api_key = resolve_api_key(raw_api_key_source, provider, env)
    elif provider.strip().lower() == "openai-codex":
        api_key = resolve_hermes_credential_pool_token(hermes_home, provider)
    else:
        api_key = resolve_api_key("", provider, env)
    return {**transport, "api_key": api_key}


def codex_cloudflare_headers(access_token: str) -> dict[str, str]:
    headers = {
        "User-Agent": "codex_cli_rs/0.0.0 (Scope Recall)",
        "originator": "codex_cli_rs",
    }
    if not isinstance(access_token, str) or not access_token.strip():
        return headers
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return headers
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        acct_id = claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
        if isinstance(acct_id, str) and acct_id:
            headers["ChatGPT-Account-ID"] = acct_id
    except Exception:
        pass
    return headers


def responses_endpoint(
    base_url: str,
    *,
    allow_insecure_endpoint: bool = False,
) -> str:
    endpoint = str(base_url or "").strip().rstrip("/")
    if not endpoint:
        endpoint = "https://api.openai.com/v1"
    if not endpoint.endswith("/responses"):
        endpoint += "/responses"
    return require_safe_endpoint(
        endpoint,
        allow_insecure=allow_insecure_endpoint,
    ).url


def anthropic_messages_endpoint(
    base_url: str,
    *,
    endpoint: str = "",
    allow_insecure_endpoint: bool = False,
) -> str:
    explicit = str(endpoint or "").strip().rstrip("/")
    if explicit:
        candidate = explicit
    else:
        base = str(base_url or "").strip().rstrip("/")
        if not base:
            base = "https://api.anthropic.com"
        if base.endswith("/v1/messages"):
            candidate = base
        elif base.endswith("/v1"):
            candidate = base + "/messages"
        else:
            candidate = base + "/v1/messages"
    return require_safe_endpoint(
        candidate,
        allow_insecure=allow_insecure_endpoint,
    ).url


def response_item_get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    value = getattr(item, key, default)
    return value if value is not None else default


def extract_responses_text(data: dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text
    parts: list[str] = []
    for item in data.get("output") or []:
        if response_item_get(item, "type") != "message":
            continue
        for content_part in response_item_get(item, "content", []) or []:
            part_type = response_item_get(content_part, "type")
            if part_type in {"output_text", "text"}:
                text = response_item_get(content_part, "text", "")
                if text:
                    parts.append(str(text))
    if parts:
        return "".join(parts)
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return str(message.get("content") or "")


def extract_responses_sse_text(body: str) -> str:
    delta_parts: list[str] = []
    item_parts: list[str] = []
    completed_payload: dict[str, Any] | None = None
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        event_type = str(event.get("type") or "")
        if event_type == "error":
            message = event.get("message") or event.get("error") or raw
            raise RuntimeError(f"LLM stream error: {redact_sensitive(str(message))}")
        if "output_text.delta" in event_type:
            delta = event.get("delta")
            if isinstance(delta, str):
                delta_parts.append(delta)
            continue
        if event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict):
                text = extract_responses_text({"output": [item]})
                if text:
                    item_parts.append(text)
            continue
        if event_type in {"response.completed", "response.incomplete", "response.failed"}:
            response = event.get("response")
            if isinstance(response, dict):
                completed_payload = response
            if event_type == "response.failed":
                failure_payload = event.get("response") or raw
                raise RuntimeError(f"LLM stream failed: {redact_sensitive(str(failure_payload))}")
    if delta_parts:
        return "".join(delta_parts)
    if item_parts:
        return "".join(item_parts)
    if completed_payload:
        return extract_responses_text(completed_payload)
    return ""


def decode_responses_body(body: str) -> str:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return extract_responses_sse_text(body)
    if not isinstance(data, dict):
        return ""
    return extract_responses_text(data)


def call_chat_completions_llm(
    prompt: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    timeout: float,
    endpoint: str = "",
    append_v1: bool = True,
    allow_insecure_endpoint: bool = False,
    system_prompt: str = "You extract durable memory as strict JSON.",
) -> str:
    endpoint_url = chat_completions_endpoint(
        base_url,
        endpoint=endpoint,
        append_v1=append_v1,
        allow_insecure_endpoint=allow_insecure_endpoint,
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        # Some reasoning-heavy providers can spend a large part of the
        # completion budget on internal thinking tokens before emitting strict
        # JSON. A 1.8k cap caused truncated arrays and parse dead-letters
        # during digest repair.
        "max_tokens": 4096,
    }
    request = urllib.request.Request(
        endpoint_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with safe_urlopen(
            request,
            timeout=timeout,
            allow_insecure=allow_insecure_endpoint,
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = redact_sensitive(exc.read().decode("utf-8", errors="replace")[:500])
        raise RuntimeError(
            f"LLM HTTP {exc.code} at {safe_endpoint_display(endpoint_url)}: {body}"
        ) from exc
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return str(message.get("content") or "")


def call_codex_responses_llm(
    prompt: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    timeout: float,
    allow_insecure_endpoint: bool = False,
    system_prompt: str = "You extract durable memory as strict JSON.",
) -> str:
    payload = {
        "model": model,
        "instructions": system_prompt,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        ],
        "store": False,
        "stream": True,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        **codex_cloudflare_headers(api_key),
    }
    endpoint_url = responses_endpoint(
        base_url,
        allow_insecure_endpoint=allow_insecure_endpoint,
    )
    request = urllib.request.Request(
        endpoint_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with safe_urlopen(
            request,
            timeout=timeout,
            allow_insecure=allow_insecure_endpoint,
        ) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = redact_sensitive(exc.read().decode("utf-8", errors="replace")[:500])
        raise RuntimeError(
            f"LLM HTTP {exc.code} at {safe_endpoint_display(endpoint_url)}: {body}"
        ) from exc
    return decode_responses_body(body)


def extract_anthropic_messages_text(data: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in data.get("content") or []:
        if isinstance(item, dict):
            text = item.get("text")
            if text:
                parts.append(str(text))
        elif isinstance(item, str):
            parts.append(item)
    if parts:
        return "".join(parts)
    return str(data.get("text") or "")


def call_anthropic_messages_llm(
    prompt: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    timeout: float,
    endpoint: str = "",
    allow_insecure_endpoint: bool = False,
    system_prompt: str = "You extract durable memory as strict JSON.",
) -> str:
    endpoint_url = anthropic_messages_endpoint(
        base_url,
        endpoint=endpoint,
        allow_insecure_endpoint=allow_insecure_endpoint,
    )
    payload = {
        "model": model,
        "system": system_prompt,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 4096,
    }
    request = urllib.request.Request(
        endpoint_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with safe_urlopen(
            request,
            timeout=timeout,
            allow_insecure=allow_insecure_endpoint,
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = redact_sensitive(exc.read().decode("utf-8", errors="replace")[:500])
        raise RuntimeError(
            f"LLM HTTP {exc.code} at {safe_endpoint_display(endpoint_url)}: {body}"
        ) from exc
    return extract_anthropic_messages_text(data)


def call_llm(
    prompt: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    timeout: float,
    api_mode: str = "chat_completions",
    endpoint: str = "",
    append_v1: bool = True,
    allow_insecure_endpoint: bool = False,
    system_prompt: str = "You extract durable memory as strict JSON.",
) -> str:
    if not api_key:
        raise RuntimeError("API key not found for nightly digest")
    mode = normalize_digest_api_mode(api_mode, provider="", base_url=base_url)
    if mode == "codex_responses":
        return call_codex_responses_llm(
            prompt,
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            allow_insecure_endpoint=allow_insecure_endpoint,
            system_prompt=system_prompt,
        )
    if mode == "anthropic_messages":
        return call_anthropic_messages_llm(
            prompt,
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            endpoint=endpoint,
            allow_insecure_endpoint=allow_insecure_endpoint,
            system_prompt=system_prompt,
        )
    if mode != "chat_completions":
        raise RuntimeError(f"Unsupported digest api_mode: {api_mode}")
    return call_chat_completions_llm(
        prompt,
        model=model,
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        endpoint=endpoint,
        append_v1=append_v1,
        allow_insecure_endpoint=allow_insecure_endpoint,
        system_prompt=system_prompt,
    )


def classify_llm_error(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, NightlyDigestLLMError):
        return exc.error_kind, exc.retryable
    if isinstance(exc, UnsafeEndpointError):
        return "endpoint_policy", False
    message = str(exc or "").lower()
    if isinstance(exc, TimeoutError) or "timeout" in message or "timed out" in message:
        return "timeout", True
    if "429" in message or "rate limit" in message or "too many requests" in message:
        return "rate_limit", True
    if any(token in message for token in ("500", "502", "503", "504", "server error", "bad gateway", "service unavailable", "gateway timeout")):
        return "server", True
    if any(token in message for token in ("connection", "network", "temporarily", "reset by peer", "remote end closed")):
        return "network", True
    if any(token in message for token in ("401", "403", "unauthorized", "forbidden", "invalid api key", "permission")):
        return "auth", False
    if any(token in message for token in ("402", "quota", "billing", "insufficient_quota")):
        return "quota", False
    if any(token in message for token in ("json", "parse", "decode")):
        return "parse", False
    return "unknown", True


def call_llm_with_retries(
    prompt: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    timeout: float,
    api_mode: str,
    endpoint: str = "",
    append_v1: bool = True,
    allow_insecure_endpoint: bool = False,
    max_attempts: int = 1,
    retry_delay: float = 0.0,
    system_prompt: str = "You extract durable memory as strict JSON.",
) -> str:
    last_error: Exception | None = None
    last_kind = "unknown"
    last_retryable = True
    last_attempt = 0
    attempts = max(1, int(max_attempts or 1))
    for attempt in range(1, attempts + 1):
        last_attempt = attempt
        try:
            return call_llm(
                prompt,
                model=model,
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
                api_mode=api_mode,
                endpoint=endpoint,
                append_v1=append_v1,
                allow_insecure_endpoint=allow_insecure_endpoint,
                system_prompt=system_prompt,
            )
        except Exception as exc:
            last_error = exc
            last_kind, last_retryable = classify_llm_error(exc)
            if (not last_retryable) or attempt >= attempts:
                break
            if retry_delay > 0:
                time.sleep(max(0.0, float(retry_delay)))
    assert last_error is not None
    raise NightlyDigestLLMError(
        f"{last_kind} after {last_attempt} attempt(s): {type(last_error).__name__}: {redact_sensitive(str(last_error)[:400])}",
        attempts=last_attempt,
        error_kind=last_kind,
        retryable=last_retryable,
    ) from last_error


__all__ = [
    "anthropic_messages_endpoint",
    "call_anthropic_messages_llm",
    "call_chat_completions_llm",
    "call_codex_responses_llm",
    "call_llm",
    "call_llm_with_retries",
    "classify_llm_error",
    "config_bool_value",
    "decode_responses_body",
    "extract_responses_sse_text",
    "extract_responses_text",
    "load_dotenv",
    "NightlyDigestLLMError",
    "normalize_digest_api_mode",
    "resolve_api_key",
    "resolve_llm_config",
    "resolve_llm_transport_config",
    "responses_endpoint",
]
