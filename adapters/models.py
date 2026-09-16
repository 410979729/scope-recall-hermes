"""Small production auxiliary-model adapters with explicit two-route configuration."""
from __future__ import annotations

from dataclasses import dataclass
import base64
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Protocol

from ..core.recall_policy import (
    EMBEDDING_DIALECTS,
    EMBEDDING_SPACE,
    build_embedding_space,
    encode_embedding_text,
)
from ..core.storage import StoredSource
from ..runtime.model_budget import AuxiliaryBudgetLedger, BudgetPolicy
from ..secret_patterns import contains_secret_like_text


MAX_CHAT_RESPONSE_BYTES = 1_048_576
MAX_EMBED_RESPONSE_BYTES = 16 * 1024 * 1024
EMBED_RESERVE_FLOOR = 8192
RESERVE_ENVELOPE_MARGIN = 256
MAX_CREDENTIAL_BYTES = 8192
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class AuxiliaryModelError(RuntimeError):
    def __init__(self, error_type: str, *, detail: str | None = None) -> None:
        self.error_type = error_type
        self.detail = detail if type(detail) is str and detail.isdigit() and len(detail) == 3 else None
        message = error_type if detail is None else f"{error_type}:{detail}"
        if contains_secret_like_text(message):
            message = error_type
        super().__init__(message)



#: A provider's own name for why it refused, e.g. ``GoUsageLimitError``.  Short,
#: symbolic, drawn from the provider's vocabulary -- unlike the message beside
#: it, which is free text and may carry account identifiers or URLs.
_REFUSAL_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def provider_refusal_code(raw: object) -> str | None:
    """The bounded symbol naming why a provider refused, or ``None``.

    The whole body is discarded on a failure, and rightly so -- it is free text.
    But the *type* inside it is not: it is the difference between "the model is
    failing" and "the monthly quota is exhausted until the 27th".  A live
    instance spent four hours reporting every worker run as degraded with no
    capability gap at all, while the provider was answering every single call
    with exactly that sentence.
    """
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_CHAT_RESPONSE_BYTES:
        return None
    try:
        payload = json.loads(bytes(raw).decode("utf-8", "replace"))
    except (ValueError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    for candidate in (error.get("type") if isinstance(error, dict) else None,
                      error.get("code") if isinstance(error, dict) else None,
                      payload.get("type")):
        if type(candidate) is str and _REFUSAL_CODE.match(candidate)                 and not contains_secret_like_text(candidate):
            return candidate
    return None

class HttpTransport(Protocol):
    def post(
        self,
        url: str,
        *,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> tuple[int, bytes]: ...


_HTTP_WORKER_PATH = (Path(__file__).resolve().parents[1] / "runtime" / "_http_worker.py").resolve()
_HTTP_WORKER_STDOUT_MARGIN = 8 * 1024
_HTTP_WORKER_MAX_REQUEST_BYTES = 3 * 1024 * 1024
_HTTP_WORKER_CLEANUP_GRACE_SECONDS = 0.2


def _cleanup_http_worker(process: subprocess.Popen[bytes] | None) -> None:
    """Kill/reap only this helper, with a short bounded cleanup grace."""
    if process is None or process.poll() is not None:
        return
    try:
        process.kill()
    except OSError:
        return
    try:
        process.communicate(timeout=_HTTP_WORKER_CLEANUP_GRACE_SECONDS)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        # The primary transport exception must remain authoritative. The fixed
        # grace prevents a broken child pipe from extending the request bound.
        pass


def _validate_timeout_seconds(timeout_seconds: float) -> float:
    if type(timeout_seconds) is bool or type(timeout_seconds) not in (int, float):
        raise AuxiliaryModelError("timeout")
    value = float(timeout_seconds)
    if not math.isfinite(value) or value <= 0:
        raise AuxiliaryModelError("timeout")
    return value


def _remaining_seconds(deadline: float) -> float:
    return deadline - time.monotonic()


def _validate_credential_env_name(env_name: object) -> str:
    if type(env_name) is not str or not env_name or _ENV_NAME_RE.fullmatch(env_name) is None:
        raise ValueError("credential_env")
    return env_name


class HttpsTransport:
    """Bounded HTTPS POST; query callers may own a persistent stdlib worker."""

    def __init__(self, *, persistent: bool = False):
        from ..runtime.http_session import HttpWorkerSession
        self._session = HttpWorkerSession() if persistent else None
        self._post_lock = threading.Lock()

    def close(self):
        if self._session is not None:
            self._session.close()

    def post(self, url: str, *, body: bytes, headers: Mapping[str, str],
             timeout_seconds: float, max_response_bytes: int) -> tuple[int, bytes]:
        """Bound the entire exchange, including waiting for a query session."""
        if self._session is None:
            return self._post(url, body=body, headers=headers,
                              timeout_seconds=timeout_seconds, max_response_bytes=max_response_bytes)
        deadline = time.monotonic() + _validate_timeout_seconds(timeout_seconds)
        if not self._post_lock.acquire(timeout=max(0, _remaining_seconds(deadline))):
            raise AuxiliaryModelError("timeout")
        try:
            return self._post(url, body=body, headers=headers,
                              timeout_seconds=_remaining_seconds(deadline),
                              max_response_bytes=max_response_bytes)
        except BaseException:
            if self._session is not None:
                self._session.discard()
            raise
        finally:
            self._post_lock.release()

    def _post(
        self,
        url: str,
        *,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> tuple[int, bytes]:
        budget = _validate_timeout_seconds(timeout_seconds)
        deadline = time.monotonic() + budget
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise AuxiliaryModelError("endpoint_invalid")
        if not _HTTP_WORKER_PATH.is_file():
            raise AuxiliaryModelError("transport_unavailable")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise AuxiliaryModelError("response_limit")
        if not isinstance(body, bytes) or not isinstance(headers, Mapping):
            raise AuxiliaryModelError("request_invalid")
        request = {
            "url": url,
            "body_b64": base64.b64encode(body).decode("ascii"),
            "headers": dict(headers),
            "timeout_seconds": budget,
            "max_response_bytes": max_response_bytes,
        }
        try:
            request_bytes = json.dumps(
                request, ensure_ascii=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise AuxiliaryModelError("request_invalid") from exc
        if len(request_bytes) > _HTTP_WORKER_MAX_REQUEST_BYTES:
            raise AuxiliaryModelError("request_limit")
        command = [sys.executable, "-I", "-B", str(_HTTP_WORKER_PATH)]
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process: subprocess.Popen[bytes] | None = None
        try:
            remaining = _remaining_seconds(deadline)
            if remaining <= 0:
                raise AuxiliaryModelError("timeout")
            if self._session is not None:
                stdout, stderr = self._session.exchange(
                    command, request_bytes, deadline=deadline,
                    max_stdout=(max_response_bytes * 4) // 3 + _HTTP_WORKER_STDOUT_MARGIN,
                    startupinfo=startupinfo, creationflags=creationflags,
                )
            else:
                process = subprocess.Popen(
                    command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, startupinfo=startupinfo, creationflags=creationflags,
                )
                remaining = _remaining_seconds(deadline)
                if remaining <= 0:
                    raise AuxiliaryModelError("timeout")
                stdout, stderr = process.communicate(input=request_bytes, timeout=remaining)
            if _remaining_seconds(deadline) <= 0:
                raise AuxiliaryModelError("timeout")
            if process is not None and process.returncode != 0:
                raise AuxiliaryModelError("transport_worker", detail=str(process.returncode))
            max_stdout = (max_response_bytes * 4) // 3 + _HTTP_WORKER_STDOUT_MARGIN
            if len(stdout) > max_stdout or len(stderr) > _HTTP_WORKER_STDOUT_MARGIN:
                raise AuxiliaryModelError("transport_worker_protocol")
            try:
                result = json.loads(stdout.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise AuxiliaryModelError("transport_worker_protocol") from exc
            if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                raise AuxiliaryModelError("transport_worker_protocol")
            status = result.get("status")
            if status is not None and (type(status) is not int or not 100 <= status <= 599):
                raise AuxiliaryModelError("transport_worker_protocol")
            body_b64 = result.get("body_b64", "")
            if type(body_b64) is not str:
                raise AuxiliaryModelError("transport_worker_protocol")
            try:
                response_body = base64.b64decode(body_b64.encode("ascii"), validate=True)
            except (UnicodeError, ValueError) as exc:
                raise AuxiliaryModelError("transport_worker_protocol") from exc
            if len(response_body) > max_response_bytes:
                raise AuxiliaryModelError("response_limit")
            if result["ok"]:
                if set(result) != {"ok", "status", "body_b64"} or status is None:
                    raise AuxiliaryModelError("transport_worker_protocol")
                return status, response_body
            error_type = result.get("error")
            if (
                set(result) - {"ok", "error", "status", "body_b64"}
                or type(error_type) is not str
                or error_type not in {
                    "endpoint_invalid",
                    "http_redirect",
                    "http_protocol",
                    "network_error",
                    "request_limit",
                    "response_limit",
                    "timeout",
                }
            ):
                raise AuxiliaryModelError("transport_worker_protocol")
            raise AuxiliaryModelError(error_type, detail=None if status is None else str(status))
        except AuxiliaryModelError:
            if self._session is not None:
                self._session.discard()
            raise
        except (subprocess.TimeoutExpired, TimeoutError) as exc:
            if self._session is not None:
                self._session.discard()
            raise AuxiliaryModelError("timeout") from exc
        except OSError as exc:
            raise AuxiliaryModelError("network_error", detail=type(exc).__name__) from exc
        finally:
            _cleanup_http_worker(process)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _load_credential(env_name: str) -> str:
    _validate_credential_env_name(env_name)
    value = os.environ.get(env_name)
    if type(value) is not str:
        raise AuxiliaryModelError("credential_missing")
    value = value.strip()
    if not value or "\r" in value or "\n" in value:
        raise AuxiliaryModelError("credential_shape_invalid")
    if len(value.encode("utf-8")) > MAX_CREDENTIAL_BYTES:
        raise AuxiliaryModelError("credential_shape_invalid")
    return value


def _reject_secrets(value: str) -> None:
    if contains_secret_like_text(value):
        raise AuxiliaryModelError("sensitive_request")


def _load_json_object(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise AuxiliaryModelError("unicode_error", detail=type(exc).__name__) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AuxiliaryModelError("invalid_json") from exc
    if not isinstance(payload, dict):
        raise AuxiliaryModelError("unsupported_response_shape")
    return payload


def _extract_chat_content(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise AuxiliaryModelError("unsupported_response_shape")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise AuxiliaryModelError("unsupported_response_shape")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise AuxiliaryModelError("unsupported_response_shape")
    if message.get("role") != "assistant":
        raise AuxiliaryModelError("unsupported_response_shape")
    # OpenAI-compatible gateways may attach reasoning/refusal/annotation
    # metadata to an assistant message.  It is not answer content and must be
    # ignored; a tool request is a different protocol and cannot be silently
    # treated as a text proposal.
    if message.get("tool_calls") or message.get("function_call"):
        raise AuxiliaryModelError("unsupported_response_shape")
    content = message.get("content")
    if type(content) is not str:
        raise AuxiliaryModelError("unsupported_response_shape")
    return content


def build_gemini_embed_body(encoded_text: str, *, model: str | None = None, dimensions: int | None = None) -> bytes:
    model = model or EMBEDDING_SPACE["model"]
    body = {
        "requests": [
            {
                "model": f"models/{model}",
                "content": {"parts": [{"text": encoded_text}]},
                "embedContentConfig": {
                    "outputDimensionality": dimensions or EMBEDDING_SPACE["dimensions"],
                    "autoTruncate": False,
                },
            }
        ]
    }
    return _json_bytes(body)


def build_openai_embed_body(encoded_text: str, *, model: str, dimensions: int) -> bytes:
    """The /v1/embeddings request shape MiniMax, Qwen and OpenAI all accept.

    ``dimensions`` is sent because the space digest commits to a width: a
    provider that silently returned a different one would produce vectors the
    store cannot compare, and the length check on the response catches it.
    """
    return _json_bytes({"model": model, "input": [encoded_text], "dimensions": dimensions})


def parse_embedding_response(payload: object, *, dialect: str, dimensions: int) -> tuple[tuple[float, ...], dict[str, int] | None]:
    """Read one vector, and any usage the provider reported, from a response.

    The two dialects differ only here and in the request body; reservation,
    transport, deadlines and error mapping are shared.
    """
    if not isinstance(payload, dict):
        raise AuxiliaryModelError("unsupported_response_shape")
    usage: dict[str, int] | None = None
    if dialect == "gemini":
        metadata = payload.get("usageMetadata")
        if isinstance(metadata, dict) and type(metadata.get("promptTokenCount")) is int:
            usage = {"promptTokenCount": metadata["promptTokenCount"]}
        embeddings = payload.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != 1:
            raise AuxiliaryModelError("unsupported_response_shape")
        row = embeddings[0]
        if not isinstance(row, dict) or set(row) != {"values"}:
            raise AuxiliaryModelError("unsupported_response_shape")
        return validate_embedding_vector(row["values"], dimensions=dimensions), usage
    metadata = payload.get("usage")
    if isinstance(metadata, dict) and type(metadata.get("prompt_tokens")) is int:
        usage = {"promptTokenCount": metadata["prompt_tokens"]}
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise AuxiliaryModelError("unsupported_response_shape")
    return validate_embedding_vector(data[0].get("embedding"), dimensions=dimensions), usage


def validate_embedding_vector(values: object, *, dimensions: int | None = None) -> tuple[float, ...]:
    if not isinstance(values, list):
        raise AuxiliaryModelError("unsupported_response_shape")
    if len(values) != (dimensions or EMBEDDING_SPACE["dimensions"]):
        raise AuxiliaryModelError("vector_dimension_mismatch")
    converted: list[float] = []
    for value in values:
        if type(value) not in (int, float) or not math.isfinite(float(value)):
            raise AuxiliaryModelError("vector_nonfinite")
        converted.append(float(value))
    if not any(number != 0.0 for number in converted):
        raise AuxiliaryModelError("vector_zero")
    return tuple(converted)


def conservative_embed_reserve(body: bytes) -> int:
    return max(EMBED_RESERVE_FLOOR, len(body) + RESERVE_ENVELOPE_MARGIN)


def conservative_consolidation_input_reserve(body: bytes, configured: int) -> int:
    return max(configured, len(body) + RESERVE_ENVELOPE_MARGIN)


def _validate_consolidation_response_format(value: object) -> Mapping[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("response_format")
    if dict(value) != {"type": "json_object"}:
        raise ValueError("response_format")
    return MappingProxyType({"type": "json_object"})


def _validate_consolidation_reasoning_effort(value: object, *, opencode_go: bool = False) -> str | None:
    if value is None:
        return None
    allowed = {"low", "high", "max", "none"} if opencode_go else {"low", "high", "max"}
    if type(value) is not str or value not in allowed:
        raise ValueError("reasoning_effort")
    return value


_CONSOLIDATION_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$")
_CONSOLIDATION_HEADER_RESERVED = {"authorization", "content-type", "user-agent", "host", "content-length"}
_OPENCODE_GO_SESSION_HEADER = "x-opencode-session"
_OPENCODE_GO_DEFAULT_SESSION = "scope-recall-auxiliary-consolidation"
_OPENCODE_GO_ENDPOINT_MARK = "opencode.ai/zen/go"


def _validate_consolidation_headers(value: object) -> Mapping[str, str] | None:
    """Optional provider-specific static headers (e.g. routing session ids).

    Credential-bearing or transport-owned header names are rejected so this
    channel can never smuggle a second Authorization or override the explicit
    Content-Type/User-Agent set by the client.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("headers")
    if len(value) > 8:
        raise ValueError("headers")
    clean: dict[str, str] = {}
    for raw_name, raw_val in value.items():
        if type(raw_name) is not str or _CONSOLIDATION_HEADER_NAME_RE.fullmatch(raw_name) is None:
            raise ValueError("headers")
        if raw_name.casefold() in _CONSOLIDATION_HEADER_RESERVED:
            raise ValueError("headers")
        if type(raw_val) is not str:
            raise ValueError("headers")
        text = raw_val.strip()
        if not text or len(text) > 512 or "\r" in text or "\n" in text:
            raise ValueError("headers")
        clean[raw_name] = text
    return MappingProxyType(clean)


def _opencode_go_endpoint(endpoint: str) -> bool:
    if type(endpoint) is not str:
        return False
    parsed = urllib.parse.urlsplit(endpoint)
    return (parsed.scheme == "https" and parsed.hostname == "opencode.ai"
            and (parsed.path == "/zen/go" or parsed.path.startswith("/zen/go/")))


def _opencode_session_id() -> str:
    for name in ("SCOPE_RECALL_TEST_OPENCODE_SESSION", "SCOPE_RECALL_P11_TEST_CONTEXT"):
        value = os.environ.get(name)
        if type(value) is not str:
            continue
        text = value.strip()
        if not text or len(text) > 512 or "\r" in text or "\n" in text:
            continue
        if name == "SCOPE_RECALL_P11_TEST_CONTEXT":
            return "scope-recall-test-" + text
        return text
    return _OPENCODE_GO_DEFAULT_SESSION


def _consolidation_request_headers(route: "ConsolidationRouteConfig", key: str) -> dict[str, str]:
    headers = {
        **dict(route.headers or {}),
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "ScopeRecall-AuxiliaryConsolidation/1.1",
    }
    if _opencode_go_endpoint(route.endpoint) and not any(
        name.casefold() == _OPENCODE_GO_SESSION_HEADER for name in headers
    ):
        headers[_OPENCODE_GO_SESSION_HEADER] = _opencode_session_id()
    return headers


def model_output_reserve(policy: BudgetPolicy, model: str, requested_output: int) -> int:
    floor = policy.model_reserve_output.get(model, policy.default_reserve_output)
    return max(requested_output, floor)


@dataclass(frozen=True)
class EmbeddingRouteConfig:
    #: The embedding model, its endpoint and its wire dialect are configuration,
    #: not a constant. Omitting them keeps the shipped Gemini defaults, so an
    #: existing installation resolves the same space digest and keeps its vector
    #: directory; naming a different model produces a different digest, which
    #: moves the store and refuses the old vectors rather than comparing across
    #: incompatible geometries.
    credential_env: str
    model: str | None = None
    endpoint: str | None = None
    dimensions: int | None = None
    dialect: str | None = None

    def __post_init__(self) -> None:
        _validate_credential_env_name(self.credential_env)
        stated = [self.model, self.endpoint, self.dimensions, self.dialect]
        if any(value is not None for value in stated) and any(value is None for value in stated):
            # Half a descriptor would silently mix a new model with the default
            # dimensionality or dialect, and the digest would not reveal it.
            raise ValueError("embedding_route_partial_space")
        if self.dialect is not None and self.dialect not in EMBEDDING_DIALECTS:
            raise ValueError("embedding_route_dialect")

    def space(self) -> dict:
        """The embedding space this route addresses, defaults included."""
        if self.model is None:
            return dict(EMBEDDING_SPACE)
        return build_embedding_space(
            model=self.model, dimensions=self.dimensions,
            endpoint=self.endpoint, dialect=self.dialect,
        )

    def wire_dialect(self) -> str:
        return self.dialect or "gemini"


@dataclass(frozen=True)
class ConsolidationRouteConfig:
    model: str
    endpoint: str
    credential_env: str
    output_limit_field: str
    max_output_tokens: int
    thinking: Mapping[str, str] | None = None
    response_format: Mapping[str, str] | None = None
    reasoning_effort: str | None = None
    stream: bool = False
    n: int = 1
    headers: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if type(self.model) is not str or not self.model:
            raise ValueError("model")
        if type(self.endpoint) is not str or not self.endpoint.startswith("https://"):
            raise ValueError("endpoint")
        _validate_credential_env_name(self.credential_env)
        if self.output_limit_field not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("output_limit_field")
        if type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 131_072:
            raise ValueError("max_output_tokens")
        if self.stream is not False:
            raise ValueError("stream")
        if type(self.n) is not int or self.n != 1:
            raise ValueError("n")
        if self.thinking is not None and not isinstance(self.thinking, Mapping):
            raise ValueError("thinking")
        object.__setattr__(self, "response_format", _validate_consolidation_response_format(self.response_format))
        object.__setattr__(
            self, "reasoning_effort", _validate_consolidation_reasoning_effort(
                self.reasoning_effort, opencode_go=_opencode_go_endpoint(self.endpoint)
            )
        )
        object.__setattr__(self, "headers", _validate_consolidation_headers(self.headers))


class GeminiEmbeddingAdapter:
    def __init__(
        self,
        route: EmbeddingRouteConfig,
        *,
        ledger: AuxiliaryBudgetLedger,
        transport: HttpTransport | None = None,
    ) -> None:
        self._route = route
        self._ledger = ledger
        self._transport = transport if transport is not None else HttpsTransport()
        self._query_transport = transport if transport is not None else HttpsTransport(persistent=True)
        self._owns_transport = transport is None
        space = route.space()
        self._space = space
        self._endpoint = space["endpoint"]
        self._model = space["model"]
        self._dimensions = space["dimensions"]
        self._dialect = route.wire_dialect()

    def embed_query(self, text: str, *, remaining_seconds: float) -> Sequence[float]:
        encoded = encode_embedding_text(text, kind="query")
        return self._embed(encoded, remaining_seconds=remaining_seconds, transport=self._query_transport)

    def close(self):
        """Release only transports created by this adapter, not injected ports."""
        if self._owns_transport:
            self._query_transport.close()
            self._transport.close()

    def embed_source(self, source: StoredSource, *, remaining_seconds: float) -> Sequence[float]:
        encoded = encode_embedding_text(source.event["content"], kind="document")
        return self._embed(encoded, remaining_seconds=remaining_seconds)

    def embed_text(self, text: str, *, remaining_seconds: float) -> Sequence[float]:
        """Embed already-rendered text as a document.

        Derived objects have no ``event["content"]`` to read, so a claim arrives
        here as the rendered assertion. Same encoding as a source, so both land
        in one comparable space.
        """
        encoded = encode_embedding_text(text, kind="document")
        return self._embed(encoded, remaining_seconds=remaining_seconds)

    def _embed(self, encoded_text: str, *, remaining_seconds: float, transport: HttpTransport | None = None) -> Sequence[float]:
        deadline = time.monotonic() + _validate_timeout_seconds(remaining_seconds)
        _reject_secrets(encoded_text)
        if self._dialect == "gemini":
            body = build_gemini_embed_body(encoded_text, model=self._model, dimensions=self._dimensions)
        else:
            body = build_openai_embed_body(encoded_text, model=self._model, dimensions=self._dimensions)
        reserve_input = conservative_embed_reserve(body)
        remaining = _remaining_seconds(deadline)
        if remaining <= 0:
            raise AuxiliaryModelError("timeout")
        request_id: int | None = None
        status = "network_error"
        usage: dict[str, int] | None = None
        pending: BaseException | None = None
        result: Sequence[float] | None = None
        try:
            key = _load_credential(self._route.credential_env)
            request_id = self._ledger.reserve(
                self._model,
                body,
                reserved_input=reserve_input,
                reserved_output=0,
                timeout_seconds=_remaining_seconds(deadline),
            )
            http_remaining = _remaining_seconds(deadline)
            if http_remaining <= 0:
                raise AuxiliaryModelError("timeout")
            status_code, raw = (transport or self._transport).post(
                self._endpoint,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    # Google authenticates with its own header; every
                    # OpenAI-compatible provider uses bearer auth.
                    **({"x-goog-api-key": key} if self._dialect == "gemini"
                       else {"Authorization": f"Bearer {key}"}),
                    "User-Agent": "ScopeRecall-AuxiliaryEmbed/1.1",
                },
                timeout_seconds=http_remaining,
                max_response_bytes=MAX_EMBED_RESPONSE_BYTES,
            )
            status = f"http_{status_code}"
            if status_code != 200:
                refusal = provider_refusal_code(raw)
                if refusal is not None:
                    status = f"{status}:{refusal}"
                raise AuxiliaryModelError("http_status", detail=str(status_code))
            payload = _load_json_object(raw)
            result, usage = parse_embedding_response(
                payload, dialect=self._dialect, dimensions=self._dimensions
            )
        except AuxiliaryModelError as exc:
            pending = exc
        except ValueError as exc:
            if str(exc) == "budget_exhausted_or_meter_breach":
                pending = AuxiliaryModelError("budget_exhausted")
            elif str(exc) in {"ledger_not_initialized", "unsupported_model", "unsupported_model_or_size"}:
                pending = AuxiliaryModelError("budget_unavailable")
            elif str(exc) == "ledger_busy_timeout":
                pending = AuxiliaryModelError("budget_unavailable")
            else:
                pending = AuxiliaryModelError("request_rejected", detail=type(exc).__name__)
        finally:
            remaining = _remaining_seconds(deadline)
            if request_id is not None:
                try:
                    final = self._ledger.finish_embedding(
                        request_id,
                        status,
                        usage,
                        timeout_seconds=max(.001, remaining),
                    )
                except Exception as settle_exc:
                    if pending is None:
                        pending = settle_exc
                else:
                    if pending is None:
                        if "meter_breach" in final:
                            pending = AuxiliaryModelError("meter_breach")
                        elif "usage_unknown" in final and status.startswith("http_200"):
                            pending = AuxiliaryModelError("missing_usage")
        if pending is not None:
            raise pending
        assert result is not None
        return result


class OpenAIConsolidationAdapter:
    def __init__(
        self,
        route: ConsolidationRouteConfig,
        *,
        ledger: AuxiliaryBudgetLedger,
        reserve_input: int,
        transport: HttpTransport | None = None,
    ) -> None:
        self._route = route
        self._ledger = ledger
        self._reserve_input = reserve_input
        self._transport = transport if transport is not None else HttpsTransport()

    def propose(self, messages: list[dict], *, remaining_seconds: float) -> str:
        deadline = time.monotonic() + _validate_timeout_seconds(remaining_seconds)
        if not isinstance(messages, list) or not messages:
            raise AuxiliaryModelError("input_invalid")
        for message in messages:
            if not isinstance(message, dict) or set(message) - {"role", "content"}:
                raise AuxiliaryModelError("input_invalid")
            if message.get("role") not in {"system", "user", "assistant", "tool"}:
                raise AuxiliaryModelError("input_invalid")
            content = message.get("content")
            if type(content) is not str:
                raise AuxiliaryModelError("input_invalid")
            _reject_secrets(content)
        body_obj: dict[str, Any] = {
            "model": self._route.model,
            "messages": messages,
            "stream": self._route.stream,
            "n": self._route.n,
            self._route.output_limit_field: self._route.max_output_tokens,
        }
        if self._route.thinking is not None:
            body_obj["thinking"] = dict(self._route.thinking)
        if self._route.response_format is not None:
            body_obj["response_format"] = dict(self._route.response_format)
        if self._route.reasoning_effort is not None:
            body_obj["reasoning_effort"] = self._route.reasoning_effort
        body = _json_bytes(body_obj)
        _reject_secrets(body.decode("utf-8"))
        reserved_input = conservative_consolidation_input_reserve(body, self._reserve_input)
        reserved_output = model_output_reserve(
            self._ledger.policy,
            self._route.model,
            self._route.max_output_tokens,
        )
        remaining = _remaining_seconds(deadline)
        if remaining <= 0:
            raise AuxiliaryModelError("timeout")
        request_id: int | None = None
        status = "network_error"
        usage: dict[str, int] | None = None
        pending: BaseException | None = None
        result: str | None = None
        try:
            key = _load_credential(self._route.credential_env)
            request_id = self._ledger.reserve(
                self._route.model,
                body,
                reserved_input=reserved_input,
                reserved_output=reserved_output,
                timeout_seconds=_remaining_seconds(deadline),
            )
            http_remaining = _remaining_seconds(deadline)
            if http_remaining <= 0:
                raise AuxiliaryModelError("timeout")
            status_code, raw = self._transport.post(
                self._route.endpoint,
                body=body,
                headers=_consolidation_request_headers(self._route, key),
                timeout_seconds=http_remaining,
                max_response_bytes=MAX_CHAT_RESPONSE_BYTES,
            )
            status = f"http_{status_code}"
            if status_code != 200:
                refusal = provider_refusal_code(raw)
                if refusal is not None:
                    status = f"{status}:{refusal}"
                raise AuxiliaryModelError("http_status", detail=str(status_code))
            payload = _load_json_object(raw)
            candidate = payload.get("usage")
            if isinstance(candidate, dict) and all(
                type(candidate.get(name)) is int and candidate[name] >= 0 for name in ("prompt_tokens", "completion_tokens")
            ):
                usage = {
                    "prompt_tokens": candidate["prompt_tokens"],
                    "completion_tokens": candidate["completion_tokens"],
                }
            result = _extract_chat_content(payload)
        except AuxiliaryModelError as exc:
            pending = exc
        except ValueError as exc:
            if str(exc) == "budget_exhausted_or_meter_breach":
                pending = AuxiliaryModelError("budget_exhausted")
            elif str(exc) in {"ledger_not_initialized", "unsupported_model", "unsupported_model_or_size"}:
                pending = AuxiliaryModelError("budget_unavailable")
            elif str(exc) == "ledger_busy_timeout":
                pending = AuxiliaryModelError("budget_unavailable")
            else:
                pending = AuxiliaryModelError("request_rejected", detail=type(exc).__name__)
        finally:
            remaining = _remaining_seconds(deadline)
            if request_id is not None:
                try:
                    final = self._ledger.finish(
                        request_id,
                        status,
                        usage,
                        timeout_seconds=max(.001, remaining),
                    )
                except Exception as settle_exc:
                    if pending is None:
                        pending = settle_exc
                else:
                    if pending is None:
                        if final == "meter_breach" or "meter_breach" in final:
                            pending = AuxiliaryModelError("meter_breach")
                        elif "usage_unknown" in final and status.startswith("http_200"):
                            pending = AuxiliaryModelError("missing_usage")
        if pending is not None:
            raise pending
        assert result is not None
        return result
