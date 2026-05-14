from __future__ import annotations

from scope_recall.embedders import OpenAICompatibleEmbedder


class _FakeEmbeddingsAPI:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def create(self, *, model: str, input: list[str]):
        self.calls.append(len(input))
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

    safe_placeholder_key = "public-test-key"
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
    assert embedder.dimensions == 3
