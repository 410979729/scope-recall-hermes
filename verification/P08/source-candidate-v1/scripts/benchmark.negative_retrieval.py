#!/usr/bin/env python3
"""Recompute deterministic negative-retrieval release evidence offline."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "scope_recall_negative_retrieval_runtime"
if PACKAGE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load scope-recall package from {PLUGIN_ROOT}")
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = package
    spec.loader.exec_module(package)

from scope_recall_negative_retrieval_runtime.memory_queries import (  # noqa: E402
    context_payload,
)
from scope_recall_negative_retrieval_runtime.models import RecallItem  # noqa: E402
from scope_recall_negative_retrieval_runtime.prompting import (  # noqa: E402
    render_current_turn_recall,
)
from scope_recall_negative_retrieval_runtime.recall import RecallService  # noqa: E402

_AT = "2026-08-30T00:00:00+00:00"
_NEGATIVE_QUERIES = (
    "550e8400-e29b-41d4-a716-446655440000",
    "c799ccd3",
    "U2NvcGVSZWNhbGxSYW5kb21QYXlsb2Fk",
    "qzxvbnmkjhgf",
    "qzxvbnmkjhgfdspoiuytrewq",
    "峰猫泪珠墙锁qzxvbn",
    "㐀㐁㐂㐃㐄㐅㐆㐇",
    "!!! 🚀 ???",
    "XAS-OPS-404",
    "the",
)


def _item(
    memory_id: str,
    content: str,
    *,
    source: str = "tool-store",
    target: str = "memory",
    lexical_score: float = 0.0,
    vector_score: float = 0.0,
    temporal: bool = False,
) -> RecallItem:
    score = max(float(lexical_score), float(vector_score))
    return RecallItem(
        id=memory_id,
        content=content,
        summary=content,
        source=source,
        target=target,
        score=score,
        updated_at=_AT,
        metadata={
            "lexical_score": float(lexical_score),
            "vector_score": float(vector_score),
            "scope_id": "shared-scope",
            "importance": 0.8,
            "confidence": 0.9,
            "memory_type": "factual",
            "temporal_authoritative": bool(temporal),
            "temporal_fact_current": bool(temporal),
        },
    )


class _EvidenceRecallService(RecallService):
    def _temporal_current_candidates(
        self,
        query: str,
        *,
        limit: int,
        candidate_memory_ids: list[str],
    ) -> tuple[list[RecallItem], frozenset[str]] | None:
        del query, limit, candidate_memory_ids
        temporal = list(getattr(self.provider, "_temporal_items", []) or [])
        return (temporal, frozenset()) if temporal else None


class _EvidenceProvider:
    def __init__(
        self,
        *,
        lexical: list[RecallItem] | None = None,
        vector: list[RecallItem] | None = None,
        curated: list[RecallItem] | None = None,
        temporal: list[RecallItem] | None = None,
    ) -> None:
        self._retrieval_config = {
            "mode": "hybrid",
            "min_score": 0.18,
            "candidate_pool": 5,
            "top_k": 5,
            "vector_only_min_score": 0.70,
            "vector_only_min_margin": 0.035,
            "zero_signal_gate_enabled": True,
        }
        self._vector_config: dict[str, Any] = {}
        self._config = {
            "auto_recall": True,
            "auto_recall_min_length": 1,
            "auto_recall_min_repeated": 0,
            "query_char_limit": 1000,
        }
        self._scope_id = "local-scope"
        self._shared_scope_id = "shared-scope"
        self._accessible_scope_ids = [self._scope_id, self._shared_scope_id]
        self._scope = SimpleNamespace(agent_context="primary")
        self._last_recall_turns: dict[str, int] = {}
        self._current_turn = 1
        self._lexical_items = list(lexical or [])
        self._vector_items = list(vector or [])
        self._curated_items = list(curated or [])
        self._temporal_items = list(temporal or [])
        self._recall_service = _EvidenceRecallService(self)

    def _search_db_memories(self, query: str, *, limit: int) -> list[RecallItem]:
        del query
        return self._lexical_items[:limit]

    def _search_vector_memories(self, query: str, *, limit: int) -> list[RecallItem]:
        del query
        return self._vector_items[:limit]

    def _search_vector_memories_with_vector(
        self, query_vector: list[float], *, limit: int
    ) -> list[RecallItem]:
        del query_vector
        return self._vector_items[:limit]

    def _search_curated_memories(self, query: str) -> list[RecallItem]:
        del query
        return list(self._curated_items)

    @staticmethod
    def _dedup_key(content: str) -> str:
        return content.casefold()

    @staticmethod
    def _config_value(key: str, default: Any) -> Any:
        del key
        return default

    @staticmethod
    def _normalize_query(query: str, limit: int) -> str:
        return str(query)[:limit]

    @staticmethod
    def recall_limit() -> int:
        return 5

    def recall_service_view(self) -> RecallService:
        return self._recall_service

    @staticmethod
    def _mark_recalled(memory_ids: list[str]) -> None:
        del memory_ids


def _negative_provider() -> _EvidenceProvider:
    return _EvidenceProvider(
        vector=[
            _item(
                "unrelated-neighbor-1",
                "Workstation timezone is America/New_York.",
                vector_score=0.99,
            ),
            _item("unrelated-neighbor-2", "A background build queue is idle.", vector_score=0.80),
            _item("unrelated-neighbor-3", "The archive rotation completed.", vector_score=0.70),
            _item("unrelated-neighbor-4", "A local display uses dark mode.", vector_score=0.60),
            _item("unrelated-background", "The printer tray contains paper.", vector_score=0.50),
        ]
    )


def _positive_cases() -> tuple[tuple[str, str, _EvidenceProvider], ...]:
    return (
        (
            "deployment command",
            "lexical-hit",
            _EvidenceProvider(
                lexical=[
                    _item(
                        "lexical-hit",
                        "The deployment command is uv run app.",
                        lexical_score=0.94,
                    )
                ]
            ),
        ),
        (
            "c799ccd3",
            "c799ccd3",
            _EvidenceProvider(
                lexical=[
                    _item(
                        "c799ccd3",
                        "Release commit c799ccd3 passed review.",
                        lexical_score=0.95,
                    )
                ]
            ),
        ),
        (
            "durable preference retrieval",
            "vector-hit",
            _EvidenceProvider(
                vector=[
                    _item(
                        "vector-hit",
                        "The persistent knowledge subsystem restores prior choices.",
                        vector_score=0.82,
                    ),
                    _item(
                        "vector-background",
                        "A deliberately separated background neighbour.",
                        vector_score=0.55,
                    ),
                ]
            ),
        ),
        (
            "What response style does the sample user prefer?",
            "curated:reply-style",
            _EvidenceProvider(
                curated=[
                    _item(
                        "curated:reply-style",
                        "The sample user prefers concise replies.",
                        source="builtin-curated",
                        target="user",
                        lexical_score=0.90,
                    )
                ]
            ),
        ),
        (
            "Where does the sample user live now?",
            "temporal-current-city",
            _EvidenceProvider(
                temporal=[
                    _item(
                        "temporal-current-city",
                        "The sample user lives in Tokyo.",
                        source="temporal-fact",
                        lexical_score=0.92,
                        temporal=True,
                    )
                ]
            ),
        ),
        (
            "用户偏好什么回答方式？",
            "chinese-preference",
            _EvidenceProvider(
                lexical=[
                    _item(
                        "chinese-preference",
                        "用户偏好简洁、直接并附带验证证据的中文回答。",
                        target="user",
                        lexical_score=0.93,
                    )
                ]
            ),
        ),
    )


def build_negative_retrieval_evidence() -> dict[str, Any]:
    """Run every case against current production contracts; read no fixture verdict."""

    negative_nonempty_count = 0
    for query in _NEGATIVE_QUERIES:
        search_results = _negative_provider().recall_service_view().search_memories(
            query, limit=5
        )
        context_results = context_payload(
            _negative_provider(), query=query, limit=5
        )["results"]
        prefetch = render_current_turn_recall(_negative_provider(), query)
        negative_nonempty_count += int(bool(search_results))
        negative_nonempty_count += int(bool(context_results))
        negative_nonempty_count += int(bool(str(prefetch or "").strip()))

    positive_hit_count = 0
    positive_cases = _positive_cases()
    for query, expected_id, provider in positive_cases:
        returned_ids = {
            item.id
            for item in provider.recall_service_view().search_memories(query, limit=5)
        }
        positive_hit_count += int(expected_id in returned_ids)

    positive_hit_rate = round(
        positive_hit_count / max(1, len(positive_cases)),
        6,
    )
    passed = negative_nonempty_count == 0 and positive_hit_rate == 1.0
    return {
        "schema_version": 1,
        "negative_case_count": len(_NEGATIVE_QUERIES),
        "negative_surface_count": len(_NEGATIVE_QUERIES) * 3,
        "negative_nonempty_count": negative_nonempty_count,
        "positive_case_count": len(positive_cases),
        "positive_hit_count": positive_hit_count,
        "positive_hit_rate": positive_hit_rate,
        "passed": passed,
    }


def main() -> int:
    payload = build_negative_retrieval_evidence()
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
