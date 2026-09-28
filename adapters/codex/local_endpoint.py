"""Prompts answered by the client's own MCP server, warm for as long as the client is open.

Claude Code and Codex start a new process for every hook, and a prompt hook that started LanceDB for its recall was
often not ready before the recall's budget ran out: on the pilot 6 of 8 cold Claude Code prompts recalled by words
alone (``helper_request_deadline``).  The client's MCP server lives exactly as long as the client, so it also answers
the entry's prompt hooks on this machine (``serve``), with a LanceDB helper kept ready (``vector.process_store``).
Only the prompt is sent: it is the hook that recalls.  The others store what was said and read no vectors, and do it
in their own process as before.

A prompt hook asks the newest server of its entry, host and version (``ask``).  Servers name themselves in a folder of
the user's own profile (``endpoints``), not in the entry's home, which may sit on a drive every account can read:
whoever holds a server's token can recall the owner's memory and store words as the owner's.  A name whose process is
gone, or is another process under a reused id, is removed without a connection.  Before a hook sends anything, the
server proves it holds the token, and it signs its answer; the token itself never crosses the socket, so a process
that took over a stopped server's port learns nothing and cannot answer for it.

A server that does not answer in time is named on stderr (``CODEX_HOOK:resident_timeout``) and its name is removed,
so later prompts go past it; the hook then does the work itself in a budget of its own, and the turn keeps its
capture.  A server that answers its own check names itself again (``ADVERTISE_SECONDS``).
"""
from __future__ import annotations

import atexit
import dataclasses
import hashlib
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
from typing import Any, Callable

#: What a hook may send: the same bound as a hook's own stdin.
MAX_REQUEST_BYTES = 65536
#: Seconds a hook waits to connect, and then for the server's proof.  A live server on this machine answers at once.
CONNECT_SECONDS = 0.3
PROOF_SECONDS = 1.0
#: Servers a hook tries, newest first, and how long it may spend finding one.
MAX_TRIED = 2
FIND_SECONDS = 1.5
#: Seconds from the hook's start that it waits for its server's answer.  The server's budget for a prompt is the
#: hook's own (6 s from the hook's start, ``hook_processing_seconds``), so a healthy one has answered by then; a hook
#: that stops waiting still has a whole prompt's budget of its own before the client's 15 s hook timeout.
PROMPT_WAIT_SECONDS = 7.0
#: How often a server looks for its own name, and puts it back when a hook removed it and it answers its own check.
ADVERTISE_SECONDS = 30.0
_NONCE = "X-Scope-Recall-Nonce"
_PROOF = "X-Scope-Recall-Proof"
_ELAPSED = "X-Scope-Recall-Elapsed"


def endpoints(home: Path | str) -> Path:
    """Where the servers of one entry name themselves: a folder of this user's profile, one for each home."""
    digest = hashlib.sha256(str(Path(home).expanduser().resolve()).encode("utf-8")).hexdigest()[:16]
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "scope-recall" / "hook-endpoints" / digest


def _proof(token: str, *parts: str) -> str:
    """What proves the token without sending it; the first part keeps a hello, a hook and an answer apart."""
    return hmac.new(token.encode("utf-8"), "\x00".join(parts).encode("utf-8"), hashlib.sha256).hexdigest()


def _proven(given: str | None, token: str, *parts: str) -> bool:
    return hmac.compare_digest((given or "").encode("ascii", "replace"), _proof(token, *parts).encode("ascii"))


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    #: Prompts from several sessions at once wait to be accepted rather than being refused.
    request_queue_size = 64


class _Handler(BaseHTTPRequestHandler):
    server_version = "scope-recall-hooks"
    protocol_version = "HTTP/1.1"
    #: A connection that sends nothing is let go rather than holding a thread.
    timeout = 30

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class's name
        return  # the MCP server's stdout is the MCP protocol, and its stderr is the client's

    def do_POST(self) -> None:  # noqa: N802 - the base class's name
        endpoint: HookEndpoint = self.server.endpoint  # type: ignore[attr-defined]
        nonce = self.headers.get(_NONCE, "")
        size = self.headers.get("Content-Length", "")
        if not (16 <= len(nonce) <= 64 and nonce.isalnum() and size.isdigit() and int(size) <= MAX_REQUEST_BYTES):
            self._refuse(400)
            return
        body = self.rfile.read(int(size))
        if self.path == "/hello":
            self._answer(b"{}", endpoint.token, "hello", nonce)
            return
        elapsed = self.headers.get(_ELAPSED, "")
        if self.path != "/hook" or not _proven(self.headers.get(_PROOF), endpoint.token, "hook", nonce,
                                               hashlib.sha256(body).hexdigest(), elapsed):
            self._refuse(401)
            return
        try:
            since = float(elapsed)
            if not 0.0 <= since <= 60.0:
                raise ValueError("elapsed")
        except ValueError:
            self._refuse(400)
            return
        answer = json.dumps(endpoint.handle(body, started=time.monotonic() - since), ensure_ascii=True).encode("ascii")
        self._answer(answer, endpoint.token, "answer", nonce, hashlib.sha256(answer).hexdigest())

    def _answer(self, data: bytes, token: str, *parts: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header(_PROOF, _proof(token, *parts))
        self.end_headers()
        self.wfile.write(data)

    def _refuse(self, status: int) -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()


def _hello(connection: http.client.HTTPConnection, token: str) -> bool:
    """Whether the server on this open connection holds ``token``: it proves it, and the token is not sent."""
    nonce = secrets.token_hex(16)
    try:
        connection.sock.settimeout(PROOF_SECONDS)
        connection.request("POST", "/hello", body=b"", headers={_NONCE: nonce})
        hello = connection.getresponse()
        hello.read()
    except (OSError, http.client.HTTPException):
        return False
    return hello.status == 200 and _proven(hello.getheader(_PROOF), token, "hello", nonce)


def _exchange(port: int, token: str, raw: bytes, *, started: float) -> tuple[str, Any]:
    """One prompt to the server on ``port``: ``("answered", (result, diagnostics))``, or why not."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=CONNECT_SECONDS)
    try:
        try:
            connection.connect()
        except ConnectionRefusedError:
            return "refused", None
        except OSError:
            return "unreachable", None
        if not _hello(connection, token):
            return "unproven", None
        wait = PROMPT_WAIT_SECONDS - (time.monotonic() - started)
        if wait <= 0:
            return "timeout", None
        nonce, elapsed = secrets.token_hex(16), f"{max(0.0, time.monotonic() - started):.3f}"
        headers = {_NONCE: nonce, _ELAPSED: elapsed, "Content-Type": "application/json",
                   _PROOF: _proof(token, "hook", nonce, hashlib.sha256(raw).hexdigest(), elapsed)}
        try:
            connection.sock.settimeout(wait)
            connection.request("POST", "/hook", body=raw, headers=headers)
            response = connection.getresponse()
            data = response.read()
        except (socket.timeout, TimeoutError):
            return "timeout", None
        except (OSError, http.client.HTTPException):
            # It may have stored the prompt before the connection broke; the hook's own capture of the same prompt
            # is then a duplicate (its key), not a second copy.
            return "timeout", None
        if response.status != 200 or not _proven(response.getheader(_PROOF), token, "answer", nonce,
                                                 hashlib.sha256(data).hexdigest()):
            return "unproven", None
        answer = json.loads(data.decode("ascii"))
        return "answered", (answer["result"], answer["diagnostics"])
    finally:
        connection.close()


def _forget(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def ask(home: Path | str, host: str, raw: bytes, *, started: float) -> tuple[str, Any]:
    """Hand one prompt to a server of this entry: ``("answered", (result, diagnostics))``; ``("timeout", None)`` when
    one took it and did not answer in time; ``("none", None)`` when none took it.  In the last two the hook does the
    work itself."""
    from ..._version import __version__
    from ...runtime.process_probe import probe_process

    try:
        named = sorted(endpoints(home).glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return "none", None
    tried = 0
    for path in named:
        if tried >= MAX_TRIED or time.monotonic() - started > FIND_SECONDS:
            break
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
            port, token, pid = int(info["port"]), str(info["token"]), int(info["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        # A server started before an upgrade runs the code it was started with, until its client restarts.
        if info.get("host") != host or info.get("version") != __version__:
            continue
        try:
            state = probe_process(pid)
        except (OSError, ValueError):
            continue
        if not state.running or (info.get("start") and state.start_token and state.start_token != info["start"]):
            _forget(path)  # a server that ended without removing its name, or another process under its id
            continue
        tried += 1
        outcome, answer = _exchange(port, token, raw, started=started)
        if outcome == "answered":
            return outcome, answer
        if outcome == "timeout":
            _forget(path)
            return outcome, None
        if outcome in ("refused", "unproven"):
            _forget(path)
    return "none", None


class HookEndpoint:
    """The MCP server's side: a 127.0.0.1 HTTP server in a daemon thread, and the file that names it."""

    def __init__(self, home: Path | str, host: str, *, env_file: Path | None = None,
                 refresh: Callable[[], object] | None = None) -> None:
        self.home = Path(home)
        self.host = host
        self.token = secrets.token_urlsafe(32)
        self.path = endpoints(home) / f"{os.getpid()}.json"
        self.port = 0
        self._server: _Server | None = None
        self._stopped = threading.Event()
        # A key rotated in the env file is taken up at the next prompt, as a hook of its own would read it.
        self._env_file, self._refresh = env_file, refresh
        self._env_seen = self._env_stamp()
        self._env_lock = threading.Lock()

    def _env_stamp(self) -> tuple[int, int] | None:
        try:
            status = self._env_file.stat() if self._env_file is not None else None
        except OSError:
            return None
        return (status.st_mtime_ns, status.st_size) if status is not None else None

    def handle(self, raw: bytes, *, started: float) -> dict[str, Any]:
        """One prompt, as ``hook_entry`` would have handled it in its own process."""
        from .handler import CodexHookHandler

        with self._env_lock:
            stamp = self._env_stamp()
            if self._refresh is not None and stamp != self._env_seen:
                self._env_seen = stamp
                self._refresh()
        handler = CodexHookHandler.from_home(str(self.home), self.host, hook_started_at=started)
        try:
            result = handler.handle_bytes(raw)
        finally:
            handler.close()
        diagnostics = dataclasses.asdict(handler.diagnostics)
        diagnostics["capability_gaps"] = list(diagnostics.get("capability_gaps") or ())
        return {"result": result, "diagnostics": diagnostics}

    def _advertise(self) -> None:
        from ..._version import __version__
        from ...runtime.process_probe import probe_process

        folder = self.path.parent
        folder.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            for part in (folder, folder.parent, folder.parent.parent):
                part.chmod(0o700)
        record = {"host": self.host, "port": self.port, "token": self.token, "pid": os.getpid(),
                  "start": probe_process(os.getpid()).start_token, "version": __version__}
        pending = self.path.with_suffix(".tmp")
        handle = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(record))
        os.replace(pending, self.path)

    def _keep_named(self) -> None:
        while not self._stopped.wait(ADVERTISE_SECONDS):
            if self.path.exists():
                continue
            # A hook removed the name of a server that kept it waiting.  One that proves itself again, as a hook
            # would check it, names itself again; a hung one cannot.
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=CONNECT_SECONDS)
            try:
                connection.connect()
                answers = _hello(connection, self.token)
            except OSError:
                answers = False
            finally:
                connection.close()
            if answers and not self._stopped.is_set():
                try:
                    self._advertise()
                except OSError:
                    pass

    def start(self) -> None:
        server = _Server(("127.0.0.1", 0), _Handler)
        server.endpoint = self  # type: ignore[attr-defined]
        self._server = server
        self.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, name="scope-recall-hooks", daemon=True).start()
        self._advertise()
        threading.Thread(target=self._keep_named, name="scope-recall-hooks-name", daemon=True).start()
        atexit.register(self.stop)
        if sys.platform == "win32":
            try:
                from ...vector.process_store import prestart
                prestart(keep=True)
            except OSError:
                pass  # each prompt's recall then starts its own helper, as a hook of its own did

    def stop(self) -> None:
        self._stopped.set()
        server, self._server = self._server, None
        _forget(self.path)
        if server is not None:
            server.shutdown()
            server.server_close()


def serve(home: Path | str, host: str, *, env_file: Path | None = None,
          refresh: Callable[[], object] | None = None) -> HookEndpoint | None:
    """Answer this entry's prompts from this process until it exits; None when that cannot start."""
    endpoint = HookEndpoint(home, host, env_file=env_file, refresh=refresh)
    try:
        endpoint.start()
    except OSError:
        endpoint.stop()
        return None
    return endpoint
