"""Execute auditable product scenarios against an explicitly selected installed package.

Use -I so the interpreter selects the product; only sibling test helpers are
added to sys.path. All homes, stores and outputs must be under the supplied
scratch root. The suite never launches a real host or contacts a model service.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

# Isolated mode omits the script directory. This imports test helpers only and
# leaves the installed product selection untouched, including historical wheels.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scenario_checks import CHECKS  # noqa: E402
from scenario_store import ScenarioStore  # noqa: E402
from scope_recall._version import __version__  # noqa: E402


class Results:
    """Record independent assertions without turning unsupported coverage into passes."""

    def __init__(self, specs):
        self.specs = {row["id"]: row for row in specs["checks"]}
        self.rows = {}

    def record(self, key, passed, actual, *, classification="product_defect", status=None):
        """Retain the definition, method, criterion and actual evidence for every check."""
        self.rows[key] = dict(
            **self.specs[key],
            status=status or ("pass" if passed else "fail"),
            classification=None if passed else classification,
            actual=actual,
        )

    def machine(self, key, missing):
        """Keep unavailable real-host or platform evidence outside the pass count."""
        self.record(key, False, dict(missing=missing), classification="real_machine", status="m6")


def deny_network(*args, **kwargs):
    """Fail closed if a scenario accidentally attempts an external connection."""
    raise AssertionError("scenario suite forbids network connections")


def run(args):
    """Execute each scenario independently and preserve unexpected measurement errors."""
    specs = json.loads(Path(__file__).with_name("scenarios_fixture.json").read_text(encoding="utf-8"))
    results = Results(specs)
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    socket.create_connection = deny_network
    socket.socket.connect = deny_network
    for scenario, check in CHECKS.items():
        if args.only and scenario not in args.only.split(","):
            continue
        scenes = ScenarioStore(args.output / scenario)
        try:
            check(scenes, results, args)
        # Unexpected failures remain measurement errors, not product verdicts.
        except Exception:  # noqa: BLE001
            error = traceback.format_exc()
            for key in results.specs:
                if key.startswith(scenario + ".") and key not in results.rows:
                    results.record(key, False, dict(error=error), classification="measurement_error", status="error")
        finally:
            scenes.close()
        print(scenario, "recorded", flush=True)
    report = dict(
        schema="scenarios/2",
        version=__version__,
        package_file=__import__("scope_recall").__file__,
        created_at=datetime.now(UTC).isoformat(),
        real_model_calls=0,
        real_embedding_calls=0,
        real_host_processes=0,
        elapsed_seconds=time.perf_counter() - started,
        fixture=str(Path(__file__).with_name("scenarios_fixture.json")),
        fixture_sha256=hashlib.sha256(Path(__file__).with_name("scenarios_fixture.json").read_bytes()).hexdigest(),
        checks=list(results.rows.values()),
    )
    (args.output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(version=__version__, rows=len(results.rows), output=str(args.output)), ensure_ascii=False))


def main():
    """Require explicit scratch and evidence roots; no ambient user homes are read."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--only", default="", help="comma-separated scenario IDs for a minimal reproduction")
    parser.add_argument("--scale", type=int, default=350000)
    args = parser.parse_args()
    args.output.resolve().relative_to(args.scratch.resolve())
    run(args)


if __name__ == "__main__":
    main()
