"""End-to-end tests for the memory quality kernel release scenarios.

These cases encode the Phase 10 benchmark contract: durable preferences and
fresh facts should remain useful, while task progress, tool traces, secrets,
stale facts, and conflicting facts must not silently dominate recall or
promotion.
"""
from __future__ import annotations

import json
import sqlite3
import threading

from scope_recall.candidate_promotion import classify_candidate_row
from scope_recall.freshness import attach_freshness_metadata
from scope_recall.memory_quality import quality_decision_for_memory
from scope_recall.models import RecallItem
from scope_recall.recall import RecallService
from scope_recall.sql_store import ensure_schema, now_iso


class DummyProvider:
    def __init__(self, items: list[RecallItem]) -> None:
        self._retrieval_config = {
            "mode": "lexical",
            "min_score": 0.01,
            "include_general": "same-scope",
            "fact_freshness_enabled": True,
            "fact_freshness_stale_penalty": 0.35,
            "relation_rerank_enabled": True,
            "relation_supersedes_boost": 0.08,
        }
        self._scope_id = "local-scope"
        self._shared_scope_id = "shared-scope"
        self._accessible_scope_ids = [self._scope_id, self._shared_scope_id]
        self._items = list(items)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        ensure_schema(self._conn)
        for item in self._items:
            self._conn.execute(
                """
                INSERT INTO memories(id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
                    agent_identity, agent_workspace, session_id, source, target, content, summary, created_at, updated_at,
                    last_recalled_turn, dedup_key, metadata)
                VALUES (?, 'shared-scope', 'telegram', 'joy', 'dm', '', '', 'yuheng', 'hermes', 'kernel-fixture',
                    'fixture', 'project', ?, ?, '2026-07-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00',
                    0, ?, ?)
                """,
                (item.id, item.content, item.summary, item.id, json.dumps(item.metadata or {}, ensure_ascii=False, sort_keys=True)),
            )
        self._conn.commit()

    def _search_db_memories(self, query: str, *, limit: int) -> list[RecallItem]:
        return self._items[:limit]

    def _search_vector_memories(self, query: str, *, limit: int) -> list[RecallItem]:
        return []

    def _search_curated_memories(self, query: str) -> list[RecallItem]:
        return []

    def _dedup_key(self, content: str) -> str:
        return str(content).lower()

    def _config_value(self, key: str, default):
        return default

    def _require_conn(self):
        return self._conn

    def close(self) -> None:
        self._conn.close()


def _row(memory_id: str, content: str, *, metadata: dict | None = None, target: str = "memory") -> dict:
    return {
        "id": memory_id,
        "scope_id": "shared-scope",
        "source": "fixture",
        "target": target,
        "content": content,
        "summary": content,
        "updated_at": "2026-07-01T00:00:00+00:00",
        "metadata": json.dumps(metadata or {}, ensure_ascii=False),
    }


def _item(memory_id: str, content: str, score: float, *, memory_type: str = "factual") -> RecallItem:
    return RecallItem(
        id=memory_id,
        content=content,
        summary=content,
        source="fixture",
        target="project",
        score=score,
        updated_at="2026-07-01T00:00:00+00:00",
        metadata={"memory_type": memory_type, "scope_id": "shared-scope", "entities": ["Northstar"]},
    )


def test_quality_contract_keeps_preferences_promotable_and_blocks_low_value_or_secret_candidates():
    preference = quality_decision_for_memory(
        _row(
            "pref",
            "Joy prefers concise Chinese progress updates for narrow technical questions.",
            metadata={"lifecycle": "candidate", "memory_type": "preference", "confidence": 0.92, "importance": 0.8, "evidence_refs": ["journal:1"]},
            target="user",
        )
    )
    progress = quality_decision_for_memory(
        _row(
            "progress",
            "Phase 2 done, continue next time.",
            metadata={"lifecycle": "candidate", "memory_type": "episodic", "confidence": 0.8, "importance": 0.7, "evidence_refs": ["journal:2"]},
        )
    )
    tool_trace = quality_decision_for_memory(
        _row(
            "tool",
            "Tool execution summary (terminal): output omitted; pytest passed.",
            metadata={"lifecycle": "candidate", "memory_type": "tool_trace", "confidence": 0.9, "importance": 0.8, "evidence_refs": ["journal:3"]},
        )
    )
    secret = quality_decision_for_memory(
        _row(
            "secret",
            "Use token=[REDACTED] for deploy.",
            metadata={"lifecycle": "candidate", "memory_type": "factual", "confidence": 0.9, "importance": 0.8, "evidence_refs": ["journal:4"]},
        )
    )

    assert preference.action == "promote"
    assert progress.action in {"archive", "keep_candidate"}
    assert tool_trace.action in {"archive", "keep_candidate"}
    assert secret.action == "keep_candidate"
    assert secret.risk == "high"


def test_candidate_promotion_requires_evidence_and_preserves_active_conflict_for_review():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        now = now_iso()
        conn.execute(
            """
            INSERT INTO memories(id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
                agent_identity, agent_workspace, session_id, source, target, content, summary, created_at, updated_at,
                last_recalled_turn, dedup_key, metadata)
            VALUES('active-fact', 'shared-scope', 'telegram', 'joy', 'dm', '', '', 'yuheng', 'hermes', 'kernel-fixture',
                'fixture', 'project', 'Northstar API base URL is https://api.northstar.example/v2.',
                'Northstar API base URL is https://api.northstar.example/v2.', ?, ?, 0, 'active-fact', ?)
            """,
            (now, now, json.dumps({"memory_type": "factual"}, ensure_ascii=False)),
        )
        conn.execute(
            """
            INSERT INTO memories(id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
                agent_identity, agent_workspace, session_id, source, target, content, summary, created_at, updated_at,
                last_recalled_turn, dedup_key, metadata)
            VALUES('candidate-conflict', 'shared-scope', 'telegram', 'joy', 'dm', '', '', 'yuheng', 'hermes', 'kernel-fixture',
                'fixture', 'project', 'Northstar API base URL is https://api.northstar.example/v2.',
                'Northstar API base URL is https://api.northstar.example/v2.', ?, ?, 0, 'candidate-conflict', ?)
            """,
            (
                now,
                now,
                json.dumps({"lifecycle": "candidate", "memory_type": "factual", "confidence": 0.95, "importance": 0.8, "evidence_refs": ["journal:5"]}, ensure_ascii=False),
            ),
        )
        conn.execute(
            """
            INSERT INTO memories(id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
                agent_identity, agent_workspace, session_id, source, target, content, summary, created_at, updated_at,
                last_recalled_turn, dedup_key, metadata)
            VALUES('candidate-no-evidence', 'shared-scope', 'telegram', 'joy', 'dm', '', '', 'yuheng', 'hermes', 'kernel-fixture',
                'fixture', 'project', 'Northstar CLI command is northstar deploy.', 'Northstar CLI command is northstar deploy.',
                ?, ?, 0, 'candidate-no-evidence', ?)
            """,
            (
                now,
                now,
                json.dumps({"lifecycle": "candidate", "memory_type": "factual", "confidence": 0.95, "importance": 0.8}, ensure_ascii=False),
            ),
        )
        rows = {row["id"]: row for row in conn.execute("SELECT * FROM memories WHERE id LIKE 'candidate-%'").fetchall()}
        conflict = classify_candidate_row(rows["candidate-conflict"], conn)
        missing = classify_candidate_row(rows["candidate-no-evidence"], conn)
    finally:
        conn.close()

    assert conflict.action == "keep_candidate"
    assert conflict.reason == "active_memory_conflict"
    assert conflict.conflict_with == "active-fact"
    assert missing.action == "keep_candidate"
    assert missing.reason == "missing_evidence_anchor"


def test_stale_factual_memory_is_penalized_below_current_fact_and_keeps_live_check_metadata():
    stale = _item("northstar-old", "Northstar API base URL is https://old.northstar.invalid/v1.", 0.92)
    current = _item("northstar-current", "Northstar API base URL is https://api.northstar.example/v2.", 0.74)
    provider = DummyProvider([stale, current])
    try:
        now = now_iso()
        provider._require_conn().execute(
            """
            INSERT INTO fact_freshness(id, subject_type, subject_id, fact_key, truth_type, validator_kind,
                ttl_days, last_checked_at, valid_until, status, stale_reason, created_at, updated_at)
            VALUES('fresh-old', 'memory', 'northstar-old', 'api_base_url', 'config', 'http', 7, ?, '2026-01-01T00:00:00+00:00', 'stale', 'superseded by live check', ?, ?)
            """,
            (now, now, now),
        )
        provider._require_conn().execute(
            """
            INSERT INTO fact_freshness(id, subject_type, subject_id, fact_key, truth_type, validator_kind,
                ttl_days, last_checked_at, valid_until, status, stale_reason, created_at, updated_at)
            VALUES('fresh-current', 'memory', 'northstar-current', 'api_base_url', 'config', 'http', 7, ?, '2027-01-01T00:00:00+00:00', 'current', '', ?, ?)
            """,
            (now, now, now),
        )
        provider._require_conn().commit()
        results = RecallService(provider).search_memories("Northstar API base URL", limit=2)
    finally:
        provider.close()

    assert [item.id for item in results] == ["northstar-current", "northstar-old"]
    assert results[1].metadata["needs_live_check"] is True
    assert results[1].metadata["fact_freshness_status"] == "stale"


def test_attach_freshness_metadata_exposes_validator_contract_without_overwriting_fact():
    metadata = {}
    penalty = attach_freshness_metadata(
        metadata,
        {
            "status": "needs_live_check",
            "fact_key": "service_port",
            "truth_type": "config",
            "validator_kind": "file_exists",
            "last_checked_at": "2026-01-01T00:00:00+00:00",
            "valid_until": "2026-01-02T00:00:00+00:00",
            "stale_reason": "ttl expired",
            "needs_live_check": True,
        },
    )

    assert penalty > 0
    assert metadata["validator_kind"] == "file_exists"
    assert metadata["needs_live_check"] is True
