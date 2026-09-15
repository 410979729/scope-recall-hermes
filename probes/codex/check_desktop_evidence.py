import hashlib
import json
from pathlib import Path
import time


ROOT = Path(__file__).resolve().parents[2]


def main():
    started = time.monotonic()
    report = json.loads((ROOT / "verification/P02/codex-desktop-interface.json").read_text(encoding="utf-8"))
    rows = {row["sequence"]: row for row in report["observations"]}
    checks = {}
    checks["current_probe_identity"] = hashlib.sha256((ROOT / "probes/codex/hook_probe.py").read_bytes()).hexdigest() == report["current_probe_sha256"]
    checks["historical_failures_preserved"] = all(rows[i]["metadata"]["last_assistant_message_characters"] == 9 for i in (11, 14, 16))
    checks["current_nonce_delivery"] = all(rows[i]["metadata"]["last_assistant_message_contains_current_turn_marker"] for i in (24, 27, 33, 38, 42))
    checks["nonce_absent_from_current_prompt"] = all(not rows[i]["metadata"]["prompt_contains_current_turn_marker"] for i in (17, 25, 32, 35, 41))
    markers = [rows[i]["metadata"]["emitted_output"]["hookSpecificOutput"]["additionalContext"] for i in (17, 25, 32, 35, 41)]
    checks["distinct_per_turn_context"] = len(set(markers)) == 5
    checks["success_and_nonzero_fixture_callbacks"] = all(rows[i]["event"] == "PostToolUse" and "TEST_SCOPE_RECALL_TOOL_EXIT_" + str(code) in rows[i]["metadata"]["tool_response"] for i, code in ((22, 0), (23, 7)))
    checks["shell_exit_status_gap_explicit"] = all(rows[i]["metadata"]["field_types"]["tool_response"] == "str" for i in (22, 23)) and any("no structured exit_code" in gap for gap in report["gap_policy"])
    prompt = rows[32]["metadata"]["synthetic_prompt"]
    checks["same_name_distinct_paths_and_evaluation"] = prompt.count("## diagram.svg:") == 2 and "认可 v1 的圆形结构，否定 v2 的橙色" in prompt
    captured = rows[32]["metadata"]["artifacts"]
    retained = {item["sha256"]: item for item in report["retained_artifacts"]}
    checks["immutable_source_capture"] = len(captured) == 2 and len(retained) == 2 and all(hashlib.sha256(retained[item["sha256"]]["content_utf8"].encode("utf-8")).hexdigest() == item["sha256"] for item in captured)
    checks["fixture_source_identity"] = all(hashlib.sha256(item["content_utf8"].encode("utf-8")).hexdigest() == item["sha256"] for item in report["fixtures"])
    reopened = rows[37]["metadata"]["tool_response"]
    checks["new_session_reopens_exact_bytes"] = rows[32]["session_id"] != rows[37]["session_id"] and all(item["content_utf8"].strip() in reopened and item["sha256"].upper() in reopened for item in retained.values())
    init = report["new_context_initialization"]
    checks["fresh_context_has_no_prior_SVG"] = all("svg" not in arg.lower() for arg in init["command"]) and any(event.get("item", {}).get("text") == "TEST_CONTEXT_READY" for event in init["events"])
    checks["public_preview_tool_observed"] = rows[36]["metadata"]["tool_name"] == "mcp__codex_app__open_in_codex" and rows[36]["metadata"]["tool_response"]["isError"] is False
    checks["interrupt_and_actual_side_effect_boundary"] = rows[40]["event"] == "Interrupt" and report["interrupt_observation"]["post_interrupt_fixture_state"]["phase"] == "finished" and report["interrupt_observation"]["underlying_fixture_stopped_by_UI"] is False
    interrupted_turn = rows[40]["turn_id"]
    checks["missing_interrupted_completion_is_explicit"] = not any(row["event"] in {"Stop", "PostToolUse"} and row["turn_id"] == interrupted_turn for row in rows.values())
    checks["resume_after_interrupt"] = rows[41]["session_id"] == rows[40]["session_id"] and rows[41]["turn_id"] != interrupted_turn and rows[42]["metadata"]["last_assistant_message_contains_current_turn_marker"]
    checks["no_transcript_or_hidden_reasoning"] = report["transcripts_read"] is False and report["hidden_reasoning_retained"] is False and all(row["metadata"]["transcript_read"] is False for row in rows.values())
    result = {"scope": "Recompute assertions over actual saved public callback evidence; no host/model rerun", "checks": checks, "passed": sum(checks.values()), "failed": len(checks) - sum(checks.values()), "skipped": 0, "duration_seconds": time.monotonic() - started}
    print(json.dumps(result, ensure_ascii=False))
    return int(not all(checks.values()))


if __name__ == "__main__":
    raise SystemExit(main())
