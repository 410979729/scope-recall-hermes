"""One-shot bounded local meter/transport bridge for the TEST main model route.

The bridge is localhost-only and has no activity until the official Hermes
gateway sends a POST.  Every upstream POST is reserved in the shared TEST
ledger before the credential is loaded or network I/O begins.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hmac
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scope_recall.adapters.models import AuxiliaryModelError, HttpsTransport
from scope_recall.runtime.model_budget import AuxiliaryBudgetLedger, load_hermes_attempt_authorization
from probes.hermes.p11_a2a_testkit import (
    BATCH_NAME, LEDGER, LOCAL_BRIDGE_TOKEN_ENV, MAIN_BRIDGE_HOST, MAIN_MODEL, MAX_MODEL_POSTS,
    MAIN_RESERVE_OUTPUT, MAX_REQUEST_BYTES, REQUEST_TIMEOUT_SECONDS,
    RESERVE_INPUT, UPSTREAM_ENDPOINT, UPSTREAM_KEY_ENV,
    active_bridge_ledger, assert_env_budget_matches_isolated, budget_policy,
    digest_bytes, resolve_hash_bound_runtime_budget, scrub, write_json,
)

FORMAL_BATCH_NAME = "P18_EVALUATION"


def _hermes_attempt_authorization() -> dict | None:
    """Load a hash-bound Hermes amendment; never copy or reset the original ledger."""
    return load_hermes_attempt_authorization()


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _usage(payload: object) -> dict[str, int] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("usage"), dict):
        return None
    raw = payload["usage"]
    prompt = raw.get("prompt_tokens")
    completion = raw.get("completion_tokens")
    if type(prompt) is int and type(completion) is int:
        return {"prompt_tokens": prompt, "completion_tokens": completion}
    return None


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "ScopeRecallTestMeter/1"

    def log_message(self, _format, *_args):
        return

    def _send(self, status: int, payload: object) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - stdlib HTTP handler API
        if self.path == "/health":
            self._send(200, {"ready": True, "route": "main"})
        else:
            self._send(404, {"error": "TEST route not found"})

    def do_POST(self):  # noqa: N802 - stdlib HTTP handler API
        bridge = self.server.bridge  # type: ignore[attr-defined]
        if self.path != "/v1/chat/completions":
            self._send(404, {"error": "TEST route not found"})
            return
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + bridge.local_token
        if not bridge.local_token or not hmac.compare_digest(supplied, expected):
            self._send(401, {"error": "TEST local authorization required"})
            return
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_REQUEST_BYTES:
            self._send(413, {"error": "TEST request size bound"})
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self._send(400, {"error": "TEST truncated request"})
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"error": "TEST JSON required"})
            return
        if not isinstance(payload, dict) or payload.get("model") != MAIN_MODEL:
            self._send(400, {"error": "TEST main model route required"})
            return
        request_tag = uuid4().hex[:16]
        required_marker = "TEST_P11_REGISTER_TRACE" if bridge.zero_model_diagnostic else "TEST_SCOPE_RECALL"
        if required_marker not in body.decode("utf-8", errors="replace"):
            self._send(400, {"error": "TEST marker required"})
            return
        if bridge.zero_model_diagnostic:
            bridge.diagnostic_requests += 1
            bridge._archive(request_tag, body, payload, {
                "status": "diagnostic_rejected_before_ledger",
                "ledger_reservation_attempted": False,
                "credential_load_attempted": False,
                "upstream_post_attempted": False,
                "transport_calls": bridge.transport_calls,
            })
            self._send(503, {"error": "TEST zero-model diagnostic rejection"})
            return
        bridge.handle_request(body, payload, self)


class FormalP18BudgetLedger(AuxiliaryBudgetLedger):
    """Same original ledger; covered historical meter_breach rows stay meter_breach."""


class Bridge:
    def __init__(
        self,
        state: Path,
        port: int,
        *,
        zero_model_diagnostic: bool = False,
        formal_active_operation: bool = False,
        formal_freeze_sha256: str | None = None,
        formal_config_path: Path | None = None,
        runtime_config_path: Path | None = None,
        runtime_config_sha256: str | None = None,
    ) -> None:
        self.state = state.resolve()
        self.port = port
        self.zero_model_diagnostic = zero_model_diagnostic
        self.formal_active_operation = formal_active_operation
        self.formal_freeze_sha256 = formal_freeze_sha256.lower() if isinstance(formal_freeze_sha256, str) else None
        self.formal_config_path = Path(formal_config_path).resolve() if formal_config_path is not None else None
        self.runtime_config_path = Path(runtime_config_path).resolve() if runtime_config_path is not None else None
        self.runtime_config_sha256 = runtime_config_sha256.lower() if isinstance(runtime_config_sha256, str) else None
        self.diagnostic_requests = 0
        self.transport_calls = 0
        self.local_token = os.environ.get(LOCAL_BRIDGE_TOKEN_ENV, "")
        ledger = active_bridge_ledger(
            formal_config_path=self.formal_config_path,
            formal_freeze_sha256=self.formal_freeze_sha256,
        )
        if ledger.resolve() != LEDGER.resolve():
            if self.runtime_config_path is None or not self.runtime_config_sha256:
                raise ValueError("isolated TEST ledger requires hash-bound runtime budget")
            policy = resolve_hash_bound_runtime_budget(
                self.runtime_config_path, self.runtime_config_sha256, ledger
            )
            assert_env_budget_matches_isolated(policy)
        else:
            policy = budget_policy()
            if self.formal_active_operation:
                # Shared-ledger P11/P18 formal keeps the original batch name.
                # Isolated TEST configs keep their authorized batch/caps above.
                policy = replace(policy, batch=FORMAL_BATCH_NAME)
        auth = _hermes_attempt_authorization()
        self.attempt_authorization = auth
        self.ledger = FormalP18BudgetLedger(
            ledger,
            policy,
            covered_historical_breaches=tuple(auth.get("covered_historical_breaches", ())) if auth else (),
        )
        self.transport = HttpsTransport()

    def _active_binding(self) -> dict | None:
        """Read the redacted P18 marker before touching the model ledger."""
        if not self.formal_active_operation:
            return None
        if not self.formal_freeze_sha256 or len(self.formal_freeze_sha256) != 64:
            return None
        marker = self.state / "active-operation.json"
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or set(value) != {
            "schema", "operation_id", "request_id", "task_id", "context_id", "formal_config_sha256", "expires_ns"
        }:
            return None
        if value.get("schema") != "scope-recall.p18.hermes-active-operation.v1":
            return None
        if self.formal_freeze_sha256 and value.get("formal_config_sha256") != self.formal_freeze_sha256:
            return None
        if type(value.get("expires_ns")) is not int or value["expires_ns"] <= time.time_ns():
            return None
        if any(not isinstance(value.get(key), str) or not value[key].strip() for key in ("operation_id", "request_id", "task_id", "context_id", "formal_config_sha256")):
            return None
        return value

    def batch_has_room(self) -> bool:
        if self.formal_active_operation:
            # AuxiliaryBudgetLedger.reserve enforces the shared formal caps
            # atomically across every batch.  There is no P11 diagnostic cap
            # on the formal P18 batch.
            return True
        # The shared runtime ledger enforces the global 8000-call cap.  This
        # TEST-only read adds the P11 batch's independent eight-call ceiling;
        # the launcher is single-controller, so the check is intentionally
        # bounded and followed immediately by the atomic global reservation.
        uri = self.ledger.path
        with sqlite3.connect(uri.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
            count = int(db.execute("SELECT count(*) FROM requests WHERE batch=?", (BATCH_NAME,)).fetchone()[0])
        return count < MAX_MODEL_POSTS

    def handle_request(self, body: bytes, payload: dict, handler: BridgeHandler) -> None:
        active_binding = self._active_binding()
        if self.formal_active_operation and active_binding is None:
            self._archive(uuid4().hex[:16], body, payload, {"status": "formal_operation_binding_missing", "ledger_reservation_attempted": False})
            handler._send(409, {"error": "TEST formal operation binding required"})
            return
        request_tag = uuid4().hex[:16]
        request_id = None
        reserve_error = None
        if not self.batch_has_room():
            reserve_error = "batch_call_cap"
        try:
            # This is deliberately the first operation that can authorize a
            # model request. Credential loading and upstream I/O follow it.
            if reserve_error is None:
                reserved_input = RESERVE_INPUT
                reserved_output = MAIN_RESERVE_OUTPUT
                if self.attempt_authorization:
                    reserved_input = int(self.attempt_authorization.get("reserved_input") or reserved_input)
                    reserved_output = int(self.attempt_authorization.get("reserved_output") or reserved_output)
                request_id = self.ledger.reserve(
                    MAIN_MODEL, body, reserved_input=reserved_input,
                    reserved_output=reserved_output,
                    timeout_seconds=REQUEST_TIMEOUT_SECONDS,
                )
        except Exception as exc:  # bounded TEST error, never a retry
            reserve_error = type(exc).__name__ + ": " + str(exc)
        if request_id is None:
            self._archive(request_tag, body, payload, {"status": "reserve_rejected", "error": reserve_error})
            handler._send(429, {"error": "TEST budget reservation rejected"})
            return

        upstream_key = os.environ.get(UPSTREAM_KEY_ENV, "")
        if not upstream_key:
            final_status = self.ledger.finish(request_id, "network_error", None, timeout_seconds=REQUEST_TIMEOUT_SECONDS)
            self._archive(request_tag, body, payload, {"status": "missing_upstream_key", "ledger_status": final_status})
            handler._send(503, {"error": "TEST upstream credential is not loaded"})
            return

        status = 502
        response_body = b""
        error_type = None
        try:
            # Preserve the exact validated/reserved bytes. HttpsTransport
            # provides the absolute deadline, no redirect, and response cap.
            self.transport_calls += 1
            status, response_body = self.transport.post(
                UPSTREAM_ENDPOINT, body=body,
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + upstream_key, "x-opencode-session": os.environ.get("SCOPE_RECALL_TEST_OPENCODE_SESSION", "scope-recall-test-p11-bridge")},
                timeout_seconds=REQUEST_TIMEOUT_SECONDS,
                max_response_bytes=1_048_576,
            )
        except AuxiliaryModelError as exc:
            error_type = exc.error_type
        response_payload = None
        if response_body:
            try:
                response_payload = json.loads(response_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                response_payload = {"raw_response_sha256": digest_bytes(response_body)}
        usage = _usage(response_payload)
        final_status = self.ledger.finish(
            request_id, "completed" if error_type is None and status < 400 else "network_error",
            usage, timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        )
        receipt = {
            "request_tag": request_tag, "ledger_request_id": request_id,
            "route": "main", "model": MAIN_MODEL,
            "request_bytes": len(body), "request_sha256": digest_bytes(body),
            "request": scrub(payload), "response": scrub(response_payload),
            "response_bytes": len(response_body), "http_status": status,
            "error_type": error_type, "error_present": bool(error_type), "usage": usage,
            "ledger_status": final_status, "upstream": UPSTREAM_ENDPOINT,
            "credential_values_written": False,
            "p18_active_operation": active_binding,
        }
        self._archive(request_tag, body, payload, receipt, response_payload=response_payload)
        if error_type is not None:
            handler._send(502, {"error": "TEST upstream request failed", "ledger_status": final_status})
        else:
            handler._send(status, response_payload if response_payload is not None else {"error": "TEST empty upstream response"})

    def _archive(self, tag: str, body: bytes, payload: object, receipt: dict, *, response_payload: object = None) -> None:
        record = dict(receipt)
        record.setdefault("request", scrub(payload))
        if response_payload is not None:
            record.setdefault("response", scrub(response_payload))
        record["request_sha256"] = digest_bytes(body)
        raw_path = self.state / "archive" / f"bridge-request-{tag}.body"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(body)
        record["request_artifact_path"] = str(raw_path)
        record["request_artifact_sha256"] = digest_bytes(body)
        write_json(self.state / "archive" / f"bridge-request-{tag}.json", scrub(record))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--port", type=int, default=29991)
    parser.add_argument("--zero-model-diagnostic", action="store_true")
    parser.add_argument("--formal-active-operation", action="store_true")
    parser.add_argument("--formal-freeze-sha256")
    parser.add_argument("--formal-config", type=Path)
    parser.add_argument("--runtime-config", type=Path)
    parser.add_argument("--runtime-config-sha256")
    args = parser.parse_args()
    if not 29991 <= args.port <= 30100 or (not args.formal_active_operation and args.port != 29991):
        raise SystemExit("TEST bridge requires port 29991; formal isolated runs allow 29991..30100")
    if args.formal_config is not None and not args.formal_freeze_sha256:
        raise SystemExit("TEST formal config path and hash must be supplied together")
    if (args.runtime_config is None) != (args.runtime_config_sha256 is None):
        raise SystemExit("TEST runtime config path and hash must be supplied together")
    bridge = Bridge(
        args.state,
        args.port,
        zero_model_diagnostic=args.zero_model_diagnostic,
        formal_active_operation=args.formal_active_operation,
        formal_freeze_sha256=args.formal_freeze_sha256,
        formal_config_path=args.formal_config,
        runtime_config_path=args.runtime_config,
        runtime_config_sha256=args.runtime_config_sha256,
    )
    server = ThreadingHTTPServer((MAIN_BRIDGE_HOST, args.port), BridgeHandler)
    server.bridge = bridge  # type: ignore[attr-defined]
    print(json.dumps({"ready": True, "route": "main", "port": args.port}), flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        write_json(bridge.state / "archive" / f"bridge-diagnostic-final-{uuid4().hex[:16]}.json", {
            "zero_model_diagnostic": bridge.zero_model_diagnostic,
            "diagnostic_requests": bridge.diagnostic_requests,
            "transport_calls": bridge.transport_calls,
            "credential_values_written": False,
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
