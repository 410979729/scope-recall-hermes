from __future__ import annotations

import json
import sqlite3
import threading

from plugins.memory import load_memory_provider

from scope_recall.graph import ensure_graph_schema
from scope_recall.models import RecallItem
from scope_recall.recall import RecallService
from scope_recall.sql_store import ensure_schema, now_iso


class DummyProvider:
    def __init__(self, retrieval_config: dict, items: list[RecallItem]) -> None:
        self._retrieval_config = dict(retrieval_config)
        self._scope_id = "local-scope"
        self._shared_scope_id = "shared-scope"
        self._accessible_scope_ids = [self._scope_id, self._shared_scope_id]
        self._items = list(items)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE memories(id TEXT PRIMARY KEY, scope_id TEXT NOT NULL DEFAULT '', metadata TEXT NOT NULL DEFAULT '{}')"
        )
        ensure_graph_schema(self._conn)
        self._conn.execute(
            """
            CREATE TABLE fact_freshness(
                id TEXT PRIMARY KEY,
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                fact_key TEXT NOT NULL,
                truth_type TEXT NOT NULL,
                validator_kind TEXT NOT NULL DEFAULT '',
                validator_spec TEXT NOT NULL DEFAULT '{}',
                ttl_days INTEGER NOT NULL DEFAULT 0,
                last_checked_at TEXT,
                valid_until TEXT,
                status TEXT NOT NULL DEFAULT 'unknown',
                stale_reason TEXT NOT NULL DEFAULT '',
                superseded_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        for item in self._items:
            self._conn.execute(
                "INSERT INTO memories(id, scope_id, metadata) VALUES (?, ?, ?)",
                (item.id, str((item.metadata or {}).get("scope_id") or self._shared_scope_id), json.dumps(item.metadata or {}, ensure_ascii=False)),
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


def _item(memory_id: str, score: float) -> RecallItem:
    return RecallItem(
        id=memory_id,
        content=f"Northstar API base URL config claim {memory_id}.",
        summary=f"Northstar API base URL config claim {memory_id}.",
        source="tool-store",
        target="ops",
        score=score,
        updated_at="2026-06-01T00:00:00+00:00",
        metadata={"lexical_score": score, "scope_id": "shared-scope", "memory_type": "factual", "entities": ["Northstar"]},
    )


def _mark_freshness(conn: sqlite3.Connection, memory_id: str, *, status: str, fact_key: str = "api_base_url") -> None:
    now = now_iso()
    valid_until = "2027-01-01T00:00:00+00:00" if status in {"current", "fresh", "valid", "verified"} else "2026-01-01T00:00:00+00:00"
    conn.execute(
        """
        INSERT INTO fact_freshness(
            id, subject_type, subject_id, fact_key, truth_type, validator_kind,
            ttl_days, last_checked_at, valid_until, status, stale_reason, created_at, updated_at
        ) VALUES (?, 'memory', ?, ?, 'config', 'manual-live-check', 7, ?, ?, ?, 'fixture', ?, ?)
        """,
        (f"fresh_{memory_id}", memory_id, fact_key, now, valid_until, status, now, now),
    )
    conn.commit()


def test_fact_freshness_stale_memory_is_marked_and_downgraded_below_current_fact():
    stale = _item("old-northstar-url", 0.92)
    current = _item("current-northstar-url", 0.74)
    provider = DummyProvider(
        {
            "mode": "lexical",
            "min_score": 0.01,
            "include_general": "same-scope",
            "fact_freshness_enabled": True,
            "fact_freshness_stale_penalty": 0.35,
        },
        [stale, current],
    )
    try:
        _mark_freshness(provider._require_conn(), "old-northstar-url", status="stale")
        _mark_freshness(provider._require_conn(), "current-northstar-url", status="current")

        results = RecallService(provider).search_memories("Northstar API base URL latest config", limit=2)

        assert [item.id for item in results] == ["current-northstar-url", "old-northstar-url"]
        stale_meta = results[1].metadata or {}
        assert stale_meta["fact_freshness_status"] == "stale"
        assert stale_meta["needs_live_check"] is True
        assert stale_meta["fact_freshness_penalty"] > 0
        assert results[1].score < results[0].score
    finally:
        provider.close()


def test_profile_marks_stale_operational_fact_as_needing_live_check(tmp_path):
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-freshness-profile",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        stored = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Northstar API base URL is https://old-api.invalid/v1 according to an old check.",
                    "target": "ops",
                    "memory_type": "factual",
                    "entities": ["Northstar"],
                },
            )
        )
        memory_id = stored["id"]
        with plugin._lock:
            _mark_freshness(plugin._require_conn(), memory_id, status="needs_live_check")

        profile = json.loads(
            plugin.handle_tool_call(
                "scope_recall_profile",
                {"query": "Northstar API base URL", "targets": ["ops"], "include_curated": False, "limit": 5, "max_chars": 800},
            )
        )

        [item] = profile["sections"]["ops"]["items"]
        assert item["id"] == memory_id
        assert item["needs_live_check"] is True
        assert item["fact_freshness_status"] == "needs_live_check"
        assert "needs-live-check" in profile["context"]
    finally:
        plugin.shutdown()


def test_context_marks_stale_operational_fact_as_needing_live_check_without_db_writes(tmp_path):
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-freshness-context",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        stored = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Northstar API base URL is https://old-api.invalid/v1 according to an old check.",
                    "target": "ops",
                    "memory_type": "factual",
                    "entities": ["Northstar"],
                },
            )
        )
        memory_id = stored["id"]
        with plugin._lock:
            conn = plugin._require_conn()
            _mark_freshness(conn, memory_id, status="needs_live_check")
            before_changes = conn.total_changes

        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_context",
                {"query": "Northstar API base URL", "limit": 5, "max_chars": 800},
            )
        )

        with plugin._lock:
            after_changes = plugin._require_conn().total_changes
        matches = [row for row in payload["results"] if row["id"] == memory_id]
        assert matches
        item = matches[0]
        assert after_changes == before_changes
        assert item["needs_live_check"] is True
        assert item["fact_freshness_status"] == "needs_live_check"
        assert "needs-live-check" in payload["context"]
    finally:
        plugin.shutdown()


def test_doctor_experience_reports_fact_freshness_coverage(tmp_path):
    from scope_recall.doctor_experience import experience_report

    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True)
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        now = now_iso()
        conn.execute(
            """
            INSERT INTO memories(
                id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
                agent_identity, agent_workspace, session_id, source, target, content, summary,
                created_at, updated_at, last_recalled_turn, dedup_key, metadata
            ) VALUES (
                'fact-memory', 'shared-scope', 'telegram', 'joy', 'dm', '', '', 'yuheng', 'hermes', 's',
                'tool-store', 'ops', 'Northstar API base URL is old.', 'Northstar API base URL is old.',
                ?, ?, 0, 'fact-memory', ?
            )
            """,
            (now, now, json.dumps({"memory_type": "factual", "entities": ["Northstar"]}, ensure_ascii=False)),
        )
        _mark_freshness(conn, "fact-memory", status="stale")
    finally:
        conn.close()

    payload, check, recommendations = experience_report(tmp_path)

    assert check == {"ok": True, "failures": []}
    assert payload["fact_freshness"]["tracked_facts"] == 1
    assert payload["fact_freshness"]["by_status"] == {"stale": 1}
    assert payload["fact_freshness"]["needs_live_check"] == 1
    assert any("Fact freshness" in item for item in recommendations)
