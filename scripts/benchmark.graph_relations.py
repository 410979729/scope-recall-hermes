#!/usr/bin/env python3
"""Deterministic graph/relation rerank benchmark for Scope Recall.

This benchmark is intentionally small and API-free. It exercises the same
RecallService relation evidence path used by search/explain while avoiding live
Hermes storage, vector providers, or network calls.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "scope_recall_graph_benchmark_runtime"
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

from scope_recall_graph_benchmark_runtime.graph import ensure_graph_schema  # noqa: E402
from scope_recall_graph_benchmark_runtime.models import RecallItem  # noqa: E402
from scope_recall_graph_benchmark_runtime.recall import RecallService  # noqa: E402


class BenchmarkProvider:
    def __init__(self, retrieval_config: dict[str, Any], items: list[RecallItem]) -> None:
        self._retrieval_config = dict(retrieval_config)
        self._scope_id = "local-scope"
        self._shared_scope_id = "shared-scope"
        self._accessible_scope_ids = [self._scope_id, self._shared_scope_id]
        self._items = list(items)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("CREATE TABLE memories(id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}')")
        ensure_graph_schema(self._conn)
        for item in self._items:
            scope_id = str((item.metadata or {}).get("scope_id") or self._shared_scope_id)
            self._conn.execute("INSERT OR REPLACE INTO memories(id, scope_id, metadata) VALUES (?, ?, ?)", (item.id, scope_id, json.dumps(item.metadata or {}, sort_keys=True)))
        self._conn.commit()

    def _search_db_memories(self, query: str, *, limit: int) -> list[RecallItem]:
        del query
        return [
            RecallItem(
                id=item.id,
                content=item.content,
                summary=item.summary,
                source=item.source,
                target=item.target,
                score=item.score,
                updated_at=item.updated_at,
                metadata=dict(item.metadata or {}),
            )
            for item in self._items[:limit]
        ]

    def _search_vector_memories(self, query: str, *, limit: int) -> list[RecallItem]:
        del query, limit
        return []

    def _search_curated_memories(self, query: str) -> list[RecallItem]:
        del query
        return []

    def _dedup_key(self, content: str) -> str:
        return str(content).lower()

    def _config_value(self, key: str, default: Any) -> Any:
        del key
        return default

    def _require_conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()


def item(memory_id: str, score: float, *, lifecycle: str = "") -> RecallItem:
    metadata: dict[str, Any] = {"lexical_score": score, "scope_id": "shared-scope", "memory_type": "project"}
    if lifecycle:
        metadata["lifecycle"] = lifecycle
    return RecallItem(
        id=memory_id,
        content=f"Project Atlas deploy command candidate {memory_id}.",
        summary=f"Project Atlas deploy command candidate {memory_id}.",
        source="tool-store",
        target="project",
        score=score,
        updated_at="2026-06-01T00:00:00+00:00",
        metadata=metadata,
    )


def _insert_relation(provider: BenchmarkProvider, source: str, target: str, relation_type: str, confidence: float = 1.0) -> None:
    provider._require_conn().execute(
        """
        INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
        VALUES (?, ?, ?, ?, 'benchmark', '2026-06-01T00:00:00+00:00')
        """,
        (source, target, relation_type, confidence),
    )
    provider._require_conn().commit()


def _run_search(retrieval_config: dict[str, Any], items: list[RecallItem], relations: list[tuple[str, str, str]], *, extra_memories: list[tuple[str, str, dict[str, Any]]] | None = None) -> list[RecallItem]:
    provider = BenchmarkProvider(retrieval_config, items)
    try:
        for memory_id, scope_id, metadata in extra_memories or []:
            provider._require_conn().execute(
                "INSERT OR REPLACE INTO memories(id, scope_id, metadata) VALUES (?, ?, ?)",
                (memory_id, scope_id, json.dumps(metadata, sort_keys=True)),
            )
        for source, target, relation_type in relations:
            _insert_relation(provider, source, target, relation_type)
        return RecallService(provider).search_memories("Project Atlas deploy command", limit=5)
    finally:
        provider.close()


def case_supersedes_improves_current_fact() -> dict[str, Any]:
    older = item("older-deploy-command", 0.82)
    newer = item("newer-deploy-command", 0.78)
    relations = [("newer-deploy-command", "older-deploy-command", "supersedes")]
    off = _run_search({"mode": "lexical", "min_score": 0.01}, [older, newer], relations)
    on = _run_search({"mode": "lexical", "min_score": 0.01, "relation_rerank_enabled": True}, [older, newer], relations)
    off_order = [row.id for row in off]
    on_order = [row.id for row in on]
    on_by_id = {row.id: row for row in on}
    passed = off_order[:2] == ["older-deploy-command", "newer-deploy-command"] and on_order[:2] == ["newer-deploy-command", "older-deploy-command"]
    return {
        "name": "supersedes improves current fact",
        "passed": passed,
        "improved": on_order.index("newer-deploy-command") < off_order.index("newer-deploy-command"),
        "off_order": off_order,
        "on_order": on_order,
        "newer_bonus": on_by_id["newer-deploy-command"].metadata.get("relation_rerank_bonus"),
        "older_bonus": on_by_id["older-deploy-command"].metadata.get("relation_rerank_bonus"),
    }


def case_hidden_peers_do_not_leak_or_rerank() -> dict[str, Any]:
    visible = item("visible-deploy-command", 0.82)
    results = _run_search(
        {"mode": "lexical", "min_score": 0.01, "relation_rerank_enabled": True, "relation_supports_boost": 0.2},
        [visible],
        [
            ("visible-deploy-command", "archived-peer", "supports"),
            ("visible-deploy-command", "deleted-peer", "supports"),
        ],
        extra_memories=[("archived-peer", "shared-scope", {"lifecycle": "archived"})],
    )
    row = results[0]
    evidence_ids = row.metadata.get("relation_evidence_ids") or []
    bonus = row.metadata.get("relation_rerank_bonus")
    forbidden = sorted(set(evidence_ids) & {"archived-peer", "deleted-peer"})
    passed = evidence_ids == [] and bonus == 0.0 and not forbidden
    return {
        "name": "hidden peers do not leak or rerank",
        "passed": passed,
        "forbidden_id_violations": forbidden,
        "relation_evidence_ids": evidence_ids,
        "relation_rerank_bonus": bonus,
    }


def case_explicit_zero_penalty_is_respected() -> dict[str, Any]:
    older = item("older-deploy-command", 0.82)
    newer = item("newer-deploy-command", 0.78)
    results = _run_search(
        {
            "mode": "lexical",
            "min_score": 0.01,
            "relation_rerank_enabled": True,
            "relation_supersedes_boost": 0.08,
            "relation_superseded_penalty": 0.0,
        },
        [older, newer],
        [("newer-deploy-command", "older-deploy-command", "supersedes")],
    )
    by_id = {row.id: row for row in results}
    older_bonus = by_id["older-deploy-command"].metadata.get("relation_rerank_bonus")
    newer_bonus = by_id["newer-deploy-command"].metadata.get("relation_rerank_bonus")
    passed = newer_bonus > 0.0 and older_bonus == 0.0
    return {
        "name": "explicit zero superseded penalty is respected",
        "passed": passed,
        "newer_bonus": newer_bonus,
        "older_bonus": older_bonus,
        "order": [row.id for row in results],
    }


def run_benchmark() -> dict[str, Any]:
    cases = [case_supersedes_improves_current_fact(), case_hidden_peers_do_not_leak_or_rerank(), case_explicit_zero_penalty_is_respected()]
    forbidden_id_violations = sum(len(case.get("forbidden_id_violations") or []) for case in cases)
    improved_cases = sum(1 for case in cases if case.get("improved"))
    passed_cases = sum(1 for case in cases if case.get("passed"))
    return {
        "benchmark_name": "graph_relation_rerank_v1",
        "passed": passed_cases == len(cases) and forbidden_id_violations == 0,
        "metrics": {
            "case_count": len(cases),
            "passed_cases": passed_cases,
            "improved_cases": improved_cases,
            "unchanged_cases": len(cases) - improved_cases,
            "forbidden_id_violations": forbidden_id_violations,
        },
        "cases": cases,
    }


def main() -> int:
    payload = run_benchmark()
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
