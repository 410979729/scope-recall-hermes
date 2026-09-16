"""Offline transport checks; sockets are restricted to synthetic loopback HTTP."""
from __future__ import annotations

import base64
from http.client import HTTPResponse
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import time

import pytest

from scope_recall.adapters import models


class _Socket:
    def __init__(self, wire: bytes) -> None:
        self._wire = BytesIO(wire)
        self.timeouts: list[float | None] = []

    def makefile(self, *_args):
        return self._wire

    def settimeout(self, value):
        self.timeouts.append(value)

    def close(self):
        pass


def _response(wire: bytes) -> HTTPResponse:
    response = HTTPResponse(_Socket(wire))
    response.begin()
    return response


def test_http_response_parser_consumes_chunked_framing_without_read1() -> None:
    response = _response(
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: keep-alive\r\n\r\n"
        b"5\r\nhello\r\n6\r\n world\r\n0\r\nX-Trailer: yes\r\n\r\n"
    )
    assert response.read() == b"hello world"


def test_http_response_parser_honors_content_length_on_keepalive() -> None:
    response = _response(
        b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: keep-alive\r\n\r\n"
        b"helloEXTRA-WIRE-BYTES"
    )
    assert response.read() == b"hello"


def _write_worker(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8", newline="\n")


def _worker_result(*, ok: bool, status: int | None = 200, body: bytes = b"", error: str | None = None) -> str:
    result: dict[str, object] = {
        "ok": ok,
        "status": status,
        "body_b64": base64.b64encode(body).decode("ascii"),
    }
    if error is not None:
        result["error"] = error
    return json.dumps(result, separators=(",", ":"))


def _post(monkeypatch, worker: Path, *, timeout: float = 2.0, max_response_bytes: int = 1024):
    monkeypatch.setattr(models, "_HTTP_WORKER_PATH", worker)
    return models.HttpsTransport().post(
        "https://synthetic.invalid/response",
        body=b"body",
        headers={"X-Synthetic": "ok"},
        timeout_seconds=timeout,
        max_response_bytes=max_response_bytes,
    )


def test_parent_worker_protocol_success_and_error(tmp_path, monkeypatch) -> None:
    success = tmp_path / "success_worker.py"
    _write_worker(
        success,
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        f"sys.stdout.write({ _worker_result(ok=True, body=b'response')!r})\n",
    )
    assert _post(monkeypatch, success) == (200, b"response")

    failure = tmp_path / "failure_worker.py"
    _write_worker(
        failure,
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        f"sys.stdout.write({_worker_result(ok=False, error='http_protocol')!r})\n",
    )
    with pytest.raises(models.AuxiliaryModelError) as exc:
        _post(monkeypatch, failure)
    assert exc.value.error_type == "http_protocol"

    oversized = tmp_path / "oversized_worker.py"
    _write_worker(
        oversized,
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        f"sys.stdout.write({_worker_result(ok=True, body=b'1234')!r})\n",
    )
    with pytest.raises(models.AuxiliaryModelError) as oversize_exc:
        _post(monkeypatch, oversized, max_response_bytes=3)
    assert oversize_exc.value.error_type == "response_limit"


def test_parent_worker_timeout_kills_owned_process(tmp_path, monkeypatch) -> None:
    marker = tmp_path / "completed.txt"
    timeout_worker = tmp_path / "timeout_worker.py"
    _write_worker(
        timeout_worker,
        "import pathlib, sys, time\n"
        "sys.stdin.buffer.read()\n"
        "time.sleep(2.0)\n"
        f"pathlib.Path({str(marker)!r}).write_text('completed')\n",
    )
    started = time.perf_counter()
    with pytest.raises(models.AuxiliaryModelError) as exc:
        _post(monkeypatch, timeout_worker, timeout=0.15)
    elapsed = time.perf_counter() - started
    assert exc.value.error_type == "timeout"
    assert elapsed < 1.5
    time.sleep(0.1)
    assert not marker.exists()


def test_worker_path_is_fixed_to_runtime_helper() -> None:
    expected = (Path(models.__file__).resolve().parents[1] / "runtime" / "_http_worker.py").resolve()
    assert models._HTTP_WORKER_PATH == expected
    assert expected.is_file()


@pytest.fixture
def persistent_transport(tmp_path, monkeypatch):
    """Real helper/HTTP framing; loopback replaces TLS, no provider or key."""
    import http.server
    import threading

    connections = set()
    slow_started = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            connections.add(self.client_address)
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if body == b"slow":
                slow_started.set()
                time.sleep(0.5)
            response = (json.dumps({"embeddings": [{"values": [0.01] * 3072}], "usageMetadata": {"promptTokenCount": 10}}).encode()
                        if body.startswith(b"{") else b"ok")
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            try:
                self.wfile.write(response)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    worker = tmp_path / "loopback_worker.py"
    worker.write_text(
        "import importlib.util, http.client\n"
        f"s=importlib.util.spec_from_file_location('worker', {str(models._HTTP_WORKER_PATH)!r})\n"
        "w=importlib.util.module_from_spec(s); s.loader.exec_module(w)\n"
        f"w._open_https_connection=lambda *a, **k: http.client.HTTPConnection('127.0.0.1', {server.server_port})\n"
        "raise SystemExit(w.main())\n", encoding="utf-8",
    )
    monkeypatch.setattr(models, "_HTTP_WORKER_PATH", worker)
    transport = models.HttpsTransport(persistent=True)
    try:
        yield transport, connections, slow_started
    finally:
        transport.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _persistent_post(transport, body=b"request", *, timeout=2, limit=1024):
    return transport.post("https://synthetic.invalid/embed", body=body,
                          headers={"X-Synthetic": "not-a-credential"},
                          timeout_seconds=timeout, max_response_bytes=limit)


def test_query_helper_reuses_process_and_connection_and_recovers(persistent_transport):
    transport, connections, _ = persistent_transport
    assert _persistent_post(transport) == (200, b"ok")
    process = transport._session._process
    assert _persistent_post(transport) == (200, b"ok")
    assert transport._session._process is process
    assert len(connections) == 1
    with pytest.raises(models.AuxiliaryModelError, match="response_limit"):
        _persistent_post(transport, limit=1)
    assert process.poll() is not None
    assert _persistent_post(transport) == (200, b"ok")
    process = transport._session._process
    with pytest.raises(models.AuxiliaryModelError, match="timeout"):
        _persistent_post(transport, b"slow", timeout=0.1)
    assert process.poll() is not None
    assert _persistent_post(transport) == (200, b"ok")
    process = transport._session._process
    transport.close()
    assert process.poll() is not None


def test_query_adapter_uses_persistent_helper_but_source_does_not(persistent_transport, tmp_path, monkeypatch):
    from test_runtime_auxiliary import _runtime_config, _source
    from scope_recall.runtime.auxiliary import build_auxiliary_runtime
    _, connections, _ = persistent_transport
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "synthetic-local-value")
    config, _, _ = _runtime_config(tmp_path)
    runtime = build_auxiliary_runtime(config)
    adapter = runtime.query_embedding
    # Never let a stale imported model class escape the loopback harness.
    assert adapter._query_transport._post.__globals__["_HTTP_WORKER_PATH"] == models._HTTP_WORKER_PATH
    assert adapter._transport._post.__globals__["_HTTP_WORKER_PATH"] == models._HTTP_WORKER_PATH
    try:
        first = adapter.embed_query("TEST first query", remaining_seconds=2)
        process = adapter._query_transport._session._process
        second = adapter.embed_query("TEST second query", remaining_seconds=2)
        assert len(first) == len(second) == 3072
        assert adapter._query_transport._session._process is process
        assert len(connections) == 1
        assert len(adapter.embed_source(_source("TEST source"), remaining_seconds=2)) == 3072
        assert adapter._transport._session is None and len(connections) == 2
    finally:
        runtime.close()
    assert process.poll() is not None


def test_query_helper_close_cancels_active_request(persistent_transport):
    from concurrent.futures import ThreadPoolExecutor
    transport, _, slow_started = persistent_transport
    assert _persistent_post(transport) == (200, b"ok")
    process = transport._session._process
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(_persistent_post, transport, b"slow")
        assert slow_started.wait(2)
        transport.close()
        with pytest.raises(models.AuxiliaryModelError):
            pending.result(timeout=2)
    assert process.poll() is not None
