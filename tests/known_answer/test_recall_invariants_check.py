"""Known answers for three recall invariants (#179, #213), in the storage tier.

The check is ``recall_invariants_check.py`` beside this file, which also runs on
its own against an installed release.  Here it runs in process against this
checkout, on a fresh temp store with synthetic data: no model, no network, no
key.  The test fails on the first answer that differs from
``expected_answers.json``, in the order the standalone run prints them.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent


def _load_check():
    spec = importlib.util.spec_from_file_location("recall_invariants_check", HERE / "recall_invariants_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_known_answer_matches() -> None:
    check = _load_check()
    expected = json.loads((HERE / "expected_answers.json").read_text(encoding="utf-8"))["checks"]
    assert expected, "expected_answers.json holds no checks"

    observed, diagnostics = check.run()

    for key in sorted(expected, key=check._order):
        got = observed.get(key, "<not run>")
        want = expected[key]["expected"]
        assert got == want, (
            f"{key}: observed {json.dumps(got)}, expected {json.dumps(want)}. "
            f"{expected[key]['means']} Diagnostics: {json.dumps(diagnostics, default=str)}"
        )
