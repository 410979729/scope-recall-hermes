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

from .http_utils import chat_completions_endpoint, redact_sensitive


def config_bool_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


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
    if provider:
        candidates.append(f"{provider.upper().replace('-', '_')}_API_KEY")
    candidates.extend(["DEEPSEEK_API_KEY", "OPENAI_API_KEY"])
    for key in candidates:
        value = env.get(key)
        if value:
            return value
    return ""


def _dict_child(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    value = mapping.get(key)
    return value if isinstance(value, dict) else {}


def resolve_llm_config(hermes_home: Path, options: Any) -> dict[str, Any]:
    config_path = hermes_home / "config.yaml"
    env = load_dotenv(hermes_home / ".env")
    env.update(os.environ)
    cfg: dict[str, Any] = {}
    if config_path.exists():
        try:
            import yaml  # type: ignore

            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            cfg = loaded if isinstance(loaded, dict) else {}
        except Exception:
            cfg = {}
    model_cfg = _dict_child(cfg, "model")
    providers_cfg = _dict_child(cfg, "providers")
    nightly_cfg = _dict_child(cfg, "scope_recall_nightly_digest")

    provider = str(getattr(options, "provider", "") or nightly_cfg.get("provider") or model_cfg.get("provider") or "").strip()
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
        append_v1_raw = nightly_cfg.get("append_v1", provider_cfg.get("append_v1", model_cfg.get("append_v1", True)))
    append_v1 = config_bool_value(append_v1_raw, True)
    api_key = getattr(options, "api_key", "") or resolve_api_key(
        getattr(options, "api_key_env", "")
        or nightly_cfg.get("api_key")
        or nightly_cfg.get("api_key_env")
        or nightly_cfg.get("key_env")
        or provider_cfg.get("api_key")
        or provider_cfg.get("api_key_env")
        or provider_cfg.get("key_env")
        or model_cfg.get("api_key"),
        provider,
        env,
    )
    api_mode = normalize_digest_api_mode(
        getattr(options, "api_mode", "") or nightly_cfg.get("api_mode") or provider_cfg.get("api_mode") or model_cfg.get("api_mode"),
        provider=provider,
        base_url=str(base_url or ""),
    )
    return {
        "provider": provider,
        "model": str(model or "gpt-4o-mini"),
        "base_url": str(base_url or "https://api.openai.com").rstrip("/"),
        "endpoint": str(endpoint or "").rstrip("/"),
        "append_v1": append_v1,
        "api_key": api_key,
        "api_mode": api_mode,
    }


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


def responses_endpoint(base_url: str) -> str:
    endpoint = str(base_url or "").strip().rstrip("/")
    if not endpoint:
        endpoint = "https://api.openai.com/v1"
    if endpoint.endswith("/responses"):
        return endpoint
    return endpoint + "/responses"


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
) -> str:
    endpoint_url = chat_completions_endpoint(base_url, endpoint=endpoint, append_v1=append_v1)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You extract durable memory as strict JSON."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 1800,
    }
    request = urllib.request.Request(
        endpoint_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = redact_sensitive(exc.read().decode("utf-8", errors="replace")[:500])
        raise RuntimeError(f"LLM HTTP {exc.code} at {endpoint_url}: {body}") from exc
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return str(message.get("content") or "")


def call_codex_responses_llm(prompt: str, *, model: str, base_url: str, api_key: str, timeout: float) -> str:
    payload = {
        "model": model,
        "instructions": "You extract durable memory as strict JSON.",
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
    endpoint_url = responses_endpoint(base_url)
    request = urllib.request.Request(
        endpoint_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = redact_sensitive(exc.read().decode("utf-8", errors="replace")[:500])
        raise RuntimeError(f"LLM HTTP {exc.code} at {endpoint_url}: {body}") from exc
    return decode_responses_body(body)


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
) -> str:
    if not api_key:
        raise RuntimeError("API key not found for nightly digest")
    mode = normalize_digest_api_mode(api_mode, provider="", base_url=base_url)
    if mode == "codex_responses":
        return call_codex_responses_llm(prompt, model=model, base_url=base_url, api_key=api_key, timeout=timeout)
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
    )


def classify_llm_error(exc: Exception) -> tuple[str, bool]:
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
    max_attempts: int = 1,
    retry_delay: float = 0.0,
) -> str:
    last_error: Exception | None = None
    last_kind = "unknown"
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
            )
        except Exception as exc:
            last_error = exc
            last_kind, last_retryable = classify_llm_error(exc)
            if (not last_retryable) or attempt >= attempts:
                break
            if retry_delay > 0:
                time.sleep(max(0.0, float(retry_delay)))
    assert last_error is not None
    raise RuntimeError(
        f"{last_kind} after {last_attempt} attempt(s): {type(last_error).__name__}: {redact_sensitive(str(last_error)[:400])}"
    ) from last_error


__all__ = [
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
    "normalize_digest_api_mode",
    "resolve_api_key",
    "resolve_llm_config",
    "responses_endpoint",
]
