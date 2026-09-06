"""Regressions for request-budget propagation and zero-valued ranking weights.

These tests exercise the ordinary search/prefetch call path with temporary
fixtures only. They reuse PR70 helper/embedding timeout seams instead of
inventing a second I/O stack.
"""

from __future__ import annotations

import ast
import inspect
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfoNotFoundError

import pytest

import scope_recall.provider as provider_module
import scope_recall.recall as recall_module
from scope_recall.models import RecallItem
from scope_recall.provider import ScopeRecallMemoryProvider
from scope_recall.recall import RecallService
from scope_recall.temporal_query import TemporalQueryError


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _item(
    memory_id: str,
    *,
    content: str,
    lifecycle: str = "promoted",
    importance: float = 1.0,
    lexical_score: float = 0.9,
) -> RecallItem:
    return RecallItem(
        id=memory_id,
        content=content,
        summary=content,
        source="tool-store",
        target="user",
        score=lexical_score,
        updated_at="2026-07-14T00:00:00+00:00",
        metadata={
            "lifecycle": lifecycle,
            "memory_type": "factual",
            "scope_id": "scope-a",
            "lexical_score": lexical_score,
            "vector_score": 0.0,
            "importance": importance,
        },
    )


class _SearchProvider:
    def __init__(self, items: list[RecallItem]) -> None:
        self._retrieval_config = {
            "mode": "lexical",
            "include_general": "same-scope",
            "entity_scope_filter_enabled": False,
            "min_score": 0.0,
            "metadata_weight": 0.08,
            "entity_weight": 0.06,
            "entity_distance_weight": 0.04,
        }
        self._vector_config = {}
        self._config = {
            "auto_recall": True,
            "auto_recall_min_length": 1,
            "auto_recall_min_repeated": 0,
            "query_char_limit": 1000,
            "temporal_queries": {"enabled": False},
            "vector": {"embedder": {"query_timeout_seconds": 8.0}},
            "experience": {"enabled": False, "prefetch_enabled": False},
        }
        self._scope = SimpleNamespace(agent_context="primary")
        self._scope_id = "scope-a"
        self._shared_scope_id = "scope-a"
        self._accessible_scope_ids = ["scope-a"]
        self._lock = threading.RLock()
        self._items = list(items)
        self._vector_items: list[RecallItem] = []
        self._last_recall_turns: dict[str, int] = {}
        self._current_turn = 1
        self.search_calls = 0
        self.vector_calls = 0
        self.rollback_contexts: list[str] = []
        self.truth_writes = 0

    def _search_db_memories(self, query: str, *, limit: int) -> list[RecallItem]:
        self.search_calls += 1
        del query
        return list(self._items)[:limit]

    def _search_vector_memories(self, query: str, *, limit: int) -> list[RecallItem]:
        self.vector_calls += 1
        del query
        return list(self._vector_items)[:limit]

    def _search_vector_memories_with_vector(
        self, query_vector: list[float], *, limit: int
    ) -> list[RecallItem]:
        del query_vector
        return self._search_vector_memories("", limit=limit)

    def _search_curated_memories(self, query: str) -> list[RecallItem]:
        del query
        return []

    def _dedup_key(self, content: str) -> str:
        return str(content).strip().casefold()

    def _config_value(self, key: str, default):
        return self._config.get(key, default)

    def _normalize_query(self, query: str, limit: int) -> str:
        return str(query)[:limit]

    def _require_conn(self) -> sqlite3.Connection:
        raise AssertionError("ordinary query fixtures must not open a live home database")

    def _rollback_conn_after_error(self, context: str) -> None:
        self.rollback_contexts.append(context)

    def recall_service_view(self) -> RecallService:
        return RecallService(self)

    def recall_limit(self) -> int:
        return 5

    def _mark_recalled(self, memory_ids: list[str]) -> None:
        del memory_ids


def test_zero_metadata_and_entity_weights_are_honored() -> None:
    provider = _SearchProvider(
        [_item("memory-lex", content="Project Atlas deploy command is uv run app.")]
    )
    provider._retrieval_config["metadata_weight"] = 0
    provider._retrieval_config["entity_weight"] = 0.0
    provider._retrieval_config["entity_distance_weight"] = 0

    results = RecallService(provider).search_memories(
        "Project Atlas deploy command",
        limit=5,
    )

    assert [item.id for item in results] == ["memory-lex"]
    meta = results[0].metadata or {}
    assert meta["metadata_weight"] == 0.0
    assert meta["entity_overlap_bonus"] == 0.0
    assert meta["entity_distance_weight"] == 0.0
    assert meta["quality_weight_applied"] == 0.0


def test_missing_weights_keep_packaged_defaults() -> None:
    provider = _SearchProvider(
        [_item("memory-lex", content="Project Atlas deploy command is uv run app.")]
    )
    provider._retrieval_config.pop("metadata_weight", None)
    provider._retrieval_config.pop("entity_weight", None)
    provider._retrieval_config.pop("entity_distance_weight", None)

    results = RecallService(provider).search_memories(
        "Project Atlas deploy command",
        limit=5,
    )

    meta = results[0].metadata or {}
    assert meta["metadata_weight"] == 0.08
    assert meta["entity_distance_weight"] == 0.04


@pytest.mark.parametrize(
    "key, value",
    [
        ("metadata_weight", True),
        ("entity_weight", float("nan")),
        ("entity_distance_weight", float("inf")),
        ("metadata_weight", -0.1),
        ("entity_weight", "abc"),
        ("entity_distance_weight", 1.5),
    ],
)
def test_invalid_ranking_weights_are_rejected(key: str, value: object) -> None:
    provider = _SearchProvider(
        [_item("memory-lex", content="Project Atlas deploy command is uv run app.")]
    )
    provider._retrieval_config[key] = value

    with pytest.raises(ValueError, match=key):
        RecallService(provider).search_memories("Project Atlas deploy command", limit=5)


def test_doctor_effective_weights_match_runtime_zero() -> None:
    provider = _SearchProvider(
        [_item("memory-lex", content="Project Atlas deploy command is uv run app.")]
    )
    provider._retrieval_config["metadata_weight"] = 0
    provider._retrieval_config["entity_weight"] = 0.0
    provider._retrieval_config["entity_distance_weight"] = 0
    host = ScopeRecallMemoryProvider()
    host._retrieval_config = dict(provider._retrieval_config)

    results = RecallService(provider).search_memories(
        "Project Atlas deploy command",
        limit=5,
    )
    reported = host.retrieval_status_view()["config"]
    meta = results[0].metadata or {}
    assert reported["metadata_weight"] == meta["metadata_weight"] == 0.0
    assert reported["entity_weight"] == 0.0
    assert reported["entity_distance_weight"] == meta["entity_distance_weight"] == 0.0


def test_unrelated_random_query_stays_empty() -> None:
    provider = _SearchProvider([])
    service = RecallService(provider)
    results = service.search_memories("qzxvbnmkjhgfdspoiuytrewq", limit=5)
    assert results == []
    assert service.last_funnel_trace["filters"]["no_admissible_evidence"] == 1


def test_candidate_lifecycle_stays_hidden() -> None:
    provider = _SearchProvider(
        [
            _item(
                "memory-candidate",
                content="Project Atlas deploy command is uv run app.",
                lifecycle="candidate",
            )
        ]
    )
    results = RecallService(provider).search_memories(
        "Project Atlas deploy command",
        limit=5,
    )
    assert results == []


def test_temporal_authority_failure_keeps_lexical_and_does_not_mark_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_temporal_recall_integration import TemporalRecallProvider, _memory

    provider = TemporalRecallProvider(enabled=True, db_items=[])
    try:
        lexical = _memory(
            provider,
            "memory-lexical",
            content="Project Atlas deploy command is uv run app.",
        )

        def unavailable(*_args, **_kwargs):
            raise TemporalQueryError("unknown timezone: America/New_York")

        monkeypatch.setattr(
            recall_module,
            "query_temporal_memory_precedence",
            unavailable,
        )
        service = RecallService(provider)
        results = service.search_memories("Project Atlas deploy command", limit=5)

        assert [item.id for item in results] == [lexical.id]
        assert not (results[0].metadata or {}).get("temporal_fact_current")
        assert not (results[0].metadata or {}).get("temporal_authoritative")
        diagnostics = service.last_temporal_query_diagnostics
        assert diagnostics["state"] == "unavailable"
        assert diagnostics["current_claims_usable"] is False
        assert diagnostics["reason_code"] == "temporal_timezone_unavailable"
        assert service.last_funnel_trace["stages"]["temporal_current"]["state"] == "unavailable"
    finally:
        provider.close()


def test_temporal_integrity_failure_is_not_fail_soft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_temporal_recall_integration import TemporalRecallProvider, _memory

    provider = TemporalRecallProvider(enabled=True, db_items=[])
    try:
        _memory(
            provider,
            "memory-lexical",
            content="Project Atlas deploy command is uv run app.",
        )

        def corrupt(*_args, **_kwargs):
            raise sqlite3.DatabaseError("disk image is malformed")

        monkeypatch.setattr(
            recall_module,
            "query_temporal_memory_precedence",
            corrupt,
        )
        with pytest.raises(sqlite3.DatabaseError, match="malformed"):
            RecallService(provider).search_memories(
                "Project Atlas deploy command",
                limit=5,
            )
    finally:
        provider.close()


def test_timezone_database_gap_is_temporal_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_temporal_recall_integration import TemporalRecallProvider, _memory

    provider = TemporalRecallProvider(enabled=True, db_items=[])
    try:
        lexical = _memory(
            provider,
            "memory-lexical",
            content="Project Atlas deploy command is uv run app.",
        )

        def missing_zone(*_args, **_kwargs):
            raise ZoneInfoNotFoundError("America/New_York")

        monkeypatch.setattr(
            recall_module,
            "query_temporal_memory_precedence",
            missing_zone,
        )
        service = RecallService(provider)
        results = service.search_memories("Project Atlas deploy command", limit=5)
        assert [item.id for item in results] == [lexical.id]
        assert not (results[0].metadata or {}).get("temporal_fact_current")
        assert service.last_temporal_query_diagnostics["state"] == "unavailable"
        assert service.last_temporal_query_diagnostics["reason_code"] == (
            "temporal_timezone_unavailable"
        )
        assert service.last_temporal_query_diagnostics["current_claims_usable"] is False
    finally:
        provider.close()


def test_provider_lock_contention_returns_without_rollback_or_hang() -> None:
    from scope_recall._internal.recall.deadline import (
        RequestDeadline,
        bind_request_deadline,
        reset_request_deadline,
    )

    provider = _SearchProvider(
        [_item("memory-lex", content="Project Atlas deploy command is uv run app.")]
    )
    started = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with provider._lock:
            started.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock, name="recall-lock-holder")
    holder.start()
    assert started.wait(timeout=1)
    service = RecallService(provider)
    token = bind_request_deadline(RequestDeadline.from_budget(0.15))
    try:
        began = time.monotonic()
        results = service.search_memories(
            "Project Atlas deploy command",
            limit=5,
        )
        elapsed = time.monotonic() - began
    finally:
        reset_request_deadline(token)
        release.set()
        holder.join(timeout=2)

    assert elapsed < 1.5
    assert results == []
    assert provider.rollback_contexts == []
    trace = service.last_funnel_trace
    assert any(
        entry.get("source") == "lexical"
        for entry in (trace.get("source_unavailable") or [])
    )

    recovered = service.search_memories(
        "Project Atlas deploy command",
        limit=5,
    )
    assert [item.id for item in recovered] == ["memory-lex"]


def test_slow_embedding_is_vector_unavailable_and_keeps_lexical() -> None:
    from scope_recall._internal.recall.deadline import (
        RequestDeadline,
        bind_request_deadline,
        reset_request_deadline,
    )

    provider = _SearchProvider(
        [_item("memory-lex", content="Project Atlas deploy command is uv run app.")]
    )
    entered = threading.Event()
    release = threading.Event()

    def slow_vector(query: str, *, limit: int) -> list[RecallItem]:
        del query, limit
        from scope_recall._internal.recall.deadline import remaining_seconds

        remaining = remaining_seconds()
        assert remaining is not None
        entered.set()
        raise TimeoutError("query embedding exceeded the request budget")

    provider._search_vector_memories = slow_vector  # type: ignore[method-assign]
    service = RecallService(provider)
    token = bind_request_deadline(RequestDeadline.from_budget(0.2))
    try:
        began = time.monotonic()
        results = service.search_memories(
            "Project Atlas deploy command",
            limit=5,
        )
        elapsed = time.monotonic() - began
    finally:
        release.set()
        reset_request_deadline(token)

    assert [item.id for item in results] == ["memory-lex"]
    assert elapsed < 1.5
    assert entered.is_set()
    assert any(
        entry.get("source") == "vector"
        for entry in (service.last_funnel_trace.get("source_unavailable") or [])
    )


def test_remaining_does_not_exceed_budget_when_monotonic_does_not_advance() -> None:
    from scope_recall._internal.recall.deadline import (
        EXPERIENCE_MIN_REMAINING_SECONDS,
        RequestDeadline,
    )

    for origin in (1.0, 65536.0):
        deadline = RequestDeadline.from_budget(
            EXPERIENCE_MIN_REMAINING_SECONDS,
            now=origin,
        )
        leftover = deadline.remaining(now=origin)
        assert leftover <= deadline.budget_seconds
        assert leftover <= EXPERIENCE_MIN_REMAINING_SECONDS
        assert leftover > 0.0
        assert deadline.exhausted(now=origin) is False


def test_remaining_keeps_negative_expiry_and_absolute_factory() -> None:
    from scope_recall._internal.recall.deadline import RequestDeadline

    expired = RequestDeadline.from_budget(0.05, now=65536.0)
    leftover = expired.remaining(now=65536.0 + 1.0)
    assert leftover < 0.0
    assert expired.exhausted(now=65536.0 + 1.0) is True

    absolute = RequestDeadline.from_absolute(100.0, now=90.0)
    assert absolute.budget_seconds == 10.0
    assert absolute.deadline_monotonic == 100.0
    assert absolute.remaining(now=90.0) == 10.0
    assert absolute.remaining(now=110.0) == -10.0
    assert absolute.exhausted(now=110.0) is True

    already_past = RequestDeadline.from_absolute(50.0, now=60.0)
    assert already_past.budget_seconds == 0.0
    assert already_past.remaining(now=60.0) < 0.0
    assert already_past.exhausted(now=60.0) is True


def test_request_deadline_context_stays_isolated() -> None:
    from scope_recall._internal.recall.deadline import (
        RequestDeadline,
        current_request_deadline,
        remaining_seconds,
        using_request_deadline,
    )

    outer = RequestDeadline.from_budget(0.05, now=65536.0)
    inner = RequestDeadline.from_absolute(100.0, now=90.0)
    assert current_request_deadline() is None
    assert remaining_seconds(now=65536.0) is None
    with using_request_deadline(outer):
        assert current_request_deadline() is outer
        assert remaining_seconds(now=65536.0) <= outer.budget_seconds
        with using_request_deadline(inner):
            assert current_request_deadline() is inner
            assert remaining_seconds(now=90.0) == inner.budget_seconds
        assert current_request_deadline() is outer
    assert current_request_deadline() is None
    assert remaining_seconds() is None


def test_prefetch_skips_slow_experience_and_does_not_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scope_recall._internal.recall import deadline as deadline_module
    from scope_recall._internal.recall import prefetch as prefetch_module

    monkeypatch.setattr(
        deadline_module,
        "time",
        SimpleNamespace(monotonic=lambda: 65536.0),
    )
    provider = _SearchProvider(
        [_item("memory-lex", content="Project Atlas deploy command is uv run app.")]
    )
    provider._config["experience"] = {"enabled": True, "prefetch_enabled": True}
    called = {"preflight": 0}

    def fake_render(_provider, query: str) -> str:
        del _provider, query
        return "## Scope Recall Packet\nlocal recall"

    def slow_preflight(_provider, query: str = "") -> dict[str, str]:
        del _provider, query
        called["preflight"] += 1
        time.sleep(2.0)
        return {"packet": "experience-late"}

    monkeypatch.setattr(provider_module, "render_current_turn_recall", fake_render)
    monkeypatch.setattr(provider_module, "run_experience_preflight", slow_preflight)
    provider._config["vector"] = {"embedder": {"query_timeout_seconds": 0.05}}

    began = time.monotonic()
    rendered = prefetch_module.prefetch_prompt(provider, "Project Atlas deploy command")
    elapsed = time.monotonic() - began

    assert "local recall" in rendered
    assert "experience-late" not in rendered
    assert elapsed < 1.5
    assert provider.rollback_contexts == []
    assert called["preflight"] == 0


def test_prefetch_runs_experience_when_remaining_is_above_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scope_recall._internal.recall import deadline as deadline_module
    from scope_recall._internal.recall import prefetch as prefetch_module

    monkeypatch.setattr(
        deadline_module,
        "time",
        SimpleNamespace(monotonic=lambda: 65536.0),
    )
    provider = _SearchProvider(
        [_item("memory-lex", content="Project Atlas deploy command is uv run app.")]
    )
    provider._config["experience"] = {"enabled": True, "prefetch_enabled": True}
    called = {"preflight": 0}

    def fake_render(_provider, query: str) -> str:
        del _provider, query
        return "## Scope Recall Packet\nlocal recall"

    def instant_preflight(_provider, query: str = "") -> dict[str, str]:
        del _provider, query
        called["preflight"] += 1
        return {"packet": "experience-ok"}

    monkeypatch.setattr(provider_module, "render_current_turn_recall", fake_render)
    monkeypatch.setattr(provider_module, "run_experience_preflight", instant_preflight)
    provider._config["vector"] = {"embedder": {"query_timeout_seconds": 8.0}}

    rendered = prefetch_module.prefetch_prompt(provider, "Project Atlas deploy command")

    assert "local recall" in rendered
    assert "experience-ok" in rendered
    assert called["preflight"] == 1
    assert provider.rollback_contexts == []


def test_prefetch_budget_exhaustion_does_not_rollback_shared_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scope_recall._internal.recall import prefetch as prefetch_module
    from scope_recall._internal.recall.deadline import RequestDeadline

    provider = _SearchProvider([])
    provider._config["vector"] = {"embedder": {"query_timeout_seconds": 0.05}}

    def boom(_provider, _query: str) -> str:
        raise TimeoutError("request deadline exhausted")

    monkeypatch.setattr(provider_module, "render_current_turn_recall", boom)
    rendered = prefetch_module.prefetch_prompt(provider, "Project Atlas deploy command")
    assert rendered == ""
    assert provider.rollback_contexts == []
    del RequestDeadline


def test_native_helper_honors_remaining_request_budget(tmp_path, monkeypatch) -> None:
    import scope_recall.lance_process_store as process_store
    from scope_recall._internal.recall.deadline import (
        RequestDeadline,
        bind_request_deadline,
        reset_request_deadline,
    )

    monkeypatch.setattr(
        process_store,
        "_worker_command",
        lambda: [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    store = process_store.ProcessLanceVectorStore(
        tmp_path / "lancedb",
        table_name="memories",
        dimensions=3,
    )
    assert store._request_timeout == 60.0
    before_threads = {
        thread.name
        for thread in threading.enumerate()
        if thread.is_alive()
    }
    token = bind_request_deadline(RequestDeadline.from_budget(0.2))
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="SQLite truth is intact"):
            store.search([1.0, 0.0, 0.0], scope_id="scope-a", limit=3)
        elapsed = time.monotonic() - started
    finally:
        reset_request_deadline(token)
        store.close()
        _wait_lance_helper_drain()

    assert elapsed < 5
    after_threads = {
        thread.name
        for thread in threading.enumerate()
        if thread.is_alive()
        and thread.name.startswith("scope-recall-lance-")
        and thread.name not in before_threads
    }
    assert after_threads == set()


def test_malformed_helper_then_next_request_recovers(tmp_path, monkeypatch) -> None:
    import scope_recall.lance_process_store as process_store

    monkeypatch.setattr(
        process_store,
        "_worker_command",
        lambda: [
            sys.executable,
            "-c",
            "import sys; sys.stdin.buffer.readline(); print('invalid frame', flush=True)",
        ],
    )
    store = process_store.ProcessLanceVectorStore(
        tmp_path / "lancedb",
        table_name="memories",
        dimensions=3,
    )
    with pytest.raises(RuntimeError, match="SQLite truth is intact"):
        store.search([1.0, 0.0, 0.0], scope_id="scope-a", limit=3)
    assert store.requires_reopen is True

    monkeypatch.setattr(
        process_store,
        "_worker_command",
        lambda: [
            sys.executable,
            "-c",
            (
                "import json,sys\n"
                "for _ in range(2):\n"
                "    r=json.loads(sys.stdin.buffer.readline())\n"
                "    result=[] if r.get('method')=='search' else True\n"
                "    print(json.dumps({'id':r['id'],'ok':True,'result':result}), flush=True)\n"
            ),
        ],
    )
    store.open()
    try:
        assert store.search([1.0, 0.0, 0.0], scope_id="scope-a", limit=3) == []
    finally:
        store.close()


def test_disabled_experience_preserves_local_prefetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scope_recall._internal.recall import prefetch as prefetch_module

    provider = _SearchProvider(
        [_item("memory-lex", content="Project Atlas deploy command is uv run app.")]
    )
    provider._config["experience"] = {"enabled": False, "prefetch_enabled": True}

    def fake_render(_provider, query: str) -> str:
        del _provider, query
        return "## Scope Recall Packet\nlocal recall"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled experience must not start preflight")

    monkeypatch.setattr(provider_module, "render_current_turn_recall", fake_render)
    monkeypatch.setattr(provider_module, "run_experience_preflight", forbidden)
    assert prefetch_module.prefetch_prompt(provider, "Project Atlas deploy command") == (
        "## Scope Recall Packet\nlocal recall"
    )


def test_prefetch_reuses_provider_module_hooks() -> None:
    source = (PLUGIN_ROOT / "_internal" / "recall" / "prefetch.py").read_text(
        encoding="utf-8"
    )
    assert "ProviderModuleHooks" in source
    assert "def _provider_modules" not in source
    assert "def _module_attr" not in source


def test_collected_source_closure_has_no_provider_private_access() -> None:
    from scope_recall._internal.recall.sources import collect_sources

    tree = ast.parse(inspect.getsource(collect_sources))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {
                "provider",
                "_retrieval_config",
                "_vector_config",
                "_accessible_scope_ids",
                "_lock",
                "_search_db_memories",
                "_search_vector_memories",
                "_search_curated_memories",
            }


def test_existing_prefetch_monkeypatch_anchor_still_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ScopeRecallMemoryProvider()

    def fail_render(_provider, _query):
        raise RuntimeError("injected recall failure")

    monkeypatch.setattr(provider_module, "render_current_turn_recall", fail_render)
    assert provider.prefetch("new query") == ""


def _wait_lance_helper_drain(*, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    leftover: list[str] = []
    while time.monotonic() < deadline:
        leftover = [
            thread.name
            for thread in threading.enumerate()
            if thread.is_alive() and thread.name.startswith("scope-recall-lance-")
        ]
        if not leftover:
            return
        time.sleep(0.05)
    raise AssertionError(f"native helper threads still alive: {leftover}")


def _good_lance_worker() -> list[str]:
    return [
        sys.executable,
        "-c",
        (
            "import json,sys\n"
            "for _ in range(8):\n"
            "    line=sys.stdin.buffer.readline()\n"
            "    if not line:\n"
            "        break\n"
            "    r=json.loads(line)\n"
            "    method=r.get('method')\n"
            "    if method=='count_rows':\n"
            "        result=0\n"
            "    elif method=='search':\n"
            "        result=[]\n"
            "    else:\n"
            "        result=True\n"
            "    print(json.dumps({'id':r['id'],'ok':True,'result':result}), flush=True)\n"
        ),
    ]


def _exclusive_locked_truth(
    tmp_path: Path,
) -> tuple[sqlite3.Connection, sqlite3.Connection, str]:
    from scope_recall.sql_store import ensure_schema

    db_path = tmp_path / "recall-locked.sqlite3"
    writer = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    writer.row_factory = sqlite3.Row
    writer.execute("PRAGMA busy_timeout=30000")
    ensure_schema(writer)
    memory_id = "memory-lex-locked"
    writer.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES (?, 'scope-a', 'tool-store', 'user', ?, ?,
                  '2026-01-01T00:00:00+00:00',
                  '2026-07-14T00:00:00+00:00', ?)
        """,
        (
            memory_id,
            "Project Atlas deploy command is uv run app.",
            "Project Atlas deploy command is uv run app.",
            json.dumps(
                {
                    "lifecycle": "promoted",
                    "memory_type": "factual",
                    "scope_id": "scope-a",
                },
                sort_keys=True,
            ),
        ),
    )
    writer.execute("BEGIN EXCLUSIVE")
    reader = sqlite3.connect(str(db_path), timeout=10.0)
    reader.row_factory = sqlite3.Row
    reader.execute("PRAGMA busy_timeout=2000")
    probe = sqlite3.connect(str(db_path), timeout=0.1)
    try:
        probe.execute("PRAGMA busy_timeout=50")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            probe.execute(
                "SELECT id FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
    finally:
        probe.close()
    return writer, reader, memory_id


class _FileLexicalProvider(_SearchProvider):
    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__([])
        self._conn = conn
        self._retrieval_config["mode"] = "lexical"
        self._retrieval_config["min_score"] = 0.0
        self._vector_config = {"enabled": False}

    def _require_conn(self) -> sqlite3.Connection:
        return self._conn

    def _search_db_memories(self, query: str, *, limit: int) -> list[RecallItem]:
        from scope_recall.storage_views import search_db_memories

        return search_db_memories(self, query, limit=limit)

    def _search_vector_memories(self, query: str, *, limit: int) -> list[RecallItem]:
        del query, limit
        return []


def test_helper_mutex_acquisition_honors_remaining_budget(tmp_path: Path) -> None:
    import scope_recall.lance_process_store as process_store
    from scope_recall._internal.recall.deadline import (
        RequestDeadline,
        using_request_deadline,
    )

    store = process_store.ProcessLanceVectorStore(
        tmp_path / "lancedb",
        table_name="memories",
        dimensions=3,
    )
    store._closed = True
    held = threading.Event()

    def owner() -> None:
        with store._lock:
            held.set()
            time.sleep(0.4)

    holder = threading.Thread(target=owner, name="recall-helper-lock-owner")
    holder.start()
    assert held.wait(timeout=1)
    started = time.monotonic()
    try:
        with using_request_deadline(RequestDeadline.from_budget(0.05)):
            with pytest.raises(RuntimeError):
                store.count_rows()
        elapsed = time.monotonic() - started
    finally:
        holder.join(timeout=2)

    assert elapsed < 0.2
    assert store._process is None
    assert store._failed is False


def test_held_helper_lock_then_next_request_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scope_recall.lance_process_store as process_store
    from scope_recall._internal.recall.deadline import (
        RequestDeadline,
        using_request_deadline,
    )

    monkeypatch.setattr(process_store, "_worker_command", _good_lance_worker)
    store = process_store.ProcessLanceVectorStore(
        tmp_path / "lancedb",
        table_name="memories",
        dimensions=3,
    )
    starts: list[int] = []
    original_start = store._start

    def tracking_start() -> None:
        starts.append(1)
        original_start()

    store._start = tracking_start  # type: ignore[method-assign]
    held = threading.Event()

    def owner() -> None:
        with store._lock:
            held.set()
            time.sleep(0.4)

    holder = threading.Thread(target=owner, name="recall-helper-lock-owner")
    holder.start()
    assert held.wait(timeout=1)
    began = time.monotonic()
    with using_request_deadline(RequestDeadline.from_budget(0.05)):
        with pytest.raises(RuntimeError):
            store.count_rows()
    elapsed = time.monotonic() - began
    holder.join(timeout=2)
    assert elapsed < 0.2
    assert store._process is None
    assert store._failed is False
    assert starts == []

    assert store.count_rows() == 0
    assert starts == [1]
    store.close()
    _wait_lance_helper_drain()


def test_helper_slow_cleanup_is_owned_reap_not_caller_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scope_recall.lance_process_store as process_store
    from scope_recall._internal.recall.deadline import (
        RequestDeadline,
        using_request_deadline,
    )

    real_stop = process_store._stop_worker
    cleanup_started = threading.Event()

    def slow_stop(process: object) -> None:
        cleanup_started.set()
        time.sleep(0.8)
        real_stop(process)

    monkeypatch.setattr(process_store, "_stop_worker", slow_stop)
    monkeypatch.setattr(
        process_store,
        "_worker_command",
        lambda: [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    store = process_store.ProcessLanceVectorStore(
        tmp_path / "lancedb",
        table_name="memories",
        dimensions=3,
    )
    created: list[object] = []
    original_start = store._start

    def tracking_start() -> None:
        original_start()
        created.append(store._process)

    store._start = tracking_start  # type: ignore[method-assign]
    with using_request_deadline(RequestDeadline.from_budget(0.15)):
        began = time.monotonic()
        with pytest.raises(RuntimeError, match="SQLite truth is intact"):
            store.count_rows()
        elapsed = time.monotonic() - began
    assert elapsed < 0.55
    assert cleanup_started.wait(timeout=2)
    _wait_lance_helper_drain()
    assert created
    assert created[0].poll() is not None  # type: ignore[attr-defined]
    store.close()


def test_helper_request_timeout_does_not_consume_next_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scope_recall.lance_process_store as process_store
    from scope_recall._internal.recall.deadline import (
        RequestDeadline,
        using_request_deadline,
    )

    monkeypatch.setattr(
        process_store,
        "_worker_command",
        lambda: [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    store = process_store.ProcessLanceVectorStore(
        tmp_path / "lancedb",
        table_name="memories",
        dimensions=3,
    )
    with using_request_deadline(RequestDeadline.from_budget(0.15)):
        with pytest.raises(RuntimeError, match="SQLite truth is intact"):
            store.search([1.0, 0.0, 0.0], scope_id="scope-a", limit=3)
    monkeypatch.setattr(process_store, "_worker_command", _good_lance_worker)
    store.open()
    try:
        assert store.search([1.0, 0.0, 0.0], scope_id="scope-a", limit=3) == []
        assert store.count_rows() == 0
    finally:
        store.close()
        _wait_lance_helper_drain()


def test_supplied_query_vector_does_not_keep_stale_last_error() -> None:
    provider = _SearchProvider(
        [_item("memory-lex", content="Project Atlas deploy command is uv run app.")]
    )
    provider._vector_query_last_error = "RuntimeError"
    provider._vector_items = [
        _item("memory-vec", content="Project Atlas deploy command is uv run app.")
    ]
    service = RecallService(provider)
    results = service._search_memories_internal(
        "Project Atlas deploy command",
        limit=5,
        query_vector=[0.1, 0.2, 0.3],
    )
    assert [item.id for item in results] == ["memory-lex"]
    assert service.last_funnel_trace["stages"]["vector"]["state"] == "ok"
    unavailable = service.last_funnel_trace.get("source_unavailable") or []
    assert all(entry.get("source") != "vector" for entry in unavailable)


def test_lexical_sqlite_busy_wait_honors_request_budget(tmp_path: Path) -> None:
    from scope_recall._internal.recall.deadline import (
        RequestDeadline,
        bind_request_deadline,
        reset_request_deadline,
    )

    writer, reader, memory_id = _exclusive_locked_truth(tmp_path)
    provider = _FileLexicalProvider(reader)
    service = RecallService(provider)
    token = bind_request_deadline(RequestDeadline.from_budget(0.15))
    try:
        began = time.monotonic()
        results = service.search_memories(memory_id, limit=5)
        elapsed = time.monotonic() - began
    finally:
        reset_request_deadline(token)

    try:
        assert elapsed < 1.5
        assert results == []
        assert provider.rollback_contexts == []
        assert int(reader.execute("PRAGMA busy_timeout").fetchone()[0]) == 2000
        unavailable = service.last_funnel_trace.get("source_unavailable") or []
        assert any(
            entry.get("source") == "lexical"
            and entry.get("reason_code") == "sqlite_lock_timeout"
            for entry in unavailable
        )
        assert service.last_funnel_trace["stages"]["vector"]["reason_code"] == (
            "authority_read_unavailable"
        )
        writer.execute("COMMIT")
        recovered = service.search_memories(
            "Project Atlas deploy command",
            limit=5,
        )
        assert [item.id for item in recovered] == [memory_id]
    finally:
        try:
            writer.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        writer.close()
        reader.close()


def test_temporal_sqlite_busy_wait_keeps_lexical_and_does_not_mark_current(
    tmp_path: Path,
) -> None:
    from scope_recall._internal.recall.deadline import (
        RequestDeadline,
        bind_request_deadline,
        reset_request_deadline,
    )

    writer, reader, memory_id = _exclusive_locked_truth(tmp_path)
    provider = _SearchProvider(
        [
            _item(
                memory_id,
                content="Project Atlas deploy command is uv run app.",
            )
        ]
    )
    provider._conn = reader
    provider._require_conn = lambda: reader  # type: ignore[method-assign]
    provider._config["temporal_queries"] = {
        "enabled": True,
        "timezone": "UTC",
        "current_limit": 50,
    }
    service = RecallService(provider)
    token = bind_request_deadline(RequestDeadline.from_budget(0.15))
    try:
        began = time.monotonic()
        results = service.search_memories("Project Atlas deploy command", limit=5)
        elapsed = time.monotonic() - began
    finally:
        reset_request_deadline(token)

    try:
        assert elapsed < 1.5
        assert [item.id for item in results] == [memory_id]
        assert not (results[0].metadata or {}).get("temporal_fact_current")
        diagnostics = service.last_temporal_query_diagnostics
        assert diagnostics.get("current_claims_usable") is False
        assert service.last_funnel_trace["stages"]["temporal_current"]["state"] == (
            "unavailable"
        )
        assert int(reader.execute("PRAGMA busy_timeout").fetchone()[0]) == 2000
    finally:
        try:
            writer.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        writer.close()
        reader.close()


def test_experience_preflight_sqlite_wait_honors_request_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scope_recall._internal.experience import runtime as experience_runtime
    from scope_recall._internal.recall.deadline import (
        RequestDeadline,
        using_request_deadline,
    )
    from scope_recall._internal.recall.sources import SourceUnavailable

    writer, reader, _memory_id = _exclusive_locked_truth(tmp_path)

    def locked_read(conn: sqlite3.Connection, **_kwargs: object) -> dict[str, str]:
        conn.execute("SELECT id FROM memories LIMIT 1").fetchall()
        return {"packet": "late-experience"}

    monkeypatch.setattr(
        "scope_recall.experience_preflight.experience_preflight",
        locked_read,
    )
    provider = _SearchProvider([])
    provider._require_conn = lambda: reader  # type: ignore[method-assign]
    try:
        began = time.monotonic()
        with using_request_deadline(RequestDeadline.from_budget(0.15)):
            with pytest.raises(SourceUnavailable) as caught:
                experience_runtime.run_experience_preflight(
                    provider, query="Project Atlas"
                )
        elapsed = time.monotonic() - began
        assert elapsed < 1.5
        assert caught.value.reason_code == "sqlite_lock_timeout"
        assert int(reader.execute("PRAGMA busy_timeout").fetchone()[0]) == 2000
    finally:
        try:
            writer.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        writer.close()
        reader.close()


def test_request_busy_timeout_preserves_progress_handler(tmp_path: Path) -> None:
    from scope_recall._internal.recall.deadline import (
        RequestDeadline,
        using_request_deadline,
    )
    from scope_recall.recall_sqlite_budget import using_request_busy_timeout

    conn = sqlite3.connect(str(tmp_path / "progress.sqlite3"))
    try:
        conn.execute("CREATE TABLE items (id INTEGER)")
        conn.execute("INSERT INTO items VALUES (1)")
        conn.commit()
        seen = {"n": 0}

        def handler() -> int:
            seen["n"] += 1
            return 0

        conn.set_progress_handler(handler, 1)
        with using_request_deadline(RequestDeadline.from_budget(1.0)):
            with using_request_busy_timeout(conn):
                conn.execute("SELECT id FROM items").fetchall()
        conn.execute("SELECT id FROM items").fetchall()
        conn.set_progress_handler(None, 0)
        assert seen["n"] > 0
    finally:
        conn.close()


def test_lexical_integrity_error_is_not_fail_soft() -> None:
    provider = _SearchProvider(
        [_item("memory-lex", content="Project Atlas deploy command is uv run app.")]
    )

    def corrupt(query: str, *, limit: int) -> list[RecallItem]:
        del query, limit
        raise sqlite3.DatabaseError("disk image is malformed")

    provider._search_db_memories = corrupt  # type: ignore[method-assign]
    with pytest.raises(sqlite3.DatabaseError, match="malformed"):
        RecallService(provider).search_memories(
            "Project Atlas deploy command",
            limit=5,
        )
