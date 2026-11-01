"""Evaluate synthetic extraction, support, promotion latency and prompt ablation."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from scope_recall.contracts import TrustedSourcePrincipal  # noqa: E402
from scope_recall.core import CoreConfig, MemoryCore  # noqa: E402
from scope_recall.core.worker import WorkerConfig, drain_worker  # noqa: E402
from scope_recall.core.worker_consolidation import build_consolidation_model  # noqa: E402
from subject import ChatPort, capabilities, digest, one_system, operation, package_identity  # noqa: E402
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
        self.positions = capabilities()["evidence_positions"] == "applicable"

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
            if not self.positions:
                for span in claim["evidence_spans"]:
                    span.pop("start")
                    span.pop("end")
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

    def __init__(self, wrapped, clause, *, max_calls=64):
        self.wrapped, self.clause = wrapped, clause
        self.removed = 0
        self.max_calls = max_calls
        self.calls = Counter()
        self.prompts = {}
        self.errors = []

    def propose(self, messages, **kwargs):
        copied = [dict(message) for message in messages]
        if self.clause:
            for message in copied:
                if message["role"] == "system":
                    pattern = re.escape(self.clause).replace(re.escape("{feedback}"), r"[\s\S]*?")
                    message["content"], removed = re.subn(pattern, "", message["content"], count=1)
                    self.removed += removed
        from scope_recall.runtime.models import AuxiliaryModelError

        if sum(self.calls.values()) >= self.max_calls:
            self.errors.append("budget_exhausted")
            raise AuxiliaryModelError("budget_exhausted")
        copied = one_system(copied)
        stage = operation(copied)
        self.calls[stage] += 1
        self.prompts.setdefault(stage, set()).add(hashlib.sha256(copied[0]["content"].encode()).hexdigest())
        try:
            return self.wrapped.propose(copied, **kwargs)
        except Exception as exc:
            self.errors.append(getattr(exc, "error_type", type(exc).__name__))
            raise


def prompt_clauses(name):
    """Read removable prose clauses without treating the JSON schema as a prompt clause."""
    from scope_recall.core.language_resources import prompt_text

    text = prompt_text(name)
    pattern = r"[^。；]+[。；]|[^。；]+$" if "。" in text else r".+?(?:\.(?:\s+|$)|$)"
    return [part for part in re.findall(pattern, text, flags=re.S) if part.strip() and "{schema}" not in part]


def live_port(config, approved, output):
    """Build an approved chat-only evaluation port; embeddings are never constructed."""
    return ChatPort(config, approved)


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
    with core.storage.read(ctx, remaining_seconds=20) as tx:
        rows = (
            tx._check()
            .execute(
                "SELECT v.state,v.qualification_reason,v.recorded_from,v.revision,c.current_revision "
                "FROM claim_versions v JOIN claims c ON c.claim_id=v.claim_id"
            )
            .fetchall()
        )
        states = [dict(state=r[0], reason=r[1], at=r[2], current=r[3] == r[4]) for r in rows]
    candidates = [r for r in states if r["current"] and r["state"] in {"proposed", "disputed"}]
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
        candidate_count=len(candidates),
        candidates_with_reasons=sum(isinstance(r["reason"], str) and bool(r["reason"].strip()) for r in candidates),
        model_calls=dict(port.calls),
        prompt_sha256={stage: sorted(values) for stage, values in port.prompts.items()},
        operational_errors=sorted(set(port.errors)),
    )


def summarize(rows):
    """Count validation failures, false acceptance, timely promotion and every candidate reason."""
    positives = [r for r in rows if r["gold_active"]]
    negatives = [r for r in rows if not r["gold_active"]]
    candidates = sum(r.get("candidate_count", 0) for r in rows)
    reasoned = sum(r.get("candidates_with_reasons", 0) for r in rows)
    return dict(
        cases=len(rows),
        validation_failure_rate=sum(r["validation_failed"] for r in rows) / len(rows),
        false_acceptance_rate=sum(r["active"] for r in negatives) / max(1, len(negatives)),
        owner_promotion_rate=sum(r["promoted_within_1200"] for r in positives) / max(1, len(positives)),
        candidate_reasons=dict(Counter(s["reason"] for r in rows for s in r["states"] if s["state"] == "proposed")),
        candidate_count=candidates,
        candidates_with_reasons=reasoned,
        candidate_reason_coverage=reasoned / candidates if candidates else 1.0,
    )


def files_for_prompts():
    """Fingerprint installed prompt definitions, including inline formatters and their schema."""
    from importlib.resources import files

    package = files("scope_recall")
    names = ["core/consolidate.py", "contracts/consolidation_result.schema.json"]
    names += [
        f"prompts/{name}.txt" for name in ("consolidation", "candidate", "fragment", "repair", "support", "merge")
    ]
    return {
        name: hashlib.sha256(package.joinpath(name).read_bytes()).hexdigest()
        for name in names
        if package.joinpath(name).is_file()
    }


def arguments():
    """Validate common execution settings before creating an output directory or model port."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--allow-model-calls", action="store_true")
    parser.add_argument("--approved-endpoint", action="append", default=[])
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--max-calls-per-case", type=int, default=64)
    parser.add_argument("--expected-version")
    parser.add_argument("--ablate", action="store_true")
    parser.add_argument(
        "--prompt",
        choices=("consolidation", "candidate", "fragment", "repair", "support", "merge"),
        default="consolidation",
    )
    parser.add_argument("--ablation-limit", type=int)
    args = parser.parse_args()
    if not args.fake and (not args.allow_model_calls or args.config is None):
        parser.error("live evaluation requires --config and --allow-model-calls")
    if args.concurrency < 1 or args.max_calls_per_case < 1 or (args.limit is not None and args.limit < 1):
        parser.error("concurrency, call limit and case limit must be positive")
    return args


def main():
    """Live mode requires explicit endpoints; fake mode exercises formatting and worker publication only."""
    args = arguments()
    if args.fake:

        def offline(event, _args):
            if event in {"socket.connect", "socket.getaddrinfo"}:
                raise RuntimeError("fake evaluation forbids network access")

        sys.addaudithook(offline)
    subject = package_identity()
    if args.expected_version and subject["version"] != args.expected_version:
        raise SystemExit("the installed subject does not match --expected-version")
    args.output.mkdir(parents=True, exist_ok=False)
    corpus = ROOT / "fixtures/language/extraction_cases.json"
    cases = json.loads(corpus.read_text(encoding="utf-8"))
    if args.limit:
        cases = cases[: args.limit]
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else None
    base_port = live_port(config, args.approved_endpoint, args.output) if not args.fake else None
    clauses = [None]
    if args.ablate:
        choices = prompt_clauses(args.prompt)
        clauses.extend(choices[: args.ablation_limit] if args.ablation_limit else choices)
    variants = []
    for index, clause in enumerate(clauses):

        def evaluate_case(case, clause=clause, index=index):
            port = PromptPort(base_port or RecordingPort(case["recording"]), clause, max_calls=args.max_calls_per_case)
            row = run_case(case, args.output / f"variant-{index}" / case["id"], port)
            row["requests_with_clause_removed"] = port.removed
            return row

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            rows = list(pool.map(evaluate_case, cases))
        variants.append(
            dict(
                clause=clause,
                rows=rows,
                metrics=summarize(rows),
                requests_with_clause_removed=sum(r["requests_with_clause_removed"] for r in rows),
            )
        )
    protocol = dict(
        observation_seconds=[0, 900, 1200],
        worker_items_per_pass=8,
        pass_budget_seconds=180,
        max_calls_per_case=args.max_calls_per_case,
        concurrency=args.concurrency,
        judgement="gold_active, any_active, first_active_within_1200, final_candidate_reasons",
    )
    prompts = {}
    for variant in variants:
        for row in variant["rows"]:
            for stage, values in row["prompt_sha256"].items():
                prompts.setdefault(stage, set()).update(values)
    definitions = files_for_prompts()
    if base_port is not None:
        base_port.close()
    report = dict(
        schema="language-evaluation/2",
        mode="fake" if args.fake else "live",
        subject=subject,
        protocol=protocol,
        protocol_sha256=digest(protocol),
        evaluator_sha256=digest(
            {
                name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                for name in ("evaluate.py", "subject.py", "acceptance.py")
            }
        ),
        case_ids_sha256=digest([case["id"] for case in cases]),
        prompt_definitions_sha256=definitions,
        ablated_prompt=args.prompt,
        corpus_sha256=hashlib.sha256(corpus.read_bytes()).hexdigest(),
        model_config_sha256=hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
        prompt_sha256={stage: sorted(values) for stage, values in prompts.items()},
        model_config_file_sha256=hashlib.sha256(args.config.read_bytes()).hexdigest() if args.config else None,
        model_calls=base_port.requests
        if base_port
        else sum(sum(r["model_calls"].values()) for v in variants for r in v["rows"]),
        network_requests=base_port.requests if base_port else 0,
        calls_by_stage=dict(sum((Counter(r["model_calls"]) for v in variants for r in v["rows"]), Counter())),
        embedding_items=0,
        usage=base_port.usage if base_port else None,
        complete=all(not r["operational_errors"] for v in variants for r in v["rows"]),
        variants=variants,
        quality_claim="workflow only" if args.fake else "synthetic corpus only",
    )
    if args.baseline:
        from acceptance import compare

        report["acceptance"] = compare(json.loads(args.baseline.read_text(encoding="utf-8")), report)["acceptance"]
    (args.output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"mode": report["mode"], "metrics": variants[0]["metrics"], "variants": len(variants)}))
    return int(not report["complete"] or ("acceptance" in report and not all(report["acceptance"].values())))


if __name__ == "__main__":
    raise SystemExit(main())
