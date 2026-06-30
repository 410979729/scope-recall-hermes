"""Tests for embedding adapters, hosted-provider request shapes, and fallback behavior.

They isolate provider quirks such as OpenAI-compatible float vector responses."""

from __future__ import annotations

import json
from urllib import error as urllib_error

import pytest

from scope_recall.embedders import (
    MiniMaxEmbedder,
    OpenAICompatibleEmbedder,
    _KNOWN_EMBEDDING_DIMS,
    build_embedder,
)


class _FakeEmbeddingsAPI:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.encoding_formats: list[str | None] = []

    def create(self, *, model: str, input: list[str], encoding_format: str | None = None):
        self.calls.append(len(input))
        self.encoding_formats.append(encoding_format)
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

    def fake_urlopen(request, timeout):  # noqa: ARG001
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

    monkeypatch.setattr("scope_recall.embedders.urllib.request.urlopen", fake_urlopen)

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

    def fake_urlopen(request, timeout):  # noqa: ARG001
        captured.append(
            {
                "url": request.full_url,
                "body": request.data.decode("utf-8") if request.data else "",
            }
        )
        return _FakeHTTPResponse({"vectors": [[0.7, 0.8, 0.9]]})

    monkeypatch.setattr("scope_recall.embedders.urllib.request.urlopen", fake_urlopen)

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
    )
    captured_urls: list[str] = []

    def fake_urlopen(request, timeout):  # noqa: ARG001
        captured_urls.append(request.full_url)
        return _FakeHTTPResponse({"vectors": [[0.1, 0.2, 0.3]]})

    monkeypatch.setattr("scope_recall.embedders.urllib.request.urlopen", fake_urlopen)

    embedder.embed_texts(["alpha"])

    assert captured_urls == ["https://example.invalid/v1/embeddings?GroupId=public-group-id"]
    payload = embedder.describe()
    assert payload["group_id_configured"] is True


def test_minimax_embedder_chunks_large_batches(monkeypatch):
    embedder = _make_minimax_embedder()
    call_sizes: list[int] = []

    def fake_urlopen(request, timeout):  # noqa: ARG001
        body = json.loads(request.data.decode("utf-8"))
        call_sizes.append(len(body["texts"]))
        return _FakeHTTPResponse(
            {"vectors": [[0.0, 0.0, 0.0] for _ in body["texts"]]}
        )

    monkeypatch.setattr("scope_recall.embedders.urllib.request.urlopen", fake_urlopen)

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
    )
    assert len(embedder._api_keys) == 1

    def fake_urlopen(request, timeout):  # noqa: ARG001
        raise urllib_error.HTTPError(
            request.full_url,
            429,
            "Too Many Requests",
            {},  # type: ignore[arg-type]
            _FakeHTTPResponse({"error": "rate limited"}),  # type: ignore[arg-type]
        )

    monkeypatch.setattr("scope_recall.embedders.urllib.request.urlopen", fake_urlopen)

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
    )
    assert len(embedder._api_keys) == 2

    call_auths: list[str] = []

    def fake_urlopen(request, timeout):  # noqa: ARG001
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

    monkeypatch.setattr("scope_recall.embedders.urllib.request.urlopen", fake_urlopen)

    vectors = embedder.embed_texts(["hello"])

    assert len(vectors) == 1
    assert vectors[0] == [0.1, 0.2, 0.3]
    assert call_auths == ["Bearer key-one", "Bearer key-two"]


def test_minimax_embedder_raises_on_missing_vectors(monkeypatch):
    embedder = _make_minimax_embedder()

    def fake_urlopen(request, timeout):  # noqa: ARG001
        return _FakeHTTPResponse({"base_resp": {"status_msg": "ok"}})

    monkeypatch.setattr("scope_recall.embedders.urllib.request.urlopen", fake_urlopen)

    with pytest.raises(RuntimeError) as exc:
        embedder.embed_texts(["one"])

    assert "vectors" in str(exc.value)
