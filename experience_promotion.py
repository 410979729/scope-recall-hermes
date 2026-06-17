from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Sequence

from .capture_filters import contains_secret_like_text, redact_secret_like_text
from .experience_store import create_playbook, review_playbook
from .gating import compact_text
from .sql_store import ensure_schema

SUCCESS_TOKENS = (
    "passed",
    "pass",
    "ok",
    "green",
    "完成",
    "通过",
    "已验证",
    "验证完成",
    "成功",
)

VERIFICATION_TOKENS = (
    "pytest",
    "ruff",
    "doctor",
    "release gate",
    "smoke",
    "测试",
    "检查通过",
    "验证",
)

HIGH_RISK_TOKENS = (
    "push",
    "commit",
    "tag",
    "restart",
    "delete",
    "rm -",
    "token",
    "password",
    "secret",
    "api key",
    "密钥",
    "密码",
    "凭据",
    "重启",
    "删除",
    "推送",
    "提交仓库",
)

TOOL_HINTS = (
    "pytest",
    "ruff",
    "doctor",
    "release gate",
    "terminal",
    "git",
    "gh",
    "browser",
    "web_search",
    "scope_recall",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _hash_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha1("\n".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _coerce_bool(config: dict[str, Any], key: str, default: bool) -> bool:
    raw = config.get(key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def _experience_config(config: dict[str, Any] | None) -> dict[str, Any]:
    raw = (config or {}).get("experience")
    return dict(raw) if isinstance(raw, dict) else {}


def _contains_any(text: str, tokens: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(token.lower() in lowered for token in tokens)


def _entry_text(entries: Sequence[sqlite3.Row]) -> str:
    return "\n".join(str(entry["content"] or "") for entry in entries)


def _tool_names(entries: Sequence[sqlite3.Row]) -> list[str]:
    text = _entry_text(entries).lower()
    names = {"tool" for entry in entries if str(entry["role"] or "") == "tool"}
    for hint in TOOL_HINTS:
        if hint in text:
            names.add(hint)
    return sorted(names)


def _verification(entries: Sequence[sqlite3.Row]) -> list[str]:
    text = _entry_text(entries)
    checks: list[str] = []
    lowered = text.lower()
    if "pytest" in lowered or "测试" in text:
        checks.append("测试结果显示通过。")
    if "ruff" in lowered:
        checks.append("代码静态检查通过。")
    if "doctor" in lowered:
        checks.append("健康检查通过。")
    if "release gate" in lowered:
        checks.append("发布检查通过。")
    if not checks and _contains_any(text, VERIFICATION_TOKENS):
        checks.append("任务记录包含明确验证信号。")
    return checks


def _risk_level(text: str) -> str:
    if contains_secret_like_text(text):
        return "secret"
    return "high" if _contains_any(text, HIGH_RISK_TOKENS) else "low"


def _first_user_goal(entries: Sequence[sqlite3.Row]) -> str:
    for entry in entries:
        if str(entry["role"] or "") == "user":
            return compact_text(str(entry["content"] or ""), 180)
    return compact_text(str(entries[0]["content"] or ""), 180) if entries else "自动提取的任务"


def _task_class(text: str) -> str:
    lowered = text.lower()
    if "scope-recall" in lowered and any(token in lowered for token in ("release", "push", "version", "发布", "推送", "版本")):
        return "scope_recall_release_closeout"
    if "scope-recall" in lowered:
        return "scope_recall_quality_check"
    if "hermes" in lowered:
        return "hermes_operations"
    return "agent_verified_task"


def _title(task_class: str, text: str) -> str:
    if task_class == "scope_recall_release_closeout":
        return "scope-recall 发布候选收口经验手册"
    if task_class == "scope_recall_quality_check":
        return "scope-recall 质量检查经验手册"
    if task_class == "hermes_operations":
        return "Hermes 操作经验手册"
    words = re.findall(r"[\w\u4e00-\u9fff-]+", text)[:8]
    suffix = " ".join(words) if words else "自动提取任务"
    return compact_text(f"{suffix} 经验手册", 80)


def _payload(*, task_class: str, title: str, goal: str, text: str, risk_level: str, tool_names: list[str], verification: list[str]) -> dict[str, Any]:
    high_risk = risk_level == "high"
    capability = "local_write" if high_risk else "read_only"
    pitfalls = [
        {
            "signal": "任务记录来自自动提取",
            "mistake": "把一次性结果当成永久事实",
            "correction": "复用前必须重新读取现场证据。",
        }
    ]
    if high_risk:
        pitfalls.append(
            {
                "signal": "涉及推送、发布、重启、删除或凭据相邻操作",
                "mistake": "自动执行高风险动作",
                "correction": "只复用检查流程；执行前必须现场核验并遵守 Joy 授权边界。",
            }
        )
    return {
        "schema_version": "procedural_playbook.v1",
        "task_class": task_class,
        "title": title,
        "trigger": f"遇到类似任务：{goal}",
        "goal": "复用已验证的执行顺序，减少重复踩坑，同时保留现场核验。",
        "preconditions": [
            {"id": "p1", "check": "确认当前任务与经验手册目标一致。", "evidence_required": "用户请求或任务描述"},
            {"id": "p2", "check": "复用前重新读取现场状态。", "evidence_required": "本轮工具输出或文件/服务状态"},
        ],
        "steps": [
            {
                "number": 1,
                "capability_class": "read_only",
                "action": "先读取当前现场状态，不使用旧记忆替代现场证据。",
                "evidence_required": "本轮读取到的文件、仓库、服务或配置状态",
                "why": "自动经验只能给流程，不能替代实时事实。",
                "previous_mistakes": ["把旧发布状态或旧路径当成当前事实。"],
            },
            {
                "number": 2,
                "capability_class": capability,
                "action": "按已验证顺序执行最小必要检查。",
                "evidence_required": ", ".join(tool_names) if tool_names else "相关工具检查输出",
                "why": "任务轨迹显示这些检查曾经证明结果可靠。",
                "previous_mistakes": [],
            },
            {
                "number": 3,
                "capability_class": "read_only",
                "action": "收尾时明确列出通过项、剩余风险和是否需要 Joy 授权。",
                "evidence_required": "测试/检查结果和授权边界说明",
                "why": "避免把候选状态误报成已发布或已执行。",
                "previous_mistakes": ["把本地候选版本说成远端正式版本。"],
            },
        ],
        "pitfalls": pitfalls,
        "verification": verification or ["任务记录包含成功和验证信号。"],
        "cleanup": ["清理临时产物或说明未清理原因。", "记录哪些事实需要下次 live check。"],
        "reuse_policy": {
            "default_decision": "guided_reuse" if high_risk else "direct_reuse",
            "allow_direct_reuse": not high_risk,
            "risk_level": risk_level,
        },
        "status": "candidate",
        "confidence": 0.78 if high_risk else 0.86,
    }


def _load_candidate_sessions(conn: sqlite3.Connection, *, accessible_scope_ids: Sequence[str], limit_sessions: int) -> list[list[sqlite3.Row]]:
    scopes = [str(scope_id) for scope_id in accessible_scope_ids if str(scope_id)]
    if not scopes:
        return []
    placeholders = ",".join("?" for _ in scopes)
    rows = conn.execute(
        f"""
        SELECT *
        FROM journal_entries
        WHERE scope_id IN ({placeholders})
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        [*scopes, max(20, limit_sessions * 20)],
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in reversed(rows):
        grouped[str(row["session_id"] or "")].append(row)
    sessions = [entries for _, entries in sorted(grouped.items(), key=lambda item: str(item[1][-1]["created_at"]), reverse=True)]
    return sessions[: max(1, limit_sessions)]


def _episode_exists(conn: sqlite3.Connection, episode_id: str) -> bool:
    return conn.execute("SELECT 1 FROM task_episodes WHERE id = ?", (episode_id,)).fetchone() is not None


def _playbook_exists_for_episode(conn: sqlite3.Connection, episode_id: str) -> bool:
    return conn.execute("SELECT 1 FROM procedural_playbooks WHERE created_from_episode_id = ?", (episode_id,)).fetchone() is not None


def _insert_episode(
    conn: sqlite3.Connection,
    *,
    episode_id: str,
    scope_id: str,
    shared_scope_id: str,
    entries: Sequence[sqlite3.Row],
    task_class: str,
    goal: str,
    outcome: str,
    tool_names: list[str],
    verification: list[str],
    risk_level: str,
) -> None:
    now = _now_iso()
    ids = [int(entry["id"]) for entry in entries]
    conn.execute(
        """
        INSERT INTO task_episodes(
            id, scope_id, shared_scope_id, session_id, task_class, task_goal, user_intent,
            status, outcome, started_at, ended_at, message_ids, journal_entry_ids,
            tool_names, evidence, verification, environment, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'closed', ?, ?, ?, '[]', ?, ?, ?, ?, '{}', ?)
        """,
        (
            episode_id,
            scope_id,
            shared_scope_id,
            str(entries[0]["session_id"] or ""),
            task_class,
            goal,
            goal,
            outcome,
            str(entries[0]["created_at"]),
            str(entries[-1]["created_at"]),
            _json_dumps(ids),
            _json_dumps(tool_names),
            _json_dumps([compact_text(str(entry["content"] or ""), 260) for entry in entries if str(entry["role"] or "") in {"tool", "assistant"}][:8]),
            _json_dumps(verification),
            _json_dumps({"auto_extracted": True, "risk_level": risk_level, "created_at": now}),
        ),
    )


def promote_experiences(
    conn: sqlite3.Connection,
    *,
    accessible_scope_ids: Sequence[str],
    scope_id: str,
    shared_scope_id: str = "",
    config: dict[str, Any] | None = None,
    limit_sessions: int = 20,
    dry_run: bool = True,
) -> dict[str, Any]:
    """自动从任务轨迹中提取可复用经验手册。

    这个函数只使用 SQLite 中已经存在的任务轨迹，不调用外部模型；第一版强调可审计、可回放和低风险。
    """

    ensure_schema(conn)
    experience_config = _experience_config(config)
    min_entries = int(experience_config.get("promotion_min_entries") or 3)
    min_tool_entries = int(experience_config.get("promotion_min_tool_entries") or 1)
    require_verification = _coerce_bool(experience_config, "promotion_require_verification", True)
    auto_promote_low_risk = _coerce_bool(experience_config, "auto_promote_low_risk", True)
    result: dict[str, Any] = {
        "dry_run": bool(dry_run),
        "episodes_created": 0,
        "handbooks_created": 0,
        "handbooks_promoted": 0,
        "handbooks_needing_agent_review": 0,
        "duplicates_skipped": 0,
        "skipped": 0,
        "items": [],
    }

    for entries in _load_candidate_sessions(conn, accessible_scope_ids=accessible_scope_ids, limit_sessions=limit_sessions):
        if len(entries) < min_entries:
            result["skipped"] += 1
            continue
        text = _entry_text(entries)
        if contains_secret_like_text(text):
            result["skipped"] += 1
            result["items"].append({"action": "skip", "reason": "secret-like-content"})
            continue
        tool_names = _tool_names(entries)
        tool_entry_count = sum(1 for entry in entries if str(entry["role"] or "") == "tool")
        verification = _verification(entries)
        if tool_entry_count < min_tool_entries or not _contains_any(text, SUCCESS_TOKENS):
            result["skipped"] += 1
            continue
        if require_verification and not verification:
            result["skipped"] += 1
            continue
        goal = _first_user_goal(entries)
        task_class = _task_class(text)
        risk_level = _risk_level(text)
        episode_id = _hash_id("episode_auto", scope_id, entries[0]["session_id"], [int(entry["id"]) for entry in entries])
        if _episode_exists(conn, episode_id) or _playbook_exists_for_episode(conn, episode_id):
            result["duplicates_skipped"] += 1
            continue
        title = _title(task_class, text)
        playbook_id = _hash_id("pb_auto", episode_id, title)
        if dry_run:
            result["episodes_created"] += 1
            result["handbooks_created"] += 1
            if risk_level == "low" and auto_promote_low_risk:
                result["handbooks_promoted"] += 1
            elif risk_level == "high":
                result["handbooks_needing_agent_review"] += 1
            result["items"].append({"action": "would_create", "episode_id": episode_id, "playbook_id": playbook_id, "risk_level": risk_level})
            continue

        _insert_episode(
            conn,
            episode_id=episode_id,
            scope_id=scope_id,
            shared_scope_id=shared_scope_id,
            entries=entries,
            task_class=task_class,
            goal=goal,
            outcome="success",
            tool_names=tool_names,
            verification=verification,
            risk_level=risk_level,
        )
        result["episodes_created"] += 1
        payload = _payload(task_class=task_class, title=title, goal=goal, text=text, risk_level=risk_level, tool_names=tool_names, verification=verification)
        created = create_playbook(
            conn,
            playbook_id=playbook_id,
            scope_id=scope_id,
            shared_scope_id=shared_scope_id,
            payload=payload,
            status="candidate",
            confidence=float(payload["confidence"]),
            created_from_episode_id=episode_id,
            evidence_anchors=[{"kind": "journal_entries", "ids": [int(entry["id"]) for entry in entries]}],
            related_skills=[],
            environment_constraints={"risk_level": risk_level, "requires_live_check": True},
            metadata={
                "auto_extracted": True,
                "risk_level": risk_level,
                "source": "experience_promotion",
                "journal_entry_ids": [int(entry["id"]) for entry in entries],
                "safe_summary": redact_secret_like_text(compact_text(text, 500)),
            },
        )
        result["handbooks_created"] += 1
        status = created.get("status")
        if risk_level == "low" and auto_promote_low_risk:
            reviewed = review_playbook(
                conn,
                playbook_id=playbook_id,
                accessible_scope_ids=[scope_id, shared_scope_id],
                action="promote",
                reason="自动提取经验自检通过：低风险且有验证证据。",
            )
            status = reviewed.get("status", status)
            result["handbooks_promoted"] += 1
        elif risk_level == "high":
            reviewed = review_playbook(
                conn,
                playbook_id=playbook_id,
                accessible_scope_ids=[scope_id, shared_scope_id],
                action="needs_review",
                reason="自动提取经验自检发现高风险动作；需要后续代理复核，不要求用户逐条复审。",
            )
            status = reviewed.get("status", status)
            result["handbooks_needing_agent_review"] += 1
        result["items"].append({"action": "created", "episode_id": episode_id, "playbook_id": playbook_id, "risk_level": risk_level, "status": status})
    if not dry_run:
        conn.commit()
    return result
