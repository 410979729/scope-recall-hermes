"""Per-turn evidence of the real Codex D native callback (TEST only)."""
import hashlib
import json
from pathlib import Path


def observe_simple_capture(path, before, query, session_id, result, output):
    path, output = Path(path), Path(output)
    after = path.read_bytes() if path.is_file() else b""
    snapshot = output / "simple-search-capture.jsonl"
    with snapshot.open("xb") as handle:
        handle.write(after)
    append_only = after.startswith(before)
    try:
        rows = [json.loads(line) for line in after[len(before):].decode("utf-8").splitlines() if line.strip()] if append_only else []
    except (UnicodeError, ValueError):
        rows = []
    matching = [row for row in rows if str(row.get("event", "")).replace("_", "").lower() == "userpromptsubmit"
                and row.get("history") == [{"role": "user", "text": query}]]
    turn_id = result.get("association", {}).get("turn_id")
    hooks = [event for event in result.get("hook_events", [])
             if event.get("phase") == "completed" and event.get("eventName") == "userPromptSubmit"
             and event.get("status") == "completed" and event.get("threadId") == session_id
             and bool(turn_id) and event.get("turnId") == turn_id]
    return {"status": "OBSERVED" if append_only and len(matching) == 1 and hooks else "NOT_OBSERVED",
            "capture_path": str(path), "snapshot_path": str(snapshot),
            "snapshot_sha256": hashlib.sha256(after).hexdigest(),
            "prior_bytes": len(before), "prior_sha256": hashlib.sha256(before).hexdigest(),
            "append_only": append_only, "matching_new_query_captures": len(matching),
            "matching_completed_native_hook_events": len(hooks),
            "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
            "thread_id": session_id, "turn_id": turn_id}
