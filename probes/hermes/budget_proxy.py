import argparse
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time


ROOT = Path(__file__).resolve().parents[2]
UPSTREAM_HOST = "opencode.ai"
UPSTREAM_PATH = "/zen/go/v1/chat/completions"
MODEL = "glm-5.3-flash"
MAX_REQUESTS = 12
MAX_BYTES = 768 * 1024
MAX_OUTPUT_TOKENS = 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
BUDGET_STATE = ROOT / ".execution/TEST-P02-hermes"


class BudgetDenied(ValueError):
    pass


def validate_body(raw):
    if not raw or len(raw) > MAX_BYTES:
        raise BudgetDenied("request_size")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        raise BudgetDenied("invalid_json") from None
    if not isinstance(body, dict) or body.get("model") != MODEL:
        raise BudgetDenied("model")
    limits = [body[k] for k in ("max_tokens", "max_completion_tokens") if k in body]
    if not limits or any(type(x) is not int or not 0 < x <= MAX_OUTPUT_TOKENS for x in limits):
        raise BudgetDenied("output_limit")
    if type(body.get("n", 1)) is not int or body.get("n", 1) != 1:
        raise BudgetDenied("completion_count")
    messages = body.get("messages")
    if not isinstance(messages, list) or not any(
        isinstance(m, dict) and m.get("role") == "user"
        and "TEST_SCOPE_RECALL " in json.dumps(m.get("content"), ensure_ascii=False)
        for m in messages
    ):
        raise BudgetDenied("synthetic_marker")
    return body


class Ledger:
    def __init__(self, directory):
        self.path = Path(directory) / "call-budget.sqlite3"
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, request_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, started_ns INTEGER NOT NULL, status TEXT NOT NULL)")
            if "metadata" not in [row[1] for row in db.execute("PRAGMA table_info(requests)")]:
                db.execute("ALTER TABLE requests ADD COLUMN metadata TEXT")

    def reserve(self, raw):
        body = validate_body(raw)
        metadata = {"messages":[{"role":m.get("role"), "content_type":type(m.get("content")).__name__, "probe_marker_count":json.dumps(m.get("content"), ensure_ascii=False).count("TEST_SCOPE_RECALL_P02_HERMES_CONTEXT_42a9")} for m in body["messages"] if isinstance(m,dict)], "max_tokens":body.get("max_tokens"), "max_completion_tokens":body.get("max_completion_tokens")}
        with sqlite3.connect(self.path, timeout=2) as db:
            db.execute("BEGIN IMMEDIATE")
            count, used = db.execute("SELECT COUNT(*), COALESCE(SUM(request_bytes),0) FROM requests").fetchone()
            if count >= MAX_REQUESTS or used + len(raw) > MAX_BYTES:
                raise BudgetDenied("budget_exhausted")
            return db.execute("INSERT INTO requests(request_bytes,sha256,started_ns,status,metadata) VALUES (?,?,?,?,?)", (len(raw), hashlib.sha256(raw).hexdigest(), time.time_ns(), "reserved_before_network", json.dumps(metadata))).lastrowid

    def finish(self, request_id, status):
        with sqlite3.connect(self.path, timeout=2) as db:
            db.execute("UPDATE requests SET status=? WHERE id=?", (status, request_id))

    def snapshot(self):
        with sqlite3.connect(self.path) as db:
            count, used = db.execute("SELECT COUNT(*), COALESCE(SUM(request_bytes),0) FROM requests").fetchone()
        return {"reserved_requests_including_failures":count, "request_json_bytes":used, "max_requests":MAX_REQUESTS, "max_request_json_bytes":MAX_BYTES}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, reason):
        raw = json.dumps({"error":{"type":"p02_probe_gate", "message":reason}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.reply(404, "unsupported_route")

    def do_POST(self):
        self.connection.settimeout(60)
        if self.path != "/v1/chat/completions":
            self.reply(404, "unsupported_route")
            return
        expected = "Bearer " + self.server.local_token
        if not secrets.compare_digest(self.headers.get("Authorization", ""), expected):
            self.reply(401, "local_token")
            return
        try:
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                raise BudgetDenied("content_length")
            size = int(self.headers["Content-Length"])
            if not 0 < size <= MAX_BYTES:
                raise BudgetDenied("request_size")
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise BudgetDenied("truncated_request")
            request_id = self.server.ledger.reserve(raw)
        except (ValueError, TimeoutError, OSError) as error:
            self.reply(429 if str(error) == "budget_exhausted" else 400, str(error) if isinstance(error, BudgetDenied) else "invalid_request")
            return
        connection = http.client.HTTPSConnection(UPSTREAM_HOST, timeout=60)
        status = "network_error"
        try:
            headers = {"Authorization":"Bearer " + self.server.upstream_key, "Content-Type":"application/json", "User-Agent":"ScopeRecall-P02-Hermes/0.21.0", "x-opencode-session":self.server.test_session}
            connection.request("POST", UPSTREAM_PATH, body=raw, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            received = 0
            while block := response.read1(8192):
                received += len(block)
                if received > MAX_RESPONSE_BYTES:
                    status = "response_limit"
                    break
                self.wfile.write(block)
                self.wfile.flush()
            else:
                status = f"http_{response.status}"
        except (OSError, http.client.HTTPException):
            self.close_connection = True
        finally:
            connection.close()
            self.server.ledger.finish(request_id, status)


def make_server(directory, key, local_token, port=0):
    if not key or not local_token:
        raise BudgetDenied("credential_missing")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.ledger = Ledger(directory)
    server.upstream_key = key
    server.local_token = local_token
    server.test_session = "scope-recall-p02-" + Path(directory).name
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    state = args.state_dir.resolve()
    if not state.is_relative_to((ROOT / ".execution").resolve()) or not state.name.startswith("TEST-P02-"):
        raise SystemExit("P02 isolated state required")
    approval = json.loads((ROOT / "verification/P02/model-budget-authorization.json").read_text(encoding="utf-8"))
    if approval.get("status") != "approved" or approval.get("route") != "opencode-go / glm-5.3-flash" or approval.get("max_requests") != MAX_REQUESTS or approval.get("max_request_json_bytes") != MAX_BYTES or approval.get("max_output_tokens") != MAX_OUTPUT_TOKENS:
        raise SystemExit("P02 budget authorization mismatch")
    # State/output directories may differ; the owner-authorized batch ledger may not.
    server = make_server(BUDGET_STATE, os.environ.get("OPENCODE_GO_API_KEY", ""), os.environ.get("SCOPE_RECALL_P02_LOCAL_TOKEN", ""))
    endpoint = {"base_url":f"http://127.0.0.1:{server.server_port}/v1", "upstream":"https://opencode.ai/zen/go/v1", "model":MODEL, "pid":os.getpid(), "ledger":str(server.ledger.path)}
    (state / "budget-endpoint.json").write_text(json.dumps(endpoint, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(endpoint), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
