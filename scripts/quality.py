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
size.  The check fails when ``ruff format`` would change a file, when a file has more findings of a rule than
recorded, or when a function is over a size limit it was not over, or by more than recorded.  A count says how many,
not which: one finding fixed and another of the same rule added in the same file passes.  It also fails when a file
has fewer findings than recorded, so that the baseline only goes down: ``--update`` records them.  ``--update``
refuses, unless ``--allow-more`` is given, to record more findings of a rule, a function grown under its name, or
renamed and moved functions bigger than those that went.  pyright runs as Linux and as Windows, and a finding either
reports counts once.
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


def install_problem(installed: Path, root: Path) -> str | None:
    """Why an installed package is not this tree's, or None.  It must hold exactly the Python files the wheel ships
    (``packaging/v11-module-allowlist.json``), each as this tree has it, and no type stub besides: pyright reads a stub
    in place of its module."""
    allowlist = json.loads((root / "packaging" / "v11-module-allowlist.json").read_text(encoding="utf-8"))
    shipped = {*allowlist["python_modules"], *(path for path in allowlist["package_data"] if path.endswith(".py"))}
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
    order, so that each is held to its own size."""
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
                old, new = old_rules.get(rule), new_rules.get(rule)
                if isinstance(old, dict) or isinstance(new, dict):
                    pairs = [
                        (f" {name}", (old or {}).get(name, 0), (new or {}).get(name, 0))
                        for name in sorted(set(old or {}) | set(new or {}))
                    ]
                else:
                    pairs = [(" findings", old or 0, new or 0)]
                for label, was, now in pairs:
                    line = f"{tool} {path} {rule}{label}: {now}, recorded {was}"
                    if now > was:
                        over.append(line)
                        flagged.add((tool, path, rule))
                    elif now < was:
                        under.append(line)
    return over, under, flagged


def _totals(record: dict, tool: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rules in record.get(tool, {}).values():
        for rule, value in rules.items():
            counts[rule] = counts.get(rule, 0) + (len(value) if isinstance(value, dict) else value)
    return counts


def _sizes(record: dict, tool: str) -> dict[str, dict[tuple[str, str], int]]:
    """Each size rule's functions, by (file, name)."""
    found: dict[str, dict[tuple[str, str], int]] = {}
    for path, rules in record.get(tool, {}).items():
        for rule, value in rules.items():
            if isinstance(value, dict):
                found.setdefault(rule, {}).update({(path, name): size for name, size in value.items()})
    return found


def grown(recorded: dict, current: dict) -> list[str]:
    """What ``--update`` would record above the baseline: more findings of a rule, a function bigger under its name,
    or functions under new names (renamed or moved) bigger than those whose names went.  Those are matched largest to
    largest, which allows any renaming in which none grew; like a count, it cannot tell a renamed function from a new
    one that takes the place of one brought under the limit."""
    lines: list[str] = []
    for tool in TOOLS:
        was, now = _totals(recorded, tool), _totals(current, tool)
        lines += [
            f"{tool} {rule}: {count} findings, {was.get(rule, 0)} recorded"
            for rule, count in sorted(now.items())
            if count > was.get(rule, 0)
        ]
        before_sizes, after_sizes = _sizes(recorded, tool), _sizes(current, tool)
        for rule, after in sorted(after_sizes.items()):
            before = before_sizes.get(rule, {})
            lines += [
                f"{tool} {path} {rule} {name}: {size}, recorded {before[(path, name)]}"
                for (path, name), size in sorted(after.items())
                if (path, name) in before and size > before[(path, name)]
            ]
            gone = sorted((size for key, size in before.items() if key not in after), reverse=True)
            came = sorted(((size, key) for key, size in after.items() if key not in before), reverse=True)
            lines += [
                f"{tool} {path} {rule} {name}: {size}, bigger than the {left} it may have been before a rename or move"
                for (size, (path, name)), left in zip(came, gone, strict=False)
                if size > left
            ]
    return lines


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
        print("fewer findings than recorded: run `python scripts/quality.py --update` to lower the baseline")
        failed = True
    if not failed:
        print(f"quality: formatted, and nothing above the baseline ({len(findings)} findings recorded)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
