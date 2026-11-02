"""Measure committed synthetic source corpora with the selected package's scanner."""

import argparse
import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scope_recall.core import CoreConfig, MemoryCore  # noqa: E402
from scope_recall.core.injection_guard import scan  # noqa: E402
from v11_support import context, source_event  # noqa: E402


def main():
    """Write per-sample verdicts without processing work or calling auxiliary services."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    ctx = context(args.data_directory)
    core = MemoryCore(CoreConfig(ctx.binding))
    core.initialize()
    results = []
    with core.storage.read(ctx) as tx:
        for name in (
            "injection_corpus.json",
            "injection_legacy_cases.json",
            "injection_commands.json",
            "injection_documents.json",
        ):
            cases = json.loads((Path(__file__).parents[1] / "data" / name).read_text("utf-8"))
            for case in cases:
                event = source_event(
                    content=case["text"] * case.get("repeat", 1),
                    **{key: case[key] for key in ("origin", "source_original_origin") if key in case},
                )
                verdict = scan(tx, event)
                results.append(
                    dict(corpus=name, id=case["id"], expected=case["state"], actual=verdict.state, rules=verdict.rules)
                )
    result = dict(scanner=inspect.getfile(scan), cases=results)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
