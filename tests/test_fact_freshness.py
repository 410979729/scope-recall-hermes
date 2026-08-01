"""Tests for factual-memory freshness metadata, reports, and recall behavior.

Freshness is advisory evidence, so these cases prevent it from overwriting the underlying fact."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plugins.memory import load_memory_provider

from scope_recall.freshness import (
    _parse_iso,
    attach_freshness_metadata,
    backfill_untracked_memory_freshness,
    fact_freshness_report,
    freshness_penalty,
    normalize_validator_kind,
)
from scope_recall.graph import ensure_graph_schema
from scope_recall.models import RecallItem
from scope_recall.recall import RecallService
import scope_recall.sql_store as sql_store_module
from scope_recall.sql_store import ensure_schema, now_iso, store_row


def _disable_unrelated_vector_runtime(hermes_home: Path) -> None:
    """Keep freshness tests independent of optional native vector runtimes."""

    config_path = hermes_home / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"vector": {"enabled": False}}) + "\n",
        encoding="utf-8",
    )


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


def test_freshness_validator_kind_normalization_contract():
    assert normalize_validator_kind("manual-live-check") == "manual"
    assert normalize_validator_kind("url") == "http"
    assert normalize_validator_kind("shell") == "command"
    assert normalize_validator_kind("path") == "file_exists"
    assert normalize_validator_kind("static") == "none"
    assert normalize_validator_kind("custom-validator") == "manual"


def _store_direct_memory(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    memory_type: str,
    content: str = "Freshness policy contract memory.",
    freshness: dict[str, object] | None = None,
) -> tuple[str, str, str, bool]:
    metadata: dict[str, object] = {"memory_type": memory_type}
    if freshness is not None:
        metadata["freshness"] = freshness
    return store_row(
        conn,
        memory_id=memory_id,
        scope_id="shared-scope",
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="freshness-contract",
        source="tool-store",
        target="memory",
        content=content,
        metadata=json.dumps(metadata),
    )


@pytest.mark.parametrize(
    ("memory_type", "expected_status", "expected_ttl", "expects_deadline"),
    [
        ("factual", "needs_live_check", 30, False),
        ("preference", "current", 365, True),
        ("procedure", "current", 180, True),
        ("workflow", "current", 180, True),
        ("tool_trace", "current", 30, True),
        ("project", "needs_live_check", 30, False),
        ("summary", "needs_live_check", 30, False),
        ("pitfall", "current", 180, True),
        ("decision", "current", 0, False),
        ("episodic", "current", 0, False),
        ("resource", "current", 365, True),
        ("constraint", "current", 365, True),
    ],
)
def test_store_row_initializes_freshness_by_memory_type_policy(
    memory_type: str,
    expected_status: str,
    expected_ttl: int,
    expects_deadline: bool,
):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        memory_id = f"policy-{memory_type}"
        _store_direct_memory(conn, memory_id=memory_id, memory_type=memory_type)
        row = conn.execute(
            "SELECT truth_type, validator_kind, ttl_days, valid_until, status "
            "FROM fact_freshness WHERE subject_id = ?",
            (memory_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["truth_type"] == memory_type
    assert row["status"] == expected_status
    assert row["ttl_days"] == expected_ttl
    assert bool(row["valid_until"]) is expects_deadline


def test_duplicate_store_repairs_missing_freshness_without_overwriting_existing_state():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    content = "Duplicate legacy factual memory for freshness repair."
    _store_direct_memory(
        conn,
        memory_id="duplicate-legacy",
        memory_type="factual",
        content=content,
    )
    conn.execute("DELETE FROM fact_freshness WHERE subject_id = ?", ("duplicate-legacy",))
    conn.commit()

    memory_id, _summary, _updated_at, inserted = _store_direct_memory(
        conn,
        memory_id="duplicate-attempt",
        memory_type="factual",
        content=content,
    )
    repaired = conn.execute(
        "SELECT status FROM fact_freshness WHERE subject_id = ?",
        ("duplicate-legacy",),
    ).fetchone()
    assert memory_id == "duplicate-legacy"
    assert inserted is False
    assert repaired is not None
    assert str(repaired[0]) == "needs_live_check"

    conn.execute(
        "UPDATE fact_freshness SET status='current', ttl_days=77 WHERE subject_id = ?",
        ("duplicate-legacy",),
    )
    conn.commit()
    _store_direct_memory(
        conn,
        memory_id="duplicate-attempt-two",
        memory_type="factual",
        content=content,
    )
    preserved = conn.execute(
        "SELECT status, ttl_days FROM fact_freshness WHERE subject_id = ?",
        ("duplicate-legacy",),
    ).fetchone()
    assert tuple(preserved) == ("current", 77)


def test_store_row_rolls_back_memory_when_freshness_initialization_fails(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    def fail_freshness(*_args, **_kwargs):
        raise RuntimeError("freshness companion failed")

    monkeypatch.setattr(sql_store_module, "upsert_memory_freshness", fail_freshness)
    try:
        with pytest.raises(RuntimeError, match="freshness companion failed"):
            _store_direct_memory(conn, memory_id="fail-closed", memory_type="factual")
        assert conn.execute(
            "SELECT COUNT(*) FROM memories WHERE id = 'fail-closed'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("validator_kind", "validator_spec"),
    [
        ("command", {"command_id": "git-head"}),
        ("file_exists", {"path": "state/config.yaml"}),
        ("http", {"url": "https://status.example.invalid/health"}),
    ],
)
def test_freshness_validator_specs_accept_only_declarative_safe_shapes(
    validator_kind: str,
    validator_spec: dict[str, str],
):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        memory_id = f"safe-validator-{validator_kind}"
        _store_direct_memory(
            conn,
            memory_id=memory_id,
            memory_type="factual",
            freshness={
                "validator_kind": validator_kind,
                "validator_spec": validator_spec,
            },
        )
        stored = conn.execute(
            "SELECT validator_spec FROM fact_freshness WHERE subject_id = ?",
            (memory_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert json.loads(stored) == validator_spec


@pytest.mark.parametrize(
    ("validator_kind", "validator_spec"),
    [
        ("command", {"command": "rm -rf workspace"}),
        ("command", {"command_id": "arbitrary-unregistered-command"}),
        ("file_exists", {"path": "/etc/shadow"}),
        ("file_exists", {"path": "state/../secrets.txt"}),
        ("http", {"url": "http://127.0.0.1/admin"}),
        ("http", {"url": "https://user:pass@example.invalid/health"}),
        ("http", {"url": "https://example.invalid/health?token=value"}),
    ],
)
def test_freshness_validator_specs_reject_unsafe_execution_targets(
    validator_kind: str,
    validator_spec: dict[str, str],
):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    try:
        with pytest.raises(ValueError, match="validator_spec"):
            _store_direct_memory(
                conn,
                memory_id=f"unsafe-validator-{validator_kind}",
                memory_type="factual",
                freshness={
                    "validator_kind": validator_kind,
                    "validator_spec": validator_spec,
                },
            )
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    finally:
        conn.close()


def test_untracked_freshness_is_penalized_and_requires_live_check():
    metadata: dict[str, object] = {}

    penalty = attach_freshness_metadata(metadata, None)

    assert 0.0 < penalty < freshness_penalty({"status": "needs_live_check"})
    assert metadata["fact_freshness_status"] == "untracked"
    assert metadata["needs_live_check"] is True
    assert metadata["fact_freshness_penalty"] == penalty
    assert "UNTRACKED" in str(metadata["freshness_warning"])


def test_expired_freshness_penalty_is_not_lighter_than_stale():
    assert freshness_penalty({"status": "expired"}) >= freshness_penalty(
        {"status": "stale"}
    )


def test_parse_iso_treats_naive_timestamps_as_utc(monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("requires POSIX tzset")
    old_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        assert _parse_iso("2026-01-01T00:00:00") == datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    finally:
        if old_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", old_tz)
        time.tzset()


def test_fact_freshness_report_tolerates_invalid_memory_metadata():
    conn = sqlite3.connect(":memory:")
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
                'bad-metadata-fact', 'shared-scope', 'telegram', 'joy', 'dm', '', '', 'yuheng', 'hermes', 's',
                'tool-store', 'ops', 'Bad metadata historical row.', 'Bad metadata historical row.',
                ?, ?, 0, 'bad-metadata-fact', 'not-json'
            )
            """,
            (now, now),
        )
        _mark_freshness(conn, "bad-metadata-fact", status="needs_live_check")

        report = fact_freshness_report(conn, scope_ids=["shared-scope"])
    finally:
        conn.close()

    assert report["tracked_facts"] == 1
    assert report["needs_live_check"] == 1
    assert report["coverage"]["factual_memories"] == 0


def test_empty_memory_type_falls_back_to_category_in_report_and_backfill():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        now = now_iso()
        metadata = {"memory_type": "", "category": "factual", "lifecycle": "promoted"}
        conn.execute(
            """
            INSERT INTO memories(
                id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
                agent_identity, agent_workspace, session_id, source, target, content, summary,
                created_at, updated_at, last_recalled_turn, dedup_key, metadata
            ) VALUES (
                'empty-type-factual', 'shared-scope', 'telegram', 'joy', 'dm', '', '', 'yuheng', 'hermes', 's',
                'tool-store', 'memory', 'Explicit empty memory type factual row.', 'Explicit empty memory type factual row.',
                ?, ?, 0, 'empty-type-factual', ?
            )
            """,
            (now, now, json.dumps(metadata, ensure_ascii=False, sort_keys=True)),
        )
        conn.commit()

        report = fact_freshness_report(conn, scope_ids=["shared-scope"])
        plan = backfill_untracked_memory_freshness(
            conn,
            scope_ids=["shared-scope"],
            apply=False,
            limit=10,
        )
    finally:
        conn.close()

    assert report["status"] == "needs_review"
    assert report["coverage"] == {
        "factual_memories": 1,
        "tracked_memory_facts": 0,
        "coverage_percent": 0.0,
    }
    assert plan["eligible"] == 1
    assert plan["ids"] == ["empty-type-factual"]


def test_freshness_backfill_reuses_ordinary_lifecycle_policy_for_general_scratch():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        now = now_iso()
        for memory_id, target, lifecycle in (
            ("active-fact", "ops", "active"),
            ("general-scratch-fact", "general", "scratch"),
            ("durable-scratch-fact", "memory", "scratch"),
            ("candidate-fact", "memory", "candidate"),
        ):
            metadata = {"memory_type": "factual", "lifecycle": lifecycle}
            conn.execute(
                """
                INSERT INTO memories(
                    id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
                    agent_identity, agent_workspace, session_id, source, target, content, summary,
                    created_at, updated_at, last_recalled_turn, dedup_key, metadata
                ) VALUES (?, 'shared-scope', 'telegram', 'joy', 'dm', '', '', 'yuheng', 'hermes', 's',
                    'tool-store', ?, 'Freshness fact.', 'Freshness fact.', ?, ?, 0, ?, ?)
                """,
                (memory_id, target, now, now, memory_id, json.dumps(metadata, ensure_ascii=False)),
            )
        conn.commit()

        report = backfill_untracked_memory_freshness(conn, scope_ids=["shared-scope"], apply=False)
    finally:
        conn.close()

    assert set(report["ids"]) == {"active-fact", "general-scratch-fact"}
    assert report["eligible"] == 2


def test_fact_freshness_global_report_matches_scoped_memory_join_semantics():
    conn = sqlite3.connect(":memory:")
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
                'memory-fact', 'shared-scope', 'telegram', 'joy', 'dm', '', '', 'yuheng', 'hermes', 's',
                'tool-store', 'ops', 'Tracked fact.', 'Tracked fact.', ?, ?, 0, 'memory-fact', ?
            )
            """,
            (now, now, json.dumps({"memory_type": "factual"}, ensure_ascii=False)),
        )
        for row_id, subject_type, subject_id in (
            ("fresh-memory", "memory", "memory-fact"),
            ("fresh-service", "service", "svc-1"),
            ("fresh-orphan", "memory", "missing-memory"),
        ):
            conn.execute(
                """
                INSERT INTO fact_freshness(
                    id, subject_type, subject_id, fact_key, truth_type, validator_kind,
                    ttl_days, last_checked_at, valid_until, status, stale_reason, created_at, updated_at
                ) VALUES (?, ?, ?, 'endpoint', 'config', 'manual', 7, ?, ?, 'needs_live_check', 'fixture', ?, ?)
                """,
                (row_id, subject_type, subject_id, now, now, now, now),
            )
        conn.commit()

        global_report = fact_freshness_report(conn)
        scoped_report = fact_freshness_report(conn, scope_ids=["shared-scope"])
    finally:
        conn.close()

    assert global_report["tracked_facts"] == 1
    assert global_report["coverage"]["tracked_memory_facts"] == 1
    assert global_report["coverage"]["coverage_percent"] == 100.0
    assert scoped_report["tracked_facts"] == global_report["tracked_facts"]
    assert scoped_report["coverage"] == global_report["coverage"]


def test_fact_freshness_report_groups_normalized_validator_kinds():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        now = now_iso()
        for index, validator in enumerate(["manual-live-check", "url", "shell", "path", "static"], start=1):
            conn.execute(
                """
                INSERT INTO memories(
                    id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
                    agent_identity, agent_workspace, session_id, source, target, content, summary,
                    created_at, updated_at, last_recalled_turn, dedup_key, metadata
                ) VALUES (?, 'shared-scope', 'telegram', 'joy', 'dm', '', '', 'yuheng', 'hermes', 's',
                    'tool-store', 'ops', 'Freshness fact.', 'Freshness fact.', ?, ?, 0, ?, ?)
                """,
                (
                    f"memory-{index}",
                    now,
                    now,
                    f"memory-{index}",
                    json.dumps({"memory_type": "factual"}, ensure_ascii=False),
                ),
            )
            conn.execute(
                """
                INSERT INTO fact_freshness(
                    id, subject_type, subject_id, fact_key, truth_type, validator_kind,
                    ttl_days, last_checked_at, valid_until, status, stale_reason, created_at, updated_at
                ) VALUES (?, 'memory', ?, 'endpoint', 'config', ?, 7, ?, ?, 'needs_live_check', 'fixture', ?, ?)
                """,
                (f"fresh-{index}", f"memory-{index}", validator, now, "2026-01-01T00:00:00+00:00", now, now),
            )
        conn.commit()

        report = fact_freshness_report(conn)
    finally:
        conn.close()

    assert report["by_validator_kind"] == {"command": 1, "file_exists": 1, "http": 1, "manual": 1, "none": 1}
    assert report["needs_live_check"] == 5


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


def test_advisory_warns_when_stale_out_ranks_current_and_strict_excludes_it():
    stale = _item("high-score-stale", 0.99)
    current = _item("low-score-current", 0.30)
    provider = DummyProvider(
        {
            "mode": "lexical",
            "min_score": 0.01,
            "include_general": "same-scope",
            "fact_freshness_enabled": True,
            "fact_freshness_stale_penalty": 0.05,
        },
        [stale, current],
    )
    service = RecallService(provider)
    try:
        _mark_freshness(provider._require_conn(), stale.id, status="stale")
        _mark_freshness(provider._require_conn(), current.id, status="current")

        advisory = service.search_memories(
            "Northstar API base URL latest config",
            limit=2,
            recall_mode="advisory",
        )
        assert [item.id for item in advisory] == [stale.id, current.id]
        stale_metadata = advisory[0].metadata or {}
        assert "STALE" in str(stale_metadata["freshness_warning"])
        assert stale_metadata["ranking_warning"] == "stale_result_ranked_above_current"

        strict = service.search_memories(
            "Northstar API base URL latest config",
            limit=2,
            recall_mode="strict",
        )
        assert [item.id for item in strict] == [current.id]
        assert service.last_funnel_trace["filters"]["freshness_strict_excluded"] == 1
        assert [item.id for item in service.last_rejected_candidates] == [stale.id]
        assert (service.last_rejected_candidates[0].metadata or {})[
            "rejected_reason"
        ] == "freshness_strict_excluded"
    finally:
        provider.close()


def test_freshness_penalty_respects_explicit_zero_overrides():
    config = {
        "fact_freshness_untracked_penalty": 0.0,
        "fact_freshness_needs_live_check_penalty": 0.0,
        "fact_freshness_stale_penalty": 0.0,
        "fact_freshness_expired_penalty": 0.0,
    }

    assert freshness_penalty(None, config) == 0.0
    assert freshness_penalty({"status": "needs_live_check"}, config) == 0.0
    assert freshness_penalty({"status": "stale"}, config) == 0.0
    assert freshness_penalty({"status": "expired"}, config) == 0.0


def test_public_store_tracks_unverified_factual_memory_as_needs_live_check(tmp_path):
    _disable_unrelated_vector_runtime(tmp_path)
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-freshness-production-writer",
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
                    "content": "Northstar production API base URL is https://api.invalid/v2 and must be live-checked.",
                    "target": "ops",
                    "memory_type": "factual",
                    "entities": ["Northstar"],
                },
            )
        )
        assert stored["stored"] is True
        memory_id = stored["id"]
        with plugin._lock:
            row = plugin._require_conn().execute(
                "SELECT subject_type, subject_id, fact_key, truth_type, validator_kind, status FROM fact_freshness WHERE subject_id = ?",
                (memory_id,),
            ).fetchone()

        assert dict(row) == {
            "subject_type": "memory",
            "subject_id": memory_id,
            "fact_key": "memory_fact",
            "truth_type": "factual",
            "validator_kind": "manual",
            "status": "needs_live_check",
        }
        recalled = json.loads(
            plugin.handle_tool_call(
                "scope_recall_search",
                {"query": "Northstar production API base URL", "limit": 5},
            )
        )
        matches = [entry for entry in recalled["results"] if entry["id"] == memory_id]
        assert matches
        item = matches[0]
        assert item["needs_live_check"] is True
        assert item["fact_freshness_status"] == "needs_live_check"
    finally:
        plugin.shutdown()


def test_public_store_honors_explicit_current_freshness_with_ttl(tmp_path):
    _disable_unrelated_vector_runtime(tmp_path)
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-freshness-explicit",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        store_schema = next(schema for schema in plugin.get_tool_schemas() if schema["name"] == "scope_recall_store")
        assert "freshness" in store_schema["parameters"]["properties"]
        stored = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Northstar status endpoint is https://status.invalid after an explicit live check.",
                    "target": "ops",
                    "memory_type": "factual",
                    "freshness": {
                        "fact_key": "northstar_status_endpoint",
                        "truth_type": "environment_fact",
                        "validator_kind": "http",
                        "validator_spec": {"url": "https://status.invalid"},
                        "status": "current",
                        "ttl_days": 2,
                    },
                },
            )
        )
        assert stored["stored"] is True
        with plugin._lock:
            row = plugin._require_conn().execute(
                "SELECT fact_key, truth_type, validator_kind, ttl_days, last_checked_at, valid_until, status FROM fact_freshness WHERE subject_id = ?",
                (stored["id"],),
            ).fetchone()

        assert row["fact_key"] == "northstar_status_endpoint"
        assert row["truth_type"] == "environment_fact"
        assert row["validator_kind"] == "http"
        assert row["ttl_days"] == 2
        assert row["status"] == "current"
        checked = _parse_iso(row["last_checked_at"])
        valid_until = _parse_iso(row["valid_until"])
        assert checked is not None and valid_until is not None
        assert valid_until - checked == timedelta(days=2)
    finally:
        plugin.shutdown()


def test_public_search_exposes_advisory_and_strict_freshness_contract(tmp_path):
    _disable_unrelated_vector_runtime(tmp_path)
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-freshness-public-strict",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        search_schema = next(
            schema
            for schema in plugin.get_tool_schemas()
            if schema["name"] == "scope_recall_search"
        )
        recall_mode_schema = search_schema["parameters"]["properties"]["recall_mode"]
        assert recall_mode_schema["enum"] == ["advisory", "strict"]

        stored = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Strict freshness sentinel for Northstar endpoint lookup.",
                    "target": "ops",
                    "memory_type": "factual",
                },
            )
        )
        with plugin._lock:
            plugin._require_conn().execute(
                "UPDATE fact_freshness SET status = 'stale' WHERE subject_id = ?",
                (stored["id"],),
            )
            plugin._require_conn().commit()

        advisory = json.loads(
            plugin.handle_tool_call(
                "scope_recall_search",
                {
                    "query": "Strict freshness sentinel Northstar endpoint",
                    "recall_mode": "advisory",
                },
            )
        )
        advisory_item = next(
            item for item in advisory["results"] if item["id"] == stored["id"]
        )
        assert "STALE" in advisory_item["freshness_warning"]
        assert "ranking_warning" in advisory_item

        strict = json.loads(
            plugin.handle_tool_call(
                "scope_recall_search",
                {
                    "query": "Strict freshness sentinel Northstar endpoint",
                    "recall_mode": "strict",
                    "include_trace": True,
                },
            )
        )
        assert stored["id"] not in {item["id"] for item in strict["results"]}
        assert strict["funnel_trace"]["filters"]["freshness_strict_excluded"] == 1
    finally:
        plugin.shutdown()


def test_freshness_backfill_operator_script_inventory_apply_and_idempotency(tmp_path):
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir(parents=True)
    db_path = storage_dir / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    for index, memory_type in enumerate(
        ("factual", "preference", "procedure", "decision", "episodic"),
        start=1,
    ):
        _store_direct_memory(
            conn,
            memory_id=f"legacy-{index}",
            memory_type=memory_type,
            content=f"Legacy {memory_type} memory number {index}.",
        )
    conn.execute("DELETE FROM fact_freshness")
    conn.commit()
    conn.close()

    script = Path(__file__).resolve().parents[1] / "scripts" / "backfill.freshness.py"
    dry = subprocess.run(
        [
            sys.executable,
            str(script),
            "--hermes-home",
            str(tmp_path),
            "--batch-size",
            "2",
            "--max-scan",
            "100",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert dry.returncode == 0, dry.stderr
    dry_payload = json.loads(dry.stdout)
    assert dry_payload["status"] == "dry_run"
    assert dry_payload["inventory"]["eligible"] == 5
    assert dry_payload["next_batch"]["eligible"] == 2

    blocked = subprocess.run(
        [
            sys.executable,
            str(script),
            "--hermes-home",
            str(tmp_path),
            "--apply",
            "--operation-id",
            "freshness-backfill-test",
            "--reason",
            "legacy cohort migration",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert blocked.returncode == 2

    apply_command = [
        sys.executable,
        str(script),
        "--hermes-home",
        str(tmp_path),
        "--batch-size",
        "2",
        "--max-batches",
        "10",
        "--max-scan",
        "100",
        "--apply",
        "--maintenance-confirmed",
        "--operation-id",
        "freshness-backfill-test",
        "--reason",
        "legacy cohort migration",
    ]
    applied = subprocess.run(
        apply_command,
        text=True,
        capture_output=True,
        check=False,
    )
    assert applied.returncode == 0, applied.stderr
    payload = json.loads(applied.stdout)
    assert payload["status"] == "complete"
    assert payload["inserted"] == 5
    assert payload["batches"] == 3
    assert Path(payload["backup"]["path"]).is_file()
    assert len(payload["backup"]["sha256"]) == 64
    assert Path(payload["receipt"]["receipt_path"]).is_file()

    replay = subprocess.run(
        apply_command,
        text=True,
        capture_output=True,
        check=False,
    )
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout)["status"] == "idempotent_replay"

    check = sqlite3.connect(db_path)
    try:
        assert int(check.execute("SELECT COUNT(*) FROM fact_freshness").fetchone()[0]) == 5
        row = check.execute(
            "SELECT receipt_state FROM operator_operations WHERE operation_id = ?",
            ("freshness-backfill-test",),
        ).fetchone()
        assert row == ("mirrored",)
    finally:
        check.close()


def test_factual_freshness_backfill_is_dry_run_bounded_and_idempotent(tmp_path):
    _disable_unrelated_vector_runtime(tmp_path)
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-freshness-backfill",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        active = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Backfill active factual sentinel remains visible for validation.", "target": "ops", "memory_type": "factual"},
            )
        )
        hidden = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Backfill hidden scratch factual sentinel must stay excluded.", "target": "ops", "memory_type": "factual"},
            )
        )
        with plugin._lock:
            conn = plugin._require_conn()
            row = conn.execute("SELECT metadata FROM memories WHERE id = ?", (hidden["id"],)).fetchone()
            metadata = json.loads(str(row["metadata"] or "{}"))
            metadata["lifecycle"] = "scratch"
            conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (json.dumps(metadata, sort_keys=True), hidden["id"]))
            conn.execute("DELETE FROM fact_freshness WHERE subject_id IN (?, ?)", (active["id"], hidden["id"]))
            conn.commit()

            dry_run = backfill_untracked_memory_freshness(conn, apply=False, limit=10)
            assert conn.execute("SELECT COUNT(*) FROM fact_freshness").fetchone()[0] == 0
            applied = backfill_untracked_memory_freshness(conn, apply=True, limit=10)
            repeated = backfill_untracked_memory_freshness(conn, apply=True, limit=10)

        assert dry_run == {
            "apply": False,
            "eligible": 1,
            "inserted": 0,
            "truncated": False,
            "ids": [active["id"]],
        }
        assert applied == {
            "apply": True,
            "eligible": 1,
            "inserted": 1,
            "truncated": False,
            "ids": [active["id"]],
        }
        assert repeated == {
            "apply": True,
            "eligible": 0,
            "inserted": 0,
            "truncated": False,
            "ids": [],
        }
    finally:
        plugin.shutdown()


def test_provider_startup_applies_one_bounded_idempotent_freshness_backfill(tmp_path):
    _disable_unrelated_vector_runtime(tmp_path)
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir(parents=True, exist_ok=True)
    db_path = storage_dir / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        _store_direct_memory(
            conn,
            memory_id="legacy-untracked-factual",
            memory_type="factual",
        )
        conn.execute(
            "DELETE FROM fact_freshness WHERE subject_id = 'legacy-untracked-factual'"
        )
        conn.commit()
    finally:
        conn.close()

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-startup-freshness-backfill",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        assert plugin._freshness_backfill == {
            "apply": True,
            "eligible": 1,
            "inserted": 1,
            "truncated": False,
            "ids": ["legacy-untracked-factual"],
        }
        with plugin._lock:
            status = plugin._require_conn().execute(
                "SELECT status FROM fact_freshness WHERE subject_id = ?",
                ("legacy-untracked-factual",),
            ).fetchone()[0]
        assert status == "needs_live_check"
    finally:
        plugin.shutdown()

    plugin.initialize(
        "session-startup-freshness-backfill-repeat",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        assert plugin._freshness_backfill["eligible"] == 0
        assert plugin._freshness_backfill["inserted"] == 0
    finally:
        plugin.shutdown()


def test_profile_marks_stale_operational_fact_as_needing_live_check(tmp_path):
    _disable_unrelated_vector_runtime(tmp_path)
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
    _disable_unrelated_vector_runtime(tmp_path)
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


def test_public_store_tracks_nonfactual_operational_status_snapshot_as_needs_live_check(tmp_path):
    _disable_unrelated_vector_runtime(tmp_path)
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-freshness-operational-status",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="primary-user",
        agent_context="primary",
        agent_identity="primary-agent",
        agent_workspace="hermes",
    )
    try:
        stored = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "当前 Scope Recall 版本为 1.2.3，vector 状态仍显示 needs_repair，journal backlog 为 1091。",
                    "target": "project",
                    "memory_type": "summary",
                    "allow_duplicate": True,
                },
            )
        )
        procedure = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "发布检查流程必须读取当前版本、vector 状态和 journal backlog，不得相信旧快照。",
                    "target": "ops",
                    "memory_type": "procedure",
                    "allow_duplicate": True,
                },
            )
        )
        with plugin._lock:
            status_row = plugin._require_conn().execute(
                "SELECT fact_key, truth_type, validator_kind, ttl_days, status FROM fact_freshness WHERE subject_id = ?",
                (stored["id"],),
            ).fetchone()
            procedure_row = plugin._require_conn().execute(
                "SELECT truth_type, validator_kind, ttl_days, status "
                "FROM fact_freshness WHERE subject_id = ?",
                (procedure["id"],),
            ).fetchone()

        assert dict(status_row) == {
            "fact_key": "operational_status_snapshot",
            "truth_type": "operational_status",
            "validator_kind": "manual",
            "ttl_days": 1,
            "status": "needs_live_check",
        }
        assert dict(procedure_row) == {
            "truth_type": "procedure",
            "validator_kind": "none",
            "ttl_days": 180,
            "status": "current",
        }
        assert freshness_penalty(
            {"status": "needs_live_check", "truth_type": "operational_status"}
        ) == pytest.approx(0.55)
    finally:
        plugin.shutdown()


def test_backfill_finds_untracked_nonfactual_operational_status_snapshot(tmp_path):
    _disable_unrelated_vector_runtime(tmp_path)
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-freshness-operational-backfill",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="primary-user",
        agent_context="primary",
        agent_identity="primary-agent",
        agent_workspace="hermes",
    )
    try:
        stored = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "截至 live check，Scope Recall version 为 9.9.9，vector status 为 degraded。",
                    "target": "project",
                    "memory_type": "summary",
                    "allow_duplicate": True,
                },
            )
        )
        with plugin._lock:
            conn = plugin._require_conn()
            conn.execute("DELETE FROM fact_freshness WHERE subject_id = ?", (stored["id"],))
            conn.commit()
            dry_run = backfill_untracked_memory_freshness(conn, apply=False, limit=10)
            applied = backfill_untracked_memory_freshness(conn, apply=True, limit=10)
            repeated = backfill_untracked_memory_freshness(conn, apply=True, limit=10)
            row = conn.execute(
                "SELECT truth_type, status FROM fact_freshness WHERE subject_id = ?",
                (stored["id"],),
            ).fetchone()

        assert dry_run["ids"] == [stored["id"]]
        assert applied["inserted"] == 1
        assert repeated["inserted"] == 0
        assert dict(row) == {"truth_type": "operational_status", "status": "needs_live_check"}
    finally:
        plugin.shutdown()
