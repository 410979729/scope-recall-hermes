import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).with_name("hook_probe.py")


def main():
    state = ROOT / ".execution" / f"TEST-P02-codex-command-{time.time_ns()}"
    state.mkdir(parents=True)
    command = [sys.executable, "-I", "-B", str(SCRIPT), "--state-dir", str(state)]
    env = {k:v for k,v in os.environ.items() if k.upper() in {"SYSTEMROOT","WINDIR","COMSPEC","PATH","PATHEXT"}}
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "CODEX_HOME", "HERMES_HOME"):
        target = state / key.lower()
        target.mkdir()
        env[key] = str(target)
    payload = dict(hook_event_name="UserPromptSubmit", session_id="TEST-command-only", turn_id="TEST-turn", prompt="TEST_SCOPE_RECALL 检查接口标记。", cwd=str(state), model="NO_MODEL_CALLED")
    runs = []
    for number in range(10):
        payload["turn_id"] = f"TEST-turn-{number}"
        start = time.perf_counter()
        result = subprocess.run(command, input=json.dumps(payload).encode(), capture_output=True, env=env, timeout=5)
        runs.append({"seconds":time.perf_counter()-start,"exit_code":result.returncode,"output_valid":result.returncode == 0 and "hookSpecificOutput" in json.loads(result.stdout)})
    config_state = ROOT / ".execution" / "TEST-P02-codex-desktop"
    config_state.mkdir(exist_ok=True)
    windows_command = subprocess.list2cmdline([sys.executable,"-I","-B",str(SCRIPT),"--state-dir",str(config_state)])
    config = {"description":"Scope Recall P02 synthetic interface probe. Enable only in a dedicated test profile.","hooks":{event:[{"hooks":[{"type":"command","command":windows_command,"commandWindows":windows_command,"timeout":2}]}] for event in ("SessionStart","UserPromptSubmit","PostToolUse","Stop","Interrupt","SessionEnd")}}
    config_file = Path(__file__).with_name("hooks.example.json")
    config_file.write_text(json.dumps(config,indent=2)+"\n",encoding="utf-8")
    evidence = ROOT / "verification" / "P02"
    evidence.mkdir(parents=True,exist_ok=True)
    receipt = {"kind":"synthetic_command_startup_only","host_callback_tested":False,"model_calls":0,"includes":"process startup + JSON + tiny isolated SQLite write/read; no LanceDB import","script_sha256":hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),"command":command,"state_dir":str(state),"runs":runs,"median_seconds":statistics.median(x["seconds"] for x in runs),"maximum_seconds":max(x["seconds"] for x in runs),"valid_runs":sum(x["output_valid"] for x in runs),"hooks_config":str(config_file),"hooks_config_sha256":hashlib.sha256(config_file.read_bytes()).hexdigest(),"installed_in_active_profile":False}
    (evidence / f"command-startup-{time.time_ns()}.json").write_text(json.dumps(receipt,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
