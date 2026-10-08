"""Embedding requests: the route's configuration, the request bodies, the response's vectors and usage, and the
adapter that sends them through the metered transport (``models``)."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Any

from ..core.recall_policy import EMBEDDING_DIALECTS, EMBEDDING_SPACE, build_embedding_space, encode_embedding_text
from ..core.source_records import StoredSource
from .model_budget import AuxiliaryBudgetLedger
from .models import (
    RESERVE_ENVELOPE_MARGIN,
    AuxiliaryModelError,
    HttpsTransport,
    HttpTransport,
    json_bytes,
    load_credential,
    metered_post,
    reject_secrets,
    seconds_left,
    validate_credential_env_name,
    validate_timeout_seconds,
)

MAX_EMBED_RESPONSE_BYTES = 16 * 1024 * 1024


#: Embedding requests one group may have in flight.  A document request spawns its
#: own bounded HTTP helper and shares no state, so this is threads waiting on
#: sockets; the ledger reserves and settles each on its own connection.  It is the
#: last serial cost in a pass: with a hundred documents per request at 3.8s each,
#: a pass of a thousand spent 38 of its 55 seconds waiting for one request at a
#: time, while the provider allows three thousand requests a minute and the pass
#: was making ten.
EMBED_REQUEST_CONCURRENCY = 4


#: Documents one embedding request may carry, measured against the live provider
#: rather than assumed: 32 texts answered in 2.6s, 64 in 3.2s, 100 in 3.8s, and
#: 250 was refused with HTTP 400.  A hundred 3072-wide vectors is about 4 MB of
#: response, well inside the cap above, and it is the difference between seven
#: requests for a pass of two hundred sources and two.
MAX_EMBED_BATCH = 100


EMBED_RESERVE_FLOOR = 8192


def build_gemini_embed_body(
    encoded_text: str | Sequence[str], *, model: str | None = None, dimensions: int | None = None
) -> bytes:
    """One ``batchEmbedContents`` request for one or many already-encoded texts.

    The endpoint is a batch endpoint and always was; sending arrays of one is
    what made a vector rebuild cost one HTTP request per source.
    """
    model = model or EMBEDDING_SPACE["model"]
    texts = [encoded_text] if type(encoded_text) is str else list(encoded_text)
    if not texts or any(type(text) is not str for text in texts):
        raise AuxiliaryModelError("unsupported_request_shape")
    body = {
        "requests": [
            {
                "model": f"models/{model}",
                "content": {"parts": [{"text": text}]},
                "embedContentConfig": {
                    "outputDimensionality": dimensions or EMBEDDING_SPACE["dimensions"],
                    "autoTruncate": False,
                },
            }
            for text in texts
        ]
    }
    return json_bytes(body)


def request_chunks(texts: Sequence[str], *, body: Callable[[Sequence[str]], bytes], limit: int) -> list[list[str]]:
    """Consecutive requests of at most ``MAX_EMBED_BATCH`` texts whose body stays within ``limit`` bytes.

    The ledger refuses a body over its ``max_request_bytes`` before it is sent, as
    ``budget_unavailable``, and the worker defers the whole group an hour.  On the pilot's shared
    store the last 6,000 sources of a rebuild were long ones: a hundred of them made about 600 KB
    against the 128 KB limit, and every pass for eight hours was refused without one request
    leaving.  A text whose own body passes the limit still goes, alone, so the refusal is its own.
    """
    chunks: list[list[str]] = []
    chunk: list[str] = []
    for text in texts:
        if chunk and (len(chunk) == MAX_EMBED_BATCH or len(body([*chunk, text])) > limit):
            chunks.append(chunk)
            chunk = []
        chunk.append(text)
    if chunk:
        chunks.append(chunk)
    return chunks


def build_openai_embed_body(
    encoded_text: str | Sequence[str], *, model: str, dimensions: int, dimensions_field: str = "dimensions"
) -> bytes:
    """The /v1/embeddings request shape MiniMax, Qwen and OpenAI all accept.

    The width is sent because the space digest commits to one: a provider that
    silently returned a different width would produce vectors the store cannot
    compare, and the length check on the response catches it.  Voyage names
    the field ``output_dimension``, so the route may name it.
    """
    texts = [encoded_text] if type(encoded_text) is str else list(encoded_text)
    if not texts or any(type(text) is not str for text in texts):
        raise AuxiliaryModelError("unsupported_request_shape")
    return json_bytes({"model": model, "input": texts, dimensions_field: dimensions})


def _embedding_usage(payload: Mapping[str, Any], *, dialect: str) -> dict[str, int] | None:
    if dialect == "gemini":
        metadata, key = payload.get("usageMetadata"), "promptTokenCount"
    else:
        metadata, key = payload.get("usage"), "prompt_tokens"
    if isinstance(metadata, dict) and type(metadata.get(key)) is int:
        return {"promptTokenCount": metadata[key]}
    return None


def _embedding_vector(payload: Mapping[str, Any], *, dialect: str, dimensions: int) -> tuple[float, ...]:
    if dialect == "gemini":
        embeddings = payload.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != 1:
            raise AuxiliaryModelError("unsupported_response_shape")
        row = embeddings[0]
        if not isinstance(row, dict) or set(row) != {"values"}:
            raise AuxiliaryModelError("unsupported_response_shape")
        return validate_embedding_vector(row["values"], dimensions=dimensions)
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise AuxiliaryModelError("unsupported_response_shape")
    return validate_embedding_vector(data[0].get("embedding"), dimensions=dimensions)


def _embedding_vectors(
    payload: Mapping[str, Any], *, dialect: str, dimensions: int, count: int
) -> tuple[tuple[float, ...], ...]:
    """Exactly ``count`` vectors, in the order the texts were sent.

    A provider that returns a different number has not answered this request:
    the vectors could not be matched to their sources, and a vector written
    against the wrong source is worse than no vector at all.
    """
    if dialect == "gemini":
        rows = payload.get("embeddings")
        if not isinstance(rows, list) or len(rows) != count:
            raise AuxiliaryModelError("unsupported_response_shape")
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"values"}:
                raise AuxiliaryModelError("unsupported_response_shape")
        return tuple(validate_embedding_vector(row["values"], dimensions=dimensions) for row in rows)
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != count or any(not isinstance(row, dict) for row in data):
        raise AuxiliaryModelError("unsupported_response_shape")
    # OpenAI's shape carries the position of each vector; honour it when it is
    # there rather than trusting the order the list happens to have.
    if all(type(row.get("index")) is int for row in data):
        if sorted(row["index"] for row in data) != list(range(count)):
            raise AuxiliaryModelError("unsupported_response_shape")
        data = sorted(data, key=lambda row: row["index"])
    return tuple(validate_embedding_vector(row.get("embedding"), dimensions=dimensions) for row in data)


def parse_embedding_response(
    payload: object, *, dialect: str, dimensions: int
) -> tuple[tuple[float, ...], dict[str, int] | None]:
    """Read one vector, and any usage the provider reported, from a response.

    The two dialects differ only here and in the request body; reservation,
    transport, deadlines and error mapping are shared.
    """
    if not isinstance(payload, dict):
        raise AuxiliaryModelError("unsupported_response_shape")
    usage = _embedding_usage(payload, dialect=dialect)
    return _embedding_vector(payload, dialect=dialect, dimensions=dimensions), usage


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


#: A JSON request field name a provider could accept.
_REQUEST_FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


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
    #: The request field the ``openai`` dialect sends the width in.  Voyage's
    #: /v1/embeddings is OpenAI-shaped in every other respect but calls it
    #: ``output_dimension`` and refuses ``dimensions`` outright; a request
    #: that omits the width silently gets the model's default geometry, so the
    #: name is the only lever.  A wire detail, not a geometry: it does not enter
    #: the space digest, and the response length is still checked.
    dimensions_field: str = "dimensions"

    def __post_init__(self) -> None:
        validate_credential_env_name(self.credential_env)
        stated = [self.model, self.endpoint, self.dimensions, self.dialect]
        if any(value is not None for value in stated) and any(value is None for value in stated):
            # Half a descriptor would silently mix a new model with the default
            # dimensionality or dialect, and the digest would not reveal it.
            raise ValueError("embedding_route_partial_space")
        if self.dialect is not None and self.dialect not in EMBEDDING_DIALECTS:
            raise ValueError("embedding_route_dialect")
        if type(self.dimensions_field) is not str or not _REQUEST_FIELD_RE.fullmatch(self.dimensions_field):
            raise ValueError("embedding_route_dimensions_field")

    def space(self) -> dict:
        """The embedding space this route addresses, defaults included."""
        if self.model is None:
            return dict(EMBEDDING_SPACE)
        return build_embedding_space(
            model=self.model,
            dimensions=self.dimensions,
            endpoint=self.endpoint,
            dialect=self.dialect,
        )

    def wire_dialect(self) -> str:
        return self.dialect or "gemini"


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
        self._dimensions_field = route.dimensions_field
        self._dialect = route.wire_dialect()

    def embed_query(self, text: str, *, remaining_seconds: float) -> Sequence[float]:
        # The whole query first: the request guard sees only what the input bound keeps, so a key that straddled
        # the cut went out in part.  A query is the owner's message as typed, screened by nothing before this.
        reject_secrets(text)
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

    def embed_sources(
        self, sources: Sequence[StoredSource], *, remaining_seconds: float
    ) -> tuple[tuple[float, ...], ...]:
        """One request for many documents, answered in the order they were sent.

        The provider charges per token either way; what a batch saves is the
        request, and a store with a hundred thousand sources is a hundred
        thousand requests to rebuild one at a time.
        """
        encoded = [encode_embedding_text(source.event["content"], kind="document") for source in sources]
        return self.embed_texts(encoded, remaining_seconds=remaining_seconds)

    def embed_texts(self, encoded: Sequence[str], *, remaining_seconds: float) -> tuple[tuple[float, ...], ...]:
        """Embed already-encoded document texts, in as few requests as the provider allows.

        The provider takes ``MAX_EMBED_BATCH`` texts per request and refuses more, so a longer
        group is sent as consecutive full requests rather than refused: what a caller asks for
        is how many documents it has, not how the endpoint is shaped.  Measured against the
        live provider: 32 texts in 2.6s, 100 in 3.8s, 250 refused with HTTP 400.  A request also
        stays within the ledger's ``max_request_bytes`` (``request_chunks``).
        """
        texts = list(encoded)
        if not texts:
            return ()
        deadline = time.monotonic() + validate_timeout_seconds(remaining_seconds)
        chunks = request_chunks(texts, body=self._request_body, limit=self._ledger.policy.max_request_bytes)
        if len(chunks) == 1:
            return tuple(self._embed_many(chunks[0], remaining_seconds=seconds_left(deadline)))

        def request(chunk: list[str]) -> tuple[tuple[float, ...], ...]:
            # Each task reads the clock when it starts, not when it was queued, so
            # a later request is bounded by what is actually left.
            return self._embed_many(chunk, remaining_seconds=seconds_left(deadline))

        vectors: list[tuple[float, ...]] = []
        with ThreadPoolExecutor(
            max_workers=min(EMBED_REQUEST_CONCURRENCY, len(chunks)), thread_name_prefix="scope-recall-embed"
        ) as pool:
            answers = [pool.submit(request, chunk) for chunk in chunks]
            for answer in answers:  # in the order they were asked
                vectors.extend(answer.result())
        return tuple(vectors)

    def embed_text(self, text: str, *, remaining_seconds: float) -> Sequence[float]:
        """Embed already-rendered text as a document.

        Derived objects have no ``event["content"]`` to read, so a claim arrives
        here as the rendered assertion. Same encoding as a source, so both land
        in one comparable space.
        """
        encoded = encode_embedding_text(text, kind="document")
        return self._embed(encoded, remaining_seconds=remaining_seconds)

    def _request_body(self, texts: Sequence[str]) -> bytes:
        """The request body for ``texts`` in this route's dialect."""
        if self._dialect == "gemini":
            return build_gemini_embed_body(texts, model=self._model, dimensions=self._dimensions)
        return build_openai_embed_body(
            texts, model=self._model, dimensions=self._dimensions, dimensions_field=self._dimensions_field
        )

    def _embed_many(self, texts: list[str], *, remaining_seconds: float) -> tuple[tuple[float, ...], ...]:
        """The one-request path, for any number of texts; identical bounds to ``_embed``."""
        deadline = time.monotonic() + validate_timeout_seconds(remaining_seconds)
        for text in texts:
            reject_secrets(text)
        return self._send(
            self._request_body(texts),
            deadline,
            self._transport,
            partial(_embedding_vectors, dialect=self._dialect, dimensions=self._dimensions, count=len(texts)),
        )

    def _embed(
        self, encoded_text: str, *, remaining_seconds: float, transport: HttpTransport | None = None
    ) -> Sequence[float]:
        deadline = time.monotonic() + validate_timeout_seconds(remaining_seconds)
        reject_secrets(encoded_text)
        return self._send(
            self._request_body(encoded_text),
            deadline,
            transport or self._transport,
            partial(_embedding_vector, dialect=self._dialect, dimensions=self._dimensions),
        )

    def _send(self, body: bytes, deadline: float, transport: HttpTransport, read_result):
        """One embedding request, metered; refused when its time is up or the provider is holding calls."""
        if seconds_left(deadline) <= 0:
            raise AuxiliaryModelError("timeout")
        if self._ledger.provider_hold_until(self._model) is not None:
            # The provider refused the calls just before this one; asking again
            # now only adds a refusal (runtime/model_budget.py).
            raise AuxiliaryModelError("provider_hold")
        key = load_credential(self._route.credential_env)
        # Google authenticates with its own header; every OpenAI-compatible
        # provider uses bearer auth.
        auth = {"x-goog-api-key": key} if self._dialect == "gemini" else {"Authorization": f"Bearer {key}"}
        return metered_post(
            ledger=self._ledger,
            settle=self._ledger.finish_embedding,
            model=self._model,
            body=body,
            reserved_input=conservative_embed_reserve(body),
            reserved_output=0,
            deadline=deadline,
            transport=transport,
            endpoint=self._endpoint,
            headers={"Content-Type": "application/json", **auth, "User-Agent": "ScopeRecall-AuxiliaryEmbed/1.1"},
            max_response_bytes=MAX_EMBED_RESPONSE_BYTES,
            read_usage=partial(_embedding_usage, dialect=self._dialect),
            read_result=read_result,
        )
