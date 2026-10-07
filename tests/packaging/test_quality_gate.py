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
SINGLE = "def f(a):\n    return a\n"
TWINS = "if X:\n    def f(a):\n        return a\nelse:\n    def f(a):\n        return a\n"


def _ruff(files: dict) -> dict:
    return {"ruff": files, "pyright": {}}


def _arguments(path: str, row: int, count: int) -> tuple:
    return ("ruff", path, row, "PLR0913", f"Too many arguments in function definition ({count} > 8)")


def _twins(first: int, second: int | None) -> dict:
    """Two conditional definitions of ``f`` over the limit with ``first`` and ``second`` arguments (None: the second
    is gone)."""
    if second is None:
        return quality.tally([_arguments("a.py", 1, first)], {"a.py": SINGLE})
    return quality.tally([_arguments("a.py", 2, first), _arguments("a.py", 5, second)], {"a.py": TWINS})


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
    over, under, flagged = quality.compare(recorded, current)
    assert over == [
        "ruff core/a.py C901 Store.put: 21, recorded 20",
        "ruff core/a.py C901 fresh: 16, above the 0 it may have been",
    ]
    assert under == []
    assert flagged == {("ruff", "core/a.py", "C901")}
    over, under, _ = quality.compare(recorded, _ruff({"core/a.py": {"C901": {"Store.put": 18}}}))
    assert over == []
    assert under == [
        "ruff core/a.py C901 Store.put: 18, recorded 20",
        "ruff C901 renamed, moved or sharing a name: recorded core/a.py plain 16; now none",
    ]


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


def test_a_renamed_or_moved_function_may_not_grow_on_its_way():
    recorded = _ruff({"core/a.py": {"C901": {"old": 16, "other": 30}}})
    renamed = _ruff({"core/a.py": {"C901": {"renamed": 100, "other": 30}}})
    assert quality.grown(recorded, renamed) == ["ruff core/a.py C901 renamed: 100, above the 16 it may have been"]
    moved = _ruff({"core/a.py": {"C901": {"other": 30}}, "core/b.py": {"C901": {"old": 100}}})
    assert quality.grown(recorded, moved) == ["ruff core/b.py C901 old: 100, above the 16 it may have been"]
    # Two renamed at once, each no bigger than one that went: matched largest to largest.
    both = _ruff({"core/b.py": {"C901": {"first": 29, "second": 16}}})
    assert quality.grown(recorded, both) == []


def test_a_definition_repeated_under_one_name_is_held_to_its_own_size():
    defined = quality.functions(TWINS)
    assert [name for *_, name in defined] == ["f", "f#2"]
    both = [_arguments("a.py", 2, 9), _arguments("a.py", 5, 9)]
    assert quality.tally(both, {"a.py": TWINS})["ruff"] == {"a.py": {"PLR0913": {"f#1": 9, "f#2": 9}}}
    recorded = quality.tally(both[:1], {"a.py": TWINS})
    assert recorded["ruff"] == {"a.py": {"PLR0913": {"f#1": 9}}}  # shared, though only one is over the limit
    over, _under, _flagged = quality.compare(recorded, quality.tally(both, {"a.py": TWINS}))
    assert over == ["ruff a.py PLR0913 f#1: 9, above the 0 it may have been"]


def test_removing_or_reordering_one_of_twins_is_not_taken_for_growth():
    recorded = _twins(9, 10)
    assert recorded["ruff"] == {"a.py": {"PLR0913": {"f#1": 10, "f#2": 9}}}
    for current in (_twins(10, None), _twins(9, None)):
        assert quality.grown(recorded, current) == []
        assert quality.compare(recorded, current)[0] == []
    assert _twins(10, 9) == recorded
    # Either twin growing is still growth, also beneath the other.
    assert quality.compare(recorded, _twins(9, 11))[0] == ["ruff a.py PLR0913 f#1: 11, above the 10 it may have been"]
    assert quality.grown(recorded, _twins(10, 10)) == ["ruff a.py PLR0913 f#1: 10, above the 9 it may have been"]
    # Only the larger twin renamed or moved: neither grew.
    twins = _ruff({"a.py": {"PLR0913": {"f#1": 12, "f#2": 9}}})
    assert quality.grown(twins, _ruff({"a.py": {"PLR0913": {"f": 9, "g": 12}}})) == []
    assert quality.grown(twins, _ruff({"a.py": {"PLR0913": {"f": 9}}, "b.py": {"PLR0913": {"f": 12}}})) == []
    # A function alone under its name stays held to its own size, whatever another does.
    alone = _ruff({"a.py": {"PLR0913": {"f": 12, "g": 9}}})
    assert quality.grown(alone, _ruff({"a.py": {"PLR0913": {"f": 9, "g": 12}}})) == [
        "ruff a.py PLR0913 g: 12, recorded 9"
    ]
    # A function nested in one of twins is numbered with those nested in the other.
    assert quality.by_size({"f.g": 16, "f#2.g": 20, "f#2": 30}, {"f", "f.g"}) == {"f.g#1": 20, "f.g#2": 16, "f#1": 30}


def test_a_twin_under_the_limit_still_makes_the_name_shared():
    # Twins of 9 and 8 arguments (only 9 over the limit) and a function of 12 in another file; then the 9 goes and
    # the 12 is moved into its place.  Nothing grew.
    recorded = quality.tally([_arguments("a.py", 2, 9), _arguments("b.py", 1, 12)], {"a.py": TWINS, "b.py": SINGLE})
    assert recorded["ruff"] == {"a.py": {"PLR0913": {"f#1": 9}}, "b.py": {"PLR0913": {"f": 12}}}
    current = quality.tally([_arguments("a.py", 2, 12)], {"a.py": TWINS})
    assert quality.grown(recorded, current) == []
    assert quality.compare(recorded, current)[0] == []


def test_the_installed_package_must_hold_exactly_what_this_tree_ships(tmp_path):
    root, installed = tmp_path / "tree", tmp_path / "site" / "scope_recall"
    for folder in (root / "packaging", root / "core", installed / "core"):
        folder.mkdir(parents=True)
    allowlist = {"python_modules": ["__init__.py", "core/a.py"], "package_data": ["_worker.py", "data.json"]}
    (root / "packaging" / "v11-module-allowlist.json").write_text(json.dumps(allowlist), encoding="utf-8")
    for name in ("__init__.py", "core/a.py", "_worker.py"):
        (root / name).write_text(f"# {name}\n", encoding="utf-8")
        (installed / name).write_text(f"# {name}\n", encoding="utf-8")
    assert quality.install_problem(installed, root) is None
    (installed / "core" / "a.pyi").write_text("x: int\n", encoding="utf-8")
    assert quality.install_problem(installed, root) == "core/a.pyi is in it but not shipped"
    (installed / "core" / "a.pyi").unlink()
    (installed / "core" / "a.py").unlink()
    assert quality.install_problem(installed, root) == "core/a.py is missing from it"
    (installed / "core" / "a.py").write_text("# changed\n", encoding="utf-8")
    assert quality.install_problem(installed, root) == "core/a.py differs"


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
