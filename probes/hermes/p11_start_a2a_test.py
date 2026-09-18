"""Start the isolated local bridge and official Hermes gateway (never a model call)."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from probes.hermes.p11_a2a_testkit import (
    A2A_AGENT_NAME, A2A_HOST, A2A_PORT, ARCHIVE, HERMES_CONFIG, HERMES_HOME,
    HERMES_PYTHON, HERMES_ROOT, LOCAL_BRIDGE_TOKEN_ENV, MAIN_BRIDGE_HOST,
    MAIN_BRIDGE_PORT, MAX_MODEL_POSTS, REPO_ROOT as KIT_REPO_ROOT, RUNTIME_RECORD, STATE, HOST_HEAD, HOST_VERSION,
    host_python_sha256,
    STOP_FILE, UPSTREAM_KEY_ENV, assert_test_path,
    port_status, scrub, write_json,
)

VAULT_HELPER = Path(os.environ.get("SCOPE_RECALL_VAULT_HELPER", "secret-helper.py"))
ZERO_MODEL_DIAGNOSTIC_DUMMY = "TEST_ZERO_MODEL_DIAGNOSTIC_DUMMY"
GATEWAY_READINESS_MARKER = "Press Ctrl+C to stop"
GATEWAY_READINESS_LOG = HERMES_HOME / "logs" / "gateway.log"


def _load_authorized_test_key() -> str:
    if not VAULT_HELPER.is_file():
        raise RuntimeError("authorized TEST credential loader missing")
    completed = subprocess.run(
        [sys.executable, "-B", str(VAULT_HELPER), "get", os.environ.get("SCOPE_RECALL_VAULT_ENTRY", "api:instance-env"), "--instance", os.environ.get("SCOPE_RECALL_VAULT_INSTANCE", "default")],
        capture_output=True, timeout=12,
    )
    raw = completed.stdout
    completed.stdout = b""
    if completed.returncode:
        raise RuntimeError("authorized TEST credential loader failed")
    selected = ""
    try:
        for line in raw.decode("utf-8-sig").splitlines():
            line = line.strip().removeprefix("export ").lstrip()
            name, separator, value = line.partition("=")
            if separator and name.strip() == "OPENCODE_GO_API_KEY":
                selected = value.strip()
                if selected[:1] in {"'", '"'}:
                    selected = ast.literal_eval(selected)
                break
    except (ValueError, SyntaxError, UnicodeError):
        raise RuntimeError("authorized TEST credential parsing failed") from None
    finally:
        raw = b""
    if not isinstance(selected, str) or not selected or any(ord(char) < 33 or ord(char) == 127 for char in selected):
        raise RuntimeError("authorized TEST credential unavailable")
    return selected


def _resolve_upstream_key(*, zero_model_diagnostic: bool) -> str:
    if zero_model_diagnostic:
        return ZERO_MODEL_DIAGNOSTIC_DUMMY
    return _load_authorized_test_key()


def _source_hashes() -> dict[str, str]:
    paths = {
        "hermes_a2a_adapter": HERMES_ROOT / "plugins" / "platforms" / "a2a" / "adapter.py",
        "hermes_agent_init": HERMES_ROOT / "agent" / "agent_init.py",
        "scope_recall_hermes_adapter": REPO_ROOT / "adapters" / "hermes" / "provider.py",
        "test_wrapper": HERMES_HOME / "plugins" / "scope_recall" / "__init__.py",
    }
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    hashes["hermes_python_executable"] = host_python_sha256()
    return hashes


def _safe_env(local_token: str, upstream_key: str) -> dict[str, str]:
    keep = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
    env = {key: value for key, value in os.environ.items() if key.upper() in keep}
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
        path = STATE / "environment" / key.lower()
        assert_test_path(path)
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    # This value was loaded by the allowlisted TEST loader and is inherited
    # only by the two TEST child processes; it is never persisted or logged.
    env[UPSTREAM_KEY_ENV] = upstream_key
    env.update({
        "HERMES_HOME": str(HERMES_HOME), "A2A_PORT": str(A2A_PORT),
        "A2A_HOST": A2A_HOST, "A2A_AGENT_NAME": A2A_AGENT_NAME,
        "A2A_ALLOW_ALL_USERS": "true", LOCAL_BRIDGE_TOKEN_ENV: local_token,
        "VIRTUAL_ENV": str(HERMES_PYTHON.parent.parent),
        "PYTHONPATH": str(HERMES_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8", "NO_PROXY": "localhost,127.0.0.1",
        "TERM": "dumb", "HERMES_PLUGINS_DEBUG": "1",
        "SCOPE_RECALL_P11_MAX_MODEL_POSTS": str(MAX_MODEL_POSTS),
    })
    return env


def _local_ready(url: str) -> bool:
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=1.5) as response:
            return int(response.status) == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def _gateway_log_offset(log_path: Path) -> int:
    """Return the pre-start byte boundary for the frozen gateway log."""
    try:
        return max(0, int(log_path.stat().st_size))
    except OSError:
        return 0


def _gateway_processing_ready(log_path: Path, start_offset: int = 0) -> bool:
    """Return true only after frozen Hermes finishes startup restore/wiring.

    The Agent Card is served before the startup restore gate is released.  The
    frozen 79445 host emits this marker at the end of ``run_startup.start``;
    dispatching before it can produce an empty completed A2A task.  Only bytes
    appended after this run's boundary count; the redirected stdout archive is
    not the authoritative gateway log.
    """
    try:
        with log_path.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            offset = max(0, int(start_offset))
            if size < offset:
                # A truncation/rotation after the boundary means the current
                # file contains only post-start bytes.
                offset = 0
            stream.seek(offset)
            appended = stream.read()
        return GATEWAY_READINESS_MARKER in appended.decode("utf-8", errors="replace")
    except OSError:
        return False


def _terminate_owned(process: subprocess.Popen | None) -> int | None:
    if process is None or process.poll() is not None:
        return None if process is None else process.returncode
    process.terminate()
    try:
        return process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        # This is still our child; bounded forceful cleanup is permitted only
        # for the processes started by this script.
        process.kill()
        return process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-seconds", type=int, default=600)
    parser.add_argument("--zero-model-diagnostic", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.duration_seconds <= 600:
        raise SystemExit("TEST duration must be 1..600 seconds")
    if KIT_REPO_ROOT != REPO_ROOT or not STATE.is_dir() or not HERMES_CONFIG.is_file():
        print(json.dumps({"started": False, "reason": "prepare_required", "state": str(STATE)}, sort_keys=True))
        return 2
    if STOP_FILE.exists():
        print(json.dumps({"started": False, "reason": "stop_file_present"}, sort_keys=True))
        return 2
    ports = port_status()
    if not all(ports.values()):
        print(json.dumps({"started": False, "reason": "port_occupied", "ports": ports}, sort_keys=True))
        return 2
    assert HERMES_PYTHON.is_file(), f"official Hermes python missing: {HERMES_PYTHON}"
    archive = ARCHIVE
    archive.mkdir(parents=True, exist_ok=True)
    try:
        upstream_key = _resolve_upstream_key(zero_model_diagnostic=args.zero_model_diagnostic)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        error = {"started": False, "error_type": type(exc).__name__, "reason": "credential_loader", "state": str(STATE), "credential_values_persisted": False}
        write_json(ARCHIVE / f"start-error-{time.time_ns()}.json", error)
        print(json.dumps(error, sort_keys=True), flush=True)
        return 2
    local_token = secrets.token_urlsafe(32)
    env = _safe_env(local_token, upstream_key)
    bridge_env = dict(env)
    bridge_env["PYTHONPATH"] = os.pathsep.join((str(REPO_ROOT), str(HERMES_ROOT)))
    gateway_env = dict(env)
    bridge_log_path = archive / f"bridge-{time.time_ns()}.log"
    gateway_log_path = archive / f"gateway-{time.time_ns()}.log"
    bridge_log = bridge_log_path.open("w", encoding="utf-8")
    gateway_log = gateway_log_path.open("w", encoding="utf-8")
    bridge = None
    gateway = None
    gateway_readiness_offset = 0
    started_ns = time.time_ns()
    try:
        bridge_cmd = [sys.executable, "-B", str(SCRIPT_DIR / "p11_a2a_bridge.py"), "--state", str(STATE), "--port", str(MAIN_BRIDGE_PORT)]
        if args.zero_model_diagnostic:
            bridge_cmd.append("--zero-model-diagnostic")
        bridge = subprocess.Popen(bridge_cmd, cwd=str(STATE), env=bridge_env, stdout=bridge_log, stderr=subprocess.STDOUT,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), text=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and bridge.poll() is None:
            if _local_ready(f"http://{MAIN_BRIDGE_HOST}:{MAIN_BRIDGE_PORT}/health"):
                break
            time.sleep(0.2)
        if bridge.poll() is not None:
            raise RuntimeError("TEST bridge exited before start")
        # Frozen Hermes writes readiness through logger.info to this file;
        # stdout/stderr capture is only a diagnostic archive.  Take the
        # boundary immediately before creating the owned gateway process.
        gateway_readiness_offset = _gateway_log_offset(GATEWAY_READINESS_LOG)
        gateway_cmd = [str(HERMES_PYTHON), "-B", "-m", "hermes_cli.main", "gateway", "run"]
        gateway = subprocess.Popen(gateway_cmd, cwd=str(HERMES_HOME), env=gateway_env, stdout=gateway_log, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), text=True)
        card_url = f"http://{A2A_HOST}:{A2A_PORT}/.well-known/agent-card.json"
        card_ready = False
        card_ready_ns = None
        card_deadline = time.monotonic() + 30
        while time.monotonic() < card_deadline and gateway.poll() is None:
            if _local_ready(card_url):
                card_ready = True
                card_ready_ns = time.time_ns()
                break
            time.sleep(0.25)
        processing_ready = False
        processing_ready_ns = None
        if card_ready:
            processing_deadline = time.monotonic() + 30
            while time.monotonic() < processing_deadline and gateway.poll() is None:
                if _gateway_processing_ready(GATEWAY_READINESS_LOG, gateway_readiness_offset):
                    processing_ready = True
                    processing_ready_ns = time.time_ns()
                    break
                time.sleep(0.25)
        record = {
            "started": card_ready and processing_ready, "state": str(STATE), "hermes_home": str(HERMES_HOME),
            "host_source": str(HERMES_ROOT), "host_python": str(HERMES_PYTHON), "host_head": HOST_HEAD,
            "host_version": HOST_VERSION, "gateway_pid": gateway.pid,
            "bridge_pid": bridge.pid, "controller_pid": os.getpid(),
            "gateway_command": gateway_cmd, "bridge_command": bridge_cmd,
            "gateway_url": f"http://{A2A_HOST}:{A2A_PORT}",
            "main_bridge_url": f"http://{MAIN_BRIDGE_HOST}:{MAIN_BRIDGE_PORT}/v1",
            "card_url": card_url, "card_ready": card_ready, "card_ready_ns": card_ready_ns,
            "processing_ready": processing_ready, "processing_ready_ns": processing_ready_ns,
            "processing_ready_marker": GATEWAY_READINESS_MARKER,
            "gateway_readiness_log": str(GATEWAY_READINESS_LOG),
            "gateway_readiness_log_offset": gateway_readiness_offset,
            "bridge_log": str(archive / bridge_log.name), "gateway_log": str(archive / gateway_log.name),
            "stop_file": str(STOP_FILE), "model_posts_upper_bound": MAX_MODEL_POSTS,
            "model_posts_used": 0, "credential_presence": {UPSTREAM_KEY_ENV: True, LOCAL_BRIDGE_TOKEN_ENV: True},
            "credential_values_persisted": False, "started_ns": started_ns,
            "network_model_posts_by_controller": 0,
            "source_sha256": _source_hashes(),
            "credential_loader": "TEST dummy; vault not read" if args.zero_model_diagnostic else "authorized TEST vault loader; values not persisted",
            "zero_model_diagnostic": args.zero_model_diagnostic,
        }
        if not (card_ready and processing_ready):
            failure = dict(record)
            failure.update({"reason": "startup_restore_not_ready", "runtime_record_written": False})
            write_json(ARCHIVE / f"start-readiness-{time.time_ns()}.json", scrub(failure))
            print(json.dumps(scrub(failure), ensure_ascii=False, sort_keys=True), flush=True)
            return 2
        write_json(RUNTIME_RECORD, scrub(record))
        print(json.dumps(scrub(record), ensure_ascii=False, sort_keys=True), flush=True)
        deadline = time.monotonic() + args.duration_seconds
        while gateway.poll() is None and bridge.poll() is None and time.monotonic() < deadline and not STOP_FILE.exists():
            time.sleep(0.25)
        return 0
    except (OSError, RuntimeError) as exc:
        error = {"started": False, "error_type": type(exc).__name__, "state": str(STATE), "credential_values_persisted": False}
        write_json(ARCHIVE / f"start-error-{time.time_ns()}.json", error)
        print(json.dumps(error, sort_keys=True), flush=True)
        return 2
    finally:
        gateway_code = _terminate_owned(gateway)
        bridge_code = _terminate_owned(bridge)
        bridge_log.close()
        gateway_log.close()
        if gateway is not None or bridge is not None:
            write_json(ARCHIVE / f"stop-{time.time_ns()}.json", {
                "state": str(STATE), "gateway_pid": gateway.pid if gateway else None,
                "bridge_pid": bridge.pid if bridge else None, "gateway_exit_code": gateway_code,
                "bridge_exit_code": bridge_code, "stop_file_present": STOP_FILE.exists(),
                "model_posts_by_controller": 0, "credential_values_persisted": False,
            })


if __name__ == "__main__":
    raise SystemExit(main())
