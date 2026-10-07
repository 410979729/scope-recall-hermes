"""The quality gate's comparison: what counts as a new finding, and what the baseline may record.

Running ruff and pyright is the CI lint job's part; these tests feed the comparison findings directly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import quality  # noqa: E402

SOURCE = """\
def plain():
    return 1


class Store:
    @property
    def size(self):
        return 2

    def put(self, row):
        def check(value):
            return value

        return check(row)
"""


def _ruff(files: dict) -> dict:
    return {"ruff": files, "pyright": {}}


def test_a_finding_beyond_the_record_is_over_and_fewer_is_under():
    recorded = _ruff({"core/a.py": {"E501": 2, "BLE001": 1}})
    current = _ruff({"core/a.py": {"E501": 3}, "core/b.py": {"F401": 1}})
    over, under, flagged = quality.compare(recorded, current)
    assert over == ["ruff core/a.py E501 findings: 3, recorded 2", "ruff core/b.py F401 findings: 1, recorded 0"]
    assert under == ["ruff core/a.py BLE001 findings: 0, recorded 1"]
    assert flagged == {("ruff", "core/a.py", "E501"), ("ruff", "core/b.py", "F401")}
    assert quality.compare(current, current)[:2] == ([], [])


def test_a_function_is_held_to_its_recorded_size():
    recorded = _ruff({"core/a.py": {"C901": {"Store.put": 20, "plain": 16}}})
    current = _ruff({"core/a.py": {"C901": {"Store.put": 21, "plain": 16, "fresh": 16}}})
    over, under, _ = quality.compare(recorded, current)
    assert over == ["ruff core/a.py C901 Store.put: 21, recorded 20", "ruff core/a.py C901 fresh: 16, recorded 0"]
    assert under == []
    over, under, _ = quality.compare(recorded, _ruff({"core/a.py": {"C901": {"Store.put": 18}}}))
    assert over == []
    assert under == ["ruff core/a.py C901 Store.put: 18, recorded 20", "ruff core/a.py C901 plain: 0, recorded 16"]


def test_the_baseline_records_no_more_findings_unless_asked():
    recorded = _ruff({"core/a.py": {"E501": 2, "C901": {"Store.put": 20}}})
    # Findings that moved to another file, and a renamed function of the same size, are not more.
    moved = _ruff({"core/a.py": {"E501": 1, "C901": {"Store.store": 20}}, "core/b.py": {"E501": 1}})
    assert quality.grown(recorded, moved) == []
    more = _ruff({"core/a.py": {"E501": 3, "C901": {"Store.put": 22}}})
    assert quality.grown(recorded, more) == [
        "ruff E501: 3 findings, 2 recorded",
        "ruff core/a.py C901 Store.put: 22, recorded 20",
    ]


def test_a_size_finding_names_the_function_on_its_line():
    defined = quality.functions(SOURCE)
    assert [name for *_, name in defined] == ["plain", "Store.size", "Store.put", "Store.put.check"]
    assert quality.function_at(defined, 1) == "plain"
    assert quality.function_at(defined, 7) == "Store.size"
    assert quality.function_at(defined, 6) == "Store.size"  # its decorator
    assert quality.function_at(defined, 11) == "Store.put.check"  # inside Store.put, the innermost
    assert quality.function_at(defined, 14) == "Store.put"
    assert quality.function_at(defined, 4) == "<module>"


def test_findings_are_tallied_per_file_rule_and_function():
    findings = [
        ("ruff", "core/a.py", 10, "C901", "`put` is too complex (17 > 15)"),
        ("ruff", "core/a.py", 10, "PLR0913", "Too many arguments in function definition (9 > 8)"),
        ("ruff", "core/a.py", 3, "E501", "Line too long (130 > 120)"),
        ("ruff", "core/a.py", 4, "E501", "Line too long (125 > 120)"),
        ("pyright", "core/a.py", 5, "reportArgumentType", "Argument of type ..."),
    ]
    assert quality.tally(findings, {"core/a.py": SOURCE}) == {
        "ruff": {"core/a.py": {"C901": {"Store.put": 17}, "PLR0913": {"Store.put": 9}, "E501": 2}},
        "pyright": {"core/a.py": {"reportArgumentType": 1}},
    }


def test_the_recorded_baseline_has_the_shape_the_gate_reads():
    recorded = json.loads(quality.BASELINE.read_text(encoding="utf-8"))
    assert set(recorded) == set(quality.TOOLS)
    for tool, files in recorded.items():
        for path, rules in files.items():
            assert (ROOT / path).is_file(), f"{tool} records {path}, which is not in the tree"
            for rule, value in rules.items():
                if rule in quality.SIZE_RULES:
                    assert value and all(type(size) is int and size > 0 for size in value.values()), (path, rule)
                else:
                    assert type(value) is int and value > 0, (path, rule)
