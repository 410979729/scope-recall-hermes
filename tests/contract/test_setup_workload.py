"""Measure a hundred realistic synthetic turns through the production worker and metered transport."""

from __future__ import annotations

import copy
import json
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from scope_recall.core.language_resources import prompt_text
from scope_recall.core.recall_budget import estimate_tokens
from scope_recall.maintenance.setup_configuration import runtime_payload
from scope_recall.maintenance.setup_workload import recommended_workload
from scope_recall.runtime import instance
from scope_recall.runtime.auxiliary import build_auxiliary_runtime
from scope_recall.runtime.instance import build_runtime_instance
from scope_recall.runtime.instance_config import RuntimeInstanceConfig

from tests.contract.test_runtime_auxiliary import FakeTransport
from tests.v11_support import context, recall_request, source_event


def summarize_requests(records):
    """Keep request estimates separate from ledger reservations."""
    counts = defaultdict(lambda: dict(calls=0, input_tokens=0, output_tokens=0))
    for record in records:
        row = counts[record["task"]]
        row["calls"] += 1
        row["input_tokens"] += record["input_tokens"]
        row["output_tokens"] += record["output_tokens"]
    return counts


def test_hundred_turn_worker_cost_matches_setup_catalog(tmp_path, monkeypatch, record_property):
    ctx, records = context(tmp_path / "TEST-cost"), []
    corpus = json.loads((Path(__file__).parents[2] / "prompts/entity.examples.json").read_text("utf-8"))[:5]
    proposals = [
        copy.deepcopy(p)
        for case in corpus
        for p in [*(item["payload"] for item in case["input"]["candidates"]), case["input"]["proposal"]]
    ]

    def response(**kwargs):
        body = json.loads(kwargs["body"])
        if "input" in body:
            texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
            inputs = sum(estimate_tokens(text) for text in texts)
            stage, answer = "embedding", ""
            data = dict(
                data=[dict(index=i, embedding=[0.01] * 64) for i in range(len(texts))],
                usage=dict(prompt_tokens=inputs, total_tokens=inputs),
            )
        else:
            messages = body["messages"]
            inputs = sum(estimate_tokens(message["content"]) for message in messages)
            payload = json.loads(messages[-1]["content"])
            if payload.get("task") == "locate_adoption":
                stage, answer = "adoption", json.dumps(dict(adopted=False))
            elif "proposal" in payload:
                stage = "entity_resolution" if messages[0]["content"] == prompt_text("entity") else "extraction"
                answer = json.dumps(dict(support="supported", action="new", target_ref=None, target_revision=None))
            else:
                stage, found = "extraction", []
                for source in payload["sources"]:
                    for proposal in proposals:
                        span = proposal["evidence_spans"][0]
                        if source["content"].startswith(span["quote"]):
                            item = copy.deepcopy(proposal)
                            item["evidence_spans"][0].update(
                                source_ref=source["source_ref"], source_revision=source["source_revision"]
                            )
                            found.append(item)
                            break
                answer = json.dumps(
                    dict(
                        protocol_version="1.1",
                        source_refs=payload["source_refs"],
                        claim_proposals=found,
                        resume_proposals=[],
                        reference_proposals=[],
                    )
                )
            data = dict(
                choices=[dict(message=dict(role="assistant", content=answer), finish_reason="stop")],
                usage=dict(prompt_tokens=inputs, completion_tokens=estimate_tokens(answer)),
            )
        records.append(dict(task=stage, input_tokens=inputs, output_tokens=estimate_tokens(answer) if answer else 0))
        return 200, json.dumps(data).encode()

    transport = FakeTransport(response)
    monkeypatch.setattr(
        instance, "build_auxiliary_runtime", lambda config: build_auxiliary_runtime(config, transport=transport)
    )
    monkeypatch.setenv("TEST_COST_CREDENTIAL", "synthetic-placeholder")
    models = dict(
        auxiliary=dict(
            external_embedding=True,
            external_consolidation=True,
            budget=dict(
                approved_models=["TEST-chat", "TEST-embed"],
                pricing={
                    name: dict(input_usd_per_million="0", output_usd_per_million="0")
                    for name in ("TEST-chat", "TEST-embed")
                },
                total_call_cap=10000,
                total_input_cap=100000000,
                total_output_cap=10000000,
                cap_micro_usd=1000000,
            ),
            embedding=dict(
                model="TEST-embed",
                endpoint="https://example.test/v1/embeddings",
                dimensions=64,
                dialect="openai",
                credential_env="TEST_COST_CREDENTIAL",
            ),
            consolidation=dict(
                model="TEST-chat",
                endpoint="https://example.test/v1/chat/completions",
                credential_env="TEST_COST_CREDENTIAL",
                output_limit_field="max_tokens",
                max_output_tokens=4096,
            ),
        ),
        vector=dict(
            backend="sqlite-bruteforce",
            storage_dir=str(tmp_path),
            table_name="TEST-cost",
            dimensions=64,
            test_injection_override=True,
        ),
        vector_threshold=0.7,
    )
    config = RuntimeInstanceConfig.from_mapping(runtime_payload(ctx.binding, models=models, worker="manual"))
    from scope_recall.runtime.model_budget import initialize_auxiliary_budget_ledger

    initialize_auxiliary_budget_ledger(config.auxiliary.ledger_path, config.auxiliary.budget)
    runtime = build_runtime_instance(config)
    lengths = []
    try:
        runtime.core.initialize()
        for turn in range(100):
            content = (
                proposals[turn]["evidence_spans"][0]["quote"]
                if turn < len(proposals)
                else f"For synthetic checkpoint {turn}, explain the tradeoffs of SQLite transactions and queue retries."
            )
            content += " Keep source evidence, error recovery, and resource limits explicit. " * (1 + turn % 4)
            lines = 28 + (turn % 5) * 60
            assistant = (
                "Implementation details and validation steps:\n```python\n"
                + "    result = process_record(record, retry_limit=3)  # retain evidence\n" * lines
                + "```"
            )
            lengths.append(estimate_tokens(assistant))
            for role, text in (("user", content), ("assistant", assistant)):
                origin = "human_direct" if role == "user" else "assistant_visible"
                runtime.core.record_event(
                    replace(ctx, actor_origin=origin),
                    source_event(source_event_key=f"TEST-cost/{turn}/{role}", role=role, content=text, origin=origin),
                    scope_id="TEST-scope",
                )
            receipt = runtime.drain(max_items=1000, remaining_seconds=120)
            assert receipt.failed == 0 and not any(item.error_code for item in receipt.items), [
                (item.work_type, item.error_code) for item in receipt.items
            ]
            runtime.recall(
                recall_request(
                    mode="current",
                    request_id=f"TEST-query-{turn}",
                    query=f"What does the synthetic checkpoint {turn} say about source evidence?",
                )
            )
        record_property(
            "worker_diagnostics",
            json.dumps(
                dict(
                    receipt=str(receipt),
                    gaps=runtime.auxiliary.capability_gaps,
                    config_budget=str(config.auxiliary.budget),
                )
            ),
        )
        counts = summarize_requests(records)
        record_property(
            "workload_measurement",
            json.dumps(dict(turns=100, assistant_token_lengths=lengths, tasks=counts, requests=records)),
        )
        for row in recommended_workload():
            if row["task"] in counts and row["calls"] is not None:
                assert counts[row["task"]] == {name: row[name] for name in ("calls", "input_tokens", "output_tokens")}
        assert counts["entity_resolution"]["calls"] > 0 and counts["adoption"]["calls"] > 0
    finally:
        runtime.close()
