import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
MARKER = "TEST_SCOPE_RECALL_P02_CONTEXT_91c7"
EVENTS = {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "Interrupt", "SessionEnd"}


def capture_test_svg(state, prompt):
    artifacts = []
    for version in ("v1", "v2"):
        source = state / "artifacts" / version / "diagram.svg"
        if str(source) not in prompt:
            continue
        resolved = source.resolve()
        if not resolved.is_relative_to(state) or resolved.stat().st_size > 16000:
            artifacts.append({"version": version, "status": "capture_gap"})
            continue
        content = resolved.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        target = state / "retained" / (digest + ".svg")
        target.parent.mkdir(exist_ok=True)
        try:
            with target.open("xb") as stream:
                stream.write(content)
        except FileExistsError:
            if target.read_bytes() != content:
                raise ValueError("TEST retained artifact identity conflict")
        artifacts.append({"version": version, "source": str(source.relative_to(state)), "sha256": digest,
                          "bytes": len(content), "retained": str(target.relative_to(state)), "status": "retained_artifact"})
    return artifacts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    state = args.state_dir.resolve()
    boundary = (ROOT / ".execution").resolve()
    if not state.is_relative_to(boundary) or not state.name.startswith("TEST-P02-"):
        raise SystemExit("Probe requires a dedicated TEST-P02 directory under this worktree's .execution")
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        print("P02_PROBE_INPUT_TOO_LARGE: callback received; payload not processed", file=sys.stderr)
        print("{}")
        return
    payload = json.loads(raw)
    event = payload.get("hook_event_name")
    session = payload.get("session_id")
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).resolve().is_relative_to(state):
        print("{}")
        return
    if event not in EVENTS or not isinstance(session, str) or not session or len(session) > 240:
        print("{}")
        return
    turn = str(payload.get("turn_id", ""))[:240]
    marker = MARKER + "_" + hashlib.sha256((session + ":" + turn).encode()).hexdigest()[:12]
    prompt = payload.get("prompt", "")
    test_prefixes = ("TEST_SCOPE_RECALL ", r"TEST\_SCOPE\_RECALL ")
    synthetic_prompt = isinstance(prompt, str) and prompt.lstrip().startswith(test_prefixes)
    output = {}
    state.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(state / "probe.sqlite3", timeout=0.1) as db:
        db.execute("CREATE TABLE IF NOT EXISTS test_sessions (id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE IF NOT EXISTS observations (sequence INTEGER PRIMARY KEY, event TEXT, session_id TEXT, turn_id TEXT, observed_ns INTEGER, metadata TEXT)")
        if event == "UserPromptSubmit" and synthetic_prompt:
            db.execute("INSERT OR IGNORE INTO test_sessions VALUES (?)", (session,))
        if event not in {"SessionStart", "SessionEnd"} and not db.execute("SELECT 1 FROM test_sessions WHERE id=?", (session,)).fetchone():
            print("{}")
            return
        if event == "UserPromptSubmit":
            output = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": f"Isolated interface probe marker for this turn: {marker}. If asked for the interface probe marker, report this exact value. This is a hook delivery test, not recalled user history."}}
        metadata = {"field_types": {key: type(value).__name__ for key, value in payload.items()},
                    "payload_sha256": hashlib.sha256(raw).hexdigest(), "payload_bytes": len(raw),
                    "test_probe_only": True, "transcript_read": False}
        for key in ("prompt", "last_assistant_message"):
            value = payload.get(key)
            if isinstance(value, str):
                metadata[f"{key}_characters"] = len(value)
                metadata[f"{key}_contains_marker"] = MARKER in value
                metadata[f"{key}_contains_current_turn_marker"] = marker in value
        if event == "UserPromptSubmit" and isinstance(prompt, str):
            metadata["prompt_prefix_codepoints"] = [ord(char) for char in prompt[:32]]
            metadata["test_prefix_offset"] = prompt.find("TEST_SCOPE_RECALL ")
            metadata["test_escaped_prefix_offset"] = prompt.find(r"TEST\_SCOPE\_RECALL ")
            metadata["test_suffix_characters"] = len(prompt) - len(prompt.rstrip())
        if output:
            metadata["emitted_output"] = output
            metadata["synthetic_prompt"] = prompt[:16000]
            metadata["artifacts"] = capture_test_svg(state, prompt)
        if event == "PostToolUse":
            metadata["tool_name"] = str(payload.get("tool_name", ""))[:120]
            metadata["tool_use_id"] = str(payload.get("tool_use_id", ""))[:240]
            response = payload.get("tool_response")
            metadata["response_keys"] = sorted(response) if isinstance(response, dict) else []
            for key in ("tool_input", "tool_response"):
                value = payload.get(key)
                encoded = json.dumps(value, ensure_ascii=False)
                metadata[key + "_json_characters"] = len(encoded)
                if len(encoded) <= 16000:
                    metadata[key] = value
        db.execute("INSERT INTO observations(event,session_id,turn_id,observed_ns,metadata) VALUES (?,?,?,?,?)",
                   (event, session, turn, time.time_ns(), json.dumps(metadata, ensure_ascii=False)))
    print(json.dumps(output))


if __name__ == "__main__":
    main()
