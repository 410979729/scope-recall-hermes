"""Send exactly one synthetic A2A request, only with an explicit ``--run``."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time
import urllib.error
import urllib.request

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from probes.hermes.p11_a2a_testkit import (
    A2A_AGENT_NAME, A2A_HOST, A2A_PORT, ARCHIVE, CORE_DB, LEDGER, MAIN_MODEL, MAX_MODEL_POSTS,
    RUNTIME_RECORD, STATE, TEST_CONTEXT, assert_test_path, digest_bytes, load_json,
    scrub, write_json,
)


def _get_json(url: str, timeout: float = 5.0) -> object:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout) as response:
        return json.loads(response.read(786432).decode("utf-8"))


def _core_readback() -> dict:
    if not CORE_DB.is_file():
        return {"database_present": False, "native_gem2_query": False}
    uri = CORE_DB.as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=2) as db:
        source_count = int(db.execute("SELECT count(*) FROM source_events").fetchone()[0])
        complete_count = int(db.execute("SELECT count(*) FROM source_events WHERE capture_state='complete'").fetchone()[0])
        lexical_count = int(db.execute("SELECT count(*) FROM lexical_projection").fetchone()[0])
        origins = {str(row[0]): int(row[1]) for row in db.execute("SELECT origin,count(*) FROM source_events GROUP BY origin")}
    return {
        "database_present": True, "source_events": source_count,
        "complete_source_events": complete_count, "lexical_projection_rows": lexical_count,
        "source_origins": origins, "native_gem2_query": False,
        "basic_boundary": "lexical Core readback only; no native Gem2 vector query and no semantic P18 claim",
    }


def _latest_bridge() -> dict | None:
    candidates = sorted(ARCHIVE.glob("bridge-request-*.json"), key=lambda item: item.stat().st_mtime_ns)
    if not candidates:
        return None
    value = load_json(candidates[-1])
    request_text = json.dumps(value.get("request", {}), ensure_ascii=False)
    value["l3_marker_once"] = request_text.count("TEST_SCOPE_RECALL") == 1
    value["l3_recall_block_present"] = "recall" in request_text.lower() or "记忆" in request_text
    value["archive_path"] = str(candidates[-1])
    return scrub(value)


def _ledger_status() -> dict:
    ledger = LEDGER
    if not ledger.is_file():
        return {"ledger_present": False}
    with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
        rows = db.execute("SELECT model,status,request_bytes,reserved_input,reserved_output FROM requests ORDER BY id").fetchall()
    return {"ledger_present": True, "requests": len(rows), "rows": [
        {"model": row[0], "status": row[1], "request_bytes": row[2],
         "reserved_input": row[3], "reserved_output": row[4]} for row in rows
    ]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="make the one real local A2A POST")
    parser.add_argument("--diagnostic-register", action="store_true", help="one TEST A2A load-path probe; local bridge rejects before ledger/upstream")
    parser.add_argument("--context-id", default=TEST_CONTEXT, help="TEST A2A contextId; alternate values are for isolation contrast only")
    parser.add_argument("--label", default=None, help="TEST-only receipt/request label")
    parser.add_argument("--text", default=None, help="TEST-only message text override, e.g. /new or /approve")
    args = parser.parse_args()
    if not STATE.is_dir():
        print(json.dumps({"run": False, "reason": "prepare_required", "state": str(STATE)}, sort_keys=True))
        return 2
    if not args.run:
        print(json.dumps({"run": False, "dry_run": True, "method": "message/send", "url": f"http://{A2A_HOST}:{A2A_PORT}",
                          "agent_name": A2A_AGENT_NAME, "model_posts": 0,
                          "basic_boundary": "lexical Core only; no native Gem2 query"}, ensure_ascii=False, sort_keys=True))
        return 0
    if not RUNTIME_RECORD.is_file():
        print(json.dumps({"run": False, "reason": "start_required", "state": str(STATE)}, sort_keys=True))
        return 2
    record = load_json(RUNTIME_RECORD)
    if record.get("gateway_url") != f"http://{A2A_HOST}:{A2A_PORT}" or record.get("card_url", "").endswith("agent-card.json") is False:
        print(json.dumps({"run": False, "reason": "not_a_TEST_runtime_record"}, sort_keys=True))
        return 2
    card_url = f"http://{A2A_HOST}:{A2A_PORT}/.well-known/agent-card.json"
    card = _get_json(card_url)
    if not isinstance(card, dict) or card.get("name") != A2A_AGENT_NAME:
        raise SystemExit("Refusing a non-TEST Hermes agent card")
    if args.diagnostic_register:
        label = args.label or "TEST-P11-A2A-v4-register-trace-1"
        prompt = "TEST_P11_REGISTER_TRACE"
    else:
        label = args.label or "TEST-P11-A2A-v4-ordinary-1"
        prompt = args.text if args.text is not None else (
            "TEST_SCOPE_RECALL 请根据当前 TEST 记忆回答：这是一条普通 A2A 接线验证。"
            "请说明你收到的 TEST 记忆提示，并明确这是 synthetic TEST；不要使用工具，不要读取文件。")
    if not label.startswith("TEST-"):
        raise SystemExit("TEST request label must start with TEST-")
    sent = {"jsonrpc": "2.0", "id": label, "method": "message/send", "params": {
        "message": {"messageId": label, "role": "ROLE_USER",
                     "parts": [{"text": prompt}], "contextId": args.context_id},
        "configuration": {"returnImmediately": False},
    }}
    body = json.dumps(sent, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"http://{A2A_HOST}:{A2A_PORT}", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    started = time.perf_counter()
    status = None
    raw = b""
    error = None
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=50.0) as response:
            status = int(response.status)
            raw = response.read(786432)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        error = type(exc).__name__
    response = None
    if raw:
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            response = {"raw_sha256": digest_bytes(raw)}
    receipt = {
        "scenario": "register_trace_zero_model" if args.diagnostic_register else ("command" if prompt.startswith("/") else "ordinary"), "transport": "A2A JSON-RPC message/send",
        "agent_card": {"url": card_url, "name": card.get("name"), "version": card.get("version")},
        "request": scrub(sent), "request_sha256": digest_bytes(body), "request_bytes": len(body),
        "http_status": status, "response": scrub(response), "response_bytes": len(raw),
        "response_sha256": digest_bytes(raw), "error_type": error,
        "duration_seconds": round(time.perf_counter() - started, 6),
        "l1_transport": {"a2a_post_completed": error is None, "single_post": True, "retry_count": 0},
        "l2_core": _core_readback(), "l3_bridge": _latest_bridge(), "ledger": _ledger_status(),
        "l4_final_task_reply": scrub(response),
        "model_route": {"main": MAIN_MODEL, "auxiliary": "mimo-v2.5"},
        "model_posts_upper_bound": MAX_MODEL_POSTS, "native_gem2_query": False,
        "semantic_p18_acceptance": False,
    }
    destination = ARCHIVE / f"a2a-{time.time_ns()}.json"
    write_json(destination, scrub(receipt))
    print(json.dumps({"path": str(destination), "http_status": status, "error_type": error,
                      "ledger_requests": receipt["ledger"].get("requests"),
                      "l1_transport": receipt["l1_transport"],
                      "l2_source_events": receipt["l2_core"].get("source_events"),
                      "l3_bridge_present": receipt["l3_bridge"] is not None,
                      "l4_present": response is not None}, ensure_ascii=False, sort_keys=True))
    return 0 if error is None else 2


if __name__ == "__main__":
    raise SystemExit(main())
