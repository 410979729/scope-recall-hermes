"""Synthesize weak-model-friendly procedural playbook payloads.

The storage layer remains in `experience_store.py`; this module only builds the
validated playbook vocabulary from episode evidence, risk level, and verification
signals.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from .capture_filters import sanitize_report_text
from .gating import compact_text


def _anchor_kinds(evidence_anchors: Sequence[Mapping[str, Any]] | None) -> list[str]:
    kinds: list[str] = []
    for anchor in evidence_anchors or []:
        kind = str(anchor.get("kind") or "").strip()
        if kind and kind not in kinds:
            kinds.append(kind)
    return kinds


def build_experience_playbook_payload(
    *,
    task_class: str,
    title: str,
    goal: str,
    risk_level: str,
    tool_names: Sequence[str],
    verification: Sequence[str],
    evidence_anchors: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a procedural playbook payload with explicit safety boundaries."""
    normalized_risk_level = str(risk_level or "low").strip().lower() or "low"
    high_risk = normalized_risk_level in {"high", "secret"}
    secret_risk = normalized_risk_level == "secret"
    goal_for_storage = compact_text(sanitize_report_text(goal), 220)
    verification_missing = not bool(verification)
    capability = "local_write" if high_risk and not secret_risk else "read_only"
    anchor_kinds = _anchor_kinds(evidence_anchors)
    must_stop = [
        "现场证据与经验手册目标不一致。",
        "验证命令失败、缺少输出，或最后状态不是完成/通过。",
        "无法判断是否继续复用时，必须停下问 Joy。",
    ]
    prohibited = ["不得用旧记忆替代本轮 live 证据。"]
    pitfalls = [
        {
            "signal": "任务记录来自自动提取",
            "mistake": "把一次性结果当成永久事实",
            "correction": "复用前必须重新读取现场证据。",
        }
    ]
    if high_risk:
        if secret_risk:
            must_stop.append("涉及凭据、token、secret 或密钥相邻信息；必须由 Joy 明确授权并重新核验证据。")
            prohibited.append("不得自动复用、传播或执行包含凭据相邻上下文的经验手册。")
        must_stop.append("涉及推送、发布、重启、删除、迁移、远程/跨实例或凭据相邻操作且 Joy 未明确授权。")
        prohibited.append("不得自动执行发布、推送、重启、删除、迁移或凭据相邻动作。")
        pitfalls.append(
            {
                "signal": "涉及推送、发布、重启、删除或凭据相邻操作",
                "mistake": "自动执行高风险动作",
                "correction": "只复用检查流程；执行前必须现场核验并遵守 Joy 授权边界。",
            }
        )
    if verification_missing:
        must_stop.append("缺少验证输出；复用前必须停下补验证或问 Joy。")
        prohibited.append("不得把缺少验证输出的经验手册直接当作已验证流程自动复用。")
        pitfalls.append(
            {
                "signal": "经验手册没有原始验证命令或通过输出",
                "mistake": "把闭合文字当成验证证据",
                "correction": "降为人工复核；只有重新取得现场验证输出后才能继续。",
            }
        )
    verification_list = list(verification) or ["verification_missing_requires_review"]
    return {
        "schema_version": "procedural_playbook.v1",
        "task_class": str(task_class or "agent_verified_task"),
        "title": str(title or "Agent：已验证任务流程"),
        "trigger": f"遇到类似任务：{goal_for_storage}",
        "goal": compact_text(f"复用已验证流程处理：{goal_for_storage}", 220),
        "preconditions": [
            {"id": "p1", "check": "确认当前任务与经验手册目标一致。", "evidence_required": "用户请求或任务描述"},
            {"id": "p2", "check": "复用前重新读取现场状态。", "evidence_required": "本轮工具输出或文件/服务状态"},
            {"id": "p3", "check": "确认来源证据锚点覆盖目标、工具检查和最终闭合。", "evidence_required": ", ".join(anchor_kinds) if anchor_kinds else "journal_entries"},
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
                "evidence_required": ", ".join(str(name) for name in tool_names) if tool_names else "相关工具检查输出",
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
        "verification": verification_list,
        "cleanup": ["清理临时产物或说明未清理原因。", "记录哪些事实需要下次 live check。"],
        "reuse_policy": {
            "default_decision": "guided_reuse" if high_risk or verification_missing else "direct_reuse",
            "allow_direct_reuse": not high_risk and not verification_missing,
            "risk_level": normalized_risk_level,
            "must_stop_and_ask_joy": must_stop,
            "prohibited_auto_actions": prohibited,
            "source_evidence_anchor_count": len(evidence_anchors or []),
            "source_evidence_anchor_kinds": anchor_kinds,
        },
        "status": "needs_review" if verification_missing else "candidate",
        "confidence": 0.62 if verification_missing else 0.78 if high_risk else 0.86,
    }
