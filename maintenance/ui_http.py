"""Authenticated loopback-only HTTP transport for the local owner page."""

from __future__ import annotations

import hmac
import json
import secrets
import socket
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore, Lock, Timer
from urllib.parse import parse_qs, urlsplit

from ..contracts import ContractError
from ..core.truth_connection import TruthDatabaseConnectionError
from ..core.writer_lease import TruthWriterBusyError
from ..runtime.backup_primitives import BackupError

BODY_LIMIT = 65536
REQUEST_SECONDS = 5
MAX_HANDLERS = 16
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


def form_parameters(raw):
    """Normalize form switches only; JSON continues to require boolean values."""
    data = parameters(raw)
    for key in ("confirmed", "retract", "with_attachments"):
        if key in data:
            if data[key] not in {"true", "false", "1", "0", "on", "off"}:
                raise RequestError(400, "boolean_invalid")
            data[key] = data[key] in {"true", "1", "on"}
    return data


class UIServer(ThreadingHTTPServer):
    """Own one random loopback socket, launch token and owner API for this process."""

    # Closing the listening socket joins active owner operations, including export cleanup.
    daemon_threads = False
    block_on_close = True

    def __init__(self, api):
        self.api = api
        self.token = secrets.token_urlsafe(32)
        self.session_token = secrets.token_urlsafe(32)
        self.launch_used = False
        self.launch_lock = Lock()
        self.slots = BoundedSemaphore(MAX_HANDLERS)
        super().__init__(("127.0.0.1", 0), UIHandler)
        self.hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}

    @property
    def url(self):
        """The single-use launch link exchanges its token for a process-local session."""
        return f"http://127.0.0.1:{self.server_port}/?token={self.token}"

    def process_request(self, request, client_address):
        """Refuse excess connections without starting more handler threads."""
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        """Release capacity only after the operation and socket cleanup finish."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        """Keep unexpected transport failures from logging credentials or stored content."""


class UIHandler(BaseHTTPRequestHandler):
    """Check every request before dispatch and emit no capability or memory logs."""

    server: UIServer

    def setup(self):
        """Bound the entire header/body read, even when bytes arrive continuously."""
        super().setup()
        self.read_deadline = time.monotonic() + REQUEST_SECONDS
        self.read_timer = Timer(REQUEST_SECONDS, self._expire_read)
        self.read_timer.daemon = True
        self.read_timer.start()

    def _expire_read(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        """Cancel the read timer on parser refusals and disconnected clients too."""
        self.read_timer.cancel()
        super().finish()

    def send_error(self, code, message=None, explain=None):
        """Parser failures use fixed JSON and the same headers as API responses."""
        self.request_version = "HTTP/1.0"
        self._json(code, dict(status="error", code="HTTP_REQUEST_INVALID"))

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

    def _body(self, *, authenticated):
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isascii():
            raise RequestError(400, "body_length_required")
        if not lengths[0].isdecimal() or len(lengths[0]) > 8:
            raise RequestError(400, "body_length_invalid")
        size = int(lengths[0])
        if size > BODY_LIMIT:
            raise RequestError(413, "body_too_large")
        prefix = b""
        content_type = self.headers.get_content_type()
        if not authenticated:
            # A form-only credential must be its first field. Authenticate that bounded
            # prefix before reading the remaining submission; JSON requires a header.
            if content_type != "application/x-www-form-urlencoded":
                raise RequestError(403, "token_refused")
            while len(prefix) < min(size, 256):
                part = self.rfile.read(1)
                if not part:
                    break
                prefix += part
                if part == b"&":
                    break
            self._authenticate(parameters(prefix.rstrip(b"&").decode("utf-8")).get("token"))
        raw = prefix + self.rfile.read(size - len(prefix))
        if len(raw) != size:
            raise RequestError(400, "body_incomplete")
        content_type = self.headers.get_content_type()
        if content_type == "application/x-www-form-urlencoded":
            return form_parameters(raw.decode("utf-8"))
        if content_type != "application/json":
            raise RequestError(415, "content_type_refused")
        data = json.loads(raw)
        if type(data) is not dict:
            raise RequestError(400, "object_required")
        return data

    def _authenticate(self, supplied):
        if type(supplied) is not str or not hmac.compare_digest(
            supplied.encode("utf-8"), self.server.session_token.encode()
        ):
            raise RequestError(403, "token_refused")

    def _launch(self, supplied):
        with self.server.launch_lock:
            if type(supplied) is not str or not hmac.compare_digest(
                supplied.encode("utf-8"), self.server.token.encode()
            ):
                raise RequestError(403, "token_refused")
            if self.server.launch_used:
                raise RequestError(403, "launch_used")
            self.server.launch_used = True

    def _get(self, path, query):
        if path in STATIC:
            filename, content_type = STATIC[path]
            body = (Path(__file__).with_name("ui_static") / filename).read_bytes()
            if path == "/":
                body = body.replace(b"__SESSION__", self.server.session_token.encode("ascii"))
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
            headers = self.headers.get_all("X-Scope-Recall-Token", [])
            if len(headers) > 1:
                raise RequestError(403, "token_refused")
            public_asset = self.command == "GET" and url.path in STATIC and url.path != "/"
            if self.command == "GET" and url.path == "/" and "token" in query:
                self._launch(query["token"])
            elif headers:
                self._authenticate(headers[0])
            elif self.command != "POST" and not public_asset:
                raise RequestError(403, "token_refused")
            body = self._body(authenticated=bool(headers)) if self.command == "POST" else {}
            if time.monotonic() >= self.read_deadline:
                raise RequestError(408, "request_timeout")
            # An authenticated operation may take longer than a network read. Shutdown
            # drains it instead of interrupting a store transaction or export publication.
            self.read_timer.cancel()
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
        except (OSError, BackupError, sqlite3.Error, TruthWriterBusyError, TruthDatabaseConnectionError):
            self._json(503, dict(status="error", code="STORE_UNAVAILABLE"))
        except Exception:  # noqa: BLE001 - The HTTP boundary never discloses unexpected exception content.
            self._json(500, dict(status="error", code="INTERNAL_ERROR"))

    def do_GET(self):
        """Serve the public static assets, single-use launch page and authenticated reads."""
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
