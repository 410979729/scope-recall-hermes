"""Authenticated loopback-only HTTP transport for the local owner page."""

from __future__ import annotations

import hmac
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from ..contracts import ContractError
from ..runtime.backup_primitives import BackupError

BODY_LIMIT = 65536
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/strings.js": ("strings.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'"
)


class RequestError(Exception):
    """An HTTP refusal that must not reach the store."""

    def __init__(self, status, code):
        self.status = status
        self.code = code


def parameters(query):
    """Reject duplicate fields so authentication and dispatch see the same input."""
    parsed = parse_qs(query, keep_blank_values=True, max_num_fields=40, strict_parsing=True)
    if any(len(values) != 1 for values in parsed.values()):
        raise RequestError(400, "duplicate_field")
    return {key: values[0] for key, values in parsed.items()}


class UIServer(ThreadingHTTPServer):
    """Own one random loopback socket, launch token and owner API for this process."""

    daemon_threads = True

    def __init__(self, api):
        self.api = api
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", 0), UIHandler)
        self.hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}

    @property
    def url(self):
        """The capability link, valid only while this server process remains alive."""
        return f"http://127.0.0.1:{self.server_port}/?token={self.token}"

    def get_request(self):
        """Bound slow request reads so an incomplete body cannot hold a thread forever."""
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address


class UIHandler(BaseHTTPRequestHandler):
    """Check every request before dispatch and emit no capability or memory logs."""

    server: UIServer

    def log_message(self, format, *args):
        """Suppress access logs because launch URLs carry a process capability."""

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def _json(self, status, payload):
        self._send(status, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    def _host(self):
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] not in self.server.hosts:
            raise RequestError(403, "host_refused")
        origins = self.headers.get_all("Origin", [])
        if self.command == "POST" and origins and origins != ["http://" + hosts[0]]:
            raise RequestError(403, "origin_refused")

    def _body(self):
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isascii():
            raise RequestError(400, "body_length_required")
        if not lengths[0].isdecimal() or len(lengths[0]) > 8:
            raise RequestError(400, "body_length_invalid")
        size = int(lengths[0])
        if size > BODY_LIMIT:
            raise RequestError(413, "body_too_large")
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise RequestError(400, "body_incomplete")
        content_type = self.headers.get_content_type()
        if content_type == "application/x-www-form-urlencoded":
            return parameters(raw.decode("utf-8"))
        if content_type != "application/json":
            raise RequestError(415, "content_type_refused")
        data = json.loads(raw)
        if type(data) is not dict:
            raise RequestError(400, "object_required")
        return data

    def _authenticate(self, query, body):
        headers = self.headers.get_all("X-Scope-Recall-Token", [])
        if len(headers) > 1:
            raise RequestError(403, "token_refused")
        # POST deliberately ignores the query and cookies, including the launch URL.
        supplied = headers[0] if headers else body.get("token") if self.command == "POST" else query.get("token")
        if type(supplied) is not str or not hmac.compare_digest(supplied.encode("utf-8"), self.server.token.encode()):
            raise RequestError(403, "token_refused")

    def _get(self, path, query):
        if path in STATIC:
            filename, content_type = STATIC[path]
            body = (Path(__file__).with_name("ui_static") / filename).read_bytes()
            if path == "/":
                body = body.replace(b"__TOKEN__", quote(self.server.token).encode("ascii"))
            self._send(200, body, content_type)
            return
        if not path.startswith("/api/"):
            raise RequestError(404, "route_missing")
        if path in {"/api/preview", "/api/confirm"}:
            raise RequestError(405, "post_required")
        self._json(200, dict(status="ok", data=self.server.api.read(path[5:], query)))

    def _dispatch(self):
        try:
            self._host()
            url = urlsplit(self.path)
            if url.scheme or url.netloc:
                raise RequestError(400, "relative_path_required")
            query = parameters(url.query)
            body = self._body() if self.command == "POST" else {}
            self._authenticate(query, body)
            if self.command == "GET":
                self._get(url.path, query)
            elif self.command == "POST" and url.path in {"/api/preview", "/api/confirm"}:
                call = self.server.api.preview if url.path.endswith("preview") else self.server.api.confirm
                self._json(200, dict(status="ok", data=call(body)))
            else:
                raise RequestError(405, "method_refused")
        except RequestError as exc:
            self._json(exc.status, dict(status="error", code=exc.code))
        except ContractError as exc:
            self._json(400, dict(status="error", code=exc.code, field=exc.field))
        except (ValueError, TypeError, RecursionError):
            self._json(400, dict(status="error", code="INPUT_INVALID"))
        except (OSError, BackupError):
            self._json(503, dict(status="error", code="STORE_UNAVAILABLE"))

    def do_GET(self):
        """Serve only authenticated static assets and read APIs."""
        self._dispatch()

    def do_POST(self):
        """Serve bounded, origin-checked preview and confirmation requests."""
        self._dispatch()

    def do_HEAD(self):
        """Refuse unsupported methods through the same authentication boundary."""
        self._dispatch()

    def __getattr__(self, name):
        """Route unsupported HTTP verbs through authentication and the common refusal."""
        if name.startswith("do_"):
            return self.do_HEAD
        raise AttributeError(name)
