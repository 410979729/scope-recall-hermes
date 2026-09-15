"""Offline transport boundary checks; no sockets leave this test process."""
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
