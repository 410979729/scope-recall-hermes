"""One-shot local OpenAI-compatible transport for the G0 CLI TEST.

It is deliberately not a model gateway: it returns a fixed TEST response,
records only request metadata, and binds to loopback.  It must be run with an
explicit --run and exits after one request.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    server_version = "TEST-G0-ZeroTransport/1"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self.send_error(400, "invalid JSON")
            return
        self.server.request_payload = payload  # type: ignore[attr-defined]
        body = json.dumps(
            {
                "id": "chatcmpl-test-g0-zero",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "TEST-G0-CLI-CAPTURE-OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.server.stop_after_response = True  # type: ignore[attr-defined]

    def log_message(self, *_args) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--port", type=int, default=29992)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if not args.run:
        parser.error("--run is required")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.request_payload = None  # type: ignore[attr-defined]
    server.stop_after_response = False  # type: ignore[attr-defined]
    while not server.stop_after_response:  # type: ignore[attr-defined]
        server.handle_request()
    payload = server.request_payload  # type: ignore[attr-defined]
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(
        json.dumps(
            {"requests": 1, "external_api": False, "loopback": True, "request_sha256": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(), "model": payload.get("model") if isinstance(payload, dict) else None},
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
