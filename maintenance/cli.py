"""CLI entry for bounded v1.1 install, doctor, and uninstall flows."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Callable

from .backup import BackupError
from .doctor import run_doctor
from .install import InstallError, apply_install, apply_uninstall, plan_install, plan_uninstall
from .install_common import _absolute
from .install_hermes import LOCAL_PLATFORM_CHOICES
from .rollback import RollbackError


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _path(value: str, field: str) -> Path:
    return _absolute(value, field, error=SystemExit).resolve()


def _optional_path(value: str | None, field: str) -> Path | None:
    return _path(value, field) if value else None


# Sub-commands with their own parser: the first token routes to them before the
# maintenance parser runs, and the stubs registered below only make ``--help``
# list them.  Their modules load on demand to keep the common commands quick.
def _upgrade_cli(argv: list[str]) -> int:
    from .upgrade_cli import main

    return main(argv)


def _package_upgrade(argv: list[str]) -> int:
    from .package_upgrade import main

    return main(argv[1:])


def _autostart(argv: list[str]) -> int:
    from .autostart import main

    return main(argv[1:])


def _shared(argv: list[str]) -> int:
    from .shared import main

    return main(argv)


_DELEGATED: dict[str, tuple[str, Callable[[list[str]], int]]] = {
    "setup": ("agent-operated fresh install/update/migration routing", _upgrade_cli),
    "migrate": ("prepare, resume, verify and index a legacy migration job", _upgrade_cli),
    "package-upgrade": ("offline wheel replacement after stopping all target writers", _package_upgrade),
    "autostart": ("plan, enable, pause or remove a bounded Windows background wake", _autostart),
    "init-shared": ("create a shared store, the one store every agent attaches to", _shared),
    "attach": ("make a host's home an entry of a shared store, with the grants it had", _shared),
    "detach": ("stop a home being an entry of a shared store; its memories stay", _shared),
    "adopt": ("record the directory a copied shared store now lives in", _shared),
    "entries": ("list a shared store's entries and when each was last heard from", _shared),
    "import-entry": ("copy an entry's own store, moved aside at attach, into its shared store", _shared),
}


def _run_core(
    args: argparse.Namespace,
    call: Callable[[Any, Any], dict],
    *,
    failed: Callable[[dict], bool] = lambda receipt: False,
) -> int:
    """Open the bound core for one maintenance call; contract and config failures exit 2."""
    from ..contracts import ContractError
    from ..core import CoreConfig, MemoryCore
    from ..runtime.worker_entry import load_config

    try:
        config = load_config(_path(args.config, "config"))
        receipt = call(MemoryCore(CoreConfig(config.binding)), config)
    except ContractError as exc:
        _emit({"status": "error", "code": exc.code, "field": exc.field})
        return 2
    except (ValueError, OSError):
        _emit({"status": "error", "code": "CONFIG_INVALID"})
        return 2
    _emit(receipt)
    return 1 if failed(receipt) else 0


def _add_page_arguments(parser: argparse.ArgumentParser, *, limit: int) -> None:
    parser.add_argument("--config", required=True)
    parser.add_argument("--after-ref", default="")
    parser.add_argument("--limit", type=int, default=limit)


def _add_repair_arguments(parser: argparse.ArgumentParser) -> None:
    _add_page_arguments(parser, limit=16)


def _repair_claim_frames(args: argparse.Namespace) -> int:
    return _run_core(
        args,
        lambda core, config: core.repair_claim_frames(
            config.context(), after_ref=args.after_ref, limit=args.limit, remaining_seconds=config.request_seconds
        ),
        failed=lambda receipt: bool(receipt["errors"]),
    )


def _add_requalify_arguments(parser: argparse.ArgumentParser) -> None:
    _add_page_arguments(parser, limit=16)
    parser.add_argument("--apply", action="store_true", help="write the new verdicts; without it nothing is changed")


def _requalify(args: argparse.Namespace) -> int:
    return _run_core(
        args,
        lambda core, config: core.requalify_claims(
            config.context(),
            after_ref=args.after_ref,
            limit=args.limit,
            dry_run=not args.apply,
            remaining_seconds=config.request_seconds,
        ),
    )


def _retire_rootless(args: argparse.Namespace) -> int:
    return _run_core(
        args,
        lambda core, config: core.retire_rootless_proposals(
            config.context(),
            after_ref=args.after_ref,
            limit=args.limit,
            dry_run=not args.apply,
            remaining_seconds=config.request_seconds,
        ),
    )


def _add_retry_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True)
    parser.add_argument("--limit", type=int, default=64)
    parser.add_argument(
        "--include-terminal",
        action="store_true",
        help="also re-run by-design terminal failures (derivation_invalid, budget_checked)",
    )
    parser.add_argument("--apply", action="store_true", help="write the re-opened rows; without it nothing is changed")


def _retry_failures(args: argparse.Namespace) -> int:
    return _run_core(
        args,
        lambda core, config: core.retry_failed_work(
            config.context(),
            limit=args.limit,
            include_terminal=args.include_terminal,
            dry_run=not args.apply,
            remaining_seconds=config.request_seconds,
        ),
    )


def _add_backup_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--manifest")


def _backup(args: argparse.Namespace) -> int:
    from .backup import backup_sqlite

    target = _path(args.output, "output")
    manifest = _path(args.manifest, "manifest") if args.manifest else target.with_suffix(target.suffix + ".json")
    _emit(backup_sqlite(_path(args.database, "database"), target, manifest=manifest))
    return 0


def _add_rollback_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--current-db", required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--output")
    parser.add_argument("--apply", action="store_true")


def _rollback(args: argparse.Namespace) -> int:
    from .rollback import plan_rollback, rollback_to_verified_snapshot

    current, snapshot = _path(args.current_db, "current_db"), _path(args.snapshot, "snapshot")
    output = _optional_path(args.output, "output")
    result = plan_rollback(current, snapshot, destination=output)
    if args.apply:
        result = rollback_to_verified_snapshot(current, snapshot, destination=output)
    _emit(result)
    return 0


def _add_install_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", required=True, choices=("hermes", "codex", "claude-code"))
    parser.add_argument("--target-plugin-dir", required=True)
    parser.add_argument("--instance-root", required=True)
    parser.add_argument("--project-root", default=None,
                        help="the workspace a Codex installation of its own maps; a client attached to a shared "
                        "store has none")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument(
        "--agent-workspace",
        default=None,
        help="Hermes audience workspace; defaults to hermes to match the host memory-provider init contract. Codex rejects this flag.",
    )
    parser.add_argument("--python", required=True)
    parser.add_argument(
        "--env-file",
        default=None,
        help="Codex and Claude Code: absolute file with the credential names the runtime config declares; "
        "written into .mcp.json and hooks.json because the client starts those processes without them.",
    )
    parser.add_argument(
        "--local-platform",
        action="append",
        default=[],
        choices=LOCAL_PLATFORM_CHOICES,
        help="Hermes only, repeatable: approve a host surface that names no user (the Desktop chat panel, "
        "hermes --tui) as the owner's own, the way the CLI is. Without it such a session is refused. Approve one "
        "only where everyone who can reach that surface without logging in is the owner.",
    )
    parser.add_argument(
        "--test-mode",
        action="store_true",
        help="use isolated TEST binding semantics; omitted for production installation",
    )


def _install_plan(args: argparse.Namespace):
    return plan_install(
        target_plugin_dir=_path(args.target_plugin_dir, "target_plugin_dir"),
        instance_root=_path(args.instance_root, "instance_root"),
        project_root=_optional_path(args.project_root, "project_root"),
        agent_id=args.agent_id,
        python_executable=_path(args.python, "python"),
        host=args.host,
        test_mode=args.test_mode,
        agent_workspace=args.agent_workspace,
        env_file=_optional_path(args.env_file, "env_file"),
        local_platforms=tuple(args.local_platform),
    )


def _plan_install(args: argparse.Namespace) -> int:
    plan = _install_plan(args)
    _emit(plan.to_dict())
    return 1 if plan.conflicts else 0


def _apply_install(args: argparse.Namespace) -> int:
    _emit(apply_install(_install_plan(args)).to_dict())
    return 0


def _add_doctor_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", required=True, choices=("hermes", "codex", "claude-code"))
    parser.add_argument("--instance-root", required=True)
    parser.add_argument("--python")


def _doctor(args: argparse.Namespace) -> int:
    report = run_doctor(
        host=args.host,
        instance_root=_path(args.instance_root, "instance_root"),
        python_executable=_optional_path(args.python, "python"),
    )
    _emit(report.to_dict())
    return 0 if report.status == "ok" else 1


def _restamp_header(database: Path, recorded: int, *, timeout: float) -> bool:
    """Write ``recorded`` into the header if, inside the write transaction, it is still what the store records."""
    import sqlite3
    from contextlib import closing

    from scope_recall.core.schema import stale_header_schema

    # mode=rw: a store that disappeared meanwhile is an error, never a new empty file.
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True, timeout=timeout,
                                 isolation_level=None)) as db:
        db.execute("BEGIN IMMEDIATE")
        if stale_header_schema(db) != recorded:
            db.execute("ROLLBACK")
            return False
        db.execute(f"PRAGMA user_version={int(recorded)}")
        db.execute("COMMIT")
    return True


def _tables_not_in_schema(database: Path) -> dict[str, int]:
    """Tables a current store holds that this release's schema does not create, with their rows.

    Read after a restamp once the store is at this release's schema, when every table an older
    step used is gone: what is left is another program's.  A 2.0 process that stamped the header
    may have captured turns into its own tables; those rows are not part of this store and would
    otherwise stay in the file unseen.
    """
    import sqlite3
    from contextlib import closing

    from scope_recall.core.schema import STATEMENTS

    with closing(sqlite3.connect(":memory:")) as scratch:
        for statement in STATEMENTS:
            scratch.execute(statement)
        known = {row[0] for row in scratch.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as db:
        names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                 if row[0] not in known and not row[0].startswith("sqlite_")]
        return {name: db.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                for name in names}


def _add_upgrade_store_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", required=True, choices=("hermes", "codex"))
    parser.add_argument("--instance-root", required=True)
    parser.add_argument("--backup-dir", help="take a verified snapshot of memory.sqlite3 here before touching it")
    parser.add_argument("--wait-seconds", type=float, default=30.0,
                        help="how long to wait for a running worker to release the store (at most 30)")


def _upgrade_store(args: argparse.Namespace) -> int:
    """Bring one store forward to this release's schema now, with the budget no hook has.

    A store at a known older schema is otherwise brought forward by its first
    ordinary open, except that a store above 100 MB waits for a caller with a
    minute of budget (the worker's pass or apply-install), because the 1109
    step rebuilds the lexical index: 95 s on a 1.4 GB store.  This is that
    caller, for an operator who installed the wheel and wants the upgrade
    done now, with a snapshot first.  The worker must not be running: its
    lease is waited for, never taken.
    """
    import sqlite3
    import time
    from datetime import datetime, timezone

    from scope_recall.contracts import ContractError
    from scope_recall.core.schema import SCHEMA_VERSION, UPGRADE_CHAIN
    from scope_recall.core.storage import SQLiteStorage
    from scope_recall.core.writer_lease import TruthWriterBusyError
    from .backup import backup_sqlite
    from .doctor import _journal_mode, _load_binding, _recorded_schema_under_stale_header, _schema_on_disk

    instance = _path(args.instance_root, "instance_root")
    binding, data_directory = _load_binding(args.host, instance)
    database = data_directory / "memory.sqlite3"
    before = _schema_on_disk(database)
    recorded = _recorded_schema_under_stale_header(database)
    result: dict[str, Any] = {"schema_before": before, "schema_target": SCHEMA_VERSION}
    if recorded is None and before == SCHEMA_VERSION:
        result.update(status="current", journal_mode=_journal_mode(database))
        _emit(result)
        return 0
    if recorded is None and before not in UPGRADE_CHAIN:
        result.update(status="unsupported", error="schema_not_in_upgrade_chain")
        _emit(result)
        return 2
    if not args.backup_dir:
        result.update(status="not_upgraded", error="backup_required",
                      hint="provide --backup-dir for the verified pre-upgrade snapshot")
        _emit(result)
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot = _path(args.backup_dir, "backup_dir") / f"memory-{before}-{stamp}.sqlite3"
    backup_sqlite(database, snapshot, manifest=snapshot.with_suffix(".json"))
    result["backup"] = str(snapshot)
    wait = min(max(float(args.wait_seconds), 0.0), 30.0)
    if recorded is not None:
        # The store records its own schema and only the header was overwritten (#117): put the
        # header back, in a write transaction that checks it again, and carry on from there.
        try:
            restamped = _restamp_header(database, recorded, timeout=wait)
        except sqlite3.OperationalError as exc:
            result.update(status="not_upgraded", error="store_busy", detail=type(exc).__name__,
                          hint="stop every process holding the store, including any 2.0 one, and run again")
            _emit(result)
            return 2
        if not restamped:
            result.update(status="not_upgraded", error="store_changed",
                          hint="the header or the recorded schema changed while this ran; run again")
            _emit(result)
            return 2
        result["header_restamped"] = {"from": before, "to": recorded,
                                      "cause": "a 2.0 process opened this store after its migration; make sure none runs"}
        if recorded == SCHEMA_VERSION:
            result.update(status="restamped", schema_after=SCHEMA_VERSION, journal_mode=_journal_mode(database))
            _report_other_tables(result, database)
            _emit(result)
            return 0
    started = time.monotonic()
    deadline = started + wait
    while True:
        # Never more than the wait itself: while the clock has not ticked since ``started``
        # this is (started + wait) - started, which rounds to 30.000000000000014 for some
        # clock values, and storage refuses a timeout above 30.
        remaining = min(wait, max(0.0, deadline - time.monotonic()))
        try:
            status = SQLiteStorage(binding, timeout_seconds=remaining).initialize()
            break
        except TruthWriterBusyError as exc:
            if remaining > 0:
                time.sleep(min(0.1, remaining))
                continue
            result.update(status="not_upgraded", error="store_busy", detail=type(exc).__name__,
                          hint="stop the worker (autostart pause) and run again")
            _emit(result)
            return 2
        except sqlite3.OperationalError as exc:
            result.update(status="not_upgraded", error="store_busy", detail=type(exc).__name__,
                          hint="stop the worker (autostart pause) and run again")
            _emit(result)
            return 2
        except ContractError as exc:
            result.update(status="not_upgraded", error=exc.code, detail=exc.field)
            _emit(result)
            return 2
    result.update(status="upgraded", schema_after=status.schema_version,
                  seconds=round(time.monotonic() - started, 1), journal_mode=_journal_mode(database))
    if recorded is not None:
        _report_other_tables(result, database)
    _emit(result)
    return 0


def _report_other_tables(result: dict[str, Any], database: Path) -> None:
    """Name what a restamp leaves in the file that is not this store's, so it is never a silent success.

    The restamp has already committed and the snapshot exists: a failure to read the tables is
    reported beside them, never in place of the result that names the snapshot.
    """
    import sqlite3

    try:
        others = _tables_not_in_schema(database)
    except sqlite3.Error as exc:
        result["tables_not_in_schema_error"] = f"{type(exc).__name__}: {exc}"
        result["warning"] = "this file's other tables could not be listed; look at the snapshot before relying on it"
        return
    if others:
        result["tables_not_in_schema"] = others
        if any(others.values()):
            result["warning"] = ("these tables are another program's, likely the 2.0 plugin that stamped the "
                                 "header, and anything it captured is in them, not in this store; they are kept "
                                 "in the file and in the snapshot")


def _add_uninstall_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--instance-root", required=True)
    parser.add_argument("--target-plugin-dir")
    parser.add_argument("--purge", action="store_true")


def _uninstall_plan(args: argparse.Namespace):
    return plan_uninstall(
        instance_root=_path(args.instance_root, "instance_root"),
        target_plugin_dir=_optional_path(args.target_plugin_dir, "target_plugin_dir"),
        purge=bool(args.purge),
    )


def _plan_uninstall(args: argparse.Namespace) -> int:
    plan = _uninstall_plan(args)
    _emit(plan.to_dict())
    return 1 if plan.conflicts else 0


def _apply_uninstall(args: argparse.Namespace) -> int:
    _emit(apply_uninstall(_uninstall_plan(args), purge=bool(args.purge)).to_dict())
    return 0


# name, help, argument builder, handler
_COMMANDS: tuple[tuple[str, str | None, Callable[[argparse.ArgumentParser], None], Callable[[argparse.Namespace], int]], ...] = (
    ("repair-claim-frames", "revalidate a bounded page of legacy claim frames without model calls", _add_repair_arguments, _repair_claim_frames),
    ("requalify", "re-judge a bounded page of stored claims after a gate change", _add_requalify_arguments, _requalify),
    ("retire-rootless-claims", "retire a bounded page of proposed claims no derivation root supports (tool output alone)", _add_requalify_arguments, _retire_rootless),
    ("retry-failures", "grant one bounded re-look to failed work after a fix has shipped", _add_retry_arguments, _retry_failures),
    ("backup", "create a new consistent SQLite snapshot and manifest", _add_backup_arguments, _backup),
    ("rollback", "inspect rollback; --apply may stop writes when new data must be reconciled", _add_rollback_arguments, _rollback),
    ("plan-install", None, _add_install_arguments, _plan_install),
    ("apply-install", None, _add_install_arguments, _apply_install),
    ("doctor", None, _add_doctor_arguments, _doctor),
    ("upgrade-store", "bring one store forward to this release's schema now, with a snapshot first", _add_upgrade_store_arguments, _upgrade_store),
    ("plan-uninstall", None, _add_uninstall_arguments, _plan_uninstall),
    ("apply-uninstall", None, _add_uninstall_arguments, _apply_uninstall),
)


def _subparser(sub, name: str, help_text: str | None) -> argparse.ArgumentParser:
    return sub.add_parser(name, help=help_text) if help_text else sub.add_parser(name)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] in _DELEGATED:
        return _DELEGATED[arguments[0]][1](arguments)
    parser = argparse.ArgumentParser(prog="scope-recall-maintenance")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, (help_text, _delegate) in _DELEGATED.items():
        _subparser(sub, name, help_text)
    handlers: dict[str, Callable[[argparse.Namespace], int]] = {}
    for name, help_text, add_arguments, run in _COMMANDS:
        add_arguments(_subparser(sub, name, help_text))
        handlers[name] = run
    args = parser.parse_args(arguments)
    try:
        return handlers[args.command](args)
    except (InstallError, BackupError, RollbackError) as exc:
        _emit({"status": "error", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
