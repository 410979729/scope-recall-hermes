"""Run the fixed synthetic adoption corpus against the approved chat endpoint.

No embeddings or production stores are opened. Provider errors stop the run;
there are no retries, and the concurrency bound includes every model request.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from scope_recall.contracts import ContractError
from scope_recall.core.adoption import (
    adoption_messages,
    decision_messages,
    decision_proposal,
    decode_selection,
    fragments,
)

ROOT = Path(__file__).resolve().parents[2]


def evaluate(case, config, stopped, *, endpoint):
    """Measure one classification and, for an adoption, its isolated decision request."""
    owner = SimpleNamespace(
        ref="TEST-owner",
        revision=1,
        project_id="TEST-project",
        event={"content": case["confirmation"], "occurred_at": None},
    )
    parts = fragments(case["assistant"])
    records = []

    def call(messages, stage):
        if stopped.is_set():
            raise RuntimeError("evaluation_stopped")
        body = dict(
            model=config["model"],
            temperature=0,
            max_tokens=1024,
            response_format={"type": "json_object"},
            messages=messages,
        )
        request = urllib.request.Request(
            endpoint, json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                result = json.load(response)
        except Exception:
            stopped.set()
            raise
        content = result["choices"][0]["message"]["content"]
        records.append(
            dict(
                stage=stage,
                usage=result.get("usage", {}),
                prompt_sha256=hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest(),
                response=content,
                model=result.get("model"),
            )
        )
        return content

    raw = call(adoption_messages(owner, parts), "locate")
    try:
        selection = decode_selection(raw, parts)
    except (ContractError, ValueError, TypeError) as exc:
        return dict(
            id=case["id"],
            confirmation=case["confirmation"],
            expected=case["adopted"],
            detected=None,
            correct=False,
            scope_and_fragments_correct=False,
            selection=None,
            calls=records,
            error=getattr(exc, "field", type(exc).__name__),
        )
    detected = selection is not None
    correct = detected == case["adopted"]
    exact = correct and (
        not detected or (selection["fragment_ids"] == case["fragment_ids"] and selection["scope"] == case["scope"])
    )
    if selection is not None:
        snapshot = SimpleNamespace(
            confirmation=owner, fragments=parts, assistant=SimpleNamespace(ref="TEST-assistant", revision=1)
        )
        summary = call(decision_messages(snapshot, selection), "decision")
        decision_proposal(snapshot, selection, summary)
    return dict(
        id=case["id"],
        confirmation=case["confirmation"],
        expected=case["adopted"],
        detected=detected,
        correct=correct,
        scope_and_fragments_correct=exact,
        selection=selection,
        calls=records,
    )


def main():
    """Validate the endpoint and corpus, then write a complete or failed evidence record."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--approved-endpoint", required=True, type=str)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--concurrency", type=int, choices=range(1, 5), default=1)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))["chat"]
    if config["endpoint"] != args.approved_endpoint:
        parser.error("route is outside the approved endpoint")
    if args.output.exists():
        parser.error("output must be a fresh path")
    cases_path = ROOT / "fixtures/adoption/cases.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    if len(cases) != 24 or sum(c["adopted"] for c in cases) != 12:
        parser.error("expected the fixed 12 positive and 12 negative corpus")
    stopped = threading.Event()
    result = dict(
        complete=False,
        corpus_sha256=hashlib.sha256(cases_path.read_bytes()).hexdigest(),
        endpoint=args.approved_endpoint,
        model=config["model"],
        concurrency=args.concurrency,
        embedding_calls=0,
        cases=[],
    )
    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = [executor.submit(evaluate, c, config, stopped, endpoint=args.approved_endpoint) for c in cases]
            for future in futures:
                row = future.result()
                result["cases"].append(row)
                args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(
                    json.dumps(
                        dict(id=row["id"], correct=row["correct"], detected=row["detected"]), ensure_ascii=False
                    ),
                    flush=True,
                )
        rows = result["cases"]
        result.update(
            complete=not any(r.get("error") for r in rows),
            correct=sum(r["correct"] for r in rows),
            false_positives=sum(r["detected"] is True and not r["expected"] for r in rows),
            false_negatives=sum(r["detected"] is not True and r["expected"] for r in rows),
            exact_selections=sum(r["scope_and_fragments_correct"] for r in rows),
        )
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        stopped.set()
        result["error"] = type(exc).__name__
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "cases"}, ensure_ascii=False), flush=True)
    return 0 if result.get("complete") and result.get("correct") == 24 and result.get("false_positives") == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
