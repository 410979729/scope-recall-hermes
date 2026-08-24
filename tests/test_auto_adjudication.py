"""No-human candidate adjudication regressions (goal G13).

The scheduled pass must promote aged safe candidates, archive noise, route
held candidates through the budgeted grounded-review lane, and keep every
outcome inside the existing lifecycle/governance audit trail.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scope_recall.auto_adjudication import run_auto_adjudication, run_provider_auto_adjudication
from scope_recall.governance_cleanup import (
    governance_audit_coverage_report,
    rollback_cleanup_batch,
)
from scope_recall.journal import append_journal_entry, ensure_journal_schema
from scope_recall.models import RuntimeScope
from scope_recall.scope import build_scope_id, build_shared_scope_id
from scope_recall.sql_store import ensure_schema, record_governance_audit_event


def _home(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_journal_schema(conn)
    return hermes_home, conn


def _insert_candidate(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    summary: str,
    content: str,
    metadata: dict | None = None,
    age_hours: float = 48.0,
) -> None:
    at = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    payload = {
        "lifecycle": "candidate",
        "memory_type": "workflow",
        "confidence": 0.82,
        "importance": 0.66,
        "evidence_refs": ["journal:fixture"],
        **(metadata or {}),
    }
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
            agent_identity, agent_workspace, session_id, source, target, content, summary,
            created_at, updated_at, last_recalled_turn, metadata
        ) VALUES (?, 'scope-test', '', '', '', '', '', '', '', '', 'journal-digest', 'ops', ?, ?, ?, ?, 0, ?)
        """,
        (memory_id, content, summary, at, at, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
    )
    conn.commit()


def _lifecycle(conn: sqlite3.Connection, memory_id: str) -> str:
    return str(
        conn.execute(
            "SELECT json_extract(metadata, '$.lifecycle') FROM memories WHERE id=?",
            (memory_id,),
        ).fetchone()[0]
    )


def test_lanes_promote_aged_and_archive_noise_and_defer_young(tmp_path):
    hermes_home, conn = _home(tmp_path)
    _insert_candidate(
        conn, "aged-safe",
        summary="Stable workflow",
        content="Run pytest and doctor before rollout.",
        age_hours=48,
    )
    _insert_candidate(
        conn, "young-safe",
        summary="Stable workflow two",
        content="Run doctor checks and pytest before every rollout window.",
        age_hours=2,
    )
    _insert_candidate(
        conn, "noise",
        summary="Conversation summary",
        content="One-off transcript digest that should not become a durable profile row.",
        metadata={"memory_type": "summary", "confidence": 0.62, "importance": 0.5},
        age_hours=48,
    )

    report = run_auto_adjudication(hermes_home, {}, llm_call=None)
    assert report["ok"] is True
    assert report["lanes"]["promoted"] == 1
    assert report["lanes"]["promote_deferred_young"] == 1
    assert report["lanes"]["archived"] == 1
    assert _lifecycle(conn, "aged-safe") == "promoted"
    assert _lifecycle(conn, "young-safe") == "candidate"
    assert _lifecycle(conn, "noise") == "archived"
    audit_rows = conn.execute(
        "SELECT COUNT(*) FROM governance_audit_events WHERE batch_id = ?",
        (report["batch_id"],),
    ).fetchone()[0]
    assert audit_rows >= 2, "auto decisions must land in the governance audit trail"

    coverage = governance_audit_coverage_report(conn, scope_ids=["scope-test"])
    assert coverage["new_mutation_coverage"]["missing_audit"] == 0
    assert coverage["new_mutation_coverage"]["ok"] is True

    dry_rollback = rollback_cleanup_batch(
        conn,
        batch_id=report["batch_id"],
        dry_run=True,
    )
    assert dry_rollback["rollback_candidates"] == 1
    assert dry_rollback["restore_ids"] == ["noise"]

    applied_rollback = rollback_cleanup_batch(
        conn,
        batch_id=report["batch_id"],
        dry_run=False,
    )
    assert applied_rollback["restored"] == 1
    assert _lifecycle(conn, "noise") == "candidate"


def test_auto_archive_rollback_refuses_state_changed_after_the_receipt(tmp_path):
    hermes_home, conn = _home(tmp_path)
    _insert_candidate(
        conn,
        "noise",
        summary="Conversation summary",
        content="One-off transcript digest that should remain hidden after a later review.",
        metadata={"memory_type": "summary", "confidence": 0.62, "importance": 0.5},
        age_hours=48,
    )
    report = run_auto_adjudication(hermes_home, {}, llm_call=None)
    assert _lifecycle(conn, "noise") == "archived"

    row = conn.execute("SELECT metadata FROM memories WHERE id='noise'").fetchone()
    changed = json.loads(row["metadata"])
    changed["later_operator_review"] = True
    conn.execute(
        "UPDATE memories SET metadata = ?, updated_at = ? WHERE id = 'noise'",
        (
            json.dumps(changed, ensure_ascii=False, sort_keys=True),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()

    rollback = rollback_cleanup_batch(
        conn,
        batch_id=report["batch_id"],
        dry_run=False,
    )
    assert rollback["rollback_candidates"] == 1
    assert rollback["restored"] == 0
    assert _lifecycle(conn, "noise") == "archived"


def _scope() -> RuntimeScope:
    return RuntimeScope(
        platform="telegram", user_id="joy", chat_id="dm", thread_id="",
        gateway_session_key="", agent_identity="tester", agent_workspace="hermes",
        agent_context="primary",
    )


def _held_candidate_with_evidence(conn, memory_id: str, *, evidence_text: str) -> None:
    # missing evidence_refs -> needs_review lane (held for L4)
    _insert_candidate(
        conn, memory_id,
        summary="Held claim",
        content="部署脚本必须先备份再替换插件目录，回滚锚点保留在 backups 下。",
        metadata={"evidence_refs": []},
        age_hours=72,
    )
    scope = _scope()
    entry_id = append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="l4-session",
        turn_number=1,
        role="user",
        content=evidence_text,
    )
    assert entry_id, "evidence must pass journal capture filters"
    conn.execute(
        "INSERT INTO memory_journal_sources(memory_id, journal_entry_id, run_id, created_at) VALUES (?, ?, 'run-x', ?)",
        (memory_id, entry_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def test_l4_grounded_review_promotes_supported_and_archives_unsupported(tmp_path):
    hermes_home, conn = _home(tmp_path)
    _held_candidate_with_evidence(
        conn, "held-supported",
        evidence_text="记住这条部署纪律：部署脚本必须先做完整备份再替换插件目录，回滚锚点统一放在 backups 目录里长期保留，方便随时恢复。",
    )
    _held_candidate_with_evidence(
        conn, "held-unsupported",
        evidence_text="今天下午主要聊了周末去哪里爬山和晚饭吃什么，是一段和部署流程完全无关的闲聊内容，用来验证不支持判定的路径。",
    )

    calls = []

    def fake_llm(prompt: str) -> str:
        calls.append(prompt)
        if "爬山" in prompt:
            return json.dumps({"verdict": "unsupported", "reason": "证据与记忆无关"})
        return json.dumps({"verdict": "supported", "reason": "证据直接支持"})

    report = run_auto_adjudication(hermes_home, {}, llm_call=fake_llm)
    assert report["l4"]["enabled"] is True
    assert report["l4"]["reviewed"] == 2
    assert report["l4"]["supported"] == 1
    assert report["l4"]["unsupported"] == 1
    assert _lifecycle(conn, "held-supported") == "promoted"
    assert _lifecycle(conn, "held-unsupported") == "archived"
    assert len(calls) == 2
    assert "原始证据" in calls[0]


def test_l4_uncertain_rounds_exhaust_to_archive(tmp_path):
    hermes_home, conn = _home(tmp_path)
    _held_candidate_with_evidence(
        conn, "held-uncertain",
        evidence_text="这段证据写得模糊不清，既没有确认也没有否认部署流程的任何细节，完全无法据此判断那条记忆是否成立。",
    )
    config = {"auto_adjudication": {"l4_max_uncertain_rounds": 2}}

    def uncertain_llm(prompt: str) -> str:
        return json.dumps({"verdict": "uncertain", "reason": "证据不足"})

    first = run_auto_adjudication(hermes_home, config, llm_call=uncertain_llm)
    assert first["l4"]["uncertain"] == 1
    assert _lifecycle(conn, "held-uncertain") == "candidate"
    meta = json.loads(
        conn.execute(
            "SELECT metadata FROM memories WHERE id='held-uncertain'"
        ).fetchone()[0]
    )
    assert meta["l4_uncertain_rounds"] == 1

    second = run_auto_adjudication(hermes_home, config, llm_call=uncertain_llm)
    assert second["l4"]["exhausted_archived"] == 1
    assert _lifecycle(conn, "held-uncertain") == "archived"


def test_l4_stale_uncertain_verdict_cannot_archive_concurrently_changed_content(tmp_path):
    hermes_home, conn = _home(tmp_path)
    _held_candidate_with_evidence(
        conn,
        "held-concurrent",
        evidence_text="这段证据不够明确，专门验证慢速评审期间并发修改候选正文时必须拒绝旧裁决。",
    )
    metadata = json.loads(
        conn.execute("SELECT metadata FROM memories WHERE id='held-concurrent'").fetchone()[0]
    )
    metadata["l4_uncertain_rounds"] = 1
    conn.execute(
        "UPDATE memories SET metadata = ? WHERE id = 'held-concurrent'",
        (json.dumps(metadata, ensure_ascii=False, sort_keys=True),),
    )
    conn.commit()

    db_path = hermes_home / "scope-recall" / "memory.sqlite3"

    def concurrent_uncertain(_prompt: str) -> str:
        peer = sqlite3.connect(db_path)
        try:
            peer.execute(
                "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
                (
                    "NEW UNREVIEWED CONTENT",
                    datetime.now(timezone.utc).isoformat(),
                    "held-concurrent",
                ),
            )
            peer.commit()
        finally:
            peer.close()
        return json.dumps({"verdict": "uncertain", "reason": "旧证据不足"})

    report = run_auto_adjudication(
        hermes_home,
        {"auto_adjudication": {"l4_max_uncertain_rounds": 1}},
        llm_call=concurrent_uncertain,
    )
    row = conn.execute(
        "SELECT content, json_extract(metadata, '$.lifecycle') FROM memories WHERE id='held-concurrent'"
    ).fetchone()

    assert row["content"] == "NEW UNREVIEWED CONTENT"
    assert row[1] == "candidate"
    assert report["l4"]["exhausted_archived"] == 0
    assert report["l4"]["conflicts_skipped"] == 1


def test_l4_llm_errors_keep_candidates_and_surface_exceptions(tmp_path):
    hermes_home, conn = _home(tmp_path)
    _held_candidate_with_evidence(
        conn, "held-error",
        evidence_text="这是一段足够长的证据文本，专门用于验证大模型调用失败时候选记忆保持原状并暴露异常记录的路径。",
    )

    def broken_llm(prompt: str) -> str:
        raise RuntimeError("provider unavailable http 502")

    report = run_auto_adjudication(hermes_home, {}, llm_call=broken_llm)
    assert report["l4"]["errors"] == 1
    assert report["exceptions"] and report["exceptions"][0]["kind"] == "l4_llm_error"
    assert _lifecycle(conn, "held-error") == "candidate"


def _provider_like(hermes_home: Path, *, interval_hours: float = 24.0):
    class Provider:
        def __init__(self) -> None:
            self._shutdown_requested = type("E", (), {"is_set": lambda self: False})()
            self._hermes_home = hermes_home
            self._config = {
                "auto_adjudication": {
                    "enabled": True,
                    "interval_hours": interval_hours,
                    "l4_enabled": False,
                }
            }
            self._last_adjudication_at = 0.0
            self._last_adjudication_report = {}

        def _truth_writes_blocked(self) -> bool:
            return False

        def _memory_isolated_for_scope(self) -> bool:
            return False

        def _journal_config(self) -> dict:
            return {}

    return Provider()


def test_auto_adjudication_throttle_survives_provider_recreation(tmp_path, monkeypatch):
    hermes_home, conn = _home(tmp_path)
    conn.close()
    calls: list[str] = []

    def fake_run(*_args, **_kwargs):
        calls.append("run")
        return {"ok": True, "status": "applied"}

    monkeypatch.setattr("scope_recall.auto_adjudication.run_auto_adjudication", fake_run)

    first = _provider_like(hermes_home)
    run_provider_auto_adjudication(first, trigger="first")
    second = _provider_like(hermes_home)
    run_provider_auto_adjudication(second, trigger="second")

    assert calls == ["run"]


def test_auto_adjudication_throttle_records_completion_not_start_time(
    tmp_path, monkeypatch
):
    hermes_home, conn = _home(tmp_path)
    conn.close()
    clock = [100.0]

    def fake_run(*_args, **_kwargs):
        clock[0] = 200.0
        return {"ok": True, "status": "applied"}

    monkeypatch.setattr("time.time", lambda: clock[0])
    monkeypatch.setattr(
        "scope_recall.auto_adjudication.run_auto_adjudication", fake_run
    )

    run_provider_auto_adjudication(_provider_like(hermes_home), trigger="completion")

    receipt_conn = sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3")
    try:
        row = receipt_conn.execute(
            "SELECT after_json FROM governance_audit_events "
            "WHERE event_type = 'memory_auto_adjudication' "
            "AND action = 'schedule_complete'"
        ).fetchone()
    finally:
        receipt_conn.close()
    assert row is not None
    assert json.loads(row[0])["completed_at_unix"] == 200.0


def test_failed_auto_adjudication_does_not_write_durable_throttle(tmp_path, monkeypatch):
    hermes_home, conn = _home(tmp_path)
    conn.close()
    calls: list[str] = []

    def fail_then_ok(*_args, **_kwargs):
        if "fail" not in calls:
            calls.append("fail")
            raise RuntimeError("injected adjudication failure")
        calls.append("ok")
        return {"ok": True, "status": "applied"}

    monkeypatch.setattr("scope_recall.auto_adjudication.run_auto_adjudication", fail_then_ok)

    first = _provider_like(hermes_home)
    run_provider_auto_adjudication(first, trigger="fail")
    second = _provider_like(hermes_home)
    run_provider_auto_adjudication(second, trigger="retry")

    assert calls == ["fail", "ok"]


def test_durable_throttle_ignores_receipt_for_another_target(tmp_path, monkeypatch):
    hermes_home, conn = _home(tmp_path)
    conn.close()
    receipt_conn = sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3")
    try:
        ensure_schema(receipt_conn)
        record_governance_audit_event(
            receipt_conn,
            event_id="wrong-target-throttle-receipt",
            event_type="memory_auto_adjudication",
            action="schedule_complete",
            target_id="not-the-scheduler",
            scope_id="",
            before={},
            after={"completed_at_unix": 9999999999.0},
            reason="unrelated receipt",
            actor="test",
        )
        receipt_conn.commit()
    finally:
        receipt_conn.close()
    calls: list[str] = []

    def fake_run(*_args, **_kwargs):
        calls.append("run")
        return {"ok": True, "status": "applied"}

    monkeypatch.setattr("scope_recall.auto_adjudication.run_auto_adjudication", fake_run)

    run_provider_auto_adjudication(_provider_like(hermes_home), trigger="wrong-target")

    assert calls == ["run"]


def test_lanes_only_when_llm_unavailable(tmp_path):
    hermes_home, conn = _home(tmp_path)
    _held_candidate_with_evidence(
        conn, "held-no-llm",
        evidence_text="这是一段足够长的证据文本，用于验证没有配置大模型时裁决器只跑确定性车道并如实报告降级状态。",
    )
    report = run_auto_adjudication(hermes_home, {}, llm_call=None)
    assert report["l4"]["enabled"] is False
    assert report["lanes"]["held_for_l4"] == 1
    assert _lifecycle(conn, "held-no-llm") == "candidate"


def test_scheduled_throttle_claim_is_atomic_across_concurrent_providers(
    tmp_path, monkeypatch
):
    hermes_home, conn = _home(tmp_path)
    conn.close()
    calls: list[str] = []
    first_entered = threading.Event()
    release_run = threading.Event()

    def fake_run(*_args, **_kwargs):
        calls.append("run")
        first_entered.set()
        assert release_run.wait(timeout=5)
        return {"ok": True, "status": "applied"}

    monkeypatch.setattr(
        "scope_recall.auto_adjudication.run_auto_adjudication", fake_run
    )
    workers = [
        threading.Thread(
            target=run_provider_auto_adjudication,
            args=(_provider_like(hermes_home),),
            kwargs={"trigger": f"worker-{index}"},
        )
        for index in range(2)
    ]

    workers[0].start()
    assert first_entered.wait(timeout=5)
    workers[1].start()
    time.sleep(0.1)
    release_run.set()
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()

    assert calls == ["run"]


def test_scheduled_throttle_recovers_an_expired_claim(tmp_path, monkeypatch):
    hermes_home, conn = _home(tmp_path)
    record_governance_audit_event(
        conn,
        event_id="expired-schedule-claim",
        event_type="memory_auto_adjudication",
        action="schedule_claim",
        target_id="auto_adjudication_schedule",
        after={"claim_token": "dead-worker", "expires_at_unix": 50.0},
        reason="simulate a worker that died after claiming",
        actor="test",
    )
    conn.commit()
    conn.close()
    calls: list[str] = []

    def fake_run(*_args, **_kwargs):
        calls.append("run")
        return {"ok": True, "status": "applied"}

    monkeypatch.setattr("time.time", lambda: 100.0)
    monkeypatch.setattr(
        "scope_recall.auto_adjudication.run_auto_adjudication", fake_run
    )

    run_provider_auto_adjudication(
        _provider_like(hermes_home), trigger="recover-expired-claim"
    )

    assert calls == ["run"]
