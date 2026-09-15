"""Promotion planner for moving reviewed Experience playbooks into active procedural memory.

Promotion must respect quality gates, duplicate/supersession state, and operator review outcomes."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Sequence

from .capture_filters import (
    classify_transport_noise,
    contains_secret_like_text,
    sanitize_report_text,
)
from .experience_classification import classify_experience_task
from .experience_evidence import extract_evidence_anchors
from .experience_quality import assess_experience_quality
from .experience_store import create_playbook, review_playbook
from .experience_synthesis import build_experience_playbook_payload
from .gating import compact_text
from .sql_store import ensure_schema
from .task_boundary import SUCCESS_TOKENS, classify_task_closure, goal_signal_key, has_failure_signal, is_low_signal_goal

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
    return has_failure_signal(text)


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
    """Classify whether the final task state is safe to promote."""
    closure = classify_task_closure(entries)
    return closure.state, closure.reason


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


def _promotion_quality(
    entries: Sequence[sqlite3.Row],
    *,
    goal: str,
    tool_names: list[str],
    verification: list[str],
    risk_level: str,
) -> dict[str, Any]:
    """Calculate promotion quality evidence for a playbook."""
    return assess_experience_quality(entries, goal=goal, tool_names=tool_names, verification=verification, risk_level=risk_level)


def _first_user_goal(entries: Sequence[sqlite3.Row]) -> str:
    for entry in entries:
        if str(entry["role"] or "") == "user":
            return compact_text(str(entry["content"] or ""), 180)
    return compact_text(str(entries[0]["content"] or ""), 180) if entries else "自动提取的任务"


def _goal_signal_key(goal: str) -> str:
    return goal_signal_key(goal)


def _low_signal_goal(goal: str) -> bool:
    return is_low_signal_goal(goal)


def _title_suffix(goal: str) -> str:
    words = re.findall(r"[\w\u4e00-\u9fff-]+", goal)[:10]
    suffix = " ".join(words).strip()
    return compact_text(suffix or "自动提取任务", 48)


def _task_class(text: str, goal: str = "") -> str:
    return classify_experience_task(text=text, goal=goal).task_class


def _title(task_class: str, text: str, goal: str = "") -> str:
    classification = classify_experience_task(text=text, goal=goal)
    if classification.task_class == task_class:
        return classification.title
    fallback_titles = {
        "scope_recall_release_closeout": "scope-recall：发布收口",
        "scope_recall_docs_quality": "scope-recall：文档质量检查",
        "scope_recall_quality_check": "scope-recall：质量检查",
        "scope_recall_memory_quality_governance": "scope-recall：记忆质量治理",
        "journal_backlog_drain": "scope-recall：journal backlog 清理",
        "github_release_publish": "GitHub：release 发布核验",
        "hermes_operations": "Hermes：运行维护",
        "agent_verified_task": "Agent：已验证任务流程",
    }
    return fallback_titles.get(task_class, "Agent：已验证任务流程")


def _payload(
    *,
    task_class: str,
    title: str,
    goal: str,
    text: str,
    risk_level: str,
    tool_names: list[str],
    verification: list[str],
    evidence_anchors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the structured promotion payload for one playbook candidate."""
    return build_experience_playbook_payload(
        task_class=task_class,
        title=title,
        goal=goal,
        risk_level=risk_level,
        tool_names=tool_names,
        verification=verification,
        evidence_anchors=evidence_anchors,
    )


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _missing_tables(conn: sqlite3.Connection, required: Sequence[str]) -> list[str]:
    existing = _table_names(conn)
    return [name for name in required if name not in existing]


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
    evidence_anchors: list[dict[str, Any]] | None = None,
) -> None:
    now = _now_iso()
    ids = [int(entry["id"]) for entry in entries]
    anchors = evidence_anchors if evidence_anchors is not None else extract_evidence_anchors(entries)
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
            _json_dumps(anchors),
            _json_dumps(verification),
            _json_dumps({"auto_extracted": True, "risk_level": risk_level, "created_at": now, "evidence_anchor_count": len(anchors)}),
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

    accessible = [str(item) for item in accessible_scope_ids if str(item)]
    if str(scope_id or "") not in accessible:
        raise ValueError("scope_id must be in accessible_scope_ids")
    if shared_scope_id and str(shared_scope_id) not in accessible:
        raise ValueError("shared_scope_id must be in accessible_scope_ids")
    if not dry_run:
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
        "schema_missing": [],
        "items": [],
    }
    required_input_tables = ["journal_entries"]
    missing_input_tables = _missing_tables(conn, required_input_tables)
    if missing_input_tables:
        result["schema_missing"] = missing_input_tables
        result["skipped"] += 1
        result["items"].append({"action": "skip", "reason": "schema_missing", "missing_tables": missing_input_tables})
        return result
    missing_experience_tables = _missing_tables(conn, ["task_episodes", "procedural_playbooks"])
    result["schema_missing"] = missing_experience_tables

    for entries in _load_candidate_sessions(conn, accessible_scope_ids=accessible_scope_ids, limit_sessions=limit_sessions):
        transport_reasons: set[str] = set()
        clean_entries: list[sqlite3.Row] = []
        for entry in entries:
            entry_content = str(entry["content"] or "")
            transport = classify_transport_noise(entry_content)
            if transport.blocked:
                transport_reasons.update(transport.reason_codes)
                continue
            clean_entries.append(entry)
        if transport_reasons:
            entries = clean_entries
        if len(entries) < min_entries:
            result["skipped"] += 1
            if transport_reasons:
                result["items"].append(
                    {
                        "action": "skip",
                        "reason": "transport_noise_insufficient_evidence",
                        "transport_reason_codes": sorted(transport_reasons),
                    }
                )
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
        storage_goal = sanitize_report_text(goal)
        classification = classify_experience_task(text=text, goal=goal)
        task_class = classification.task_class
        risk_level = _risk_level(text)
        quality = _promotion_quality(entries, goal=goal, tool_names=tool_names, verification=verification, risk_level=risk_level)
        if quality["decision"] == "reject":
            result["skipped"] += 1
            result["quality_rejected"] += 1
            result["items"].append({"action": "skip", "reason": "quality_gate", "quality": quality, "goal": sanitize_report_text(goal)})
            continue
        episode_id = _hash_id("episode_auto", scope_id, entries[0]["session_id"], [int(entry["id"]) for entry in entries])
        if not missing_experience_tables and (_episode_exists(conn, episode_id) or _playbook_exists_for_episode(conn, episode_id)):
            result["duplicates_skipped"] += 1
            continue
        title = classification.title
        entry_ids = [int(entry["id"]) for entry in entries]
        evidence_anchors = extract_evidence_anchors(entries)
        similar = None
        if not missing_experience_tables:
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
        candidate_summary_text = _entry_text(
            [
                entry
                for entry in entries
                if not classify_transport_noise(str(entry["content"] or "")).blocked
            ]
        )
        safe_summary = sanitize_report_text(
            compact_text(candidate_summary_text, 500)
        )
        persistence_transport_reasons: set[str] = set()
        for candidate_text in (storage_goal, title, safe_summary):
            persistence_transport_reasons.update(
                classify_transport_noise(candidate_text).reason_codes
            )
        if persistence_transport_reasons:
            result["skipped"] += 1
            result["items"].append(
                {
                    "action": "skip",
                    "reason": "transport_noise_persistence_revalidation",
                    "transport_reason_codes": sorted(
                        persistence_transport_reasons
                    ),
                }
            )
            continue
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
            goal=storage_goal,
            outcome="success",
            tool_names=tool_names,
            verification=verification,
            risk_level=risk_level,
            evidence_anchors=evidence_anchors,
        )
        result["episodes_created"] += 1
        payload = _payload(
            task_class=task_class,
            title=title,
            goal=storage_goal,
            text=text,
            risk_level=risk_level,
            tool_names=tool_names,
            verification=verification,
            evidence_anchors=evidence_anchors,
        )
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
            evidence_anchors=[{"kind": "journal_entries", "ids": entry_ids}, *evidence_anchors],
            related_skills=[],
            environment_constraints={"risk_level": risk_level, "requires_live_check": True},
            metadata={
                "auto_extracted": True,
                "risk_level": risk_level,
                "source": "experience_promotion",
                "journal_entry_ids": entry_ids,
                "evidence_anchor_count": len(evidence_anchors),
                "safe_summary": safe_summary,
                "quality_gate": quality,
                "classification": {
                    "task_class": classification.task_class,
                    "title": classification.title,
                    "domain": classification.domain,
                    "reusable_action": classification.reusable_action,
                    "matched_rule": classification.matched_rule,
                },
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
