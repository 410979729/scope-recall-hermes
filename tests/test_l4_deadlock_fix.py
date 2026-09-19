"""Regression tests for the L4 cross-scope deadlock fix (2.0.2).

Two-layer authorization (owning-memory allowlist + subtree containment)
and supported-verdict admission stamping.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from scope_recall.adjudication_l4 import collect_journal_evidence
from scope_recall.auto_adjudication import run_auto_adjudication


SHARED = "workspace:6:w|agent:7:a|canonical_user:8:u"
OTHER = "workspace:6:w|agent:7:a|canonical_user:99:other"
SESSION_QUALIFIED = SHARED + "|platform:5:qqbot|account:38:acc1|session:2:s1"
ALLOWED = (SHARED,)


def _minimal_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE memories(id TEXT PRIMARY KEY, scope_id TEXT);
        CREATE TABLE journal_entries(id TEXT PRIMARY KEY, scope_id TEXT, role TEXT, content TEXT);
        CREATE TABLE memory_journal_sources(memory_id TEXT, journal_entry_id TEXT);
        """
    )
    return conn


def test_own_memory_and_session_qualified_evidence_authorizes():
    conn = _minimal_conn()
    conn.execute("INSERT INTO memories VALUES('m1', ?)", (SHARED,))
    conn.execute(
        "INSERT INTO journal_entries VALUES('j1', ?, 'user', '本人会话')", (SESSION_QUALIFIED,)
    )
    conn.execute("INSERT INTO memory_journal_sources VALUES('m1', 'j1')")
    conn.commit()

    evidence = collect_journal_evidence(conn, "m1", scope_ids=ALLOWED, max_chars=2400)

    assert evidence.authorization_error is False
    assert evidence.total_count == 1
    assert evidence.included_count == 1
    assert "本人会话" in evidence.text


def test_poisoned_cross_scope_link_is_rejected_without_leak():
    conn = _minimal_conn()
    conn.execute("INSERT INTO memories VALUES('m1', ?)", (SHARED,))
    conn.execute(
        "INSERT INTO journal_entries VALUES('j2', ?, 'user', 'FORBIDDEN')",
        (OTHER + "|session:9",),
    )
    conn.execute("INSERT INTO memory_journal_sources VALUES('m1', 'j2')")
    conn.commit()

    evidence = collect_journal_evidence(conn, "m1", scope_ids=ALLOWED, max_chars=2400)

    assert evidence.authorization_error is True
    assert evidence.included_count == 0
    assert "FORBIDDEN" not in evidence.text


def test_other_user_memory_is_rejected():
    conn = _minimal_conn()
    conn.execute("INSERT INTO memories VALUES('m2', ?)", (OTHER,))
    conn.execute(
        "INSERT INTO journal_entries VALUES('j2', ?, 'user', 'other user content')",
        (OTHER + "|session:9",),
    )
    conn.execute("INSERT INTO memory_journal_sources VALUES('m2', 'j2')")
    conn.commit()

    evidence = collect_journal_evidence(conn, "m2", scope_ids=ALLOWED, max_chars=2400)

    assert evidence.authorization_error is True
    assert evidence.included_count == 0


def test_supported_verdict_stamps_admission_review_without_promoting(tmp_path, monkeypatch):
    # full-pipeline test using the real schema via ensure_schema
    from scope_recall.sql_store import ensure_schema
    import scope_recall.auto_adjudication as auto_module
    import scope_recall.writer_lease as wl

    home = tmp_path / "hermes-home"
    (home / "scope-recall").mkdir(parents=True)
    db = home / "scope-recall" / "memory.sqlite3"
    conn = sqlite3.connect(db)
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS journal_entries(id TEXT PRIMARY KEY, scope_id TEXT, role TEXT, content TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS memory_journal_sources(memory_id TEXT, journal_entry_id TEXT, PRIMARY KEY(memory_id, journal_entry_id))")
    metadata = {
        "lifecycle": "candidate",
        "memory_type": "factual",
        "confidence": 0.9,
        "importance": 0.8,
        "evidence_refs": ["j1"],
        "automatic_admission": {
            "version": 1,
            "source": "journal-digest",
            "route": "memory_review",
            "time_sensitive": False,
            "recommended_action": "candidate",
        },
        "candidate_status": "needs_review",
    }
    conn.execute(
        "INSERT INTO memories(id, scope_id, source, target, content, summary, created_at, updated_at, metadata) "
        "VALUES('m1', ?, 'journal-digest', 'memory', ?, '', ?, ?, ?)",
        (
            SHARED,
            "用户在准备智能系统俱乐部的招新笔试，用西瓜书复习机器学习",
            "2026-09-10T00:00:00+00:00",
            "2026-09-10T00:00:00+00:00",
            json.dumps(metadata, ensure_ascii=False),
        ),
    )
    conn.execute(
        "INSERT INTO journal_entries VALUES('j1', ?, 'user', '我在准备智能系统俱乐部招新笔试，用周志华西瓜书复习')",
        (SESSION_QUALIFIED,),
    )
    conn.execute("INSERT INTO memory_journal_sources VALUES('m1', 'j1')")
    conn.commit()

    class _Ctx:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    monkeypatch.setattr(wl, "holding_truth_writer_lease", lambda *a, **k: _Ctx(None))

    def supported(prompt, *, system_prompt):
        return json.dumps(
            {
                "schema_version": "scope_recall_l4_verdict.v1",
                "verdict": "supported",
                "reason": "evidence directly supports the claim",
            }
        )

    report = run_auto_adjudication(
        home, {"auto_adjudication": {"promote_min_age_hours": 0}},
        llm_call=supported, scope_ids=ALLOWED,
    )
    row = conn.execute("SELECT metadata FROM memories WHERE id='m1'").fetchone()
    meta = json.loads(row["metadata"])
    assert report["l4"]["supported"] == 1
    assert meta["lifecycle"] == "candidate"
    assert meta.get("admission_reviewed_at")

    conn.execute("DELETE FROM governance_audit_events")
    conn.commit()
    report2 = run_auto_adjudication(
        home, {"auto_adjudication": {"promote_min_age_hours": 0}},
        llm_call=supported, scope_ids=ALLOWED,
    )
    row = conn.execute("SELECT metadata FROM memories WHERE id='m1'").fetchone()
    meta2 = json.loads(row["metadata"])
    assert report2["lanes"]["promoted"] == 1
    assert meta2["lifecycle"] == "promoted"
