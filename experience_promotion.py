from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Sequence

from .capture_filters import contains_secret_like_text, sanitize_report_text
from .experience_store import create_playbook, review_playbook
from .gating import compact_text
from .sql_store import ensure_schema

LOW_SIGNAL_GOALS = {
    "继续",
    "继续。",
    "进度如何",
    "进度如何了",
    "进度怎么样",
    "在吗",
    "新会话测试ok",
    "新会话测试OK",
}


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

FAILURE_PATTERNS = (
    r"\bblocked\b",
    r"\bblocker\b",
    r"\bfailed\b",
    r"\bfailure\b",
    r"\bfailing\b",
    r"\bfails\b",
    r"\btraceback\b",
    r"\bexception\b",
    r"\berrors?\b(?!\s*(?:0|zero|none|found|detected|remaining))",
    r"\bnot\s+completed?\b",
    r"\bincomplete\b",
    r"\bstill\s+(?:failing|fails|blocked)\b",
    r"\btests?\s+failed\b",
    r"失败",
    r"未完成",
    r"没完成",
    r"没有完成",
    r"阻塞",
    r"报错",
    r"仍有问题",
    r"还有问题",
    r"仍然失败",
    r"还有失败",
    r"不能沉淀",
    r"不能发布",
    r"不可发布",
)

_FAILURE_RE = re.compile("|".join(f"(?:{pattern})" for pattern in FAILURE_PATTERNS), re.IGNORECASE)

COMPLETION_TOKENS = (
    "完成",
    "通过",
    "已验证",
    "验证完成",
    "验证通过",
    "成功",
    "done",
    "fixed",
    "success",
    "passed",
    "green",
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


def _has_failure_signal(text: str) -> bool:
    return bool(_FAILURE_RE.search(text or ""))


def _entry_text(entries: Sequence[sqlite3.Row]) -> str:
    return "\n".join(str(entry["content"] or "") for entry in entries)


def _tail_text(entries: Sequence[sqlite3.Row], *, roles: set[str] | None = None, limit: int = 4) -> str:
    selected: list[str] = []
    allowed_roles = roles or {"assistant", "tool"}
    for entry in reversed(entries):
        if str(entry["role"] or "") not in allowed_roles:
            continue
        content = str(entry["content"] or "").strip()
        if content:
            selected.append(content)
        if len(selected) >= limit:
            break
    return "\n".join(reversed(selected))


def _completion_state(entries: Sequence[sqlite3.Row]) -> tuple[str, str]:
    """Classify whether the final task state is safe to promote.

    Historical logs can contain earlier success tokens followed by later failure
    or blocker signals. Automatic promotion trusts the final closure, not any
    isolated ``passed``/``ok`` token anywhere in the transcript.
    """

    text = _entry_text(entries)
    tail = _tail_text(entries)
    assistant_tail = _tail_text(entries, roles={"assistant"}, limit=3)
    if _has_failure_signal(tail) or _has_failure_signal(assistant_tail):
        return "failed", "final_failure_signal"
    if _contains_any(assistant_tail, COMPLETION_TOKENS) or _contains_any(tail, COMPLETION_TOKENS):
        return "success", "final_success_signal"
    if _has_failure_signal(text):
        return "uncertain", "historical_failure_signal"
    if _contains_any(text, SUCCESS_TOKENS):
        return "uncertain", "success_not_final"
    return "unknown", "no_completion_signal"


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


SPECIFICITY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bpython(?:3)?\s+-m\s+pytest\b", re.IGNORECASE),
    re.compile(r"\bpytest\b", re.IGNORECASE),
    re.compile(r"\b(?:ruff|pyright|doctor|release gate|systemctl|git|gh)\b", re.IGNORECASE),
    re.compile(r"(?:^|\s)(?:scripts|tests|docs|\.github)/[^\s`'\"]+", re.IGNORECASE),
    re.compile(r"(?:^|\s)/(?:home|data|etc|var|tmp)/[^\s`'\"]+"),
    re.compile(r"\b\d+\s+passed\b", re.IGNORECASE),
    re.compile(r"\bok=true\b", re.IGNORECASE),
)


def _promotion_quality(
    entries: Sequence[sqlite3.Row],
    *,
    goal: str,
    tool_names: list[str],
    verification: list[str],
    risk_level: str,
) -> dict[str, Any]:
    text = _entry_text(entries)
    reasons: list[str] = []
    score = 0.0
    normalized_goal = _goal_signal_key(goal or "")
    if goal and not _low_signal_goal(goal) and len(normalized_goal) >= 4:
        score += 0.18
    else:
        reasons.append("weak_goal")
    tool_entry_count = sum(1 for entry in entries if str(entry["role"] or "") == "tool")
    concrete_tools = [name for name in tool_names if name != "tool"]
    if tool_entry_count:
        score += 0.12
    else:
        reasons.append("no_tool_evidence")
    if concrete_tools:
        score += 0.12
    else:
        reasons.append("no_concrete_tool_names")
    if verification:
        score += min(0.24, 0.12 + 0.04 * len(verification))
    else:
        reasons.append("no_verification_evidence")
    specificity_hits = sum(1 for pattern in SPECIFICITY_PATTERNS if pattern.search(text))
    if specificity_hits:
        score += min(0.18, 0.08 + 0.04 * specificity_hits)
    else:
        reasons.append("no_specific_commands_or_paths")
    if len(entries) <= 40:
        score += 0.08
    else:
        reasons.append("oversized_episode_window")
    if risk_level == "high":
        if _contains_any(text, ("授权", "authorization", "confirm", "确认", "不能自动", "等待")):
            score += 0.08
        else:
            reasons.append("high_risk_without_authorization_boundary")
    elif risk_level == "low":
        score += 0.08
    if _has_failure_signal(_tail_text(entries)):
        score = min(score, 0.35)
        reasons.append("final_failure_signal")
    score = round(min(score, 1.0), 3)
    if score < 0.70:
        decision = "reject"
    elif score < 0.85 or risk_level == "high":
        decision = "needs_review"
    else:
        decision = "auto_promote_eligible"
    return {"score": score, "decision": decision, "reasons": reasons, "specificity_hits": specificity_hits, "tool_entry_count": tool_entry_count}


def _first_user_goal(entries: Sequence[sqlite3.Row]) -> str:
    for entry in entries:
        if str(entry["role"] or "") == "user":
            return compact_text(str(entry["content"] or ""), 180)
    return compact_text(str(entries[0]["content"] or ""), 180) if entries else "自动提取的任务"


def _goal_signal_key(goal: str) -> str:
    return re.sub(r"[\s\W_]+", "", goal, flags=re.UNICODE).lower()


def _low_signal_goal(goal: str) -> bool:
    stripped = goal.strip()
    key = _goal_signal_key(stripped)
    low_signal_keys = {_goal_signal_key(item) for item in LOW_SIGNAL_GOALS}
    if key in low_signal_keys:
        return True
    lowered = stripped.lower()
    if lowered.startswith("只回答") or lowered.startswith("只回复"):
        return True
    return False


def _title_suffix(goal: str) -> str:
    words = re.findall(r"[\w\u4e00-\u9fff-]+", goal)[:10]
    suffix = " ".join(words).strip()
    return compact_text(suffix or "自动提取任务", 48)


def _task_class(text: str) -> str:
    lowered = text.lower()
    if "scope-recall" in lowered and any(token in lowered for token in ("release", "push", "version", "发布", "推送", "版本")):
        return "scope_recall_release_closeout"
    if "scope-recall" in lowered:
        return "scope_recall_quality_check"
    if "hermes" in lowered:
        return "hermes_operations"
    return "agent_verified_task"


def _title(task_class: str, text: str, goal: str = "") -> str:
    suffix = _title_suffix(goal or text)
    if task_class == "scope_recall_release_closeout":
        return compact_text(f"scope-recall 发布收口：{suffix}", 80)
    if task_class == "scope_recall_quality_check":
        return compact_text(f"scope-recall 质量检查：{suffix}", 80)
    if task_class == "hermes_operations":
        return compact_text(f"Hermes 操作：{suffix}", 80)
    words = re.findall(r"[\w\u4e00-\u9fff-]+", text)[:8]
    fallback = " ".join(words) if words else suffix
    return compact_text(f"{fallback} 经验手册", 80)


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
        "goal": compact_text(f"复用已验证流程处理：{goal}", 220),
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


def _metadata_journal_entry_ids(raw: object) -> set[int]:
    if not raw:
        return set()
    try:
        metadata = json.loads(str(raw))
    except (TypeError, ValueError):
        return set()
    values = metadata.get("journal_entry_ids") if isinstance(metadata, dict) else None
    if not isinstance(values, list):
        return set()
    ids: set[int] = set()
    for value in values:
        try:
            ids.add(int(value))
        except (TypeError, ValueError):
            continue
    return ids


def _similar_playbook_exists(
    conn: sqlite3.Connection,
    *,
    accessible_scope_ids: Sequence[str],
    task_class: str,
    title: str,
    entry_ids: Sequence[int],
    min_overlap_ratio: float = 0.75,
) -> dict[str, Any] | None:
    """Detect near-duplicate auto playbooks from overlapping journal windows."""

    candidate_ids = {int(value) for value in entry_ids}
    if not candidate_ids:
        return None
    scopes = [str(scope_id) for scope_id in accessible_scope_ids if str(scope_id)]
    if not scopes:
        return None
    placeholders = ",".join("?" for _ in scopes)
    rows = conn.execute(
        f"""
        SELECT id, status, metadata
        FROM procedural_playbooks
        WHERE scope_id IN ({placeholders})
          AND task_class = ?
          AND title = ?
          AND status IN ('candidate', 'needs_review', 'reviewed', 'promoted')
        ORDER BY updated_at DESC, created_at DESC
        """,
        [*scopes, task_class, title],
    ).fetchall()
    for row in rows:
        existing_ids = _metadata_journal_entry_ids(row["metadata"])
        if not existing_ids:
            continue
        overlap = len(candidate_ids & existing_ids)
        if not overlap:
            continue
        ratio = overlap / max(1, min(len(candidate_ids), len(existing_ids)))
        if ratio >= min_overlap_ratio:
            return {
                "id": str(row["id"]),
                "status": str(row["status"]),
                "overlap": overlap,
                "candidate_ids": len(candidate_ids),
                "existing_ids": len(existing_ids),
                "overlap_ratio": round(ratio, 4),
            }
    return None


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
            _json_dumps([sanitize_report_text(compact_text(str(entry["content"] or ""), 260)) for entry in entries if str(entry["role"] or "") in {"tool", "assistant"}][:8]),
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
    auto_promote_low_risk = _coerce_bool(experience_config, "auto_promote_low_risk", False)
    result: dict[str, Any] = {
        "dry_run": bool(dry_run),
        "episodes_created": 0,
        "handbooks_created": 0,
        "handbooks_promoted": 0,
        "handbooks_needing_agent_review": 0,
        "duplicates_skipped": 0,
        "quality_rejected": 0,
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
        completion_state, completion_reason = _completion_state(entries)
        if completion_state != "success":
            result["skipped"] += 1
            result["items"].append({"action": "skip", "reason": completion_reason, "completion_state": completion_state})
            continue
        if tool_entry_count < min_tool_entries or not _contains_any(text, SUCCESS_TOKENS):
            result["skipped"] += 1
            continue
        if require_verification and not verification:
            result["skipped"] += 1
            continue
        goal = _first_user_goal(entries)
        if _low_signal_goal(goal):
            result["skipped"] += 1
            result["items"].append({"action": "skip", "reason": "low_signal_goal", "goal": sanitize_report_text(goal)})
            continue
        task_class = _task_class(text)
        risk_level = _risk_level(text)
        quality = _promotion_quality(entries, goal=goal, tool_names=tool_names, verification=verification, risk_level=risk_level)
        if quality["decision"] == "reject":
            result["skipped"] += 1
            result["quality_rejected"] += 1
            result["items"].append({"action": "skip", "reason": "quality_gate", "quality": quality, "goal": sanitize_report_text(goal)})
            continue
        episode_id = _hash_id("episode_auto", scope_id, entries[0]["session_id"], [int(entry["id"]) for entry in entries])
        if _episode_exists(conn, episode_id) or _playbook_exists_for_episode(conn, episode_id):
            result["duplicates_skipped"] += 1
            continue
        title = _title(task_class, text, goal)
        entry_ids = [int(entry["id"]) for entry in entries]
        similar = _similar_playbook_exists(
            conn,
            accessible_scope_ids=accessible_scope_ids,
            task_class=task_class,
            title=title,
            entry_ids=entry_ids,
        )
        if similar is not None:
            result["duplicates_skipped"] += 1
            result["items"].append({"action": "skip", "reason": "similar_playbook_exists", "similar_playbook": similar})
            continue
        playbook_id = _hash_id("pb_auto", episode_id, title)
        if dry_run:
            result["episodes_created"] += 1
            result["handbooks_created"] += 1
            if quality["decision"] == "auto_promote_eligible" and auto_promote_low_risk:
                result["handbooks_promoted"] += 1
            elif quality["decision"] != "auto_promote_eligible":
                result["handbooks_needing_agent_review"] += 1
            result["items"].append({"action": "would_create", "episode_id": episode_id, "playbook_id": playbook_id, "risk_level": risk_level, "quality": quality})
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
        payload["confidence"] = max(float(payload["confidence"]), float(quality["score"]))
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
                "safe_summary": sanitize_report_text(compact_text(text, 500)),
                "quality_gate": quality,
            },
        )
        result["handbooks_created"] += 1
        status = created.get("status")
        if quality["decision"] == "auto_promote_eligible" and auto_promote_low_risk:
            reviewed = review_playbook(
                conn,
                playbook_id=playbook_id,
                accessible_scope_ids=[scope_id, shared_scope_id],
                action="promote",
                reason=f"自动提取经验自检通过：低风险、有验证证据，quality_score={quality['score']}。",
            )
            status = reviewed.get("status", status)
            result["handbooks_promoted"] += 1
        elif quality["decision"] != "auto_promote_eligible":
            reviewed = review_playbook(
                conn,
                playbook_id=playbook_id,
                accessible_scope_ids=[scope_id, shared_scope_id],
                action="needs_review",
                reason=f"自动提取经验质量门槛要求复核：risk={risk_level}, decision={quality['decision']}, score={quality['score']}。",
            )
            status = reviewed.get("status", status)
            result["handbooks_needing_agent_review"] += 1
        result["items"].append({"action": "created", "episode_id": episode_id, "playbook_id": playbook_id, "risk_level": risk_level, "status": status, "quality": quality})
    if not dry_run:
        conn.commit()
    return result
