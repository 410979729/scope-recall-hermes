"""Reflection tool gating, bounded synthesis, and mental-model candidates."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Callable

import pytest

from scope_recall.models import RecallItem, RuntimeScope
from scope_recall.provider_schemas import build_tool_schemas
from scope_recall.sql_store import ensure_schema
from scope_recall.tooling import ScopeRecallToolService


def _schema_names(config: dict) -> list[str]:
    return [str(item["name"]) for item in build_tool_schemas(config)]


class ReflectionToolProvider:
    def __init__(
        self,
        tmp_path: Path,
        *,
        reflection_enabled: bool = True,
        maintenance_enabled: bool = False,
        write_candidates: bool = False,
    ) -> None:
        self._config = {
            "query_char_limit": 1_000,
            "maintenance_tools_enabled": maintenance_enabled,
            "temporal_queries": {"enabled": True, "timezone": "UTC"},
            "reflection": {
                "enabled": reflection_enabled,
                "write_candidates": write_candidates,
                "max_hops": 1,
                "max_evidence": 8,
                "max_chars": 4_000,
                "max_item_chars": 1_000,
                "recall_limit": 8,
                "fact_limit": 8,
                "timeout": 5.0,
                "max_attempts": 1,
                "candidate_min_citations": 2,
                "candidate_min_sources": 2,
                "candidate_min_confidence": 0.8,
            },
        }
        self._retrieval_config = {
            "mode": "lexical",
            "include_general": "same-scope",
            "entity_scope_filter_enabled": False,
            "min_score": 0.0,
        }
        self._vector_config: dict[str, object] = {}
        self._scope_id = "scope-a"
        self._shared_scope_id = "scope-a"
        self._accessible_scope_ids = ["scope-a"]
        self._writable_scope_ids = ["scope-a"]
        self._shared_pool_scope_id = ""
        self._session_id = "session-reflect"
        self._scope = RuntimeScope(
            platform="cli",
            user_id="joy",
            agent_identity="yuheng",
            agent_workspace="home",
        )
        self._hermes_home = tmp_path
        self._lock = RLock()
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        ensure_schema(self._conn)
        # Test-only owner: this fixture holds the writable in-memory pager.
        # Production still fail-closes unless the live provider role is owner.
        self._truth_writer_role = "owner"
        self.items: list[RecallItem] = []
        self.queries: list[str] = []
        self._reflection_transport: Callable[[str], str] | None = None

    def _search_db_memories(self, query: str, *, limit: int) -> list[RecallItem]:
        self.queries.append(query)
        return self.items[:limit]

    def _search_vector_memories(self, query: str, *, limit: int) -> list[RecallItem]:
        return []

    def _search_curated_memories(self, query: str) -> list[RecallItem]:
        return []

    @staticmethod
    def _dedup_key(content: str) -> str:
        return str(content).strip().casefold()

    def _require_conn(self) -> sqlite3.Connection:
        return self._conn

    def _config_value(self, key: str, default):
        return self._config.get(key, default)

    @staticmethod
    def _clean_text(value: str) -> str:
        return str(value or "").strip()

    @staticmethod
    def _normalize_query(value: str, limit: int) -> str:
        return str(value or "").strip()[:limit]

    def _rollback_conn_after_error(self, context: str) -> None:
        del context
        if self._conn.in_transaction:
            self._conn.rollback()


def _insert_memory(
    provider: ReflectionToolProvider,
    *,
    memory_id: str,
    content: str,
    source: str,
    score: float = 0.95,
    provenance_root: str = "",
) -> None:
    metadata = {
        "lifecycle": "promoted",
        "memory_type": "factual",
        "scope_id": "scope-a",
        "lexical_score": score,
        "vector_score": 0.0,
        "importance": score,
    }
    if provenance_root:
        metadata.update(
            {
                "source_type": "user_message",
                "source_ref": provenance_root,
                "provenance_root": provenance_root,
            }
        )
    provider._conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, platform, user_id, agent_identity, agent_workspace,
            session_id, source, target, content, summary, created_at, updated_at,
            dedup_key, metadata
        ) VALUES (?, 'scope-a', 'cli', 'joy', 'yuheng', 'home',
                  'seed', ?, 'memory', ?, ?,
                  '2026-07-01T00:00:00+00:00',
                  '2026-07-14T00:00:00+00:00', ?, ?)
        """,
        (
            memory_id,
            source,
            content,
            content,
            content.casefold(),
            json.dumps(metadata, sort_keys=True),
        ),
    )
    provider._conn.execute(
        "INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)",
        (memory_id, content, content),
    )
    provider.items.append(
        RecallItem(
            id=memory_id,
            content=content,
            summary=content,
            source=source,
            target="memory",
            score=score,
            updated_at="2026-07-14T00:00:00+00:00",
            metadata=metadata,
        )
    )
    provider._conn.commit()


def _seed_two_sources(provider: ReflectionToolProvider) -> None:
    _insert_memory(
        provider,
        memory_id="memory-one",
        content="Aurora uses PostgreSQL for durable project state.",
        source="tool-store",
    )
    _insert_memory(
        provider,
        memory_id="memory-two",
        content="Aurora requires row-level security for tenant isolation.",
        source="event-digest",
    )


def _response(*, followup: bool = False, forged: bool = False) -> str:
    citation_one = "memory:forged" if forged else "memory:memory-one"
    return json.dumps(
        {
            "observations": [
                {
                    "text": "Aurora uses PostgreSQL.",
                    "citations": [citation_one],
                },
                {
                    "text": "Aurora requires row-level security.",
                    "citations": ["memory:memory-two"],
                },
            ],
            "inferences": [
                {
                    "text": "Aurora's storage design prioritizes tenant isolation.",
                    "citations": [citation_one, "memory:memory-two"],
                }
            ],
            "uncertainties": [
                {
                    "text": "The evidence does not identify the deployment region.",
                    "citations": [citation_one],
                }
            ],
            "answer": (
                "Aurora uses PostgreSQL and requires row-level security; together "
                "these sources support a tenant-isolated storage design."
            ),
            "citations": [citation_one, "memory:memory-two"],
            "followup_queries": ["Aurora deployment region"] if followup else [],
        },
        ensure_ascii=False,
    )


def _payload(service: ScopeRecallToolService, args: dict) -> dict:
    return json.loads(service.handle("scope_recall_reflect", args))


def test_reflect_schema_is_feature_gated_and_extra_tools_cannot_bypass() -> None:
    assert "scope_recall_reflect" not in _schema_names({})
    assert "scope_recall_reflect" not in _schema_names(
        {"tool_schema_extra_tools": ["scope_recall_reflect"]}
    )
    enabled = _schema_names({"reflection": {"enabled": True}})
    assert enabled.count("scope_recall_reflect") == 1


def test_reflect_dispatcher_gate_and_unavailable_transport_are_explicit(tmp_path: Path) -> None:
    disabled = ReflectionToolProvider(tmp_path, reflection_enabled=False)
    disabled_payload = _payload(
        ScopeRecallToolService(disabled),
        {"query": "Aurora storage"},
    )
    assert "reflection.enabled" in disabled_payload["error"]

    enabled = ReflectionToolProvider(tmp_path, reflection_enabled=True)
    _seed_two_sources(enabled)
    before = enabled._conn.total_changes
    unavailable = _payload(
        ScopeRecallToolService(enabled),
        {"query": "Aurora storage"},
    )
    assert unavailable["ok"] is False
    assert unavailable["status"] == "unavailable"
    assert unavailable["reason"] == "llm_unavailable"
    assert enabled._conn.total_changes == before


def test_reflect_runs_one_followup_and_remains_read_only(tmp_path: Path) -> None:
    provider = ReflectionToolProvider(tmp_path)
    _seed_two_sources(provider)
    calls = 0

    def transport(prompt: str) -> str:
        nonlocal calls
        calls += 1
        assert "EVIDENCE_PACKAGE_JSON" in prompt
        return _response(followup=calls == 1)

    provider._reflection_transport = transport
    before = provider._conn.total_changes
    payload = _payload(
        ScopeRecallToolService(provider),
        {"query": "Aurora storage", "include_trace": True},
    )

    assert payload["ok"] is True
    assert payload["answer"].startswith("Aurora uses PostgreSQL")
    assert payload["hops_used"] == 1
    assert calls == 2
    assert provider.queries == ["Aurora storage", "Aurora deployment region"]
    assert payload["trace"]["write_delta"] == 0
    assert provider._conn.total_changes == before


def test_reflect_ignores_equivalent_followup_query(tmp_path: Path) -> None:
    provider = ReflectionToolProvider(tmp_path)
    _seed_two_sources(provider)
    calls = 0

    def transport(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        payload = json.loads(_response(followup=True))
        payload["followup_queries"] = ["  AURORA   storage  "]
        return json.dumps(payload)

    provider._reflection_transport = transport
    payload = _payload(ScopeRecallToolService(provider), {"query": "Aurora storage"})

    assert payload["ok"] is True
    assert payload["hops_used"] == 0
    assert calls == 1
    assert provider.queries == ["Aurora storage"]


def test_reflect_rejects_unknown_citation(tmp_path: Path) -> None:
    provider = ReflectionToolProvider(tmp_path)
    _seed_two_sources(provider)
    provider._reflection_transport = lambda prompt: _response(forged=True)
    before = provider._conn.total_changes

    payload = _payload(
        ScopeRecallToolService(provider),
        {"query": "Aurora storage"},
    )

    assert "unknown citation" in payload["error"]
    assert provider._conn.total_changes == before


@pytest.mark.parametrize(
    ("maintenance", "write_candidates", "error_token"),
    [
        (False, True, "maintenance_tools_enabled"),
        (True, False, "write_candidates"),
    ],
)
def test_propose_memory_requires_both_write_gates(
    tmp_path: Path,
    maintenance: bool,
    write_candidates: bool,
    error_token: str,
) -> None:
    provider = ReflectionToolProvider(
        tmp_path,
        maintenance_enabled=maintenance,
        write_candidates=write_candidates,
    )
    _seed_two_sources(provider)
    provider._reflection_transport = lambda prompt: _response()
    before = provider._conn.total_changes

    payload = _payload(
        ScopeRecallToolService(provider),
        {"query": "Aurora storage", "propose_memory": True},
    )

    assert error_token in payload["error"]
    assert provider._conn.total_changes == before


def test_mental_model_quality_thresholds_fail_closed(tmp_path: Path) -> None:
    provider = ReflectionToolProvider(
        tmp_path,
        maintenance_enabled=True,
        write_candidates=True,
    )
    _seed_two_sources(provider)
    provider._config["reflection"]["candidate_min_citations"] = 3
    provider._reflection_transport = lambda prompt: _response()
    before = provider._conn.total_changes

    payload = _payload(
        ScopeRecallToolService(provider),
        {"query": "Aurora storage", "propose_memory": True},
    )

    assert payload["ok"] is True
    assert payload["candidate"]["created"] is False
    assert payload["candidate"]["reason"] == "insufficient_citations"
    assert provider._conn.total_changes == before


def test_valid_citation_ids_do_not_authorize_unsupported_answer_candidate(
    tmp_path: Path,
) -> None:
    provider = ReflectionToolProvider(
        tmp_path,
        maintenance_enabled=True,
        write_candidates=True,
    )
    _seed_two_sources(provider)
    response = json.loads(_response())
    response["answer"] = "Aurora uses MongoDB in ap-south-1."
    provider._reflection_transport = lambda prompt: json.dumps(response)
    before = provider._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    payload = _payload(
        ScopeRecallToolService(provider),
        {"query": "Aurora storage", "propose_memory": True},
    )

    assert payload["ok"] is True
    assert payload["candidate"]["created"] is False
    assert payload["candidate"]["reason"] == "unsupported_answer"
    assert provider._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == before


def test_same_provenance_root_does_not_satisfy_candidate_source_diversity(
    tmp_path: Path,
) -> None:
    provider = ReflectionToolProvider(
        tmp_path,
        maintenance_enabled=True,
        write_candidates=True,
    )
    _insert_memory(
        provider,
        memory_id="memory-one",
        content="Aurora uses PostgreSQL for durable project state.",
        source="nightly-digest",
        provenance_root="message:shared-root",
    )
    _insert_memory(
        provider,
        memory_id="memory-two",
        content="Aurora requires row-level security for tenant isolation.",
        source="nightly-digest",
        provenance_root="message:shared-root",
    )
    provider._reflection_transport = lambda prompt: _response()

    payload = _payload(
        ScopeRecallToolService(provider),
        {"query": "Aurora storage", "propose_memory": True},
    )

    assert payload["ok"] is True
    assert payload["candidate"]["created"] is False
    assert payload["candidate"]["reason"] == "insufficient_source_diversity"
    assert payload["candidate"]["quality"]["source_count"] == 1


def test_mental_model_candidate_is_needs_review_and_idempotent(tmp_path: Path) -> None:
    provider = ReflectionToolProvider(
        tmp_path,
        maintenance_enabled=True,
        write_candidates=True,
    )
    _seed_two_sources(provider)
    provider._reflection_transport = lambda prompt: _response()
    service = ScopeRecallToolService(provider)

    first = _payload(
        service,
        {"query": "Aurora storage", "propose_memory": True},
    )
    second = _payload(
        service,
        {"query": "Aurora storage", "propose_memory": True},
    )

    assert first["candidate"]["created"] is True
    assert second["candidate"]["created"] is False
    assert second["candidate"]["idempotent"] is True
    assert second["candidate"]["id"] == first["candidate"]["id"]
    row = provider._conn.execute(
        "SELECT source, target, content, metadata FROM memories WHERE id = ?",
        (first["candidate"]["id"],),
    ).fetchone()
    metadata = json.loads(row["metadata"])
    assert row["source"] == "reflection"
    assert row["target"] == "memory"
    assert row["content"] == (
        "Aurora uses PostgreSQL.\nAurora requires row-level security."
    )
    assert "tenant-isolated storage design" not in row["content"]
    assert metadata["lifecycle"] == "candidate"
    assert metadata["candidate_status"] == "needs_review"
    assert metadata["memory_type"] == "mental_model"
    assert metadata["reflection_candidate"] is True
    assert metadata["evidence_refs"] == ["memory:memory-one", "memory:memory-two"]
    assert metadata["reflection"]["inferences"] == []
    assert metadata["reflection"]["uncertainties"] == []
    assert provider._conn.execute(
        "SELECT COUNT(*) FROM governance_audit_events WHERE target_id = ?",
        (first["candidate"]["id"],),
    ).fetchone()[0] == 1


def test_candidate_audit_failure_rolls_back_all_surfaces(
    tmp_path: Path,
    monkeypatch,
) -> None:
    provider = ReflectionToolProvider(
        tmp_path,
        maintenance_enabled=True,
        write_candidates=True,
    )
    _seed_two_sources(provider)
    provider._reflection_transport = lambda prompt: _response()
    before = {
        table: provider._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("memories", "memories_fts", "memory_entities", "governance_audit_events")
    }

    def fail_audit(*args, **kwargs):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(
        "scope_recall.reflection_tooling.record_governance_audit_event",
        fail_audit,
    )
    payload = _payload(
        ScopeRecallToolService(provider),
        {"query": "Aurora storage", "propose_memory": True},
    )

    assert "injected audit failure" in payload["error"]
    after = {
        table: provider._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    }
    assert after == before
