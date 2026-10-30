"""Install every published 3.x release in an isolated task environment and test upgrades."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

from packaging.version import Version


def main():
    """Generate release-written fixtures and verify them with the target interpreter."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--base-python", type=Path)
    parser.add_argument("--target-python", type=Path, required=True)
    parser.add_argument("--uv", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument(
        "--output", type=Path, required=True, help="verification copies and reports, separate from historical fixtures"
    )
    args = parser.parse_args()
    if not args.verify_only and (args.base_python is None or args.uv is None):
        parser.error("fixture generation requires --base-python and --uv")
    releases = json.loads((args.data / "pypi-releases.json").read_text(encoding="utf-8"))["releases"]
    versions = sorted(
        (
            version
            for version, files in releases.items()
            if Version(version).major == 3 and not Version(version).is_prerelease and files
        ),
        key=Version,
    )
    # Whitelist only operating-system essentials. No installed plugin, model
    # credential, host setting, or ambient real-store selector is inspected.
    env = {
        name: os.environ[name] for name in ("SystemRoot", "WINDIR", "COMSPEC", "PATH", "PATHEXT") if name in os.environ
    }
    env.update(
        TMP=str(args.scratch),
        TEMP=str(args.scratch),
        PYTHONUTF8="1",
        PYTHONDONTWRITEBYTECODE="1",
        UV_CACHE_DIR=str(args.scratch / "uv-cache"),
        PIP_CACHE_DIR=str(args.scratch / "pip-cache"),
    )
    script = Path(__file__).with_name("sample_store.py")
    reports = []
    consecutive_failures = 0
    args.samples.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    for version in versions:
        environment = args.scratch / "releases" / version
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        sample = args.samples / (f"{version}-pypi" if version == "3.16.1" else version)
        sample.mkdir(exist_ok=True)
        destination = args.output / version / f"run-{time.time_ns()}"
        destination.mkdir(parents=True)
        log = destination / "verification.log"
        commands = []
        if not args.verify_only:
            if not python.exists():
                commands.append([str(args.uv), "venv", "--python", str(args.base_python), str(environment)])
            commands.append(
                [str(args.uv), "pip", "install", "--python", str(python), f"hermes-scope-recall=={version}"]
            )
            if not (sample / "pre-upgrade.sqlite3").exists():
                commands.append([str(python), "-I", "-B", "-X", "utf8", str(script), "create", "--root", str(sample)])
        commands.append(
            [
                str(args.target_python),
                "-I",
                "-B",
                "-X",
                "utf8",
                str(script),
                "verify",
                "--root",
                str(sample),
                "--output",
                str(destination),
            ]
        )
        result = {
            "version": version,
            "sample": str(sample),
            "verification_output": str(destination),
            "commands": commands,
        }
        if not args.verify_only:
            result["environment"] = str(environment)
        with log.open("a", encoding="utf-8") as output:
            for command in commands:
                done = subprocess.run(
                    command, env=env, stdout=output, stderr=subprocess.STDOUT, timeout=600, check=False
                )
                if done.returncode:
                    report = destination / "upgrade.json"
                    if command == commands[-1] and report.exists():
                        result.update(status="mismatch", upgrade=json.loads(report.read_text(encoding="utf-8")))
                    else:
                        result.update(status="failed", exit_code=done.returncode, command=command)
                    break
            else:
                upgraded = json.loads((destination / "upgrade.json").read_text(encoding="utf-8"))
                result.update(status="passed" if upgraded["passed"] else "mismatch", upgrade=upgraded)
        reports.append(result)
        (args.output / "matrix.json").write_text(json.dumps(reports, indent=2), encoding="utf-8")
        print(version, result["status"], flush=True)
        consecutive_failures = consecutive_failures + 1 if result["status"] == "failed" else 0
        if consecutive_failures == 3:
            raise RuntimeError("three consecutive failures; inspect generation logs before resuming")
    if any(report["status"] != "passed" for report in reports):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
