"""CLI entry for bounded v1.1 install, doctor, and uninstall flows."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .doctor import run_doctor
from .backup import BackupError
from .rollback import RollbackError
from .install import (
    InstallError,
    apply_install,
    apply_uninstall,
    plan_install,
    plan_uninstall,
)


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _absolute(value: str, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SystemExit(f"{field} must be absolute")
    return path.resolve()


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "autostart":
        from .autostart import main as autostart_main
        return autostart_main(arguments[1:])
    if arguments and arguments[0] in {"setup", "migrate"}:
        from .upgrade_cli import main as upgrade_main
        return upgrade_main(arguments)
    parser = argparse.ArgumentParser(prog="scope-recall-maintenance")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="agent-operated fresh install/update/migration routing")
    sub.add_parser("migrate", help="prepare, resume, verify and index a legacy migration job")
    sub.add_parser("autostart", help="plan, enable, pause or remove a bounded Windows background wake")
    repair = sub.add_parser('repair-claim-frames', help='revalidate a bounded page of legacy claim frames without model calls')
    repair.add_argument('--config', required=True)
    repair.add_argument('--after-ref', default='')
    repair.add_argument('--limit', type=int, default=16)
    requalify = sub.add_parser('requalify',
                               help='re-judge a bounded page of stored claims after a gate change')
    requalify.add_argument('--config', required=True)
    requalify.add_argument('--after-ref', default='')
    requalify.add_argument('--limit', type=int, default=16)
    requalify.add_argument('--apply', action='store_true',
                           help='write the new verdicts; without it nothing is changed')
    retry = sub.add_parser('retry-failures',
                           help='grant one bounded re-look to failed work after a fix has shipped')
    retry.add_argument('--config', required=True)
    retry.add_argument('--limit', type=int, default=64)
    retry.add_argument('--include-terminal', action='store_true',
                       help='also re-run by-design terminal failures (derivation_invalid, budget_checked)')
    retry.add_argument('--apply', action='store_true',
                       help='write the re-opened rows; without it nothing is changed')
    backup = sub.add_parser("backup", help="create a new consistent SQLite snapshot and manifest")
    backup.add_argument("--database", required=True)
    backup.add_argument("--output", required=True)
    backup.add_argument("--manifest")
    rollback = sub.add_parser("rollback", help="inspect rollback; --apply may stop writes when new data must be reconciled")
    rollback.add_argument("--current-db", required=True)
    rollback.add_argument("--snapshot", required=True)
    rollback.add_argument("--output")
    rollback.add_argument("--apply", action="store_true")

    plan = sub.add_parser("plan-install")
    plan.add_argument("--host", required=True, choices=("hermes", "codex"))
    plan.add_argument("--target-plugin-dir", required=True)
    plan.add_argument("--instance-root", required=True)
    plan.add_argument("--project-root", required=True)
    plan.add_argument("--agent-id", required=True)
    plan.add_argument(
        "--agent-workspace",
        default=None,
        help="Hermes audience workspace; defaults to hermes to match the host memory-provider init contract. Codex rejects this flag.",
    )
    plan.add_argument("--python", required=True)
    plan.add_argument(
        "--env-file",
        default=None,
        help="Codex only: absolute file with the credential names the runtime config declares; "
        "written into .mcp.json and hooks.json because Codex starts those processes without them.",
    )
    plan.add_argument(
        "--test-mode",
        action="store_true",
        help="use isolated TEST binding semantics; omitted for production installation",
    )

    apply_cmd = sub.add_parser("apply-install")
    apply_cmd.add_argument("--host", required=True, choices=("hermes", "codex"))
    apply_cmd.add_argument("--target-plugin-dir", required=True)
    apply_cmd.add_argument("--instance-root", required=True)
    apply_cmd.add_argument("--project-root", required=True)
    apply_cmd.add_argument("--agent-id", required=True)
    apply_cmd.add_argument(
        "--agent-workspace",
        default=None,
        help="Hermes audience workspace; defaults to hermes to match the host memory-provider init contract. Codex rejects this flag.",
    )
    apply_cmd.add_argument("--python", required=True)
    apply_cmd.add_argument(
        "--env-file",
        default=None,
        help="Codex only: absolute file with the credential names the runtime config declares; "
        "written into .mcp.json and hooks.json because Codex starts those processes without them.",
    )
    apply_cmd.add_argument(
        "--test-mode",
        action="store_true",
        help="use isolated TEST binding semantics; omitted for production installation",
    )

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--host", required=True, choices=("hermes", "codex"))
    doctor.add_argument("--instance-root", required=True)
    doctor.add_argument("--python")

    uninstall_plan = sub.add_parser("plan-uninstall")
    uninstall_plan.add_argument("--instance-root", required=True)
    uninstall_plan.add_argument("--target-plugin-dir")
    uninstall_plan.add_argument("--purge", action="store_true")

    uninstall_apply = sub.add_parser("apply-uninstall")
    uninstall_apply.add_argument("--instance-root", required=True)
    uninstall_apply.add_argument("--target-plugin-dir")
    uninstall_apply.add_argument("--purge", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == 'repair-claim-frames':
            from ..runtime.worker_entry import load_config
            from ..core import MemoryCore, CoreConfig
            from ..contracts import ContractError
            try:
                config = load_config(_absolute(args.config, 'config'))
                core = MemoryCore(CoreConfig(config.binding))
                receipt = core.repair_claim_frames(config.context(), after_ref=args.after_ref,
                                                  limit=args.limit, remaining_seconds=config.request_seconds)
            except ContractError as exc:
                _emit({'status': 'error', 'code': exc.code, 'field': exc.field})
                return 2
            except (ValueError, OSError):
                _emit({'status': 'error', 'code': 'CONFIG_INVALID'})
                return 2
            _emit(receipt)
            return 1 if receipt['errors'] else 0
        if args.command == 'requalify':
            from ..runtime.worker_entry import load_config
            from ..core import MemoryCore, CoreConfig
            from ..contracts import ContractError
            try:
                config = load_config(_absolute(args.config, 'config'))
                core = MemoryCore(CoreConfig(config.binding))
                receipt = core.requalify_claims(config.context(), after_ref=args.after_ref,
                                                limit=args.limit, dry_run=not args.apply,
                                                remaining_seconds=config.request_seconds)
            except ContractError as exc:
                _emit({'status': 'error', 'code': exc.code, 'field': exc.field})
                return 2
            except (ValueError, OSError):
                _emit({'status': 'error', 'code': 'CONFIG_INVALID'})
                return 2
            _emit(receipt)
            return 0
        if args.command == 'retry-failures':
            from ..runtime.worker_entry import load_config
            from ..core import MemoryCore, CoreConfig
            from ..contracts import ContractError
            try:
                config = load_config(_absolute(args.config, 'config'))
                core = MemoryCore(CoreConfig(config.binding))
                receipt = core.retry_failed_work(config.context(), limit=args.limit,
                                                 include_terminal=args.include_terminal,
                                                 dry_run=not args.apply,
                                                 remaining_seconds=config.request_seconds)
            except ContractError as exc:
                _emit({'status': 'error', 'code': exc.code, 'field': exc.field})
                return 2
            except (ValueError, OSError):
                _emit({'status': 'error', 'code': 'CONFIG_INVALID'})
                return 2
            _emit(receipt)
            return 0
        if args.command == "backup":
            from .backup import backup_sqlite
            target = _absolute(args.output, "output")
            manifest = _absolute(args.manifest, "manifest") if args.manifest else target.with_suffix(target.suffix+".json")
            _emit(backup_sqlite(_absolute(args.database, "database"), target, manifest=manifest))
            return 0
        if args.command == "rollback":
            from .rollback import plan_rollback, rollback_to_verified_snapshot
            current, snapshot = _absolute(args.current_db,"current_db"), _absolute(args.snapshot,"snapshot")
            output = _absolute(args.output,"output") if args.output else None
            result = plan_rollback(current,snapshot,destination=output)
            if args.apply:
                result = rollback_to_verified_snapshot(current,snapshot,destination=output)
            _emit(result)
            return 0
        if args.command == "plan-install":
            result = plan_install(
                target_plugin_dir=_absolute(args.target_plugin_dir, "target_plugin_dir"),
                instance_root=_absolute(args.instance_root, "instance_root"),
                project_root=_absolute(args.project_root, "project_root"),
                agent_id=args.agent_id,
                python_executable=_absolute(args.python, "python"),
                host=args.host,
                test_mode=args.test_mode,
                agent_workspace=args.agent_workspace,
                env_file=_absolute(args.env_file, "env_file") if args.env_file else None,
            )
            _emit(result.to_dict())
            return 1 if result.conflicts else 0
        if args.command == "apply-install":
            install_plan = plan_install(
                target_plugin_dir=_absolute(args.target_plugin_dir, "target_plugin_dir"),
                instance_root=_absolute(args.instance_root, "instance_root"),
                project_root=_absolute(args.project_root, "project_root"),
                agent_id=args.agent_id,
                python_executable=_absolute(args.python, "python"),
                host=args.host,
                test_mode=args.test_mode,
                agent_workspace=args.agent_workspace,
                env_file=_absolute(args.env_file, "env_file") if args.env_file else None,
            )
            result = apply_install(install_plan)
            _emit(result.to_dict())
            return 0
        if args.command == "doctor":
            python = _absolute(args.python, "python") if args.python else None
            report = run_doctor(
                host=args.host,
                instance_root=_absolute(args.instance_root, "instance_root"),
                python_executable=python,
            )
            _emit(report.to_dict())
            return 0 if report.status == "ok" else 1
        if args.command == "plan-uninstall":
            target = _absolute(args.target_plugin_dir, "target_plugin_dir") if args.target_plugin_dir else None
            result = plan_uninstall(
                instance_root=_absolute(args.instance_root, "instance_root"),
                target_plugin_dir=target,
                purge=bool(args.purge),
            )
            _emit(result.to_dict())
            return 1 if result.conflicts else 0
        if args.command == "apply-uninstall":
            target = _absolute(args.target_plugin_dir, "target_plugin_dir") if args.target_plugin_dir else None
            uninstall_plan = plan_uninstall(
                instance_root=_absolute(args.instance_root, "instance_root"),
                target_plugin_dir=target,
                purge=bool(args.purge),
            )
            result = apply_uninstall(uninstall_plan, purge=bool(args.purge))
            _emit(result.to_dict())
            return 0
    except (InstallError, BackupError, RollbackError) as exc:
        _emit({"status": "error", "error": str(exc)})
        return 2
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
