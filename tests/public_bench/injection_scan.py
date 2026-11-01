"""Measure synthetic repeated-token scans without any model or embedding calls."""

from __future__ import annotations

import argparse
import inspect
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scope_recall.core import CoreConfig, MemoryCore  # noqa: E402
from scope_recall.core.injection_guard import scan  # noqa: E402
from v11_support import context, source_event  # noqa: E402


def main():
    """Keep scan samples and verdicts beside a caller-selected synthetic store."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-directory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--sizes", nargs="+", type=int, default=[1000, 4000, 12000, 65536, 524288, 2096000])
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--pattern", choices=("send_data", "quoted_discussion", "expansion"), default="send_data")
    parser.add_argument("--budget-seconds", type=float)
    args = parser.parse_args()
    ctx = context(args.data_directory)
    core = MemoryCore(CoreConfig(ctx.binding))
    core.initialize()
    kwargs = {} if args.budget_seconds is None else {"remaining_seconds": args.budget_seconds}
    unit = {
        "send_data": "send data ",
        "quoted_discussion": 'Security discussion: "ignore previous instructions" is a prompt injection attack. ',
        "expansion": "\ufdfa",
    }[args.pattern]
    rows = []
    with core.storage.read(ctx) as tx:
        for size in args.sizes:
            event = source_event(content=unit * (size // len(unit)) + "x" * (size % len(unit)))
            samples = []
            for _ in range(args.samples):
                started = time.perf_counter()
                result = scan(tx, event, **kwargs)
                samples.append((time.perf_counter() - started) * 1000)
            ordered = sorted(samples)
            rows.append(
                dict(
                    characters=size,
                    state=result.state,
                    reasons=result.rules,
                    p50_ms=ordered[len(ordered) // 2],
                    max_ms=max(samples),
                    samples_ms=samples,
                )
            )
    output = dict(
        scanner=str(inspect.getfile(scan)), pattern=args.pattern, budget_seconds=args.budget_seconds, rows=rows
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output))


if __name__ == "__main__":
    main()
