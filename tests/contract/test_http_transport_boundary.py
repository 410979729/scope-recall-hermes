"""Offline transport checks; sockets are restricted to synthetic loopback HTTP."""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPResponse
from io import BytesIO
from pathlib import Path
from threading import Condition, Event, Lock, get_ident
from types import SimpleNamespace

import pytest
from scope_recall.runtime import models


class _Socket:
    def __init__(self, wire: bytes) -> None:
        self._wire = BytesIO(wire)
        self.timeouts: list[float | None] = []

    def makefile(self, *_args):
        return self._wire

    def settimeout(self, value):
        self.timeouts.append(value)

    def close(self):
        pass


def _response(wire: bytes) -> HTTPResponse:
    response = HTTPResponse(_Socket(wire))
    response.begin()
    return response


def test_http_response_parser_consumes_chunked_framing_without_read1() -> None:
    response = _response(
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: keep-alive\r\n\r\n"
        b"5\r\nhello\r\n6\r\n world\r\n0\r\nX-Trailer: yes\r\n\r\n"
    )
    assert response.read() == b"hello world"


def test_http_response_parser_honors_content_length_on_keepalive() -> None:
    response = _response(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: keep-alive\r\n\r\nhelloEXTRA-WIRE-BYTES")
    assert response.read() == b"hello"


def _write_worker(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8", newline="\n")


def _worker_result(*, ok: bool, status: int | None = 200, body: bytes = b"", error: str | None = None) -> str:
    result: dict[str, object] = {
        "ok": ok,
        "status": status,
        "body_b64": base64.b64encode(body).decode("ascii"),
    }
    if error is not None:
        result["error"] = error
    return json.dumps(result, separators=(",", ":"))


def _post(monkeypatch, worker: Path, *, timeout: float = 2.0, max_response_bytes: int = 1024):
    monkeypatch.setattr(models, "_HTTP_WORKER_PATH", worker)
    return models.HttpsTransport().post(
        "https://synthetic.invalid/response",
        body=b"body",
        headers={"X-Synthetic": "ok"},
        timeout_seconds=timeout,
        max_response_bytes=max_response_bytes,
    )


def test_parent_worker_protocol_success_and_error(tmp_path, monkeypatch) -> None:
    success = tmp_path / "success_worker.py"
    _write_worker(
        success,
        f"import sys\nsys.stdin.buffer.read()\nsys.stdout.write({_worker_result(ok=True, body=b'response')!r})\n",
    )
    assert _post(monkeypatch, success) == (200, b"response")

    failure = tmp_path / "failure_worker.py"
    _write_worker(
        failure,
        f"import sys\nsys.stdin.buffer.read()\nsys.stdout.write({_worker_result(ok=False, error='http_protocol')!r})\n",
    )
    with pytest.raises(models.AuxiliaryModelError) as exc:
        _post(monkeypatch, failure)
    assert exc.value.error_type == "http_protocol"

    oversized = tmp_path / "oversized_worker.py"
    _write_worker(
        oversized,
        f"import sys\nsys.stdin.buffer.read()\nsys.stdout.write({_worker_result(ok=True, body=b'1234')!r})\n",
    )
    with pytest.raises(models.AuxiliaryModelError) as oversize_exc:
        _post(monkeypatch, oversized, max_response_bytes=3)
    assert oversize_exc.value.error_type == "response_limit"


def test_parent_worker_timeout_kills_owned_process(tmp_path, monkeypatch) -> None:
    marker = tmp_path / "completed.txt"
    timeout_worker = tmp_path / "timeout_worker.py"
    _write_worker(
        timeout_worker,
        "import pathlib, sys, time\n"
        "sys.stdin.buffer.read()\n"
        "time.sleep(2.0)\n"
        f"pathlib.Path({str(marker)!r}).write_text('completed')\n",
    )
    started = time.perf_counter()
    with pytest.raises(models.AuxiliaryModelError) as exc:
        _post(monkeypatch, timeout_worker, timeout=0.15)
    elapsed = time.perf_counter() - started
    assert exc.value.error_type == "timeout"
    assert elapsed < 1.5
    time.sleep(0.1)
    assert not marker.exists()


def test_worker_path_is_fixed_to_runtime_helper() -> None:
    expected = (Path(models.__file__).resolve().parents[1] / "runtime" / "_http_worker.py").resolve()
    assert models._HTTP_WORKER_PATH == expected
    assert expected.is_file()


@pytest.fixture
def persistent_responses():
    """Optional response callbacks let a test hold real HTTP requests at the server."""
    return {}


@pytest.fixture
def persistent_transport(tmp_path, monkeypatch, persistent_responses):
    """Real helper/HTTP framing; loopback replaces TLS, no provider or key."""
    import http.server
    import threading

    connections = set()
    slow_started = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            connections.add(self.client_address)
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if body == b"slow":
                slow_started.set()
                time.sleep(0.5)
            response = (
                json.dumps(
                    {"embeddings": [{"values": [0.01] * 3072}], "usageMetadata": {"promptTokenCount": 10}}
                ).encode()
                if body.startswith(b"{")
                else b"ok"
            )
            if body in persistent_responses:
                response = persistent_responses[body]()
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            try:
                self.wfile.write(response)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    worker = tmp_path / "loopback_worker.py"
    worker.write_text(
        "import importlib.util, http.client\n"
        f"s=importlib.util.spec_from_file_location('worker', {str(models._HTTP_WORKER_PATH)!r})\n"
        "w=importlib.util.module_from_spec(s); s.loader.exec_module(w)\n"
        f"w._open_https_connection=lambda *a, **k: http.client.HTTPConnection('127.0.0.1', {server.server_port})\n"
        "raise SystemExit(w.main())\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(models, "_HTTP_WORKER_PATH", worker)
    transport = models.HttpsTransport(persistent=True)
    try:
        yield transport, connections, slow_started
    finally:
        transport.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _persistent_post(transport, body=b"request", *, timeout=2, limit=1024):
    return transport.post(
        "https://synthetic.invalid/embed",
        body=body,
        headers={"X-Synthetic": "not-a-credential"},
        timeout_seconds=timeout,
        max_response_bytes=limit,
    )


class _WaitingCondition(Condition):
    """Expose each caller's first wait while preserving the lock handoff used by the pool.

    ``wait_for`` waits again for the same caller when a timed wait ends before its clock reaches the deadline,
    which Windows timer resolution allows, so the events belong to calling threads rather than to calls.
    """

    def __init__(self, count=1, *, first_resumes_first=False):
        super().__init__()
        self.waiting = [Event() for _ in range(count)]
        self.first_resumed = Event()
        self.first_resumes_first = first_resumes_first
        self._callers = {}

    def wait(self, timeout=None):
        # Every caller holds this condition's lock here, so the caller table needs no lock of its own.
        index = self._callers.setdefault(get_ident(), len(self._callers))
        self.waiting[index].set()
        result = super().wait(timeout)
        if index == 0:
            self.first_resumed.set()
        elif self.first_resumes_first:
            # The expired waiter must inspect its deadline before the live one
            # can borrow; Condition.wait returns with this lock held.
            self.release()
            try:
                assert self.first_resumed.wait(2)
            finally:
                self.acquire()
        return result


@pytest.fixture
def held_pool(monkeypatch):
    """Hold one synthetic request and record the budgets passed to its transport."""
    entered, release = Event(), Event()
    budgets = {}

    def post(_transport, _url, *, body, timeout_seconds, **_kwargs):
        budgets[body] = timeout_seconds
        if body == b"held":
            entered.set()
            assert release.wait(5)
        return 200, b"ok"

    monkeypatch.setattr(models.HttpsTransport, "post", post)
    pool = models.HttpsTransportPool(size=1)
    try:
        yield pool, entered, release, budgets
    finally:
        release.set()
        pool.close()


def test_returned_connection_reaches_a_live_waiter_after_an_expired_waiter(held_pool, monkeypatch):
    """Expiry of the first notified caller cannot strand an idle connection."""
    pool, entered, release, _ = held_pool
    condition = _WaitingCondition(2, first_resumes_first=True)
    pool._condition = condition
    clock = [100.0]
    monkeypatch.setattr(models, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    with ThreadPoolExecutor(max_workers=3) as executor:
        active = executor.submit(_persistent_post, pool, b"held", timeout=30)
        try:
            assert entered.wait(2)
            expired = executor.submit(_persistent_post, pool, b"expired", timeout=10)
            assert condition.waiting[0].wait(2)
            live = executor.submit(_persistent_post, pool, b"live", timeout=30)
            assert condition.waiting[1].wait(2)
            with condition:
                clock[0] += 11
                release.set()
            assert active.result(timeout=2) == (200, b"ok")
            with pytest.raises(models.AuxiliaryModelError, match="timeout"):
                expired.result(timeout=2)
            assert live.result(timeout=2) == (200, b"ok")
        finally:
            release.set()
            pool.close()


def test_queued_request_passes_only_its_remaining_budget_to_the_transport(held_pool, monkeypatch):
    """Time spent waiting for a pooled connection belongs to the same request deadline."""
    pool, entered, release, budgets = held_pool
    condition = _WaitingCondition()
    pool._condition = condition
    clock = [100.0]
    monkeypatch.setattr(models, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    with ThreadPoolExecutor(max_workers=2) as executor:
        active = executor.submit(_persistent_post, pool, b"held", timeout=20)
        try:
            assert entered.wait(2)
            queued = executor.submit(_persistent_post, pool, b"queued", timeout=10)
            assert condition.waiting[0].wait(2)
            with condition:
                clock[0] += 4
                release.set()
            assert active.result(timeout=2) == queued.result(timeout=2) == (200, b"ok")
            assert budgets[b"queued"] == pytest.approx(6)
        finally:
            release.set()
            pool.close()


def test_pool_close_attempts_every_transport_before_raising_the_first_error(monkeypatch):
    """One broken close cannot keep the remaining helpers alive."""
    pool = models.HttpsTransportPool(size=3)
    errors = [RuntimeError("close1"), ValueError("close2")]
    closed = []

    def close(transport):
        closed.append(transport)
        if len(closed) <= len(errors):
            raise errors[len(closed) - 1]

    monkeypatch.setattr(models.HttpsTransport, "close", close)
    with pytest.raises(RuntimeError) as exc:
        pool.close()
    assert exc.value is errors[0]
    assert closed == list(pool._transports)
    pool.close()
    assert len(closed) == 3


def test_session_close_during_io_thread_start_never_joins_an_unstarted_thread(persistent_transport, monkeypatch):
    """Pause before Thread.start so shutdown meets the exact publication boundary."""
    from threading import Thread

    from scope_recall.runtime import http_session

    transport, _, _ = persistent_transport
    starting, release, closing = Event(), Event(), Event()
    original_start = Thread.start
    original_discard = http_session.HttpWorkerSession.discard

    def start(thread):
        if thread.daemon:
            starting.set()
            assert release.wait(5)
        original_start(thread)

    def discard(session):
        closing.set()
        return original_discard(session)

    # Start executor workers before intercepting only the session's daemon I/O thread.
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert executor.submit(lambda: None).result(timeout=2) is None
        monkeypatch.setattr(Thread, "start", start)
        monkeypatch.setattr(http_session.HttpWorkerSession, "discard", discard)
        request = executor.submit(_persistent_post, transport, timeout=5)
        try:
            assert starting.wait(2)
            assert transport._session._io_thread is None, "only started threads may be published for cleanup"
            process = transport._session._process
            closing.clear()
            shutdown = executor.submit(transport.close)
            assert closing.wait(2)
        finally:
            release.set()
        shutdown.result(timeout=2)
        with pytest.raises(models.AuxiliaryModelError):
            request.result(timeout=2)
    assert process.poll() is not None


def test_query_helper_reuses_process_and_connection_and_recovers(persistent_transport):
    transport, connections, _ = persistent_transport
    assert _persistent_post(transport) == (200, b"ok")
    process = transport._session._process
    assert _persistent_post(transport) == (200, b"ok")
    assert transport._session._process is process
    assert len(connections) == 1
    with pytest.raises(models.AuxiliaryModelError, match="response_limit"):
        _persistent_post(transport, limit=1)
    assert process.poll() is not None
    assert _persistent_post(transport) == (200, b"ok")
    process = transport._session._process
    with pytest.raises(models.AuxiliaryModelError, match="timeout"):
        _persistent_post(transport, b"slow", timeout=0.1)
    assert process.poll() is not None
    assert _persistent_post(transport) == (200, b"ok")
    process = transport._session._process
    transport.close()
    assert process.poll() is not None


def test_query_adapter_uses_persistent_helper_but_source_does_not(persistent_transport, tmp_path, monkeypatch):
    from scope_recall.runtime.auxiliary import build_auxiliary_runtime
    from test_runtime_auxiliary import _runtime_config, _source

    _, connections, _ = persistent_transport
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "synthetic-local-value")
    config, _, _ = _runtime_config(tmp_path)
    runtime = build_auxiliary_runtime(config)
    adapter = runtime.query_embedding
    # Never let a stale imported model class escape the loopback harness.
    query_transports = adapter._query_transport._transports
    assert all(port._post.__globals__["_HTTP_WORKER_PATH"] == models._HTTP_WORKER_PATH for port in query_transports)
    assert adapter._transport._post.__globals__["_HTTP_WORKER_PATH"] == models._HTTP_WORKER_PATH
    try:
        first = adapter.embed_query("TEST first query", remaining_seconds=2)
        process = query_transports[-1]._session._process
        second = adapter.embed_query("TEST second query", remaining_seconds=2)
        assert len(first) == len(second) == 3072
        assert query_transports[-1]._session._process is process
        assert len(connections) == 1
        assert len(adapter.embed_source(_source("TEST source"), remaining_seconds=2)) == 3072
        assert adapter._transport._session is None and len(connections) == 2
    finally:
        runtime.close()
    assert process.poll() is not None


def test_query_pool_returns_a_connection_after_a_failed_request(persistent_transport):
    """A failed exchange discards its helper but leaves the bounded slot usable for the next request."""
    pool = models.HttpsTransportPool(size=1)
    try:
        with pytest.raises(models.AuxiliaryModelError, match="response_limit"):
            _persistent_post(pool, limit=1)
        assert _persistent_post(pool) == (200, b"ok")
        process = pool._transports[0]._session._process
    finally:
        pool.close()
    assert process.poll() is not None


def test_query_pool_close_wakes_waiters_and_closes_every_transport(persistent_transport, persistent_responses):
    """Shutdown cancels real HTTP exchanges and reaps every helper, including when another caller is queued."""
    _, connections, _ = persistent_transport
    pool = models.HttpsTransportPool(size=3)
    entered, release = Event(), Event()
    condition = _WaitingCondition(2)
    pool._condition = condition
    lock = Lock()
    active = 0

    def respond():
        nonlocal active
        with lock:
            active += 1
            if active == 3:
                entered.set()
        assert release.wait(5)
        return b"ok"

    persistent_responses[b"held"] = respond
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            borrowed = [executor.submit(_persistent_post, pool, b"held", timeout=5) for _ in range(3)]
            try:
                assert entered.wait(3), "three requests must reach the server before any response is released"
                processes = [port._session._process for port in pool._transports]
                assert len({process.pid for process in processes}) == 3
                with pytest.raises(models.AuxiliaryModelError, match="timeout"):
                    _persistent_post(pool, timeout=0.05)
                assert len(connections) == 3
                waiter = executor.submit(_persistent_post, pool, timeout=5)
                assert condition.waiting[1].wait(1)
                # Acquiring the condition proves wait() released it after the event.
                with condition:
                    assert not pool._available
                pool.close()
                with pytest.raises(models.AuxiliaryModelError, match="transport_unavailable"):
                    waiter.result(timeout=1)
                for request in borrowed:
                    with pytest.raises(models.AuxiliaryModelError):
                        request.result(timeout=2)
                assert all(process.poll() is not None for process in processes)
                assert all(port._session._closed for port in pool._transports)
            finally:
                release.set()
    finally:
        pool.close()
    with pytest.raises(models.AuxiliaryModelError, match="transport_unavailable"):
        _persistent_post(pool)


def test_query_helper_close_cancels_active_request(persistent_transport):
    from concurrent.futures import ThreadPoolExecutor

    transport, _, slow_started = persistent_transport
    assert _persistent_post(transport) == (200, b"ok")
    process = transport._session._process
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(_persistent_post, transport, b"slow")
        assert slow_started.wait(2)
        transport.close()
        with pytest.raises(models.AuxiliaryModelError):
            pending.result(timeout=2)
    assert process.poll() is not None


def _closing_transport(tmp_path, monkeypatch, *, idle_reuse=None, close=True):
    """A loopback server that closes each connection after its answer without saying so (``close``), as a server does
    to one left idle past its own keep-alive time, or keeps it open; the helper's reuse limit can be set."""
    import http.server
    import threading

    connections = []

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            connections.append(self.client_address)
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            self.wfile.flush()
            self.close_connection = close

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    tmp_path.mkdir(parents=True, exist_ok=True)
    worker = tmp_path / "closing_worker.py"
    # The helper itself, not whatever an earlier call patched ``_HTTP_WORKER_PATH`` to.
    helper = Path(models.__file__).resolve().parents[1] / "runtime" / "_http_worker.py"
    worker.write_text(
        "import importlib.util, http.client\n"
        f"s=importlib.util.spec_from_file_location('worker', {str(helper)!r})\n"
        "w=importlib.util.module_from_spec(s); s.loader.exec_module(w)\n"
        f"w._open_https_connection=lambda *a, **k: http.client.HTTPConnection('127.0.0.1', {server.server_port})\n"
        + (f"w.IDLE_REUSE_SECONDS={idle_reuse!r}\n" if idle_reuse is not None else "")
        + "raise SystemExit(w.main())\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(models, "_HTTP_WORKER_PATH", worker)
    return models.HttpsTransport(persistent=True), connections, server, thread


def test_the_helper_sends_nothing_on_a_connection_its_server_closed(tmp_path, monkeypatch):
    """A worker kept between a server's prompts does not send the next request on a connection the provider closed
    while it sat idle, which would fail at once and leave the recall without its vector search."""
    transport, connections, server, thread = _closing_transport(tmp_path, monkeypatch)
    try:
        assert _persistent_post(transport) == (200, b"ok")
        time.sleep(0.3)
        assert _persistent_post(transport) == (200, b"ok")
        assert len(set(connections)) == 2
    finally:
        transport.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_a_plaintext_request_carries_no_credential(tmp_path, monkeypatch):
    """A loopback model server needs no key, but the route's ``credential_env`` is still required, so the key is
    loaded and would otherwise ride the request to a plaintext socket.  Permission to use plain HTTP is not
    permission to send the credential over it."""
    import http.server
    import threading

    seen: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            seen.update(
                auth=self.headers.get("Authorization"),
                goog=self.headers.get("x-goog-api-key"),
                cookie=self.headers.get("Cookie"),
                content_type=self.headers.get("Content-Type"),
            )
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            self.wfile.flush()
            self.close_connection = True

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    tmp_path.mkdir(parents=True, exist_ok=True)
    worker = tmp_path / "plaintext_worker.py"
    # The helper itself, not whatever an earlier call patched ``_HTTP_WORKER_PATH`` to.
    helper = Path(models.__file__).resolve().parents[1] / "runtime" / "_http_worker.py"
    worker.write_text(
        "import importlib.util\n"
        f"s=importlib.util.spec_from_file_location('worker', {str(helper)!r})\n"
        "w=importlib.util.module_from_spec(s); s.loader.exec_module(w)\n"
        "raise SystemExit(w.main())\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(models, "_HTTP_WORKER_PATH", worker)
    try:
        assert models.HttpsTransport().post(
            f"http://127.0.0.1:{server.server_port}/v1/embeddings",
            body=b"body",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer sk-must-not-cross-a-plaintext-socket",
                "x-goog-api-key": "sk-must-not-cross-a-plaintext-socket",
                "Cookie": "session=secret",
            },
            timeout_seconds=2.0,
            max_response_bytes=1024,
        ) == (200, b"ok")
        assert seen["auth"] is None, "a bearer token must not cross a plaintext socket"
        assert seen["goog"] is None
        assert seen["cookie"] is None
        assert seen["content_type"] == "application/json", "what the server needs still travels"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_plain_http_is_refused_for_a_host_that_is_not_this_machine(tmp_path, monkeypatch):
    """Loopback HTTP is the carve-out; anywhere else is not, and the worker says so
    without starting a connection."""
    import http.server
    import threading

    reached = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            reached.append(1)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    tmp_path.mkdir(parents=True, exist_ok=True)
    worker = tmp_path / "refusing_worker.py"
    helper = Path(models.__file__).resolve().parents[1] / "runtime" / "_http_worker.py"
    worker.write_text(
        "import importlib.util\n"
        f"s=importlib.util.spec_from_file_location('worker', {str(helper)!r})\n"
        "w=importlib.util.module_from_spec(s); s.loader.exec_module(w)\n"
        "raise SystemExit(w.main())\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(models, "_HTTP_WORKER_PATH", worker)
    try:
        # A literal address that is not loopback, pointed at the live server: only
        # the scheme/host rule can refuse it, so this proves the rule and not DNS.
        with pytest.raises(models.AuxiliaryModelError) as refused:
            models.HttpsTransport().post(
                f"http://192.0.2.1:{server.server_port}/v1/embeddings",
                body=b"body",
                headers={"Content-Type": "application/json"},
                timeout_seconds=2.0,
                max_response_bytes=1024,
            )
        assert refused.value.error_type == "endpoint_invalid"
        assert reached == [], "the request never left for a host the policy refuses"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_the_helper_does_not_reuse_a_connection_idle_past_its_limit(tmp_path, monkeypatch):
    """A connection idle past ``IDLE_REUSE_SECONDS`` is not used again even when nothing says it was closed: a proxy
    or a NAT drops it silently.  The same server keeps one connection within the limit."""
    from scope_recall.runtime import _http_worker as worker

    assert worker.IDLE_REUSE_SECONDS == 30.0
    for limit, expected in ((None, 1), (0.0, 2)):
        transport, connections, server, thread = _closing_transport(
            tmp_path / f"TEST-{expected}", monkeypatch, idle_reuse=limit, close=False
        )
        try:
            assert _persistent_post(transport) == (200, b"ok")
            time.sleep(0.1)  # past a tick of Windows' 15.6 ms clock: an idle time of 0 is not past a limit of 0
            assert _persistent_post(transport) == (200, b"ok")
            assert len(set(connections)) == expected, (limit, connections)
        finally:
            transport.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


@pytest.mark.parametrize("batch", [False, True])
def test_concurrent_query_embeddings_use_bounded_independent_connections(tmp_path, monkeypatch, batch):
    """Only the provider is simulated; each persistent transport keeps its real serialization lock."""
    from scope_recall.runtime.auxiliary import build_auxiliary_runtime
    from test_runtime_auxiliary import _runtime_config

    entered = set()
    lock = Lock()
    ready, release = Event(), Event()

    def respond(transport, url, *, body, headers, timeout_seconds, max_response_bytes):
        with lock:
            entered.add(transport)
            if len(entered) == 3:
                ready.set()
        assert release.wait(5), "the synthetic provider must be released"
        count = len(json.loads(body)["requests"])
        return 200, json.dumps(
            {"embeddings": [{"values": [0.01] * 3072}] * count, "usageMetadata": {"promptTokenCount": 10}}
        ).encode()

    monkeypatch.setattr(models.HttpsTransport, "_post", respond)
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "synthetic-local-value")
    config, _, _ = _runtime_config(tmp_path)
    runtime = build_auxiliary_runtime(config)
    adapter = runtime.query_embedding
    query = adapter.embed_queries if batch else adapter.embed_query
    text = ["TEST first", "TEST second"] if batch else "TEST query"
    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            requests = [executor.submit(query, text, remaining_seconds=5) for _ in range(3)]
            try:
                assert ready.wait(1), "concurrent recalls must reach independent connections"
                with pytest.raises(models.AuxiliaryModelError, match="timeout"):
                    query(text, remaining_seconds=0.05)
                assert len(entered) == 3, "a saturated pool must not open an extra connection"
            finally:
                release.set()
            assert all(len(request.result(timeout=2)) == (2 if batch else 3072) for request in requests)
        assert len(query(text, remaining_seconds=2)) == (2 if batch else 3072)
        assert len(entered) == 3, "later requests reuse a returned warm connection"
    finally:
        runtime.close()
