"""Private prompt text cannot cross the recall endpoint or embedding boundary."""

from __future__ import annotations

import json
import threading
import urllib.request
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from scope_recall.adapters.clients.handler import CodexHookHandler
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.runtime.vector_port import VectorPort

from tests.host.codex.test_capture_ledger import ledger_client
from tests.host.codex.test_shared_client import store


@pytest.mark.parametrize("route", ["hook", "resident"])
def test_private_spans_never_reach_resident_query_embedding(ledger_client, monkeypatch, route):
    _root, config, session, _record = ledger_client
    received, sent = [], []

    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps({"embedding": [1.0, 0.0]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class QueryEmbedding:
        def embed_query(self, text, *, remaining_seconds):
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/embeddings",
                data=json.dumps({"input": text}).encode(),
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=remaining_seconds) as response:
                return json.load(response)["embedding"]

    vectors = VectorPort(SimpleNamespace(search_scopes=lambda *a, **k: []), QueryEmbedding())
    core = MemoryCore(CoreConfig(config.to_binding()), vectors=vectors)
    payload = dict(
        hook_event_name="UserPromptSubmit",
        session_id=session,
        turn_id="TEST-private-query",
        prompt_id="TEST-private-query",
        generation_id="TEST-private-query",
        cwd=str(config.home),
        prompt=(
            "TEST explain the public project architecture <private>TEST hidden personal words</private> with examples"
        ),
    )
    monkeypatch.setattr(CodexHookHandler, "ensure_runtime", lambda *a, **k: None)
    try:
        with closing(CodexHookHandler(config, core=core)) as resident:

            def recall(payload, refs, gaps, remaining):
                sent.append(payload)
                answer = resident.resident_recall_for(payload, refs, gaps, remaining)
                return answer, dict(recall_vectors=True)

            if route == "hook":
                with closing(CodexHookHandler(config)) as hook:
                    hook.resident_recall = recall
                    hook.handle_payload(payload, local_record=False)
                assert sent
            else:
                resident.resident_recall_for(payload, (), (), 5)
        assert received, "the actual query embedding port must be exercised"
        assert all("hidden personal words" not in row["input"] for row in received)
        assert all("public project architecture" in row["input"] for row in received)
        assert all("hidden personal words" not in json.dumps(row) for row in sent)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize("module", ["audiences", "installation", "shared_entries"])
def test_hermes_compatibility_exports_only_interfaces(module):
    from importlib import import_module

    shim = import_module(f"scope_recall.adapters.hermes.{module}")
    assert not {
        name
        for name in ("Any", "Path", "Mapping", "Sequence", "Callable", "dataclass", "json", "os")
        if hasattr(shim, name)
    }
