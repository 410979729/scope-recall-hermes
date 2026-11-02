"""Realistic-length synthetic conversations measured through the actual worker queue."""

import json
from collections import Counter
from datetime import datetime, timedelta

import pytest
from scope_recall.core.recall_budget import estimate_tokens
from scope_recall.core.worker_consolidation import build_consolidation_model

from tests.contract.test_adoption import assistant, selected
from tests.contract.test_claims import app, capture, draft


@pytest.mark.parametrize("with_adoptions", [False, True], ids=["no-adoptions", "mixed"])
def test_hundred_turn_cost_and_ordinary_decision_survival(app, with_adoptions, record_property):
    core, ctx = app
    records, lengths = [], []
    counts = Counter()
    choices = {7, 19, 38, 57, 76, 95} if with_adoptions else set()
    ordinary = {10, 30, 50, 70, 90}
    targets = [400, 700, 1000, 1500, 2000, 2500, 3500, 5000, 700, 1200]

    class Port:
        def propose(self, messages, **kwargs):
            body = json.loads(messages[-1]["content"])
            task = body.get("task", "support" if "proposal" in body else "ordinary")
            if task == "ordinary":
                proposals = [draft(owner, value="PostgreSQL", predicate=f"数据库{turn}")] if turn in ordinary else []
                response = dict(
                    protocol_version="1.1",
                    source_refs=body["source_refs"],
                    claim_proposals=proposals,
                    resume_proposals=[],
                    reference_proposals=[],
                )
            elif task == "locate_adoption":
                counts[(turn, task)] += 1
                if turn in {31, 62} and counts[(turn, task)] == 1:
                    raw = "invalid json"
                    records.append(
                        dict(
                            turn=turn,
                            task=task,
                            retry=False,
                            input_tokens=sum(estimate_tokens(m["content"]) for m in messages),
                            output_tokens=estimate_tokens(raw),
                        )
                    )
                    return raw
                response = (
                    (
                        selected()
                        | dict(
                            fragment_ids=[1],
                            reference_label=f"任务{turn}方案一",
                            confirmation_quote=owner.event["content"],
                        )
                    )
                    if turn in choices
                    else dict(adopted=False)
                )
            elif task == "describe_adoption":
                response = dict(topic=f"任务{turn}备份策略", decision="先备份配置")
            else:
                response = dict(support="supported", action="new", target_ref=None, target_revision=None)
            raw = json.dumps(response, ensure_ascii=False)
            records.append(
                dict(
                    turn=turn,
                    task=task,
                    retry=counts[(turn, task)] > 1,
                    input_tokens=sum(estimate_tokens(m["content"]) for m in messages),
                    output_tokens=estimate_tokens(raw),
                )
            )
            return raw

    for turn in range(100):
        target = targets[turn % len(targets)]
        code = "    config['workers'] = max(1, requested_workers)  # preserve worker capacity\n"
        text = f"任务{turn}方案一：先备份配置。\n\n任务{turn}方案二：分批验证配置。\n\n"
        text += "- 读取配置并检查路径\n- 对比进程状态和恢复记录\n\n```python\n"
        text += code * max(1, (target * 4 - len(text)) // len(code)) + "```"
        lengths.append(estimate_tokens(text))
        assistant(core, ctx, text)
        content = (
            f"采用任务{turn}方案一"
            if turn in choices
            else f"TEST-project 数据库{turn} PostgreSQL。"
            if turn in ordinary
            else f"第{turn}轮继续"
            if turn % 3
            else f"第{turn}轮这个错误的原因是什么？"
        )
        owner = capture(core, ctx, content)
        model = build_consolidation_model(Port())
        core.drain_worker(ctx, consolidation=model, max_items=12, remaining_seconds=30)
        if turn in {31, 62}:
            core.clock.now = (
                (datetime.fromisoformat(core.clock.utc_now().replace("Z", "+00:00")) + timedelta(hours=2))
                .isoformat()
                .replace("+00:00", "Z")
            )
            core.drain_worker(ctx, consolidation=model, max_items=12, remaining_seconds=30)
    with core.storage.read(ctx) as tx:
        review = (
            tx._check().execute("SELECT count(*) FROM adoption_relations WHERE state='pending_review'").fetchone()[0]
        )
        bound = tx._check().execute("SELECT count(*) FROM adoption_relations WHERE state='bound'").fetchone()[0]
        kept = (
            tx._check()
            .execute(
                "SELECT count(*) FROM claims c JOIN claim_versions v ON c.claim_id=v.claim_id "
                "AND c.current_revision=v.revision WHERE c.predicate LIKE '数据库%' AND v.state='active'"
            )
            .fetchone()[0]
        )
    extra = [r for r in records if r["task"] != "ordinary"]
    result = dict(
        turns=100,
        adoptions=len(choices),
        assistant_token_estimates=lengths,
        ordinary_requests=sum(r["task"] == "ordinary" for r in records),
        added_requests=len(extra),
        added_input_tokens=sum(r["input_tokens"] for r in extra),
        added_output_tokens=sum(r["output_tokens"] for r in extra),
        retries=sum(r["retry"] for r in extra),
        stage_requests=dict(Counter(r["task"] for r in extra)),
        pending_review=review,
        bound=bound,
        ordinary_decisions=len(ordinary),
        wrongly_removed=len(ordinary) - kept,
        token_method="core.recall_budget.estimate_tokens; estimates, not server tokenizer",
        requests=records,
    )
    record_property("cost", json.dumps(result, ensure_ascii=False))
    assert review == 0
    assert bound == len(choices)
    assert kept == len(ordinary)
    assert result["retries"] == 2
    assert len(extra) == 102 + 2 * len(choices)
