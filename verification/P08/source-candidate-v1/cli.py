"""Installed command-line entry point for operating Scope Recall outside the Hermes plugin loader.

The CLI keeps operator actions explicit: install, upgrade, verify, rollback, and maintenance commands prefer dry-run/read-only paths before mutation."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import installer

_SCRIPT_COMMANDS: dict[tuple[str, ...], tuple[str, list[str]]] = {
    ("doctor",): ("doctor.py", []),
    ("dashboard",): ("report.dashboard.py", []),
    ("journal", "digest"): ("journal-digest.py", []),
    ("journal", "recovery"): ("journal.recovery.py", []),
    ("journal", "source-restore"): ("journal.source_restore.py", []),
    ("lexical", "plan"): ("migrate.lexical_index.py", []),
    ("lexical", "build"): ("migrate.lexical_index.py", ["--apply"]),
    ("lexical", "activate"): (
        "migrate.lexical_index.py",
        ["--apply", "--activate"],
    ),
    ("lexical", "rollback"): (
        "migrate.lexical_index.py",
        ["--apply", "--rollback"],
    ),
    ("candidates", "report"): ("promote.memory_candidates.py", ["--dry-run"]),
    ("candidates", "list"): ("memory.browser.py", ["candidates", "list"]),
    ("candidates", "promote"): ("candidate.review.py", ["promote", "--dry-run"]),
    ("candidates", "archive"): ("candidate.review.py", ["archive", "--dry-run"]),
    ("candidates", "supersede"): ("candidate.review.py", ["supersede", "--dry-run"]),
    ("candidates", "apply"): ("promote.memory_candidates.py", ["--apply"]),
    ("vector", "generation", "activate"): ("migrate.vector_generation.py", ["--activate-existing-ready"]),
    ("vector", "generation", "build"): ("migrate.vector_generation.py", ["--apply"]),
    ("vector", "generation", "plan"): ("migrate.vector_generation.py", ["--dry-run"]),
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
    ("memories", "list"): ("memory.browser.py", ["memories", "list"]),
    ("memories", "inspect"): ("memory.browser.py", ["memories", "inspect"]),
    ("recall", "explain"): ("memory.browser.py", ["recall", "explain"]),
    ("playbooks", "bootstrap"): ("playbook.bootstrap.py", []),
    ("playbooks", "list"): ("playbooks.py", ["list"]),
    ("playbooks", "dedupe"): ("playbooks.py", ["dedupe"]),
    ("playbooks", "review"): ("playbooks.py", ["review"]),
    ("playbooks", "promote"): ("playbooks.py", ["promote"]),
    ("playbooks", "skill-candidates"): ("skill.bridge.py", ["skill-candidates", "--dry-run"]),
    ("playbooks", "quarantine"): ("playbooks.py", ["quarantine"]),
    ("playbooks", "receipts"): ("playbooks.py", ["receipts"]),
    ("playbooks", "supersede"): ("playbooks.py", ["supersede"]),
}

_HELP = """hermes-scope-recall: Scope Recall operator CLI

Usage:
  hermes-scope-recall install [installer options]
  hermes-scope-recall update [--hermes-home <path>] [--json]
  hermes-scope-recall upgrade [installer options]
  hermes-scope-recall rollback --backup-dir <path> [installer options]
  hermes-scope-recall managed-upgrade auto [--hermes-home <path>]
  hermes-scope-recall managed-upgrade prepare --hermes-home <path> --candidate <path> --expected-tree-sha256 <sha256>
  hermes-scope-recall managed-upgrade worker --hermes-home <path> --operation-id <id>
  hermes-scope-recall managed-upgrade status --hermes-home <path> --operation-id <id>
  hermes-scope-recall managed-upgrade resume --hermes-home <path> --operation-id <id>
  hermes-scope-recall verify [verify options]
  hermes-scope-recall doctor [doctor options]
  hermes-scope-recall dashboard [dashboard options]
  hermes-scope-recall journal digest [digest options]
  hermes-scope-recall journal recovery [recovery options]
  hermes-scope-recall journal source-restore [source-restore options]
  hermes-scope-recall lexical plan [lexical options]      # zero-write status
  hermes-scope-recall lexical build --maintenance-confirmed [lexical options]
  hermes-scope-recall lexical activate --expected-current legacy --maintenance-confirmed
  hermes-scope-recall lexical rollback --expected-current <generation> --maintenance-confirmed
  hermes-scope-recall candidates report [candidate options]
  hermes-scope-recall candidates list --json [browser options]
  hermes-scope-recall candidates promote --id <id> --dry-run --json [review options]
  hermes-scope-recall candidates archive --id <id> --dry-run --json [review options]
  hermes-scope-recall candidates supersede --id <id> --superseded-by <id> --dry-run --json [review options]
  hermes-scope-recall candidates apply [candidate options]
  hermes-scope-recall vector generation plan [generation options]      # zero-write plan
  hermes-scope-recall vector generation build [generation options]     # build READY shadow
  hermes-scope-recall vector generation activate --generation-id <id>  # explicit CAS switch
  hermes-scope-recall vector repair [vector options]          # legacy dry-run repair
  hermes-scope-recall vector repair apply [vector options]    # legacy repair path
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
  hermes-scope-recall memories list --target project --json [browser options]
  hermes-scope-recall memories inspect --id <id> --json [browser options]
  hermes-scope-recall recall explain --query <query> --json [browser options]
  hermes-scope-recall playbooks bootstrap [bootstrap options]
  hermes-scope-recall playbooks list [playbook options]
  hermes-scope-recall playbooks review --id <id> [review options]
  hermes-scope-recall playbooks dedupe [dedupe options]
  hermes-scope-recall playbooks skill-candidates --dry-run --json [bridge options]
  hermes-scope-recall playbooks promote --id <id> [review options]
  hermes-scope-recall playbooks quarantine --id <id> [review options]
  hermes-scope-recall playbooks receipts [--apply] [--include-failed]
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


def _active_plugin_home() -> Path:
    """Resolve the exact Hermes home that loaded this plugin CLI."""

    plugin_dir = Path(__file__).resolve().parent
    if plugin_dir.name != "scope-recall" or plugin_dir.parent.name != "plugins":
        raise ValueError("scope_recall_cli_not_loaded_from_active_plugin")
    return plugin_dir.parent.parent.resolve()


def register_cli(parent_parser: argparse.ArgumentParser) -> None:
    """Expose the zero-choice updater through ``hermes scope-recall``."""

    commands = parent_parser.add_subparsers(
        dest="scope_recall_command_name",
        required=True,
    )
    update = commands.add_parser(
        "update",
        help="safely update to the latest official stable Scope Recall",
    )
    update.set_defaults(func=scope_recall_command)

    for name in ("update-status", "update-resume"):
        command = commands.add_parser(name)
        command.add_argument("--operation-id", required=True)
        command.set_defaults(func=scope_recall_command)


def scope_recall_command(args: argparse.Namespace) -> int:
    """Handle the active-plugin CLI without asking the model for decisions."""

    from . import managed_upgrade

    try:
        home = _active_plugin_home()
        command = str(args.scope_recall_command_name)
        if command == "update":
            payload = managed_upgrade.auto_update(
                hermes_home=home,
            )
        elif command == "update-status":
            payload = managed_upgrade.status(
                hermes_home=home,
                operation_id=str(args.operation_id),
            )
        elif command == "update-resume":
            payload = managed_upgrade.resume(
                hermes_home=home,
                operation_id=str(args.operation_id),
            )
        else:
            raise managed_upgrade.ManagedUpgradeError("unknown_command")
    except OSError:
        payload = managed_upgrade.failure_payload(
            "scope_recall_cli_home_unavailable"
        )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    except (ValueError, managed_upgrade.ManagedUpgradeError) as exc:
        reason = getattr(exc, "reason_code", str(exc))
        payload = managed_upgrade.failure_payload(str(reason))
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print(_HELP)
        return 0
    if args[0] in {"install", "verify", "upgrade", "rollback"}:
        return installer.main(args)
    if args[0] == "update":
        from . import managed_upgrade

        return managed_upgrade.main(["auto", *args[1:]])
    if args[0] == "managed-upgrade":
        from . import managed_upgrade

        return managed_upgrade.main(args)
    matched = _match_script_command(args)
    if matched is not None:
        script_name, forwarded = matched
        return _run_script(script_name, forwarded)
    print(f"scope-recall error: unknown command: {' '.join(args)}", file=sys.stderr)
    print(_HELP, file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
