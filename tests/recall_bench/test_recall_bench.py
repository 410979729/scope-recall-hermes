"""The recall benchmark's dev split at the CI size, in the retrieval tier.

``recall_bench.py`` beside this file builds a synthetic shared store, asks every question the way hosts do and
scores the answers; it also runs on its own, on the held-out split and at a larger size.  Here it fails on any item
from a scope the asking entry does not read, and on any metric ``baseline.json`` holds that moved further than its
tolerance to the wrong side.  A change meant to move the numbers records them with
``python tests/recall_bench/recall_bench.py --update``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load_bench():
    spec = importlib.util.spec_from_file_location("recall_bench", HERE / "recall_bench.py")
    module = importlib.util.module_from_spec(spec)
    # Registered first: its dataclasses resolve their annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def measured():
    bench = _load_bench()
    return bench, bench.run("dev", scale=1)


def test_no_entry_recalls_another_scope(measured) -> None:
    _bench, report = measured
    leaked = [(q["id"], path) for q in report["questions"] for path in ("auto", "explicit") if q[path]["leaks"]]
    assert report["leaks"] == 0, f"items from a scope the asking entry does not read: {leaked}"
    revoked = report["revoked_seen_before"]
    assert revoked["asked"] and revoked["seen"] == revoked["asked"], (
        f"only {revoked['seen']} of {revoked['asked']} revoked scopes showed in the packet before the revocation; "
        "for the others, not showing after proves nothing"
    )


def test_no_metric_falls_below_its_baseline(measured) -> None:
    bench, report = measured
    baseline = json.loads(bench.BASELINE.read_text(encoding="utf-8"))
    problems = bench.regressions(report, baseline)
    assert not problems, (
        "\n".join(problems) + "\n\n" + bench.summary(report) + "\n\nIf the change is meant to move these numbers: "
        "python tests/recall_bench/recall_bench.py --update (a drop also needs --allow-worse)"
    )
