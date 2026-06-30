"""Embedding provider adapters for local, OpenAI-compatible, and hosted vector backends.

Keep provider quirks isolated here so vector stores and repair scripts only see float vectors with stable dimensions."""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass
from typing import Any, Iterable

from .aliases import canonicalize_alias
from .gating import clean_text, query_tokens

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - optional dependency
    OpenAI = None

try:
    from sentence_transformers import SentenceTransformer
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

    def embed_texts(self, texts: Iterable[str]) -> list[list[float]]:
        raise NotImplementedError


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

    Provider-specific request quirks, including float vector response formats, are contained here rather than spread across vector code."""
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
    ) -> None:
        resolved_dimensions = int(dimensions or _known_dimensions(model, 1536) or 1536)
        super().__init__(provider=provider, dimensions=resolved_dimensions, model=model)
        self._api_keys = _resolve_api_keys(api_key, api_key_env or "OPENAI_API_KEY")
        self._base_url = _resolve_optional_value(base_url, base_url_env or "OPENAI_BASE_URL")
        self._client = None
        self._active_key_index = 0

    def is_available(self) -> bool:
        return bool(OpenAI is not None and self._api_keys)

    def describe(self) -> dict[str, Any]:
        payload = super().describe()
        if self._base_url:
            payload["base_url"] = self._base_url
        return payload

    def _client_or_raise(self):
        if not self.is_available() or OpenAI is None:
            raise RuntimeError(f"{self.provider} embedder is not configured")
        if self._client is None:
            self._client = OpenAI(api_key=self._api_keys[self._active_key_index], base_url=self._base_url)
        return self._client

    def _rotate_client_after_failure(self) -> bool:
        if len(self._api_keys) <= 1:
            return False
        self._active_key_index = (self._active_key_index + 1) % len(self._api_keys)
        self._client = None
        return True

    def embed_texts(self, texts: Iterable[str]) -> list[list[float]]:
        items = [clean_text(text) or " " for text in texts]
        if not items:
            return []
        vectors: list[list[float]] = []
        batch_size = 100
        for start in range(0, len(items), batch_size):
            batch = items[start : start + batch_size]
            response = None
            last_error: Exception | None = None
            for _ in range(max(1, len(self._api_keys))):
                client = self._client_or_raise()
                try:
                    response = client.embeddings.create(model=self.model, input=batch, encoding_format="float")
                    break
                except Exception as exc:
                    last_error = exc
                    if not self._rotate_client_after_failure():
                        raise
            if response is None:
                assert last_error is not None
                raise last_error
            vectors.extend(list(row.embedding) for row in response.data)
        if vectors:
            self.info.dimensions = len(vectors[0])
        return vectors


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
        cached = _SENTENCE_TRANSFORMER_CACHE.get(key)
        if cached is not None:
            return cached
        if SentenceTransformer is None:
            raise RuntimeError("sentence-transformers is not installed")
        kwargs: dict[str, Any] = {}
        if self._device:
            kwargs["device"] = self._device
        instance = SentenceTransformer(model, **kwargs)
        _SENTENCE_TRANSFORMER_CACHE[key] = instance
        return instance

    def is_available(self) -> bool:
        return SentenceTransformer is not None and not self._load_error

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
            self._model_obj = self._load_model(self.model)
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

    def embed_texts(self, texts: Iterable[str]) -> list[list[float]]:
        items = [clean_text(text) or " " for text in texts]
        if not items:
            return []
        model = self._model_or_raise()
        vectors = model.encode(items, normalize_embeddings=self._normalize, convert_to_numpy=True)
        output = [list(map(float, row)) for row in vectors]
        if output:
            self.info.dimensions = len(output[0])
        return output


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
        timeout: float = 30.0,
        dimensions: int | None = None,
    ) -> None:
        resolved_dimensions = int(dimensions or _known_dimensions(model, 1536) or 1536)
        super().__init__(provider=provider, dimensions=resolved_dimensions, model=model)
        self._api_keys = _resolve_api_keys(api_key, api_key_env)
        self._base_url = (
            _resolve_optional_value(base_url, base_url_env)
            or self._DEFAULT_BASE_URL
        ).rstrip("/")
        self._document_type = self._coerce_request_type(request_type or document_type, "db")
        self._query_type = self._coerce_request_type(query_type, "query")
        self._group_id = _resolve_optional_value(group_id, group_id_env)
        self._timeout = float(timeout)
        self._active_key_index = 0

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
        return payload

    def _rotate_key_after_failure(self) -> bool:
        if len(self._api_keys) <= 1:
            return False
        self._active_key_index = (self._active_key_index + 1) % len(self._api_keys)
        return True

    def _post_embeddings(self, texts: list[str], *, request_type: str) -> list[list[float]]:
        url = f"{self._base_url}/v1/embeddings"
        if self._group_id:
            url = f"{url}?{urllib.parse.urlencode({'GroupId': self._group_id})}"
        body = _json_lib.dumps(
            {"model": self.model, "texts": texts, "type": request_type}
        ).encode("utf-8")
        last_error: Exception | None = None
        for _ in range(max(1, len(self._api_keys))):
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
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    raw = resp.read().decode("utf-8")
                payload = _json_lib.loads(raw)
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
            if not vectors:
                raise RuntimeError(
                    f"minimax embeddings response missing 'vectors': {payload!r}"
                )
            return [list(map(float, row)) for row in vectors]
        assert last_error is not None
        raise last_error

    def embed_texts(self, texts: Iterable[str]) -> list[list[float]]:
        items = [clean_text(text) or " " for text in texts]
        if not items:
            return []
        vectors: list[list[float]] = []
        # MiniMax endpoint accepts batches comfortably up to a few hundred
        # items; keep chunks conservative to stay well under request limits.
        batch_size = 64
        for start in range(0, len(items), batch_size):
            batch = items[start : start + batch_size]
            vectors.extend(self._post_embeddings(batch, request_type=self._document_type))
        if vectors:
            self.info.dimensions = len(vectors[0])
        return vectors

    def embed_query(self, text: str) -> list[float]:
        item = clean_text(text) or " "
        vectors = self._post_embeddings([item], request_type=self._query_type)
        if vectors:
            self.info.dimensions = len(vectors[0])
        return vectors[0]


def build_embedder(config: dict[str, Any]) -> BaseEmbedder:
    raw = dict(config or {})
    provider = str(raw.get("provider") or "local-hash").strip().lower()
    dimensions = int(raw.get("dimensions") or 0)
    model = str(raw.get("model") or "").strip()

    if provider == "local-debug":
        return LocalDebugEmbedder(dimensions=dimensions or 16, model=model or "debug-hash-v1")

    if provider in {"openai", "openai-compatible", "generic-openai-compatible", "gemini-openai-compatible"}:
        embedder_cls = OpenAIEmbedder if provider == "openai" else OpenAICompatibleEmbedder
        return embedder_cls(
            model=model or "text-embedding-3-small",
            api_key=raw.get("api_key"),
            api_key_env=raw.get("api_key_env"),
            base_url=raw.get("base_url"),
            base_url_env=raw.get("base_url_env"),
            dimensions=dimensions or None,
        )

    if provider in {"sentence-transformers", "local-model", "local-embedding", "huggingface"}:
        return SentenceTransformersEmbedder(
            provider="sentence-transformers",
            model=model or "sentence-transformers/all-MiniLM-L6-v2",
            dimensions=dimensions or None,
            device=raw.get("device"),
            normalize=bool(raw.get("normalize", True)),
        )

    if provider in {"minimax"}:
        return MiniMaxEmbedder(
            provider="minimax",
            model=model or "embo-01",
            api_key=raw.get("api_key"),
            api_key_env=raw.get("api_key_env"),
            base_url=raw.get("base_url"),
            base_url_env=raw.get("base_url_env"),
            request_type=raw.get("request_type"),
            document_type=str(raw.get("document_type") or raw.get("embed_type_db") or "db"),
            query_type=str(raw.get("query_type") or raw.get("embed_type_query") or "query"),
            group_id=raw.get("group_id"),
            group_id_env=raw.get("group_id_env"),
            timeout=float(raw.get("timeout") or 30.0),
            dimensions=dimensions or None,
        )

    return LocalHashEmbedder(provider="local-hash", dimensions=dimensions or 256, model=model or "hash-v1")
