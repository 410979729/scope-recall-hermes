"""Offline HTTP worker proxy and transport guards; no external network."""
from __future__ import annotations

import base64
import http.client
import json
import time
from io import BytesIO
from types import SimpleNamespace

import pytest

from scope_recall.runtime import _http_worker as worker


class _FakeSocket:
    def settimeout(self, _value):
        return None


class _FakeHTTPResponse:
    def __init__(self, *, status: int, body: bytes) -> None:
        self.status = status
        self.will_close = True  # This one-shot response double never permits reuse.
        self._body = body

    def read(self, _amt: int | None = None) -> bytes:
        if not self._body:
            return b""
        chunk = self._body
        self._body = b""
        return chunk


class _RecordingHTTPSConnection:
    created: list[tuple[object, ...]] = []
    tunnels: list[tuple[object, ...]] = []
    requests: list[tuple[object, ...]] = []
    connect_calls = 0
    response_status = 200
    response_body = b"ok"
    connect_error: Exception | None = None

    def __init__(self, host, port=443, context=None, timeout=object()) -> None:
        self.created.append((host, port, context))
        self.host = host
        self.port = port
        self.context = context
        self.timeout = timeout
        self.sock: _FakeSocket | None = None
        self._tunnel_host: str | None = None
        self._tunnel_port: int | None = None
        self._tunnel_headers: dict[str, str] | None = None

    def set_tunnel(self, host, port=None, headers=None) -> None:
        self.tunnels.append((host, port, dict(headers or {})))
        self._tunnel_host = host
        self._tunnel_port = port
        self._tunnel_headers = dict(headers or {})

    def connect(self) -> None:
        type(self).connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error
        self.sock = _FakeSocket()

    def request(self, method, path, body=None, headers=None, *, encode_chunked=False) -> None:
        self.requests.append((method, path, body, dict(headers or {})))

    def getresponse(self) -> _FakeHTTPResponse:
        return _FakeHTTPResponse(status=self.response_status, body=self.response_body)

    def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_recording_connection() -> None:
    _RecordingHTTPSConnection.created = []
    _RecordingHTTPSConnection.tunnels = []
    _RecordingHTTPSConnection.requests = []
    _RecordingHTTPSConnection.connect_calls = 0
    _RecordingHTTPSConnection.response_status = 200
    _RecordingHTTPSConnection.response_body = b"ok"
    _RecordingHTTPSConnection.connect_error = None


def _payload(
    *,
    url: str = "https://target.example/api",
    body: bytes = b"body",
    headers: dict[str, str] | None = None,
    timeout_seconds: float = 2.0,
    max_response_bytes: int = 1024,
) -> bytes:
    return json.dumps(
        {
            "url": url,
            "body_b64": base64.b64encode(body).decode("ascii"),
            "headers": headers or {"X-Test": "1"},
            "timeout_seconds": timeout_seconds,
            "max_response_bytes": max_response_bytes,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _decode_result(raw: bytes) -> dict[str, object]:
    return json.loads(raw.decode("utf-8"))


def _install_fake_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http.client, "HTTPSConnection", _RecordingHTTPSConnection)


def test_proxy_connect_is_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_connection(monkeypatch)
    monkeypatch.setattr(
        worker.urllib.request,
        "getproxies",
        lambda: {"https": "http://proxy.local:8080"},
    )
    monkeypatch.setattr(worker.urllib.request, "proxy_bypass", lambda _host: False)

    result = _decode_result(worker._request(_payload()))

    assert result["ok"] is True
    assert len(_RecordingHTTPSConnection.created) == 1
    assert _RecordingHTTPSConnection.created[0][:2] == ("proxy.local", 8080)
    assert _RecordingHTTPSConnection.tunnels == [("target.example", 443, {})]
    assert _RecordingHTTPSConnection.connect_calls == 1


def test_no_proxy_bypasses_configured_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_connection(monkeypatch)
    monkeypatch.setattr(
        worker.urllib.request,
        "getproxies",
        lambda: {"https": "http://proxy.local:8080"},
    )
    monkeypatch.setattr(worker.urllib.request, "proxy_bypass", lambda host: host == "target.example")

    result = _decode_result(worker._request(_payload()))

    assert result["ok"] is True
    assert len(_RecordingHTTPSConnection.created) == 1
    assert _RecordingHTTPSConnection.created[0][:2] == ("target.example", 443)
    assert _RecordingHTTPSConnection.tunnels == []


def test_unsupported_proxy_fails_without_direct_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_connection(monkeypatch)
    monkeypatch.setattr(
        worker.urllib.request,
        "getproxies",
        lambda: {"https": "socks5://proxy.local:1080"},
    )
    monkeypatch.setattr(worker.urllib.request, "proxy_bypass", lambda _host: False)

    result = _decode_result(worker._request(_payload()))

    assert result == {
        "ok": False,
        "status": None,
        "body_b64": "",
        "error": "http_protocol",
    }
    assert _RecordingHTTPSConnection.created == []
    assert _RecordingHTTPSConnection.connect_calls == 0


def test_proxy_authorization_is_connect_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_connection(monkeypatch)
    monkeypatch.setattr(
        worker.urllib.request,
        "getproxies",
        lambda: {"https": "http://proxy-user:proxy-pass@proxy.local:8080"},
    )
    monkeypatch.setattr(worker.urllib.request, "proxy_bypass", lambda _host: False)

    result = _decode_result(worker._request(_payload()))

    assert result["ok"] is True
    tunnel_headers = _RecordingHTTPSConnection.tunnels[0][2]
    assert "Proxy-Authorization" in tunnel_headers
    assert tunnel_headers["Proxy-Authorization"].startswith("Basic ")
    request_headers = _RecordingHTTPSConnection.requests[0][3]
    assert "Proxy-Authorization" not in request_headers
    assert "proxy-authorization" not in {key.lower() for key in request_headers}


def test_caller_proxy_authorization_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_connection(monkeypatch)
    monkeypatch.setattr(worker.urllib.request, "getproxies", dict)
    monkeypatch.setattr(worker.urllib.request, "proxy_bypass", lambda _host: True)

    result = _decode_result(
        worker._request(
            _payload(headers={"Proxy-Authorization": "Basic caller-secret"}),
        ),
    )

    assert result["error"] == "http_protocol"
    assert _RecordingHTTPSConnection.created == []


def test_https_only_guard_rejects_non_https() -> None:
    result = _decode_result(
        worker._request(
            _payload(url="http://target.example/api"),
        ),
    )
    assert result["error"] == "endpoint_invalid"


def test_redirect_guard_rejects_3xx(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_connection(monkeypatch)
    monkeypatch.setattr(worker.urllib.request, "getproxies", dict)
    monkeypatch.setattr(worker.urllib.request, "proxy_bypass", lambda _host: True)
    _RecordingHTTPSConnection.response_status = 302

    result = _decode_result(worker._request(_payload()))

    assert result["ok"] is False
    assert result["error"] == "http_redirect"
    assert result["status"] == 302


def test_response_limit_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_connection(monkeypatch)
    monkeypatch.setattr(worker.urllib.request, "getproxies", dict)
    monkeypatch.setattr(worker.urllib.request, "proxy_bypass", lambda _host: True)
    _RecordingHTTPSConnection.response_body = b"12345"

    result = _decode_result(worker._request(_payload(max_response_bytes=3)))

    assert result["ok"] is False
    assert result["error"] == "response_limit"
    # The worker refuses to expose an over-limit response body; the transport
    # raises response_limit from the bounded error envelope.
    assert base64.b64decode(result["body_b64"]) == b""


def test_deadline_guard_before_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_connection(monkeypatch)
    monkeypatch.setattr(worker.urllib.request, "getproxies", dict)
    monkeypatch.setattr(worker.urllib.request, "proxy_bypass", lambda _host: True)
    times = iter([0.0, 0.0, 0.6])

    monkeypatch.setattr(worker.time, "monotonic", lambda: next(times))

    result = _decode_result(worker._request(_payload(timeout_seconds=0.5)))

    assert result["error"] == "timeout"
    assert _RecordingHTTPSConnection.connect_calls == 0


def test_chunked_response_framing_via_http_response_parser() -> None:
    wire = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: keep-alive\r\n\r\n"
        b"5\r\nhello\r\n6\r\n world\r\n0\r\nX-Trailer: yes\r\n\r\n"
    )
    response = http.client.HTTPResponse(SimpleNamespace(makefile=lambda *_args: BytesIO(wire)))
    response.begin()
    assert response.read() == b"hello world"
