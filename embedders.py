"""Embedding provider adapters for local, OpenAI-compatible, and hosted vector backends.

Keep provider quirks isolated here so vector stores and repair scripts only see float vectors with stable dimensions."""

from __future__ import annotations

import hashlib
import importlib
import math
import os
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from functools import partial
from typing import Any, Iterable, Mapping

from .aliases import canonicalize_alias
from .capture_filters import sanitize_report_text
from .embedding_request_runner import (
    BoundedEmbedderRequestRunner,
    EmbedderRequestClosedError,
    EmbedderRequestDeadlineError,
    HostedEmbedderWorkerLimitError,
    InFlightEmbedderRequestError,
)
from .embedding_validation import validate_embedding_batch, zip_embedding_rows
from .gating import clean_text, query_tokens
from .http_utils import (
    UnsafeEndpointError,
    is_credential_key,
    require_safe_endpoint,
    safe_urlopen,
)

try:
    from openai import OpenAI  # type: ignore[reportMissingImports]
except Exception:  # pragma: no cover - optional dependency
    OpenAI = None

try:
    from openai import DefaultHttpxClient  # type: ignore[reportMissingImports]
except Exception:  # pragma: no cover - compatibility with older optional SDKs
    DefaultHttpxClient = None

try:
    import httpx as _httpx  # type: ignore[reportMissingImports]
except Exception:  # pragma: no cover - optional dependency of OpenAI adapters
    _httpx = None

try:
    from sentence_transformers import SentenceTransformer  # type: ignore[reportMissingImports]
except Exception:  # pragma: no cover - optional dependency
    SentenceTransformer = None

import urllib.error
import urllib.parse
import urllib.request
import json as _json_lib


_KNOWN_EMBEDDING_DIMS = {
    "hash-v1": 256,
    "debug-hash-v1": 16,
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-004": 768,
    "gemini-embedding-001": 3072,
    "gemini-embedding-2": 3072,
    "jina-embeddings-v5-text-small": 1024,
    "jina-embeddings-v5-text-nano": 768,
    "voyage-3": 1024,
    "voyage-3-lite": 512,
    "voyage-3-large": 1024,
    "voyage-4": 1024,
    "voyage-4-lite": 1024,
    "voyage-4-large": 1024,
    "nomic-embed-text": 768,
    "mxbai-embed-large": 1024,
    "bge-m3": 1024,
    "baai/bge-m3": 1024,
    "bge-small-en-v1.5": 384,
    "baai/bge-small-en-v1.5": 384,
    "all-minilm-l6-v2": 384,
    "sentence-transformers/all-minilm-l6-v2": 384,
    "all-mpnet-base-v2": 768,
    "sentence-transformers/all-mpnet-base-v2": 768,
    "embo-01": 1536,
    "minimax-embedding": 1536,
}


_SENTENCE_TRANSFORMER_CACHE: dict[tuple[str, str | None], Any] = {}
_SENTENCE_TRANSFORMER_CACHE_GUARD = threading.Lock()
_SENTENCE_TRANSFORMER_LOAD_FLIGHTS: dict[
    tuple[str, str | None], Future[Any]
] = {}


def _resolve_sentence_transformer() -> Any:
    """Resolve the optional local embedder even when installed after module import."""

    global SentenceTransformer
    if SentenceTransformer is not None:
        return SentenceTransformer
    try:
        module = importlib.import_module("sentence_transformers")
    except Exception:
        return None
    candidate = getattr(module, "SentenceTransformer", None)
    if candidate is not None:
        SentenceTransformer = candidate
    return candidate


class _SanitizedModelLoadError(RuntimeError):
    """A cohort-safe model-load error with no raw exception cause."""

_CONNECTION_EXCEPTION_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "NetworkError",
        "PoolTimeout",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "WriteError",
        "WriteTimeout",
    }
)
_CONNECTION_EXCEPTION_MODULE_PREFIXES = ("httpcore", "httpx", "openai")
_DEFAULT_CONNECTION_RETRY_DELAYS = (2.0, 4.0, 8.0)
_MAX_CONNECTION_RETRY_DELAYS = 8
_MAX_CONNECTION_RETRY_DELAY_SECONDS = 300.0
_MIN_TRANSPORT_TIMEOUT_SECONDS = 0.05
_MAX_TRANSPORT_TIMEOUT_SECONDS = 300.0
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
_DEFAULT_READ_TIMEOUT_SECONDS = 15.0
_DEFAULT_WRITE_TIMEOUT_SECONDS = 15.0
_DEFAULT_POOL_TIMEOUT_SECONDS = 5.0
_DEFAULT_QUERY_TIMEOUT_SECONDS = 8.0
_DEFAULT_WRITER_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAINTENANCE_TIMEOUT_SECONDS = 45.0
_SDK_MAX_RETRIES = 0
_EMBED_OPERATIONS = frozenset({"query", "writer", "maintenance"})


def _close_quietly(handle: Any) -> None:
    """Close a transport handle on this thread. Never spawn a closer thread."""

    closer = getattr(handle, "close", None)
    if not callable(closer):
        return
    try:
        closer()
    except Exception:
        return


class UnsupportedEmbedderSdkError(RuntimeError):
    """Hosted SDK or HTTP client cannot honor timeout or max_retries."""


def _unsupported_sdk_contract(provider: str, surface: str) -> UnsupportedEmbedderSdkError:
    """Fail closed when a hosted SDK/client cannot honor timeout or max_retries."""

    return UnsupportedEmbedderSdkError(
        f"{provider} {surface} rejected required timeout or max_retries; "
        "hosted OpenAI-compatible embeddings fail closed as unsupported"
    )


def _config_bool_value(value: Any, default: bool = False) -> bool:
    """Interpret JSON booleans and Hermes UI strings without truthy-string bugs."""

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default
    return default


def _coerce_connection_retry_delays(value: Any) -> tuple[float, ...]:
    """Return bounded retry delays or reject unsafe/malformed configuration."""

    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("connection_retry_delays must be an array of seconds")
    if len(value) > _MAX_CONNECTION_RETRY_DELAYS:
        raise ValueError(
            f"connection_retry_delays supports at most {_MAX_CONNECTION_RETRY_DELAYS} entries"
        )
    delays: list[float] = []
    for index, item in enumerate(value):
        if isinstance(item, bool):
            raise ValueError(
                f"connection_retry_delays[{index}] must be a finite number between 0 and "
                f"{_MAX_CONNECTION_RETRY_DELAY_SECONDS:g}"
            )
        try:
            delay = float(item)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"connection_retry_delays[{index}] must be a finite number between 0 and "
                f"{_MAX_CONNECTION_RETRY_DELAY_SECONDS:g}"
            ) from exc
        if not math.isfinite(delay) or not 0.0 <= delay <= _MAX_CONNECTION_RETRY_DELAY_SECONDS:
            raise ValueError(
                f"connection_retry_delays[{index}] must be a finite number between 0 and "
                f"{_MAX_CONNECTION_RETRY_DELAY_SECONDS:g}"
            )
        delays.append(delay)
    return tuple(delays)



def _coerce_timeout_seconds(
    value: Any,
    default: float,
    *,
    name: str,
    minimum: float = _MIN_TRANSPORT_TIMEOUT_SECONDS,
    maximum: float = _MAX_TRANSPORT_TIMEOUT_SECONDS,
) -> float:
    """Return a finite timeout in seconds or reject unsafe configuration."""

    if value is None:
        return float(default)
    if isinstance(value, bool):
        raise ValueError(
            f"{name} must be a finite number between {minimum:g} and {maximum:g}"
        )
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name} must be a finite number between {minimum:g} and {maximum:g}"
        ) from exc
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise ValueError(
            f"{name} must be a finite number between {minimum:g} and {maximum:g}"
        )
    return parsed


def _normalize_embed_operation(operation: Any) -> str:
    name = str(operation or "writer").strip().casefold()
    if name not in _EMBED_OPERATIONS:
        raise ValueError("embedding operation must be query, writer, or maintenance")
    return name


def close_embedder(embedder: Any) -> None:
    """Idempotently terminal-close an embedder if it exposes close.

    This is lifecycle cleanup (setup replacement, failed init, shutdown).
    It is not a retry-time transport reset.
    """

    if embedder is None:
        return
    closer = getattr(embedder, "close", None)
    if callable(closer):
        closer()


def _normalize_feature(token: str) -> str:
    return canonicalize_alias(token)



def _char_ngrams(token: str, n: int = 3) -> list[str]:
    token = token.strip().lower()
    if len(token) <= n:
        return [token] if token else []
    return [token[i : i + n] for i in range(0, len(token) - n + 1)]



def _coerce_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []



def _resolve_from_env(env_names: Any) -> str:
    for name in _coerce_list(env_names):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""



def _resolve_optional_value(raw_value: Any = None, env_names: Any = None) -> str | None:
    direct = str(raw_value or "").strip()
    if direct:
        return direct
    value = _resolve_from_env(env_names)
    return value or None


_HOSTED_EMBEDDER_DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "openai-compatible": "https://api.openai.com/v1",
    "generic-openai-compatible": "https://api.openai.com/v1",
    "gemini-openai-compatible": "https://api.openai.com/v1",
    "minimax": "https://api.minimaxi.com",
}


def _configured_embedder_base_url(
    provider: str,
    base_url: Any = None,
    base_url_env: Any = None,
) -> str | None:
    """Resolve an explicitly configured hosted base URL without credentials."""

    provider_name = str(provider or "").strip().casefold()
    default_env = (
        "OPENAI_BASE_URL"
        if provider_name in _HOSTED_EMBEDDER_DEFAULT_BASE_URLS
        and provider_name != "minimax"
        else None
    )
    return _resolve_optional_value(base_url, base_url_env or default_env)


def resolve_embedder_base_url(config: Mapping[str, Any]) -> str | None:
    """Return the runtime hosted base URL without reading any API key source.

    A runtime config that explicitly names ``base_url_env`` delegates URL
    selection to that environment variable. Its non-empty value therefore
    takes precedence over a packaged fallback ``base_url`` retained by deep
    config merging. Direct constructor calls keep their existing explicit
    argument precedence through :func:`_configured_embedder_base_url`.
    """

    provider = str(config.get("provider") or "local-hash").strip().casefold()
    default_url = _HOSTED_EMBEDDER_DEFAULT_BASE_URLS.get(provider)
    if default_url is None:
        return None
    configured_env_url = _resolve_from_env(config.get("base_url_env"))
    return (
        configured_env_url
        or _configured_embedder_base_url(
            provider,
            config.get("base_url"),
            config.get("base_url_env"),
        )
        or default_url
    ).rstrip("/")



def _resolve_api_keys(raw_value: Any = None, env_names: Any = None) -> list[str]:
    values: list[str] = []
    for item in _coerce_list(raw_value):
        if item:
            values.append(item)
    env_value = _resolve_from_env(env_names)
    if env_value:
        values.append(env_value)
    deduped: list[str] = []
    seen: set[str] = set()
    for item in values:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped



def _known_dimensions(model: str, fallback: int = 0) -> int:
    key = str(model or "").strip().lower()
    return int(_KNOWN_EMBEDDING_DIMS.get(key) or fallback or 0)


@dataclass
class EmbedderInfo:
    provider: str
    dimensions: int
    model: str = ""


class BaseEmbedder:
    def __init__(self, *, provider: str, dimensions: int, model: str = "") -> None:
        self.info = EmbedderInfo(provider=provider, dimensions=int(dimensions), model=model)

    @property
    def provider(self) -> str:
        return self.info.provider

    @property
    def dimensions(self) -> int:
        return self.info.dimensions

    @property
    def model(self) -> str:
        return self.info.model

    def is_available(self) -> bool:
        return True

    def probe_readiness(self) -> None:
        """Validate local prerequisites without probing a remote provider.

        Hosted adapters deliberately stop at local package/config validation so
        startup does not spend tokens or depend on network reachability. Local
        adapters may override this hook to load their model before an immutable
        vector-generation identity is recorded.
        """

        if not self.is_available():
            raise RuntimeError(f"{self.provider} embedder is unavailable")

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "dimensions": self.dimensions,
            "model": self.model,
        }

    def embed(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]

    def embed_query(self, text: str) -> list[float]:
        return self.embed(text)

    def embed_queries(self, texts: Iterable[str]) -> list[list[float]]:
        """Embed retrieval queries while preserving adapter query semantics."""

        return [self.embed_query(text) for text in texts]

    def embed_texts(self, texts: Iterable[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_maintenance(self, texts: Iterable[str]) -> list[list[float]]:
        """Embed indexed documents under the maintenance operation budget."""

        return self.embed_texts(texts)

    def close(self) -> None:
        """Release transport handles. Idempotent and safe if never opened."""

        return None


class LocalHashEmbedder(BaseEmbedder):
    def __init__(self, *, provider: str = "local-hash", dimensions: int = 256, model: str = "hash-v1") -> None:
        super().__init__(provider=provider, dimensions=dimensions, model=model)

    def _features(self, text: str) -> list[tuple[str, float]]:
        tokens = [_normalize_feature(token) for token in query_tokens(clean_text(text))]
        features: list[tuple[str, float]] = []
        for token in tokens:
            if not token:
                continue
            features.append((f"tok:{token}", 1.0))
            for gram in _char_ngrams(token, 3):
                features.append((f"tri:{gram}", 0.35))
        if not features:
            fallback = clean_text(text).lower()[:64]
            if fallback:
                features.append((f"raw:{fallback}", 1.0))
        return features

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        for feature, weight in self._features(text):
            digest = hashlib.sha1(feature.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = -1.0 if digest[4] % 2 else 1.0
            vec[idx] += sign * weight
        norm = math.sqrt(sum(value * value for value in vec))
        if norm > 0:
            vec = [value / norm for value in vec]
        return vec

    def embed_texts(self, texts: Iterable[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]


class LocalDebugEmbedder(LocalHashEmbedder):
    def __init__(self, *, dimensions: int = 16, model: str = "debug-hash-v1") -> None:
        super().__init__(provider="local-debug", dimensions=dimensions, model=model)


class OpenAICompatibleEmbedder(BaseEmbedder):
    """OpenAI-compatible hosted embedding adapter.

    Provider-specific request quirks, including float vector response
    formats, stay here rather than spreading across vector code.

    Lifecycle:
    - ``close()`` is terminal. It stops new hosted requests, drops HTTP
      and OpenAI handles on this thread, and asks an idle request worker
      to exit without joining it. The same instance cannot be reopened.
    - ``reset_transport()`` is the nonterminal retry/key-rotation path.
      It drops handles without shutting the runner and without changing
      timeout or ``max_retries``.
    - Deadline expiry returns within the remaining operation budget. The
      same still-open embedder may recover after the underlying SDK call
      later exits; a process restart is not required. Python cannot
      forcibly kill that SDK thread, so a stuck call keeps its plugin-wide
      worker permit until it returns.
    """
    def __init__(
        self,
        *,
        provider: str = "openai-compatible",
        model: str = "text-embedding-3-small",
        api_key: Any = None,
        api_key_env: Any = None,
        base_url: str | None = None,
        base_url_env: Any = None,
        dimensions: int | None = None,
        request_dimensions: bool = False,
        document_prefix: str = "",
        query_prefix: str = "",
        prompt_profile: str = "default-v1",
        connection_retry_delays: Any = None,
        allow_insecure_endpoint: bool = False,
        connect_timeout_seconds: Any = None,
        read_timeout_seconds: Any = None,
        write_timeout_seconds: Any = None,
        pool_timeout_seconds: Any = None,
        query_timeout_seconds: Any = None,
        writer_timeout_seconds: Any = None,
        maintenance_timeout_seconds: Any = None,
    ) -> None:
        resolved_dimensions = int(dimensions or _known_dimensions(model, 1536) or 1536)
        super().__init__(provider=provider, dimensions=resolved_dimensions, model=model)
        self._api_keys = _resolve_api_keys(api_key, api_key_env or "OPENAI_API_KEY")
        self._base_url = _configured_embedder_base_url(
            provider,
            base_url,
            base_url_env,
        )
        require_safe_endpoint(
            self._base_url or "https://api.openai.com/v1",
            allow_insecure=allow_insecure_endpoint,
        )
        self._allow_insecure_endpoint = allow_insecure_endpoint
        self._request_dimensions = _config_bool_value(request_dimensions)
        self._document_prefix = str(document_prefix or "")
        self._query_prefix = str(query_prefix or "")
        self._prompt_profile = str(prompt_profile or "default-v1")
        retry_delays = (
            _DEFAULT_CONNECTION_RETRY_DELAYS
            if connection_retry_delays is None
            else connection_retry_delays
        )
        self._connection_retry_delays = _coerce_connection_retry_delays(retry_delays)
        self._connect_timeout = _coerce_timeout_seconds(
            connect_timeout_seconds,
            _DEFAULT_CONNECT_TIMEOUT_SECONDS,
            name="connect_timeout_seconds",
        )
        self._read_timeout = _coerce_timeout_seconds(
            read_timeout_seconds,
            _DEFAULT_READ_TIMEOUT_SECONDS,
            name="read_timeout_seconds",
        )
        self._write_timeout = _coerce_timeout_seconds(
            write_timeout_seconds,
            _DEFAULT_WRITE_TIMEOUT_SECONDS,
            name="write_timeout_seconds",
        )
        self._pool_timeout = _coerce_timeout_seconds(
            pool_timeout_seconds,
            _DEFAULT_POOL_TIMEOUT_SECONDS,
            name="pool_timeout_seconds",
        )
        self._query_timeout = _coerce_timeout_seconds(
            query_timeout_seconds,
            _DEFAULT_QUERY_TIMEOUT_SECONDS,
            name="query_timeout_seconds",
        )
        self._writer_timeout = _coerce_timeout_seconds(
            writer_timeout_seconds,
            _DEFAULT_WRITER_TIMEOUT_SECONDS,
            name="writer_timeout_seconds",
        )
        self._maintenance_timeout = _coerce_timeout_seconds(
            maintenance_timeout_seconds,
            _DEFAULT_MAINTENANCE_TIMEOUT_SECONDS,
            name="maintenance_timeout_seconds",
        )
        self._client = None
        self._http_client = None
        self._active_key_index = 0
        self._client_guard = threading.RLock()
        self._closed = False
        self._request_runner = BoundedEmbedderRequestRunner()

    def is_available(self) -> bool:
        return bool(OpenAI is not None and self._api_keys)

    def describe(self) -> dict[str, Any]:
        payload = super().describe()
        if self._base_url:
            payload["base_url"] = self._base_url
        if self._allow_insecure_endpoint:
            payload["allow_insecure_endpoint"] = True
        payload.update(
            {
                "request_dimensions": self._request_dimensions,
                "prompt_profile": self._prompt_profile,
                "document_prefix_configured": bool(self._document_prefix),
                "query_prefix_configured": bool(self._query_prefix),
                "connection_retry_delays": list(self._connection_retry_delays),
                "connect_timeout_seconds": self._connect_timeout,
                "read_timeout_seconds": self._read_timeout,
                "write_timeout_seconds": self._write_timeout,
                "pool_timeout_seconds": self._pool_timeout,
                "query_timeout_seconds": self._query_timeout,
                "writer_timeout_seconds": self._writer_timeout,
                "maintenance_timeout_seconds": self._maintenance_timeout,
                "sdk_max_retries": _SDK_MAX_RETRIES,
                "closed": self._closed,
                "request_resources": self._request_runner.snapshot(),
            }
        )
        return payload

    def request_resources(self) -> dict[str, int]:
        """Return the declared hosted-request bound and current occupancy."""

        return self._request_runner.snapshot()

    def _sanitize_httpx_request(self, request: Any) -> None:
        """Apply endpoint and credential policy immediately before SDK send."""

        policy = require_safe_endpoint(
            str(request.url),
            allow_insecure=self._allow_insecure_endpoint,
        )
        if policy.allow_credentials:
            return
        for header_name in list(request.headers):
            if is_credential_key(str(header_name)):
                request.headers.pop(header_name, None)

    def _operation_budget(self, operation: str) -> float:
        if operation == "query":
            return self._query_timeout
        if operation == "maintenance":
            return self._maintenance_timeout
        return self._writer_timeout

    def _transport_timeout(self, remaining: float | None = None) -> Any:
        connect = self._connect_timeout
        read = self._read_timeout
        write = self._write_timeout
        pool = self._pool_timeout
        if remaining is not None:
            capped = max(_MIN_TRANSPORT_TIMEOUT_SECONDS, float(remaining))
            connect = min(connect, capped)
            read = min(read, capped)
            write = min(write, capped)
            pool = min(pool, capped)
        timeout_factory = getattr(_httpx, "Timeout", None) if _httpx is not None else None
        if callable(timeout_factory):
            return timeout_factory(connect=connect, read=read, write=write, pool=pool)
        return read

    def close(self) -> None:
        """Terminal lifecycle close. Idempotent; does not reopen or join the worker.

        Stops accepting new hosted requests, drops OpenAI/HTTPX handles on
        this thread, and asks an idle request worker to exit. That is
        ordinary local close, not a per-close helper thread. A stuck SDK
        call cannot be killed; it keeps its plugin-wide permit until the
        vendor call returns. Shutdown can therefore return without waiting
        for that call.
        """

        with self._client_guard:
            self._closed = True
            client = self._client
            http_client = self._http_client
            self._client = None
            self._http_client = None
        self._request_runner.shutdown()
        _close_quietly(client)
        if http_client is not None and http_client is not client:
            _close_quietly(http_client)

    def reset_transport(self) -> None:
        """Drop HTTP/OpenAI handles without ending this embedder.

        Used by connection retry and key rotation. It does not spawn closer
        threads, does not shut the request runner, does not mark the
        embedder closed, and does not change timeout or ``max_retries``.
        A terminally closed embedder stays closed.
        """

        with self._client_guard:
            if self._closed:
                return
            client = self._client
            http_client = self._http_client
            self._client = None
            self._http_client = None
        _close_quietly(client)
        if http_client is not None and http_client is not client:
            _close_quietly(http_client)

    def _http_client_or_raise(self) -> Any:
        if _httpx is None:
            raise RuntimeError("httpx is required for OpenAI-compatible embeddings")
        with self._client_guard:
            if self._closed:
                raise RuntimeError(f"{self.provider} embedder is closed")
            if self._http_client is None:
                client_factory = DefaultHttpxClient or _httpx.Client
                kwargs: dict[str, Any] = {
                    "follow_redirects": False,
                    "event_hooks": {"request": [self._sanitize_httpx_request]},
                    "timeout": self._transport_timeout(),
                }
                try:
                    self._http_client = client_factory(**kwargs)
                except TypeError as exc:
                    raise _unsupported_sdk_contract(self.provider, "HTTP client") from exc
            return self._http_client

    def _client_or_raise(self):
        if not self.is_available() or OpenAI is None:
            raise RuntimeError(f"{self.provider} embedder is not configured")
        with self._client_guard:
            if self._closed:
                raise RuntimeError(f"{self.provider} embedder is closed")
            if self._client is None:
                kwargs: dict[str, Any] = {
                    "api_key": self._api_keys[self._active_key_index],
                    "base_url": self._base_url,
                    "http_client": self._http_client_or_raise(),
                    "max_retries": _SDK_MAX_RETRIES,
                    "timeout": self._transport_timeout(),
                }
                try:
                    self._client = OpenAI(**kwargs)
                except TypeError as exc:
                    raise _unsupported_sdk_contract(self.provider, "OpenAI client") from exc
            return self._client

    def _rotate_client_after_failure(self) -> bool:
        if len(self._api_keys) <= 1:
            return False
        self._active_key_index = (self._active_key_index + 1) % len(self._api_keys)
        self.reset_transport()
        return True

    @staticmethod
    def _is_connection_error(exc: Exception) -> bool:
        """Classify transport failures by type, never by message substrings."""

        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, (ConnectionError, TimeoutError)):
                return True
            error_type = type(current)
            module = str(error_type.__module__ or "").casefold()
            if (
                error_type.__name__ in _CONNECTION_EXCEPTION_NAMES
                and module.startswith(_CONNECTION_EXCEPTION_MODULE_PREFIXES)
            ):
                return True
            current = current.__cause__ or current.__context__
        return False

    def _ordered_embedding_response_rows(
        self,
        rows: list[Any],
        *,
        expected_count: int,
    ) -> list[Any]:
        """Restore input order when a compatible API supplies row indices.

        Some OpenAI-compatible servers omit ``index`` entirely; those retain
        their historical response-order behavior. Partially indexed or invalid
        responses fail closed because silent query/vector misalignment corrupts
        multi-query retrieval.
        """

        indices = [getattr(row, "index", None) for row in rows]
        if all(index is None for index in indices):
            return rows
        if any(index is None for index in indices):
            raise RuntimeError(
                f"{self.provider} embedding response mixes indexed and unindexed rows"
            )
        by_index: dict[int, Any] = {}
        for row, raw_index in zip(rows, indices, strict=True):
            if isinstance(raw_index, bool) or not isinstance(raw_index, int):
                raise RuntimeError(
                    f"{self.provider} embedding response contains a non-integer index"
                )
            if raw_index < 0 or raw_index >= expected_count or raw_index in by_index:
                raise RuntimeError(
                    f"{self.provider} embedding response contains an invalid or duplicate index"
                )
            by_index[raw_index] = row
        if len(by_index) != expected_count:
            raise RuntimeError(
                f"{self.provider} embedding response indices do not cover the input batch"
            )
        return [by_index[index] for index in range(expected_count)]

    def _remaining_budget(self, deadline: float) -> float:
        return deadline - time.monotonic()

    def _raise_if_budget_exhausted(
        self,
        deadline: float,
        *,
        operation: str,
        cause: Exception | None = None,
    ) -> float:
        remaining = self._remaining_budget(deadline)
        if remaining > 0.0:
            return remaining
        error = TimeoutError(
            f"{self.provider} {operation} embedding exceeded the "
            f"{self._operation_budget(operation):g}s operation budget"
        )
        if cause is not None:
            raise error from cause
        raise error

    def _create_embeddings(
        self,
        client: Any,
        batch: list[str],
        *,
        timeout: Any,
    ) -> Any:
        request: dict[str, Any] = {
            "model": self.model,
            "input": batch,
            "encoding_format": "float",
            "timeout": timeout,
        }
        if self._request_dimensions:
            request["dimensions"] = self.dimensions
        try:
            return client.embeddings.create(**request)
        except TypeError as exc:
            raise _unsupported_sdk_contract(self.provider, "embeddings.create") from exc

    def _call_with_deadline(self, fn: Any, *, deadline: float, operation: str) -> Any:
        remaining = self._raise_if_budget_exhausted(deadline, operation=operation)
        try:
            return self._request_runner.run(fn, timeout=remaining)
        except EmbedderRequestClosedError:
            raise RuntimeError(f"{self.provider} embedder is closed") from None
        except InFlightEmbedderRequestError:
            raise TimeoutError(
                f"{self.provider} {operation} embedding rejected because "
                "a request is already in flight"
            ) from None
        except HostedEmbedderWorkerLimitError:
            raise TimeoutError(
                f"{self.provider} {operation} embedding rejected because "
                "the plugin hosted-embedding worker limit is exhausted"
            ) from None
        except EmbedderRequestDeadlineError:
            # Drop half-open handles so a later recovered call builds a fresh
            # client. Do not terminally close: the same embedder may run again
            # after the occupied slot clears.
            self.reset_transport()
            raise TimeoutError(
                f"{self.provider} {operation} embedding exceeded the "
                f"{self._operation_budget(operation):g}s operation budget"
            ) from None

    def _embed_with_prefix(
        self,
        texts: Iterable[str],
        *,
        prefix: str,
        operation: str = "writer",
    ) -> list[list[float]]:
        resolved_operation = _normalize_embed_operation(operation)
        items = [clean_text(f"{prefix}{clean_text(text)}") or " " for text in texts]
        if not items:
            return []
        deadline = time.monotonic() + self._operation_budget(resolved_operation)
        vectors: list[list[float]] = []
        batch_size = 100
        for start in range(0, len(items), batch_size):
            remaining = self._raise_if_budget_exhausted(
                deadline, operation=resolved_operation
            )
            batch = items[start : start + batch_size]
            response = None
            key_count = max(1, len(self._api_keys))
            max_attempts = len(self._connection_retry_delays) + 1
            for retry_index in range(max_attempts):
                remaining = self._raise_if_budget_exhausted(
                    deadline, operation=resolved_operation
                )
                connection_error: Exception | None = None
                for key_attempt in range(key_count):
                    try:
                        client = self._client_or_raise()
                        attempt_timeout = self._transport_timeout(remaining)
                        response = self._call_with_deadline(
                            partial(
                                self._create_embeddings,
                                client,
                                batch,
                                timeout=attempt_timeout,
                            ),
                            deadline=deadline,
                            operation=resolved_operation,
                        )
                        break
                    except UnsupportedEmbedderSdkError:
                        raise
                    except UnsafeEndpointError:
                        raise
                    except TimeoutError:
                        raise
                    except Exception as exc:
                        if self._is_connection_error(exc):
                            # Transport failures are endpoint-wide, not key-specific.
                            # Recreate the client before the next bounded retry so an
                            # on-demand local server can finish starting cleanly.
                            connection_error = exc
                            self.reset_transport()
                            break
                        if key_attempt + 1 >= key_count:
                            raise
                        self._rotate_client_after_failure()
                if response is not None:
                    break
                assert connection_error is not None
                if retry_index + 1 >= max_attempts:
                    raise connection_error
                delay = self._connection_retry_delays[retry_index]
                remaining = self._remaining_budget(deadline)
                if remaining <= delay:
                    raise TimeoutError(
                        f"{self.provider} {resolved_operation} embedding exceeded the "
                        f"{self._operation_budget(resolved_operation):g}s operation budget"
                    ) from connection_error
                time.sleep(delay)
            if response is None:
                raise RuntimeError(
                    f"{self.provider} embedding request produced no response"
                )
            response_rows = self._ordered_embedding_response_rows(
                list(response.data),
                expected_count=len(batch),
            )
            vectors.extend(
                validate_embedding_batch(
                    response_rows,
                    expected_count=len(batch),
                    expected_dimensions=self.dimensions,
                    provider=self.provider,
                )
            )
        return [
            vector
            for _text, vector in zip_embedding_rows(
                items, vectors, provider=self.provider
            )
        ]

    def embed_texts(self, texts: Iterable[str]) -> list[list[float]]:
        return self._embed_with_prefix(
            texts, prefix=self._document_prefix, operation="writer"
        )

    def embed_maintenance(self, texts: Iterable[str]) -> list[list[float]]:
        return self._embed_with_prefix(
            texts, prefix=self._document_prefix, operation="maintenance"
        )

    def embed_query(self, text: str) -> list[float]:
        return self.embed_queries([text])[0]

    def embed_queries(self, texts: Iterable[str]) -> list[list[float]]:
        return self._embed_with_prefix(
            texts, prefix=self._query_prefix, operation="query"
        )


class OpenAIEmbedder(OpenAICompatibleEmbedder):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(provider="openai", **kwargs)


class SentenceTransformersEmbedder(BaseEmbedder):
    """Local sentence-transformers embedder adapter.

    It keeps optional heavy dependencies isolated so deployments without local embedding packages can still use SQLite-only mode."""
    def __init__(
        self,
        *,
        provider: str = "sentence-transformers",
        model: str = "sentence-transformers/all-MiniLM-L6-v2",
        dimensions: int | None = None,
        device: str | None = None,
        normalize: bool = True,
    ) -> None:
        self._device = str(device or "").strip() or None
        self._normalize = bool(normalize)
        known_dimensions = _known_dimensions(model, 0)
        resolved_dimensions = int(known_dimensions or dimensions or 384)
        self._load_error = ""
        self._model_obj = None
        super().__init__(provider=provider, dimensions=resolved_dimensions, model=model)

    def _load_model(self, model: str):
        key = (model, self._device)
        with _SENTENCE_TRANSFORMER_CACHE_GUARD:
            cached = _SENTENCE_TRANSFORMER_CACHE.get(key)
            if cached is not None:
                return cached
            flight = _SENTENCE_TRANSFORMER_LOAD_FLIGHTS.get(key)
            leader = flight is None
            if flight is None:
                flight = Future()
                _SENTENCE_TRANSFORMER_LOAD_FLIGHTS[key] = flight
        if not leader:
            return flight.result()

        try:
            transformer_class = _resolve_sentence_transformer()
            if transformer_class is None:
                raise RuntimeError("sentence-transformers is not installed")
            kwargs: dict[str, Any] = {}
            if self._device:
                kwargs["device"] = self._device
            instance = transformer_class(model, **kwargs)
        except Exception as exc:
            detail = " ".join(sanitize_report_text(exc).split())[:300]
            safe_message = (
                f"{type(exc).__name__}: {detail}"
                if detail
                else f"{type(exc).__name__}: model load failed"
            )
            safe_error = _SanitizedModelLoadError(safe_message)
            flight.set_exception(safe_error)
            with _SENTENCE_TRANSFORMER_CACHE_GUARD:
                if _SENTENCE_TRANSFORMER_LOAD_FLIGHTS.get(key) is flight:
                    del _SENTENCE_TRANSFORMER_LOAD_FLIGHTS[key]
            raise safe_error from None

        with _SENTENCE_TRANSFORMER_CACHE_GUARD:
            _SENTENCE_TRANSFORMER_CACHE[key] = instance
            if _SENTENCE_TRANSFORMER_LOAD_FLIGHTS.get(key) is flight:
                del _SENTENCE_TRANSFORMER_LOAD_FLIGHTS[key]
        flight.set_result(instance)
        return instance

    def is_available(self) -> bool:
        return _resolve_sentence_transformer() is not None and not self._load_error

    def describe(self) -> dict[str, Any]:
        payload = super().describe()
        payload["normalize"] = self._normalize
        if self._device:
            payload["device"] = self._device
        if self._load_error:
            payload["load_error"] = self._load_error
        return payload

    def _model_or_raise(self):
        if self._model_obj is None:
            if self._load_error:
                raise RuntimeError(self._load_error)
            try:
                self._model_obj = self._load_model(self.model)
            except Exception as exc:
                if isinstance(exc, _SanitizedModelLoadError):
                    self._load_error = str(exc)
                else:
                    detail = " ".join(sanitize_report_text(exc).split())[:300]
                    self._load_error = (
                        f"{type(exc).__name__}: {detail}"
                        if detail
                        else f"{type(exc).__name__}: model load failed"
                    )
                # The wrapper is safe for diagnostics; the original exception
                # can contain private cache paths or credential-bearing model
                # URLs, so do not retain it as a traceback cause.
                raise RuntimeError(self._load_error) from None
            try:
                if hasattr(self._model_obj, "get_embedding_dimension"):
                    dims = int(self._model_obj.get_embedding_dimension() or 0)
                elif hasattr(self._model_obj, "get_sentence_embedding_dimension"):
                    dims = int(self._model_obj.get_sentence_embedding_dimension() or 0)
                else:
                    dims = 0
                if dims > 0:
                    self.info.dimensions = dims
            except Exception:
                pass
        return self._model_obj

    def probe_readiness(self) -> None:
        """Load the local model so bootstrap records its real usable identity."""

        self._model_or_raise()

    def embed_texts(self, texts: Iterable[str]) -> list[list[float]]:
        items = [clean_text(text) or " " for text in texts]
        if not items:
            return []
        model = self._model_or_raise()
        vectors = model.encode(items, normalize_embeddings=self._normalize, convert_to_numpy=True)
        return validate_embedding_batch(
            vectors,
            expected_count=len(items),
            expected_dimensions=self.dimensions,
            provider=self.provider,
        )


class MiniMaxEmbedder(BaseEmbedder):
    """Embedder for MiniMax (MiniMax) embo-01 embeddings.

    MiniMax exposes a non-OpenAI-compatible endpoint:
        POST {base_url}/v1/embeddings
        body  = {"model": "embo-01", "texts": [...], "type": "db" | "query"}
        reply = {"vectors": [[...], ...], "base_resp": {...}}

    The OpenAI SDK cannot talk to this shape (``input`` is singular, response
    uses ``data[].embedding``), so this class talks to the API directly with
    ``urllib``. Document/indexing calls use ``db`` while vector-search query
    calls use ``query``. Both request shapes return the same ``vectors`` array.
    """

    _DEFAULT_BASE_URL = "https://api.minimaxi.com"

    def __init__(
        self,
        *,
        provider: str = "minimax",
        model: str = "embo-01",
        api_key: Any = None,
        api_key_env: Any = None,
        base_url: Any = None,
        base_url_env: Any = None,
        request_type: str | None = None,
        document_type: str = "db",
        query_type: str = "query",
        group_id: Any = None,
        group_id_env: Any = None,
        timeout: Any = None,
        connect_timeout_seconds: Any = None,
        read_timeout_seconds: Any = None,
        write_timeout_seconds: Any = None,
        pool_timeout_seconds: Any = None,
        query_timeout_seconds: Any = None,
        writer_timeout_seconds: Any = None,
        maintenance_timeout_seconds: Any = None,
        dimensions: int | None = None,
        allow_insecure_endpoint: bool = False,
    ) -> None:
        resolved_dimensions = int(dimensions or _known_dimensions(model, 1536) or 1536)
        super().__init__(provider=provider, dimensions=resolved_dimensions, model=model)
        self._api_keys = _resolve_api_keys(api_key, api_key_env)
        self._base_url = (
            _configured_embedder_base_url(provider, base_url, base_url_env)
            or self._DEFAULT_BASE_URL
        ).rstrip("/")
        require_safe_endpoint(
            self._base_url,
            allow_insecure=allow_insecure_endpoint,
        )
        self._allow_insecure_endpoint = allow_insecure_endpoint
        self._document_type = self._coerce_request_type(request_type or document_type, "db")
        self._query_type = self._coerce_request_type(query_type, "query")
        self._group_id = _resolve_optional_value(group_id, group_id_env)
        legacy_timeout = _coerce_timeout_seconds(
            timeout,
            _DEFAULT_WRITER_TIMEOUT_SECONDS,
            name="timeout",
        )
        self._connect_timeout = _coerce_timeout_seconds(
            connect_timeout_seconds,
            _DEFAULT_CONNECT_TIMEOUT_SECONDS,
            name="connect_timeout_seconds",
        )
        self._read_timeout = _coerce_timeout_seconds(
            read_timeout_seconds,
            legacy_timeout,
            name="read_timeout_seconds",
        )
        self._write_timeout = _coerce_timeout_seconds(
            write_timeout_seconds,
            _DEFAULT_WRITE_TIMEOUT_SECONDS,
            name="write_timeout_seconds",
        )
        self._pool_timeout = _coerce_timeout_seconds(
            pool_timeout_seconds,
            _DEFAULT_POOL_TIMEOUT_SECONDS,
            name="pool_timeout_seconds",
        )
        self._query_timeout = _coerce_timeout_seconds(
            query_timeout_seconds,
            _DEFAULT_QUERY_TIMEOUT_SECONDS,
            name="query_timeout_seconds",
        )
        self._writer_timeout = _coerce_timeout_seconds(
            writer_timeout_seconds,
            legacy_timeout,
            name="writer_timeout_seconds",
        )
        self._maintenance_timeout = _coerce_timeout_seconds(
            maintenance_timeout_seconds,
            _DEFAULT_MAINTENANCE_TIMEOUT_SECONDS,
            name="maintenance_timeout_seconds",
        )
        self._timeout = self._read_timeout
        self._active_key_index = 0
        self._closed = False
        self._request_runner = BoundedEmbedderRequestRunner()

    @staticmethod
    def _coerce_request_type(value: Any, default: str) -> str:
        request_type = str(value or default).strip().lower()
        return request_type if request_type in {"db", "query"} else default

    def is_available(self) -> bool:
        return bool(self._api_keys)

    def describe(self) -> dict[str, Any]:
        payload = super().describe()
        payload["base_url"] = self._base_url
        payload["document_type"] = self._document_type
        payload["query_type"] = self._query_type
        if self._group_id:
            payload["group_id_configured"] = True
        if self._allow_insecure_endpoint:
            payload["allow_insecure_endpoint"] = True
        payload.update(
            {
                "connect_timeout_seconds": self._connect_timeout,
                "read_timeout_seconds": self._read_timeout,
                "write_timeout_seconds": self._write_timeout,
                "pool_timeout_seconds": self._pool_timeout,
                "query_timeout_seconds": self._query_timeout,
                "writer_timeout_seconds": self._writer_timeout,
                "maintenance_timeout_seconds": self._maintenance_timeout,
                "closed": self._closed,
                "request_resources": self._request_runner.snapshot(),
            }
        )
        return payload

    def request_resources(self) -> dict[str, int]:
        """Return the declared hosted-request bound and current occupancy."""

        return self._request_runner.snapshot()

    def close(self) -> None:
        """Terminally stop new MiniMax requests without joining a stuck call."""

        if self._closed:
            return
        self._closed = True
        self._request_runner.shutdown()

    def _rotate_key_after_failure(self) -> bool:
        if len(self._api_keys) <= 1:
            return False
        self._active_key_index = (self._active_key_index + 1) % len(self._api_keys)
        return True

    def _operation_budget(self, operation: str) -> float:
        if operation == "query":
            return self._query_timeout
        if operation == "maintenance":
            return self._maintenance_timeout
        return self._writer_timeout

    def _request_timeout(self, remaining: float) -> float:
        return min(self._read_timeout, max(_MIN_TRANSPORT_TIMEOUT_SECONDS, remaining))

    def _call_with_deadline(self, fn: Any, *, deadline: float, operation: str) -> Any:
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise TimeoutError(
                f"{self.provider} {operation} embedding exceeded the "
                f"{self._operation_budget(operation):g}s operation budget"
            )
        try:
            return self._request_runner.run(fn, timeout=remaining)
        except EmbedderRequestClosedError:
            raise RuntimeError(f"{self.provider} embedder is closed") from None
        except InFlightEmbedderRequestError:
            raise TimeoutError(
                f"{self.provider} {operation} embedding rejected because "
                "a request is already in flight"
            ) from None
        except HostedEmbedderWorkerLimitError:
            raise TimeoutError(
                f"{self.provider} {operation} embedding rejected because "
                "the plugin hosted-embedding worker limit is exhausted"
            ) from None
        except EmbedderRequestDeadlineError:
            raise TimeoutError(
                f"{self.provider} {operation} embedding exceeded the "
                f"{self._operation_budget(operation):g}s operation budget"
            ) from None

    def _request_payload(
        self,
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> Any:
        """Perform and fully consume one urllib response inside the hard deadline."""

        with safe_urlopen(
            request,
            timeout=timeout,
            allow_insecure=self._allow_insecure_endpoint,
        ) as resp:
            raw = resp.read().decode("utf-8")
        return _json_lib.loads(raw)

    def _post_embeddings(
        self,
        texts: list[str],
        *,
        request_type: str,
        operation: str = "writer",
        deadline: float | None = None,
    ) -> list[list[float]]:
        resolved_operation = _normalize_embed_operation(operation)
        resolved_deadline = deadline if deadline is not None else (
            time.monotonic() + self._operation_budget(resolved_operation)
        )
        remaining = resolved_deadline - time.monotonic()
        if remaining <= 0.0:
            raise TimeoutError(
                f"{self.provider} {resolved_operation} embedding exceeded the "
                f"{self._operation_budget(resolved_operation):g}s operation budget"
            )
        url = f"{self._base_url}/v1/embeddings"
        if self._group_id:
            url = f"{url}?{urllib.parse.urlencode({'GroupId': self._group_id})}"
        body = _json_lib.dumps(
            {"model": self.model, "texts": texts, "type": request_type}
        ).encode("utf-8")
        last_error: Exception | None = None
        for _ in range(max(1, len(self._api_keys))):
            remaining = resolved_deadline - time.monotonic()
            if remaining <= 0.0:
                raise TimeoutError(
                    f"{self.provider} {resolved_operation} embedding exceeded the "
                    f"{self._operation_budget(resolved_operation):g}s operation budget"
                )
            req = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_keys[self._active_key_index]}",
                },
            )
            try:
                payload = self._call_with_deadline(
                    partial(
                        self._request_payload,
                        req,
                        timeout=self._request_timeout(remaining),
                    ),
                    deadline=resolved_deadline,
                    operation=resolved_operation,
                )
            except UnsafeEndpointError:
                raise
            except TimeoutError:
                raise
            except urllib.error.HTTPError as exc:  # pragma: no cover - network
                last_error = exc
                if not self._rotate_key_after_failure():
                    raise
                continue
            except Exception as exc:  # pragma: no cover - network
                last_error = exc
                if not self._rotate_key_after_failure():
                    raise
                continue
            vectors = payload.get("vectors") if isinstance(payload, dict) else None
            if vectors is None:
                raise RuntimeError(
                    f"minimax embeddings response missing 'vectors': {payload!r}"
                )
            return validate_embedding_batch(
                vectors,
                expected_count=len(texts),
                expected_dimensions=self.dimensions,
                provider=self.provider,
            )
        assert last_error is not None
        raise last_error

    def _embed_documents(self, texts: Iterable[str], *, operation: str) -> list[list[float]]:
        items = [clean_text(text) or " " for text in texts]
        if not items:
            return []
        deadline = time.monotonic() + self._operation_budget(operation)
        vectors: list[list[float]] = []
        # MiniMax endpoint accepts batches comfortably up to a few hundred
        # items; keep chunks conservative to stay well under request limits.
        batch_size = 64
        for start in range(0, len(items), batch_size):
            batch = items[start : start + batch_size]
            vectors.extend(
                self._post_embeddings(
                    batch,
                    request_type=self._document_type,
                    operation=operation,
                    deadline=deadline,
                )
            )
        return [
            vector
            for _text, vector in zip_embedding_rows(items, vectors, provider=self.provider)
        ]

    def embed_texts(self, texts: Iterable[str]) -> list[list[float]]:
        return self._embed_documents(texts, operation="writer")

    def embed_maintenance(self, texts: Iterable[str]) -> list[list[float]]:
        return self._embed_documents(texts, operation="maintenance")

    def embed_query(self, text: str) -> list[float]:
        item = clean_text(text) or " "
        vectors = self._post_embeddings(
            [item], request_type=self._query_type, operation="query"
        )
        return vectors[0]


def build_embedder(config: dict[str, Any]) -> BaseEmbedder:
    raw = dict(config or {})
    provider = str(raw.get("provider") or "local-hash").strip().lower()
    dimensions = int(raw.get("dimensions") or 0)
    model = str(raw.get("model") or "").strip()
    resolved_base_url = resolve_embedder_base_url(raw)

    if provider == "local-debug":
        return LocalDebugEmbedder(dimensions=dimensions or 16, model=model or "debug-hash-v1")

    if provider in {"openai", "openai-compatible", "generic-openai-compatible", "gemini-openai-compatible"}:
        embedder_cls = OpenAIEmbedder if provider == "openai" else OpenAICompatibleEmbedder
        return embedder_cls(
            model=model or "text-embedding-3-small",
            api_key=raw.get("api_key"),
            api_key_env=raw.get("api_key_env"),
            base_url=resolved_base_url,
            dimensions=dimensions or None,
            request_dimensions=_config_bool_value(raw.get("request_dimensions"), False),
            document_prefix=str(raw.get("document_prefix") or ""),
            query_prefix=str(raw.get("query_prefix") or ""),
            prompt_profile=str(raw.get("prompt_profile") or "default-v1"),
            connection_retry_delays=raw.get("connection_retry_delays"),
            allow_insecure_endpoint=raw.get("allow_insecure_endpoint", False),
            connect_timeout_seconds=raw.get("connect_timeout_seconds"),
            read_timeout_seconds=raw.get("read_timeout_seconds"),
            write_timeout_seconds=raw.get("write_timeout_seconds"),
            pool_timeout_seconds=raw.get("pool_timeout_seconds"),
            query_timeout_seconds=raw.get("query_timeout_seconds"),
            writer_timeout_seconds=raw.get("writer_timeout_seconds"),
            maintenance_timeout_seconds=raw.get("maintenance_timeout_seconds"),
        )

    if provider in {"sentence-transformers", "local-model", "local-embedding", "huggingface"}:
        return SentenceTransformersEmbedder(
            provider="sentence-transformers",
            model=model or "sentence-transformers/all-MiniLM-L6-v2",
            dimensions=dimensions or None,
            device=raw.get("device"),
            normalize=_config_bool_value(raw.get("normalize"), True),
        )

    if provider in {"minimax"}:
        return MiniMaxEmbedder(
            provider="minimax",
            model=model or "embo-01",
            api_key=raw.get("api_key"),
            api_key_env=raw.get("api_key_env"),
            base_url=resolved_base_url,
            request_type=raw.get("request_type"),
            document_type=str(raw.get("document_type") or raw.get("embed_type_db") or "db"),
            query_type=str(raw.get("query_type") or raw.get("embed_type_query") or "query"),
            group_id=raw.get("group_id"),
            group_id_env=raw.get("group_id_env"),
            timeout=raw.get("timeout"),
            connect_timeout_seconds=raw.get("connect_timeout_seconds"),
            read_timeout_seconds=raw.get("read_timeout_seconds"),
            write_timeout_seconds=raw.get("write_timeout_seconds"),
            pool_timeout_seconds=raw.get("pool_timeout_seconds"),
            query_timeout_seconds=raw.get("query_timeout_seconds"),
            writer_timeout_seconds=raw.get("writer_timeout_seconds"),
            maintenance_timeout_seconds=raw.get("maintenance_timeout_seconds"),
            dimensions=dimensions or None,
            allow_insecure_endpoint=raw.get("allow_insecure_endpoint", False),
        )

    return LocalHashEmbedder(provider="local-hash", dimensions=dimensions or 256, model=model or "hash-v1")
