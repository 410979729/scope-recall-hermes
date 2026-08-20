"""Architecture-convergence: one production command-port route.

These tests fail on the live dual assembly (ProviderCommandAdapter vs
``_LegacyPersistCommandPort``), inward shim imports, and sys.modules
lookups. They must stay runtime/identity checks, not source-shape stand-ins
for the command-port unification.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from scope_recall._internal.recall import orchestrator as orchestrator_module
from scope_recall._internal.recall import pipeline as recall_pipeline
from scope_recall._internal.runtime.command_adapter import ProviderCommandAdapter
from scope_recall._internal.runtime.kernel import (
    COMMAND_KERNEL,
    _LegacyPersistCommandPort,
)
from scope_recall.models import RecallItem
from scope_recall.provider import ScopeRecallMemoryProvider
from scope_recall.recall import RecallService
from scope_recall.tooling import ScopeRecallToolService

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SHIM_MODULES = frozenset({"memory_scope", "recall_pipeline", "runtime_kernel"})
_TOUCHED_FOR_SHIMS = (
    "_internal/recall/orchestrator.py",
    "memory_ops.py",
    "memory_queries.py",
    "provider.py",
)
_COMMAND_SPY_METHODS = (
    "store",
    "update",
    "merge",
    "archive",
    "delete",
    "feedback",
    "govern",
    "dedupe",
    "repair",
)


def _import_from_modules(rel: str) -> set[str]:
    tree = ast.parse((PLUGIN_ROOT / rel).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        names.add(node.module.rsplit(".", 1)[-1])
    return names


def _spy_kernel_ports(monkeypatch) -> list[tuple[str, object]]:
    seen: list[tuple[str, object]] = []

    def _wrap(name: str, default: Any) -> Any:
        def spy(port: object, *args: Any, **kwargs: Any) -> Any:
            seen.append((name, port))
            return default

        return spy

    monkeypatch.setattr(COMMAND_KERNEL, "store", _wrap("store", ("mem-arch", True, "stored")))
    monkeypatch.setattr(COMMAND_KERNEL, "update", _wrap("update", (True, "updated", "")))
    monkeypatch.setattr(COMMAND_KERNEL, "merge", _wrap("merge", {"merged": True}))
    monkeypatch.setattr(COMMAND_KERNEL, "archive", _wrap("archive", {"archived": 1}))
    monkeypatch.setattr(COMMAND_KERNEL, "delete", _wrap("delete", 1))
    monkeypatch.setattr(COMMAND_KERNEL, "feedback", _wrap("feedback", {"ok": True}))
    monkeypatch.setattr(COMMAND_KERNEL, "govern", _wrap("govern", {"ok": True}))
    monkeypatch.setattr(COMMAND_KERNEL, "dedupe", _wrap("dedupe", {"ok": True}))
    monkeypatch.setattr(COMMAND_KERNEL, "repair", _wrap("repair", {"ok": True}))
    return seen


def _provider_command_calls(provider: ScopeRecallMemoryProvider) -> None:
    provider._store_now(
        content="arch convergence provider store",
        source="manual",
        target="memory",
        session_id="sess-arch-provider",
    )
    provider._update_memory("mem-arch", "updated", None)
    provider._merge_memories("mem-arch", ["mem-src"], None, None)
    provider._archive_memories(["mem-arch"])
    provider._delete_memories(["mem-arch"])
    provider._feedback_memory(memory_id="mem-arch", rating="up")
    provider._govern_memories()
    provider._dedupe_memories()
    provider._repair_vector()


def _tooling_command_calls(provider: ScopeRecallMemoryProvider) -> None:
    port = provider._composition.tool_port
    port.store_now(
        content="arch convergence tooling store",
        source="tool-store",
        target="memory",
        session_id="sess-arch-tool",
    )
    port.update_memory("mem-arch", "updated", None)
    port.merge_memories("mem-arch", ["mem-src"], None, None)
    port.archive_memories(["mem-arch"])
    port.delete_memories(["mem-arch"])
    port.feedback_memory(memory_id="mem-arch", rating="up")
    port.govern_memories()
    port.dedupe_memories()
    port.repair_vector()


def test_provider_and_tooling_entries_use_same_command_port_object(monkeypatch) -> None:
    provider = ScopeRecallMemoryProvider()
    assembled = provider._composition.command_port
    seen = _spy_kernel_ports(monkeypatch)

    _provider_command_calls(provider)
    provider_calls = list(seen)
    seen.clear()
    _tooling_command_calls(provider)
    tooling_calls = list(seen)

    assert [name for name, _port in provider_calls] == list(_COMMAND_SPY_METHODS)
    assert [name for name, _port in tooling_calls] == list(_COMMAND_SPY_METHODS)
    provider_ports = [port for _name, port in provider_calls]
    tooling_ports = [port for _name, port in tooling_calls]
    assert provider_ports == [assembled] * len(_COMMAND_SPY_METHODS)
    assert tooling_ports == [assembled] * len(_COMMAND_SPY_METHODS)
    assert all(port is assembled for port in provider_ports)
    assert all(port is assembled for port in tooling_ports)
    assert type(assembled) is ProviderCommandAdapter
    assert not isinstance(assembled, _LegacyPersistCommandPort)
    assert all(not isinstance(port, _LegacyPersistCommandPort) for port in tooling_ports)


def test_tool_service_on_provider_reuses_assembled_command_port(monkeypatch) -> None:
    provider = ScopeRecallMemoryProvider()
    assembled = provider._composition.command_port
    seen = _spy_kernel_ports(monkeypatch)
    ScopeRecallToolService(provider)._port.store_now(
        content="arch convergence service store",
        source="tool-store",
        target="memory",
        session_id="sess-arch-service",
    )
    assert seen == [("store", assembled)]
    assert seen[0][1] is assembled
    assert not isinstance(seen[0][1], _LegacyPersistCommandPort)


def test_isolated_host_keeps_legacy_command_port_fallback(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    ports: list[object] = []
    original = COMMAND_KERNEL.store

    def spy(port: object, *args: Any, **kwargs: Any) -> Any:
        ports.append(port)
        return original(port, *args, **kwargs)

    monkeypatch.setattr(COMMAND_KERNEL, "store", spy)

    class FakeProvider:
        _config: dict[str, Any] = {}
        _session_id = "sess-arch-fake"
        _shared_pool_enabled = False

        def _clean_text(self, text: Any) -> str:
            return str(text or "")

        def _store_now(self, **kwargs: Any) -> tuple[str, bool, str]:
            calls.append(kwargs)
            return "mem-arch-fake", True, "stored"

    payload = json.loads(
        ScopeRecallToolService(FakeProvider())._handle_store(
            {
                "content": "Synthetic isolated-host fixture stores a Scope Recall fallback copy.",
                "target": "memory",
            }
        )
    )
    assert calls and calls[0]["content"]
    assert payload["id"] == "mem-arch-fake"
    assert payload["stored"] is True
    assert ports
    assert all(isinstance(port, _LegacyPersistCommandPort) for port in ports)
    assert all(not isinstance(port, ProviderCommandAdapter) for port in ports)


def test_touched_internals_import_canonical_modules_not_shims() -> None:
    assert orchestrator_module.recall_pipeline is recall_pipeline
    assert orchestrator_module.recall_pipeline.__name__ == "scope_recall._internal.recall.pipeline"
    for rel in _TOUCHED_FOR_SHIMS:
        imported = _import_from_modules(rel)
        leaked = imported & _SHIM_MODULES
        assert leaked == set(), f"{rel} still imports shims {sorted(leaked)}"


class _ArchDummyProvider:
    def __init__(self) -> None:
        self._retrieval_config = {"mode": "lexical", "min_score": 0.01}
        self._vector_config: dict[str, Any] = {}
        self._scope_id = "local-scope"
        self._shared_scope_id = "shared-scope"
        self._accessible_scope_ids = [self._scope_id, self._shared_scope_id]
        self._config = {"auto_recall": True, "query_char_limit": 1000}

    def _search_db_memories(self, query, *, limit):
        del query
        return [][:limit]

    def _search_vector_memories(self, query, *, limit):
        del query, limit
        return []

    def _search_vector_memories_with_vector(self, query_vector, *, limit):
        del query_vector, limit
        return []

    def _search_curated_memories(self, query):
        del query
        return []

    def _dedup_key(self, content):
        return str(content).lower()


def test_run_search_uses_injected_host_sanitize_not_sys_modules(monkeypatch) -> None:
    seen: list[str] = []
    provider = _ArchDummyProvider()
    low = RecallItem(
        id="below",
        content="unrelated chatter",
        summary="unrelated chatter",
        source="tool-store",
        target="memory",
        score=0.0,
        updated_at="2026-05-01T00:00:00+00:00",
        metadata={"lexical_score": 0.01, "vector_score": 0.0},
    )
    provider._search_db_memories = lambda query, *, limit: [low]  # type: ignore[method-assign]
    service = RecallService(provider)

    def fake_safe(item):
        seen.append(item.id)
        return item

    monkeypatch.setattr(service, "safe_recall_item", fake_safe)
    service.search_memories("zzzz", limit=5)
    assert "below" in seen
    assert "sys.modules" not in orchestrator_module.__dict__
    assert not hasattr(orchestrator_module, "_recall_module")
    assert not hasattr(orchestrator_module, "_recall_fn")
