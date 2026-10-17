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


LOCAL_MODEL = "TEST-local-model"
LOCAL_KEY_ENV = "SCOPE_RECALL_TEST_LOCAL_KEY"


def _local_config(folder, **route):
    """A runtime config whose only route is consolidation through ``route``, to a model priced and approved here."""
    from decimal import Decimal

    from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig
    from scope_recall.runtime.model_budget import BudgetPolicy, ModelPricing, initialize_auxiliary_budget_ledger

    pricing = {LOCAL_MODEL: ModelPricing(Decimal("0.01"), Decimal("0.01"))}
    budget = BudgetPolicy(
        batch="TEST-LOCAL-CONSOLIDATION",
        cap_micro_usd=2_000_000,
        total_input_cap=64_000_000,
        total_output_cap=8_000_000,
        total_call_cap=8,
        max_request_bytes=32_000,
        default_reserve_input=32_768,
        default_reserve_output=4_096,
        model_reserve_output={},
        model_token_caps={},
        pricing=pricing,
        approved_models=frozenset(pricing),
    )
    folder.mkdir(parents=True, exist_ok=True)
    ledger = folder / "auxiliary-budget.sqlite3"
    initialize_auxiliary_budget_ledger(ledger, budget)
    return AuxiliaryRuntimeConfig.from_mapping(
        {
            "external_embedding": False,
            "external_consolidation": True,
            "ledger_path": str(ledger),
            "budget": {
                "batch": budget.batch,
                "cap_micro_usd": budget.cap_micro_usd,
                "total_input_cap": budget.total_input_cap,
                "total_output_cap": budget.total_output_cap,
                "total_call_cap": budget.total_call_cap,
                "max_request_bytes": budget.max_request_bytes,
                "approved_models": sorted(budget.approved_models),
                "pricing": {
                    model: {
                        "input_usd_per_million": str(rates.input_usd_per_million),
                        "output_usd_per_million": str(rates.output_usd_per_million),
                    }
                    for model, rates in pricing.items()
                },
            },
            "consolidation": {"model": LOCAL_MODEL, "credential_env": LOCAL_KEY_ENV, **route},
        }
    )


def _chat(endpoint, **more):
    return {"endpoint": endpoint, "output_limit_field": "max_tokens", "max_output_tokens": 256, **more}


def _responses(endpoint, **more):
    return {"kind": "openai_responses", "endpoint": endpoint, "max_output_tokens": 256, **more}


def test_a_consolidation_route_reaches_a_model_server_on_this_machine(tmp_path, monkeypatch):
    """The consolidation route reaches a model server on this machine over plain HTTP, as the embedding route does,
    so a local-first setup can embed and consolidate against the same loopback server.  Both chat dialects go there
    through the real worker, and the key never leaves over plain HTTP."""
    from scope_recall.runtime.auxiliary import build_auxiliary_runtime

    replies = {
        "/v1/chat/completions": {
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            "choices": [{"message": {"role": "assistant", "content": '{"TEST": "chat"}'}, "finish_reason": "stop"}],
        },
        "/v1/responses": {
            "id": "resp_1",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "id": "msg_1",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": '{"TEST": "responses"}', "annotations": []}],
                }
            ],
            "usage": {"input_tokens": 5, "output_tokens": 2},
        },
    }
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, dict(self.headers), body.get("model")))
            reply = json.dumps(replies[self.path]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(LOCAL_KEY_ENV, "TEST-key-canary")
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        for index, (route, answer) in enumerate(
            (
                (_chat(f"{base}/v1/chat/completions"), '{"TEST": "chat"}'),
                (_responses(f"{base}/v1/responses"), '{"TEST": "responses"}'),
            )
        ):
            runtime = build_auxiliary_runtime(_local_config(tmp_path / f"TEST-{index}", **route))
            assert runtime.consolidation is not None, runtime
            messages = [{"role": "user", "content": "TEST 本机整理。"}]
            assert runtime.consolidation.propose(messages, remaining_seconds=10.0) == answer
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert [(path, model) for path, _headers, model in seen] == [
        ("/v1/chat/completions", LOCAL_MODEL),
        ("/v1/responses", LOCAL_MODEL),
    ]
    assert all("TEST-key-canary" not in str(headers) for _path, headers, _model in seen), "plain HTTP carries no key"


@pytest.mark.parametrize("shape", [_chat, _responses])
def test_the_consolidation_route_holds_the_embedding_route_s_endpoint_rule(tmp_path, shape):
    """Plain HTTP to a model server on this machine needs no opt-in, every spelling of it; another host needs
    the literal ``allow_insecure_endpoint``, as the embedding route's, and the transport that sends holds it.  What the
    sending boundary refused anyway (a credential in the URL, user-info, another scheme) is refused at load."""
    from scope_recall.runtime.auxiliary import build_auxiliary_runtime

    folder = iter(tmp_path / f"TEST-{index}" for index in range(100))
    for endpoint in (
        "http://127.0.0.1:8080/v1/x",
        "http://localhost:8080/v1/x",
        "http://[::1]:8080/v1/x",
        "https://api.example.test/v1/x?api-version=1",
    ):
        transport = build_auxiliary_runtime(_local_config(next(folder), **shape(endpoint))).consolidation._transport
        assert transport._allow_insecure_endpoint is False, endpoint
    for endpoint in ("http://172.17.0.1:8080/v1/x", "http://192.0.2.1/v1/x", "http://api.example.test/v1/x"):
        with pytest.raises(ValueError) as refused:
            _local_config(next(folder), **shape(endpoint))
        assert str(refused.value) == "endpoint"
        config = _local_config(next(folder), **shape(endpoint, allow_insecure_endpoint=True))
        assert build_auxiliary_runtime(config).consolidation._transport._allow_insecure_endpoint is True
    for endpoint in (
        "ftp://api.example.test/v1/x",
        "http://user@127.0.0.1/v1/x",
        "https://api.example.test/v1/x?api_key=TEST-canary",
        "https://api.example.test/v1/x#TEST",
        "api.example.test/v1/x",
    ):
        for opt_in in (False, True):
            with pytest.raises(ValueError) as refused:
                _local_config(next(folder), **shape(endpoint, allow_insecure_endpoint=opt_in))
            assert str(refused.value) == "endpoint", endpoint
    for not_a_boolean in ("true", 1, "yes", None):
        with pytest.raises(ValueError) as refused:
            _local_config(next(folder), **shape("http://192.0.2.1/v1/x", allow_insecure_endpoint=not_a_boolean))
        assert str(refused.value) == "consolidation_allow_insecure_endpoint"
