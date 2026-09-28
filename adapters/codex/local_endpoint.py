"""Hooks answered by the client's own MCP server, warm for as long as the client is open.

Claude Code and Codex start a new process for every hook, and a process that starts LanceDB for its recall was often not
ready before the recall's budget ran out: on the pilot 6 of 8 cold Claude Code hooks recalled by words alone
(``helper_request_deadline``).  The client's MCP server lives exactly as long as the client, so it also answers the
entry's hooks on this machine (``serve``), with a LanceDB helper kept ready for each (``vector.process_store.prestart``).

A hook asks the newest server that names itself in ``<home>/scope-recall/hook-endpoints/`` (``ask``).  One that finds
none, or whose server refuses the connection, does the work itself as before; one whose server took the request and
did not answer in time gives the turn no memory rather than run out the client's hook timeout doing it twice.  Only
this machine connects (127.0.0.1), and only with the token the server wrote beside its port.
"""
from __future__ import annotations

import atexit
import dataclasses
import hmac
import http.client
import json
import os
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

#: What a hook may send: the same bound as a hook's own stdin.
MAX_REQUEST_BYTES = 65536
#: Seconds a hook waits to connect.  A live server on this machine accepts at once.
CONNECT_SECONDS = 0.5
#: What a hook keeps back from its answer's wait, to print it and exit before the client's own timeout.
MARGIN_SECONDS = 0.5
_TOKEN_HEADER = "X-Scope-Recall-Token"
#: How long a hook waits for its server, below the timeout the installer gives each hook (``hooks.json``): Codex ends
#: PostToolUse at 2 s and Interrupt and SessionEnd at 3 s, and waits 10 s for Stop and 15 s for a prompt; Claude Code
#: waits 10 s for Stop and SessionEnd and 15 s for a prompt.  The server's own budget is the handler's (6 s for a
#: prompt and for a record read, 2 s otherwise), so its answer comes well inside these.
_WAITS = {"UserPromptSubmit": 9.0, "Stop": 8.0, "SessionEnd": 8.0, "PostToolUse": 1.8, "Interrupt": 2.5}
_CODEX_WAITS = {"SessionEnd": 2.5}


def client_wait(host: str, event: object) -> float | None:
    """How long a hook of ``event`` waits for its server; None for a hook that is not sent to one."""
    if type(event) is not str:
        return None
    if host == "codex" and event in _CODEX_WAITS:
        return _CODEX_WAITS[event]
    return _WAITS.get(event)


def endpoints(home: Path | str) -> Path:
    """Where the servers of one entry name themselves."""
    return Path(home) / "scope-recall" / "hook-endpoints"


class _Handler(BaseHTTPRequestHandler):
    server_version = "scope-recall-hooks"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class's name
        return  # the MCP server's stdout is the MCP protocol, and its stderr is the client's

    def do_POST(self) -> None:  # noqa: N802 - the base class's name
        endpoint: HookEndpoint = self.server.endpoint  # type: ignore[attr-defined]
        if self.path != "/hook":
            self._answer(404, {"error": "not_found"})
            return
        token = self.headers.get(_TOKEN_HEADER, "")
        if not hmac.compare_digest(token.encode("utf-8"), endpoint.token.encode("utf-8")):
            self._answer(401, {"error": "token"})
            return
        size = self.headers.get("Content-Length", "")
        if not size.isdigit() or int(size) > MAX_REQUEST_BYTES + 1024:
            self._answer(413, {"error": "size"})
            return
        try:
            body = json.loads(self.rfile.read(int(size)).decode("utf-8"))
            raw = body["hook"].encode("utf-8")
            elapsed = float(body.get("elapsed", 0.0))
            if len(raw) > MAX_REQUEST_BYTES or not 0.0 <= elapsed <= 60.0:
                raise ValueError("bounds")
        except (ValueError, KeyError, TypeError, UnicodeError):
            self._answer(400, {"error": "request"})
            return
        self._answer(200, endpoint.handle(raw, started=time.monotonic() - elapsed))

    def _answer(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=True).encode("ascii")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class HookEndpoint:
    """The MCP server's side: a 127.0.0.1 HTTP server in a daemon thread, and the file that names it."""

    def __init__(self, home: Path | str, host: str) -> None:
        self.home = Path(home)
        self.host = host
        self.token = secrets.token_urlsafe(32)
        self.path = endpoints(home) / f"{os.getpid()}.json"
        self._server: ThreadingHTTPServer | None = None

    def handle(self, raw: bytes, *, started: float) -> dict[str, Any]:
        """One hook, as ``hook_entry`` would have handled it in its own process."""
        from .handler import CodexHookHandler

        handler = CodexHookHandler.from_home(str(self.home), self.host, hook_started_at=started)
        try:
            result = handler.handle_bytes(raw)
        finally:
            handler.close()
        diagnostics = dataclasses.asdict(handler.diagnostics)
        diagnostics["capability_gaps"] = list(diagnostics.get("capability_gaps") or ())
        return {"result": result, "diagnostics": diagnostics}

    def start(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        server.endpoint = self  # type: ignore[attr-defined]
        self._server = server
        threading.Thread(target=server.serve_forever, name="scope-recall-hooks", daemon=True).start()
        from ..._version import __version__

        self.path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.path.with_suffix(".tmp")
        pending.write_text(json.dumps({"host": self.host, "port": server.server_address[1], "token": self.token,
                                       "pid": os.getpid(), "version": __version__}), encoding="utf-8")
        os.replace(pending, self.path)
        atexit.register(self.stop)
        if sys.platform == "win32":
            try:
                from ...vector.process_store import prestart
                prestart(keep=True)
            except OSError:
                pass  # each hook's recall then starts its own helper, as a hook of its own did

    def stop(self) -> None:
        server, self._server = self._server, None
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
        if server is not None:
            server.shutdown()
            server.server_close()


def serve(home: Path | str, host: str) -> HookEndpoint | None:
    """Answer this entry's hooks from this process until it exits; None when that cannot start."""
    endpoint = HookEndpoint(home, host)
    try:
        endpoint.start()
    except OSError:
        endpoint.stop()
        return None
    return endpoint


def ask(home: Path | str, host: str, raw: bytes, *, started: float,
        budget: float) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The answer of a server of this entry, or None when none took the hook (the caller then handles it itself).

    A server that took the hook and did not answer within ``budget`` gives ``({}, {})``: the turn has no memory, and
    handling it again here would run past the client's own hook timeout.
    """
    from ..._version import __version__

    folder = endpoints(home)
    try:
        named = sorted(folder.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return None
    body = json.dumps({"hook": raw.decode("utf-8", errors="replace"),
                       "elapsed": max(0.0, time.monotonic() - started)}).encode("utf-8")
    for path in named[:4]:
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
            port, token = int(info["port"]), str(info["token"])
            # A server started before an upgrade runs the code it was started with, until its client restarts.
            if info.get("host") != host or info.get("version") != __version__:
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=CONNECT_SECONDS)
        try:
            connection.connect()
        except OSError:
            # Nothing listens there: a server that ended without removing its name.
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        try:
            wait = budget - (time.monotonic() - started) - MARGIN_SECONDS
            if wait <= 0:
                return {}, {}
            connection.sock.settimeout(wait)
            connection.request("POST", "/hook", body=body,
                               headers={_TOKEN_HEADER: token, "Content-Type": "application/json"})
            response = connection.getresponse()
            answer = json.loads(response.read().decode("ascii"))
            if response.status != 200:
                continue
            return answer["result"], answer["diagnostics"]
        except (socket.timeout, TimeoutError):
            return {}, {}
        except (OSError, ValueError, KeyError, http.client.HTTPException):
            continue
        finally:
            connection.close()
    return None
