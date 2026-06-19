from __future__ import annotations

import json
import sqlite3

from scope_recall.experience_promotion import promote_experiences
from scope_recall.experience_preflight import experience_preflight
from scope_recall.journal import append_journal_entry, ensure_journal_schema
from scope_recall.models import RuntimeScope
from scope_recall.scope import accessible_scope_ids, build_scope_id, build_shared_scope_id
from scope_recall.sql_store import ensure_schema


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_journal_schema(conn)
    return conn


def _scope() -> RuntimeScope:
    return RuntimeScope(
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        agent_context="primary",
    )


def _append(conn: sqlite3.Connection, *, scope: RuntimeScope, session_id: str, turn: int, role: str, content: str) -> int:
    return append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id=session_id,
        turn_number=turn,
        role=role,
        content=content,
    )


def test_low_risk_verified_task_auto_creates_and_promotes_experience_handbook():
    conn = _conn()
    scope = _scope()
    scope_id = build_scope_id(scope)
    shared_scope_id = build_shared_scope_id(scope)

    _append(conn, scope=scope, session_id="session-docs", turn=1, role="user", content="检查 scope-recall 文档链接和发布说明是否一致。")
    _append(
        conn,
        scope=scope,
        session_id="session-docs",
        turn=2,
        role="tool",
        content="Tool execution trace (terminal): python -m pytest tests/test_release.py -q -> 5 passed; wrote /home/a/private/output.log; ruff ok; docs smoke ok.",
    )
    _append(
        conn,
        scope=scope,
        session_id="session-docs",
        turn=3,
        role="assistant",
        content="完成：文档检查通过，测试通过，验证完成。下次可以复用这套检查流程。",
    )

    result = promote_experiences(
        conn,
        accessible_scope_ids=accessible_scope_ids(scope),
        scope_id=scope_id,
        shared_scope_id=shared_scope_id,
        config={"experience": {"auto_promote_low_risk": True}},
        dry_run=False,
    )

    assert result["episodes_created"] == 1
    assert result["handbooks_created"] == 1
    assert result["handbooks_promoted"] == 1

    episode = conn.execute("SELECT * FROM task_episodes").fetchone()
    assert episode is not None
    assert episode["outcome"] == "success"
    assert "pytest" in episode["tool_names"]
    evidence = json.loads(episode["evidence"])
    evidence_text = json.dumps(evidence, ensure_ascii=False)
    assert "Tool execution summary (terminal): output omitted" in evidence_text
    assert "Tool execution trace" not in evidence_text
    assert "/home/a/private" not in evidence_text
    assert "[REDACTED_PATH]" in evidence_text

    row = conn.execute("SELECT * FROM procedural_playbooks").fetchone()
    assert row is not None
    assert row["status"] == "promoted"
    assert row["created_from_episode_id"] == episode["id"]
    assert "scope-recall" in row["title"].lower()

    preflight = experience_preflight(conn, query="scope-recall 文档发布说明检查", accessible_scope_ids=accessible_scope_ids(scope), config={})
    assert preflight["decision"] in {"direct_reuse", "guided_reuse"}
    assert "scope-recall" in preflight["packet"].lower()

    second = promote_experiences(
        conn,
        accessible_scope_ids=accessible_scope_ids(scope),
        scope_id=scope_id,
        shared_scope_id=shared_scope_id,
        config={"experience": {"auto_promote_low_risk": True}},
        dry_run=False,
    )
    assert second["handbooks_created"] == 0
    assert second["duplicates_skipped"] >= 1
    assert conn.execute("SELECT COUNT(*) FROM procedural_playbooks").fetchone()[0] == 1


def test_high_risk_task_creates_candidate_but_does_not_auto_promote():
    conn = _conn()
    scope = _scope()
    scope_id = build_scope_id(scope)
    shared_scope_id = build_shared_scope_id(scope)

    _append(conn, scope=scope, session_id="session-release", turn=1, role="user", content="检查 scope-recall 是否可以推送仓库并发布。")
    _append(
        conn,
        scope=scope,
        session_id="session-release",
        turn=2,
        role="tool",
        content="Tool execution trace (terminal): pytest 327 passed; ruff ok; release gate ok; git push still requires Joy authorization.",
    )
    _append(
        conn,
        scope=scope,
        session_id="session-release",
        turn=3,
        role="assistant",
        content="完成：候选版本检查通过，但 commit/push/tag 必须等待 Joy 明确授权，不能自动执行。",
    )

    result = promote_experiences(
        conn,
        accessible_scope_ids=accessible_scope_ids(scope),
        scope_id=scope_id,
        shared_scope_id=shared_scope_id,
        config={"experience": {"auto_promote_low_risk": True}},
        dry_run=False,
    )

    assert result["handbooks_created"] == 1
    assert result["handbooks_promoted"] == 0
    assert result["handbooks_needing_agent_review"] == 1

    row = conn.execute("SELECT * FROM procedural_playbooks").fetchone()
    assert row is not None
    assert row["status"] == "needs_review"
    assert "授权" in row["pitfalls"] or "push" in row["pitfalls"].lower()
    metadata = json.loads(row["metadata"])
    assert metadata["risk_level"] == "high"

def test_overlapping_auto_promotion_window_skips_similar_existing_playbook():
    conn = _conn()
    scope = _scope()
    scope_id = build_scope_id(scope)
    shared_scope_id = build_shared_scope_id(scope)

    _append(conn, scope=scope, session_id="release-overlap", turn=1, role="user", content="检查 scope-recall 是否可以推送仓库并发布。")
    _append(conn, scope=scope, session_id="release-overlap", turn=2, role="tool", content="pytest 357 passed; ruff ok; release gate ok; git push 需要授权。")
    _append(conn, scope=scope, session_id="release-overlap", turn=3, role="assistant", content="完成：发布候选检查通过，push/tag 需等待 Joy 授权。")

    first = promote_experiences(
        conn,
        accessible_scope_ids=accessible_scope_ids(scope),
        scope_id=scope_id,
        shared_scope_id=shared_scope_id,
        config={"experience": {"auto_promote_low_risk": True}},
        dry_run=False,
    )
    assert first["handbooks_created"] == 1

    _append(conn, scope=scope, session_id="release-overlap", turn=4, role="assistant", content="补充：测试通过，release gate ok，仍然不能自动 push/tag。")

    second = promote_experiences(
        conn,
        accessible_scope_ids=accessible_scope_ids(scope),
        scope_id=scope_id,
        shared_scope_id=shared_scope_id,
        config={"experience": {"auto_promote_low_risk": True}},
        dry_run=False,
    )

    assert second["handbooks_created"] == 0
    assert second["duplicates_skipped"] >= 1
    assert second["items"][0]["reason"] == "similar_playbook_exists"
    assert conn.execute("SELECT COUNT(*) FROM procedural_playbooks").fetchone()[0] == 1
