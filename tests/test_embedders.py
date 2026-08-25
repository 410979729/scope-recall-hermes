"""Tests for embedding adapters, hosted-provider request shapes, and fallback behavior.

They isolate provider quirks such as OpenAI-compatible float vector responses."""

from __future__ import annotations

import json
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from urllib import error as urllib_error

import pytest

import scope_recall.embedders as embedders_module
from scope_recall.embedders import (
    MiniMaxEmbedder,
    OpenAICompatibleEmbedder,
    SentenceTransformersEmbedder,
    _KNOWN_EMBEDDING_DIMS,
    build_embedder,
)


@pytest.fixture(autouse=True)
def _close_hosted_embedders(monkeypatch: pytest.MonkeyPatch):
    """Terminal-close hosted embedders so idle workers release global permits."""

    created: list[object] = []
    original_openai_init = OpenAICompatibleEmbedder.__init__
    original_minimax_init = MiniMaxEmbedder.__init__

    def _openai_init(self: OpenAICompatibleEmbedder, *args, **kwargs) -> None:
        original_openai_init(self, *args, **kwargs)
        created.append(self)

    def _minimax_init(self: MiniMaxEmbedder, *args, **kwargs) -> None:
        original_minimax_init(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(OpenAICompatibleEmbedder, "__init__", _openai_init)
    monkeypatch.setattr(MiniMaxEmbedder, "__init__", _minimax_init)
    yield
    for embedder in created:
        close = getattr(embedder, "close", None)
        if callable(close):
            close()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            resources = getattr(embedder, "request_resources")()
            if resources["workers"] == 0:
                break
            time.sleep(0.01)
        else:
            raise AssertionError(
                "fixture close left a live hosted worker: "
                f"{getattr(embedder, 'request_resources')()}"
            )


def test_sentence_transformer_readiness_records_model_load_failure(monkeypatch):
    """Package importability alone must not certify an unloadable local model."""

    embedders_module._SENTENCE_TRANSFORMER_CACHE.clear()

    class BrokenSentenceTransformer:
        def __init__(self, _model: str, **_kwargs) -> None:
            raise RuntimeError(
                "synthetic model artifact unavailable api_key=not-a-real-secret at "
                r"C:\Users\Administrator\.cache\broken-model"
            )

    monkeypatch.setattr(
        embedders_module,
        "SentenceTransformer",
        BrokenSentenceTransformer,
    )
    embedder = SentenceTransformersEmbedder(
        model="sentence-transformers/all-mpnet-base-v2",
    )

    assert embedder.is_available() is True
    with pytest.raises(RuntimeError, match="synthetic model artifact unavailable") as caught:
        embedder.probe_readiness()

    assert embedder.is_available() is False
    load_error = embedder.describe()["load_error"]
    assert "synthetic model artifact unavailable" in load_error
    assert "[REDACTED_SECRET]" in load_error
    assert "[REDACTED_PATH]" in load_error
    assert "Administrator" not in load_error
    rendered_traceback = "".join(
        traceback.format_exception(caught.type, caught.value, caught.tb)
    )
    assert "not-a-real-secret" not in rendered_traceback
    assert "Administrator" not in rendered_traceback


def test_sentence_transformer_model_cache_is_single_flight_per_key(monkeypatch):
    """Concurrent instances must share one expensive model construction."""

    embedders_module._SENTENCE_TRANSFORMER_CACHE.clear()
    constructor_calls = 0
    constructor_lock = threading.Lock()

    class SlowSentenceTransformer:
        def __init__(self, _model: str, **_kwargs) -> None:
            nonlocal constructor_calls
            with constructor_lock:
                constructor_calls += 1
            time.sleep(0.1)

        @staticmethod
        def get_embedding_dimension() -> int:
            return 5

    monkeypatch.setattr(
        embedders_module,
        "SentenceTransformer",
        SlowSentenceTransformer,
    )
    embedders = [
        SentenceTransformersEmbedder(model="synthetic-single-flight-model")
        for _ in range(2)
    ]
    start = threading.Barrier(3)

    def load_model(embedder: SentenceTransformersEmbedder) -> None:
        start.wait(timeout=5)
        embedder.probe_readiness()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(load_model, embedder) for embedder in embedders]
        start.wait(timeout=5)
        for future in futures:
            future.result(timeout=5)

    assert constructor_calls == 1
    assert [embedder.dimensions for embedder in embedders] == [5, 5]


def test_sentence_transformer_failed_load_is_shared_by_waiting_cohort_then_retried(
    monkeypatch,
):
    """Overlapping callers share one safe failure; a later caller may retry."""

    embedders_module._SENTENCE_TRANSFORMER_CACHE.clear()
    in_flight = getattr(embedders_module, "_SENTENCE_TRANSFORMER_LOAD_FLIGHTS", None)
    if isinstance(in_flight, dict):
        in_flight.clear()
    constructor_calls = 0
    constructor_lock = threading.Lock()

    class SlowBrokenSentenceTransformer:
        def __init__(self, _model: str, **_kwargs) -> None:
            nonlocal constructor_calls
            with constructor_lock:
                constructor_calls += 1
            time.sleep(0.1)
            raise RuntimeError("synthetic load failed api_key=not-a-real-secret")

    monkeypatch.setattr(
        embedders_module,
        "SentenceTransformer",
        SlowBrokenSentenceTransformer,
    )
    embedders = [
        SentenceTransformersEmbedder(model="synthetic-failed-flight-model")
        for _ in range(2)
    ]
    start = threading.Barrier(3)

    def load_model(embedder: SentenceTransformersEmbedder) -> str:
        start.wait(timeout=5)
        with pytest.raises(RuntimeError) as caught:
            embedder.probe_readiness()
        return "".join(
            traceback.format_exception(caught.type, caught.value, caught.tb)
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(load_model, embedder) for embedder in embedders]
        start.wait(timeout=5)
        rendered = [future.result(timeout=5) for future in futures]

    assert constructor_calls == 1
    assert all("not-a-real-secret" not in item for item in rendered)
    assert all("[REDACTED_SECRET]" in item for item in rendered)

    later = SentenceTransformersEmbedder(model="synthetic-failed-flight-model")
    with pytest.raises(RuntimeError):
        later.probe_readiness()
    assert constructor_calls == 2


class _FakeEmbeddingsAPI:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.encoding_formats: list[str | None] = []
        self.dimensions: list[int | None] = []
        self.inputs: list[list[str]] = []

    def create(
        self,
        *,
        model: str,
        input: list[str],
        encoding_format: str | None = None,
        dimensions: int | None = None,
        timeout: object = None,
    ):
        self.calls.append(len(input))
        self.encoding_formats.append(encoding_format)
        self.dimensions.append(dimensions)
        self.inputs.append(list(input))
        if len(input) > 100:
            raise AssertionError(f"batch too large: {len(input)}")

        class _Item:
            def __init__(self) -> None:
                self.embedding = [0.1, 0.2, 0.3]

        class _Response:
            def __init__(self, count: int) -> None:
                self.data = [_Item() for _ in range(count)]

        return _Response(len(input))


class _FakeOpenAIClient:
    def __init__(self) -> None:
        self.embeddings = _FakeEmbeddingsAPI()


def test_openai_compatible_embedder_chunks_large_batches(monkeypatch):
    fake_client = _FakeOpenAIClient()
    monkeypatch.setattr(
        OpenAICompatibleEmbedder,
        "_client_or_raise",
        lambda self: fake_client,
    )

    safe_placeholder_key = "pk-test"
    embedder = OpenAICompatibleEmbedder(
        model="gemini-embedding-001",
        api_key=safe_placeholder_key,
        base_url="https://example.invalid/v1",
        dimensions=3,
    )

    payload = [f"memory row {i}" for i in range(205)]
    vectors = embedder.embed_texts(payload)

    assert len(vectors) == 205
    assert fake_client.embeddings.calls == [100, 100, 5]
    assert fake_client.embeddings.encoding_formats == ["float", "float", "float"]
    assert embedder.dimensions == 3


def test_openai_compatible_embedder_sends_dimensions_and_separates_query_document_prompts(monkeypatch):
    fake_client = _FakeOpenAIClient()
    monkeypatch.setattr(OpenAICompatibleEmbedder, "_client_or_raise", lambda self: fake_client)
    embedder = OpenAICompatibleEmbedder(
        model="gemini-embedding-2",
        api_key="pk-test",
        base_url="https://example.invalid/v1",
        dimensions=3,
        request_dimensions=True,
        document_prefix="Represent this document for retrieval: ",
        query_prefix="Represent this query for retrieval: ",
        prompt_profile="gemini-retrieval-v1",
    )

    embedder.embed_texts(["alpha"])
    embedder.embed_query("beta")

    assert fake_client.embeddings.dimensions == [3, 3]
    assert fake_client.embeddings.inputs == [
        ["Represent this document for retrieval: alpha"],
        ["Represent this query for retrieval: beta"],
    ]
    assert embedder.describe()["prompt_profile"] == "gemini-retrieval-v1"


def test_openai_compatible_embedder_batches_query_prompts(monkeypatch):
    fake_client = _FakeOpenAIClient()
    monkeypatch.setattr(
        OpenAICompatibleEmbedder,
        "_client_or_raise",
        lambda self: fake_client,
    )
    embedder = OpenAICompatibleEmbedder(
        model="gemini-embedding-001",
        api_key="pk-test",
        base_url="https://example.invalid/v1",
        dimensions=3,
        document_prefix="document: ",
        query_prefix="query: ",
    )

    vectors = embedder.embed_queries(["alpha", "beta"])

    assert vectors == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]
    assert fake_client.embeddings.calls == [2]
    assert fake_client.embeddings.inputs == [["query: alpha", "query: beta"]]


def test_openai_compatible_embedder_restores_indexed_response_order(monkeypatch):
    class IndexedAPI:
        @staticmethod
        def create(**_kwargs):
            class Item:
                def __init__(self, index, embedding):
                    self.index = index
                    self.embedding = embedding

            class Response:
                data = [
                    Item(1, [0.0, 1.0, 0.0]),
                    Item(0, [1.0, 0.0, 0.0]),
                ]

            return Response()

    class IndexedClient:
        embeddings = IndexedAPI()

    embedder = OpenAICompatibleEmbedder(
        model="gemini-embedding-001",
        api_key="pk-test",
        base_url="https://example.invalid/v1",
        dimensions=3,
    )
    monkeypatch.setattr(embedder, "_client_or_raise", lambda: IndexedClient())

    assert embedder.embed_queries(["alpha", "beta"]) == [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
    ]


def test_openai_compatible_embedder_rejects_response_count_and_dimension_mismatch(monkeypatch):
    class BadAPI:
        def __init__(self, vectors):
            self.vectors = vectors

        def create(self, **_kwargs):
            class Item:
                def __init__(self, vector):
                    self.embedding = vector

            class Response:
                def __init__(self, vectors):
                    self.data = [Item(vector) for vector in vectors]

            return Response(self.vectors)

    class BadClient:
        def __init__(self, vectors):
            self.embeddings = BadAPI(vectors)

    embedder = OpenAICompatibleEmbedder(model="gemini-embedding-2", api_key="pk-test", dimensions=3)
    monkeypatch.setattr(embedder, "_client_or_raise", lambda: BadClient([[0.1, 0.2, 0.3]]))
    with pytest.raises(RuntimeError, match="response count"):
        embedder.embed_texts(["alpha", "beta"])

    monkeypatch.setattr(embedder, "_client_or_raise", lambda: BadClient([[0.1, 0.2]]))
    with pytest.raises(RuntimeError, match="dimensions"):
        embedder.embed_texts(["alpha"])

    monkeypatch.setattr(embedder, "_client_or_raise", lambda: BadClient([[0.1, float("nan"), 0.3]]))
    with pytest.raises(RuntimeError, match="non-finite"):
        embedder.embed_texts(["alpha"])

    monkeypatch.setattr(embedder, "_client_or_raise", lambda: BadClient([[0.0, 0.0, 0.0]]))
    with pytest.raises(RuntimeError, match="zero vector"):
        embedder.embed_texts(["alpha"])


def test_openai_compatible_embedder_retries_only_typed_connection_failures(monkeypatch):
    fake_client = _FakeOpenAIClient()
    real_create = fake_client.embeddings.create
    attempts = 0
    sleeps: list[float] = []

    def flaky_create(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionRefusedError("synthetic endpoint startup")
        return real_create(**kwargs)

    monkeypatch.setattr(fake_client.embeddings, "create", flaky_create)
    monkeypatch.setattr(
        OpenAICompatibleEmbedder,
        "_client_or_raise",
        lambda self: fake_client,
    )
    monkeypatch.setattr("scope_recall.embedders.time.sleep", sleeps.append)
    embedder = OpenAICompatibleEmbedder(
        model="gemini-embedding-001",
        api_key="pk-test",
        dimensions=3,
        connection_retry_delays=[0.25, 0.5],
    )

    assert embedder.embed_texts(["alpha"]) == [[0.1, 0.2, 0.3]]
    assert attempts == 3
    assert sleeps == [0.25, 0.5]
    assert embedder.describe()["connection_retry_delays"] == [0.25, 0.5]


def test_openai_compatible_embedder_preserves_default_retry_schedule():
    embedder = OpenAICompatibleEmbedder(
        model="gemini-embedding-001",
        api_key="pk-test",
        dimensions=3,
    )

    assert embedder.describe()["connection_retry_delays"] == [2.0, 4.0, 8.0]


def test_openai_compatible_embedder_does_not_guess_connection_errors_from_text(monkeypatch):
    class PolicyAPI:
        calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            raise RuntimeError("connection refused by request policy")

    class PolicyClient:
        def __init__(self):
            self.embeddings = PolicyAPI()

    client = PolicyClient()
    sleeps: list[float] = []
    monkeypatch.setattr(OpenAICompatibleEmbedder, "_client_or_raise", lambda self: client)
    monkeypatch.setattr("scope_recall.embedders.time.sleep", sleeps.append)
    embedder = OpenAICompatibleEmbedder(
        model="gemini-embedding-001",
        api_key="pk-test",
        dimensions=3,
        connection_retry_delays=[1.0, 2.0],
    )

    with pytest.raises(RuntimeError, match="request policy"):
        embedder.embed_texts(["alpha"])

    assert client.embeddings.calls == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "delays",
    (
        "1,2",
        [-1],
        [float("inf")],
        [True],
        [0.0] * 9,
    ),
)
def test_openai_compatible_embedder_rejects_unsafe_retry_delays(delays):
    with pytest.raises(ValueError, match="connection_retry_delays"):
        OpenAICompatibleEmbedder(
            model="gemini-embedding-001",
            api_key="pk-test",
            dimensions=3,
            connection_retry_delays=delays,
        )


def test_build_embedder_threads_connection_retry_configuration():
    embedder = build_embedder(
        {
            "provider": "openai-compatible",
            "model": "gemini-embedding-001",
            "api_key": "pk-test",
            "dimensions": 3,
            "connection_retry_delays": [0.1],
        }
    )

    assert isinstance(embedder, OpenAICompatibleEmbedder)
    assert embedder.describe()["connection_retry_delays"] == [0.1]


def test_build_embedder_honors_configured_base_url_env_over_packaged_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "TEST_EMBEDDER_BASE_URL",
        "https://env-embedding.example.test/v1",
    )

    embedder = build_embedder(
        {
            "provider": "openai-compatible",
            "model": "test-embedding-model",
            "api_key": "pk-test",
            "base_url": "https://packaged-default.example.test/v1",
            "base_url_env": "TEST_EMBEDDER_BASE_URL",
            "dimensions": 3,
        }
    )

    assert isinstance(embedder, OpenAICompatibleEmbedder)
    assert embedder.describe()["base_url"] == (
        "https://env-embedding.example.test/v1"
    )


# ---------------------------------------------------------------------------
# MiniMax (embo-01) embedder tests
# ---------------------------------------------------------------------------


class _FakeHTTPResponse:
    """Context-manager wrapper that quacks like ``http.client.HTTPResponse``."""

    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def close(self) -> None:  # noqa: D401 - mirrors HTTPResponse API
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _make_minimax_embedder() -> MiniMaxEmbedder:
    return MiniMaxEmbedder(
        model="embo-01",
        api_key="pk-test",
        base_url="https://example.invalid",
        dimensions=3,
    )


def test_minimax_known_dimensions_for_embo_01():
    assert _KNOWN_EMBEDDING_DIMS["embo-01"] == 1536
    assert _KNOWN_EMBEDDING_DIMS["minimax-embedding"] == 1536


def test_build_embedder_routes_minimax_provider():
    embedder = build_embedder(
        {
            "provider": "minimax",
            "model": "embo-01",
            "api_key": "pk-test",
        }
    )
    assert isinstance(embedder, MiniMaxEmbedder)
    assert embedder.provider == "minimax"
    assert embedder.model == "embo-01"
    assert embedder.dimensions == 1536
    assert embedder.is_available() is True
    payload = embedder.describe()
    assert payload["base_url"].endswith("api.minimaxi.com")
    assert payload["document_type"] == "db"
    assert payload["query_type"] == "query"


def test_minimax_embedder_requires_api_key(monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    embedder = MiniMaxEmbedder(
        model="embo-01",
        api_key=None,
        api_key_env=None,
    )
    assert embedder.is_available() is False


def test_minimax_embedder_sends_expected_request(monkeypatch):
    embedder = _make_minimax_embedder()
    captured: list[dict] = []

    def fake_urlopen(request, *, timeout, allow_insecure=False):  # noqa: ARG001
        captured.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "headers": dict(request.headers),
                "body": request.data.decode("utf-8") if request.data else "",
            }
        )
        return _FakeHTTPResponse(
            {
                "vectors": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]],
                "base_resp": {"status_code": 0, "status_msg": "success"},
            }
        )

    monkeypatch.setattr("scope_recall.embedders.safe_urlopen", fake_urlopen)

    vectors = embedder.embed_texts(["alpha", "beta"])

    assert len(vectors) == 2
    assert vectors[0] == [0.1, 0.2, 0.3]
    assert vectors[1] == [0.4, 0.5, 0.6]
    assert len(captured) == 1
    request = captured[0]
    assert request["method"] == "POST"
    assert request["url"] == "https://example.invalid/v1/embeddings"
    assert request["headers"]["Authorization"] == "Bearer pk-test"
    body = json.loads(request["body"])
    assert body["model"] == "embo-01"
    assert body["texts"] == ["alpha", "beta"]
    assert body["type"] == "db"


def test_minimax_embedder_uses_query_type_for_search_queries(monkeypatch):
    embedder = _make_minimax_embedder()
    captured: list[dict] = []

    def fake_urlopen(request, *, timeout, allow_insecure=False):  # noqa: ARG001
        captured.append(
            {
                "url": request.full_url,
                "body": request.data.decode("utf-8") if request.data else "",
            }
        )
        return _FakeHTTPResponse({"vectors": [[0.7, 0.8, 0.9]]})

    monkeypatch.setattr("scope_recall.embedders.safe_urlopen", fake_urlopen)

    vector = embedder.embed_query("find this")

    assert vector == [0.7, 0.8, 0.9]
    assert captured[0]["url"] == "https://example.invalid/v1/embeddings"
    body = json.loads(captured[0]["body"])
    assert body["texts"] == ["find this"]
    assert body["type"] == "query"


def test_minimax_embedder_sends_optional_group_id_query_param(monkeypatch):
    embedder = MiniMaxEmbedder(
        model="embo-01",
        api_key="pk-test",
        base_url="https://example.invalid",
        group_id="public-group-id",
        dimensions=3,
    )
    captured_urls: list[str] = []

    def fake_urlopen(request, *, timeout, allow_insecure=False):  # noqa: ARG001
        captured_urls.append(request.full_url)
        return _FakeHTTPResponse({"vectors": [[0.1, 0.2, 0.3]]})

    monkeypatch.setattr("scope_recall.embedders.safe_urlopen", fake_urlopen)

    embedder.embed_texts(["alpha"])

    assert captured_urls == ["https://example.invalid/v1/embeddings?GroupId=public-group-id"]
    payload = embedder.describe()
    assert payload["group_id_configured"] is True


def test_minimax_embedder_chunks_large_batches(monkeypatch):
    embedder = _make_minimax_embedder()
    call_sizes: list[int] = []

    def fake_urlopen(request, *, timeout, allow_insecure=False):  # noqa: ARG001
        body = json.loads(request.data.decode("utf-8"))
        call_sizes.append(len(body["texts"]))
        return _FakeHTTPResponse(
            {"vectors": [[0.1, 0.2, 0.3] for _ in body["texts"]]}
        )

    monkeypatch.setattr("scope_recall.embedders.safe_urlopen", fake_urlopen)

    payload = [f"row {i}" for i in range(150)]
    vectors = embedder.embed_texts(payload)

    # 150 / 64 → 64 + 64 + 22
    assert call_sizes == [64, 64, 22]
    assert len(vectors) == 150


def test_minimax_embedder_raises_on_http_error(monkeypatch):
    # Pin a single key so _rotate_key_after_failure returns False — exercises
    # the "single key, terminal failure" branch.  Mirrors OpenAI's behaviour:
    # the last error is re-raised verbatim, not wrapped in RuntimeError.
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    embedder = MiniMaxEmbedder(
        model="embo-01",
        api_key="pk-test",
        base_url="https://example.invalid",
        dimensions=3,
    )
    assert len(embedder._api_keys) == 1

    def fake_urlopen(request, *, timeout, allow_insecure=False):  # noqa: ARG001
        raise urllib_error.HTTPError(
            request.full_url,
            429,
            "Too Many Requests",
            {},  # type: ignore[arg-type]
            _FakeHTTPResponse({"error": "rate limited"}),  # type: ignore[arg-type]
        )

    monkeypatch.setattr("scope_recall.embedders.safe_urlopen", fake_urlopen)

    with pytest.raises(urllib_error.HTTPError) as exc:
        embedder.embed_texts(["one"])

    assert exc.value.code == 429


def test_minimax_embedder_rotates_to_second_key(monkeypatch):
    # Two raw keys, first request fails, second succeeds. Verifies the
    # rotate + retry branch (not terminal failure).
    embedder = MiniMaxEmbedder(
        model="embo-01",
        api_key=["key-one", "key-two"],
        api_key_env=[],  # ignore MINIMAX_API_KEY from the host environment
        base_url="https://example.invalid",
        dimensions=3,
    )
    assert len(embedder._api_keys) == 2

    call_auths: list[str] = []

    def fake_urlopen(request, *, timeout, allow_insecure=False):  # noqa: ARG001
        call_auths.append(request.headers["Authorization"])
        if len(call_auths) == 1:
            raise urllib_error.HTTPError(
                request.full_url,
                429,
                "Too Many Requests",
                {},  # type: ignore[arg-type]
                _FakeHTTPResponse({"error": "rate limited"}),  # type: ignore[arg-type]
            )
        return _FakeHTTPResponse(
            {"vectors": [[0.1, 0.2, 0.3]]}
        )

    monkeypatch.setattr("scope_recall.embedders.safe_urlopen", fake_urlopen)

    vectors = embedder.embed_texts(["hello"])

    assert len(vectors) == 1
    assert vectors[0] == [0.1, 0.2, 0.3]
    assert call_auths == ["Bearer key-one", "Bearer key-two"]


def test_minimax_embedder_raises_on_missing_vectors(monkeypatch):
    embedder = _make_minimax_embedder()

    def fake_urlopen(request, *, timeout, allow_insecure=False):  # noqa: ARG001
        return _FakeHTTPResponse({"base_resp": {"status_msg": "ok"}})

    monkeypatch.setattr("scope_recall.embedders.safe_urlopen", fake_urlopen)

    with pytest.raises(RuntimeError) as exc:
        embedder.embed_texts(["one"])

    assert "vectors" in str(exc.value)
