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
#: What a server's start may spend warming its query embedding.  The warming holds the kept handler, and a provider or
#: proxy that took the connection and hung kept every recall off it for the whole ``WARM_SECONDS`` (review of 3.6.0rc1).
EMBEDDING_WARM_SECONDS = 10.0
#: A kept handler left this long without a recall searches its vector store once more, off any prompt's time, and again
#: after each such stretch.  The helper keeps the index in its memory, and a search touches the codes of every row its
#: filter keeps (the warm search filters as a recall does: ``runtime/instance.py warm_vector_store``): left alone, the
#: OS gave those pages to other work, and the first recall after an idle hour searched past its time.  This machine's
#: Claude Code lost the vector search on 2 of the 4 prompts it had after an idle hour (2026-10-02), and on none of the 5
#: it had while another process searched the same index every 10 minutes.
KEEP_WARM_IDLE_SECONDS = 600.0
#: How often a server looks whether its kept handler has been idle that long.
KEEP_WARM_CHECK_SECONDS = 60.0
#: What closing waits for a recall that holds the kept handler (a prompt's recall ends within its hook's time).
CLOSE_WAIT_SECONDS = 10.0
#: Recalls one server runs at once; a hook past that recalls itself.
MAX_CONCURRENT = 8
#: How often a server looks for its own name, and puts it back when a hook removed it: a busy server that did not
#: prove itself in time was left out for 30 s (review of rc11).
ADVERTISE_SECONDS = 2.0
#: Minutes a client's resident recall server (``resident_entry``) stays up without a recall when the entry's runtime
#: config names none (``resident_recall_minutes``).  WorkBuddy starts the entry's MCP server with each conversation's
#: agent process, so a prompt that started one met a server still opening its vector store: a cold server answered
#: with its vector search 12.7 s after its start (measured 2026-10-03), past the prompt hook's 6 s.  Claude Code and
#: Codex keep their server for as long as the client runs, and keep none.
RESIDENT_DEFAULT_MINUTES = {"workbuddy": 120, "dsh": 120}
#: How often a client starts a resident server when it finds none: a start warms for several seconds, and the next
#: prompt's hook would otherwise start another meanwhile (the second gives way, ``resident_entry``).
RESIDENT_START_EVERY_SECONDS = 60.0
#: How often a client's live MCP server looks for the resident server, marks the client in use and starts one when none
#: runs (``keep_resident``): within the shortest idle end, a minute.
RESIDENT_KEEP_SECONDS = 30.0
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
        if self.path != "/recall" or not _proven(
            self.headers.get(_PROOF), endpoint.token, "recall", nonce, hashlib.sha256(body).hexdigest()
        ):
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
                answer_body = {
                    "result": {},
                    "diagnostics": {"last_reason": "recall_exception", "recall_error_detail": detail[:64]},
                }
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
    if (
        type(payload) is not dict
        or type(refs) is not list
        or len(refs) > 64
        or not all(type(ref) is str and len(ref) <= 200 for ref in refs)
        or type(gaps) is not list
        or len(gaps) > 64
        or not all(type(gap) is str and len(gap) <= 200 for gap in gaps)
        or type(remaining) not in (int, float)
        or not 0.0 <= remaining <= 10.0
    ):
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

    def __call__(
        self, payload: dict[str, Any], current_refs: tuple[str, ...], gaps: tuple[str, ...], budget: float
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        from ..._version import __version__
        from ...runtime.process_probe import probe_process

        started = time.monotonic()
        self.outcome = "none"
        request = {"payload": payload, "current_refs": list(current_refs), "gaps": list(gaps)}
        tried = 0
        named = []
        for path in _named(endpoints(self.home)):
            try:
                info = json.loads(path.read_text(encoding="utf-8"))
                port, token, pid = int(info["port"]), str(info["token"]), int(info["pid"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            named.append((path, info, port, token, pid))
        # A resident server first, newest first within each kind (the sort keeps the order): it stays warm, while a
        # server the client just started with a conversation may still be opening its vector store.
        named.sort(key=lambda item: item[1].get("resident") is not True)
        for path, info, port, token, pid in named:
            if tried >= MAX_TRIED or time.monotonic() - started > FIND_SECONDS:
                break
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
            body = json.dumps(
                {**request, "remaining": min(10.0, wait - ANSWER_MARGIN_SECONDS)}, ensure_ascii=True
            ).encode("ascii")
            if len(body) > MAX_REQUEST_BYTES:
                return "none", None
            nonce = secrets.token_hex(16)
            headers = {
                _NONCE: nonce,
                "Content-Type": "application/json",
                _PROOF: _proof(token, "recall", nonce, hashlib.sha256(body).hexdigest()),
            }
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
            if response.status != 200 or not _proven(
                response.getheader(_PROOF), token, "answer", nonce, hashlib.sha256(data).hexdigest()
            ):
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
        #: When a recall or a warming last searched the handler's vector store (``_keep_warm``).
        self._used = time.monotonic()
        self._stopped = threading.Event()
        self._keeping = False
        #: A keep-warm search holds the handler: ``close`` does not wait for it, and it closes the handler itself.
        self._searching = False

    def warm(self, seconds: float = WARM_SECONDS) -> None:
        """Make the handler and warm its vector store in the background, when the server starts, then keep it warm
        (``_keep_warm``) until the recaller is closed.

        Made at the first prompt, the handler attached its runtime, started the vector helper, opened the table and
        read the index inside that prompt's recall, and the first prompt after every start recalled by words alone:
        for Claude Code that is every session.  A recall that comes meanwhile waits for this (``__call__``) instead
        of making a second handler.  It writes nothing."""
        done = threading.Event()
        self._warming = done
        keep = not self._keeping
        self._keeping = True

        def start() -> None:
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
                    # The query embedding route too, at the start only: warmed by the store alone, a cold server's
                    # first recalls lost their vector search to the embedding's time (measured 2026-10-03).
                    warm_embedding = getattr(self._handler, "warm_embedding", None)
                    if callable(warm_embedding):
                        try:
                            warm_embedding(min(seconds, EMBEDDING_WARM_SECONDS))
                        except Exception:  # noqa: BLE001 - a provider down now is the first recall's to report
                            pass
                    if self._closed or not getattr(self._handler, "runtime_ready", False):
                        self._discard(later=True)
            except Exception:  # noqa: BLE001 - a handler that cannot be made now is made by the first recall
                pass
            finally:
                self._used = time.monotonic()
                done.set()
                if self._closed:
                    # A close during the warming did not wait for it (``close``): the handler it made is closed here.
                    with self._lock:
                        self._discard(later=True)

        def run() -> None:
            start()
            if keep:
                self._keep_warm(seconds)

        threading.Thread(target=run, name="scope-recall-kept-warm", daemon=True).start()

    def _keep_warm(self, seconds: float) -> None:
        """Search the kept handler's vector store again after each ``KEEP_WARM_IDLE_SECONDS`` no recall searched it,
        until the recaller is closed.  A moment a recall holds the handler is skipped; no handler is made for it; it
        writes nothing.  A search that failed is tried once more at once: one that found the vector helper gone closed
        the store, and the second opens it again here, not inside the next prompt's recall.  Like the start's warming,
        a search is not waited for by ``close``, and closes the handler itself when the recaller was closed."""
        while not self._stopped.wait(KEEP_WARM_CHECK_SECONDS):
            if time.monotonic() - self._used < KEEP_WARM_IDLE_SECONDS or not self._lock.acquire(blocking=False):
                continue
            self._searching = True
            try:
                for _attempt in range(2):
                    if self._closed or self._handler is None:
                        break
                    try:
                        self._handler.warm_vectors(seconds)
                        break
                    except Exception:  # noqa: BLE001 - tried once more, then left to the next recall as before
                        continue
                self._used = time.monotonic()
            finally:
                # Cleared before the recaller is looked at, both under the lock: a close that saw the search still
                # running left the handler to it, and one that did not finds the lock free or the handler closed.
                self._searching = False
                if self._closed:
                    self._discard(later=True)
                self._lock.release()

    def __call__(
        self,
        payload: dict[str, Any],
        current_refs: tuple[str, ...],
        gaps: tuple[str, ...],
        budget: float,
        *,
        received: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
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
                result = handler.resident_recall_for(
                    payload, current_refs, gaps, max(0.0, budget - (time.monotonic() - received))
                )
            except BaseException:
                self._discard(later=True)
                raise
            diagnostics = dataclasses.asdict(handler.diagnostics)
            diagnostics["capability_gaps"] = list(diagnostics.get("capability_gaps") or ())
            if diagnostics.get("recall_vectors") is True:
                # Only a recall that searched the index puts off the next keep-warm search: one whose query embedding
                # failed (a provider or proxy outage) never reached it, and an hour of such prompts left it cold.
                self._used = time.monotonic()
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
        """Close the kept handler, once a recall that holds it is done; later recalls get None.  A warming or a
        keep-warm search that holds it (up to ``WARM_SECONDS``) is not waited for: it sees the recaller closed and
        closes its handler itself."""
        self._closed = True
        self._stopped.set()
        warming = self._warming
        if (warming is not None and not warming.is_set()) or self._searching:
            return
        if not self._lock.acquire(timeout=CLOSE_WAIT_SECONDS):
            return
        try:
            self._discard()
        finally:
            self._lock.release()


class HookEndpoint:
    """The MCP server's side: a 127.0.0.1 HTTP server in a daemon thread, and the file that names it."""

    def __init__(
        self,
        home: Path | str,
        host: str,
        *,
        env_file: Path | None = None,
        runtime_config: Path | None = None,
        credentials: Callable[[], dict[str, str]] | None = None,
        resident: bool = False,
    ) -> None:
        self.home = Path(home)
        self.host = host
        #: Said in the server's name: a resident server (``resident_entry``) runs apart from the MCP server of a
        #: conversation (though WorkBuddy's agent still ends it with that conversation's process), and hooks ask it
        #: before a server the client started for a conversation (``Recaller``).
        self.resident = resident
        #: When a prompt's recall last came in, for a resident server's idle end; keep-warm searches do not count.
        self.last_used = time.monotonic()
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

    def recall(
        self, request: dict[str, Any], *, received: float | None = None
    ) -> tuple[dict[str, Any], Callable[[], None]]:
        """One prompt's recall, as its hook would have recalled it, by the kept handler (``KeptRecaller``) or, while
        another recall holds that, by one of its own that the caller closes once the answer is out.  Its time counts
        from the request's arrival, loading the handler included."""
        received = time.monotonic() if received is None else received
        # Of two recalls at once, the one received first may come here last.
        self.last_used = max(self.last_used, received)
        self._refresh_credentials()
        kept = self.kept(
            request["payload"], request["current_refs"], request["gaps"], request["remaining"], received=received
        )
        if kept is not None:
            result, diagnostics = kept
            return {"result": result, "diagnostics": diagnostics}, _nothing
        handler = self._handler()
        try:
            remaining = max(0.0, request["remaining"] - (time.monotonic() - received))
            result = handler.resident_recall_for(
                request["payload"], request["current_refs"], request["gaps"], remaining
            )
        except BaseException:
            handler.close()
            raise
        diagnostics = dataclasses.asdict(handler.diagnostics)
        diagnostics["capability_gaps"] = list(diagnostics.get("capability_gaps") or ())
        return {"result": result, "diagnostics": diagnostics}, handler.close

    def _stuck(self) -> bool:
        with self.lock:
            return any(time.monotonic() > due for due in self.inflight.values())

    def stuck_for(self) -> float:
        """How long the oldest recall still running past its time has been so; 0 when none is."""
        with self.lock:
            now = time.monotonic()
            return max((now - due for due in self.inflight.values() if now > due), default=0.0)

    def _advertise(self) -> None:
        from ..._version import __version__
        from ...runtime.process_probe import probe_process

        folder = self.path.parent
        folder.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            for part in (folder, folder.parent, folder.parent.parent):
                part.chmod(0o700)
        record = {
            "host": self.host,
            "port": self.port,
            "token": self.token,
            "pid": os.getpid(),
            "start": probe_process(os.getpid()).start_token,
            "version": __version__,
            "resident": self.resident,
        }
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
        if sys.platform == "win32":
            from ...vector.process_store import share

            # Before anything is served: the kept handler, a handler made for a prompt that comes meanwhile and the
            # tools search one store through one helper.
            share()
        server = _Server(("127.0.0.1", 0), _Handler)
        server.endpoint = self  # type: ignore[attr-defined]
        self._server = server
        self.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, name="scope-recall-recall", daemon=True).start()
        self._advertise()
        threading.Thread(target=self._keep_named, name="scope-recall-recall-name", daemon=True).start()
        atexit.register(self.stop)
        if sys.platform == "win32":
            from ...vector.process_store import prestart

            try:
                prestart()  # for the shared store's helper, its import under way while the server starts
            except OSError:
                pass  # the shared store then starts its own helper when it opens

    def stop(self) -> None:
        if self._stopped.is_set():
            return  # stopped already, as at exit after its owner stopped it: a stuck recall is not waited for twice
        self._stopped.set()
        server, self._server = self._server, None
        _forget(self.path)
        if server is not None:
            server.shutdown()
            server.server_close()
        self.kept.close()


def serve(
    home: Path | str,
    host: str,
    *,
    env_file: Path | None = None,
    runtime_config: Path | None = None,
    credentials: Callable[[], dict[str, str]] | None = None,
    warm: bool = True,
    resident: bool = False,
) -> HookEndpoint | None:
    """Answer this entry's prompt recalls from this process until it exits; None when that cannot start.  ``warm``
    readies the kept handler's vector store now (``KeptRecaller.warm``); ``resident`` names it a resident server."""
    try:
        endpoint = HookEndpoint(
            home, host, env_file=env_file, runtime_config=runtime_config, credentials=credentials, resident=resident
        )
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


def resident_minutes(home: Path | str, host: str) -> int:
    """Minutes the entry's resident recall server stays up without a recall: the entry's runtime config's
    ``resident_recall_minutes``, else the client's default (``RESIDENT_DEFAULT_MINUTES``).  0 when the entry has no
    runtime config (its hooks recall without a vector search, which a resident server would not change) or one that
    cannot be read or names a bad value, so that a broken entry starts no process.  Read from the file as the hook's
    own budget is (``handler._configured_budget``): an entry's config may name only some fields."""
    minutes = configured_minutes(home, host)
    return 0 if minutes is None else minutes


def configured_minutes(home: Path | str, host: str, *, missing: int | None = 0) -> int | None:
    """``resident_minutes``, or None when the entry's files cannot be read just now: a file held for a moment, or a
    runtime config caught half saved.  A running server looks again at its next check instead of ending on it (review
    2 of 3.6.0rc1).  A missing file is ``missing``: 0 for a start, None for a running server, since an editor that
    saves by moving files leaves none for a moment (review 3)."""
    from ...runtime.instance import RESIDENT_RECALL_MINUTES_BOUNDS
    from ...runtime.validation import strict_int
    from .config import load_shared_client

    try:
        path = load_shared_client(Path(home), host).runtime_config_path
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return missing
    except Exception:  # noqa: BLE001 - see above
        return None
    if not isinstance(raw, dict):
        return 0
    value = raw.get("resident_recall_minutes")
    if value is None:
        return RESIDENT_DEFAULT_MINUTES.get(host, 0)
    low, high = RESIDENT_RECALL_MINUTES_BOUNDS
    try:
        strict_int("resident_recall_minutes", value, minimum=low, maximum=high)
    except ValueError:
        return 0
    return value


def resident_lock(home: Path | str, host: str) -> Path:
    """The file a resident server holds for as long as it runs (``resident_entry``): one for each entry and client,
    whatever the version."""
    return endpoints(home) / f"resident-{host}.lock"


def resident_record(home: Path | str, host: str) -> Path:
    """Where the running resident server keeps its process id, start and version, beside its lock.  A hook removes the
    name of a server that did not prove itself in time, and ``resident stop`` saw nothing until it named itself again
    (review of 3.6.0rc1); no hook removes this.  Not a ``.json``: it is not a name hooks ask."""
    return endpoints(home) / f"resident-{host}.pid"


def resident_alive(home: Path | str, host: str) -> Path:
    """What a client's live processes touch while the resident server runs (``ensure_resident``): it ends
    ``resident_recall_minutes`` after the last of these or of its recalls (``resident_entry``)."""
    return endpoints(home) / f"resident-{host}.alive"


def _residents(
    home: Path | str, host: str, *, any_version: bool = False
) -> list[tuple[list[Path], dict[str, Any], bool]]:
    """This entry's live resident servers for ``host``, of this package's version unless ``any_version``: the files
    that say each (its record and its name), what they say, and whether its identity is proven.

    A file whose process is gone, or whose process id another process took since, is removed: its start time differs,
    or cannot be read at all where it could when the file was written (a process of another account or a service, as
    a hook's ``Recaller`` reads it).  One written where no start time can be read (macOS) is kept and marked unproven:
    after a crash its id may belong to any of the user's processes, which a stop must not end (reviews of 3.6.0rc1)."""
    from ..._version import __version__
    from ...runtime.process_probe import probe_process

    said: dict[int, tuple[list[Path], dict[str, Any]]] = {}
    record = resident_record(home, host)
    for path in (record, *_named(endpoints(home))):
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
            pid = int(info["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if info.get("host") != host or (path != record and info.get("resident") is not True):
            continue
        said.setdefault(pid, ([], info))[0].append(path)
    found = []
    for pid, (paths, info) in said.items():
        try:
            state = probe_process(pid)
        except (OSError, ValueError):
            continue
        started = info.get("start")
        if not state.running or (started is not None and state.start_token != started):
            for path in paths:
                _forget(path)
            continue
        if any_version or info.get("version") == __version__:
            found.append((paths, info, started is not None))
    return found


def resident_running(home: Path | str, host: str) -> bool:
    """Whether a resident server of this entry and client runs, of any version: the lock it holds is held.  A name or
    a record can outlive its process, or name an id another process took since; a lock cannot (review of 3.6.0rc1)."""
    from ...core.file_lock import advisory_file_lock

    lock = resident_lock(home, host)
    if not lock.exists():
        return False
    try:
        with advisory_file_lock(lock, timeout_seconds=0):
            return False
    except TimeoutError:
        return True
    except OSError:
        return False


def ensure_resident(
    home: Path | str, host: str, *, minutes: int, env_file: Path | None = None, replace: bool = False
) -> str:
    """Start the entry's resident recall server (``resident_entry``) when none runs; what it did: ``off`` (``minutes``
    is 0), ``running`` (the client is then marked in use: ``resident_alive``), ``running:<version>`` (one of another
    version runs, left unmarked to its own end), ``unstoppable:<version>`` (the same, though the caller would have
    replaced it: its identity cannot be proven, or this account may not end it), ``recent`` (one was started less than
    ``RESIDENT_START_EVERY_SECONDS`` ago and may still be starting), ``upgrading`` (the package is being replaced),
    ``started``, ``replaced:<version>`` (one of another version was stopped and this version's started) or ``failed``.

    ``replace`` is the prompt hook's.  Hooks ask only a server of their own version, and one of another version that
    held the entry's lock (an installation in another venv, a canary, a build from before its self-exit) kept every
    prompt cold for as long as the client ran, while every hook and MCP server marked it in use (review 2 of
    3.6.0rc1).  None marks it now.  The hook stops one it can prove and end, and starts its own; the version an
    entry's hooks run then wins, and hooks of two versions against one entry switch it at most once a minute (review
    3).  A client's MCP server never stops one.  The server is started apart from this process, which the client may
    end at once (WorkBuddy stops a conversation's processes): in a new process group, broken away from the client's job
    where Windows allows it, with no window and no console of its own.  It writes nothing to the store; two started at
    once settle on one."""
    from ..._version import __version__
    from ...core.file_lock import advisory_file_lock

    if minutes <= 0:
        return "off"
    if _upgrading():
        return "upgrading"
    folder = endpoints(home)
    other = None
    if resident_running(home, host):
        others = [
            (info, proven)
            for _paths, info, proven in _residents(home, host, any_version=True)
            if info.get("version") != __version__
        ]
        if not others:
            try:
                resident_alive(home, host).touch()
            except OSError:
                pass  # a recall of it puts its end off as well
            return "running"
        other = str(others[0][0].get("version"))
        if not replace:
            return f"running:{other}"
        # One whose identity is not proven (macOS) is never signalled: left unmarked, it ends at its idle end.
        if not any(proven for _info, proven in others):
            return f"unstoppable:{other}"
    stamp = folder / f"resident-{host}.start"
    switched = folder / f"resident-{host}.replaced"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        # The look at the stamps, the stop, the removal of a stale stamp and the new one under one lock: two starters
        # that both found the stamp stale both started a server (review 2 of 3.6.0rc1).
        with advisory_file_lock(folder / f"resident-{host}.start.lock", timeout_seconds=1.0):
            if other is not None:
                if _recent(switched):
                    return f"running:{other}"
                # A stop that failed (a server this account may not end) said ``replaced`` at every prompt, and started
                # one that gave way each time (review 3 of 3.6.0rc1).
                if not stop_residents(home, host, other_versions=True):
                    return f"unstoppable:{other}"
                switched.write_text(str(os.getpid()), encoding="ascii")
            elif _recent(stamp):
                return "recent"
            stamp.unlink(missing_ok=True)
            # Made only where none is, for a starter of a version without the lock.
            handle = os.open(stamp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(handle, "w", encoding="ascii") as stream:
                stream.write(str(os.getpid()))
    except (TimeoutError, FileExistsError):
        return "recent"
    except OSError:
        return "failed"
    # Through a process that starts the server and ends at once (``--detach``): the server then has no living parent
    # in the client's process tree, which the client may end as a whole (``resident_entry``).
    command = [
        sys.executable,
        "-I",
        "-B",
        "-m",
        "scope_recall.adapters.codex.resident_entry",
        "--home",
        str(Path(home)),
        "--host",
        host,
        "--detach",
    ]
    if env_file is not None:
        command += ["--env-file", str(env_file)]
    if not _start_apart(command, cwd=folder):
        return "failed"
    return f"replaced:{other}" if other is not None else "started"


def _recent(stamp: Path) -> bool:
    """Whether ``stamp`` was written less than ``RESIDENT_START_EVERY_SECONDS`` ago.  One more than that far in the
    future (a clock set back) is stale, not recent: it held off every start until the clock passed it (review of
    3.6.0rc1); one just written can read a little ahead."""
    try:
        age = time.time() - stamp.stat().st_mtime
    except FileNotFoundError:
        return False
    return -RESIDENT_START_EVERY_SECONDS < age < RESIDENT_START_EVERY_SECONDS


def _upgrading() -> bool:
    """Whether ``package-upgrade`` is replacing this environment's package now (its lock in the venv is held): a server
    started meanwhile could import part of either version, and once the new ``_version.py`` was in place it would not
    end (review 2 of 3.6.0rc1).  The client should be quit for an upgrade; its MCP servers' keeping made this
    reachable without a prompt."""
    from ...core.file_lock import advisory_file_lock

    lock = Path(sys.prefix) / ".scope-recall-package-upgrade.lock"
    if not lock.exists():
        return False
    try:
        with advisory_file_lock(lock, timeout_seconds=0):
            return False
    except TimeoutError:
        return True
    except OSError:
        return False


def keep_resident(
    home: Path | str, host: str, *, env_file: Path | None = None, every: float | None = None
) -> threading.Event:
    """For a client's MCP server, for as long as it runs: ``ensure_resident`` now and after every ``every`` seconds
    (``RESIDENT_KEEP_SECONDS``), in a daemon thread; set the event returned to stop.  The resident server then ends
    ``resident_recall_minutes`` after the client's last process, not its last prompt: WorkBuddy keeps a conversation's
    process long after its prompts, and a resident that ended meanwhile left that conversation's next prompt colder
    than the conversation's own server had kept it (review of 3.6.0rc1).  The minutes are read each time, so at 0 this
    starts none.  Nothing it meets ends the MCP server, which serves its tools whatever this does."""
    stopped = threading.Event()
    every = RESIDENT_KEEP_SECONDS if every is None else every

    def run() -> None:
        while True:
            try:
                minutes = resident_minutes(home, host)
                if minutes > 0:
                    ensure_resident(home, host, minutes=minutes, env_file=env_file)
            except Exception as exc:  # noqa: BLE001 - see above
                sys.stderr.write(f"SCOPE_RECALL_RESIDENT_START:{type(exc).__name__}\n")
            if stopped.wait(every):
                return

    threading.Thread(target=run, name="scope-recall-resident-keep", daemon=True).start()
    return stopped


def _start_apart(command: list[str], *, cwd: Path) -> bool:
    """Start ``command`` so that it outlives this process and its parent's job; whether it started."""
    import subprocess

    from ...runtime.worker_launch import detached_creationflags

    quiet = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": str(cwd),
        "close_fds": True,
    }
    if os.name != "nt":
        try:
            subprocess.Popen(command, start_new_session=True, **quiet)
            return True
        except Exception:  # noqa: BLE001 - a start that failed is a later prompt's to try
            return False
    flags = detached_creationflags()
    # A job that does not allow breaking away refuses the flag; started inside it, the server still outlives its
    # parent unless the job ends it with the client.  Whatever refuses the first form, the second is tried.
    for extra in (int(getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)), 0):
        try:
            subprocess.Popen(command, creationflags=flags | extra, **quiet)
            return True
        except Exception:  # noqa: BLE001 - see above
            continue
    return False


def stop_residents(home: Path | str, host: str, *, other_versions: bool = False) -> list[int]:
    """Stop this entry's resident servers for ``host``, for an upgrade or an uninstall: a server runs from the package
    that would be replaced.  With ``other_versions``, only those of another version than this package's (a starting
    server's, ``resident_entry``).  A server writes nothing, so it is ended rather than asked (on Windows ``os.kill``
    terminates the process); its vector and embedding helpers read their requests from it and end when it is gone.
    One whose identity is not proven (``_residents``) is never signalled: it ends itself once its package is replaced
    or its minutes are 0.  Returns the process ids stopped."""
    import signal

    from ..._version import __version__

    stopped = []
    for paths, info, proven in _residents(home, host, any_version=True):
        if not proven or (other_versions and info.get("version") == __version__):
            continue
        pid = int(info["pid"])
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
        for path in paths:
            _forget(path)
        stopped.append(pid)
    return stopped
