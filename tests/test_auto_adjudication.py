"""No-human candidate adjudication regressions (goal G13).

The scheduled pass must promote aged safe candidates, archive noise, route
held candidates through the budgeted grounded-review lane, and keep every
outcome inside the existing lifecycle/governance audit trail.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scope_recall.auto_adjudication import run_auto_adjudication
from scope_recall.journal import append_journal_entry, ensure_journal_schema
from scope_recall.models import RuntimeScope
from scope_recall.scope import build_scope_id, build_shared_scope_id
from scope_recall.sql_store import ensure_schema


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
