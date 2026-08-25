"""Focused regressions for hosted embedding timeout budgets, shared
validation, and idempotent HTTP client close (P1-03, P2-02, P3-03).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any

import pytest

import scope_recall.embedding_request_runner as request_runner_module
import scope_recall.embedders as embedders_module
from scope_recall.embedding_request_runner import (
    MAX_LIVE_HOSTED_EMBEDDING_WORKERS,
    BoundedEmbedderRequestRunner,
    EmbedderRequestClosedError,
    EmbedderRequestDeadlineError,
    HostedEmbedderWorkerLimitError,
    InFlightEmbedderRequestError,
    hosted_embedding_worker_budget,
)
from scope_recall.embedding_validation import validate_embedding_batch
from scope_recall.embedders import (
    MiniMaxEmbedder,
    OpenAICompatibleEmbedder,
    SentenceTransformersEmbedder,
    build_embedder,
    close_embedder,
)
from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.storage_views import search_vector_memories
from scope_recall.vector_generation import GenerationIdentity, bootstrap_legacy_generation
from scope_recall.vector_runtime import setup_vector_layer, sync_vector_index


class _CountingHandle:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _Item:
    def __init__(self, vector: list[float]) -> None:
        self.embedding = vector


class _Response:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.data = [_Item(vector) for vector in vectors]


def _timeout_seconds(timeout: Any) -> float:
    read = getattr(timeout, "read", None)
    if read is not None:
        return float(read)
    return float(timeout)


def _request_threads(name: str) -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == name]


def _good_response() -> _Response:
    return _Response([[0.1, 0.2, 0.3]])


def _call_expecting_timeout(fn: Any, errors: list[BaseException]) -> None:
    try:
        fn()
    except TimeoutError as exc:
        errors.append(exc)
    except BaseException as exc:  # pragma: no cover - test helper
        errors.append(exc)


def _wait_until(predicate: Any, *, timeout: float = 2.0, message: Any) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(message() if callable(message) else message)


def _wait_until_idle(embedder: OpenAICompatibleEmbedder, *, timeout: float = 2.0) -> None:
    _wait_until(
        lambda: embedder.request_resources()["in_flight"] == 0,
        timeout=timeout,
        message=f"request runner stayed occupied: {embedder.request_resources()}",
    )


def _resource_snapshot(target: Any) -> dict[str, int]:
    if hasattr(target, "request_resources"):
        return target.request_resources()
    return target.snapshot()


def _wait_until_worker_exit(target: Any, *, timeout: float = 2.0) -> None:
    _wait_until(
        lambda: _resource_snapshot(target)["workers"] == 0,
        timeout=timeout,
        message=lambda: f"request worker stayed alive: {_resource_snapshot(target)}",
    )


def _wait_until_budget(*, live_workers: int, timeout: float = 2.0) -> None:
    _wait_until(
        lambda: hosted_embedding_worker_budget()["live_workers"] == live_workers,
        timeout=timeout,
        message=(
            "plugin worker budget did not recover: "
            f"{hosted_embedding_worker_budget()} expected live_workers={live_workers}"
        ),
    )


@pytest.fixture(autouse=True)
def _close_hosted_embedders(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Terminal-close hosted embedders so idle workers release global permits."""

    created: list[Any] = []
    original_openai_init = OpenAICompatibleEmbedder.__init__
    original_minimax_init = MiniMaxEmbedder.__init__

    def _openai_init(self: OpenAICompatibleEmbedder, *args: Any, **kwargs: Any) -> None:
        original_openai_init(self, *args, **kwargs)
        created.append(self)

    def _minimax_init(self: MiniMaxEmbedder, *args: Any, **kwargs: Any) -> None:
        original_minimax_init(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(OpenAICompatibleEmbedder, "__init__", _openai_init)
    monkeypatch.setattr(MiniMaxEmbedder, "__init__", _minimax_init)
    yield
    for embedder in created:
        embedder.close()
        _wait_until_worker_exit(embedder)


def _openai_embedder(**kwargs: Any) -> OpenAICompatibleEmbedder:
    options = {
        "model": "gemini-embedding-001",
        "api_key": "pk-test",
        "base_url": "https://example.invalid/v1",
        "dimensions": 3,
        "connection_retry_delays": [],
    }
    options.update(kwargs)
    return OpenAICompatibleEmbedder(**options)


def test_openai_compatible_embedder_configures_transport_timeouts_and_disables_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeHttpClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["http_kwargs"] = dict(kwargs)
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    class FakeHttpx:
        class Timeout:
            def __init__(self, **kwargs: Any) -> None:
                captured["timeout"] = dict(kwargs)

        Client = FakeHttpClient

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured["openai"] = dict(kwargs)
            self.embeddings = object()
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    monkeypatch.setattr(embedders_module, "_httpx", FakeHttpx)
    monkeypatch.setattr(embedders_module, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(embedders_module, "DefaultHttpxClient", FakeHttpx.Client)

    embedder = _openai_embedder(
        connect_timeout_seconds=1.5,
        read_timeout_seconds=2.5,
        write_timeout_seconds=3.5,
        pool_timeout_seconds=0.5,
        query_timeout_seconds=4.0,
        writer_timeout_seconds=9.0,
        maintenance_timeout_seconds=11.0,
    )
    embedder._client_or_raise()

    assert captured["openai"]["max_retries"] == 0
    assert "timeout" in captured["http_kwargs"]
    assert captured["timeout"] == {
        "connect": 1.5,
        "read": 2.5,
        "write": 3.5,
        "pool": 0.5,
    }
    described = embedder.describe()
    assert described["sdk_max_retries"] == 0
    assert described["query_timeout_seconds"] == 4.0
    assert described["writer_timeout_seconds"] == 9.0
    assert described["maintenance_timeout_seconds"] == 11.0


def test_openai_compatible_embedder_query_budget_stops_retries_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    class PolicyAPI:
        def create(self, **_kwargs: Any) -> Any:
            nonlocal attempts
            attempts += 1
            raise ConnectionRefusedError("synthetic query hang")

    class PolicyClient:
        embeddings = PolicyAPI()

    monkeypatch.setattr(
        OpenAICompatibleEmbedder,
        "_client_or_raise",
        lambda self: PolicyClient(),
    )
    monkeypatch.setattr("scope_recall.embedders.time.sleep", sleeps.append)
    embedder = _openai_embedder(
        connection_retry_delays=[0.4, 0.4],
        query_timeout_seconds=0.25,
        writer_timeout_seconds=5.0,
    )

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="query embedding exceeded"):
        embedder.embed_query("alpha")
    elapsed = time.monotonic() - started

    assert attempts == 1
    assert sleeps == []
    assert elapsed < 1.0


def test_openai_compatible_embedder_writer_and_maintenance_budgets_are_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class PolicyAPI:
        def create(self, **_kwargs: Any) -> Any:
            calls.append("create")
            raise ConnectionRefusedError("synthetic writer retry")

    class PolicyClient:
        embeddings = PolicyAPI()

    sleeps: list[float] = []
    monkeypatch.setattr(
        OpenAICompatibleEmbedder,
        "_client_or_raise",
        lambda self: PolicyClient(),
    )
    monkeypatch.setattr("scope_recall.embedders.time.sleep", sleeps.append)
    embedder = _openai_embedder(
        connection_retry_delays=[0.4],
        query_timeout_seconds=0.2,
        writer_timeout_seconds=0.2,
        maintenance_timeout_seconds=2.0,
    )

    with pytest.raises(TimeoutError, match="writer embedding exceeded"):
        embedder.embed_texts(["alpha"])
    writer_calls = len(calls)
    writer_sleeps = list(sleeps)

    calls.clear()
    sleeps.clear()
    with pytest.raises(ConnectionRefusedError):
        embedder.embed_maintenance(["alpha"])

    assert writer_calls == 1
    assert writer_sleeps == []
    assert len(calls) == 2
    assert sleeps == [0.4]


def test_openai_compatible_embedder_half_open_transport_returns_within_query_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class HangingAPI:
        def create(self, **_kwargs: Any) -> Any:
            entered.set()
            release.wait(timeout=5)
            return _good_response()

    class HangingClient:
        embeddings = HangingAPI()

    monkeypatch.setattr(
        OpenAICompatibleEmbedder,
        "_client_or_raise",
        lambda self: HangingClient(),
    )
    embedder = _openai_embedder(
        query_timeout_seconds=0.2,
        connection_retry_delays=[],
    )

    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="query embedding exceeded"):
            embedder.embed_query("alpha")
        elapsed = time.monotonic() - started

        assert entered.wait(timeout=1)
        assert elapsed < 1.0
    finally:
        release.set()


def test_search_vector_memories_falls_back_lexically_when_query_budget_expires() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    store_row(
        conn,
        memory_id="lex-only",
        scope_id="scope-a",
        platform="cli",
        user_id="sample-user",
        chat_id="",
        thread_id="",
        gateway_session_key="",
        agent_identity="sample-agent",
        agent_workspace="hermes",
        session_id="session",
        source="tool-store",
        target="memory",
        content="lexical fallback remains available",
    )

    class Provider:
        _lock = threading.RLock()
        _vector_ready = True
        _vector_store = object()
        _vector_config = {"top_k": 4}
        _retrieval_config = {"vector_min_score": 0.0}
        _accessible_scope_ids = ["scope-a"]
        _vector_status = "ready"

        def __init__(self) -> None:
            self._conn = conn
            self._embedder = _openai_embedder(query_timeout_seconds=0.2)

        def _require_conn(self):
            return self._conn

    provider = Provider()
    provider._embedder.is_available = lambda: True  # type: ignore[method-assign]

    release = threading.Event()

    class HangingAPI:
        def create(self, **_kwargs: Any) -> Any:
            release.wait(timeout=5)
            return _good_response()

    class HangingClient:
        embeddings = HangingAPI()

    provider._embedder._client_or_raise = lambda: HangingClient()  # type: ignore[method-assign]

    started = time.monotonic()
    try:
        results = search_vector_memories(provider, "lexical fallback", limit=5)
        elapsed = time.monotonic() - started

        assert results == []
        assert provider._vector_ready is True
        assert getattr(provider, "_vector_status", "ready") != "needs_repair"
        assert elapsed < 1.0
    finally:
        release.set()
        conn.close()


def test_openai_compatible_embedder_close_is_idempotent_and_releases_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http_handle = _CountingHandle()
    openai_handle = _CountingHandle()

    class FakeHttpClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.closed = http_handle

        def close(self) -> None:
            http_handle.close()

    class FakeHttpx:
        class Timeout:
            def __init__(self, **_kwargs: Any) -> None:
                return None

        Client = FakeHttpClient

    class FakeOpenAI:
        def __init__(self, **_kwargs: Any) -> None:
            self.embeddings = object()

        def close(self) -> None:
            openai_handle.close()

    monkeypatch.setattr(embedders_module, "_httpx", FakeHttpx)
    monkeypatch.setattr(embedders_module, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(embedders_module, "DefaultHttpxClient", FakeHttpx.Client)

    embedder = _openai_embedder()
    embedder._client_or_raise()
    embedder.reset_transport()
    embedder._client_or_raise()
    embedder.close()
    embedder.close()
    close_embedder(embedder)

    assert http_handle.closed == 2
    assert openai_handle.closed == 2
    assert embedder._client is None
    assert embedder._http_client is None
    assert embedder._closed is True
    with pytest.raises(RuntimeError, match="embedder is closed"):
        embedder._client_or_raise()
    with pytest.raises(RuntimeError, match="embedder is closed"):
        embedder.embed_query("closed")


def test_repeated_setup_and_cleanup_close_embedder_without_leaking_handles() -> None:
    closes: list[str] = []

    class ClosableEmbedder:
        provider = "local-hash"
        dimensions = 2
        model = "hash-v1"

        def __init__(self) -> None:
            self._closed = False

        def is_available(self) -> bool:
            return True

        def probe_readiness(self) -> None:
            return None

        def describe(self) -> dict[str, Any]:
            return {"provider": self.provider}

        def close(self) -> None:
            if self._closed:
                return
            self._closed = True
            closes.append("close")

    first = ClosableEmbedder()
    second = ClosableEmbedder()
    close_embedder(first)
    close_embedder(first)
    close_embedder(second)
    close_embedder(None)

    class Provider:
        def __init__(self) -> None:
            self._embedder = ClosableEmbedder()
            self._vector_store = None
            self._vector_config = {"enabled": False}
            self._retrieval_config = {}
            self._storage_dir = None

    provider = Provider()
    previous = provider._embedder
    setup_vector_layer(provider)
    setup_vector_layer(provider)

    assert previous._closed is True
    assert provider._embedder is None
    assert closes == ["close", "close", "close"]


@pytest.mark.parametrize(
    "rows, expected_count, expected_dimensions, match",
    (
        ([[0.1, 0.2, 0.3]], 2, 3, "response count"),
        ([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6], [0.7, 0.8, 0.9]], 2, 3, "response count"),
        ([[0.1, 0.2]], 1, 3, "dimensions"),
        ([[0.1, float("nan"), 0.3]], 1, 3, "non-finite"),
        ([[0.1, float("inf"), 0.3]], 1, 3, "non-finite"),
        ([[0.0, 0.0, 0.0]], 1, 3, "zero vector"),
    ),
)
def test_shared_embedding_validation_fails_closed(
    rows: list[list[float]],
    expected_count: int,
    expected_dimensions: int,
    match: str,
) -> None:
    with pytest.raises(RuntimeError, match=match):
        validate_embedding_batch(
            rows,
            expected_count=expected_count,
            expected_dimensions=expected_dimensions,
            provider="audit",
        )


class _FakeHTTPResponse:
    def __init__(self, payload: dict) -> None:
        import json

        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def close(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_minimax_and_sentence_transformers_share_strict_batch_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedder = MiniMaxEmbedder(
        model="embo-01",
        api_key="pk-test",
        base_url="https://example.invalid",
        dimensions=3,
    )

    def n_minus_one(request, *, timeout, allow_insecure=False):  # noqa: ARG001
        return _FakeHTTPResponse({"vectors": [[0.1, 0.2, 0.3]]})

    monkeypatch.setattr("scope_recall.embedders.safe_urlopen", n_minus_one)
    with pytest.raises(RuntimeError, match="response count"):
        embedder.embed_texts(["alpha", "beta"])

    def wrong_dimension(request, *, timeout, allow_insecure=False):  # noqa: ARG001
        return _FakeHTTPResponse({"vectors": [[0.1, 0.2]]})

    monkeypatch.setattr("scope_recall.embedders.safe_urlopen", wrong_dimension)
    with pytest.raises(RuntimeError, match="dimensions"):
        embedder.embed_texts(["alpha"])

    def non_finite(request, *, timeout, allow_insecure=False):  # noqa: ARG001
        return _FakeHTTPResponse({"vectors": [[0.1, float("nan"), 0.3]]})

    monkeypatch.setattr("scope_recall.embedders.safe_urlopen", non_finite)
    with pytest.raises(RuntimeError, match="non-finite"):
        embedder.embed_texts(["alpha"])

    def all_zero(request, *, timeout, allow_insecure=False):  # noqa: ARG001
        return _FakeHTTPResponse({"vectors": [[0.0, 0.0, 0.0]]})

    monkeypatch.setattr("scope_recall.embedders.safe_urlopen", all_zero)
    with pytest.raises(RuntimeError, match="zero vector"):
        embedder.embed_texts(["alpha"])

    def n_plus_one(request, *, timeout, allow_insecure=False):  # noqa: ARG001
        return _FakeHTTPResponse(
            {"vectors": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]}
        )

    monkeypatch.setattr("scope_recall.embedders.safe_urlopen", n_plus_one)
    with pytest.raises(RuntimeError, match="response count"):
        embedder.embed_texts(["alpha"])

    class ShortEncoder:
        def encode(self, items, **_kwargs: Any) -> list[list[float]]:
            return [[1.0, 0.0, 0.0] for _ in items[:-1]]

    st = SentenceTransformersEmbedder(model="synthetic-audit-model", dimensions=3)
    st._model_obj = ShortEncoder()
    with pytest.raises(RuntimeError, match="response count"):
        st.embed_texts(["alpha", "beta"])


def test_minimax_half_open_transport_returns_within_query_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedder = MiniMaxEmbedder(
        model="embo-01",
        api_key="pk-test",
        base_url="https://example.invalid",
        dimensions=3,
        query_timeout_seconds=0.15,
        read_timeout_seconds=5.0,
    )
    entered = threading.Event()
    release = threading.Event()
    results: list[list[float]] = []
    errors: list[BaseException] = []

    def hanging_urlopen(request: Any, *, timeout: float, allow_insecure: bool = False) -> Any:
        del request, timeout, allow_insecure
        entered.set()
        release.wait(timeout=5)
        return _FakeHTTPResponse({"vectors": [[0.1, 0.2, 0.3]]})

    monkeypatch.setattr("scope_recall.embedders.safe_urlopen", hanging_urlopen)

    def call_query() -> None:
        try:
            results.append(embedder.embed_query("half-open"))
        except BaseException as exc:  # pragma: no cover - assertion payload
            errors.append(exc)

    caller = threading.Thread(target=call_query)
    caller.start()
    try:
        assert entered.wait(timeout=1.0)
        caller.join(timeout=0.5)
        assert caller.is_alive() is False
        assert results == []
        assert len(errors) == 1
        assert isinstance(errors[0], TimeoutError)
        assert "query embedding exceeded" in str(errors[0])
        resources = embedder.request_resources()
        assert resources["in_flight"] == 1
        assert resources["queued"] == 0
        assert resources["workers"] == 1
        assert resources["live_workers"] <= MAX_LIVE_HOSTED_EMBEDDING_WORKERS
        retry_started = time.monotonic()
        with pytest.raises(TimeoutError, match="already in flight"):
            embedder.embed_query("still-half-open")
        assert time.monotonic() - retry_started < 0.15
        assert embedder.request_resources()["in_flight"] == 1
    finally:
        release.set()
        caller.join(timeout=2)
        close_embedder(embedder)
        if hasattr(embedder, "request_resources"):
            _wait_until_worker_exit(embedder)


def test_full_sync_rejects_n_minus_one_without_partial_write() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    for memory_id, content in (("row-a", "first durable"), ("row-b", "second durable")):
        store_row(
            conn,
            memory_id=memory_id,
            scope_id="local-scope",
            platform="cli",
            user_id="sample-user",
            chat_id="",
            thread_id="",
            gateway_session_key="",
            agent_identity="sample-agent",
            agent_workspace="hermes",
            session_id="session",
            source="tool-store",
            target="memory",
            content=content,
        )
    conn.commit()
    bootstrap_legacy_generation(
        conn,
        identity=GenerationIdentity(
            backend="lancedb",
            provider="fake",
            model="fixture-v1",
            dimensions=2,
        ),
        row_count=0,
    )
    conn.commit()

    class ShortEmbedder:
        dimensions = 2
        provider = "fake"

        def embed_texts(self, texts):
            return [[1.0, 0.0]]

        def embed_maintenance(self, texts):
            return self.embed_texts(texts)

    class RecordingStore:
        def __init__(self) -> None:
            self.records: dict[str, Any] = {}
            self.upserts = 0

        def list_ids(self):
            return list(self.records)

        def list_records(self):
            return dict(self.records)

        def delete_by_ids(self, ids):
            for memory_id in ids:
                self.records.pop(memory_id, None)

        def upsert_records(self, rows):
            self.upserts += 1
            for row in rows:
                self.records[str(row["id"])] = dict(row)

        def audit_counts(self):
            return {
                "physical_rows": len(self.records),
                "unique_ids": len(self.records),
                "duplicate_rows": 0,
                "duplicate_ids": 0,
            }

    class Provider:
        def __init__(self) -> None:
            self._conn = conn
            self._lock = threading.RLock()
            self._vector_lock = self._lock
            self._vector_config = {"index_general": False}
            self._vector_ready = True
            self._vector_store = RecordingStore()
            self._embedder = ShortEmbedder()
            self._scope_id = "local-scope"
            self._vector_row_count = 0
            self._vector_unique_id_count = 0
            self._vector_duplicate_row_count = 0
            self._vector_status = "ready"
            self._vector_message = ""
            self._vector_generation_id = "gen-audit"

        def _require_conn(self):
            return self._conn

        def _vector_text(self, summary, content):
            return f"{summary}\n{content}".strip()

    provider = Provider()
    with pytest.raises(RuntimeError, match="response count"):
        sync_vector_index(provider)

    assert provider._vector_store.upserts == 0
    assert provider._vector_store.records == {}
    conn.close()


def test_build_embedder_threads_timeout_budgets() -> None:
    embedder = build_embedder(
        {
            "provider": "openai-compatible",
            "model": "gemini-embedding-001",
            "api_key": "pk-test",
            "dimensions": 3,
            "connect_timeout_seconds": 1.25,
            "query_timeout_seconds": 2.5,
            "writer_timeout_seconds": 6.5,
            "maintenance_timeout_seconds": 7.5,
        }
    )

    assert isinstance(embedder, OpenAICompatibleEmbedder)
    described = embedder.describe()
    assert described["connect_timeout_seconds"] == 1.25
    assert described["query_timeout_seconds"] == 2.5
    assert described["writer_timeout_seconds"] == 6.5
    assert described["maintenance_timeout_seconds"] == 7.5
    assert described["sdk_max_retries"] == 0
    assert described["closed"] is False
    resources = described["request_resources"]
    assert resources["max_in_flight"] == 1
    assert resources["max_queued"] == 0
    assert resources["in_flight"] == 0
    assert resources["queued"] == 0
    assert resources["workers"] == 0
    assert resources["max_live_workers"] == MAX_LIVE_HOSTED_EMBEDDING_WORKERS
    assert resources["live_workers"] <= MAX_LIVE_HOSTED_EMBEDDING_WORKERS


def test_bounded_request_runner_publishes_completion_after_releasing_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = BoundedEmbedderRequestRunner()
    completion_published = threading.Event()
    release_completion = threading.Event()
    original_succeed = request_runner_module._Job.succeed

    def pause_after_publish(job: Any, value: Any) -> None:
        original_succeed(job, value)
        if value == "first":
            completion_published.set()
            release_completion.wait(timeout=5)

    monkeypatch.setattr(request_runner_module._Job, "succeed", pause_after_publish)

    try:
        assert runner.run(lambda: "first", timeout=1.0) == "first"
        assert completion_published.wait(timeout=1.0)

        results: list[str] = []
        errors: list[BaseException] = []

        def submit_second() -> None:
            try:
                results.append(runner.run(lambda: "second", timeout=1.0))
            except BaseException as exc:  # pragma: no cover - assertion payload
                errors.append(exc)

        caller = threading.Thread(target=submit_second)
        caller.start()
        time.sleep(0.05)
        release_completion.set()
        caller.join(timeout=2)

        assert caller.is_alive() is False
        assert errors == []
        assert results == ["second"]
    finally:
        release_completion.set()
        runner.shutdown()
        _wait_until_worker_exit(runner)


def test_bounded_request_runner_does_not_grow_workers_or_queue() -> None:
    runner = BoundedEmbedderRequestRunner()
    entered = threading.Event()
    release = threading.Event()

    def hang() -> str:
        entered.set()
        release.wait(timeout=5)
        return "ok"

    first_errors: list[BaseException] = []
    worker = threading.Thread(
        target=_call_expecting_timeout,
        args=(lambda: runner.run(hang, timeout=0.15), first_errors),
    )
    worker.start()
    try:
        assert entered.wait(timeout=1)
        worker.join(timeout=2)
        assert worker.is_alive() is False
        assert first_errors and isinstance(first_errors[0], EmbedderRequestDeadlineError)
        for _ in range(20):
            started = time.monotonic()
            with pytest.raises(InFlightEmbedderRequestError):
                runner.run(lambda: "queued", timeout=1.0)
            assert time.monotonic() - started < 0.15
        snapshot = runner.snapshot()
        assert snapshot["max_in_flight"] == 1
        assert snapshot["max_queued"] == 0
        assert snapshot["in_flight"] == 1
        assert snapshot["queued"] == 0
        assert snapshot["workers"] == 1
        assert snapshot["max_live_workers"] == MAX_LIVE_HOSTED_EMBEDDING_WORKERS
        assert snapshot["live_workers"] <= MAX_LIVE_HOSTED_EMBEDDING_WORKERS
        assert snapshot["live_workers"] >= 1
        assert len(_request_threads(runner.thread_name)) == 1
        started = time.monotonic()
        runner.shutdown()
        assert time.monotonic() - started < 0.15
        assert runner.snapshot()["in_flight"] == 1
        assert runner.accepting is False
    finally:
        release.set()
        worker.join(timeout=2)
        _wait_until(
            lambda: runner.snapshot()["in_flight"] == 0,
            message=f"runner slot stayed occupied: {runner.snapshot()}",
        )
        _wait_until_worker_exit(runner)


def test_many_timeouts_do_not_grow_embedder_request_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class HangingAPI:
        def create(self, **_kwargs: Any) -> Any:
            entered.set()
            release.wait(timeout=5)
            return _good_response()

    class HangingClient:
        embeddings = HangingAPI()

    monkeypatch.setattr(
        OpenAICompatibleEmbedder,
        "_client_or_raise",
        lambda self: HangingClient(),
    )
    embedder = _openai_embedder(
        query_timeout_seconds=0.15,
        connection_retry_delays=[],
    )
    runner_name = embedder._request_runner.thread_name

    first_errors: list[BaseException] = []
    first = threading.Thread(
        target=_call_expecting_timeout,
        args=(lambda: embedder.embed_query("stuck"), first_errors),
    )
    first.start()
    try:
        assert entered.wait(timeout=1)
        first.join(timeout=2)
        assert first_errors and "query embedding exceeded" in str(first_errors[0])
        close_names_before = [
            thread.name
            for thread in threading.enumerate()
            if "embedder-close" in thread.name
        ]
        for index in range(20):
            started = time.monotonic()
            with pytest.raises(TimeoutError, match="already in flight"):
                embedder.embed_query(f"later-{index}")
            assert time.monotonic() - started < 0.15
        resources = embedder.request_resources()
        assert resources["max_in_flight"] == 1
        assert resources["max_queued"] == 0
        assert resources["in_flight"] == 1
        assert resources["queued"] == 0
        assert resources["workers"] == 1
        assert resources["max_live_workers"] == MAX_LIVE_HOSTED_EMBEDDING_WORKERS
        assert resources["live_workers"] <= MAX_LIVE_HOSTED_EMBEDDING_WORKERS
        assert len(_request_threads(runner_name)) == 1
        assert [
            thread.name
            for thread in threading.enumerate()
            if "embedder-close" in thread.name
        ] == close_names_before
    finally:
        release.set()
        first.join(timeout=2)
        _wait_until_idle(embedder)


def test_later_calls_fail_fast_and_recover_after_stuck_request_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class RecoveringAPI:
        def create(self, **_kwargs: Any) -> Any:
            if not entered.is_set():
                entered.set()
                release.wait(timeout=5)
            return _good_response()

    class RecoveringClient:
        embeddings = RecoveringAPI()

    monkeypatch.setattr(
        OpenAICompatibleEmbedder,
        "_client_or_raise",
        lambda self: RecoveringClient(),
    )
    embedder = _openai_embedder(
        query_timeout_seconds=0.15,
        connection_retry_delays=[],
    )

    first_errors: list[BaseException] = []
    first = threading.Thread(
        target=_call_expecting_timeout,
        args=(lambda: embedder.embed_query("stuck"), first_errors),
    )
    first.start()
    try:
        assert entered.wait(timeout=1)
        first.join(timeout=2)
        assert first_errors and "query embedding exceeded" in str(first_errors[0])
        assert embedder._closed is False
        assert embedder._request_runner.accepting is True
        with pytest.raises(TimeoutError, match="already in flight"):
            embedder.embed_query("blocked")
        assert embedder.request_resources()["in_flight"] == 1
    finally:
        release.set()
        first.join(timeout=2)
    _wait_until_idle(embedder)
    assert embedder.embed_query("recovered") == [0.1, 0.2, 0.3]
    assert embedder.request_resources()["in_flight"] == 0
    assert embedder.request_resources()["queued"] == 0
    assert embedder._closed is False


def test_sdk_constructor_and_create_cannot_drop_timeout_or_max_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openai_calls: list[set[str]] = []
    http_calls: list[set[str]] = []

    class RejectingHttpClient:
        def __init__(self, **kwargs: Any) -> None:
            http_calls.append(set(kwargs))
            if "timeout" in kwargs:
                raise TypeError("unexpected timeout")

    class RejectingHttpx:
        class Timeout:
            def __init__(self, **_kwargs: Any) -> None:
                return None

        Client = RejectingHttpClient

    class RejectingOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            openai_calls.append(set(kwargs))
            if "timeout" in kwargs or "max_retries" in kwargs:
                raise TypeError("unexpected timeout or max_retries")

    monkeypatch.setattr(embedders_module, "_httpx", RejectingHttpx)
    monkeypatch.setattr(embedders_module, "OpenAI", RejectingOpenAI)
    monkeypatch.setattr(embedders_module, "DefaultHttpxClient", RejectingHttpx.Client)

    embedder = _openai_embedder()
    with pytest.raises(RuntimeError, match="fail closed as unsupported"):
        embedder._client_or_raise()
    assert http_calls == [{"follow_redirects", "event_hooks", "timeout"}]
    assert openai_calls == []

    class AcceptingHttpClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = dict(kwargs)

        def close(self) -> None:
            return None

    class AcceptingHttpx:
        class Timeout:
            def __init__(self, **_kwargs: Any) -> None:
                return None

        Client = AcceptingHttpClient

    class DroppingOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            openai_calls.append(set(kwargs))
            if "timeout" in kwargs:
                raise TypeError("unexpected timeout")
            if "max_retries" in kwargs:
                raise TypeError("unexpected max_retries")
            self.embeddings = object()

    openai_calls.clear()
    monkeypatch.setattr(embedders_module, "_httpx", AcceptingHttpx)
    monkeypatch.setattr(embedders_module, "OpenAI", DroppingOpenAI)
    monkeypatch.setattr(embedders_module, "DefaultHttpxClient", AcceptingHttpx.Client)
    embedder = _openai_embedder()
    with pytest.raises(RuntimeError, match="OpenAI client rejected required timeout"):
        embedder._client_or_raise()
    assert len(openai_calls) == 1
    assert {"timeout", "max_retries"} <= openai_calls[0]

    class TimeoutRejectingAPI:
        def create(self, **kwargs: Any) -> Any:
            if "timeout" in kwargs:
                raise TypeError("unexpected timeout")
            return _good_response()

    class TimeoutRejectingClient:
        embeddings = TimeoutRejectingAPI()

    monkeypatch.setattr(
        OpenAICompatibleEmbedder,
        "_client_or_raise",
        lambda self: TimeoutRejectingClient(),
    )
    embedder = _openai_embedder(connection_retry_delays=[])
    with pytest.raises(RuntimeError, match="embeddings.create rejected"):
        embedder.embed_texts(["alpha"])


def test_per_attempt_transport_timeout_is_capped_by_remaining_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[float] = []

    class CapturingAPI:
        def create(self, **kwargs: Any) -> Any:
            captured.append(_timeout_seconds(kwargs["timeout"]))
            raise ConnectionRefusedError("synthetic retry")

    class CapturingClient:
        embeddings = CapturingAPI()

    sleeps: list[float] = []
    real_sleep = time.sleep

    def record_sleep(delay: float) -> None:
        sleeps.append(delay)
        real_sleep(delay)

    monkeypatch.setattr(
        OpenAICompatibleEmbedder,
        "_client_or_raise",
        lambda self: CapturingClient(),
    )
    monkeypatch.setattr("scope_recall.embedders.time.sleep", record_sleep)
    embedder = _openai_embedder(
        connection_retry_delays=[0.2],
        read_timeout_seconds=15.0,
        writer_timeout_seconds=1.0,
        query_timeout_seconds=0.2,
    )

    with pytest.raises(ConnectionRefusedError):
        embedder.embed_texts(["alpha"])

    assert sleeps == [0.2]
    assert len(captured) == 2
    assert captured[0] <= 1.0
    assert captured[1] <= captured[0]
    assert captured[1] <= 0.85
    assert all(value < 15.0 for value in captured)


def test_embedder_shutdown_does_not_wait_on_stuck_request_or_spawn_close_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class HangingAPI:
        def create(self, **_kwargs: Any) -> Any:
            entered.set()
            release.wait(timeout=5)
            return _good_response()

    class HangingClient:
        embeddings = HangingAPI()

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        OpenAICompatibleEmbedder,
        "_client_or_raise",
        lambda self: HangingClient(),
    )
    embedder = _openai_embedder(
        query_timeout_seconds=0.15,
        connection_retry_delays=[],
    )

    first_errors: list[BaseException] = []
    first = threading.Thread(
        target=_call_expecting_timeout,
        args=(lambda: embedder.embed_query("stuck"), first_errors),
    )
    first.start()
    try:
        assert entered.wait(timeout=1)
        first.join(timeout=2)
        assert first_errors and "query embedding exceeded" in str(first_errors[0])
        started = time.monotonic()
        embedder.close()
        close_embedder(embedder)
        embedder.close()
        elapsed = time.monotonic() - started
        assert elapsed < 0.2
        assert embedder.request_resources()["in_flight"] == 1
        assert embedder.request_resources()["workers"] == 1
        assert embedder._closed is True
        assert embedder._request_runner.accepting is False
        assert not any(
            "embedder-close" in thread.name for thread in threading.enumerate()
        )
        with pytest.raises(RuntimeError, match="embedder is closed"):
            embedder.embed_query("after-close")
        started = time.monotonic()
        embedder._request_runner.shutdown()
        assert time.monotonic() - started < 0.15
    finally:
        release.set()
        first.join(timeout=2)
        _wait_until_idle(embedder)
        _wait_until_worker_exit(embedder)


def test_close_after_completed_request_exits_idle_worker_and_rejects_reopen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FastAPI:
        def create(self, **_kwargs: Any) -> Any:
            return _good_response()

    class FastClient:
        embeddings = FastAPI()

    monkeypatch.setattr(
        OpenAICompatibleEmbedder,
        "_client_or_raise",
        lambda self: FastClient(),
    )
    embedder = _openai_embedder(connection_retry_delays=[])
    assert embedder.embed_query("done") == [0.1, 0.2, 0.3]
    assert embedder.request_resources()["in_flight"] == 0
    assert embedder.request_resources()["workers"] == 1

    started = time.monotonic()
    embedder.close()
    assert time.monotonic() - started < 0.2
    _wait_until_worker_exit(embedder)
    assert embedder._closed is True
    assert embedder._request_runner.accepting is False
    assert embedder.request_resources()["workers"] == 0
    with pytest.raises(RuntimeError, match="embedder is closed"):
        embedder.embed_query("reopen")
    with pytest.raises(EmbedderRequestClosedError):
        embedder._request_runner.run(lambda: "nope", timeout=0.2)
    embedder.close()


def test_transport_reset_supports_bounded_retry_without_closing_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    class FlakyAPI:
        def create(self, **kwargs: Any) -> Any:
            nonlocal attempts
            attempts += 1
            assert "timeout" in kwargs
            if attempts < 3:
                raise ConnectionRefusedError("synthetic retry")
            return _good_response()

    class FlakyClient:
        embeddings = FlakyAPI()

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        OpenAICompatibleEmbedder,
        "_client_or_raise",
        lambda self: FlakyClient(),
    )
    sleeps: list[float] = []
    monkeypatch.setattr("scope_recall.embedders.time.sleep", sleeps.append)
    embedder = _openai_embedder(connection_retry_delays=[0.05, 0.05])

    assert embedder.embed_texts(["alpha"]) == [[0.1, 0.2, 0.3]]
    assert attempts == 3
    assert sleeps == [0.05, 0.05]
    assert embedder._closed is False
    assert embedder._request_runner.accepting is True
    assert embedder.request_resources()["workers"] == 1
    assert embedder.embed_query("still-open") == [0.1, 0.2, 0.3]


def test_repeated_construct_use_close_does_not_accumulate_idle_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FastAPI:
        def create(self, **_kwargs: Any) -> Any:
            return _good_response()

    class FastClient:
        embeddings = FastAPI()

    monkeypatch.setattr(
        OpenAICompatibleEmbedder,
        "_client_or_raise",
        lambda self: FastClient(),
    )
    baseline = hosted_embedding_worker_budget()["live_workers"]
    assert baseline == 0, f"leaked hosted workers before reuse test: {hosted_embedding_worker_budget()}"
    for index in range(6):
        embedder = _openai_embedder(connection_retry_delays=[])
        assert embedder.embed_query(f"row-{index}") == [0.1, 0.2, 0.3]
        assert embedder.request_resources()["workers"] == 1
        assert hosted_embedding_worker_budget()["live_workers"] == baseline + 1
        embedder.close()
        _wait_until_worker_exit(embedder)
        assert hosted_embedding_worker_budget()["live_workers"] == baseline
    assert hosted_embedding_worker_budget()["live_workers"] == baseline
    assert hosted_embedding_worker_budget()["live_workers"] <= MAX_LIVE_HOSTED_EMBEDDING_WORKERS


def test_plugin_wide_worker_cap_blocks_reinit_until_stuck_permits_recover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = 0
    started_guard = threading.Lock()
    release = threading.Event()

    class HangingAPI:
        def create(self, **_kwargs: Any) -> Any:
            nonlocal started
            with started_guard:
                started += 1
            release.wait(timeout=10)
            return _good_response()

    class HangingClient:
        embeddings = HangingAPI()

    monkeypatch.setattr(
        OpenAICompatibleEmbedder,
        "_client_or_raise",
        lambda self: HangingClient(),
    )
    baseline = hosted_embedding_worker_budget()["live_workers"]
    assert baseline == 0, f"leaked hosted workers before cap test: {hosted_embedding_worker_budget()}"
    stuck_embedders: list[OpenAICompatibleEmbedder] = []
    stuck_errors: list[list[BaseException]] = []
    stuck_threads: list[threading.Thread] = []

    def _start_stuck(label: str) -> None:
        embedder = _openai_embedder(
            query_timeout_seconds=0.15,
            connection_retry_delays=[],
        )
        stuck_embedders.append(embedder)
        errors: list[BaseException] = []
        stuck_errors.append(errors)
        thread = threading.Thread(
            target=_call_expecting_timeout,
            args=(lambda: embedder.embed_query(label), errors),
        )
        stuck_threads.append(thread)
        thread.start()

    try:
        for index in range(MAX_LIVE_HOSTED_EMBEDDING_WORKERS):
            before = started
            _start_stuck(f"stuck-{index}")
            _wait_until(
                lambda: started == before + 1,
                message=f"stuck worker {index} did not enter the hosted call",
            )
            stuck_threads[-1].join(timeout=2)
            assert stuck_errors[-1] and "query embedding exceeded" in str(
                stuck_errors[-1][0]
            )

        budget = hosted_embedding_worker_budget()
        assert budget["max_live_workers"] == MAX_LIVE_HOSTED_EMBEDDING_WORKERS
        assert budget["live_workers"] == baseline + MAX_LIVE_HOSTED_EMBEDDING_WORKERS
        assert started == MAX_LIVE_HOSTED_EMBEDDING_WORKERS

        for index in range(3):
            extra = _openai_embedder(
                query_timeout_seconds=0.2,
                connection_retry_delays=[],
            )
            started_at = time.monotonic()
            with pytest.raises(TimeoutError, match="worker limit is exhausted"):
                extra.embed_query(f"excess-{index}")
            assert time.monotonic() - started_at < 0.15
            with pytest.raises(HostedEmbedderWorkerLimitError):
                extra._request_runner.run(lambda: "nope", timeout=0.2)
            extra.close()

        assert started == MAX_LIVE_HOSTED_EMBEDDING_WORKERS
        assert (
            hosted_embedding_worker_budget()["live_workers"]
            == baseline + MAX_LIVE_HOSTED_EMBEDDING_WORKERS
        )

        for embedder in stuck_embedders:
            embedder.close()
        assert (
            hosted_embedding_worker_budget()["live_workers"]
            == baseline + MAX_LIVE_HOSTED_EMBEDDING_WORKERS
        )
    finally:
        release.set()
        for thread in stuck_threads:
            thread.join(timeout=2)
        for embedder in stuck_embedders:
            embedder.close()
        _wait_until_budget(live_workers=baseline)

    recovered = _openai_embedder(connection_retry_delays=[])
    assert recovered.embed_query("recovered-slot") == [0.1, 0.2, 0.3]
    recovered.close()
    _wait_until_worker_exit(recovered)
    _wait_until_budget(live_workers=baseline)
