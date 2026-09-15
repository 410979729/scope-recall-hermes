import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import types


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-source", type=Path, required=True)
    args = parser.parse_args()
    state = ROOT / ".execution" / f"TEST-P02-preflight-{time.time_ns()}"
    state.mkdir(parents=True)
    env = {k:v for k,v in os.environ.items() if k.upper() in {"SYSTEMROOT","WINDIR","COMSPEC","PATH","PATHEXT"}}
    for key in ("HOME","USERPROFILE","APPDATA","LOCALAPPDATA","TEMP","TMP","HERMES_HOME","CODEX_HOME"):
        target = state / key.lower()
        target.mkdir()
        env[key] = str(target)
    checks = []
    command = [sys.executable,"-I","-B",str(ROOT / "probes/codex/hook_probe.py"),"--state-dir",str(state)]
    common = dict(session_id="TEST-preflight",turn_id="TEST-turn",cwd=str(state),model="NO_MODEL")
    events = [dict(hook_event_name="SessionStart"), dict(hook_event_name="UserPromptSubmit",prompt="TEST_SCOPE_RECALL 校验接口"), dict(hook_event_name="PostToolUse",tool_name="Bash",tool_use_id="TEST-tool",tool_input={"command":"TEST synthetic only"},tool_response={"exit_code":1}), dict(hook_event_name="Stop",last_assistant_message="TEST synthetic final"), dict(hook_event_name="Interrupt"), dict(hook_event_name="SessionEnd")]
    start = time.perf_counter()
    for event in events:
        result = subprocess.run(command,input=json.dumps(common | event).encode(),capture_output=True,env=env,timeout=3)
        assert result.returncode == 0, result.stderr
        response = json.loads(result.stdout)
        assert bool(response) == (event["hook_event_name"] == "UserPromptSubmit")
        checks.append(event["hook_event_name"])
    with sqlite3.connect(state / "probe.sqlite3") as db:
        assert [row[0] for row in db.execute("SELECT event FROM observations ORDER BY sequence")] == [e["hook_event_name"] for e in events]
        count_before = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    wrong_cwd = common | events[1] | {"cwd":str(ROOT)}
    result = subprocess.run(command,input=json.dumps(wrong_cwd).encode(),capture_output=True,env=env,timeout=3)
    assert result.returncode == 0 and json.loads(result.stdout) == {}
    with sqlite3.connect(state / "probe.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == count_before
    checks.append("reject_non_test_cwd_without_write")
    result = subprocess.run(command[:-1] + [str(ROOT)],input=json.dumps(common | events[1]).encode(),capture_output=True,env=env,timeout=3)
    assert result.returncode != 0
    checks.append("reject_non_test_state_directory")
    result = subprocess.run(command,input=b"x"*65537,capture_output=True,env=env,timeout=3)
    assert result.returncode == 0 and json.loads(result.stdout) == {} and b"P02_PROBE_INPUT_TOO_LARGE" in result.stderr
    checks.append("oversized_callback_reported_without_payload_capture")

    os.environ.clear()
    os.environ.update(env)
    hermes_state = state / "TEST-P02-hermes"
    hermes_state.mkdir()
    os.environ["HERMES_HOME"] = str(hermes_state)
    (hermes_state / "TEST-P02-profile.json").write_text(json.dumps({"dataset":"SYNTHETIC_TEST_ONLY","purpose":"P02_HOST_INTERFACE_PROBE"}),encoding="utf-8")
    package = types.ModuleType("agent")
    package.__path__ = []
    sys.modules["agent"] = package
    source = args.hermes_source / "agent/memory_provider.py"
    official = load("agent.memory_provider", source)
    probe = load("hermes_p02_probe", ROOT / "probes/hermes/__init__.py")
    providers, registered_hooks = [], {}
    plugin_package = types.ModuleType("hermes_cli")
    plugin_package.__path__ = []
    sys.modules["hermes_cli"] = plugin_package
    plugin_api = types.ModuleType("hermes_cli.plugins")
    plugin_api.iter_hook_callbacks = lambda event: tuple(registered_hooks.get(event, []))
    sys.modules["hermes_cli.plugins"] = plugin_api
    registration = types.SimpleNamespace(register_memory_provider=providers.append,register_hook=lambda event, callback:registered_hooks.setdefault(event, []).append(callback))
    probe.register(registration)
    for _ in range(4):
        probe.register(registration)
    alias_probe = load("hermes_p02_probe_second_namespace", ROOT / "probes/hermes/__init__.py")
    alias_probe.register(registration)
    assert len(providers) == 6 and len(registered_hooks) == 7
    assert all(len(callbacks) == 1 for callbacks in registered_hooks.values())
    hooks = {event: callbacks[0] for event, callbacks in registered_hooks.items()}
    checks.append("repeated_provider_activation_and_second_namespace_keep_one_hook")
    provider = providers[0]
    assert isinstance(provider, official.MemoryProvider)
    provider.initialize("TEST-hermes",hermes_home=str(hermes_state))
    assert hooks["pre_llm_call"](session_id="",user_message="TEST_SCOPE_RECALL missing identity") is None
    assert hooks["pre_llm_call"](session_id=None,user_message="TEST_SCOPE_RECALL missing identity") is None
    with sqlite3.connect(hermes_state / "probe.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    checks.append("missing_session_never_registered_or_injected")
    response = hooks["pre_llm_call"](session_id="TEST-hermes",turn_id="TEST-turn",user_message="TEST_SCOPE_RECALL 测试接口",conversation_history=[])
    assert "context" in response
    hooks["api_request_error"](session_id="TEST-hermes", turn_id="TEST-turn", api_request_id="TEST-request", status_code=429, retry_count=0, max_retries=0, retryable=False, error={"type":"synthetic_rejection", "message":"TEST raw error must not be retained"}, request={"messages":[]})
    with sqlite3.connect(hermes_state / "probe.sqlite3") as db:
        error_metadata = json.loads(db.execute("SELECT metadata FROM observations WHERE event='api_request_error'").fetchone()[0])
        assert error_metadata["status_code"] == 429 and error_metadata["api_request_id"] == "TEST-request"
        assert "TEST raw error" not in json.dumps(error_metadata)
    checks.append("model_error_identity_and_status_without_request_or_error_body")
    assert provider.prefetch("continue") == ""
    provider.sync_turn("TEST_SCOPE_RECALL 测试接口","TEST final",session_id="TEST-hermes",messages=[])
    provider.on_session_switch("TEST-resumed")
    assert hooks["pre_llm_call"](session_id="TEST-a2a",user_message="[A2A inbound synthetic peer frame]\n\nTEST_SCOPE_RECALL probe") is not None
    checks.append("actual_a2a_frame_allows_synthetic_probe")
    with sqlite3.connect(hermes_state / "probe.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM observations WHERE event='provider_session_switch'").fetchone()[0] == 1
    checks += ["actual_MemoryProvider_ABC_compatible", "synthetic_registration_and_single_injection", "synthetic_session_switch"]
    receipt = {"status":"preflight_passed","scope":"Direct synthetic command inputs and registration recorder against the actual MemoryProvider ABC. No real host conversation or automatic callback execution.","host_callbacks_executed":False,"model_calls":0,"checks":checks,"passed":len(checks),"failed":0,"skipped":0,"exit_code":0,"duration_seconds":time.perf_counter()-start,"source_hashes":{str(source):hashlib.sha256(source.read_bytes()).hexdigest(),"probes/hermes/__init__.py":hashlib.sha256((ROOT/'probes/hermes/__init__.py').read_bytes()).hexdigest(),"probes/codex/hook_probe.py":hashlib.sha256((ROOT/'probes/codex/hook_probe.py').read_bytes()).hexdigest(),"probes/check_preflight.py":hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},"state_dirs":{"codex":str(state),"hermes":str(hermes_state)}}
    target = ROOT / "verification/P02" / f"preflight-{time.time_ns()}.json"
    target.write_text(json.dumps(receipt,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
