#!/usr/bin/env python3
"""Find (and with --apply remove) top-level definitions nothing reaches.

    python scripts/dead_code.py core/gating.py core/claims.py      # report
    python scripts/dead_code.py --apply core/gating.py             # delete

For each module given, a top-level name is a *root* when another shipped
module, a test, a probe, or a config file (json/yaml/toml/cmd) mentions it,
or when it is ``main`` or a dunder.  Names a root reaches inside the module
(through ``ast.Name`` and ``ast.Attribute`` references) are live; every other
top-level function, class or assignment is dead.  ``--apply`` removes the dead
definitions and prunes ``__all__``.  Run it again until it reports nothing:
deleting one definition can orphan the helpers only it used.

The shipped module set is ``packaging/v11-module-allowlist.json``, so this
never reasons about modules the wheel does not carry.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_KEEP = ("main",)


def _shipped() -> list[str]:
    listed = json.loads((ROOT / "packaging" / "v11-module-allowlist.json").read_text(encoding="utf-8"))
    return listed["python_modules"] + ["_lance_worker.py", "scripts/check.py", "packaging_hooks/module_inventory.py"]


def _corpus(exclude: str) -> str:
    """Everything that can name a symbol, except the module under analysis."""
    parts = []
    for relative in _shipped():
        if relative != exclude and (ROOT / relative).is_file():
            parts.append((ROOT / relative).read_text(encoding="utf-8", errors="replace"))
    for folder in ("tests", "probes"):
        for path in (ROOT / folder).rglob("*.py"):
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
    for pattern in ("*.json", "*.yaml", "*.yml", "*.toml", "*.cmd"):
        for path in ROOT.rglob(pattern):
            if not str(path.relative_to(ROOT)).startswith(("verification", "build", ".git")):
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def _definitions(tree: ast.Module) -> dict[str, ast.stmt]:
    nodes: dict[str, ast.stmt] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nodes[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id != "__all__":
                    nodes[target.id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            nodes[node.target.id] = node
    return nodes


def _references(node: ast.AST, known: set[str]) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names & known


def dead_definitions(relative: str) -> tuple[ast.Module, list[tuple[str, ast.stmt]]]:
    source = (ROOT / relative).read_text(encoding="utf-8")
    tree = ast.parse(source)
    nodes = _definitions(tree)
    corpus = _corpus(relative)
    live = {name for name in nodes if name.startswith("__") or name in _KEEP or re.search(r"\b" + re.escape(name) + r"\b", corpus)}
    pending = list(live)
    definitions = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign, ast.Import, ast.ImportFrom)
    for node in tree.body:
        if not isinstance(node, definitions):
            for name in _references(node, set(nodes)) - live:
                live.add(name)
                pending.append(name)
    while pending:
        for name in _references(nodes[pending.pop()], set(nodes)) - live:
            live.add(name)
            pending.append(name)
    return tree, [(name, nodes[name]) for name in nodes if name not in live]


def remove(relative: str, tree: ast.Module, dead: list[tuple[str, ast.stmt]]) -> None:
    path = ROOT / relative
    lines = path.read_text(encoding="utf-8").split("\n")
    drop: set[int] = set()
    for _, node in dead:
        start = min([d.lineno for d in getattr(node, "decorator_list", [])] + [node.lineno])
        drop.update(range(start - 1, node.end_lineno))
        after = node.end_lineno
        while after < len(lines) and not lines[after].strip():
            drop.add(after)
            after += 1
    dead_names = {name for name, _ in dead}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets) and isinstance(node.value, (ast.List, ast.Tuple)):
            kept = [e.value for e in node.value.elts if isinstance(e, ast.Constant) and e.value not in dead_names]
            drop.update(range(node.lineno - 1, node.end_lineno))
            lines[node.lineno - 1] = "__all__ = [" + ", ".join(repr(k) for k in kept) + "]"
            drop.discard(node.lineno - 1)
    text = "\n".join(line for index, line in enumerate(lines) if index not in drop)
    path.write_text(re.sub(r"\n{4,}", "\n\n\n", text), encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="delete the dead definitions")
    parser.add_argument("modules", nargs="+", help="module paths relative to the repository root")
    args = parser.parse_args()
    total = 0
    for relative in args.modules:
        tree, dead = dead_definitions(relative)
        if not dead:
            continue
        lines = sum(node.end_lineno - node.lineno + 1 for _, node in dead)
        total += lines
        print(f"{lines:5d} {relative}: " + ", ".join(f"{name}({node.end_lineno - node.lineno + 1})" for name, node in dead))
        if args.apply:
            remove(relative, tree, dead)
    print("dead lines:", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
