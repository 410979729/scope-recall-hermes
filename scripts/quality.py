#!/usr/bin/env python3
"""Hold the tree to its recorded lint and type findings.

    python scripts/quality.py                 # formatted, and no finding scripts/quality.baseline.json does not hold
    python scripts/quality.py --update        # record the current findings there

Run it in an environment made from the lock: it carries the ruff and pyright versions the dev extra pins, the
third-party packages pyright reads, and this tree installed as ``scope_recall`` (pyright reads the absolute
``scope_recall`` imports from the installed copy, so the check refuses a copy that is not this tree):

    uv sync --locked --no-editable --reinstall-package hermes-scope-recall --extra lancedb --extra codex --extra dev
    uv run --no-sync python scripts/quality.py

The check fails when ``ruff format`` would change a file, when a file has more findings of a rule than recorded, or
when a function is over a size limit (C901, PLR0911-PLR0915) it was not over, or by more than recorded.  It also fails
when a file has fewer findings than recorded, so that the baseline only goes down: ``--update`` records them, and
refuses to record more findings of a rule, or a recorded function grown further, unless ``--allow-more`` is given.
pyright runs as Linux and as Windows, and a finding either reports counts once.
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
    for path in sorted(installed.rglob("*.py")):
        source = ROOT / path.relative_to(installed)
        if not source.is_file() or source.read_bytes() != path.read_bytes():
            stale = path.relative_to(installed).as_posix()
            raise SystemExit(f"the installed scope_recall is not this tree ({stale} differs): reinstall it, as above")


def functions(source: str) -> list[tuple[int, int, str]]:
    """(first line, last line, qualified name) of every function in a module, decorators included."""
    found: list[tuple[int, int, str]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + child.name
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
    """What the tree has beyond the record, what the record holds that the tree no longer has, and where it is over."""
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


def grown(recorded: dict, current: dict) -> list[str]:
    """Rules with more findings than recorded, and recorded functions over a limit by more than before."""
    lines: list[str] = []
    for tool in TOOLS:
        was, now = _totals(recorded, tool), _totals(current, tool)
        lines += [
            f"{tool} {rule}: {count} findings, {was.get(rule, 0)} recorded"
            for rule, count in sorted(now.items())
            if count > was.get(rule, 0)
        ]
        for path, rules in current.get(tool, {}).items():
            for rule, sizes in rules.items():
                before = recorded.get(tool, {}).get(path, {}).get(rule)
                if isinstance(sizes, dict) and isinstance(before, dict):
                    lines += [
                        f"{tool} {path} {rule} {name}: {size}, recorded {before[name]}"
                        for name, size in sorted(sizes.items())
                        if size > before.get(name, size)
                    ]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--update", action="store_true", help="record the current findings as the baseline")
    parser.add_argument("--allow-more", action="store_true", help="with --update: record more findings than before")
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
            print("not recorded: the tree has more findings than the baseline; --allow-more records them anyway")
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
        print("new findings: fix them, or justify one with `# noqa: <rule>` or `# pyright: ignore[<rule>]`")
        failed = True
    if under:
        print("\n".join(under))
        print("fewer findings than recorded: run `python scripts/quality.py --update` to lower the baseline")
        failed = True
    if not failed:
        print(f"quality: formatted, and none of the {len(findings)} findings is new")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
