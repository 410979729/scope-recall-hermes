"""Deadline-safe fan-out regressions for the Lance vector adapter."""
from __future__ import annotations

from dataclasses import replace
import json
import sys
from types import SimpleNamespace

import pytest

from scope_recall.core import deadline as request_deadline
from scope_recall.adapters.lance import LanceVectorPort
from scope_recall.core.recall_policy import SPACE_ID
from scope_recall.core.retrieval import SearchContext, SearchLimits
from scope_recall.vector.process_store import ProcessLanceVectorStore
from tests.v11_support import context as trusted_context


class ManualClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SyntheticQueryEmbedding:
    def embed_query(self, text: str, *, remaining_seconds: float) -> list[float]:
        assert text and remaining_seconds > 0
        return [1.0, 0.0]


def _context(tmp_path, clock: ManualClock, *, budget: float) -> SearchContext:
    trusted = replace(
        trusted_context(tmp_path),
        project_id="TEST-project",
        branch_id="TEST-main",
    )
    return SearchContext(
        query="PUBLIC semantic query",
        mode="auto",
        as_of=None,
        focus_refs=(),
        limits=SearchLimits(),
        deadline=clock.monotonic() + budget,
        now="2026-09-15T00:00:00Z",
        trusted_context=trusted,
    )


def _candidate_row(scope_id: str) -> dict[str, object]:
    metadata = {
        "object_kind": "event",
        "object_ref": "TEST-hit",
        "object_revision": 1,
        "vector_id": "TEST-vector",
        "embedding_space": SPACE_ID,
        "agent_id": "TEST-agent",
        "installation_id": "TEST-installation",
        "project_id": None,
        "branch_id": None,
        "logical_scope_id": "TEST-scope",
    }
    return {
        "scope_id": scope_id,
        "target": json.dumps(metadata),
        "score": 0.9,
    }


def _worker_command(*, search_delay: float = 0.0, crash_on_search: bool = False) -> list[str]:
    program = f"""
import json
import sys
import time

for line in sys.stdin:
    request = json.loads(line)
    if request["method"] in ("search", "search_scopes"):
        if {crash_on_search!r}:
            sys.exit(17)
        time.sleep({search_delay!r})
    print(json.dumps({{"id": request["id"], "ok": True, "result": []}}), flush=True)
"""
    return [sys.executable, "-I", "-B", "-c", program]


class TimedSearchStore:
    """Advance the adapter clock by the measured helper RPC cost."""

    def __init__(self, store: ProcessLanceVectorStore, clock: ManualClock, cost: float) -> None:
        self.store = store
        self.clock = clock
        self.cost = cost
        self.calls: list[str] = []

    def search(self, vector: list[float], *, scope_id: str, limit: int) -> list[dict[str, object]]:
        self.store.search(vector, scope_id=scope_id, limit=limit)
        self.clock.advance(self.cost)
        self.calls.append(scope_id)
        return [_candidate_row(scope_id)]


def test_fanout_stops_before_over_budget_rpc_and_keeps_worker_usable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scope_recall.vector.process_store as process_store

    clock = ManualClock()
    monkeypatch.setattr(
        request_deadline,
        "time",
        SimpleNamespace(monotonic=clock.monotonic),
    )
    monkeypatch.setattr(
        process_store,
        "_worker_command",
        lambda: _worker_command(search_delay=0.1),
    )
    store = ProcessLanceVectorStore(tmp_path / "lancedb", table_name="memories", dimensions=2)
    store.open()
    worker = store._process
    timed_store = TimedSearchStore(store, clock, cost=0.1)
    port = LanceVectorPort(timed_store, SyntheticQueryEmbedding(), clock=clock.monotonic)

    try:
        first = port.search(_context(tmp_path, clock, budget=0.15), limit=6, remaining_seconds=0.15)
        assert [candidate.ref for candidate in first] == ["TEST-hit"]
        assert len(timed_store.calls) == 1
        assert store.requires_reopen is False
        assert store._process is worker and worker is not None and worker.poll() is None

        second = port.search(_context(tmp_path, clock, budget=0.15), limit=6, remaining_seconds=0.15)
        assert [candidate.ref for candidate in second] == ["TEST-hit"]
        assert len(timed_store.calls) == 2
        assert store.requires_reopen is False
        assert store._process is worker and worker.poll() is None
    finally:
        store.close()


def test_started_transport_failure_is_not_masked(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scope_recall.vector.process_store as process_store

    monkeypatch.setattr(
        process_store,
        "_worker_command",
        lambda: _worker_command(crash_on_search=True),
    )
    store = ProcessLanceVectorStore(tmp_path / "lancedb", table_name="memories", dimensions=2)
    store.open()
    clock = ManualClock()
    monkeypatch.setattr(
        request_deadline,
        "time",
        SimpleNamespace(monotonic=clock.monotonic),
    )
    port = LanceVectorPort(store, SyntheticQueryEmbedding(), clock=clock.monotonic)

    try:
        with pytest.raises(RuntimeError, match="native vector worker failed"):
            port.search(_context(tmp_path, clock, budget=1.0), limit=6, remaining_seconds=1.0)
        assert store.requires_reopen is True
    finally:
        store.close()


def test_an_entry_holding_a_hundred_scopes_searches_them_all_in_one_request(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A request per partition, in sorted order, until the budget ran out.

    On the pilot an entry held 110 scopes and searched seven of them before its budget ran out;
    the owner's own scope sorted 105th and was never searched, so what the owner had just told
    another agent was not found by meaning.  The whole trusted list is now one request.
    """
    from scope_recall.adapters.lance import physical_partition_scope_id
    from scope_recall.contracts import InstanceBinding, TrustedContext

    clock = ManualClock()
    monkeypatch.setattr(request_deadline, "time", SimpleNamespace(monotonic=clock.monotonic))
    scopes = [f"TEST-scope-{index:03d}" for index in range(110)]
    binding = InstanceBinding("TEST-agent", "TEST-installation", tmp_path / "TEST-data", frozenset(scopes), True)
    trusted = TrustedContext(binding, "TEST-session", frozenset(scopes), "human_direct")
    context = SearchContext(query="PUBLIC semantic query", mode="auto", as_of=None, focus_refs=(), limits=SearchLimits(),
                            deadline=clock.monotonic() + 1.0, now="2026-09-15T00:00:00Z", trusted_context=trusted)
    last = scopes[-1]
    wanted = physical_partition_scope_id(agent_id="TEST-agent", installation_id="TEST-installation",
                                         embedding_space=SPACE_ID, logical_scope_id=last, project_id=None, branch_id=None)
    metadata = {"object_kind": "event", "object_ref": "TEST-owner-told", "object_revision": 1, "vector_id": "TEST-vector",
                "embedding_space": SPACE_ID, "agent_id": "TEST-agent", "installation_id": "TEST-installation",
                "project_id": None, "branch_id": None, "logical_scope_id": last}

    class Store:
        def __init__(self) -> None:
            self.requests: list[object] = []

        def search(self, vector, *, scope_id, limit):
            self.requests.append(scope_id)
            clock.advance(0.15)
            return []

        def search_scopes(self, vector, *, scope_ids, limit):
            self.requests.append(tuple(scope_ids))
            clock.advance(0.15)
            return [{"scope_id": wanted, "target": json.dumps(metadata), "score": 0.9}] if wanted in scope_ids else []

    store = Store()
    found = LanceVectorPort(store, SyntheticQueryEmbedding(), clock=clock.monotonic).search(
        context, limit=6, remaining_seconds=1.0)
    assert [candidate.ref for candidate in found] == ["TEST-owner-told"]
    assert len(store.requests) == 1 and len(store.requests[0]) == len(scopes), "one request for every partition"
