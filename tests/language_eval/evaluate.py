"""Evaluate synthetic extraction, support, promotion latency and prompt ablation."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from scope_recall.contracts import TrustedSourcePrincipal  # noqa: E402
from scope_recall.core import CoreConfig, MemoryCore  # noqa: E402
from scope_recall.core.worker import WorkerConfig, drain_worker  # noqa: E402
from scope_recall.core.worker_consolidation import build_consolidation_model  # noqa: E402
from v11_support import context, source_event  # noqa: E402


class Clock:
    """Advance synthetic observation windows while counting real request and publication time."""

    def __init__(self):
        self.now = datetime(2026, 9, 6, 12, tzinfo=UTC)
        self.started = time.monotonic()

    def utc_now(self):
        return (self.now + timedelta(seconds=time.monotonic() - self.started)).isoformat().replace("+00:00", "Z")

    def monotonic(self):
        return time.monotonic()


class RecordingPort:
    """Replay fixed synthetic model answers with current source identifiers."""

    def __init__(self, recording):
        self.recording = recording
        self.calls = 0

    def propose(self, messages, **kwargs):
        self.calls += 1
        bodies = [json.loads(m["content"]) for m in messages if m["role"] == "user" and m["content"].startswith("{")]
        body = next(b for b in reversed(bodies) if "sources" in b)
        if "proposal" in body:
            return json.dumps(
                dict(support=self.recording["support"], action="new", target_ref=None, target_revision=None)
            )
        sources = body["sources"]
        claims = []
        for source in sources:
            claim = {key: value for key, value in self.recording.items() if key != "support"}
            claim.update(
                valid_from=source["occurred_at"],
                valid_to=None,
                evidence_spans=[
                    dict(
                        source_ref=source["source_ref"],
                        source_revision=source["source_revision"],
                        quote=source["content"],
                        start=0,
                        end=len(source["content"]),
                    )
                ],
            )
            claims.append(claim)
        return json.dumps(
            dict(
                protocol_version="1.1",
                source_refs=body["source_refs"],
                claim_proposals=claims,
                resume_proposals=[],
                reference_proposals=[],
            )
        )


class PromptPort:
    """Remove one exact extraction clause without changing the model route or source payload."""

    def __init__(self, wrapped, clause):
        self.wrapped, self.clause = wrapped, clause
        self.removed = 0

    def propose(self, messages, **kwargs):
        copied = [dict(message) for message in messages]
        if self.clause:
            for message in copied:
                if message["role"] == "system":
                    pattern = re.escape(self.clause).replace(re.escape("{feedback}"), r"[\s\S]*?")
                    message["content"], removed = re.subn(pattern, "", message["content"], count=1)
                    self.removed += removed
        return self.wrapped.propose(copied, **kwargs)


def prompt_clauses(name):
    """Read removable prose clauses without treating the JSON schema as a prompt clause."""
    from scope_recall.core.language_resources import prompt_text

    text = prompt_text(name)
    pattern = r"[^。；]+[。；]|[^。；]+$" if "。" in text else r".+?(?:\.(?:\s+|$)|$)"
    return [part for part in re.findall(pattern, text, flags=re.S) if part.strip() and "{schema}" not in part]


def live_port(config, approved, output):
    """Build the shipped metered chat adapter from an explicit route file; embeddings are never constructed."""
    from scope_recall.runtime.consolidation_models import ConsolidationRouteConfig, OpenAIConsolidationAdapter
    from scope_recall.runtime.model_budget import (
        AuxiliaryBudgetLedger,
        ModelPricing,
        default_budget_policy,
        initialize_auxiliary_budget_ledger,
    )

    route = config["chat"]
    if route["endpoint"] not in approved or not config.get("weights"):
        raise ValueError("an exact approved endpoint and frozen model weights are required")
    model = route["model"]
    pricing = config["pricing"][model]
    policy = replace(
        default_budget_policy(batch="language-evaluation"),
        cap_micro_usd=None,
        total_input_cap=None,
        total_output_cap=None,
        total_call_cap=None,
        approved_models=frozenset({model}),
        max_request_bytes=1024 * 1024,
        pricing={model: ModelPricing(pricing["input_usd_per_million"], pricing["output_usd_per_million"])},
    )
    ledger_path = output / "model-ledger.sqlite3"
    initialize_auxiliary_budget_ledger(ledger_path, policy)
    return OpenAIConsolidationAdapter(
        ConsolidationRouteConfig(**route), ledger=AuxiliaryBudgetLedger(ledger_path, policy), reserve_input=100000
    )


def run_case(case, directory, port):
    """Capture, extract and evaluate through the production worker in an independent synthetic store."""
    clock = Clock()
    ctx = context(directory)
    ctx = replace(ctx, source_principal=TrustedSourcePrincipal("human", "verified", "principal:TEST-language-owner"))
    core = MemoryCore(CoreConfig(ctx.binding), clock=clock)
    core.initialize()
    origin = case["origin"]
    if origin != "human_direct":
        ctx = replace(
            ctx,
            actor_origin=origin,
            source_principal=TrustedSourcePrincipal("assistant", "verified", "principal:TEST-language-assistant"),
        )
    at = clock.utc_now()
    event = source_event(
        source_event_key="TEST-language/" + case["id"],
        content=case["text"],
        origin=origin,
        role="user" if origin == "human_direct" else "assistant",
        occurred_at=at,
        recorded_at=at,
    )
    core.record_event(ctx, event, scope_id="TEST-scope", remaining_seconds=20)
    model = build_consolidation_model(port)
    receipts = []
    for seconds in (0, 900, 1200):
        clock.now = datetime(2026, 9, 6, 12, tzinfo=UTC) + timedelta(seconds=seconds)
        receipt = drain_worker(
            core.storage,
            clock,
            replace(ctx, actor_origin="host_generated"),
            config=WorkerConfig("TEST-language", max_items=8),
            consolidation=model,
            remaining_seconds=180,
        )
        receipts.extend(
            dict(type=item.work_type, disposition=item.disposition, error=item.error_code) for item in receipt.items
        )
    with core.storage.read(ctx) as tx:
        rows = tx._check().execute("SELECT state,qualification_reason,recorded_from FROM claim_versions").fetchall()
        states = [dict(state=r[0], reason=r[1], at=r[2]) for r in rows]
    active = [r for r in states if r["state"] == "active"]
    delays = [
        (
            datetime.fromisoformat(r["at"].replace("Z", "+00:00")) - datetime.fromisoformat(at.replace("Z", "+00:00"))
        ).total_seconds()
        for r in active
    ]
    return dict(
        id=case["id"],
        gold_active=case["gold_active"],
        active=bool(active),
        promoted_within_1200=bool(delays) and min(delays) <= 1200,
        delay_seconds=min(delays) if delays else None,
        validation_failed=any(
            r["error"] in {"derivation_invalid", "DERIVATION_INVALID", "INPUT_INVALID"} for r in receipts
        ),
        states=states,
        receipts=receipts,
    )


def summarize(rows):
    """Count validation failures, false acceptance, timely promotion and every candidate reason."""
    positives = [r for r in rows if r["gold_active"]]
    negatives = [r for r in rows if not r["gold_active"]]
    return dict(
        cases=len(rows),
        validation_failure_rate=sum(r["validation_failed"] for r in rows) / len(rows),
        false_acceptance_rate=sum(r["active"] for r in negatives) / max(1, len(negatives)),
        owner_promotion_rate=sum(r["promoted_within_1200"] for r in positives) / max(1, len(positives)),
        candidate_reasons=dict(Counter(s["reason"] for r in rows for s in r["states"] if s["state"] == "proposed")),
    )


def main():
    """Live mode requires explicit endpoints; fake mode exercises formatting and worker publication only."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--allow-model-calls", action="store_true")
    parser.add_argument("--approved-endpoint", action="append", default=[])
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ablate", action="store_true")
    parser.add_argument(
        "--prompt",
        choices=("consolidation", "candidate", "fragment", "repair", "support", "merge"),
        default="consolidation",
    )
    parser.add_argument("--ablation-limit", type=int)
    args = parser.parse_args()
    if args.fake and args.config:
        parser.error("fake mode accepts no live route")
    if not args.fake and (not args.allow_model_calls or args.config is None):
        parser.error("live evaluation requires --config and --allow-model-calls")
    args.output.mkdir(parents=True, exist_ok=False)
    corpus = ROOT / "fixtures/language/extraction_cases.json"
    cases = json.loads(corpus.read_text(encoding="utf-8"))
    if args.limit:
        cases = cases[: args.limit]
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else None
    base_port = live_port(config, args.approved_endpoint, args.output) if config else None
    clauses = [None]
    if args.ablate:
        choices = prompt_clauses(args.prompt)
        clauses.extend(choices[: args.ablation_limit] if args.ablation_limit else choices)
    variants = []
    for index, clause in enumerate(clauses):
        rows = []
        removed_requests = 0
        for case in cases:
            port = PromptPort(base_port or RecordingPort(case["recording"]), clause)
            rows.append(run_case(case, args.output / f"variant-{index}" / case["id"], port))
            removed_requests += port.removed
        variants.append(
            dict(clause=clause, rows=rows, metrics=summarize(rows), requests_with_clause_removed=removed_requests)
        )
    report = dict(
        mode="fake" if args.fake else "live",
        ablated_prompt=args.prompt,
        corpus_sha256=hashlib.sha256(corpus.read_bytes()).hexdigest(),
        model_config_sha256=hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
        prompt_sha256={
            name: hashlib.sha256((ROOT / f"prompts/{name}.txt").read_bytes()).hexdigest()
            for name in ("consolidation", "candidate", "fragment", "repair", "support", "merge")
        },
        variants=variants,
        quality_claim="workflow only" if args.fake else "synthetic corpus only",
    )
    if args.baseline:
        before = json.loads(args.baseline.read_text(encoding="utf-8"))
        if before["corpus_sha256"] != report["corpus_sha256"]:
            raise ValueError("baseline corpus differs")
        if before["mode"] != report["mode"] or before["model_config_sha256"] != report["model_config_sha256"]:
            raise ValueError("baseline model conditions differ")
        old, new = before["variants"][0]["metrics"], variants[0]["metrics"]
        report["acceptance"] = dict(
            validation_improves=new["validation_failure_rate"] < old["validation_failure_rate"],
            false_acceptance_not_worse=new["false_acceptance_rate"] <= old["false_acceptance_rate"],
            owner_promotion_at_least_90_percent=new["owner_promotion_rate"] >= 0.9,
        )
    (args.output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"mode": report["mode"], "metrics": variants[0]["metrics"], "variants": len(variants)}))
    return int("acceptance" in report and not all(report["acceptance"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
