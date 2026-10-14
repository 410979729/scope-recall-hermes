"""Agent-facing setup routing and migration job commands."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from ..adapters.hermes.audiences import HermesIdentityError
from ..contracts import ContractError
from .backup import BackupError
from .legacy_episode_membership import MembershipProjectionError
from .legacy_v2_compat import LegacyCompatibilityError
from .migrate_v2 import MigrationError
from .migration_records import constraint_failure
from .onboarding import inspect_installation, workflow_text
from .upgrade import (
    prepare_upgrade,
    queue_upgrade_index,
    run_upgrade,
    upgrade_status,
    verify_upgrade,
)

#: Refusals whose text is told.  Scope/identity mismatches may be acted on, but exception text must not reveal row
#: content, model credentials or arbitrary filesystem data: a contract error's text is its code and field, an identity
#: error's names what the manifest lacks, and the legacy converter's refusals are fixed phrases.
_TOLD = (
    MigrationError,
    BackupError,
    ContractError,
    HermesIdentityError,
    LegacyCompatibilityError,
    MembershipProjectionError,
)


def _reason(exc: BaseException) -> str:
    """A told refusal's text, or a constraint failure SQLite named (a migration once ended
    "invalid_or_unavailable_input" on a CHECK it could have named); anything else in general terms."""
    if isinstance(exc, _TOLD):
        return str(exc)
    return constraint_failure(exc) or "invalid_or_unavailable_input"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="scope-recall")
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser(
        "setup",
        help="agent-only discovery: choose fresh install, current update or legacy migration",
    )
    setup.add_argument("--home")
    setup.add_argument(
        "--database",
        help="actual database selected by host configuration, if nonstandard",
    )
    setup.add_argument("--host", choices=("hermes", "codex"), default="hermes")
    setup.add_argument(
        "--workflow",
        action="store_true",
        help="print the bundled agent-operated workflow",
    )
    migrate = sub.add_parser("migrate", help="resumable migration using the existing legacy converter")
    stages = migrate.add_subparsers(dest="stage", required=True)
    prepare = stages.add_parser("prepare")
    prepare.add_argument("--source", required=True)
    prepare.add_argument("--job", required=True)
    prepare.add_argument("--installation-manifest", required=True)
    prepare.add_argument("--host", choices=("hermes", "codex"))
    prepare.add_argument(
        "--scope-map",
        help="agent-generated verified mapping; exact retained IDs map automatically",
    )
    run = stages.add_parser("run")
    run.add_argument("--job", required=True)
    run.add_argument("--source-quiesced", action="store_true")
    run.add_argument("--legacy-reader-contract")
    for name in ("verify", "status", "queue-index"):
        stage = stages.add_parser(name)
        stage.add_argument("--job", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "setup":
            if args.workflow:
                print(workflow_text())
                return 0
            if not args.home:
                parser.error("setup requires --home or --workflow")
            result = inspect_installation(args.home, host=args.host, database=args.database)
        elif args.stage == "prepare":
            mapping = None
            if args.scope_map:
                mapping = json.loads(Path(args.scope_map).read_text(encoding="utf-8"))
                if type(mapping) is not dict or any(
                    type(k) is not str or type(v) is not str for k, v in mapping.items()
                ):
                    raise ValueError("scope map must contain only string pairs")
            result = prepare_upgrade(
                args.source,
                args.job,
                installation_manifest=args.installation_manifest,
                host=args.host,
                scope_map=mapping,
            )
        elif args.stage == "run":
            result = run_upgrade(
                args.job,
                source_quiesced=args.source_quiesced,
                legacy_reader_contract=args.legacy_reader_contract,
            )
        else:
            result = {
                "verify": verify_upgrade,
                "status": upgrade_status,
                "queue-index": queue_upgrade_index,
            }[args.stage](args.job)
    except (
        MigrationError,
        BackupError,
        ContractError,
        HermesIdentityError,
        ValueError,
        OSError,
        TimeoutError,
        sqlite3.Error,
    ) as exc:
        result = dict(
            status="blocked",
            error_type=type(exc).__name__,
            reason=_reason(exc),
            safe_action="preserve_old_host_and_diagnose",
            user_memory_review_required=False,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 3
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return (
        3
        if result.get("state") == "blocked"
        or result.get("route") in {"unsupported", "repair_required", "needs_agent_inspection"}
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
