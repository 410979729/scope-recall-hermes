"""Regressions for the 2026-08-16 independent review blockers."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


from scope_recall.auto_adjudication import (
    _mark_uncertain_round,
    run_auto_adjudication,
)
from scope_recall.journal import append_journal_entry, ensure_journal_schema, run_journal_digest
from scope_recall.journal_llm import JournalDigestLLMError
from scope_recall.journal_recovery import find_replay_candidates
from scope_recall.models import RuntimeScope
from scope_recall.scope import build_scope_id, build_shared_scope_id
from scope_recall.sql_store import ensure_schema


def _scope() -> RuntimeScope:
    return RuntimeScope(
        platform="telegram",
        user_id="9000000001",  # fixture
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="default",
        agent_workspace="hermes",
        agent_context="primary",
    )


def _home(tmp_path: Path, journal_config: dict | None = None) -> tuple[Path, sqlite3.Connection]:
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    payload = {"vector": {"enabled": False}, "journal": journal_config or {"extractor": "llm"}}
    (storage / "config.json").write_text(json.dumps(payload), encoding="utf-8")
    (hermes_home / ".env").write_text("SCOPE_RECALL_DIGEST_API_KEY=test-key\n", encoding="utf-8")
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_journal_schema(conn)
    return hermes_home, conn


def _insert_candidate(conn: sqlite3.Connection, memory_id: str, *, age_hours: float = 48.0) -> None:
    at = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    payload = {
        "lifecycle": "candidate",
        "memory_type": "workflow",
        "confidence": 0.4,
        "importance": 0.4,
        "needs_review": True,
        "evidence_refs": ["journal:fixture"],
    }
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
            agent_identity, agent_workspace, session_id, source, target, content, summary,
            created_at, updated_at, last_recalled_turn, metadata
        ) VALUES (?, 'scope-test', '', '', '', '', '', '', '', '', 'journal-digest', 'ops', ?, ?, ?, ?, 0, ?)
        """,
        (
            memory_id,
            "Held candidate that needs grounded review.",
            "Held candidate",
            at,
            at,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    conn.commit()


def test_disabled_adjudication_does_not_mutate(tmp_path):
    hermes_home, conn = _home(tmp_path)
    _insert_candidate(conn, "keep-candidate", age_hours=72)
    report = run_auto_adjudication(
        hermes_home,
        {"auto_adjudication": {"enabled": False}},
    )
    assert report["status"] == "disabled"
    lifecycle = conn.execute(
        "SELECT json_extract(metadata, '$.lifecycle') FROM memories WHERE id=?",
        ("keep-candidate",),
    ).fetchone()[0]
    assert lifecycle == "candidate"


def test_evidence_lock_does_not_archive(tmp_path, monkeypatch):
    hermes_home, conn = _home(tmp_path)
    _insert_candidate(
        conn,
        "lock-held",
        age_hours=72,
    )
    conn.execute(
        """
        UPDATE memories SET metadata = json_set(metadata, '$.evidence_refs', json('[]'))
        WHERE id='lock-held'
        """
    )
    conn.commit()

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("scope_recall.auto_adjudication._journal_evidence", boom)
    report = run_auto_adjudication(
        hermes_home,
        {"auto_adjudication": {"enabled": True, "l4_enabled": True, "l4_max_uncertain_rounds": 1}},
        llm_call=lambda prompt: '{"verdict":"unsupported","reason":"should not run"}',
    )
    lifecycle = conn.execute(
        "SELECT json_extract(metadata, '$.lifecycle') FROM memories WHERE id=?",
        ("lock-held",),
    ).fetchone()[0]
    assert lifecycle == "candidate"
    assert int(report.get("l4", {}).get("errors") or 0) >= 1
    assert int(report.get("l4", {}).get("exhausted_archived") or 0) == 0


def test_uncertain_round_cas_skips_stale_row(tmp_path):
    hermes_home, conn = _home(tmp_path)
    _insert_candidate(conn, "cas-row", age_hours=1)
    row = conn.execute("SELECT * FROM memories WHERE id=?", ("cas-row",)).fetchone()
    conn.execute(
        "UPDATE memories SET updated_at=? WHERE id=?",
        ("2099-01-01T00:00:00+00:00", "cas-row"),
    )
    conn.commit()
    rounds = _mark_uncertain_round(conn, row, reason="stale", at="2026-08-16T00:00:00+00:00")
    assert rounds is None
    meta = json.loads(
        conn.execute("SELECT metadata FROM memories WHERE id=?", ("cas-row",)).fetchone()[0]
    )
    assert int(meta.get("l4_uncertain_rounds") or 0) == 0


def test_nonretryable_extractor_failure_counts_attempts(tmp_path, monkeypatch):
    import scope_recall.journal as journal_module

    hermes_home, conn = _home(
        tmp_path, {"extractor": "llm", "extraction_attempts_quarantine": 1}
    )
    scope = _scope()
    entry_id = append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="dead",
        turn_number=1,
        role="user",
        content="这条必须在确定性抽取失败后离开积压，而不是永远 pending。",
    )

    def dead(*args, **kwargs):
        raise JournalDigestLLMError(
            "policy blocked", attempts=1, error_kind="policy", retryable=False
        )

    monkeypatch.setattr(journal_module, "call_llm", dead)
    result = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="t", limit_entries=10
    )
    row = conn.execute(
        "SELECT extraction_attempts, processed_run_id FROM journal_entries WHERE id=?",
        (entry_id,),
    ).fetchone()
    assert int(row["extraction_attempts"] or 0) >= 1
    assert str(row["processed_run_id"] or ""), result


def test_quarantine_reason_is_recoverable_prefix(tmp_path, monkeypatch):
    import scope_recall.journal as journal_module

    hermes_home, conn = _home(
        tmp_path, {"extractor": "llm", "extraction_attempts_quarantine": 1}
    )
    scope = _scope()
    entry_id = append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="empty",
        turn_number=1,
        role="user",
        content="空抽取达到次数后必须能被 recovery 计划看见。",
    )
    monkeypatch.setattr(journal_module, "call_llm", lambda prompt, **kwargs: "[]")
    run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="t", limit_entries=10
    )
    reason = conn.execute(
        "SELECT reason FROM journal_rejections WHERE journal_entry_id=?",
        (entry_id,),
    ).fetchone()["reason"]
    assert reason.startswith("dead-letter:") or reason.startswith("retry-exhausted:")

    items = find_replay_candidates(conn)
    ids = {int(item["journal_entry_id"]) for item in items}
    assert entry_id in ids


def test_build_l4_llm_call_does_not_pass_unknown_key_env(tmp_path, monkeypatch):
    from scope_recall.auto_adjudication import build_l4_llm_call
    from scope_recall.nightly_digest import DigestOptions

    seen: dict = {}

    def fake_resolve(home, options):
        seen["options"] = options
        assert not hasattr(options, "key_env")
        return {
            "model": "dummy",
            "base_url": "https://example.invalid/v1",
            "api_key": "dummy",
            "api_mode": "chat_completions",
            "timeout": 5.0,
            "append_v1": True,
            "allow_insecure_endpoint": False,
            "endpoint": "",
        }

    monkeypatch.setattr("scope_recall.nightly_digest.resolve_llm_config", fake_resolve)
    hermes_home = tmp_path / "hermes"
    (hermes_home / "scope-recall").mkdir(parents=True)
    fn = build_l4_llm_call(hermes_home, {"extractor": "llm", "llm_timeout": 30})
    assert fn is not None
    assert isinstance(seen["options"], DigestOptions)
