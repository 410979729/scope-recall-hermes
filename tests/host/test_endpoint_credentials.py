"""Model endpoint policy at route, parent transport and real worker boundaries."""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from scope_recall.core.endpoint_policy import endpoint_scheme_allowed, endpoint_url_shape_ok
from scope_recall.runtime import _http_worker, models
from scope_recall.runtime.embedding_models import EmbeddingRouteConfig


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize(
    "suffix",
    [
        "?api_key=TEST-canary",
        "?Access-Token=TEST-canary",
        "?x-goog-api-key=TEST-canary",
        "?%61pi%5fkey=TEST-canary",
        "?%2561pi_key=TEST-canary",
        "?api-version=1;client_secret=TEST-canary",
        "?authorization",
        "?client_assertion=TEST-canary",
        "?private-key=TEST-canary",
        "?api-token=TEST-canary",
        "?x-amz-signature=TEST-canary",
        "?model=TEST&api_key=",
        "?%zz=TEST",
        "#TEST-fragment",
    ],
)
def test_url_credentials_are_refused_at_every_model_boundary(scheme, suffix, monkeypatch):
    url = f"{scheme}://127.0.0.1:1/v1/embeddings{suffix}"
    assert not endpoint_url_shape_ok(url)
    with pytest.raises(ValueError, match="embedding_route_endpoint"):
        EmbeddingRouteConfig(
            credential_env="TEST_KEY", model="TEST-model", endpoint=url, dimensions=8, dialect="openai"
        )

    def no_process(*_args, **_kwargs):
        raise AssertionError("unsafe URL must be refused before starting a worker")

    monkeypatch.setattr(models.subprocess, "Popen", no_process)
    with pytest.raises(models.AuxiliaryModelError) as error:
        models.HttpsTransport().post(url, body=b"{}", headers={}, timeout_seconds=2, max_response_bytes=1024)
    assert error.value.error_type == "endpoint_invalid"
    request = dict(
        url=url, body_b64=base64.b64encode(b"{}").decode(), headers={}, timeout_seconds=2, max_response_bytes=1024
    )
    assert json.loads(_http_worker._request(json.dumps(request).encode()))["error"] == "endpoint_invalid"


def test_plaintext_opt_in_preserves_host_policy_and_safe_query_parameters():
    private = "http://192.0.2.1:18080/v1/embeddings?api-version=1"
    assert endpoint_url_shape_ok(private)
    assert not endpoint_scheme_allowed(private)
    assert endpoint_scheme_allowed(private, allow_insecure=True)
    for invalid in (1, "true", None):
        assert not endpoint_scheme_allowed(private, allow_insecure=invalid)
    for url in ("http://localhost:18080", "http://127.0.0.1", "http://[::1]:18080", "https://synthetic.invalid"):
        assert endpoint_scheme_allowed(url)
    for url in (
        "http://user@127.0.0.1",
        "http://@127.0.0.1",
        "http://127.0.0.1:invalid",
        "http://127.0.0.1:0",
        "http://127.0.0.1/\n",
    ):
        assert not endpoint_scheme_allowed(url, allow_insecure=True)


def test_real_loopback_worker_never_sends_a_query_credential_or_header():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            seen.append((self.path, dict(self.headers)))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/v1/embeddings"
    request = dict(
        url=url + "?api_key=TEST-query-canary",
        body_b64=base64.b64encode(b"{}").decode(),
        headers={"Authorization": "Bearer TEST-header-canary"},
        timeout_seconds=2,
        max_response_bytes=1024,
    )
    try:
        # Bypass the parent on purpose: the final sending boundary must refuse too.
        refused = json.loads(_http_worker._request(json.dumps(request).encode()))
        assert refused["error"] == "endpoint_invalid" and seen == []
        headers = {
            name: "TEST-header-canary"
            for name in (
                "Authorization",
                "x-goog-api-key",
                "Cookie",
                "Proxy-Authorization",
                "X-OpenAI-Api-Key",
                "Ocp-Apim-Subscription-Key",
                "X-Auth-Token",
                "X-Amz-Security-Token",
                "X_Api_Key",
            )
        }
        headers["Content-Type"] = "application/json"
        assert models.HttpsTransport().post(
            url + "?api-version=1", body=b"{}", headers=headers, timeout_seconds=3, max_response_bytes=1024
        ) == (200, b"{}")
        assert len(seen) == 1 and seen[0][0] == "/v1/embeddings?api-version=1"
        assert "TEST-header-canary" not in seen[0][1].values()
        assert seen[0][1]["Content-Type"] == "application/json"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()
