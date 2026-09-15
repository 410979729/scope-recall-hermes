import json
from pathlib import Path
import sqlite3
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def invoke(state, event, turn="TEST-turn", **fields):
    payload = dict(hook_event_name=event, session_id="TEST-session", turn_id=turn, cwd=str(state), **fields)
    result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "probes/codex/hook_probe.py"), "--state-dir", str(state)], input=json.dumps(payload), text=True, encoding="utf-8", capture_output=True, check=True)
    return json.loads(result.stdout)


def test_current_turn_nonce_is_replay_stable_and_not_previous_turn(tmp_path):
    state = tmp_path / "TEST-P02-nonce"
    fields = {"prompt": "TEST_SCOPE_RECALL report current marker"}
    first = invoke(state, "UserPromptSubmit", **fields)
    assert invoke(state, "UserPromptSubmit", **fields) == first
    second = invoke(state, "UserPromptSubmit", turn="TEST-new-turn", **fields)
    assert first != second
    context = first["hookSpecificOutput"]["additionalContext"]
    marker = context.split(": ", 1)[1].split(".", 1)[0]
    assert invoke(state, "Stop", last_assistant_message=marker) == {}
    with sqlite3.connect(state / "probe.sqlite3") as db:
        metadata = json.loads(db.execute("SELECT metadata FROM observations WHERE event='Stop'").fetchone()[0])
    assert metadata["last_assistant_message_contains_current_turn_marker"] is True


def test_unmarked_session_cannot_capture_tool_payload(tmp_path):
    state = tmp_path / "TEST-P02-unmarked"
    assert invoke(state, "PostToolUse", tool_name="TEST-tool", tool_response={"output": "TEST-data"}) == {}
    with sqlite3.connect(state / "probe.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_only_registered_test_session_can_capture_attachment_wrapped_prompt(tmp_path):
    state = tmp_path / "TEST-P02-attachment"
    prompt = '# Files\n<svg><title>TEST</title></svg>\nTEST_SCOPE_RECALL retain v1'
    assert invoke(state, "UserPromptSubmit", prompt=prompt) == {}
    invoke(state, "UserPromptSubmit", prompt="TEST_SCOPE_RECALL register synthetic session")
    result = invoke(state, "UserPromptSubmit", turn="TEST-attachment", prompt=prompt)
    assert "additionalContext" in result["hookSpecificOutput"]
    with sqlite3.connect(state / "probe.sqlite3") as db:
        metadata = json.loads(db.execute("SELECT metadata FROM observations WHERE turn_id='TEST-attachment'").fetchone()[0])
    assert metadata["synthetic_prompt"] == prompt


def test_whitespace_wrapped_synthetic_input_keeps_exact_recorded_prompt(tmp_path):
    state = tmp_path / "TEST-P02-whitespace"
    prompt = "\n\nTEST_SCOPE_RECALL report current marker\n"
    result = invoke(state, "UserPromptSubmit", prompt=prompt)
    assert "additionalContext" in result["hookSpecificOutput"]
    with sqlite3.connect(state / "probe.sqlite3") as db:
        metadata = json.loads(db.execute("SELECT metadata FROM observations").fetchone()[0])
    assert metadata["synthetic_prompt"] == prompt
    assert metadata["test_prefix_offset"] == 2


def test_desktop_markdown_escaped_test_prefix_is_recognized_without_rewriting_source(tmp_path):
    state = tmp_path / "TEST-P02-desktop-markdown"
    prompt = r"TEST\_SCOPE\_RECALL report current marker" + "\n"
    result = invoke(state, "UserPromptSubmit", prompt=prompt)
    assert "additionalContext" in result["hookSpecificOutput"]
    with sqlite3.connect(state / "probe.sqlite3") as db:
        metadata = json.loads(db.execute("SELECT metadata FROM observations").fetchone()[0])
    assert metadata["synthetic_prompt"] == prompt
    assert metadata["test_prefix_offset"] == -1
    assert metadata["test_escaped_prefix_offset"] == 0


def test_test_tool_response_is_bounded_and_preserves_exit_status(tmp_path):
    state = tmp_path / "TEST-P02-tool"
    invoke(state, "UserPromptSubmit", prompt="TEST_SCOPE_RECALL inspect fixture")
    assert invoke(state, "PostToolUse", tool_name="TEST-tool", tool_use_id="TEST-id", tool_input={"fixture": "TEST-fixture"}, tool_response={"exit_code": 7, "output": "TEST-error"}) == {}
    assert invoke(state, "PostToolUse", tool_name="TEST-tool", tool_response={"output": "x" * 16001}) == {}
    with sqlite3.connect(state / "probe.sqlite3") as db:
        rows = [json.loads(r[0]) for r in db.execute("SELECT metadata FROM observations WHERE event='PostToolUse' ORDER BY sequence")]
    assert rows[0]["tool_response"]["exit_code"] == 7
    assert "tool_response" not in rows[1]
    assert rows[1]["tool_response_json_characters"] > 16000


def test_test_svg_capture_preserves_old_bytes_after_same_path_changes(tmp_path):
    state = tmp_path / "TEST-P02-svg"
    source = state / "artifacts/v1/diagram.svg"
    source.parent.mkdir(parents=True)
    first = b'<svg><circle fill="#0066ff"/></svg>\n'
    second = b'<svg><rect fill="#ff8800"/></svg>\n'
    prompt = "TEST_SCOPE_RECALL retain attachment: " + str(source)
    source.write_bytes(first)
    invoke(state, "UserPromptSubmit", prompt=prompt)
    source.write_bytes(second)
    invoke(state, "UserPromptSubmit", turn="TEST-second-version", prompt=prompt)
    with sqlite3.connect(state / "probe.sqlite3") as db:
        artifacts = [json.loads(r[0])["artifacts"][0] for r in db.execute("SELECT metadata FROM observations ORDER BY sequence")]
    assert artifacts[0]["sha256"] != artifacts[1]["sha256"]
    assert (state / artifacts[0]["retained"]).read_bytes() == first
    assert (state / artifacts[1]["retained"]).read_bytes() == second
