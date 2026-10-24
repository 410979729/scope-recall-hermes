"""Real loopback requests must pass transport checks before reaching any store call."""

import http.client
import json
import socket
import sqlite3
import time
from threading import Event, Thread
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from scope_recall.core.truth_connection import TruthDatabaseConnectionError
from scope_recall.core.writer_lease import TruthWriterBusyError
from scope_recall.maintenance.ui_http import BODY_LIMIT, UIServer


@pytest.fixture
def http_server():
    calls = []

    def record(*args):
        calls.append(args)
        return {"called": True}

    with UIServer(SimpleNamespace(read=record, preview=record, confirm=record)) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, calls
        finally:
            server.shutdown()
            thread.join(6)
            assert not thread.is_alive()


def send(server, route="/api/list", *, method="GET", data=None, token=True, headers=None, raw=None):
    """Send one request without browser-added authentication or origin defaults."""
    body = raw if raw is not None else json.dumps(data or {}).encode() if method == "POST" else None
    fields = {"Content-Type": "application/json"}
    if token:
        fields["X-Scope-Recall-Token"] = server.session_token if token is True else token
    fields.update(headers or {})
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=6)
    try:
        connection.request(method, route, body=body, headers=fields)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("token", [False, "wrong", "名字一"])
def test_token_refusal_never_calls_store(http_server, method, token):
    server, calls = http_server
    # Header bytes are Latin-1; non-ASCII tokens are exercised through the URL/body.
    arguments = {"token": token}
    if token == "名字一":
        arguments = {"token": False, "data": {"token": token}}
    status, _, _ = send(server, method=method, **arguments)
    assert status == 403
    assert calls == []


@pytest.mark.parametrize("host", ["foreign.invalid", "127.0.0.1", "localhost:1", "127.0.0.1:1", "[::1]:1"])
def test_wrong_host_never_calls_store(http_server, host):
    server, calls = http_server
    assert send(server, headers={"Host": host})[0] == 403
    assert calls == []


def test_server_is_random_ipv4_loopback_and_accepts_both_local_hosts(http_server):
    server, calls = http_server
    assert server.server_address[0] == "127.0.0.1"
    assert server.server_port > 0
    for host in ("127.0.0.1", "localhost"):
        assert send(server, headers={"Host": f"{host}:{server.server_port}"})[0] == 200
    assert len(calls) == 2
    with UIServer(None) as other:
        assert server.token != other.token


@pytest.mark.parametrize("origin", ["http://foreign.invalid", "null", "https://localhost", "http://127.0.0.1:1"])
def test_foreign_origin_never_calls_store(http_server, origin):
    server, calls = http_server
    assert send(server, "/api/confirm", method="POST", headers={"Origin": origin})[0] == 403
    assert calls == []


def test_post_requires_header_or_form_token_and_ignores_cookie_and_url(http_server):
    server, calls = http_server
    url = "/api/confirm?" + urlencode({"token": server.token})
    assert send(server, url, method="POST", token=False, headers={"Cookie": "token=" + server.token})[0] == 403
    assert calls == []
    form = urlencode({"token": server.session_token}).encode()
    assert (
        send(
            server,
            "/api/preview",
            method="POST",
            token=False,
            raw=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )[0]
        == 200
    )
    assert (
        send(server, "/api/confirm", method="POST", headers={"Origin": f"http://127.0.0.1:{server.server_port}"})[0]
        == 200
    )


@pytest.mark.parametrize("route", ["preview", "confirm"])
def test_get_cannot_reach_mutating_routes(http_server, route):
    server, calls = http_server
    assert send(server, "/api/" + route)[0] == 405
    assert calls == []


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "BREW"])
def test_other_methods_are_authenticated_and_refused(http_server, method):
    server, calls = http_server
    assert send(server, method=method, token=False)[0] == 403
    assert send(server, method=method)[0] == 405
    assert calls == []


@pytest.mark.parametrize(
    "raw,content_type,status",
    [
        (b"x" * (BODY_LIMIT + 1), "application/json", 413),
        (b"{", "application/json", 400),
        (b"[]", "application/json", 400),
        (b"{}", "text/plain", 415),
        (b"token=a&token=b", "application/x-www-form-urlencoded", 400),
    ],
    ids=["oversized", "malformed", "array", "wrong-type", "duplicate-field"],
)
def test_body_limits_and_parsing_precede_dispatch(http_server, raw, content_type, status):
    server, calls = http_server
    assert send(server, "/api/preview", method="POST", raw=raw, headers={"Content-Type": content_type})[0] == status
    assert calls == []


def test_security_headers_and_authenticated_static_assets(http_server, capsys):
    server, calls = http_server
    assert send(server, "/", token=False)[0] == 403
    for route in ("/", "/app.js", "/strings.js", "/style.css"):
        status, headers, body = send(server, route)
        assert status == 200 and body
        assert "'unsafe-inline'" not in headers["Content-Security-Policy"]
        assert "default-src 'none'" in headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Cache-Control"] == "no-store"
        if route == "/":
            assert b"__SESSION__" not in body
            assert server.session_token.encode() in body
        else:
            assert send(server, route, token=False)[0] == 200
    assert calls == []
    assert server.token not in capsys.readouterr().err


def test_duplicate_host_token_and_framing_are_refused(http_server):
    server, calls = http_server
    for duplicate in ("Host", "X-Scope-Recall-Token", "Content-Length"):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=6)
        connection.putrequest("POST", "/api/preview", skip_host=True)
        fields = {
            "Host": f"127.0.0.1:{server.server_port}",
            "X-Scope-Recall-Token": server.session_token,
            "Content-Length": "2",
            "Content-Type": "application/json",
        }
        for key, value in fields.items():
            connection.putheader(key, value)
        connection.putheader(duplicate, fields[duplicate])
        connection.endheaders()
        assert connection.getresponse().status in {400, 403}
        connection.close()
    assert calls == []


def test_authentication_uses_constant_time_comparison(http_server, monkeypatch):
    from scope_recall.maintenance import ui_http

    server, calls = http_server
    comparisons = []
    original = ui_http.hmac.compare_digest

    def compare(left, right):
        comparisons.append((left, right))
        return original(left, right)

    monkeypatch.setattr(ui_http.hmac, "compare_digest", compare)
    assert send(server)[0] == 200
    assert comparisons == [(server.session_token.encode(), server.session_token.encode())]


@pytest.mark.parametrize(
    "error", [sqlite3.OperationalError, TruthWriterBusyError, TruthDatabaseConnectionError, RuntimeError]
)
def test_handler_failures_return_sanitized_json(http_server, capsys, error):
    server, _calls = http_server

    def fail(*args):
        raise error() if error is TruthWriterBusyError else error("synthetic stored content marker")

    server.api.read = fail
    status, _headers, body = send(server)
    assert status == (500 if error is RuntimeError else 503)
    assert json.loads(body)["code"] == ("INTERNAL_ERROR" if error is RuntimeError else "STORE_UNAVAILABLE")
    assert b"synthetic stored content marker" not in body
    assert "synthetic stored content marker" not in capsys.readouterr().err


def test_shutdown_drains_active_confirmation(http_server):
    server, _calls = http_server
    entered, release, returned = Event(), Event(), Event()

    def confirm(*args):
        entered.set()
        assert release.wait(5)
        return {"finished": True}

    server.api.confirm = confirm
    client = Thread(target=lambda: send(server, "/api/confirm", method="POST"))
    client.start()
    assert entered.wait(2)

    def close():
        server.shutdown()
        server.server_close()
        returned.set()

    closer = Thread(target=close)
    closer.start()
    try:
        assert not returned.wait(0.7), "shutdown returned before active confirmation finished"
    finally:
        release.set()
        client.join(3)
        closer.join(3)
    assert returned.is_set()


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_request_deadline_bounds_continuous_trickle(http_server, monkeypatch, phase):
    from scope_recall.maintenance import ui_http

    server, calls = http_server
    monkeypatch.setattr(ui_http, "REQUEST_SECONDS", 0.25, raising=False)
    with socket.create_connection(server.server_address, timeout=2) as client:
        prefix = f"POST /api/preview HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n"
        if phase == "body":
            prefix += (
                f"X-Scope-Recall-Token: {server.session_token}\r\n"
                "Content-Type: application/json\r\nContent-Length: 100\r\n\r\n"
            )
        client.sendall(prefix.encode())
        for _ in range(6):
            time.sleep(0.08)
            try:
                client.sendall(b"x")
            except OSError:
                break
        client.settimeout(0.3)
        try:
            response = client.recv(4096)
        except ConnectionError:
            response = b""
        assert not response or b"408" in response
    assert calls == []


def test_unauthorized_post_is_refused_before_body_read(http_server):
    server, calls = http_server
    with socket.create_connection(server.server_address, timeout=1) as client:
        client.sendall(
            (
                f"POST /api/confirm HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n"
                "Content-Length: 100\r\nContent-Type: application/json\r\n\r\n"
            ).encode()
        )
        assert b"403" in client.recv(4096)
    assert calls == []


def test_handler_concurrency_is_bounded(http_server, monkeypatch):
    from scope_recall.maintenance import ui_http

    monkeypatch.setattr(ui_http, "MAX_HANDLERS", 2, raising=False)
    entered, release = Event(), Event()
    calls = []

    def read(*args):
        calls.append(args)
        if len(calls) == 2:
            entered.set()
        release.wait(3)
        return {}

    with UIServer(SimpleNamespace(read=read)) as server:
        listener = Thread(target=server.serve_forever)
        listener.start()
        clients = [Thread(target=lambda: send(server)) for _ in range(2)]
        try:
            for client in clients:
                client.start()
            assert entered.wait(2)
            try:
                send(server)
            except (OSError, http.client.HTTPException):
                pass
            assert len(calls) == 2, "excess request started another handler"
        finally:
            release.set()
            for client in clients:
                client.join(3)
            server.shutdown()
            listener.join(3)


def test_launch_token_is_single_use_and_static_urls_have_no_credentials(http_server):
    server, calls = http_server
    status, _, body = send(server, "/?token=" + server.token, token=False)
    assert status == 200
    assert server.token.encode() not in body
    assert b"?token=" not in body
    assert send(server, "/?token=" + server.token, token=False)[0] == 403
    assert send(server, "/api/list?token=" + server.token, token=False)[0] == 403
    assert calls == []


@pytest.mark.parametrize(
    "line",
    [
        b"GET /?token=synthetic-launch-marker extra HTTP/1.1",
        b"GET /?token=synthetic-launch-marker HTTP/999.1",
        b"POST /?token=synthetic-launch-marker",
    ],
)
def test_parser_errors_are_fixed_and_hardened(http_server, line):
    server, calls = http_server
    with socket.create_connection(server.server_address, timeout=2) as client:
        client.sendall(line + b"\r\n\r\n")
        response = b"".join(iter(lambda: client.recv(4096), b""))
    assert b"synthetic-launch-marker" not in response
    assert b"Content-Security-Policy:" in response
    assert b"Referrer-Policy: no-referrer" in response
    assert b"Cache-Control: no-store" in response
    assert calls == []
