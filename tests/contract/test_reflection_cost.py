"""Meter a hundred synthetic turns through idle reflection and its real support protocol."""

import json
from collections import Counter
from dataclasses import replace

import pytest
from scope_recall.core.recall_budget import estimate_tokens
from test_claims import capture
from test_importance_summaries import app
from test_reflection import ReflectionModel, run


@pytest.mark.parametrize("enabled", [False, True])
def test_one_hundred_turn_reflection_cost(app, enabled, record_property):
    core, ctx = app
    records = []

    class Meter(ReflectionModel):
        def judge(self, messages, *, remaining_seconds):
            raw = super().judge(messages, remaining_seconds=remaining_seconds)
            task = json.loads(messages[-1]["content"]).get("task", "support")
            records.append(
                dict(
                    task=task,
                    input_tokens=sum(estimate_tokens(m["content"]) for m in messages),
                    output_tokens=estimate_tokens(raw),
                )
            )
            return raw

    model = Meter()
    for turn in range(100):
        session = replace(ctx, session_id=f"TEST-cost-session-{turn // 10}")
        capture(core, session, "TEST-project encounters connection timeouts.")
        with core.storage.write(ctx) as tx:
            tx._check().execute("UPDATE work_items SET state='done' WHERE work_type!='rebuild_projection'")
        run(core, ctx, model, enabled)
    value = dict(
        turns=100,
        enabled=enabled,
        calls=len(records),
        stages=dict(Counter(r["task"] for r in records)),
        input_tokens=sum(r["input_tokens"] for r in records),
        output_tokens=sum(r["output_tokens"] for r in records),
        token_method="core.recall_budget.estimate_tokens; estimated, not server usage",
    )
    record_property("cost", json.dumps(value))
    assert len(records) <= 36
    if not enabled:
        assert not records
    else:
        assert records and {r["task"] for r in records} == {"reflect", "support"}
