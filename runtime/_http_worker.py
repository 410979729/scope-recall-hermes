"""Private stdlib HTTP(S) worker for the bounded auxiliary transport."""

from __future__ import annotations

import base64
import http.client
import json
import math
import runpy
import select
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# -I executes this file without importing the package. Load only the pure
# policy module at its installed sibling path; never search cwd or PYTHONPATH.
_POLICY = runpy.run_path(str(Path(__file__).resolve().parents[1] / "core" / "endpoint_policy.py"))

MAX_REQUEST_BYTES = 3 * 1024 * 1024
_REQUEST_KEYS = frozenset({"url", "body_b64", "headers", "timeout_seconds", "max_response_bytes"})
#: The one key a caller may add: permission to send plaintext HTTP to a host that is not this machine (the route's
#: ``allow_insecure_endpoint``); loopback HTTP needs none.  A request without it is held to HTTPS and loopback.
_OPTIONAL_REQUEST_KEYS = frozenset({"allow_insecure"})


class _Failure(Exception):
    """One bounded error reply; the closed vocabulary is what the parent accepts."""

    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


def _result(*, ok: bool, status: int | None = None, body: bytes = b"", error: str | None = None) -> bytes:
    payload: dict[str, object] = {
        "ok": ok,
        "status": status,
        "body_b64": base64.b64encode(body).decode("ascii"),
    }
    if error is not None:
        payload["error"] = error
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _proxy_authorization_from_url(proxy_url: str) -> dict[str, str]:
    parsed = urllib.parse.urlparse(proxy_url)
    if parsed.username is None and parsed.password is None:
        return {}
    user = urllib.parse.unquote(parsed.username or "")
    password = urllib.parse.unquote(parsed.password or "")
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return {"Proxy-Authorization": f"Basic {token}"}


def _resolve_http_proxy(target_hostname: str) -> tuple[str, int, dict[str, str]] | None:
    if urllib.request.proxy_bypass(target_hostname):
        return None
    proxies = urllib.request.getproxies()
    proxy_url = proxies.get("https") or proxies.get("http")
    if not proxy_url:
        return None
    parsed = urllib.parse.urlparse(proxy_url)
    scheme = (parsed.scheme or "").lower()
    if scheme != "http" or not parsed.hostname:
        raise ValueError("proxy")
    port = parsed.port or 80
    return parsed.hostname, port, _proxy_authorization_from_url(proxy_url)


def _plaintext_headers(headers: dict[str, str]) -> dict[str, str]:
    """Strip credential header names with the same policy as URL queries."""
    return {name: value for name, value in headers.items() if not _POLICY["is_credential_key"](name)}


def _mark_idle(connection: http.client.HTTPConnection) -> None:
    """Stamp the connection this worker keeps between requests (see ``_still_open``).

    The stamp is a dynamic attribute, so it is set through ``vars`` rather than
    a declared one: ``HTTPConnection`` has no such field.
    """
    vars(connection)["scope_recall_idle_since"] = time.monotonic()


def _headers_and_body(request: dict, *, plaintext: bool) -> tuple[dict[str, str], str]:
    """Read the request's headers and body, stripping credentials when plaintext."""
    headers, body_b64 = request["headers"], request["body_b64"]
    if not isinstance(headers, dict) or type(body_b64) is not str:
        raise _Failure("http_protocol")
    return (_plaintext_headers(headers) if plaintext else headers), body_b64


def _open_https_connection(target_hostname: str, target_port: int, *, deadline: float) -> http.client.HTTPSConnection:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    ssl_context = ssl.create_default_context()
    proxy = _resolve_http_proxy(target_hostname)
    if proxy is None:
        connection = http.client.HTTPSConnection(target_hostname, target_port, context=ssl_context)
        connection.timeout = remaining
        return connection
    proxy_host, proxy_port, tunnel_headers = proxy
    connection = http.client.HTTPSConnection(proxy_host, proxy_port, context=ssl_context)
    connection.timeout = remaining
    connection.set_tunnel(target_hostname, target_port, headers=tunnel_headers or None)
    return connection


def _parse_request(
    raw: bytes,
) -> tuple[urllib.parse.ParseResult, bytes, dict[str, str], float, int, bool]:
    """Validate the parent's request line; each failure names one field's fault.

    The last value is whether the request goes out in plaintext: HTTP to this machine, or to another host when the
    request carries ``allow_insecure``.
    """
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise _Failure("http_protocol") from None
    if not isinstance(request, dict) or not (_REQUEST_KEYS <= set(request) <= _REQUEST_KEYS | _OPTIONAL_REQUEST_KEYS):
        raise _Failure("http_protocol")
    allow_insecure = request.get("allow_insecure", False)
    if type(allow_insecure) is not bool:
        raise _Failure("http_protocol")
    url = request["url"]
    if not _POLICY["endpoint_scheme_allowed"](url, allow_insecure=allow_insecure):
        raise _Failure("endpoint_invalid")
    parsed = urllib.parse.urlparse(url)
    plaintext = parsed.scheme == "http"
    headers, body_b64 = _headers_and_body(request, plaintext=plaintext)
    timeout_seconds, max_response_bytes = request["timeout_seconds"], request["max_response_bytes"]
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(float(timeout_seconds))
        or float(timeout_seconds) <= 0
    ):
        raise _Failure("timeout")
    if type(max_response_bytes) is not int or max_response_bytes <= 0:
        raise _Failure("response_limit")
    # Proxy credentials come only from the proxy URL, never from the caller.
    if any(
        type(key) is not str or type(value) is not str or key.lower() == "proxy-authorization"
        for key, value in headers.items()
    ):
        raise _Failure("http_protocol")
    try:
        body = base64.b64decode(body_b64.encode("ascii"), validate=True)
    except (UnicodeError, ValueError):
        raise _Failure("http_protocol") from None
    return parsed, body, headers, float(timeout_seconds), max_response_bytes, plaintext


#: A connection idle longer than this is not used again.  A server or a proxy closes an idle keep-alive connection
#: after a time of its own, and a request sent on it fails at once (``network_error``, ``http_protocol``) where a new
#: connection would be answered: a worker kept between a server's prompts sits idle between every two.  A request is
#: still never sent twice.
IDLE_REUSE_SECONDS = 30.0


def _still_open(connection: http.client.HTTPConnection) -> bool:
    """Whether a cached connection may carry the next request: idle for less than ``IDLE_REUSE_SECONDS``, and with
    nothing to read on it, which on an idle connection is its server's end of stream."""
    sock = connection.sock
    idle_since = getattr(connection, "scope_recall_idle_since", None)
    if sock is None or idle_since is None or time.monotonic() - idle_since > IDLE_REUSE_SECONDS:
        return False
    try:
        readable, _writable, _failed = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return False
    return not readable


def _take_connection(
    parsed, headers: dict[str, str], deadline: float, connections: dict | None, *, plaintext: bool = False
):
    """The cached connection for this origin and header set while it is still open, else a fresh one.

    A persistent worker keeps at most one connection; anything cached for a
    different key is closed rather than left half-open.
    """
    default_port = 80 if plaintext else 443
    cache_key = (parsed.scheme, parsed.hostname, parsed.port or default_port, tuple(sorted(headers.items())))
    connection = None
    try:
        if connections is not None:
            connection = connections.pop(cache_key, None)
            for old in connections.values():
                old.close()
            connections.clear()
            if connection is not None and not _still_open(connection):
                connection.close()
                connection = None
        if connection is None:
            if plaintext:
                connection = _open_plaintext_connection(parsed.hostname, parsed.port or 80, deadline=deadline)
            else:
                connection = _open_https_connection(parsed.hostname, parsed.port or 443, deadline=deadline)
    except ValueError:
        raise _Failure("http_protocol") from None
    return cache_key, connection


def _open_plaintext_connection(
    target_hostname: str, target_port: int, *, deadline: float
) -> http.client.HTTPConnection:
    """A plain connection to this machine.  No TLS, and no proxy: a loopback
    model server is reached directly, and a proxy would take the request away
    from this machine."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    connection = http.client.HTTPConnection(target_hostname, target_port)
    connection.timeout = remaining
    return connection


def _arm(connection: http.client.HTTPConnection, deadline: float) -> float:
    """Fail now if the deadline passed; otherwise bound the next socket operation by it."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _Failure("timeout")
    if connection.sock is not None:
        connection.sock.settimeout(remaining)
    return remaining


def _request(raw: bytes, connections: dict | None = None) -> bytes:
    try:
        parsed, body, headers, timeout_seconds, max_response_bytes, plaintext = _parse_request(raw)
    except _Failure as failure:
        return _result(ok=False, error=failure.error)
    deadline = time.monotonic() + timeout_seconds
    connection: http.client.HTTPConnection | None = None
    chunks: list[bytes] = []
    status: int | None = None
    reusable = False
    try:
        cache_key, connection = _take_connection(parsed, headers, deadline, connections, plaintext=plaintext)
        remaining = _arm(connection, deadline)
        if connection.sock is None:
            connection.timeout = remaining
            connection.connect()
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        _arm(connection, deadline)
        connection.request("POST", path, body=body, headers=headers)
        _arm(connection, deadline)
        response = connection.getresponse()
        status = response.status
        if 300 <= status < 400:
            raise _Failure("http_redirect")
        total = 0
        while True:
            _arm(connection, deadline)
            # HTTPResponse.read() owns Content-Length/chunked framing. Reading
            # response.fp directly would expose wire framing and keepalive.
            piece = response.read(min(65_536, max_response_bytes - total + 1))
            if not piece:
                break
            total += len(piece)
            if total > max_response_bytes:
                raise _Failure("response_limit")
            chunks.append(piece)
        reusable = status < 400 and not response.will_close and connection.sock is not None
        return _result(ok=True, status=status, body=b"".join(chunks))
    except _Failure as failure:
        return _result(ok=False, status=status, body=b"".join(chunks), error=failure.error)
    except socket.timeout:
        return _result(ok=False, status=status, body=b"".join(chunks), error="timeout")
    except http.client.HTTPException:
        return _result(ok=False, status=status, body=b"".join(chunks), error="http_protocol")
    except OSError:
        return _result(ok=False, status=status, body=b"".join(chunks), error="network_error")
    finally:
        if connection is not None:
            if reusable and connections is not None:
                _mark_idle(connection)
                connections[cache_key] = connection
            else:
                connection.close()


def main() -> int:
    if sys.argv[1:] == ["--persistent"]:
        connections = {}
        try:
            while True:
                raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 2)
                if not raw:
                    break
                if len(raw) > MAX_REQUEST_BYTES + 1 or not raw.endswith(b"\n"):
                    sys.stdout.buffer.write(_result(ok=False, error="request_limit") + b"\n")
                    sys.stdout.buffer.flush()
                    break
                sys.stdout.buffer.write(_request(raw, connections) + b"\n")
                sys.stdout.buffer.flush()
        finally:
            for connection in connections.values():
                connection.close()
        return 0
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        sys.stdout.buffer.write(_result(ok=False, error="request_limit"))
        sys.stdout.buffer.flush()
        return 0
    sys.stdout.buffer.write(_request(raw))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
