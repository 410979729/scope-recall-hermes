"""Evaluate each packaged prompt and its clauses on fixed synthetic format examples."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evaluate import live_port
from evaluate import prompt_clauses as clauses
from scope_recall.contracts import validate_payload
from scope_recall.core.language_resources import prompt_text

ROOT = Path(__file__).resolve().parents[2]
NAMES = ("consolidation", "candidate", "fragment", "repair", "support", "merge")


def messages(name, example, clause):
    """Build the same prompt composition used by extraction and support requests."""
    schema = json.loads((ROOT / "contracts/consolidation_result.schema.json").read_text(encoding="utf-8"))
    base = prompt_text("consolidation").format(schema=json.dumps(schema, ensure_ascii=False))
    selected = prompt_text(name)
    if name == "consolidation":
        system = base
    elif name == "repair":
        system = base + selected.format(feedback=json.dumps(example["input"]["validation_feedback"]))
    elif name == "candidate":
        system = selected + "\n\n" + base
    elif name == "fragment":
        system = base + selected
    else:
        system = prompt_text("support") + " " + prompt_text("merge")
    if clause:
        rendered = clause.format(
            schema=json.dumps(schema, ensure_ascii=False),
            feedback=json.dumps(example["input"].get("validation_feedback")),
        )
        if rendered not in system:
            raise ValueError("ablation_clause_not_present")
        system = system.replace(rendered, "", 1)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(example["input"], ensure_ascii=False)},
    ]


def valid_result(name, raw, expected, inputs):
    """Check schema and expected disposition without making a second model judgement."""
    value = json.loads(raw)
    if name in {"support", "merge"}:
        return value == expected
    value = validate_payload("consolidation_result", value)
    if len(value["claim_proposals"]) != len(expected["claim_proposals"]):
        return False
    fields = (
        "kind",
        "subject",
        "predicate",
        "value_text",
        "conditions",
        "statement_kind",
        "valid_from",
        "valid_to",
        "occurred_at",
    )
    if any(
        any(actual.get(field) != wanted.get(field) for field in fields)
        for actual, wanted in zip(value["claim_proposals"], expected["claim_proposals"], strict=True)
    ):
        return False
    sources = {(item["source_ref"], item["source_revision"]): item for item in inputs["sources"]}
    for claim in value["claim_proposals"]:
        for span in claim["evidence_spans"]:
            source = sources.get((span["source_ref"], span["source_revision"]))
            if source is None or "start" not in span or "end" not in span:
                return False
            offset = source.get("source_window", {}).get("start", 0)
            start, end = span["start"] - offset, span["end"] - offset
            if not 0 <= start < end <= len(source["content"]) or source["content"][start:end] != span["quote"]:
                return False
    return value["source_refs"] == expected["source_refs"]


def main():
    """Fake mode validates the harness; live mode requires an approved metered route."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--allow-model-calls", action="store_true")
    parser.add_argument("--approved-endpoint", action="append", default=[])
    parser.add_argument("--prompt", choices=NAMES)
    parser.add_argument("--ablate", action="store_true")
    parser.add_argument("--ablation-limit", type=int)
    args = parser.parse_args()
    if args.fake and args.config:
        parser.error("fake mode accepts no live route")
    if not args.fake and (not args.config or not args.allow_model_calls):
        parser.error("live mode requires a route and explicit model authorization")
    args.output.mkdir(parents=True, exist_ok=False)
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else None
    port = live_port(config, args.approved_endpoint, args.output) if config else None
    reports = []
    for name in (args.prompt,) if args.prompt else NAMES:
        examples = json.loads((ROOT / f"prompts/{name}.examples.json").read_text(encoding="utf-8"))
        removed = [None]
        if args.ablate:
            choices = clauses(name)
            removed.extend(choices[: args.ablation_limit] if args.ablation_limit else choices)
        for clause in removed:
            passed = 0
            errors = []
            for index, example in enumerate(examples):
                request = messages(name, example, clause)
                try:
                    raw = (
                        json.dumps(example["expected"]) if port is None else port.propose(request, remaining_seconds=60)
                    )
                    passed += valid_result(name, raw, example["expected"], example["input"])
                except (ValueError, TypeError) as error:
                    errors.append(dict(example=index, error=type(error).__name__))
            reports.append(dict(prompt=name, removed_clause=clause, passed=passed, total=len(examples), errors=errors))
    result = dict(
        mode="fake" if args.fake else "live",
        quality_claim="workflow only" if args.fake else "format corpus only",
        results=reports,
    )
    (args.output / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            dict(
                mode=result["mode"],
                variants=len(reports),
                passed=sum(r["passed"] for r in reports),
                total=sum(r["total"] for r in reports),
            )
        )
    )
    return int(any(r["passed"] != r["total"] for r in reports if r["removed_clause"] is None))


if __name__ == "__main__":
    raise SystemExit(main())
