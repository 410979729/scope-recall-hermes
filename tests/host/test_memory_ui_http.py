"""Real loopback requests must pass transport checks before reaching any store call."""

import http.client
import json
from threading import Thread
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
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
        fields["X-Scope-Recall-Token"] = server.token if token is True else token
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
    form = urlencode({"token": server.token}).encode()
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
    for route in ("/", "/app.js", "/strings.js", "/style.css"):
        assert send(server, route, token=False)[0] == 403
        status, headers, body = send(server, route + "?token=" + server.token, token=False)
        assert status == 200 and body
        assert "'unsafe-inline'" not in headers["Content-Security-Policy"]
        assert "default-src 'none'" in headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Cache-Control"] == "no-store"
        if route == "/":
            assert b"__TOKEN__" not in body
            assert server.token.encode() in body
    assert calls == []
    assert server.token not in capsys.readouterr().err


def test_duplicate_host_token_and_framing_are_refused(http_server):
    server, calls = http_server
    for duplicate in ("Host", "X-Scope-Recall-Token", "Content-Length"):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=6)
        connection.putrequest("POST", "/api/preview", skip_host=True)
        fields = {
            "Host": f"127.0.0.1:{server.server_port}",
            "X-Scope-Recall-Token": server.token,
            "Content-Length": "2",
            "Content-Type": "application/json",
        }
        for key, value in fields.items():
            connection.putheader(key, value)
        connection.putheader(duplicate, fields[duplicate])
        connection.endheaders(b"{}")
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
    assert comparisons == [(server.token.encode(), server.token.encode())]
