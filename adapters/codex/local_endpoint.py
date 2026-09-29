"""A prompt's recall answered by the client's own MCP server, warm for as long as the client is open.

Claude Code and Codex start a new process for every hook, and a prompt hook that started LanceDB for its recall was
often not ready before the recall's budget ran out: on the pilot 6 of 8 cold Claude Code prompts recalled by words
alone (``helper_request_deadline``).  The client's MCP server lives exactly as long as the client, so it keeps a
LanceDB helper ready and answers the entry's prompt hooks on this machine with the prompt's recall (``serve``).

Only the recall is asked for, and the server writes nothing.  The hook stores the prompt itself, as before, and asks
for the recall after (``handler._resident_answer``), with all of its time; if the server has not answered when 1.5 s
are left, the hook recalls as well and uses the answer that ran its vector search, and one that comes after the hook
is done is dropped.  A first version had the server store the prompt as well: one that answered after the hook stopped waiting
left the prompt stored twice.

A hook asks the newest server of its entry, host and version (``Recaller``).  Servers name themselves in a folder of
the user's own profile (``endpoints``), not in the entry's home, which may sit on a drive every account can read:
whoever holds a server's token can read the owner's memory through it.  A name whose process is gone, or is another
process under a reused id (the start time is kept with the id), is removed without a connection.  Before a hook sends
anything the server proves it holds the token; the hook proves it too, and the server signs its answer.  The token
never crosses the socket, so a process that took over a stopped server's port learns nothing and cannot answer for
it.  A hook says how its server answered on stderr (``CODEX_RECALL_RESIDENT:<outcome>``).  A server with a recall
past the time its hook gave it answers every hook that it is busy until that recall ends.  One that does not prove
itself in time (a program on its port, a process that no longer runs its threads, or one too busy) loses its name,
and names itself again once it answers its own check in time and none of its recalls is stuck.
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
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

#: What a hook may send: its payload (a hook's own stdin is at most 64 KiB, and written as ASCII JSON a character
#: of it takes up to six bytes) and the refs and gaps of its capture.
MAX_REQUEST_BYTES = 7 * 65536
#: Seconds a hook waits to connect, and then for the server's proof.  A live server on this machine takes the
#: connection at once; one that does not prove itself in time loses its name, with time left to try the next (a hung
#: first name took all of ``FIND_SECONDS``, and kept, it cost every later prompt its wait, reviews of rc11).
CONNECT_SECONDS = 0.3
#: A server serving several recalls at once proves itself in 0.15-0.3 s (each hand-over of Python's lock waits for a
#: timer tick on Windows): at 0.3 s such a server lost its name (review of rc11).
PROOF_SECONDS = 0.5
#: What a server's check of itself may take to name itself again, by the clock.  Made from inside the busy process, the
#: check waits for its own share of Python's lock besides the answer, and reads what a hook sees times 1.3-1.9 as a
#: rule (up to 3.7 under the heaviest load measured).  Held to ``PROOF_SECONDS`` it kept out 14% of the servers hooks
#: reached in time; at twice, it let back 31% of those they could not; at one and a half, 4% and 10% (reviews of rc11).
SELF_CHECK_SECONDS = 1.5 * PROOF_SECONDS
#: Servers a hook tries, newest first, and how long it may spend finding one.
MAX_TRIED = 2
FIND_SECONDS = 1.0
#: Of the time a hook gives its server, what the server keeps back for its answer to reach the hook.
ANSWER_MARGIN_SECONDS = 0.3
#: What a server's start may spend warming its kept handler's vector store (the table open and the first search, each
#: a few seconds on a large store), and the share of its time a recall that comes meanwhile waits for that.
WARM_SECONDS = 60.0
WARM_WAIT_SHARE = 0.5
#: What closing waits for a recall that holds the kept handler (a prompt's recall ends within its hook's time).
CLOSE_WAIT_SECONDS = 10.0
#: Recalls one server runs at once; a hook past that recalls itself.
MAX_CONCURRENT = 8
#: How often a server looks for its own name, and puts it back when a hook removed it: a busy server that did not
#: prove itself in time was left out for 30 s (review of rc11).
ADVERTISE_SECONDS = 2.0
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

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001 - the base class's signature
        # One line on the client's stderr, not a traceback: a hook that is done with its server closes its end.  A
        # recall that fails is answered as failed, with its traceback (``_Handler.do_POST``).
        sys.stderr.write(f"SCOPE_RECALL_ENDPOINT:{type(sys.exc_info()[1]).__name__}\n")


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
        if endpoint._stuck():
            # A recall is past the time its hook gave it, and what holds it may hold the next: hooks go on at once
            # until it ends.  Counted 2 s later, a hung server answered, named itself again, and the next prompt
            # waited on it (review of rc11).
            self._refuse(503)
            return
        if self.path == "/hello":
            self._answer(b"{}", endpoint.token, "hello", nonce)
            return
        if self.path != "/recall" or not _proven(self.headers.get(_PROOF), endpoint.token, "recall", nonce,
                                                 hashlib.sha256(body).hexdigest()):
            self._refuse(401)
            return
        try:
            request = _request(body)
        except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
            self._refuse(400)
            return
        if not endpoint.slots.acquire(blocking=False):
            self._refuse(503)
            return
        received = time.monotonic()
        close = None
        with endpoint.lock:
            endpoint.inflight[id(self)] = received + request["remaining"]
        try:
            try:
                answer_body, close = endpoint.recall(request, received=received)
            except Exception as exc:  # noqa: BLE001 - answered as a failed recall; the hook recalls itself
                # Dropped, the hook took the server for another program and removed its name, which came back and
                # failed the same way; and the log held only the error's class (review of rc11).
                sys.stderr.write(f"SCOPE_RECALL_ENDPOINT:recall_failed\n{traceback.format_exc(limit=-8)}")
                code = getattr(exc, "code", None)
                detail = f"{type(exc).__name__}:{code}" if isinstance(code, str) else type(exc).__name__
                answer_body = {"result": {}, "diagnostics": {"last_reason": "recall_exception",
                                                             "recall_error_detail": detail[:64]}}
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


def _hello(connection: http.client.HTTPConnection, token: str) -> str:
    """Whether the server on this open connection holds ``token``: ``ok`` once it proves it (the token is not sent),
    ``busy`` when it says so, ``unproven`` when it does not answer in time or answers otherwise."""
    nonce = secrets.token_hex(16)
    try:
        connection.sock.settimeout(PROOF_SECONDS)
        connection.request("POST", "/hello", body=b"", headers={_NONCE: nonce})
        hello = connection.getresponse()
        hello.read()
    except (socket.timeout, TimeoutError, OSError, http.client.HTTPException):
        return "unproven"
    if hello.status == 503:
        return "busy"
    return "ok" if hello.status == 200 and _proven(hello.getheader(_PROOF), token, "hello", nonce) else "unproven"


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
    answer in time); ``busy`` (its recalls all taken, or one of them stuck); ``refused`` (it could not read this
    request); ``unproven`` (no proof in time, a
    program on the port, or a broken answer: the name is removed); ``none`` (no server of this entry, host and version
    runs).  The hook says what it did with an answer (``handler._resident_answer``): ``failed:<reason>`` when the
    server's recall failed, or came back empty because its read did not finish (``recall_incomplete``),
    ``without_vectors:<gap>`` when the hook's own recall had its vector search and the server's did not, ``slow`` when
    the hook's own, with it, was done first, and ``late`` when no answer came before the hook's own time was up."""

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
            if outcome == "unproven":
                _forget(path)
            self.outcome = outcome
            if outcome in ("late", "refused"):
                return None  # the next would take this request no differently
        return None

    def _exchange(self, port: int, token: str, request: dict[str, Any], *, until: float) -> tuple[str, Any]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=CONNECT_SECONDS)
        try:
            try:
                connection.connect()
            except OSError:
                return "none", None  # busy or gone; the name stays for its process's own check above
            proof = _hello(connection, token)
            if proof != "ok":
                return proof, None
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
            if response.status == 400:
                return "refused", None  # this request, not the server: its name stays
            if response.status != 200 or not _proven(response.getheader(_PROOF), token, "answer", nonce,
                                                     hashlib.sha256(data).hexdigest()):
                return "unproven", None
            answer = json.loads(data.decode("ascii"))
            return "answered", (answer["result"], answer["diagnostics"])
        except (ValueError, KeyError, TypeError):
            return "unproven", None
        finally:
            connection.close()


def file_stamp(*paths: Path | None) -> tuple:
    """When each file last changed, and its size; None for one that cannot be read (``KeptRecaller``)."""
    stamps = []
    for path in paths:
        if path is None:
            continue
        try:
            status = path.stat()
        except OSError:
            stamps.append(None)
        else:
            stamps.append((status.st_mtime_ns, status.st_size))
    return tuple(stamps)


def entry_files(home: Path | str) -> tuple[Path, ...]:
    """What a shared entry's handler is made from besides its credentials and runtime config: its pointer to the
    store, and the store's record of the entry's grants and binding."""
    from ..hermes.installation import MANIFEST_FILENAME, attachment_path, read_attachment

    files = [attachment_path(Path(home))]
    try:
        attachment = read_attachment(Path(home))
    except Exception:  # noqa: BLE001 - an unreadable pointer is one more reason the stamp changed
        attachment = None
    if attachment is not None:
        files.append(Path(attachment.root) / MANIFEST_FILENAME)
    return tuple(files)


def _nothing() -> None:
    return None


def _close_later(handler: Any) -> None:
    """Close a handler off the request's time: its vector helper can take seconds to stop."""
    def close() -> None:
        try:
            handler.close()
        except Exception:  # noqa: BLE001 - a handler that cannot close is dropped all the same
            pass

    threading.Thread(target=close, name="scope-recall-kept-close", daemon=True).start()


class KeptRecaller:
    """One handler kept across a long-lived server's prompt recalls, with the vector store and embedding worker its
    runtime keeps open.

    A server that made a handler for each recall opened the LanceDB table (about 2.3 s) and started the embedding
    worker and its connection (about 1 s) for every prompt: on the pilot a warm server's recall took 3.9-4.1 s and
    two of five lost their vector search to the time; with the handler kept, 1.6-2.1 s with it (rc12).  The handler
    only recalls (``resident_recall_for``), which writes nothing.  One recall uses it at a time: another at the same
    moment gets None, and its caller recalls as it did before.  It is made anew when ``stamp`` changes (the files it
    was made from), after a recall that raised, and while its runtime is not attached from a readable config; the
    handler it replaces is closed after, not within, the recall that replaced it.  Once closed it answers nothing."""

    def __init__(self, build: Callable[[], Any], stamp: Callable[[], object] = tuple) -> None:
        self._build = build
        self._stamp = stamp
        self._lock = threading.Lock()
        self._handler: Any = None
        self._made_with: object = None
        self._closed = False
        self._warming: threading.Event | None = None

    def warm(self, seconds: float = WARM_SECONDS) -> None:
        """Make the handler and warm its vector store in the background, when the server starts.

        Made at the first prompt, the handler attached its runtime, started the vector helper, opened the table and
        read the index inside that prompt's recall, and the first prompt after every start recalled by words alone:
        for Claude Code that is every session.  A recall that comes meanwhile waits for this (``__call__``) instead
        of making a second handler.  It writes nothing."""
        done = threading.Event()
        self._warming = done

        def run() -> None:
            try:
                with self._lock:
                    if self._closed or self._handler is not None:
                        return
                    # The stamp before the build, as a recall takes it: a change during the build is then a change.
                    stamp = self._stamp()
                    self._handler, self._made_with = self._build(), stamp
                    try:
                        self._handler.warm_vectors(seconds)
                    except Exception:  # noqa: BLE001 - the first recall opens what is not open, as before
                        pass
                    if self._closed or not getattr(self._handler, "runtime_ready", False):
                        self._discard(later=True)
            except Exception:  # noqa: BLE001 - a handler that cannot be made now is made by the first recall
                pass
            finally:
                done.set()
                if self._closed:
                    # A close during the warming did not wait for it (``close``): the handler it made is closed here.
                    with self._lock:
                        self._discard(later=True)

        threading.Thread(target=run, name="scope-recall-kept-warm", daemon=True).start()

    def __call__(self, payload: dict[str, Any], current_refs: tuple[str, ...], gaps: tuple[str, ...], budget: float,
                 *, received: float | None = None) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """This prompt's (result, diagnostics), in ``budget`` seconds from ``received``; None when another recall
        holds the handler or the recaller is closed."""
        received = time.monotonic() if received is None else received
        warming = self._warming
        if warming is not None and not warming.is_set():
            # The server's start is making the handler this recall needs: wait for it, up to half of the recall's time.
            warming.wait(max(0.0, budget - (time.monotonic() - received)) * WARM_WAIT_SHARE)
        if not self._lock.acquire(blocking=False):
            return None
        try:
            if self._closed:
                return None
            stamp = self._stamp()
            if self._handler is not None and stamp != self._made_with:
                self._discard(later=True)
            if self._handler is None:
                self._handler, self._made_with = self._build(), stamp
            handler = self._handler
            try:
                result = handler.resident_recall_for(payload, current_refs, gaps,
                                                     max(0.0, budget - (time.monotonic() - received)))
            except BaseException:
                self._discard(later=True)
                raise
            diagnostics = dataclasses.asdict(handler.diagnostics)
            diagnostics["capability_gaps"] = list(diagnostics.get("capability_gaps") or ())
            if not getattr(handler, "runtime_ready", False):
                self._discard(later=True)
            return result, diagnostics
        finally:
            self._lock.release()

    def _discard(self, *, later: bool = False) -> None:
        handler, self._handler = self._handler, None
        if handler is None:
            return
        if later:
            _close_later(handler)
            return
        try:
            handler.close()
        except Exception:  # noqa: BLE001 - a handler that cannot close is dropped all the same
            pass

    def close(self) -> None:
        """Close the kept handler, once a recall that holds it is done; later recalls get None.  A warming that holds
        it (up to ``WARM_SECONDS``) is not waited for: it sees the recaller closed and closes its handler itself."""
        self._closed = True
        warming = self._warming
        if warming is not None and not warming.is_set():
            return
        if not self._lock.acquire(timeout=CLOSE_WAIT_SECONDS):
            return
        try:
            self._discard()
        finally:
            self._lock.release()


class HookEndpoint:
    """The MCP server's side: a 127.0.0.1 HTTP server in a daemon thread, and the file that names it."""

    def __init__(self, home: Path | str, host: str, *, env_file: Path | None = None,
                 runtime_config: Path | None = None,
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
        # taken out of it is taken out here too; so is a key the runtime config comes to name instead.  What cannot be
        # read now is read at the next prompt: a server whose first read failed, here or at its own start, recalled
        # by words alone until its client restarted (review of rc11).
        self._watched = tuple(path for path in (env_file, runtime_config) if path is not None)
        self._credentials = credentials
        self._env_seen: tuple | None = None
        self._env_loaded: dict[str, str] = {}
        self.kept = KeptRecaller(self._handler, stamp=self._kept_stamp)
        if credentials is not None:
            stamp = self._env_stamp()
            try:
                loaded = dict(credentials())
            except Exception:  # noqa: BLE001 - read again at the first prompt
                pass
            else:
                os.environ.update(loaded)
                self._env_loaded, self._env_seen = loaded, stamp

    def _env_stamp(self) -> tuple:
        return file_stamp(*self._watched)

    def _kept_stamp(self) -> tuple:
        return file_stamp(*self._watched, *entry_files(self.home))

    def _handler(self) -> Any:
        from .handler import CodexHookHandler

        return CodexHookHandler.from_home(str(self.home), self.host)

    def _refresh_credentials(self) -> None:
        with self.lock:
            stamp = self._env_stamp()
            if self._credentials is None or stamp == self._env_seen:
                return
            try:
                loaded = dict(self._credentials())
            except Exception:  # noqa: BLE001 - read again at the next prompt; what is loaded stays
                return  # a file just saved can be locked, and a runtime config being edited may not load
            self._env_seen = stamp
            for name in set(self._env_loaded) - set(loaded):
                os.environ.pop(name, None)
            os.environ.update(loaded)
            self._env_loaded = loaded

    def recall(self, request: dict[str, Any], *, received: float | None = None
               ) -> tuple[dict[str, Any], Callable[[], None]]:
        """One prompt's recall, as its hook would have recalled it, by the kept handler (``KeptRecaller``) or, while
        another recall holds that, by one of its own that the caller closes once the answer is out.  Its time counts
        from the request's arrival, loading the handler included."""
        received = time.monotonic() if received is None else received
        self._refresh_credentials()
        kept = self.kept(request["payload"], request["current_refs"], request["gaps"], request["remaining"],
                         received=received)
        if kept is not None:
            result, diagnostics = kept
            return {"result": result, "diagnostics": diagnostics}, _nothing
        handler = self._handler()
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
            # A hook removed the name of a server that did not prove itself in time.  It names itself again once
            # none of its recalls is stuck and its own check comes back within ``SELF_CHECK_SECONDS``, counted by the
            # clock: from inside the server, the time its own busy threads held Python's lock did not count against
            # the socket's, and a server hooks could not reach in time named itself again (reviews of rc11).
            if self.path.exists() or self._stuck():
                continue
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=CONNECT_SECONDS)
            started = time.monotonic()
            try:
                connection.connect()
                answers = _hello(connection, self.token) == "ok"
            except OSError:
                answers = False
            finally:
                connection.close()
            if answers and time.monotonic() - started <= SELF_CHECK_SECONDS and not self._stopped.is_set():
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
        self.kept.close()


def serve(home: Path | str, host: str, *, env_file: Path | None = None, runtime_config: Path | None = None,
          credentials: Callable[[], dict[str, str]] | None = None, warm: bool = True) -> HookEndpoint | None:
    """Answer this entry's prompt recalls from this process until it exits; None when that cannot start.  ``warm``
    readies the kept handler's vector store now (``KeptRecaller.warm``)."""
    try:
        endpoint = HookEndpoint(home, host, env_file=env_file, runtime_config=runtime_config, credentials=credentials)
    except Exception:  # noqa: BLE001 - the MCP server starts whatever this does; its hooks recall themselves
        return None
    try:
        endpoint.start()
    except Exception:  # noqa: BLE001 - as above
        endpoint.stop()
        return None
    if warm:
        endpoint.kept.warm()
    return endpoint

