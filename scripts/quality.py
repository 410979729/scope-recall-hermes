#!/usr/bin/env python3
"""Hold the tree to its recorded lint and type findings.

    python scripts/quality.py                 # formatted, and nothing above scripts/quality.baseline.json
    python scripts/quality.py --update        # record the current findings there

Run it in an environment made from the lock: it carries the ruff and pyright versions the dev extra pins, the
third-party packages pyright reads, and this tree installed as ``scope_recall`` (pyright reads the absolute
``scope_recall`` imports from the installed copy, so the check refuses a copy that is not this tree):

    uv sync --locked --no-editable --reinstall-package hermes-scope-recall --extra lancedb --extra codex --extra dev
    uv run --no-sync python scripts/quality.py

The baseline holds a count per file and rule, and for a function-size rule (C901, PLR0911-PLR0915) each function's
size.  A function is known by its name where it is the one definition of that name in its file; definitions that
share a name are recorded as ``name#1``, ``name#2``, largest first.  The check fails when ``ruff format`` would change
a file, when a file has more findings of a rule than recorded, or when a function is bigger than recorded: a known
function under its name, the others (renamed, moved, sharing a name) matched largest to largest.  A count says how
many, not which: one finding fixed and another of the same rule added in the same file passes, and so does one of the
unknown functions growing while another shrinks as much.  The check also fails when there is less than recorded, so
that the baseline only goes down: ``--update`` records it, and refuses to record more unless ``--allow-more`` is given.
pyright runs as Linux and as Windows, and a finding either reports counts once.

The package's imports are held to its layers with no baseline: no cycle among its modules (an import inside a
function counts, since it closes the cycle the first time it runs, and importing a module runs the packages around
it), and no module importing from a layer above its own (``LAYERS``), directly or through a module outside the
layers, but the entry modules the installed command lines name and the lazy imports ``LAZY_UPWARD`` names (made in a
function its module does not call while loading).  Only what a type checker alone runs (``if TYPE_CHECKING:``) is
left out.  A literal ``importlib.import_module`` or ``__import__`` of the package's modules counts as an import, and a
star import of them fails, since what it binds cannot be followed; no other dynamic import is seen.  The check reads
syntax, not values: a call through another function or a called lambda while a module loads, a function that is only
named like ``import_module``, and code that changes ``typing.TYPE_CHECKING`` are beyond it.  For the named lazy
imports, ``tests/packaging/test_clean_v11_wheel.py`` loads the runtime from the built wheel and finds no adapter
module loaded.
"""

from __future__ import annotations

import argparse
import ast
import importlib.metadata
import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "scripts" / "quality.baseline.json"
TOOLS = ("ruff", "pyright")
SIZE_RULES = frozenset({"C901", "PLR0911", "PLR0912", "PLR0913", "PLR0915"})
_SIZE = re.compile(r"\((\d+) > \d+\)")

#: One finding: (tool, path, line, rule, message).
Finding = tuple[str, str, int, str, str]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")


def _relative(path: str) -> str:
    return Path(os.path.relpath(path, ROOT)).as_posix()


def shipped_python(root: Path) -> set[str]:
    """The Python files the wheel ships (``packaging/v11-module-allowlist.json``), as paths in the package."""
    allowlist = json.loads((root / "packaging" / "v11-module-allowlist.json").read_text(encoding="utf-8"))
    return {*allowlist["python_modules"], *(path for path in allowlist["package_data"] if path.endswith(".py"))}


def install_problem(installed: Path, root: Path) -> str | None:
    """Why an installed package is not this tree's, or None.  It must hold exactly the Python files the wheel ships
    (``shipped_python``), each as this tree has it, and no type stub besides: pyright reads a stub in place of its
    module."""
    shipped = shipped_python(root)
    present = {
        path.relative_to(installed).as_posix() for path in installed.rglob("*") if path.suffix in (".py", ".pyi")
    }
    unmatched = sorted(present ^ shipped)
    if unmatched:
        return f"{unmatched[0]} is {'missing from it' if unmatched[0] in shipped else 'in it but not shipped'}"
    differing = [path for path in sorted(shipped) if (root / path).read_bytes() != (installed / path).read_bytes()]
    return f"{differing[0]} differs" if differing else None


def check_environment() -> None:
    """Refuse a tool version other than the pinned one, and an installed ``scope_recall`` that is not this tree."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    for spec in project["optional-dependencies"]["dev"]:
        name, _, wanted = spec.partition("==")
        if name not in TOOLS:
            continue
        try:
            have = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            have = "not installed"
        if have != wanted:
            raise SystemExit(f"{name} is {have}; the baseline holds what {name} {wanted} finds (see this file's usage)")
    spec = importlib.util.find_spec("scope_recall")
    installed = Path(spec.origin).parent if spec and spec.origin else None
    if installed is None or installed.resolve() == ROOT:
        raise SystemExit("scope_recall is not installed in this environment (see this file's usage)")
    problem = install_problem(installed, ROOT)
    if problem is not None:
        raise SystemExit(f"the installed scope_recall is not this tree ({problem}): reinstall it, as above")


def functions(source: str) -> list[tuple[int, int, str]]:
    """(first line, last line, name) of every function in a module, decorators included.  The name is the qualified
    one, and a definition that repeats one (a conditional ``def``, a property's setter) adds ``#2``, ``#3`` in source
    order, so that each keeps a size of its own (``tally`` numbers them again, by size)."""
    found: list[tuple[int, int, str]] = []
    seen: dict[str, int] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + child.name
                seen[name] = seen.get(name, 0) + 1
                if seen[name] > 1:
                    name = f"{name}#{seen[name]}"
                if not isinstance(child, ast.ClassDef):
                    first = min([child.lineno, *(item.lineno for item in child.decorator_list)])
                    found.append((first, child.end_lineno or child.lineno, name))
                visit(child, name + ".")
            else:
                visit(child, prefix)

    visit(ast.parse(source), "")
    return found


def function_at(defined: list[tuple[int, int, str]], row: int) -> str:
    """The function a size finding names: the innermost one around its line (ruff points at the ``def`` line)."""
    around = [(last - first, name) for first, last, name in defined if first <= row <= last]
    return min(around)[1] if around else "<module>"


def base_name(name: str) -> str:
    """A function's name without the ``#n`` that tells definitions sharing it apart."""
    return re.sub(r"#\d+", "", name)


def shared_names(defined: list[tuple[int, int, str]]) -> set[str]:
    """The names (``#n`` taken out) that more than one definition of the module has, whether or not over a limit."""
    counts: dict[str, int] = {}
    for _first, _last, name in defined:
        counts[base_name(name)] = counts.get(base_name(name), 0) + 1
    return {name for name, count in counts.items() if count > 1}


def by_size(sizes: dict[str, int], shared: set[str]) -> dict[str, int]:
    """The sizes of one rule in one file under the names the baseline keeps: a name of one definition as it is, and
    the definitions of a ``shared`` name as ``name#1``, ``name#2``, largest first, so that removing or reordering one
    of them changes no other's entry."""
    groups: dict[str, list[int]] = {}
    for name, size in sizes.items():
        groups.setdefault(base_name(name), []).append(size)
    return {
        f"{name}#{rank}" if name in shared else name: size
        for name, values in groups.items()
        for rank, size in enumerate(sorted(values, reverse=True), start=1)
    }


def tally(findings: list[Finding], sources: dict[str, str]) -> dict:
    """Findings as the baseline records them: a count per file and rule, and for a size rule each function's size."""
    record: dict = {tool: {} for tool in TOOLS}
    defined: dict[str, list[tuple[int, int, str]]] = {}
    for tool, path, row, rule, message in findings:
        rules = record[tool].setdefault(path, {})
        size = _SIZE.search(message) if rule in SIZE_RULES else None
        if size is None:
            rules[rule] = rules.get(rule, 0) + 1
            continue
        if path not in defined:
            defined[path] = functions(sources[path])
        name = function_at(defined[path], row)
        sizes = rules.setdefault(rule, {})
        sizes[name] = max(sizes.get(name, 0), int(size.group(1)))
    for files in record.values():
        for path, rules in files.items():
            for rule, value in rules.items():
                if isinstance(value, dict):
                    rules[rule] = by_size(value, shared_names(defined[path]))
    return record


def ruff_findings() -> list[Finding]:
    done = _run("ruff", "check", ".", "--output-format", "json", "--exit-zero")
    if done.returncode != 0:
        raise SystemExit(f"ruff check did not run: {done.stderr.strip()}")
    return [
        ("ruff", _relative(item["filename"]), item["location"]["row"], item["code"] or "syntax-error", item["message"])
        for item in json.loads(done.stdout)
    ]


def pyright_findings() -> list[Finding]:
    seen: dict[tuple[str, int, int, str, str], None] = {}
    for platform in ("Linux", "Windows"):
        done = _run("pyright", "--outputjson", "--pythonpath", sys.executable, "--pythonplatform", platform)
        try:
            report = json.loads(done.stdout)
        except ValueError:
            reason = (done.stderr or done.stdout).strip()[:2000]
            raise SystemExit(f"pyright did not report ({platform}): {reason}") from None
        if done.returncode not in (0, 1):
            raise SystemExit(f"pyright failed ({platform}, exit {done.returncode}): {done.stderr.strip()[:2000]}")
        for item in report["generalDiagnostics"]:
            if item["severity"] in ("error", "warning"):
                start = item["range"]["start"]
                rule = item.get("rule") or item["severity"]
                seen[(_relative(item["file"]), start["line"] + 1, start["character"], rule, item["message"])] = None
    return [("pyright", path, row, rule, message.splitlines()[0]) for path, row, _, rule, message in seen]


def _sizes(record: dict, tool: str) -> dict[str, dict[tuple[str, str], int]]:
    """Each size rule's functions, by (file, name)."""
    found: dict[str, dict[tuple[str, str], int]] = {}
    for path, rules in record.get(tool, {}).items():
        for rule, value in rules.items():
            if isinstance(value, dict):
                found.setdefault(rule, {}).update({(path, name): size for name, size in value.items()})
    return found


def size_changes(
    tool: str, rule: str, before: dict[tuple[str, str], int], after: dict[tuple[str, str], int]
) -> tuple[list[tuple[str, str]], list[str]]:
    """One size rule's functions above the record, as (file, line), and below or moved, as lines.  A function is known
    by its name where it is the one definition of that name in its file before and after; the rest (renamed, moved,
    sharing a name) are matched largest to largest, which allows any renaming or reordering in which none grew."""
    known = {key for key in before.keys() & after.keys() if "#" not in key[1]}
    over: list[tuple[str, str]] = []
    under: list[str] = []
    for path, name in sorted(known):
        was, now = before[(path, name)], after[(path, name)]
        if now > was:
            over.append((path, f"{tool} {path} {rule} {name}: {now}, recorded {was}"))
        elif now < was:
            under.append(f"{tool} {path} {rule} {name}: {now}, recorded {was}")
    gone = sorted(((size, key) for key, size in before.items() if key not in known), reverse=True)
    came = sorted(((size, key) for key, size in after.items() if key not in known), reverse=True)
    pooled = [
        (path, f"{tool} {path} {rule} {name}: {size}, above the {left} it may have been")
        for (size, (path, name)), left in zip(came, [size for size, _key in gone] + [0] * len(came), strict=False)
        if size > left
    ]
    over += pooled
    if gone != came and not pooled:
        recorded = ", ".join(f"{path} {name} {size}" for size, (path, name) in gone) or "none"
        now_held = ", ".join(f"{path} {name} {size}" for size, (path, name) in came) or "none"
        under.append(f"{tool} {rule} renamed, moved or sharing a name: recorded {recorded}; now {now_held}")
    return over, under


def compare(recorded: dict, current: dict) -> tuple[list[str], list[str], set[tuple[str, str, str]]]:
    """What the tree has above the record, what the record holds that the tree no longer has, and where it is above."""
    over: list[str] = []
    under: list[str] = []
    flagged: set[tuple[str, str, str]] = set()
    for tool in TOOLS:
        old_files, new_files = recorded.get(tool, {}), current.get(tool, {})
        for path in sorted(set(old_files) | set(new_files)):
            old_rules, new_rules = old_files.get(path, {}), new_files.get(path, {})
            for rule in sorted(set(old_rules) | set(new_rules)):
                if isinstance(old_rules.get(rule), dict) or isinstance(new_rules.get(rule), dict):
                    continue  # function sizes, below
                was, now = old_rules.get(rule) or 0, new_rules.get(rule) or 0
                line = f"{tool} {path} {rule} findings: {now}, recorded {was}"
                if now > was:
                    over.append(line)
                    flagged.add((tool, path, rule))
                elif now < was:
                    under.append(line)
        before, after = _sizes(recorded, tool), _sizes(current, tool)
        for rule in sorted(before.keys() | after.keys()):
            above, below = size_changes(tool, rule, before.get(rule, {}), after.get(rule, {}))
            over += [line for _path, line in above]
            flagged |= {(tool, path, rule) for path, _line in above}
            under += below
    return over, under, flagged


def _totals(record: dict, tool: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rules in record.get(tool, {}).values():
        for rule, value in rules.items():
            counts[rule] = counts.get(rule, 0) + (len(value) if isinstance(value, dict) else value)
    return counts


def grown(recorded: dict, current: dict) -> list[str]:
    """What ``--update`` would record above the baseline: more findings of a rule than recorded across the tree, or a
    function bigger than recorded (``size_changes``)."""
    lines: list[str] = []
    for tool in TOOLS:
        was, now = _totals(recorded, tool), _totals(current, tool)
        lines += [
            f"{tool} {rule}: {count} findings, {was.get(rule, 0)} recorded"
            for rule, count in sorted(now.items())
            if count > was.get(rule, 0)
        ]
        before, after = _sizes(recorded, tool), _sizes(current, tool)
        for rule in sorted(after):
            lines += [line for _path, line in size_changes(tool, rule, before.get(rule, {}), after[rule])[0]]
    return lines


#: The package's layers, lowest first: a module imports from its own layer or a lower one, at the top or in a
#: function.  Modules outside them (the package's root, ``_version``, ``_lance_worker``, ``distribution``) keep none,
#: and an import through one of them counts as an import of what it imports.
LAYERS = ("contracts", "core", "vector", "runtime", "adapters", "maintenance")
#: The entry modules the installed hook and server command lines name: composition roots, free to import any layer.
ENTRY_MODULES = frozenset(
    f"adapters/codex/{name}.py"
    for name in ("hook_entry", "mcp_entry", "remote_client", "remote_server", "resident_entry")
)
#: The upward imports the layering keeps, each made in the function that needs it, never while its module loads.  A
#: worker that replays a capture checks it against its host's current installation, which is the adapters' identity
#: code, and the worker's module path is in its wake command, so no composition root above the adapters can be put in
#: front of it.
LAZY_UPWARD = frozenset(
    {
        ("runtime/instance.py", "adapters/clients/authorization.py"),
        ("runtime/instance.py", "adapters/hermes/authorization.py"),
    }
)
_TYPING = ("typing", "typing_extensions")

#: One import: (importer, imported, in a function, line, the module its statement names).  ``imported`` is that
#: module or a package Python runs around it.
Edge = tuple[str, str, bool, int, str]


def _dotted(path: str) -> str:
    parts = path.removesuffix(".py").split("/")
    return ".".join(["scope_recall", *(parts[:-1] if parts[-1] == "__init__" else parts)])


def _bound(tree: ast.Module) -> Counter[str]:
    """How often each name is bound anywhere in a module: a definition, an assignment, an argument, an import, an
    exception or a match."""
    found: Counter[str] = Counter()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found[node.name] += 1
        elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            found[node.id] += 1
        elif isinstance(node, ast.arg):
            found[node.arg] += 1
        elif isinstance(node, ast.alias):
            found[(node.asname or node.name).split(".")[0]] += 1
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            found[node.name] += 1
        elif isinstance(node, ast.MatchMapping) and node.rest:
            found[node.rest] += 1
    return found


def _guard(test: ast.expr, names: set[str], modules: set[str]) -> bool:
    """Whether ``test`` is ``typing.TYPE_CHECKING`` itself, under a name it was imported as or through ``typing``."""
    if isinstance(test, ast.Name):
        return test.id in names
    return (
        isinstance(test, ast.Attribute)
        and test.attr == "TYPE_CHECKING"
        and isinstance(test.value, ast.Name)
        and test.value.id in modules
    )


def typing_only(tree: ast.Module) -> set[int]:
    """The nodes only a type checker runs: the body of ``if TYPE_CHECKING:`` and the ``else`` of ``if not
    TYPE_CHECKING:``, the flag (or ``typing`` itself) imported under a name the module binds nowhere else.  Every other
    branch runs, and so does a test that only contains the flag."""
    bound = _bound(tree)
    names: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in _TYPING:
            names |= {alias.asname or alias.name for alias in node.names if alias.name == "TYPE_CHECKING"}
        elif isinstance(node, ast.Import):
            modules |= {alias.asname or alias.name for alias in node.names if alias.name in _TYPING}
    names = {name for name in names if bound[name] == 1}
    modules = {name for name in modules if bound[name] == 1}
    skipped: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if _guard(node.test, names, modules):
            branch = node.body
        elif isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not):
            branch = node.orelse if _guard(node.test.operand, names, modules) else []
        else:
            continue
        skipped |= {id(inner) for statement in branch for inner in ast.walk(statement)}
    return skipped


def _deferred(tree: ast.Module) -> set[int]:
    """The nodes that run only when called: function and lambda bodies.  A definition's decorators and defaults, and a
    class body, run where the definition does."""
    deferred: set[int] = set()
    todo: list[tuple[ast.AST, bool]] = [(tree, False)]
    while todo:
        node, later = todo.pop()
        if later:
            deferred.add(id(node))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            now: list[ast.AST] = [*node.args.defaults, *(item for item in node.args.kw_defaults if item is not None)]
            if not isinstance(node, ast.Lambda):
                now += node.decorator_list
            body: list[ast.AST] = [node.body] if isinstance(node, ast.Lambda) else list(node.body)
            todo += [(item, later) for item in now] + [(item, True) for item in body]
        else:
            todo += [(child, later) for child in ast.iter_child_nodes(node)]
    return deferred


def _binds(node: ast.stmt) -> set[str]:
    """The names one statement binds in its own scope (not what the blocks under it bind).  An annotation without a
    value binds nothing, and ``from . import x`` binds the submodule x, no other attribute."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        own = isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None
        return {(alias.asname or alias.name).split(".")[0] for alias in node.names if not own or alias.asname}
    if isinstance(node, (ast.Assign, ast.AugAssign)) or (isinstance(node, ast.AnnAssign) and node.value is not None):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return {name.id for target in targets for name in ast.walk(target) if isinstance(name, ast.Name)}
    return set()


def _attributes(statements: list[ast.stmt], skipped: set[int], sure: bool = True) -> tuple[set[str], set[str]]:
    """What a package's own code binds at its top level: (the names bound whenever it runs, those bound on some paths
    only: under if, try, with or a loop).  What only a type checker runs (``skipped``) binds nothing."""
    found: tuple[set[str], set[str]] = (set(), set())
    for node in statements:
        if id(node) in skipped:
            continue
        found[0 if sure else 1].update(_binds(node))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        blocks = [getattr(node, "body", []), getattr(node, "orelse", []), getattr(node, "finalbody", [])]
        for block in blocks + [handler.body for handler in getattr(node, "handlers", [])]:
            found[1].update(*_attributes(block, skipped, sure=False))
    return found


def _package_attributes(tree: ast.Module) -> tuple[set[str], set[str]]:
    """``_attributes`` of a package's code, where a name it also deletes somewhere is bound on some paths only."""
    skipped = typing_only(tree)
    sure, maybe = _attributes(tree.body, skipped)
    deleted = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Delete) and id(node) not in skipped
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    return sure - deleted, maybe | (sure & deleted)


def _dynamic(node: ast.AST) -> tuple[str, list[str]] | None:
    """A literal ``importlib.import_module(...)`` or ``__import__(...)`` of the package's modules: the module, and the
    names a literal ``fromlist`` of ``__import__`` asks for in it."""
    if not isinstance(node, ast.Call) or not node.args or not isinstance(node.args[0], ast.Constant):
        return None
    name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
    value = node.args[0].value
    if (
        name not in ("__import__", "import_module")
        or not isinstance(value, str)
        or value.split(".")[0] != "scope_recall"
    ):
        return None
    fromlist = next((keyword.value for keyword in node.keywords if keyword.arg == "fromlist"), None)
    if fromlist is None and len(node.args) > 3:
        fromlist = node.args[3]
    entries = []
    if name == "__import__" and isinstance(fromlist, (ast.List, ast.Tuple)):
        entries = [
            item.value for item in fromlist.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)
        ]
    return value, entries


def _imports(tree: ast.Module) -> list[tuple[ast.Import | ast.ImportFrom | ast.Call, bool]]:
    """The imports a module runs (statements, and literal dynamic imports of the package's modules), each with
    whether it waits for a call (``_deferred``); what only a type checker runs (``typing_only``) is left out."""
    skipped = typing_only(tree)
    deferred = _deferred(tree)
    found: list[tuple[ast.Import | ast.ImportFrom | ast.Call, bool]] = []
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)) or (isinstance(node, ast.Call) and _dynamic(node)):
            found.append((node, id(node) in deferred))
    return found


def _from(
    base: str, name: str, own: str, paths: dict[str, str], attributes: dict[str, tuple[set[str], set[str]]]
) -> list[str]:
    """What ``from base import name`` loads, as dotted names, as Python takes it: base's own attribute where base's
    code binds name whenever it runs, the submodule base.name where it does not, both where it binds name on some
    paths only.  A package importing from itself has no such attribute yet."""
    submodule = f"{base}.{name}"
    if submodule not in paths:
        return [base]
    sure, maybe = attributes.get(base, (set(), set())) if base != own else (set(), set())
    if name in sure:
        return [base]
    return [base, submodule] if name in maybe else [submodule]


def _requested(
    node: ast.Import | ast.ImportFrom | ast.Call,
    own: str,
    package: list[str],
    paths: dict[str, str],
    attributes: dict[str, tuple[set[str], set[str]]],
) -> list[str]:
    """The dotted names an import asks for: ``_from`` for each name a ``from`` import or a ``fromlist`` names."""
    if isinstance(node, ast.Call):
        module, entries = _dynamic(node) or ("", [])
        return [module, *(loaded for entry in entries for loaded in _from(module, entry, own, paths, attributes))]
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = node.module or ""
    if node.level:
        base = ".".join([*package[: len(package) - node.level + 1], *([node.module] if node.module else [])])
    return [loaded for alias in node.names for loaded in _from(base, alias.name, own, paths, attributes)]


def _loaded(name: str, own: str, paths: dict[str, str]) -> list[str]:
    """The modules importing ``name`` runs, as dotted names: the module it is or is in, and after it each package
    around that module that the importer (``own``) is not itself inside."""
    while name and name not in paths:
        name = name.rpartition(".")[0]
    if not name:
        return []
    parts = name.split(".")
    around = (".".join(parts[:depth]) for depth in range(1, len(parts)))
    return [name, *(package for package in around if package in paths and not f"{own}.".startswith(f"{package}."))]


def import_edges(root: Path, files: Iterable[str]) -> set[Edge]:
    """Each import of one of ``files`` by another (``Edge``).  Importing a module also runs the packages around it
    (``_loaded``); what a type checker alone runs is no import (``typing_only``)."""
    paths = {_dotted(path): path for path in files}
    trees = {path: ast.parse((root / path).read_text(encoding="utf-8")) for path in paths.values()}
    attributes = {
        _dotted(path): _package_attributes(tree) for path, tree in trees.items() if path.endswith("__init__.py")
    }
    edges: set[Edge] = set()
    for path, tree in sorted(trees.items()):
        own = _dotted(path)
        package = own.split(".") if path.endswith("__init__.py") else own.split(".")[:-1]
        for node, lazy in _imports(tree):
            for name in _requested(node, own, package, paths, attributes):
                loaded = _loaded(name, own, paths)
                edges |= {
                    (path, paths[target], lazy, node.lineno, paths[loaded[0]])
                    for target in loaded
                    if paths[target] != path
                }
    return edges


def strongly_connected(graph: dict[str, set[str]]) -> list[list[str]]:
    """The groups of more than one node that reach each other along ``graph``'s edges (Kosaraju, without recursion)."""
    order: list[str] = []
    seen: set[str] = set()
    for start in sorted(graph):
        if start in seen:
            continue
        seen.add(start)
        stack = [(start, iter(sorted(graph[start])))]
        while stack:
            node, children = stack[-1]
            for child in children:
                if child not in seen:
                    seen.add(child)
                    stack.append((child, iter(sorted(graph[child]))))
                    break
            else:
                stack.pop()
                order.append(node)
    reverse: dict[str, set[str]] = {node: set() for node in graph}
    for node, children in graph.items():
        for child in children:
            reverse[child].add(node)
    groups: list[list[str]] = []
    placed: set[str] = set()
    for start in reversed(order):
        if start in placed:
            continue
        placed.add(start)
        group: list[str] = []
        todo = [start]
        while todo:
            node = todo.pop()
            group.append(node)
            for parent in reverse[node] - placed:
                placed.add(parent)
                todo.append(parent)
        if len(group) > 1:
            groups.append(sorted(group))
    return sorted(groups)


def _layer(path: str) -> int | None:
    top = path.split("/")[0].removesuffix(".py")
    return LAYERS.index(top) if top in LAYERS else None


def _reached(path: str, graph: dict[str, set[str]]) -> list[tuple[str, str]]:
    """The modules in a layer that importing ``path`` reaches, each with the module outside the layers it is reached
    through (empty for ``path`` itself)."""
    if _layer(path) is not None:
        return [(path, "")]
    found: list[tuple[str, str]] = []
    seen, todo = {path}, [path]
    while todo:
        for following in sorted(graph[todo.pop()] - seen):
            seen.add(following)
            if _layer(following) is None:
                todo.append(following)
            else:
                found.append((following, path))
    return sorted(found)


def _named_lazy(edge: Edge) -> bool:
    """Whether an import is one of ``LAZY_UPWARD`` made in a function (or a package Python runs around its target)."""
    importer, _imported, lazy, _line, named = edge
    return lazy and (importer, named) in LAZY_UPWARD


def _stars(root: Path, files: list[str]) -> list[str]:
    """The package's star imports of its own modules: the check cannot tell what they bind or which submodules
    ``__all__`` makes them load."""
    return [
        f"star import: {path}:{node.lineno} (name what it imports: the import check cannot follow a star)"
        for path in files
        for node in ast.walk(ast.parse((root / path).read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom)
        and any(alias.name == "*" for alias in node.names)
        and (node.level or (node.module or "").split(".")[0] == "scope_recall")
    ]


def _named_at_load(root: Path, edges: list[Edge]) -> list[str]:
    """A named lazy import (``LAZY_UPWARD``) in a function its module calls while loading: a call or a decorator
    outside every function and lambda body (``_deferred``), in a class body or a default included.  Python would run
    the import with the module half made, which the exception does not cover."""
    problems: list[str] = []
    for importer in sorted({edge[0] for edge in edges if _named_lazy(edge)}):
        tree = ast.parse((root / importer).read_text(encoding="utf-8"))
        later = _deferred(tree) | typing_only(tree)
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and id(node) not in later
        }
        called |= {
            decorator.id
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and id(node) not in later
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Name)
        }
        lines = {edge[3] for edge in edges if edge[0] == importer and _named_lazy(edge)}
        problems += [
            f"named lazy import while its module loads: {importer}:{function.lineno} {function.name} runs before the"
            " module is complete"
            for function in ast.walk(tree)
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
            and function.name in called
            and any(function.lineno <= line <= (function.end_lineno or function.lineno) for line in lines)
        ]
    return problems


def import_problems(root: Path, files: Iterable[str]) -> list[str]:
    """What the package's imports may not do: form a cycle (an import in a function counts: it closes the cycle the
    first time it runs), or reach a higher layer (``LAYERS``), directly or through modules outside the layers, but
    from an entry module or as one of ``LAZY_UPWARD``.  A cycle through one of those is that import itself (the
    target's side imports the runtime, as everything above the runtime does), so they are left out of the cycles; one
    run while its module loads is not, and neither is a star import of the package's own modules."""
    files = sorted(files)
    edges = sorted(import_edges(root, files), key=lambda edge: (edge[0], edge[3], edge[1]))  # by importer and line
    graph: dict[str, set[str]] = {path: set() for path in files}
    for edge in edges:
        if not _named_lazy(edge):
            graph[edge[0]].add(edge[1])
    problems = _stars(root, files) + _named_at_load(root, edges)
    for group in strongly_connected(graph):
        inside = dict.fromkeys(
            f"\n    {importer}:{line} imports {imported}" + (" in a function" if lazy else "")
            for importer, imported, lazy, line, _named in edges
            if importer in group and imported in group
        )
        problems.append(f"import cycle among {len(group)} modules:" + "".join(inside))
    return problems + _upward(edges, graph)


def _upward(edges: list[Edge], graph: dict[str, set[str]]) -> list[str]:
    """Each import of a higher layer than the importer's, but an entry module's and ``LAZY_UPWARD``: one line per
    module it reaches there, the packages Python runs around that module left unsaid."""
    reached: dict[tuple[str, int, bool], set[tuple[str, str]]] = {}
    for edge in edges:
        importer, imported, lazy, line, _named = edge
        low = _layer(importer)
        if low is None or importer in ENTRY_MODULES or _named_lazy(edge):
            continue
        higher = {(module, through) for module, through in _reached(imported, graph) if (_layer(module) or 0) > low}
        if higher:
            reached.setdefault((importer, line, lazy), set()).update(higher)
    problems: list[str] = []
    for (importer, line, lazy), found in reached.items():
        for module, through in sorted({item for item in found if not item[0].endswith("__init__.py")} or found):
            via = f" through {through}" if through else ""
            problems.append(
                f"upward import: {importer}:{line} imports {module}{via}" + (" in a function" if lazy else "")
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--update", action="store_true", help="record the current findings as the baseline")
    parser.add_argument("--allow-more", action="store_true", help="with --update: record more than before")
    args = parser.parse_args(argv)
    check_environment()
    recorded = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    findings = ruff_findings() + pyright_findings()
    sized = {path for _, path, _, rule, _ in findings if rule in SIZE_RULES}
    current = tally(findings, {path: (ROOT / path).read_text(encoding="utf-8") for path in sized})
    if args.update:
        more = grown(recorded, current)
        if more and not args.allow_more:
            print("\n".join(more))
            print("not recorded: the tree is above the baseline; --allow-more records it anyway")
            return 1
        text = json.dumps(current, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
        BASELINE.write_text(text, encoding="utf-8", newline="\n")
        print(f"recorded {len(findings)} findings in {BASELINE.relative_to(ROOT).as_posix()}")
        return 0
    failed = False
    problems = import_problems(ROOT, shipped_python(ROOT))
    if problems:
        print("\n".join(problems))
        print("the package's imports may form no cycle and may not reach a layer above their own (LAYERS in this file)")
        failed = True
    done = _run("ruff", "format", "--check", "--output-format", "concise", ".")
    if done.returncode != 0:
        print(done.stdout.strip() or done.stderr.strip())
        print("run `ruff format .`")
        failed = True
    over, under, flagged = compare(recorded, current)
    if over:
        print("\n".join(over))
        print(
            "\n".join(
                f"  {path}:{row}: {rule} {message}"
                for tool, path, row, rule, message in sorted(findings)
                if (tool, path, rule) in flagged
            )
        )
        print("above the baseline: fix them, or justify one with `# noqa: <rule>` or `# pyright: ignore[<rule>]`")
        failed = True
    if under:
        print("\n".join(under))
        print("below the baseline, or renamed or moved: run `python scripts/quality.py --update` to record it")
        failed = True
    if not failed:
        print(
            f"quality: formatted, imports along the layers, and nothing above the baseline ({len(findings)} findings"
            " recorded)"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
