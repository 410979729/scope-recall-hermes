"""Lazy query work preserves the caller's monotonic clock domain."""

import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from scope_recall.core.deadline import RequestDeadline, remaining_seconds, using_request_deadline
from scope_recall.core.retrieval import SearchContext, SearchLimits
from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig
from scope_recall.runtime.instance import RuntimeInstance, _LazyVectorPort
from scope_recall.runtime.instance_config import RuntimeInstanceConfig, VectorRuntimeConfig

from tests.contract.test_runtime_worker_entry import _binding, _context


@pytest.mark.parametrize("prefetch", [False, True])
@pytest.mark.parametrize("caller", ["core", "bound", "shorter"])
def test_lazy_query_and_prefetch_use_injected_clock(tmp_path, prefetch, caller):
    """Tick 100.0 with deadline 100.5 still allows the existing store to open."""
    binding = _binding(tmp_path / "data")
    config = RuntimeInstanceConfig(
        binding=binding,
        session_id="TEST-clock",
        allowed_scope_ids=binding.scope_ids,
        auxiliary=AuxiliaryRuntimeConfig.from_mapping({"external_embedding": False, "external_consolidation": False}),
        vector=VectorRuntimeConfig(
            backend="sqlite",
            storage_dir=tmp_path / "vectors",
            table_name="TEST-vectors",
            dimensions=2,
            test_injection_override=True,
        ),
    )
    opened, searched, embedded = [], [], []

    class Store:
        def open_existing(self):
            opened.append(remaining_seconds())

        def search_scopes(self, values, *, scope_ids, limit):
            searched.append(remaining_seconds())
            return []

    class Embed:
        def embed_query(self, text, *, remaining_seconds):
            embedded.append(remaining_seconds)
            return [1.0, 0.0]

    core_clock = time.monotonic if caller == "bound" else lambda: 100.0
    core = SimpleNamespace(clock=SimpleNamespace(monotonic=core_clock), private_mode=lambda _: "off")
    instance = RuntimeInstance(
        config=config, core=core, auxiliary=SimpleNamespace(query_embedding=Embed()), _vector_factory=lambda _: Store()
    )
    port = _LazyVectorPort(instance)
    context = SearchContext(
        query="TEST clock",
        mode="auto",
        as_of=None,
        focus_refs=(),
        limits=SearchLimits(max_items=1),
        deadline=100.5,
        now="2026-01-01T00:00:00Z",
        trusted_context=_context(binding, "TEST-clock"),
    )
    expected = 0.25 if caller == "shorter" else 0.5
    bound = RequestDeadline.from_absolute(100.0 + expected, clock=lambda: 100.0)
    with using_request_deadline(bound) if caller != "core" else nullcontext():
        pending = port.prefetch_query(context) if prefetch else None
        if prefetch:
            assert pending is not None
        assert port.search(context, limit=1, remaining_seconds=0.5, prefetched=pending) == ()
    assert opened == [expected]
    assert searched == [expected]
    assert len(embedded) == 1 and 0 < embedded[0] <= expected
