"""A prompt's recall answered by the client's own MCP server, warm for as long as the client is open.

Claude Code and Codex start a new process for every hook, and a prompt hook that started LanceDB for its recall was
often not ready before the recall's budget ran out: on the pilot 6 of 8 cold Claude Code prompts recalled by words
alone (``helper_request_deadline``).  The client's MCP server lives exactly as long as the client, so it keeps a
LanceDB helper ready and answers the entry's prompt hooks on this machine with the prompt's recall (``serve``).

Only the recall is asked for, and the server writes nothing.  The hook stores the prompt itself, as before, and asks
for the recall after (``handler._resident_answer``); if the server does not answer in time the hook recalls itself
in the time it kept back, and a late answer is dropped.  A first version had the server store the prompt as well:
one that answered after the hook stopped waiting left the prompt stored twice.

A hook asks the newest server of its entry, host and version (``Recaller``).  Servers name themselves in a folder of
the user's own profile (``endpoints``), not in the entry's home, which may sit on a drive every account can read:
whoever holds a server's token can read the owner's memory through it.  A name whose process is gone, or is another
process under a reused id (the start time is kept with the id), is removed without a connection.  Before a hook sends
anything the server proves it holds the token; the hook proves it too, and the server signs its answer.  The token
never crosses the socket, so a process that took over a stopped server's port learns nothing and cannot answer for
it.  A hook says how its server answered on stderr (``CODEX_RECALL_RESIDENT:<outcome>``).  A server that kept a
prompt waiting loses its name; it names itself again only once none of its recalls is stuck.
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

#: What a hook may send: its payload (a hook's own stdin is at most 64 KiB, and written as ASCII JSON a character
#: of it takes up to six bytes) and the refs and gaps of its capture.
MAX_REQUEST_BYTES = 7 * 65536
#: Seconds a hook waits to connect, and then for the server's proof.  A live server on this machine answers at once.
CONNECT_SECONDS = 0.3
PROOF_SECONDS = 1.0
#: Servers a hook tries, newest first, and how long it may spend finding one.
MAX_TRIED = 2
FIND_SECONDS = 1.0
#: Of the time a hook gives its server, what the server keeps back for its answer to reach the hook.
ANSWER_MARGIN_SECONDS = 0.3
#: Recalls one server runs at once; a hook past that recalls itself.
MAX_CONCURRENT = 8
#: A recall running this much past the time its hook gave it is stuck: its server does not name itself again until
#: it ends.
STUCK_GRACE_SECONDS = 2.0
#: How often a server looks for its own name, and puts it back when a hook removed it.
ADVERTISE_SECONDS = 30.0
_NONCE = "X-Scope-Recall-Nonce"
_PROOF = "X-Scope-Recall-Proof"


def endpoints(home: Path | str) -> Path:
    """Where the servers of one entry name themselves: a folder of this user's profile, one for each home.

    ``~/.cache`` rather than ``XDG_CACHE_HOME`` on POSIX: Codex does not pass that to its MCP servers, and a server
    and its hooks that looked in different folders would never meet."""
    digest = hashlib.sha256(str(Path(home).expanduser().resolve()).encode("utf-8")).hexdigest()[:16]
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path.home() / ".cache"
    return base / "scope-recall" / "hook-endpoints" / digest


def _proof(token: str, *parts: str) -> str:
    """What proves the token without sending it; the first part keeps a hello, a recall and an answer apart."""
    return hmac.new(token.encode("utf-8"), "\x00".join(parts).encode("utf-8"), hashlib.sha256).hexdigest()


def _proven(given: str | None, token: str, *parts: str) -> bool:
    return hmac.compare_digest((given or "").encode("ascii", "replace"), _proof(token, *parts).encode("ascii"))


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    #: Prompts from several sessions at once wait to be accepted rather than being refused.
    request_queue_size = 64


class _Handler(BaseHTTPRequestHandler):
    server_version = "scope-recall-recall"
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
        if self.path != "/recall" or not _proven(self.headers.get(_PROOF), endpoint.token, "recall", nonce,
                                                 hashlib.sha256(body).hexdigest()):
            self._refuse(401)
            return
        try:
            request = _request(body)
        except (ValueError, KeyError, TypeError, UnicodeError):
            self._refuse(400)
            return
        if not endpoint.slots.acquire(blocking=False):
            self._refuse(503)
            return
        received = time.monotonic()
        close = None
        with endpoint.lock:
            endpoint.inflight[id(self)] = received + request["remaining"] + STUCK_GRACE_SECONDS
        try:
            answer_body, close = endpoint.recall(request, received=received)
            data = json.dumps(answer_body, ensure_ascii=True).encode("ascii")
            self._answer(data, endpoint.token, "answer", nonce, hashlib.sha256(data).hexdigest())
        finally:
            with endpoint.lock:
                endpoint.inflight.pop(id(self), None)
            endpoint.slots.release()
            # Closed after the answer is out: closing the runtime ends its vector helper, which can take seconds.
            if close is not None:
                close()

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


def _request(body: bytes) -> dict[str, Any]:
    request = json.loads(body.decode("utf-8"))
    payload, refs, gaps, remaining = request["payload"], request["current_refs"], request["gaps"], request["remaining"]
    if (type(payload) is not dict or type(refs) is not list or len(refs) > 64
            or not all(type(ref) is str and len(ref) <= 200 for ref in refs)
            or type(gaps) is not list or len(gaps) > 64 or not all(type(gap) is str and len(gap) <= 200 for gap in gaps)
            or type(remaining) not in (int, float) or not 0.0 <= remaining <= 10.0):
        raise ValueError("request")
    return {"payload": payload, "current_refs": tuple(refs), "gaps": tuple(gaps), "remaining": float(remaining)}


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


def _forget(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _named(folder: Path) -> list[Path]:
    """The names in a folder, newest first; one removed while this looks is passed over."""
    found = []
    try:
        paths = list(folder.glob("*.json"))
    except OSError:
        return []
    for path in paths:
        try:
            found.append((path.stat().st_mtime, path))
        except OSError:
            continue
    return [path for _mtime, path in sorted(found, key=lambda item: item[0], reverse=True)]


class Recaller:
    """The hook's side: asks the newest server of its entry for one prompt's recall (``handler.resident_recall``).

    ``outcome`` says how it went, for the hook's stderr: ``answered``; ``late`` (a server took the prompt and did not
    answer in time, and loses its name); ``busy``; ``unproven`` (a program on the port, or a broken answer); ``none``
    (no server of this entry, host and version runs)."""

    def __init__(self, home: Path | str, host: str) -> None:
        self.home = Path(home)
        self.host = host
        self.outcome: str | None = None

    def __call__(self, payload: dict[str, Any], current_refs: tuple[str, ...], gaps: tuple[str, ...],
                 budget: float) -> tuple[dict[str, Any], dict[str, Any]] | None:
        from ..._version import __version__
        from ...runtime.process_probe import probe_process

        started = time.monotonic()
        self.outcome = "none"
        request = {"payload": payload, "current_refs": list(current_refs), "gaps": list(gaps)}
        tried = 0
        for path in _named(endpoints(self.home)):
            if tried >= MAX_TRIED or time.monotonic() - started > FIND_SECONDS:
                break
            try:
                info = json.loads(path.read_text(encoding="utf-8"))
                port, token, pid = int(info["port"]), str(info["token"]), int(info["pid"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            try:
                state = probe_process(pid)
            except (OSError, ValueError):
                continue
            # Its process is gone, or another holds its id: our own server runs as this user, so its start time can
            # be read, and one that cannot (another account's process) is not it.
            if not state.running or state.start_token != info.get("start"):
                _forget(path)
                continue
            # A server started before an upgrade runs the code it was started with, until its client restarts.
            if info.get("host") != self.host or info.get("version") != __version__:
                continue
            tried += 1
            outcome, answer = self._exchange(port, token, request, until=started + budget)
            if outcome == "answered":
                self.outcome = outcome
                return answer
            if outcome in ("late", "unproven"):
                _forget(path)
            self.outcome = outcome
            if outcome == "late":
                return None
        return None

    def _exchange(self, port: int, token: str, request: dict[str, Any], *, until: float) -> tuple[str, Any]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=CONNECT_SECONDS)
        try:
            try:
                connection.connect()
            except OSError:
                return "none", None  # busy or gone; the name stays for its process's own check above
            if not _hello(connection, token):
                return "unproven", None
            # The server's time is what is left now, after finding and checking it, less the answer's way back.
            wait = until - time.monotonic()
            if wait - ANSWER_MARGIN_SECONDS < 0.5:
                return "none", None  # never sent: the server did nothing wrong
            body = json.dumps({**request, "remaining": min(10.0, wait - ANSWER_MARGIN_SECONDS)},
                              ensure_ascii=True).encode("ascii")
            if len(body) > MAX_REQUEST_BYTES:
                return "none", None
            nonce = secrets.token_hex(16)
            headers = {_NONCE: nonce, "Content-Type": "application/json",
                       _PROOF: _proof(token, "recall", nonce, hashlib.sha256(body).hexdigest())}
            try:
                connection.sock.settimeout(wait)
                connection.request("POST", "/recall", body=body, headers=headers)
                response = connection.getresponse()
                data = response.read()
            except (socket.timeout, TimeoutError):
                return "late", None
            except (OSError, http.client.HTTPException):
                return "unproven", None
            if response.status == 503:
                return "busy", None
            if response.status != 200 or not _proven(response.getheader(_PROOF), token, "answer", nonce,
                                                     hashlib.sha256(data).hexdigest()):
                return "unproven", None
            answer = json.loads(data.decode("ascii"))
            return "answered", (answer["result"], answer["diagnostics"])
        except (ValueError, KeyError, TypeError):
            return "unproven", None
        finally:
            connection.close()


class HookEndpoint:
    """The MCP server's side: a 127.0.0.1 HTTP server in a daemon thread, and the file that names it."""

    def __init__(self, home: Path | str, host: str, *, env_file: Path | None = None,
                 credentials: Callable[[], dict[str, str]] | None = None) -> None:
        self.home = Path(home)
        self.host = host
        self.token = secrets.token_urlsafe(32)
        self.path = endpoints(home) / f"{os.getpid()}.json"
        self.port = 0
        self.slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        self.lock = threading.Lock()
        self.inflight: dict[int, float] = {}
        self._server: _Server | None = None
        self._stopped = threading.Event()
        # A key rotated in the env file is taken up at the next prompt, as a hook of its own would read it, and one
        # taken out of it is taken out here too.
        self._env_file, self._credentials = env_file, credentials
        self._env_seen = self._env_stamp()
        self._env_loaded: dict[str, str] = {}
        if credentials is not None:
            try:
                self._env_loaded = dict(credentials())  # what the server loaded at its start
            except (OSError, ValueError):
                pass

    def _env_stamp(self) -> tuple[int, int] | None:
        try:
            status = self._env_file.stat() if self._env_file is not None else None
        except OSError:
            return None
        return (status.st_mtime_ns, status.st_size) if status is not None else None

    def _refresh_credentials(self) -> None:
        with self.lock:
            stamp = self._env_stamp()
            if self._credentials is None or stamp == self._env_seen:
                return
            try:
                loaded = dict(self._credentials())
            except (OSError, ValueError):
                return  # read again at the next prompt (a file just saved can be locked); what is loaded stays
            self._env_seen = stamp
            for name in set(self._env_loaded) - set(loaded):
                os.environ.pop(name, None)
            os.environ.update(loaded)
            self._env_loaded = loaded

    def recall(self, request: dict[str, Any], *, received: float | None = None
               ) -> tuple[dict[str, Any], Callable[[], None]]:
        """One prompt's recall, as its hook would have recalled it; the handler is closed by the caller once the
        answer is out.  Its time counts from the request's arrival, loading the handler included."""
        from .handler import CodexHookHandler

        received = time.monotonic() if received is None else received
        self._refresh_credentials()
        handler = CodexHookHandler.from_home(str(self.home), self.host)
        try:
            remaining = max(0.0, request["remaining"] - (time.monotonic() - received))
            result = handler.resident_recall_for(request["payload"], request["current_refs"], request["gaps"],
                                                 remaining)
        except BaseException:
            handler.close()
            raise
        diagnostics = dataclasses.asdict(handler.diagnostics)
        diagnostics["capability_gaps"] = list(diagnostics.get("capability_gaps") or ())
        return {"result": result, "diagnostics": diagnostics}, handler.close

    def _stuck(self) -> bool:
        with self.lock:
            return any(time.monotonic() > due for due in self.inflight.values())

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
            # A hook removed the name of a server that kept a prompt waiting.  It names itself again once none of
            # its recalls is stuck and it proves itself as a hook would check it; a hung one cannot.
            if self.path.exists() or self._stuck():
                continue
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
        threading.Thread(target=server.serve_forever, name="scope-recall-recall", daemon=True).start()
        self._advertise()
        threading.Thread(target=self._keep_named, name="scope-recall-recall-name", daemon=True).start()
        atexit.register(self.stop)
        if sys.platform == "win32":
            try:
                from ...vector.process_store import prestart
                prestart(keep=True)
            except OSError:
                pass  # each prompt's recall then starts its own helper, as a hook of its own does

    def stop(self) -> None:
        self._stopped.set()
        server, self._server = self._server, None
        _forget(self.path)
        if server is not None:
            server.shutdown()
            server.server_close()


def serve(home: Path | str, host: str, *, env_file: Path | None = None,
          credentials: Callable[[], dict[str, str]] | None = None) -> HookEndpoint | None:
    """Answer this entry's prompt recalls from this process until it exits; None when that cannot start."""
    try:
        endpoint = HookEndpoint(home, host, env_file=env_file, credentials=credentials)
    except Exception:  # noqa: BLE001 - the MCP server starts whatever this does; its hooks recall themselves
        return None
    try:
        endpoint.start()
    except Exception:  # noqa: BLE001 - as above
        endpoint.stop()
        return None
    return endpoint


def live_server(home: Path | str, host: str) -> bool:
    """Whether a server of this entry, host and version names itself and its process runs: a prompt hook then starts
    no LanceDB helper of its own, which its recall would not use."""
    from ..._version import __version__
    from ...runtime.process_probe import probe_process

    for path in _named(endpoints(home)):
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
            if info.get("host") != host or info.get("version") != __version__:
                continue
            state = probe_process(int(info["pid"]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if state.running and state.start_token == info.get("start"):
            return True
    return False
