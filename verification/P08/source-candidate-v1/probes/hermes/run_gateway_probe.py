import argparse
import ast
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time

import yaml


ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / ".execution/TEST-P02-hermes"
HOST = Path(r"F:\Agents\runtime\windows\hermes-tianxuan\hermes-agent")
VAULT = Path(r"F:\Agents\shared\beidou\access-vault\bin\beidou-secret.py")


def credential():
    result = subprocess.run([sys.executable, "-B", str(VAULT), "get", "api:yuheng-instance-env", "--instance", "tianji"], capture_output=True, timeout=12)
    if result.returncode:
        raise RuntimeError("Approved OpenCode vault lookup failed")
    selected = ""
    try:
        for line in result.stdout.decode("utf-8-sig").splitlines():
            line = line.strip().removeprefix("export ").lstrip()
            name, separator, value = line.partition("=")
            if separator and name.strip() == "OPENCODE_GO_API_KEY":
                selected = value.strip()
                if selected[:1] in {"'", '"'}:
                    selected = ast.literal_eval(selected)
                break
    except (ValueError, SyntaxError, UnicodeError):
        raise RuntimeError("Approved bundle credential parsing failed") from None
    finally:
        result.stdout = b""
    if not isinstance(selected, str) or not selected or any(ord(c) < 33 or ord(c) == 127 for c in selected):
        raise RuntimeError("Approved bundle has no usable OpenCode key")
    return selected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-seconds", type=int, default=600)
    parser.add_argument("--fresh-registration-check", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.duration_seconds <= 1200:
        raise SystemExit("Probe duration must be 1..1200 seconds")
    state = STATE
    if args.fresh_registration_check:
        state = ROOT / ".execution/TEST-P02-hermes-registration-fixed"
        state.mkdir(exist_ok=False)
        (state / "TEST-P02-profile.json").write_bytes((STATE / "TEST-P02-profile.json").read_bytes())
        shutil.copytree(ROOT / "probes/hermes", state / "plugins/scope-recall-p02", ignore=shutil.ignore_patterns("__pycache__"))
    approval = json.loads((ROOT / "verification/P02/model-budget-authorization.json").read_text(encoding="utf-8"))
    if approval.get("status") != "approved" or approval.get("route") != "opencode-go / glm-5.3-flash":
        raise SystemExit("Selected route is not approved")
    marker = json.loads((state / "TEST-P02-profile.json").read_text(encoding="utf-8"))
    if marker != {"dataset":"SYNTHETIC_TEST_ONLY", "purpose":"P02_HOST_INTERFACE_PROBE"}:
        raise SystemExit("Isolated TEST profile required")
    with socket.socket() as check:
        check.bind(("127.0.0.1", 19921))
    spec = importlib.util.spec_from_file_location("p02_budget_proxy", Path(__file__).with_name("budget_proxy.py"))
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)
    if (approval.get("max_requests"), approval.get("max_request_json_bytes"), approval.get("max_output_tokens")) != (proxy.MAX_REQUESTS, proxy.MAX_BYTES, proxy.MAX_OUTPUT_TOKENS):
        raise SystemExit("Budget mismatch")
    local_token = secrets.token_urlsafe(32)
    server = proxy.make_server(STATE, credential(), local_token)
    endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    config = {
        "model":{"default":proxy.MODEL, "provider":"p02-opencode-go", "max_tokens":1024, "context_length":131072},
        "custom_providers":[{"name":"p02-opencode-go", "base_url":endpoint, "key_env":"SCOPE_RECALL_P02_LOCAL_TOKEN", "api_mode":"chat_completions", "model":proxy.MODEL, "models":{proxy.MODEL:{"context_length":131072}}}],
        "agent":{"max_turns":3, "api_max_retries":1, "verbose":False, "system_prompt":"This is an isolated Scope Recall interface test using synthetic data. Work only in the TEST workspace. Never contact other agents or read outside this workspace."},
        "memory":{"memory_enabled":True, "user_profile_enabled":False, "nudge_interval":0, "provider":"scope-recall-p02"},
        "plugins":{"enabled":["platforms/a2a", "scope-recall-p02"]},
        "gateway":{"platforms":{"a2a":{"enabled":True, "extra":{"host":"127.0.0.1", "port":19921}}}},
        "platform_toolsets":{"a2a":["terminal"]},
        "terminal":{"backend":"local", "cwd":str(state)},
        "compression":{"enabled":False}, "fallback_model":[],
    }
    (state / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    env = {k:v for k,v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}}
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
        path = state / "environment" / key.lower()
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    env.update(HERMES_HOME=str(state), A2A_PORT="19921", A2A_HOST="127.0.0.1", A2A_AGENT_NAME="TEST_SCOPE_RECALL_P02", A2A_ALLOW_ALL_USERS="true", SCOPE_RECALL_P02_LOCAL_TOKEN=local_token, HERMES_MAX_TOKENS="1024", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8", NO_PROXY="localhost,127.0.0.1", TERM="dumb")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log_path = state / f"gateway-{time.time_ns()}.log"
    command = [str(HOST / "venv/Scripts/python.exe"), "-B", "-m", "hermes_cli.main", "gateway", "run"]
    with log_path.open("wb") as log:
        process = subprocess.Popen(command, cwd=state, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        record = {"host_source":str(HOST), "host_pid":process.pid, "controller_pid":os.getpid(), "gateway_url":"http://127.0.0.1:19921", "model_proxy_url":endpoint, "log_path":str(log_path), "credential_loaded":True, "credential_persisted":False, "upstream":"https://opencode.ai/zen/go/v1", "model":proxy.MODEL, "stop_file":str(state / "STOP-P02"), "budget_ledger":str(STATE / "call-budget.sqlite3")}
        (state / "live-run.json").write_text(json.dumps(record, indent=2)+"\n", encoding="utf-8")
        print(json.dumps(record), flush=True)
        try:
            deadline = time.monotonic() + args.duration_seconds
            while process.poll() is None and time.monotonic() < deadline and not (state / "STOP-P02").exists():
                time.sleep(0.25)
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
            server.shutdown()
            server.server_close()
            record.update(exit_code=process.returncode, budget=server.ledger.snapshot(), completed_ns=time.time_ns())
            (state / "live-run.json").write_text(json.dumps(record, indent=2)+"\n", encoding="utf-8")
            print(json.dumps(record), flush=True)


if __name__ == "__main__":
    main()
