"""Installed command-line entry point for operating Scope Recall outside the Hermes plugin loader.

The CLI keeps operator actions explicit: install, upgrade, verify, rollback, and maintenance commands prefer dry-run/read-only paths before mutation."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import installer

_SCRIPT_COMMANDS: dict[tuple[str, ...], tuple[str, list[str]]] = {
    ("doctor",): ("doctor.py", []),
    ("dashboard",): ("report.dashboard.py", []),
    ("journal", "digest"): ("journal-digest.py", []),
    ("journal", "recovery"): ("journal.recovery.py", []),
    ("candidates", "report"): ("promote.memory_candidates.py", ["--dry-run"]),
    ("candidates", "apply"): ("promote.memory_candidates.py", ["--apply"]),
    ("vector", "repair", "apply"): ("repair.vector_index.py", ["--apply"]),
    ("vector", "repair"): ("repair.vector_index.py", ["--dry-run"]),
    ("governance", "cleanup"): ("governance.cleanup.py", []),
    ("governance", "rollback"): ("governance.cleanup.py", ["--rollback-batch"]),
    ("governance", "audit-coverage"): ("governance.audit_coverage.py", []),
    ("migrate", "status"): ("migrate.status.py", []),
    ("migrate", "apply"): ("migrate.legacy_hygiene.py", ["--apply"]),
    ("migrate", "legacy"): ("migrate.legacy_hygiene.py", []),
    ("migrate", "openclaw-import"): ("import.openclaw.memory_lancedb_pro.py", []),
    ("rollout", "profiles"): ("rollout.profiles.py", []),
    ("benchmark", "golden"): ("benchmark.golden.py", []),
    ("benchmark", "experience"): ("experience-replay.py", []),
    ("playbooks", "bootstrap"): ("playbook.bootstrap.py", []),
    ("playbooks", "list"): ("playbooks.py", ["list"]),
    ("playbooks", "dedupe"): ("playbooks.py", ["dedupe"]),
    ("playbooks", "review"): ("playbooks.py", ["review"]),
    ("playbooks", "promote"): ("playbooks.py", ["promote"]),
    ("playbooks", "quarantine"): ("playbooks.py", ["quarantine"]),
    ("playbooks", "supersede"): ("playbooks.py", ["supersede"]),
}

_HELP = """hermes-scope-recall: Scope Recall operator CLI

Usage:
  hermes-scope-recall install [installer options]
  hermes-scope-recall upgrade [installer options]
  hermes-scope-recall rollback --backup-dir <path> [installer options]
  hermes-scope-recall verify [verify options]
  hermes-scope-recall doctor [doctor options]
  hermes-scope-recall dashboard [dashboard options]
  hermes-scope-recall journal digest [digest options]
  hermes-scope-recall journal recovery [recovery options]
  hermes-scope-recall candidates report [candidate options]
  hermes-scope-recall candidates apply [candidate options]
  hermes-scope-recall vector repair [vector options]          # dry-run by default
  hermes-scope-recall vector repair apply [vector options]    # rebuilds with backup
  hermes-scope-recall governance cleanup [cleanup options]
  hermes-scope-recall governance rollback [rollback options]
  hermes-scope-recall governance audit-coverage [audit options]
  hermes-scope-recall migrate status [migration options]
  hermes-scope-recall migrate apply [migration options]
  hermes-scope-recall migrate legacy [migration options]
  hermes-scope-recall migrate openclaw-import [import options]
  hermes-scope-recall rollout profiles [rollout options]
  hermes-scope-recall benchmark golden [benchmark options]
  hermes-scope-recall benchmark experience [experience replay options]
  hermes-scope-recall playbooks bootstrap [bootstrap options]
  hermes-scope-recall playbooks list [playbook options]
  hermes-scope-recall playbooks review --id <id> [review options]
  hermes-scope-recall playbooks dedupe [dedupe options]
  hermes-scope-recall playbooks promote --id <id> [review options]
  hermes-scope-recall playbooks quarantine --id <id> [review options]
  hermes-scope-recall playbooks supersede --id <id> --superseded-by <id> [review options]

Existing script options are forwarded unchanged. Use --help after any command
for that command's detailed options.
"""


def _scripts_dir() -> Path:
    return Path(__file__).resolve().parent / "scripts"


def _run_script(script_name: str, forwarded_args: list[str]) -> int:
    script_path = _scripts_dir() / script_name
    if not script_path.is_file():
        print(f"scope-recall error: script not found: {script_path}", file=sys.stderr)
        return 2
    completed = subprocess.run([sys.executable, str(script_path), *forwarded_args], check=False)
    return int(completed.returncode)


def _merge_injected_args(injected: list[str], forwarded: list[str]) -> list[str]:
    explicit_apply = "--apply" in forwarded
    merged: list[str] = []
    for arg in injected:
        if arg in forwarded:
            continue
        if arg == "--dry-run" and explicit_apply:
            continue
        merged.append(arg)
    return [*merged, *forwarded]


def _match_script_command(argv: list[str]) -> tuple[str, list[str]] | None:
    for key in sorted(_SCRIPT_COMMANDS, key=len, reverse=True):
        if tuple(argv[: len(key)]) == key:
            script_name, injected = _SCRIPT_COMMANDS[key]
            return script_name, _merge_injected_args(injected, argv[len(key) :])
    return None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print(_HELP)
        return 0
    if args[0] in {"install", "verify", "upgrade", "rollback"}:
        return installer.main(args)
    matched = _match_script_command(args)
    if matched is not None:
        script_name, forwarded = matched
        return _run_script(script_name, forwarded)
    print(f"scope-recall error: unknown command: {' '.join(args)}", file=sys.stderr)
    print(_HELP, file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
