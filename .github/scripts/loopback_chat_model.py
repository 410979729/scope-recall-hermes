"""A loopback stand-in for an OpenAI-compatible chat endpoint, for exercising a Hermes install without model keys.

Every request body is appended as one JSON line to the log named on the command line; every chat completion answers
"OK", streamed or not as the request asks. It serves 127.0.0.1 only.

    python loopback_chat_model.py <port> <requests.jsonl>
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL = "loopback-model"
USAGE = {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}


def _chunk(created: int, delta: dict[str, str], finish: str | None) -> dict[str, object]:
    head = {"id": "loopback", "object": "chat.completion.chunk", "created": created, "model": MODEL}
    return dict(head, choices=[{"index": 0, "delta": delta, "finish_reason": finish}])


class Handler(BaseHTTPRequestHandler):
    log_path = Path("requests.jsonl")

    def _send(self, status: int, payload: object, content_type: str = "application/json") -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, {"object": "list", "data": [{"id": MODEL, "object": "model"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            request = json.loads(raw or b"{}")
        except ValueError:
            request = {"unparsed": raw.decode("utf-8", "replace")}
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(json.dumps({"at": time.time(), "path": self.path, "body": request}) + "\n")
        created = int(time.time())
        if isinstance(request, dict) and request.get("stream"):
            last = dict(_chunk(created, {}, "stop"), usage=USAGE)
            chunks = [_chunk(created, {"role": "assistant", "content": "OK"}, None), last]
            body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks) + b"data: [DONE]\n\n"
            self._send(200, body, "text/event-stream")
            return
        message = {"role": "assistant", "content": "OK"}
        choice = {"index": 0, "message": message, "finish_reason": "stop"}
        completion = {"id": "loopback", "object": "chat.completion", "created": created, "model": MODEL}
        self._send(200, dict(completion, choices=[choice], usage=USAGE))

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def main() -> None:
    Handler.log_path = Path(sys.argv[2])
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()


if __name__ == "__main__":
    main()
