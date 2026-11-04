"""Scenario assertions over real adapter outputs and independent stored evidence."""

from __future__ import annotations

import json
import math
import time
from dataclasses import replace
from pathlib import Path

from scenario_model import FixtureModel, drain, seed_claim
from scenario_mutations import correction, inspection, outages, privacy
from scenario_scale import scale
from scenario_store import HOSTS, contains, host_text, rendered_contains, request_context
from scope_recall._version import __version__
from scope_recall.core.recall_budget import estimate_tokens
from scope_recall.core.schema import SCHEMA_VERSION


def p95(values):
    """Nearest-rank p95, retaining the caller's actual sample count."""
    return sorted(values)[math.ceil(len(values) * 0.95) - 1]


def fresh(s, r, args):
    """Separate raw capture/search, fake-model flow and real installation evidence."""
    s.hook("codex", "TEST-azalea 配色是琥珀色。")
    packet = s.recall("codex", "TEST-azalea 配色")
    r.record("S1.raw", contains(packet, "琥珀色"), packet)
    evidence = drain(s, FixtureModel())
    read = s.recall("claude-code", "TEST-azalea 配色")
    rows = s.rows("SELECT state,payload_json FROM claim_versions")
    r.record(
        "S1.full",
        any(row["state"] == "active" for row in rows) and contains(read, "琥珀色"),
        dict(worker=evidence, claims=rows, read=read, scope="flow_only_real_model_quality_unverified"),
    )
    for key, text in (
        ("install", "干净机器公开一条安装命令"),
        ("host", "真实宿主子进程跨会话"),
        ("time", "真实安装非下载阶段墙钟 <= 600 秒"),
    ):
        r.machine("S1." + key, text)


def upgrade(s, r, args):
    """Consume freshly rerun historical-release compatibility evidence."""
    name = "compat-3.16.1" if __version__ == "3.16.1" else "compat-4.0"
    path = args.scratch / name / "matrix.json"
    matrix = json.loads(path.read_text(encoding="utf-8"))
    target = SCHEMA_VERSION if __version__ != "3.16.1" else None
    truth = all(
        "upgrade" in row
        and all(row["upgrade"]["truth_checks"].values())
        and (target is None or row["upgrade"]["to_schema"] == target)
        for row in matrix
    )
    r.record(
        "S2.truth",
        len(matrix) == 58 and truth,
        dict(
            path=str(path),
            count=len(matrix),
            failed=[row for row in matrix if row["status"] != "passed"],
            schemas=sorted({row["upgrade"]["to_schema"] for row in matrix if "upgrade" in row}),
        ),
    )
    r.record(
        "S2.query",
        all(
            row.get("upgrade", {}).get("query_equal") and row.get("upgrade", {}).get("snapshot_restore_equal")
            for row in matrix
        ),
        dict(
            path=str(path),
            query_equal=sum(row.get("upgrade", {}).get("query_equal", False) for row in matrix),
            snapshot_equal=sum(row.get("upgrade", {}).get("snapshot_restore_equal", False) for row in matrix),
        ),
    )
    r.machine("S2.restart", "58 个样例的真实宿主安装、重启及无技术命令的操作验收")


def cross_entry(s, r, args):
    """Measure next-read visibility separately from controlled worker scheduling."""
    s.hook("codex", "TEST-azalea 配色是琥珀色。")
    raw = {host: s.recall(host, "TEST-azalea 配色") for host in HOSTS}
    r.record("S3.raw", all(contains(value, "琥珀色") for value in raw.values()), raw)
    auto = {host: s.hook(host, "TEST-azalea 配色是什么？") for host in HOSTS}
    r.record("S3.auto", all("琥珀色" in host_text(row["output"]) for row in auto.values()), auto)
    model = FixtureModel()
    for index in range(5):
        s.hook("codex", f"TEST-promotion{index} 配色可能是琥珀色。")
    first = drain(s, FixtureModel(sufficient=False), rounds=2)
    s.clock.advance(1)
    for index in range(5):
        s.hook("codex", f"补充确认：TEST-promotion{index} 配色是琥珀色。")
    before_settle = drain(s, FixtureModel(sufficient=False), rounds=2)
    pending = s.rows("SELECT processing_state,reason FROM candidate_lifecycle")
    s.clock.advance(900)
    second = drain(s, model)
    states = s.rows(
        "SELECT c.subject,v.state FROM claims c JOIN claim_versions v "
        "ON c.claim_id=v.claim_id WHERE c.subject LIKE 'TEST-promotion%'"
    )
    active = {row["subject"] for row in states if row["state"] == "active"}
    r.record(
        "S3.promotion",
        len(active) == 5 and model.calls.count("evaluate_candidate") >= 5,
        dict(
            first=first,
            before_settle=before_settle,
            pending=pending,
            settled=second,
            states=states,
            controlled_observation_seconds=900,
            scope="flow_only_not_operational_p95",
        ),
    )
    s.hook("codex", "TEST-model-unavailable 等待提炼的合成消息。")
    unavailable = drain(s, None, rounds=1)
    s.hook("codex", "TEST-promotion-insufficient 配色可能是琥珀色。")
    insufficient = drain(s, FixtureModel(sufficient=False))
    candidates = s.rows("SELECT processing_state,reason FROM candidate_lifecycle")
    r.record(
        "S3.unavailable",
        "unavailable_work_types" in str(unavailable)
        and "consolidate" in str(unavailable)
        and any(row["processing_state"] == "waiting_evidence" for row in candidates)
        and all(
            row["processing_state"] in {"waiting_evidence", "blocked", "pending_evaluation", "resolved"}
            for row in candidates
        ),
        dict(unavailable=unavailable, insufficient=insufficient, states=candidates),
    )
    r.machine("S3.quality", "真实模型语义质量和正常调度墙钟 p95；业务钟样本不能替代")


def adoption(s, r, args):
    """Drive assistant capture, owner adoption and subsequent automatic injection."""
    session = s.hook("codex", "TEST 配置重启策略，请给我两个方案。")["session"]
    plan = "方案一：直接覆盖配置。\n\n方案二：先备份配置，再平滑重启，失败时恢复备份。"
    stopped = s.hook("codex", "", session=session, event="Stop", last_assistant_message=plan)
    confirmed = s.hook("codex", "好，就用方案二", session=session)
    worker = drain(s, FixtureModel(), rounds=12)
    rows = s.rows("SELECT state,payload_json FROM claim_versions")
    decisions = [row for row in rows if row["state"] == "active" and "先备份配置" in row["payload_json"]]
    evidence = dict(
        stop=stopped,
        confirmation=confirmed,
        worker=worker,
        claims=rows,
        scope="flow_only_real_model_quality_unverified",
    )
    r.record(
        "S4.adoption",
        bool(decisions) and all(len(json.loads(row["payload_json"])["evidence_spans"]) >= 2 for row in decisions),
        evidence,
    )
    start = s.hook("claude-code", "", event="SessionStart")
    read = s.hook("claude-code", "按上次定的方案来", session=start["session"])
    text = host_text(start["output"]) + host_text(read["output"])
    r.record(
        "S4.inject",
        "先备份配置" in text and "平滑重启" in text and "直接覆盖配置" not in text,
        dict(start=start, question=read),
    )
    r.machine("S4.quality", "真实模型方案定位、采纳解释质量")


def question_cases(s, host, prompts):
    """Measure question retrieval before briefing or prior delivery can hide distractors."""
    core = s.hooks[host].core
    original = core.config
    values = dict(session_brief_enabled=False, session_token_cap=None, injection_token_cap=12654)
    core.config = replace(original, **{key: value for key, value in values.items() if hasattr(original, key)})
    session, rows = str(__import__("uuid").uuid4()), []
    try:
        for prompt in prompts:
            row = s.hook(host, prompt, session=session)
            refs = row["diagnostics"].get("injection_refs")
            injected = bool(refs) if refs is not None else bool(host_text(row["output"]))
            rows.append(dict(host=host, prompt=prompt, question_injected=injected, **row))
    finally:
        core.config = original
    return rows


def unrelated(s, r, args):
    """Compare identical question-only fixtures while accounting for briefing separately."""
    seed_claim(s)
    for hook in s.hooks.values():
        if hasattr(hook.core.config, "session_token_cap"):
            hook.core.config = replace(hook.core.config, session_token_cap=1200, injection_token_cap=700)
    fixture = json.loads(Path(__file__).with_name("scenarios_fixture.json").read_text(encoding="utf-8"))
    for item in fixture["distractors"]:
        s.hook("codex", f"{item['subject']} {item['predicate']} {item['value']}.")
    drain(s, FixtureModel())
    cases, budget_cases, starts, usage, deliveries = [], [], [], [], []
    for host in HOSTS:
        rates = question_cases(s, host, fixture["unrelated_prompts"])
        cases.extend(rates)
        deliveries.extend(rates)
        session = str(__import__("uuid").uuid4())
        start = s.hook(host, "", session=session, event="SessionStart")
        starts.append(start)
        positive = s.hook(host, "TEST-azalea 配色是什么？", session=session)
        deliveries.extend((start, positive))
        used = sum(
            estimate_tokens(text) if text else 0 for text in (host_text(start["output"]), host_text(positive["output"]))
        )
        for prompt in fixture["unrelated_prompts"]:
            row = s.hook(host, prompt, session=session)
            diag = row["diagnostics"]
            # The explicit field names question retrieval. Brief text and
            # correction notices have independent delivery ledgers.
            question_refs = diag.get("injection_refs")
            injected = bool(question_refs) if question_refs is not None else bool(host_text(row["output"]))
            budget_cases.append(dict(host=host, prompt=prompt, question_injected=injected, **row))
            text = host_text(row["output"])
            used += estimate_tokens(text) if text else 0
            deliveries.append(row)
        usage.append(dict(host=host, delivered_tokens=used, diagnostics=budget_cases[-1]["diagnostics"]))
    false = sum(row["question_injected"] for row in cases)
    r.record(
        "S6.unrelated",
        false == 0,
        dict(
            cases=cases,
            false_injections=false,
            count=len(cases),
            starts=starts,
            budget_cases=budget_cases,
            distractors=fixture["distractors"],
            briefing_disabled_for_rate=True,
        ),
    )
    r.record(
        "S6.compare",
        True,
        dict(false_injections=false, count=len(cases), compare="computed_in_combined_report"),
        status="comparison",
    )
    r.record(
        "S6.turn",
        all(
            row["diagnostics"].get("injection_tokens", 0) <= row["diagnostics"].get("injection_token_cap", 12654)
            for row in deliveries
        )
        and all(
            (estimate_tokens(host_text(row["output"])) if host_text(row["output"]) else 0)
            <= (row["diagnostics"].get("injection_token_cap") or 12654)
            for row in deliveries
        ),
        deliveries,
    )
    caps = [row["diagnostics"].get("session_token_cap") for row in usage]
    r.record(
        "S6.session",
        all(
            cap is not None and row["diagnostics"].get("session_tokens", 0) <= cap and row["delivered_tokens"] <= cap
            for cap, row in zip(caps, usage, strict=True)
        ),
        usage,
    )
    r.record(
        "S6.latency",
        p95([row["ms"] for row in cases]) <= 1000 and p95([row["ms"] for row in starts]) <= 200,
        dict(
            prompt_p95_ms=p95([row["ms"] for row in cases]),
            start_p95_ms=p95([row["ms"] for row in starts]),
            prompt_n=len(cases),
            start_n=len(starts),
        ),
    )


def handoff(s, r, args):
    """Transfer one project-scoped task through the real MCP handoff surface."""
    if not hasattr(s.mcp["codex"], "handoff_write"):
        for key in ("write", "brief", "accept"):
            r.record("S11." + key, False, dict(missing="handoff tools unavailable"))
        return
    capture = s.hook("codex", "TEST-handoff 布局已经完成，下一步检查配色，等待主人选择尺寸。")
    source = s.source("TEST-handoff 布局已经完成，下一步检查配色，等待主人选择尺寸。")
    task = s.core.episodes(s.context(session=capture["session"]))[0]
    saved = s.mcp["codex"].handoff_write(
        request_context(capture["session"]),
        "1.1",
        dict(
            task_ref=task.ref,
            progress="TEST-handoff 布局已经完成",
            next_step="下一步检查配色",
            owner_decisions=["等待主人选择尺寸"],
            changed_files=["src/palette.py"],
            uncommitted_files=[],
            evidence_refs=[f"{source.ref}@1"],
        ),
    )
    item = saved["result"]
    r.record("S11.write", item["state"] == "pending" and not s.rows("SELECT * FROM claims"), saved)
    brief = s.hook("claude-code", "", event="SessionStart")
    text = host_text(brief["output"])
    r.record(
        "S11.brief",
        all(rendered_contains(text, value) for value in ("布局已经完成", "下一步检查配色", "等待主人选择尺寸")),
        brief,
    )
    accepted = s.mcp["claude-code"].handoff_accept(request_context(), "1.1", item["ref"], item["revision"])
    third = s.hook("workbuddy", "", event="SessionStart")
    listed = s.mcp["dsh"].handoff_list(request_context(), "1.1")
    r.record(
        "S11.accept",
        accepted["result"]["state"] == "accepted"
        and not contains(listed, item["ref"])
        and item["ref"] not in host_text(third["output"]),
        dict(accepted=accepted, third=third, listed=listed),
    )


CHECKS = {
    "S1": fresh,
    "S2": upgrade,
    "S3": cross_entry,
    "S4": adoption,
    "S5": correction,
    "S6": unrelated,
    "S7": inspection,
    "S8": privacy,
    "S9": scale,
    "S10": outages,
    "S11": handoff,
}
