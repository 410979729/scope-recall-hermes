from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from test_directories import TestDirectory


ROOT = Path(__file__).resolve().parents[1]
SUITES = {
    "unit": ["tests/unit/test_v11_context.py", "tests/unit/test_check_runner.py"],
    "contract": ["tests/contract/test_v11_protocol.py", "tests/contract/test_v11_inputs.py"],
    "storage": ["tests/contract/test_v11_storage.py"],
    "capture": ["tests/contract/test_v11_capture.py"],
    "claims": ["tests/contract/test_v11_claims.py"],
    "deletion": ["tests/contract/test_v11_deletion.py"],
    "episodes": ["tests/contract/test_v11_episodes.py","tests/contract/test_v11_retained_artifacts.py","tests/contract/test_v11_episode_authority.py","tests/contract/test_v11_consolidation_input.py","tests/contract/test_v11_aliases.py"],
    "retrieval": ["tests/contract/test_p08_retrieval.py", "tests/contract/test_p08_evidence_followup.py", "tests/contract/test_v11_recall_admission.py"],
    "model_runtime": ["tests/host/test_eval_model_runtime.py"],
    "integration": [],
    "release": [],
    "eval": [],
}


def _safe_error(exc: BaseException) -> dict[str, str]:
    reason = " ".join(str(exc).split()) or "no detail"
    return {"type": type(exc).__name__, "reason": reason[:240]}


def _as_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", required=True, choices=SUITES)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--changed-base")
    parser.add_argument("--allow-model-calls", action="store_true")
    parser.add_argument("--task", choices=[f"P{i:02d}" for i in range(20)], default="P01")
    args = parser.parse_args()
    selected = list(SUITES[args.tier])
    if args.changed_base:
        changed = subprocess.run(["git", "diff", "--name-only", args.changed_base, "--"], cwd=ROOT, capture_output=True, text=True)
        if changed.returncode:
            print(json.dumps({"status": "invalid_base", "exit_code": changed.returncode}))
            return 2
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=True)
        paths = set(changed.stdout.splitlines()) | set(untracked.stdout.splitlines())
        unknown = [p for p in sorted(paths) if p not in {"contracts.py", "scripts/check.py", "tests/v11_guard.py", "tests/v11_support.py"} and not p.startswith(("contracts/", "tests/unit/test_v11_", "tests/contract/test_v11_", "fixtures/", "verification/", "docs/"))]
        if unknown:
            print(json.dumps({"status": "integration_required_not_implemented", "unmapped_files": unknown}))
            return 2
        if args.tier in {"unit", "contract"}:
            selected = SUITES["unit"] + SUITES["contract"]
    if not selected:
        print(json.dumps({"status": "not_implemented", "tier": args.tier, "selected": [], "model_calls": False}))
        return 2
    if any(not (ROOT / p).is_file() for p in selected):
        print(json.dumps({"status": "missing_tests", "selected": selected}))
        return 2
    if args.list:
        print(json.dumps({"status": "listed_not_executed", "selected": selected, "model_calls": False}))
        return 0
    evidence = ROOT / "verification" / args.task
    evidence.mkdir(parents=True, exist_ok=True)
    parent = ROOT / ".execution"
    parent.mkdir(exist_ok=True)
    names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, text=True).splitlines()
    manifest = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in sorted(set(names)) if not p.startswith(("verification/", "docs/")) and (ROOT / p).is_file()}
    source_digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    stamp = time.time_ns()
    stem = f"{args.tier}-{stamp}"
    log = evidence / f"{stem}.log"
    inputs_path = evidence / f"{stem}-inputs.json"
    receipt_path = evidence / f"{stem}.json"
    command = []
    output_parts: list[str] = []
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    elapsed = 0.0
    pytest_exit_code = 2
    wrapper_exit_code = 0
    pytest_state = "not_started"
    pytest_error = None
    wrapper_error = None
    cleanup_exit_code = 0
    cleanup_state = "not_started"
    cleanup_error = None
    transition_history = ["prepared"]
    directory = None

    try:
        directory = TestDirectory(prefix="TEST-v11-", dir=parent)
        isolated = Path(directory.name if hasattr(directory, "name") else directory)
        env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS"}}
        for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "HERMES_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
            target = isolated / key.lower()
            target.mkdir()
            env[key] = str(target)
        env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONPATH=os.pathsep.join((str(ROOT / "tests"), str(ROOT))), SCOPE_RECALL_TEST_BOUNDARY_PARENT=str(isolated), SCOPE_RECALL_TEST_PROTECTED_HOME=str(Path.home()), SCOPE_RECALL_ACTIVE_HERMES_HOME=str(isolated / "protected-unused"), SCOPE_RECALL_REAL_HOME=str(isolated / "unused-real"), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        junit = isolated / "junit.xml"
        command = [sys.executable, "-B", "-m", "pytest", *selected, "-q", "-p", "v11_guard", "-p", "no:cacheprovider", "--durations=10", f"--junitxml={junit}"]
        transition_history.append("pytest_started")
        start = time.perf_counter()
        try:
            result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        except subprocess.TimeoutExpired as exc:
            result = subprocess.CompletedProcess(command, 124, _as_text(exc.stdout), "TEST_TIMEOUT after 180 seconds\n")
        elapsed = time.perf_counter() - start
        output_parts.extend((_as_text(result.stdout), _as_text(result.stderr)))
        if junit.is_file():
            tree = ET.parse(junit)
            cases = tree.findall(".//testcase")
            counts["failed"] = sum(c.find("failure") is not None or c.find("error") is not None for c in cases)
            counts["skipped"] = sum(c.find("skipped") is not None for c in cases)
            counts["passed"] = len(cases) - counts["failed"] - counts["skipped"]
        pytest_exit_code = result.returncode if result.returncode else (0 if counts["passed"] and not counts["failed"] else 2)
        pytest_state = "completed"
        transition_history.append("pytest_completed_waiting_cleanup")
    except Exception as exc:
        pytest_exit_code = 2
        wrapper_exit_code = 2
        pytest_state = "wrapper_failed"
        wrapper_error = _safe_error(exc)
        output_parts.append(json.dumps({"transition": "pytest_wrapper_failed", "error": wrapper_error}, sort_keys=True))
        transition_history.append("pytest_wrapper_failed_waiting_cleanup")
    finally:
        if directory is not None:
            try:
                directory.cleanup()
                cleanup_state = "succeeded"
                transition_history.append("cleanup_succeeded")
            except Exception as exc:
                cleanup_exit_code = 3
                cleanup_state = "failed"
                cleanup_error = _safe_error(exc)
                output_parts.append(json.dumps({"transition": "cleanup_failed", "error": cleanup_error}, sort_keys=True))
                transition_history.append("cleanup_failed")
        else:
            cleanup_exit_code = 3
            cleanup_state = "not_started"
            cleanup_error = {"type": "CleanupNotStarted", "reason": "test directory was not created"}
            transition_history.append("cleanup_not_started")

    overall_code = cleanup_exit_code or wrapper_exit_code or pytest_exit_code
    transition_history.append("receipt_written")
    log_lines = [part for part in output_parts if part]
    log_lines.append(json.dumps({"transition_history": transition_history, "pytest_exit_code": pytest_exit_code, "wrapper_exit_code": wrapper_exit_code, "cleanup_exit_code": cleanup_exit_code, "overall_exit_code": overall_code}, sort_keys=True))
    log.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    source_base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    receipt = {
        "command": command,
        "exit_code": overall_code,
        "overall_exit_code": overall_code,
        "pytest_exit_code": pytest_exit_code,
        "wrapper_exit_code": wrapper_exit_code,
        "cleanup_exit_code": cleanup_exit_code,
        "pytest": {"state": pytest_state, "exit_code": pytest_exit_code, "error": pytest_error},
        "wrapper": {"state": "failed" if wrapper_exit_code else "succeeded", "exit_code": wrapper_exit_code, "error": wrapper_error},
        "cleanup": {"state": cleanup_state, "exit_code": cleanup_exit_code, "error": cleanup_error},
        "transition_history": transition_history,
        "duration_seconds": elapsed,
        **counts,
        "selected": selected,
        "log": log.relative_to(ROOT).as_posix(),
        "model_calls": False,
        "test_data": "synthetic_only",
        "source_base": source_base,
        "source_inputs_sha256": source_digest,
        "source_manifest": f"verification/{args.task}/{stem}-inputs.json",
        "evidence_scope": f"{args.task} selected local contracts only; no M/J semantic or host behavior execution",
    }
    inputs_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print("".join(output_parts), end="")
    print(json.dumps(receipt))
    return overall_code


if __name__ == "__main__":
    raise SystemExit(main())
