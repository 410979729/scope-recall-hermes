"""Plan and confirm one complete, repeatable local installation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scope_recall.adapters.host_memory import inspect_host_memory

from ..adapters.hermes.shared_entries import read_attachment, read_shared_payload
from .install_common import InstallError, require_absolute
from .install_receipt import load_receipt
from .onboarding import inspect_installation, planning_database_state
from .setup_bindings import scope_choice
from .setup_configuration import read_models
from .setup_discovery import HOST_DIRECTORIES, HostLocation, discover_hosts, host_locations, interpreter, user_home
from .setup_existing import existing_entry_home
from .setup_packages import choose_python
from .setup_projects import project_suggestions
from .setup_workload import recommended_workload


@dataclass
class SetupPlan:
    """A read-only decision record; model configuration is kept out of its printable representation."""

    hosts: list[dict[str, Any]]
    store: Path
    project: Path
    worker: str
    models: dict[str, Any] | None = field(default=None, repr=False)
    test_mode: bool = False
    project_copies: list[dict[str, str]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    user_root: Path | None = None
    worker_explicit: bool = False
    configuration_changes: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Describe actions and reference names, never model credentials or arbitrary input configuration."""
        auxiliary = (self.models or {}).get("auxiliary", {})
        budget = auxiliary.get("budget") or {}
        return {
            "format": "scope-recall.setup/1",
            "configuration_changes": self.configuration_changes,
            "hosts": self.hosts,
            "store": str(self.store),
            "project": str(self.project),
            "worker": self.worker,
            "test_mode": self.test_mode,
            "models": {
                "action": "configure" if self.models else "preserve existing; new installations start disabled",
                "embedding": bool(auxiliary.get("external_embedding")),
                "extraction": bool(auxiliary.get("external_consolidation")),
                "credential_environment": sorted(
                    {
                        route["credential_env"]
                        for key in ("embedding", "consolidation")
                        if isinstance(route := auxiliary.get(key), dict) and route.get("credential_env")
                    }
                ),
                "budget": {
                    name: budget.get(name, 0)
                    for name in ("cap_micro_usd", "total_call_cap", "total_input_cap", "total_output_cap")
                },
            },
            "recommendations_per_100_turns": recommended_workload(),
            "project_copies_requiring_confirmation": self.project_copies,
            "blockers": self.blockers,
            "steps": [
                "create or reuse store",
                "bind approved scopes",
                "configure routes and budget ledger",
                "install hooks and memory skill",
                "configure worker",
                "doctor",
                "isolated cross-session check",
            ],
        }


def parser() -> argparse.ArgumentParser:
    """Define setup options once; the documentation generator reads the same definitions."""
    out = argparse.ArgumentParser(prog="scope-recall setup", description=__doc__)
    out.add_argument(
        "--host",
        action="append",
        choices=tuple(HOST_DIRECTORIES),
        help="Host to configure; repeat to select several. Default: discovered hosts.",
    )
    out.add_argument("--home", type=Path, help="Host home override; requires exactly one host.")
    out.add_argument(
        "--store",
        type=Path,
        help="Create or join this shared store. Default: the existing attachment or ~/.scope-recall/shared.",
    )
    out.add_argument("--share-with", help="Copy owner grants from this registered shared entry when grants differ.")
    out.add_argument("--project", type=Path, help="Project directory to bind or inspect. Default: current directory.")
    out.add_argument("--python", type=Path, help="Override the interpreter; default: a host venv, then setup's Python.")
    out.add_argument(
        "--configure-models",
        action="store_true",
        help="Guide endpoint, model, environment reference, pricing and budget choices before confirmation.",
    )
    out.add_argument(
        "--models",
        type=Path,
        help="JSON with auxiliary routes, budget and vector configuration; credentials must be environment references.",
    )
    out.add_argument(
        "--worker",
        choices=("on-demand", "manual"),
        help="Use bounded host-triggered workers, or print a manual command; no system service is registered.",
    )
    out.add_argument("--dry-run", action="store_true", help="Print the plan without writing or running checks.")
    out.add_argument(
        "--plan-json", action="store_true", help="Print the plan as JSON; combine with --dry-run for automation."
    )
    out.add_argument("--yes", action="store_true", help="Apply the displayed plan without an interactive confirmation.")
    out.add_argument(
        "--register-project-copy",
        action="store_true",
        help="Explicitly approve the matching project copy shown by setup; --yes alone never grants it.",
    )
    out.add_argument(
        "--test-mode", action="store_true", help="Bind synthetic test installations; never use with a personal store."
    )
    return out


def _target(host: str, home: Path) -> Path:
    if host in {"workbuddy", "dsh"}:
        return home
    if host == "hermes":
        return home / "plugins" / "scope-recall"
    return home / "skills" / "scope-recall"


def _check_store(store: Path, test_mode: bool, blockers: list[str]) -> None:
    """Refuse a damaged store before any attach or installer may write to it."""
    if (store / "installation.json").is_file():
        payload = read_shared_payload(store)
        if payload["test_mode"] != test_mode:
            blockers.append("test mode differs from the existing store")
    elif store.exists() and any(store.iterdir()):
        blockers.append("shared store directory is nonempty without a manifest")


def _check_model_change(store: Path, rows: list[dict], models: dict | None, blockers: list[str]) -> None:
    """Keep one embedding space until the existing explicit respace workflow coordinates all entries."""
    if models is None:
        return
    from ..core.recall_policy import embedding_space_id
    from ..runtime.auxiliary import AuxiliaryRuntimeConfig

    route = AuxiliaryRuntimeConfig.from_mapping(models["auxiliary"]).embedding
    if route is None:
        return
    candidates = [store / "runtime-config.json"]
    candidates += [
        Path(row["home"]) / ("data" if row["host"] == "codex" else "scope-recall") / "runtime-config.json"
        for row in rows
        if row["state"] == "local"
    ]
    for path in candidates:
        if path.is_file():
            old = json.loads(path.read_text(encoding="utf-8"))
            previous = AuxiliaryRuntimeConfig.from_mapping(old["auxiliary"])
            if previous.embedding is not None and embedding_space_id(previous.embedding.space()) != embedding_space_id(
                route.space()
            ):
                blockers.append(
                    "embedding space change needs respace-embeddings and coordinated entry routes; "
                    "existing configuration is preserved"
                )


def _selected_hosts(args: argparse.Namespace, found: dict) -> list[str]:
    """Require an explicit host only when discovery cannot select one."""
    selected = list(dict.fromkeys(args.host or found))
    if not selected:
        raise InstallError("no host detected; select --host and --home")
    if args.home is not None and len(selected) != 1:
        raise InstallError("--home needs exactly one --host")
    return selected


def _existing_settings(row: dict, store: Path) -> dict:
    """Read known runtime settings once for planning; a new entry inherits its shared worker's choices."""
    directory = Path(row["home"]) / ("data" if row["host"] == "codex" and row["state"] == "local" else "scope-recall")
    path = directory / "runtime-config.json"
    if not path.is_file() and row["state"] == "fresh":
        path = store / "runtime-config.json"
    if not path.is_file():
        return {}
    if path.stat().st_size > 1024 * 1024:
        raise InstallError("existing runtime configuration exceeds the supported size")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise InstallError("existing runtime configuration is not an object")
    return value


def _local_state(host: str, home: Path, blockers: list[str]) -> str:
    """Classify local stores through the existing read-only detector; clients otherwise need an attachment."""
    if host in {"codex", "hermes"}:
        inspected = inspect_installation(home, host=host)
        if inspected["route"] == "current_upgrade":
            return "local"
        if inspected["route"] != "fresh_install":
            blockers.append(f"{host}: {inspected['route']}; preserve the store and follow the migration workflow")
    return "fresh"


def _shared_worker_mode(args, store: Path, rows: list[dict]) -> str:
    """Keep the shared worker's mode independent of an entry's trigger configuration."""
    if args.worker is not None:
        return args.worker
    path = store / "runtime-config.json"
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        return "on-demand" if raw.get("supervisor_enabled", True) else "manual"
    return rows[0]["worker"]


def build_plan(args: argparse.Namespace, *, environment=None) -> SetupPlan:
    """Discover only explicit roots; classify existing installs without opening a writable database."""
    env = os.environ if environment is None else environment
    locations = host_locations(env)
    found = {item.host: item for item in discover_hosts(env)}
    selected = _selected_hosts(args, found)
    project = require_absolute(args.project or Path.cwd(), "project")
    rows, stores, copies, blockers = [], set(), [], []
    for host in selected:
        host_home = require_absolute(args.home or locations[host], "home")
        home = existing_entry_home(host, host_home, _target(host, host_home), user_root=user_home(env))
        item = found.get(host) if args.home is None else None
        if item is None:
            item = HostLocation(host, home, *interpreter(home))
        receipt = load_receipt(home)
        python = args.python or (Path(receipt["python_executable"]) if receipt else item.python)
        if not python.is_absolute():
            raise InstallError("python must be absolute")
        python, package_command, package_problem = choose_python(host, python, explicit=args.python is not None)
        if package_problem:
            blockers.append(host + ": " + package_problem)
        pointer = read_attachment(home)
        state = "attached" if pointer is not None else _local_state(host, home, blockers)
        if pointer is not None:
            stores.add(pointer.root)
        if state == "fresh" and receipt is not None:
            blockers.append(host + ": installation receipt has no usable binding; preserve and repair it")
        if state == "local" and host == "codex":
            copies.extend(dict(copy, home=str(home)) for copy in project_suggestions(home, project))
        target = Path(receipt["target_plugin_dir"]) if receipt is not None else _target(host, home)
        rows.append(
            dict(
                host=host,
                host_home=str(host_home),
                home=str(home),
                env_file=receipt.get("env_file") if receipt else None,
                python=str(python),
                python_source=item.python_source
                if python == item.python
                else "setup-interpreter; host venv lacks core",
                host_python_candidate=str(item.python),
                package_command=package_command,
                target=str(target),
                state=state,
                action="repair missing configuration" if receipt else "install",
                memory_skill=True,
                host_memory=inspect_host_memory(host, host_home, project=project, environment=env),
            )
        )
    if len(stores) > 1 and args.store is None:
        blockers.append("selected hosts use different shared stores; select one host at a time")
    store = require_absolute(args.store or next(iter(stores), user_home(env) / ".scope-recall" / "shared"), "store")
    if stores and any(root.resolve() != store for root in stores):
        blockers.append("a selected host is attached to another store; use the explicit move workflow")
    _check_store(store, args.test_mode, blockers)
    models = read_models(args.models)
    _check_model_change(store, rows, models, blockers)
    first_fresh = next((row["host"] for row in rows if row["state"] == "fresh"), None)
    for row in rows:
        row["scopes"] = scope_choice(
            Path(row["home"]), row["host"], store, share_with=args.share_with, fresh=row["state"] == "fresh"
        )
        if (
            not (store / "installation.json").is_file()
            and row["state"] == "fresh"
            and row["host"] != first_fresh
            and row["host"] != "hermes"
        ):
            row["scopes"]["source_entry"] = first_fresh
    for row in rows:
        directory = Path(row["home"]) / ("data" if row["host"] == "codex" else "scope-recall")
        row["database_inspection"] = planning_database_state(
            (directory if row["state"] == "local" else store) / "memory.sqlite3"
        )
        existing = _existing_settings(row, store)
        row["worker"] = args.worker or ("on-demand" if existing.get("supervisor_enabled", True) else "manual")
        auxiliary = existing.get("auxiliary") or {}
        row["existing_models"] = dict(
            embedding=bool(auxiliary.get("external_embedding")),
            extraction=bool(auxiliary.get("external_consolidation")),
            budget={
                key: (auxiliary.get("budget") or {}).get(key, 0)
                for key in ("cap_micro_usd", "total_call_cap", "total_input_cap", "total_output_cap")
            },
        )
    plan = SetupPlan(
        rows,
        store,
        project,
        _shared_worker_mode(args, store, rows),
        models,
        args.test_mode,
        copies,
        blockers,
        user_home(env),
        args.worker is not None,
    )
    from .setup_preflight import check_plan, configuration_changes

    plan.blockers.extend(check_plan(plan))
    plan.configuration_changes = configuration_changes(plan)
    return plan


def print_plan(plan: SetupPlan, *, machine: bool = False) -> None:
    """Print one reviewable plan before requesting approval."""
    value = plan.to_dict()
    if machine:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    print("Scope Recall setup plan")
    for row in plan.hosts:
        print(f"  {row['host']}: {row['home']} ({row['state']}); Python: {row['python']}")
        print(f"    Scopes: {row.get('scopes')}; worker: {row.get('worker')}")
        print(f"    Existing models and budget: {row.get('existing_models')}")
        print(f"    Database planning state: {row.get('database_inspection')}; health is checked by doctor.")
        print(f"    Host memory: {row.get('host_memory')}")
        if row.get("package_command"):
            print(f"    Install core in host venv: {row['package_command']}")
    print(f"  Store: {plan.store}; scopes: preserve existing grants, or owner's private scope for new entries")
    print(f"  Models: {value['models']}; worker: {plan.worker}; memory skill: all selected hosts")
    for item in value["recommendations_per_100_turns"]:
        print(
            f"  Per 100 turns: {item['task']}: {item['calls']} calls, "
            f"{item['input_tokens']} input / {item['output_tokens']} output; {item['basis']}"
        )
    for copy in plan.project_copies:
        print(f"  Project copy requires separate approval: {copy['directory']} -> {copy['same_project_as']}")
    for blocker in plan.blockers:
        print(f"  Blocked: {blocker}")
    for change in plan.configuration_changes:
        print(f"  Change {change['path']} {change['field']}: {change['before']} -> {change['after']}")
    print("  Then: doctor and an isolated capture, worker pass and cross-session recall check.")


def package_interpreter(start: Path | None = None) -> Path:
    """The interpreter of the environment this package is installed in.

    Hermes runs plugins on its base interpreter and adds the plugin environment's site-packages at start-up, so
    ``sys.executable`` there cannot import this package on its own; the environment's own interpreter can.  Outside
    a virtual environment (a checkout on ``sys.path``) the running interpreter is the answer.
    """
    for parent in (start or Path(__file__)).resolve().parents:
        if (parent / "pyvenv.cfg").is_file():
            # Not resolved: a POSIX venv's bin/python is a link whose target is the base interpreter.
            candidate = parent / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            if candidate.is_file():
                return candidate
    return Path(sys.executable)


def configure_hermes(hermes_home: str, config: dict) -> None:
    """Run the host wizard's noninteractive local setup before writing activation through its exact patch.

    Only ``hermes memory setup`` calls this, and it does not catch provider errors, so a refusal ends the command
    with one line naming the reason and a nonzero status instead of a traceback.
    """
    from .setup_apply import apply_setup

    args = parser().parse_args(
        [
            "--host",
            "hermes",
            "--home",
            str(Path(hermes_home).resolve()),
            "--python",
            str(package_interpreter()),
            "--yes",
        ]
    )
    plan = build_plan(args)
    if plan.blockers:
        raise SystemExit("Scope Recall setup blocked: " + "; ".join(plan.blockers))
    result = apply_setup(plan)
    if result["status"] != "ready":
        reason = result.get("reason") or result.get("error_type") or result["status"]
        raise SystemExit(
            f"Scope Recall setup failed at {result.get('failed_step') or 'verification'}: {reason}. "
            f"Setup receipt: {result['receipt']}"
        )
    config.setdefault("memory", {})["provider"] = "scope-recall"
    print("Scope Recall: original-message retrieval ready. Start a new Hermes session.")


def main(argv: list[str] | None = None) -> int:
    """Confirm once, apply in order and return a structured completion or resumable failure."""
    from .setup_apply import apply_setup

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        plan = build_plan(args)
        if args.configure_models:
            if args.yes or args.dry_run or args.models:
                raise InstallError("guided models require an interactive setup; use --models for scripts or dry-run")
            from .setup_models import configure_models

            plan.models = configure_models()
            _check_model_change(plan.store, plan.hosts, plan.models, plan.blockers)
            from .setup_preflight import configuration_changes

            plan.configuration_changes = configuration_changes(plan)
        print_plan(plan, machine=args.plan_json)
        if args.dry_run:
            return 2 if plan.blockers else 0
        if plan.blockers:
            return 2
        if not args.yes:
            if input("Apply this plan? [y/N] ").strip().lower() not in {"y", "yes"}:
                print("Cancelled; no changes made.")
                return 0
        approved = args.register_project_copy
        if plan.project_copies and not approved and not args.yes:
            approved = input("Register the shown directory as the same project? [y/N] ").strip().lower() in {"y", "yes"}
        result = apply_setup(plan, register_copies=approved)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "ready" else 2
    except (InstallError, OSError, ValueError, EOFError) as exc:
        print(
            json.dumps(
                dict(
                    status="blocked",
                    error_type=type(exc).__name__,
                    reason=str(exc)
                    if isinstance(exc, InstallError)
                    else "configuration unavailable; no credentials are printed",
                )
            )
        )
        return 2
