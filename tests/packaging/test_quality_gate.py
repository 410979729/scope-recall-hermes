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


def _tree(root: Path, files: dict[str, str]) -> list[str]:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return list(files)


def test_the_package_imports_form_no_cycle_and_follow_its_layers():
    assert quality.import_problems(ROOT, quality.shipped_python(ROOT)) == []


def test_an_import_in_a_function_that_closes_a_cycle_is_one(tmp_path):
    files = _tree(
        tmp_path,
        {
            "core/__init__.py": "",
            "core/a.py": "from .b import helper\n",
            "core/b.py": "def helper():\n    from . import a\n\n    return a\n",
            "core/c.py": "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from .d import Row\n",
            "core/d.py": "from .c import TYPE_CHECKING\n",
        },
    )
    # c's import of d is for the type checker alone: d importing c closes no cycle.
    assert quality.import_problems(tmp_path, files) == [
        "import cycle among 2 modules:\n"
        "    core/a.py:1 imports core/b.py\n"
        "    core/b.py:2 imports core/a.py in a function"
    ]


def test_only_what_the_type_checker_alone_runs_is_left_out(tmp_path):
    files = _tree(
        tmp_path,
        {
            "core/__init__.py": "",
            "core/a.py": (
                "import typing\n"
                "from typing import TYPE_CHECKING as TC\n"
                "\n"
                "if TC:\n"
                "    from ..runtime import t1\n"
                "if typing.TYPE_CHECKING:\n"
                "    from ..runtime import t2\n"
                "else:\n"
                "    from ..runtime import r1\n"
                "if not TC:\n"
                "    from ..runtime import r2\n"
                "else:\n"
                "    from ..runtime import t3\n"
            ),
            **{f"runtime/{name}.py": "" for name in ("__init__", "t1", "t2", "t3", "r1", "r2")},
        },
    )
    # The flag under another name or through typing still guards its body; an else, or the body of `if not`, runs.
    assert quality.import_problems(tmp_path, files) == [
        "upward import: core/a.py:9 imports runtime/r1.py",
        "upward import: core/a.py:11 imports runtime/r2.py",
    ]


def test_importing_a_module_runs_the_packages_around_it(tmp_path):
    for case, first in enumerate(("from .pkg import leaf\n", "import scope_recall.core.pkg.leaf as leaf\n")):
        files = _tree(
            tmp_path / f"case{case}",
            {
                "core/__init__.py": "from .x import X\n",
                "core/x.py": "from .y import Y\n\nX = Y\n",
                "core/y.py": "Y = 1\n",
                "core/a.py": first + "\nA = 1\n",
                "core/pkg/__init__.py": "from ..a import A\n",
                "core/pkg/leaf.py": "",
            },
        )
        # Python stops a's import half-way: pkg's package code asks a for A before a defines it.  The core package
        # importing x, which imports its sibling y, is no cycle: x and y are inside the package that runs first.
        assert quality.import_problems(tmp_path / f"case{case}", files) == [
            "import cycle among 2 modules:\n"
            "    core/a.py:1 imports core/pkg/__init__.py\n"
            "    core/pkg/__init__.py:1 imports core/a.py"
        ]


def test_an_import_from_a_higher_layer_is_one_but_from_an_entry_or_as_a_named_lazy_one(tmp_path):
    files = _tree(
        tmp_path,
        {
            "__init__.py": "from .runtime.y import Y\n",
            "contracts.py": "",
            "core/__init__.py": "",
            "core/x.py": "from .. import contracts\nfrom ..runtime import y\nfrom .. import Y\n",
            "runtime/__init__.py": "",
            "runtime/y.py": "",
            "runtime/instance.py": (
                "from ..adapters.hermes import authorization\n\n\n"
                "def build():\n    from ..adapters.clients.authorization import build\n\n    return build\n"
            ),
            "adapters/__init__.py": "",
            "adapters/clients/__init__.py": "",
            "adapters/clients/authorization.py": "from ...runtime import instance\n",
            "adapters/hermes/__init__.py": "",
            "adapters/hermes/authorization.py": "",
            "adapters/codex/__init__.py": "",
            "adapters/codex/hook_entry.py": "from ...maintenance import install\n",
            "adapters/codex/helper.py": "from ...maintenance import install\n",
            "maintenance/__init__.py": "",
            "maintenance/install.py": "from scope_recall.core import x\n",
        },
    )
    # Only the five installed entry modules may reach any layer; a re-export by the package root is followed; the
    # instance's import of the Hermes check is named, but made at the top, where every worker start pays for it.  The
    # named lazy import closes no cycle with the client check importing the runtime: that is the import itself.
    assert quality.import_problems(tmp_path, files) == [
        "upward import: adapters/codex/helper.py:1 imports maintenance/install.py",
        "upward import: core/x.py:2 imports runtime/y.py",
        "upward import: core/x.py:3 imports runtime/y.py through __init__.py",
        "upward import: runtime/instance.py:1 imports adapters/hermes/authorization.py",
    ]


def test_a_typing_flag_bound_twice_guards_nothing(tmp_path):
    files = _tree(
        tmp_path,
        {
            "core/__init__.py": "",
            "core/a.py": (
                "from typing import TYPE_CHECKING as TC\n\n\n"
                "def run(TC):\n    if TC:\n        from ..runtime import b\n"
            ),
            "core/c.py": (
                "from typing import TYPE_CHECKING as TC\n\n"
                'match {"x": 1}:\n    case {**TC}:\n        if TC:\n            from ..runtime import d\n'
            ),
            **{f"runtime/{name}.py": "" for name in ("__init__", "b", "d")},
        },
    )
    # Inside run, TC is whatever the caller passes; the match binds TC to the rest of its mapping.
    assert quality.import_problems(tmp_path, files) == [
        "upward import: core/a.py:6 imports runtime/b.py in a function",
        "upward import: core/c.py:6 imports runtime/d.py",
    ]


def test_the_named_lazy_imports_cover_their_own_statements_alone(tmp_path):
    files = _tree(
        tmp_path,
        {
            "__init__.py": "from .maintenance.b import B\n",
            "runtime/__init__.py": "",
            "runtime/instance.py": (
                "def build():\n    from ..adapters import helper\n    from .. import B\n\n    return helper, B\n"
            ),
            "adapters/__init__.py": "helper = 1\n",
            "maintenance/__init__.py": "",
            "maintenance/b.py": "B = 7\n",
        },
    )
    # Lazy and from runtime/instance.py like the named ones, but neither names a host's authorization check.
    assert quality.import_problems(tmp_path, files) == [
        "upward import: runtime/instance.py:2 imports adapters/__init__.py in a function",
        "upward import: runtime/instance.py:3 imports maintenance/b.py through __init__.py in a function",
    ]


def test_a_package_attribute_wins_over_a_submodule_of_its_name(tmp_path):
    files = _tree(
        tmp_path,
        {
            "__init__.py": "from .runtime.b import B as helper\n",
            "helper.py": "",
            "core/__init__.py": "",
            "core/a.py": "from .pkg import value\nfrom .. import helper\n\nA = value\n",
            "core/pkg/__init__.py": "value = 1\n",
            "core/pkg/value.py": "from ..a import A\n",
            "runtime/__init__.py": "",
            "runtime/b.py": "B = 7\n",
        },
    )
    # Python takes pkg's value and never loads pkg/value.py (no cycle), and the root's helper is runtime's B.
    assert quality.import_problems(tmp_path, files) == [
        "upward import: core/a.py:2 imports runtime/b.py through __init__.py"
    ]


def test_an_attribute_counts_only_where_python_binds_it(tmp_path):
    package = (
        "from typing import TYPE_CHECKING\n\n"
        "noted: object\n"
        "if TYPE_CHECKING:\n    typed = 1\n"
        "try:\n    tried = 1\nexcept ImportError:\n    pass\n"
        "gone = 1\ndel gone\n"
        "from . import own as own\n"
    )
    names = ("noted", "typed", "tried", "gone", "own")
    files = _tree(
        tmp_path,
        {
            "core/__init__.py": "",
            "core/a.py": f"from .pkg import {', '.join(names)}\n\nA = 1\n",
            "core/pkg/__init__.py": package,
            **{f"core/pkg/{name}.py": "from ..a import A\n" for name in names},
            "core/other/__init__.py": (
                "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    typed = 1\nfrom . import inner\n"
            ),
            "core/other/inner.py": "from . import typed\n",
            "core/other/typed.py": "",
        },
    )
    # An annotation and what a type checker alone runs bind nothing, so Python loads those submodules; a name bound on
    # some paths only, or deleted, may be the submodule; the package's own `from . import own as own` loads own.  Each
    # submodule asks a for A before a defines it.  In other, inner's `from . import typed` loads the submodule alone:
    # the half-made package it is imported from binds no typed, so inner does not wait for it.
    problems = quality.import_problems(tmp_path, files)
    assert len(problems) == 1 and problems[0].startswith("import cycle among 7 modules:")
    for name in names:
        assert f"    core/pkg/{name}.py:1 imports core/a.py" in problems[0]
    assert "    core/pkg/__init__.py:12 imports core/pkg/own.py" in problems[0]


def test_what_runs_while_a_module_loads_is_told_from_what_waits_for_a_call(tmp_path):
    build = "def build():\n    from ..adapters.hermes.authorization import check\n\n    return check\n\n\n"
    loads = {
        "a class body": "class Holder:\n    value = build()\n",
        "a default": "def wrap(value=build()):\n    return value\n",
        "a decorator call": "@build()\ndef wrapped():\n    pass\n",
        "a decorator": "@build\ndef wrapped():\n    pass\n",
    }
    waits = {
        "a lambda": "later = lambda: build()  # noqa: E731\n",
        "a typing-only branch": "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    build()\n",
        "a method": "class Holder:\n    def value(self):\n        return build()\n",
    }
    for case, (tail, expected) in enumerate(
        [(code, True) for code in loads.values()] + [(code, False) for code in waits.values()]
    ):
        files = _tree(
            tmp_path / f"case{case}",
            {
                "runtime/__init__.py": "",
                "runtime/instance.py": build + tail,
                "adapters/__init__.py": "",
                "adapters/hermes/__init__.py": "",
                "adapters/hermes/authorization.py": "check = 1\n",
            },
        )
        problems = quality.import_problems(tmp_path / f"case{case}", files)
        message = (
            "named lazy import while its module loads: runtime/instance.py:1 build runs before the module is complete"
        )
        assert problems == ([message] if expected else []), tail


def test_star_imports_dynamic_imports_and_a_named_lazy_import_run_at_load(tmp_path):
    files = _tree(
        tmp_path,
        {
            "core/__init__.py": "",
            "core/a.py": (
                "from .pkg import *\n"
                'load = lambda: __import__("scope_recall.runtime.b")\n'
                '__import__("scope_recall.core.pkg", fromlist=["leaf"])\n'
                "A = 1\n"
            ),
            "core/pkg/__init__.py": "",
            "core/pkg/leaf.py": "from ..a import A\n",
            "runtime/__init__.py": "",
            "runtime/b.py": "",
            "runtime/instance.py": (
                "def build():\n    from ..adapters.hermes.authorization import check\n\n    return check\n\n\nbuild()\n"
            ),
            "adapters/__init__.py": "",
            "adapters/hermes/__init__.py": "",
            "adapters/hermes/authorization.py": "check = 1\n",
        },
    )
    # The fromlist makes Python load pkg/leaf.py, which asks a for A before a defines it.
    assert quality.import_problems(tmp_path, files) == [
        "star import: core/a.py:1 (name what it imports: the import check cannot follow a star)",
        "named lazy import while its module loads: runtime/instance.py:1 build runs before the module is complete",
        "import cycle among 2 modules:\n"
        "    core/a.py:3 imports core/pkg/leaf.py\n"
        "    core/pkg/leaf.py:1 imports core/a.py",
        "upward import: core/a.py:2 imports runtime/b.py in a function",
    ]
