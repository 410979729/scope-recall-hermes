"""Run the nine remaining scenario checks with isolated hosts and environment-only routes.

--dry-run exercises deterministic ports and host subprocesses. Its results are
flow evidence and never replace real-machine verdicts in the scenario table.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from real_host_checks import host_check, install_check, restart_check  # noqa: E402
from real_host_io import (  # noqa: E402
    MARKER,
    Children,
    clean_root,
    environment,
    host_account_environment,
    new_root,
    write_json,
)
from real_host_quality import adoption_check, outage_check, quality_check  # noqa: E402
from real_host_runtime import load_routes, route_template  # noqa: E402
from real_host_scale import frozen_check  # noqa: E402

IDS = (
    "S1.install",
    "S1.host",
    "S1.time",
    "S2.restart",
    "S3.quality",
    "S4.quality",
    "S9.frozen",
    "S9.linux",
    "S10.real",
)


class Run:
    """One freshly allocated experiment; all owned resources close before publication."""

    def __init__(self, args):
        self.args, self.root = args, new_root(args.scratch)
        self.ownership_token = json.loads((self.root / MARKER).read_text(encoding="utf-8"))["token"]
        self.specs = json.loads(Path(__file__).with_name("scenarios_fixture.json").read_text(encoding="utf-8"))
        self.routes = load_routes(args.routes) if args.routes and not args.dry_run else None
        self.credential_names = (
            [
                route["credential_env"]
                for route in self.routes["auxiliary"].values()
                if isinstance(route, dict) and "credential_env" in route
            ]
            if self.routes
            else []
        )
        self.credential_names += ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_API_KEY", "OPENAI_API_KEY"]
        self.env = environment(self.root / "environment", credential_names=self.credential_names)
        # Only host CLI children may see the operator's own sign-in; everything else keeps the isolated homes.
        self.host_env = host_account_environment(self.env) if args.use_host_homes else self.env
        self.children = Children(self.env, self.root)
        self.stores, self.installed = [], None
        self.cache, self.rows = {}, []

    def stop_supervisor(self, process):
        """Retire only this invocation's contained supervisor before replacing its config."""
        self.children.stop(process)
        stdout, stderr = process.communicate(timeout=10)
        write_json(
            self.root / f"supervisor-{process.pid}.json",
            dict(
                pid=process.pid,
                exit_code=process.returncode,
                stdout=self.children.redact(stdout),
                stderr=self.children.redact(stderr),
            ),
        )

    def close(self):
        """Quiesce all children, then flush diagnostic writers owned by synthetic cores."""
        self.children.close()
        for store in self.stores:
            store.close()
        from scope_recall.core.recall_log_writer import close_writers

        if not close_writers(self.root):
            raise RuntimeError("owned diagnostic writers still active; retain scratch root")


def preflight(run):
    """Read installed CLI help in isolation and refuse unsupported invocation flags."""
    args = run.args
    if args.dry_run or set(args.only) <= {"S9.frozen", "S9.linux"}:
        return
    probes = [
        (args.claude, ["--help"], ["--plugin-dir", "--strict-mcp-config", "--setting-sources"]),
        (args.codex, ["exec", "--help"], ["--dangerously-bypass-hook-trust", "--ignore-user-config", "--json"]),
    ]
    for index, (binary, command, required) in enumerate(probes):
        result = run.children.run([binary, *command], run.root / f"cli-help-{index}.json", timeout=30)
        if result["exit_code"] or any(flag not in result["stdout"] for flag in required):
            raise ValueError("host CLI lacks required temporary-configuration flags")
    if args.login_wait:
        print(
            json.dumps(
                dict(
                    root=str(run.root),
                    CODEX_HOME=run.env["CODEX_HOME"],
                    CLAUDE_CONFIG_DIR=run.env["CLAUDE_CONFIG_DIR"],
                    instruction="Authenticate the two CLIs in these isolated homes, then press Enter here.",
                ),
                ensure_ascii=False,
            ),
            flush=True,
        )
        input()


def execute(run, key):
    """Dispatch by stable existing subcheck ID; installation/time share one observation."""
    if key in {"S1.install", "S1.time"}:
        if "installation" not in run.cache:
            run.cache["installation"] = install_check(run)
        passed, actual = run.cache["installation"]
        return passed and (key != "S1.time" or actual["non_download_seconds"] <= 600), actual
    if key == "S9.linux":
        if platform.system() != "Linux":
            return False, dict(missing="待 Linux 机器", platform=platform.system(), flow_pass=run.args.dry_run)
        if "frozen" not in run.cache:
            run.cache["frozen"] = frozen_check(run)
        return run.cache["frozen"]
    if key == "S9.frozen":
        if "frozen" not in run.cache:
            run.cache["frozen"] = frozen_check(run)
        return run.cache["frozen"]
    return {
        "S1.host": host_check,
        "S2.restart": restart_check,
        "S3.quality": quality_check,
        "S4.quality": adoption_check,
        "S10.real": outage_check,
    }[key](run)


def record(run, key, passed, actual, elapsed):
    """Keep acceptance status separate from successful deterministic plumbing."""
    specs = {row["id"]: row for row in run.specs["checks"]}
    status = "pass" if passed else "fail"
    partial = key in {"S9.frozen", "S9.linux"} and not actual.get("complete_frozen", False)
    if run.args.dry_run or partial or "missing" in actual:
        status = "m6"
        flow = actual.get("flow_pass", passed)
        if key == "S9.frozen" or (key == "S9.linux" and "scales" in actual):
            flow = bool(actual.get("scales"))
        actual = dict(actual, flow_pass=flow, missing=actual.get("missing", "真机验收未运行；仅流程或小规模证据"))
    row = dict(
        **specs[key],
        status=status,
        classification=None if status == "pass" else "real_machine" if status == "m6" else "product_defect",
        actual=dict(actual, evidence=str(run.root / (key + ".json"))),
        elapsed_seconds=elapsed,
    )
    run.rows.append(row)
    write_json(run.root / (key + ".json"), row)


def main(argv=None):
    """Run selected checks and publish one compatible evidence file outside the temporary root."""
    args = arguments(argv)
    if args.write_routes:
        if args.write_routes.exists():
            raise SystemExit("refusing to replace an existing route file")
        write_json(args.write_routes, route_template(args.chat_endpoint, args.chat_model))
        return 0
    args.output.resolve().relative_to(args.scratch.resolve())
    if args.output.exists():
        raise SystemExit("--output must be a fresh file")
    if args.dry_run:
        args.scales, args.scale_samples, args.scale_recovery = [100], 4, 2
    run = Run(args)
    try:
        preflight(run)
        for key in args.only:
            started = time.monotonic()
            try:
                passed, actual = execute(run, key)
                record(run, key, passed, actual, time.monotonic() - started)
            except Exception:  # noqa: BLE001 - preserve independent measurement failures
                spec = next(row for row in run.specs["checks"] if row["id"] == key)
                run.rows.append(
                    dict(
                        **spec,
                        status="error",
                        classification="measurement_error",
                        actual=dict(error=run.children.redact(traceback.format_exc()), evidence=str(run.root)),
                    )
                )
            write_json(run.root / "partial-results.json", run.rows)
    finally:
        run.close()
    from scope_recall._version import __version__

    result = dict(
        schema="real-host-scenarios/1",
        version=__version__,
        mode="dry-run" if args.dry_run else "real",
        created_at=datetime.now(UTC).isoformat(),
        root=str(run.root),
        platform=platform.system(),
        host_homes="operator" if args.use_host_homes else "isolated",
        fixture_sha256=hashlib.sha256(Path(__file__).with_name("scenarios_fixture.json").read_bytes()).hexdigest(),
        checks=run.rows,
        children_exited=all(p.poll() is not None for p in run.children.processes),
    )
    write_json(args.output, result)
    if args.cleanup:
        # Evidence is retained separately; destructive cleanup is opt-in and
        # authenticated by the token allocated in this process, never a path alone.
        clean_root(run.root, args.scratch, run.ownership_token)
    print(
        json.dumps(
            dict(output=str(args.output), root=str(run.root), results={row["id"]: row["status"] for row in run.rows}),
            ensure_ascii=False,
        )
    )
    return (
        1
        if any(
            row["status"] in {"fail", "error"} or (args.dry_run and not row["actual"].get("flow_pass"))
            for row in run.rows
        )
        else 0
    )


def arguments(argv):
    """Expose portable paths, explicit account handoff and bounded measurement windows."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only", default=",".join(IDS))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--routes", type=Path)
    parser.add_argument("--write-routes", type=Path)
    parser.add_argument("--chat-endpoint", help="with --write-routes: the auxiliary chat completions URL")
    parser.add_argument("--chat-model", help="with --write-routes: the auxiliary chat model name")
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--samples", type=Path)
    parser.add_argument("--claude", default=shutil.which("claude") or "claude")
    parser.add_argument("--codex", default=shutil.which("codex") or "codex")
    parser.add_argument("--uv", default=shutil.which("uv") or "uv")
    parser.add_argument("--claude-model")
    parser.add_argument("--codex-model")
    parser.add_argument("--login-wait", action="store_true")
    parser.add_argument(
        "--use-host-homes",
        action="store_true",
        help="run claude and codex with the operator's own sign-in and profile homes; their sessions land in the "
        "operator's history and must be removed afterwards",
    )
    parser.add_argument("--host-timeout", type=float, default=180)
    parser.add_argument("--promotion-timeout", type=float, default=1500)
    parser.add_argument("--recovery-timeout", type=float, default=1800)
    parser.add_argument("--scales", type=int, nargs="+", default=[350000, 1000000])
    parser.add_argument("--scale-samples", type=int, default=60)
    parser.add_argument("--scale-recovery", type=float, default=1200)
    args = parser.parse_args(argv)
    args.only = args.only.split(",")
    if not set(args.only) <= set(IDS) or len(set(args.only)) != len(args.only):
        parser.error("--only accepts unique existing M6 subcheck IDs")
    if any(scale < 100 for scale in args.scales) or args.scale_samples < 1:
        parser.error("scale >= 100 and scale-samples >= 1 required")
    if args.use_host_homes and args.login_wait:
        parser.error("--use-host-homes runs with the operator's existing sign-in; drop --login-wait")
    if args.write_routes and not (args.chat_endpoint and args.chat_model):
        parser.error("--write-routes requires --chat-endpoint and --chat-model")
    if not args.dry_run and not args.write_routes:
        if set(args.only) & {"S1.install", "S1.time", "S2.restart"} and not args.wheel:
            parser.error("installation and restart checks require --wheel")
        if "S2.restart" in args.only and not args.samples:
            parser.error("restart requires --samples with 58 release snapshots")
        needs_routes = bool(set(args.only) & {"S1.install", "S1.time", "S3.quality", "S4.quality", "S10.real"})
        needs_routes |= bool(set(args.only) & {"S9.frozen", "S9.linux"}) and set(args.scales) == {350000, 1000000}
        if needs_routes and not args.routes:
            parser.error("real model checks require --routes; use --write-routes to create the recipe")
    return args


if __name__ == "__main__":
    raise SystemExit(main())
