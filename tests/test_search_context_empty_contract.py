"""Empty ordinary recall surfaces expose one additive, stable status."""

from __future__ import annotations

import json
import sys
import types

from scope_recall.memory_queries import context_payload

_tools_package = types.ModuleType("tools")
_tools_registry = types.ModuleType("tools.registry")
_tools_registry.tool_error = lambda message: json.dumps({"error": str(message)})
sys.modules.setdefault("tools", _tools_package)
sys.modules.setdefault("tools.registry", _tools_registry)

from scope_recall.tooling import ScopeRecallToolService  # noqa: E402


class _EmptyRecall:
    last_temporal_query_diagnostics: dict[str, object] = {}
    last_funnel_trace = {
        "query_signal_state": "none",
        "filters": {"no_admissible_evidence": 1},
    }

    @staticmethod
    def search_memories(query: str, *, limit: int, **_kwargs):
        del query, limit
        return []


class _Provider:
    _retrieval_config = {"top_k": 5}
    _recall_service = _EmptyRecall()

    @staticmethod
    def _normalize_query(value, limit):
        return str(value).strip()[:limit]

    @staticmethod
    def _config_value(_key, default):
        return default

    @classmethod
    def recall_service_view(cls):
        return cls._recall_service


def test_search_empty_response_is_additive_and_explicit() -> None:
    payload = json.loads(
        ScopeRecallToolService(_Provider())._handle_search(
            {"query": "4f6bd06e-98bd-4f4d-a891-e31b7f08ad2e", "limit": 5}
        )
    )

    assert payload["count"] == 0
    assert payload["results"] == []
    assert payload["retrieval_status"] == "no_relevant_memory"
    assert payload["reason_codes"] == ["no_admissible_evidence"]


def test_context_empty_response_is_additive_and_explicit() -> None:
    payload = context_payload(
        _Provider(),
        query="4f6bd06e-98bd-4f4d-a891-e31b7f08ad2e",
        limit=5,
    )

    assert payload["count"] == 0
    assert payload["context"] == ""
    assert payload["results"] == []
    assert payload["retrieval_status"] == "no_relevant_memory"
    assert payload["reason_codes"] == ["no_admissible_evidence"]
