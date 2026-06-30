"""Bootstrap logic for creating Experience playbooks from vetted memories and journal evidence.

Bootstrap is conservative: it should create reviewable procedural candidates, not silently promote every repeated interaction."""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping, Sequence

from .experience_store import create_playbook, inspect_playbook, review_playbook


def _step(number: int, action: str, evidence_required: str, *, capability_class: str = "read_only", why: str = "") -> dict[str, Any]:
    payload = {
        "number": number,
        "capability_class": capability_class,
        "action": action,
        "evidence_required": evidence_required,
    }
    if why:
        payload["why"] = why
    return payload


def _playbook(
    *,
    playbook_id: str,
    task_class: str,
    title: str,
    trigger: str,
    goal: str,
    steps: Sequence[Mapping[str, Any]],
    verification: Sequence[str],
    risk_level: str = "low",
    related_skills: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "id": playbook_id,
        "payload": {
            "schema_version": "procedural_playbook.v1",
            "task_class": task_class,
            "title": title,
            "trigger": trigger,
            "goal": goal,
            "preconditions": [
                {"id": "p1", "check": "确认当前任务与该经验手册目标一致。", "evidence_required": "用户请求或任务描述"},
                {"id": "p2", "check": "复用前读取 live 现场，不把旧经验当当前事实。", "evidence_required": "本轮工具输出"},
            ],
            "steps": list(steps),
            "pitfalls": [
                {"signal": "旧经验包含具体版本、路径或发布状态", "mistake": "把旧事实当当前事实", "correction": "只复用流程；所有状态必须 live check。"},
                {"signal": "任务涉及发布、push、tag、删除或重启", "mistake": "未经授权执行高风险动作", "correction": "先报告验证结果，等待 Joy 明确授权。"},
            ],
            "verification": list(verification),
            "cleanup": ["记录已验证项、未验证边界和临时产物处理情况。"],
            "reuse_policy": {
                "default_decision": "guided_reuse" if risk_level != "low" else "direct_reuse",
                "allow_direct_reuse": risk_level == "low",
                "risk_level": risk_level,
                "requires_live_check": True,
            },
            "status": "candidate",
            "confidence": 0.88 if risk_level == "low" else 0.82,
        },
        "related_skills": list(related_skills),
        "environment_constraints": {"risk_level": risk_level, "requires_live_check": True},
        "metadata": {"source": "experience_bootstrap", "curated": True},
    }


CORE_PLAYBOOKS: tuple[dict[str, Any], ...] = (
    _playbook(
        playbook_id="pb_core_scope_recall_release_closeout",
        task_class="scope_recall_release_closeout",
        title="scope-recall：发布收口",
        trigger="Scope Recall 发版、版本收口、docs/README/PyPI/GitHub release 核验任务。",
        goal="用最小风险流程完成 Scope Recall 发布候选验证和收口报告。",
        steps=(
            _step(1, "读取 git status、版本号、release workflow 和发布说明现场。", "git/config/workflow/docs 当前输出"),
            _step(2, "运行 focused tests、ruff、pyright、release gate。", "真实命令输出", capability_class="local_write"),
            _step(3, "发布前回读 tag/release/PyPI/CI；未获授权不得 push/tag/release。", "远端回读或明确未执行边界"),
        ),
        verification=("focused tests 通过", "ruff/pyright 通过", "release gate 通过", "远端状态已回读或明确未发布"),
        risk_level="high",
        related_skills=("debugging-and-quality-workflows", "github-workflows"),
    ),
    _playbook(
        playbook_id="pb_core_journal_backlog_drain",
        task_class="journal_backlog_drain",
        title="scope-recall：journal backlog 清理",
        trigger="Scope Recall journal backlog、digest、retry/dead-letter、watermark 修复任务。",
        goal="安全推进 journal backlog drain，保证 rejected/covered 也推进水位且可审计。",
        steps=(
            _step(1, "只读统计 journal backlog、processed_run、水位、retry/dead-letter 分类。", "doctor/journal report 输出"),
            _step(2, "先写回归测试，再修复 replay/rejection/watermark 逻辑。", "新增或更新测试"),
            _step(3, "运行 journal focused tests 与 doctor smoke；live apply 前备份 SQLite。", "测试输出和备份路径"),
        ),
        verification=("journal recovery tests 通过", "doctor journal health 通过", "release gate 不缺新增文件"),
        risk_level="high",
        related_skills=("debugging-and-quality-workflows",),
    ),
    _playbook(
        playbook_id="pb_core_scope_recall_codex_audit_loop",
        task_class="scope_recall_codex_audit_loop",
        title="scope-recall：Codex 审计修复闭环",
        trigger="Scope Recall 复杂修复后需要 Codex/子代理/独立审计的任务。",
        goal="把外部/子代理审计当作不可信候选，逐条复现、测试、修复、验证。",
        steps=(
            _step(1, "读取审计意见并映射到具体代码路径和失败模式。", "审计项与源码位置"),
            _step(2, "为有效问题添加 counterexample/regression test。", "失败测试或新增断言"),
            _step(3, "修复后跑 focused tests，再跑 broader gates。", "测试和 gate 输出", capability_class="local_write"),
        ),
        verification=("每个采纳意见都有测试覆盖", "counterexample 不回归", "ruff/pyright/release gate 通过"),
        risk_level="low",
        related_skills=("debugging-and-quality-workflows",),
    ),
    _playbook(
        playbook_id="pb_core_release_gate_pypi_readback",
        task_class="github_release_publish",
        title="GitHub：release 发布核验",
        trigger="GitHub release、tag、PyPI publish、CI 回读任务。",
        goal="发布动作前后都以远端事实为准，禁止用本地候选状态替代发布结果。",
        steps=(
            _step(1, "发布前读取 git diff/status、版本号、workflow、token/auth 状态。", "本地和远端状态输出"),
            _step(2, "发布只在 Joy 授权后执行；执行后回读 commit/tag/release/CI/PyPI。", "远端回读证据", capability_class="network_or_remote"),
            _step(3, "报告成功项、失败项、传播延迟和未验证边界。", "最终回执"),
        ),
        verification=("tag/release/CI/PyPI 均已回读", "失败或延迟明确标注", "无伪造发布结果"),
        risk_level="high",
        related_skills=("github-workflows",),
    ),
    _playbook(
        playbook_id="pb_core_vector_repair_safe_rebuild",
        task_class="vector_repair_safe_rebuild",
        title="scope-recall：vector 安全重建",
        trigger="Scope Recall vector index repair/rebuild、graph hygiene、SQLite truth 对齐任务。",
        goal="以 SQLite truth 为准安全重建 vector companion，避免 archived/hidden rows 回流。",
        steps=(
            _step(1, "只读比较 SQLite eligible memories、vector row count、stale ids、hidden lifecycle rows。", "doctor/vector/graph report 输出"),
            _step(2, "dry-run repair，确认将删除/重建的范围。", "dry-run plan"),
            _step(3, "备份后 apply repair，再回读 counts 和 stale ids。", "备份路径与回读输出", capability_class="local_write"),
        ),
        verification=("SQLite eligible count 与 vector count 对齐", "stale ids 清零或解释", "archived rows 不参与召回"),
        risk_level="high",
        related_skills=("debugging-and-quality-workflows",),
    ),
    _playbook(
        playbook_id="pb_core_candidate_memory_promotion_review",
        task_class="candidate_memory_promotion_review",
        title="scope-recall：候选记忆晋升",
        trigger="Scope Recall candidate memory promotion、memory quality lint、候选归档任务。",
        goal="自动审查 candidate memories，晋升可复用经验/事实，归档低价值或污染候选。",
        steps=(
            _step(1, "只读生成 candidate debt 与 memory quality lint 报告。", "candidate/lint report"),
            _step(2, "按 action=promote/archive/monitor 分组，先 dry-run。", "promotion plan"),
            _step(3, "apply 前后回读 promoted/archived/counts，保留治理 audit metadata。", "前后 counts、样本和 governance audit event", capability_class="local_write"),
        ),
        verification=("promoted rows 有证据锚点", "archived rows 不参与 profile/context", "governance audit 可回读", "doctor quality signal 可解释"),
        risk_level="high",
        related_skills=("debugging-and-quality-workflows",),
    ),
)


def _experience_schema_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'procedural_playbooks'").fetchone()
    return row is not None


def bootstrap_core_playbooks(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    shared_scope_id: str = "",
    accessible_scope_ids: Sequence[str] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    access = list(accessible_scope_ids or [scope_id, shared_scope_id])
    schema_exists = _experience_schema_exists(conn)
    result: dict[str, Any] = {"dry_run": bool(dry_run), "created": 0, "promoted": 0, "skipped_existing": 0, "items": []}
    if not schema_exists:
        result["schema_missing"] = True
    for item in CORE_PLAYBOOKS:
        playbook_id = str(item["id"])
        existing = inspect_playbook(conn, playbook_id=playbook_id, accessible_scope_ids=access) if schema_exists else {"found": False}
        if existing.get("found"):
            playbook = existing.get("playbook")
            status = playbook.get("status") if isinstance(playbook, dict) else ""
            result["skipped_existing"] += 1
            result["items"].append({"id": playbook_id, "action": "skip_existing", "status": status})
            continue
        if dry_run:
            result["items"].append({"id": playbook_id, "action": "would_create_promote", "title": item["payload"]["title"], "task_class": item["payload"]["task_class"]})
            continue
        create_playbook(
            conn,
            playbook_id=playbook_id,
            scope_id=scope_id,
            shared_scope_id=shared_scope_id,
            payload=item["payload"],
            status="candidate",
            confidence=float(item["payload"].get("confidence") or 0.82),
            evidence_anchors=[{"kind": "curated_bootstrap", "id": playbook_id}],
            related_skills=item.get("related_skills") if isinstance(item.get("related_skills"), list) else [],
            environment_constraints=item.get("environment_constraints") if isinstance(item.get("environment_constraints"), dict) else {},
            metadata=item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
        )
        reviewed = review_playbook(
            conn,
            playbook_id=playbook_id,
            accessible_scope_ids=access,
            action="promote",
            reason="curated core Experience bootstrap",
        )
        result["created"] += 1
        if reviewed.get("status") == "promoted":
            result["promoted"] += 1
        result["items"].append({"id": playbook_id, "action": "created_promoted", "status": reviewed.get("status")})
    return result
