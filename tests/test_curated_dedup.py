"""Regression tests for curated-memory prompt and candidate deduplication."""

from __future__ import annotations

import sqlite3

from scope_recall.candidate_extraction import ExtractedCandidate
from scope_recall.candidate_store import store_event_candidates
from scope_recall.config_schema import build_config_registry, load_packaged_config
from scope_recall.journal import JournalDigestCandidate, apply_journal_candidates, ensure_journal_schema
from scope_recall.models import RuntimeScope
from scope_recall.sql_store import ensure_schema
from scope_recall.storage_views import search_curated_memories


def _scope() -> RuntimeScope:
    return RuntimeScope(
        platform="cli",
        user_id="jojo",
        chat_id="local",
        agent_identity="default",
        agent_workspace="hermes",
    )


def _connection(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_journal_schema(conn)
    return conn


def test_curated_memory_split_controls_are_registered() -> None:
    curated = load_packaged_config()["curated_memory"]
    assert curated["auto_recall"] is True
    assert curated["include_in_tools"] is True
    assert curated["dedupe_candidates"] is True
    keys = {row["key"] for row in build_config_registry()}
    assert {
        "curated_memory.auto_recall",
        "curated_memory.include_in_tools",
        "curated_memory.dedupe_candidates",
    } <= keys


def test_curated_tool_surface_can_be_disabled_independently(tmp_path) -> None:
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "USER.md").write_text("User prefers concise reports.", encoding="utf-8")
    provider = type(
        "Provider",
        (),
        {
            "_config": {
                "curated_memory": {
                    "mode": "profile-global",
                    "auto_recall": False,
                    "include_in_tools": False,
                }
            },
            "_scope": type("Scope", (), {"user_id": "jojo"})(),
            "_retrieval_config": {"min_score": 0.0},
            "_hermes_home": tmp_path,
            "_config_value": staticmethod(lambda _key, default: default),
        },
    )()

    assert search_curated_memories(provider, "concise reports") == []


def test_event_candidate_strictly_covered_by_curated_memory_is_not_stored(tmp_path) -> None:
    conn = _connection(tmp_path)
    content = "User prefers concise Chinese answers with verified results."
    report = store_event_candidates(
        conn,
        candidates=[
            ExtractedCandidate(
                target="user",
                content=content,
                memory_type="preference",
                confidence=0.9,
                evidence_refs=["session:test:turn:1"],
            )
        ],
        scope=_scope(),
        scope_id="scope-a",
        session_id="session-a",
        dry_run=False,
        curated_entries=[("user", f"Profile heading\n{content}\nStable footer", "2026-08-05T00:00:00+00:00")],
    )

    assert report["inserted"] == 0
    assert report["skipped_curated_duplicate"] == 1
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    audit = conn.execute("SELECT action FROM governance_audit_events").fetchone()
    assert audit["action"] == "skip_curated_duplicate"


def test_changed_value_is_not_discarded_as_curated_duplicate(tmp_path) -> None:
    conn = _connection(tmp_path)
    report = store_event_candidates(
        conn,
        candidates=[
            ExtractedCandidate(
                target="project",
                content="The alert threshold is 28 centimeters.",
                memory_type="project_fact",
                confidence=0.9,
                evidence_refs=["session:test:turn:2"],
            )
        ],
        scope=_scope(),
        scope_id="scope-a",
        session_id="session-a",
        dry_run=False,
        curated_entries=[
            (
                "memory",
                "The alert threshold is 27 centimeters.",
                "2026-08-05T00:00:00+00:00",
            )
        ],
    )

    assert report["skipped_curated_duplicate"] == 0
    assert report["inserted"] == 1


def test_journal_candidate_strictly_covered_by_curated_memory_is_rejected_with_receipt(tmp_path) -> None:
    conn = _connection(tmp_path)
    content = "The project uses pytest for verification."
    candidate = JournalDigestCandidate(
        content=content,
        target="project",
        memory_type="project_fact",
        entry_ids=[41],
        session_ids=["session-a"],
    )

    result = apply_journal_candidates(
        conn,
        None,
        _scope(),
        run_id="curated-dedup",
        candidates=[candidate],
        dry_run=False,
        curated_entries=[("memory", f"Stable environment: {content}", "2026-08-05T00:00:00+00:00")],
    )

    assert result["counts"]["skipped"] == 1
    assert result["processed_entry_ids"] == [41]
    assert result["actions"][0]["reason"] == "curated memory covers candidate"
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    receipt = conn.execute(
        "SELECT reason FROM journal_rejections WHERE journal_entry_id = 41"
    ).fetchone()
    assert receipt["reason"] == "curated memory covers candidate"