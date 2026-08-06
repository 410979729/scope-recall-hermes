from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

import pytest

import scope_recall.embedders as embedders_module
from scope_recall.capture_llm import _call_openai_compatible, extract_capture_candidates
from scope_recall.embedders import MiniMaxEmbedder, OpenAICompatibleEmbedder, build_embedder
from scope_recall.http_utils import (
    _SafeRedirectHandler,
    UnsafeEndpointError,
    chat_completions_endpoint,
    prepare_safe_request,
    require_safe_endpoint,
    safe_endpoint_display,
    safe_urlopen,
)
from scope_recall.nightly_llm import (
    call_anthropic_messages_llm,
    call_chat_completions_llm,
    call_codex_responses_llm,
    classify_llm_error,
)

_TEST_CREDENTIAL = "opaque-" + "test-value"
_TEST_AUTHORIZATION = "Bear" + "er " + _TEST_CREDENTIAL
_EXTERNAL_CREDENTIAL_ALIASES = (
    "x-token",
    "access_key_id",
    "subscription-key",
    "auth_token",
    "bearer_token",
    "client_assertion",
    "sig",
    "x-goog-credential",
    "x-goog-signature",
    "x-amz-security-token",
    "awsaccesskeyid",
)


class _RecordingServer(ThreadingHTTPServer):
    redirect_url: str
    records: list[dict[str, Any]]


class _RecordingHandler(BaseHTTPRequestHandler):
    server: _RecordingServer

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.server.records.append(
            {
                "method": "POST",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "api_key": self.headers.get("x-api-key"),
                "body": body,
            }
        )
        if self.path in {"/same-origin-301", "/same-origin-302", "/same-origin-303"}:
            self.send_response(int(self.path.rsplit("-", 1)[1]))
            self.send_header("Location", "/final-get")
            self.end_headers()
            return
        if self.path == "/same-origin":
            self.send_response(307)
            self.send_header("Location", "/final")
            self.end_headers()
            return
        if self.path == "/cross-origin":
            self.send_response(307)
            self.send_header("Location", self.server.redirect_url)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        self.server.records.append(
            {
                "method": "GET",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "api_key": self.headers.get("x-api-key"),
                "body": b"",
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))

    def log_message(self, _format: str, *_args: Any) -> None:
        return


@contextmanager
def _recording_server() -> Iterator[_RecordingServer]:
    server = _RecordingServer(("127.0.0.1", 0), _RecordingHandler)
    server.records = []
    server.redirect_url = ""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _server_url(server: _RecordingServer, path: str) -> str:
    return f"http://127.0.0.1:{server.server_port}{path}"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "file:///etc/passwd",
        "gopher://example.com/resource",
        "ftp://example.com/resource",
        "data:text/plain,hello",
        "https://user:password@example.com/v1",
        "https://example.com/v1#fragment",
        "https://example.com/line\nbreak",
        "https:///missing-host",
    ],
)
def test_require_safe_endpoint_rejects_unsafe_url_shapes(url: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        require_safe_endpoint(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1",
        "http://api.localhost:11434/v1",
        "http://127.0.0.1:1234/v1",
        "http://[::1]:1234/v1",
    ],
)
def test_loopback_http_is_compatible_but_never_credentialed(url: str) -> None:
    policy = require_safe_endpoint(url)

    assert policy.url == url
    assert policy.insecure is True
    assert policy.allow_credentials is False


def test_insecure_endpoint_warning_redacts_arbitrary_configured_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    endpoint = "http://127.0.0.1:34569/tenant/opaque-path-secret-marker/v1"

    with caplog.at_level("WARNING", logger="scope_recall.http_utils"):
        require_safe_endpoint(endpoint)

    warning = caplog.text
    assert "opaque-path-secret-marker" not in warning
    assert "http://127.0.0.1:34569" in warning
    assert "/tenant/" not in warning


def test_endpoint_display_defaults_to_public_api_suffix_only() -> None:
    endpoint = "https://api.example.test/tenant/opaque-path-marker/v1/embeddings"

    display = safe_endpoint_display(endpoint)

    assert display == "https://api.example.test/v1/embeddings"
    assert "opaque-path-marker" not in display


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/v1",
        "http://192.168.1.20:1234/v1",
        "http://100.64.0.3:1234/v1",
        "http://model.internal:1234/v1",
        "http://model.local:1234/v1",
    ],
)
def test_non_loopback_http_requires_explicit_opt_in(url: str) -> None:
    with pytest.raises(UnsafeEndpointError, match="allow_insecure_endpoint"):
        require_safe_endpoint(url)

    policy = require_safe_endpoint(url, allow_insecure=True)
    assert policy.insecure is True
    assert policy.allow_credentials is False


def test_https_endpoint_allows_credentials_and_query_parameters() -> None:
    policy = require_safe_endpoint("https://api.example.com/v1?api-version=2026-01-01")

    assert policy.insecure is False
    assert policy.allow_credentials is True


@pytest.mark.parametrize(
    "query_key",
    [
        "api_key",
        "api-key",
        "api%5Fkey",
        "api%255Fkey",
        "apikey",
        "api-token",
        "x-api-key",
        "x-goog-api-key",
        "x-openai-api-key",
        "ocp-apim-subscription-key",
        "x-auth-token",
        "refresh_token",
        "id_token",
        "key",
        "token",
        "access_token",
        "access_key",
        "session_token",
        "security_token",
        "auth",
        "authorization",
        "credential",
        "credentials",
        "x-amz-credential",
        "x-amz-signature",
        "signature",
        "secret",
        "client_secret",
        "private_key",
        "secret_key",
        "password",
        "foo=1;api_key",
    ],
)
def test_endpoint_policy_rejects_credential_like_query_parameters(query_key: str) -> None:
    with pytest.raises(UnsafeEndpointError, match="credential-like query"):
        require_safe_endpoint(
            f"https://api.example.com/v1?{query_key}={_TEST_CREDENTIAL}"
        )


@pytest.mark.parametrize(
    "query_key",
    ("api-version", "model-version", "page_token", "token-estimate"),
)
def test_endpoint_policy_allows_noncredential_query_parameters(query_key: str) -> None:
    policy = require_safe_endpoint(f"https://api.example.com/v1?{query_key}=2026-01-01")

    assert policy.insecure is False
    assert policy.allow_credentials is True


def _percent_encode_every_byte(value: str) -> str:
    """Encode all bytes, including RFC-unreserved punctuation, for adversarial query tests."""

    return "".join(f"%{byte:02X}" for byte in value.encode("utf-8"))


@pytest.mark.parametrize("credential_key", _EXTERNAL_CREDENTIAL_ALIASES)
def test_external_credential_contract_rejects_query_and_strips_http_header(
    credential_key: str,
) -> None:
    encoded_once = _percent_encode_every_byte(credential_key)
    query_variants = (
        credential_key,
        credential_key.upper(),
        credential_key.replace("-", "_"),
        encoded_once,
        _percent_encode_every_byte(encoded_once),
    )
    httpx_module = pytest.importorskip("httpx")
    embedder = OpenAICompatibleEmbedder(
        model="test-embedding-model",
        base_url="http://127.0.0.1:1234/v1",
        api_key=_TEST_CREDENTIAL,
        dimensions=3,
    )

    for variant in query_variants:
        with pytest.raises(UnsafeEndpointError, match="credential-like query"):
            require_safe_endpoint(
                f"https://api.example.com/v1?{variant}={_TEST_CREDENTIAL}"
            )

        request = urllib.request.Request(
            "http://127.0.0.1:1234/v1",
            headers={variant: _TEST_CREDENTIAL, "X-Request-ID": "public-request-id"},
        )
        prepared = prepare_safe_request(request)
        assert _TEST_CREDENTIAL not in dict(prepared.header_items()).values()
        assert prepared.get_header("X-request-id") == "public-request-id"

        sdk_request = httpx_module.Request(
            "POST",
            "http://127.0.0.1:1234/v1",
            headers={variant: _TEST_CREDENTIAL, "X-Request-ID": "public-request-id"},
        )
        embedder._sanitize_httpx_request(sdk_request)
        assert _TEST_CREDENTIAL not in sdk_request.headers.values()
        assert sdk_request.headers["X-Request-ID"] == "public-request-id"


@pytest.mark.parametrize(
    "metadata_key",
    ("api-version", "model-version", "page_token", "token-estimate"),
)
def test_noncredential_metadata_contract_allows_query_and_http_header(
    metadata_key: str,
) -> None:
    assert require_safe_endpoint(
        f"https://api.example.com/v1?{metadata_key}=2026-01-01"
    ).allow_credentials is True

    prepared = prepare_safe_request(
        urllib.request.Request(
            "http://127.0.0.1:1234/v1",
            headers={metadata_key: "public-metadata"},
        )
    )
    normalized_headers = {key.casefold() for key, _value in prepared.header_items()}
    assert metadata_key in normalized_headers


@pytest.mark.parametrize("false_value", ["false", "0", "off"])
@pytest.mark.parametrize("provider", ["openai-compatible", "minimax"])
def test_vector_embedder_false_like_insecure_opt_in_remains_fail_closed(
    provider: str,
    false_value: str,
) -> None:
    with pytest.raises(UnsafeEndpointError, match="allow_insecure_endpoint"):
        build_embedder(
            {
                "provider": provider,
                "base_url": "http://model.internal:1234/v1",
                "allow_insecure_endpoint": false_value,
            }
        )


@pytest.mark.parametrize(
    "raw_value",
    ("true", [False], {"enabled": True}, 1, 1.0),
    ids=("true-string", "array", "object", "integer", "float"),
)
def test_vector_embedder_non_boolean_insecure_opt_in_remains_fail_closed(
    raw_value: object,
) -> None:
    with pytest.raises(UnsafeEndpointError):
        build_embedder(
            {
                "provider": "openai-compatible",
                "model": "test-embedding-model",
                "base_url": "http://model.internal:1234/v1",
                "api_key": _TEST_CREDENTIAL,
                "allow_insecure_endpoint": raw_value,
            }
        )


@pytest.mark.parametrize(
    "raw_value",
    ("false", "true", [False], {"enabled": True}, 1),
    ids=("false-string", "true-string", "array", "object", "integer"),
)
def test_transport_boundary_rejects_non_boolean_insecure_opt_in(
    raw_value: object,
) -> None:
    endpoint = "http://model.internal:1234/v1"
    request = urllib.request.Request(endpoint, data=b"{}", method="POST")

    with pytest.raises(UnsafeEndpointError, match="must be a boolean"):
        require_safe_endpoint(endpoint, allow_insecure=raw_value)  # type: ignore[arg-type]
    with pytest.raises(UnsafeEndpointError, match="must be a boolean"):
        chat_completions_endpoint(
            endpoint,
            allow_insecure_endpoint=raw_value,  # type: ignore[arg-type]
        )
    with pytest.raises(UnsafeEndpointError, match="must be a boolean"):
        prepare_safe_request(request, allow_insecure=raw_value)  # type: ignore[arg-type]
    with pytest.raises(UnsafeEndpointError, match="must be a boolean"):
        safe_urlopen(
            request,
            timeout=1,
            allow_insecure=raw_value,  # type: ignore[arg-type]
        )
    with pytest.raises(UnsafeEndpointError, match="must be a boolean"):
        _SafeRedirectHandler(allow_insecure=raw_value)  # type: ignore[arg-type]
    with pytest.raises(UnsafeEndpointError, match="must be a boolean"):
        call_chat_completions_llm(
            "memory",
            model="model",
            base_url=endpoint,
            api_key=_TEST_CREDENTIAL,
            timeout=1,
            allow_insecure_endpoint=raw_value,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("raw_value", ("true", [False]))
def test_capture_non_boolean_insecure_opt_in_is_passed_as_false(
    monkeypatch: pytest.MonkeyPatch,
    raw_value: object,
) -> None:
    captured: dict[str, object] = {}

    def fake_call(*_args: object, **kwargs: object) -> str:
        captured.update(kwargs)
        return "[]"

    monkeypatch.setattr("scope_recall.capture_llm._call_openai_compatible", fake_call)

    assert extract_capture_candidates(
        "remember this",
        "acknowledged",
        {
            "capture_llm": {
                "enabled": True,
                "base_url": "http://model.internal:1234/v1",
                "api_key": _TEST_CREDENTIAL,
                "allow_insecure_endpoint": raw_value,
            }
        },
    ) == []
    assert captured["allow_insecure_endpoint"] is False


@pytest.mark.parametrize("embedder_cls", (OpenAICompatibleEmbedder, MiniMaxEmbedder))
@pytest.mark.parametrize(
    "endpoint",
    ("https://api.example.test/v1", "http://127.0.0.1:1234/v1"),
    ids=("https", "loopback-http"),
)
@pytest.mark.parametrize(
    "raw_value",
    ("true", 1, [False], {"enabled": True}),
    ids=("true-string", "integer", "array", "object"),
)
def test_embedder_constructor_rejects_non_boolean_insecure_opt_in_on_safe_endpoints(
    embedder_cls,
    endpoint: str,
    raw_value: object,
) -> None:
    with pytest.raises(UnsafeEndpointError, match="must be a boolean"):
        embedder_cls(
            model="test-embedding-model",
            base_url=endpoint,
            api_key=_TEST_CREDENTIAL,
            allow_insecure_endpoint=raw_value,
        )


@pytest.mark.parametrize("provider", ("openai-compatible", "minimax"))
@pytest.mark.parametrize(
    "endpoint",
    ("https://api.example.test/v1", "http://127.0.0.1:1234/v1"),
    ids=("https", "loopback-http"),
)
@pytest.mark.parametrize(
    "raw_value",
    ("true", 1, [False], {"enabled": True}),
    ids=("true-string", "integer", "array", "object"),
)
def test_build_embedder_preserves_non_boolean_opt_in_for_strict_constructor_gate(
    provider: str,
    endpoint: str,
    raw_value: object,
) -> None:
    with pytest.raises(UnsafeEndpointError, match="must be a boolean"):
        build_embedder(
            {
                "provider": provider,
                "model": "test-embedding-model",
                "base_url": endpoint,
                "api_key": _TEST_CREDENTIAL,
                "allow_insecure_endpoint": raw_value,
            }
        )


def test_prepare_safe_request_strips_sensitive_headers_from_http() -> None:
    request = urllib.request.Request(
        "http://127.0.0.1:1234/v1/embeddings",
        data=b'{"input":"memory text"}',
        headers={
            "Authorization": _TEST_AUTHORIZATION,
            "x-api-key": _TEST_CREDENTIAL,
            "Proxy-Authorization": "Basic " + _TEST_CREDENTIAL,
            "Cookie": "session=" + _TEST_CREDENTIAL,
            "Content-Type": "application/json",
            "X-Request-ID": "public-request-id",
        },
        method="POST",
    )

    prepared = prepare_safe_request(request)
    headers = {key.casefold(): value for key, value in prepared.header_items()}

    assert prepared.data == request.data
    assert prepared.get_method() == "POST"
    assert "authorization" not in headers
    assert "x-api-key" not in headers
    assert "proxy-authorization" not in headers
    assert "cookie" not in headers
    assert headers["content-type"] == "application/json"
    assert headers["x-request-id"] == "public-request-id"


def test_prepare_safe_request_preserves_sensitive_headers_for_https() -> None:
    request = urllib.request.Request(
        "https://api.example.com/v1",
        data=b"{}",
        headers={"Authorization": _TEST_AUTHORIZATION},
        method="POST",
    )

    prepared = prepare_safe_request(request)

    assert prepared.get_header("Authorization") == _TEST_AUTHORIZATION


def test_same_origin_redirect_preserves_body_but_not_http_credentials() -> None:
    with _recording_server() as server:
        request = urllib.request.Request(
            _server_url(server, "/same-origin"),
            data=b'{"memory":"body-must-stay-on-origin"}',
            headers={
                "Authorization": _TEST_AUTHORIZATION,
                "Content-Type": "application/json",
            },
            method="POST",
        )

        with safe_urlopen(request, timeout=5) as response:
            assert response.status == 200

    assert [record["path"] for record in server.records] == ["/same-origin", "/final"]
    assert server.records[0]["authorization"] is None
    assert server.records[1]["authorization"] is None
    assert server.records[1]["body"] == b'{"memory":"body-must-stay-on-origin"}'


@pytest.mark.parametrize("status", (301, 302, 303))
def test_same_origin_legacy_redirects_drop_post_body_and_http_credentials(
    status: int,
) -> None:
    with _recording_server() as server:
        request = urllib.request.Request(
            _server_url(server, f"/same-origin-{status}"),
            data=b'{"memory":"body-must-not-be-replayed-as-get"}',
            headers={"Authorization": _TEST_AUTHORIZATION},
            method="POST",
        )

        with safe_urlopen(request, timeout=5) as response:
            assert response.status == 200

    assert [record["method"] for record in server.records] == ["POST", "GET"]
    assert [record["path"] for record in server.records] == [
        f"/same-origin-{status}",
        "/final-get",
    ]
    assert server.records[0]["authorization"] is None
    assert server.records[1]["authorization"] is None
    assert server.records[1]["body"] == b""


def test_redirect_handler_rejects_https_downgrade_and_credential_query_target() -> None:
    handler = _SafeRedirectHandler(allow_insecure=False)
    source = urllib.request.Request(
        "https://api.example.com/v1",
        data=b'{"memory":"must-stay-secure"}',
        method="POST",
    )

    with pytest.raises(UnsafeEndpointError, match="downgrade"):
        handler.redirect_request(
            source,
            None,
            302,
            "Found",
            {},
            "http://api.example.com/v2",
        )
    with pytest.raises(UnsafeEndpointError, match="credential-like query"):
        handler.redirect_request(
            source,
            None,
            302,
            "Found",
            {},
            f"https://api.example.com/v2?api_key={_TEST_CREDENTIAL}",
        )


def test_cross_origin_redirect_is_blocked_before_target_receives_body() -> None:
    with _recording_server() as target, _recording_server() as origin:
        origin.redirect_url = _server_url(target, "/stolen")
        request = urllib.request.Request(
            _server_url(origin, "/cross-origin"),
            data=b'{"memory":"must-not-cross-origin"}',
            headers={"Authorization": _TEST_AUTHORIZATION},
            method="POST",
        )

        with pytest.raises(UnsafeEndpointError, match="cross-origin redirect"):
            safe_urlopen(request, timeout=5)

    assert len(origin.records) == 1
    assert target.records == []


def test_chat_endpoint_builder_applies_policy_to_explicit_endpoint() -> None:
    with pytest.raises(UnsafeEndpointError):
        chat_completions_endpoint(
            "https://api.example.com",
            endpoint="file:///tmp/chat-completions",
        )

    assert (
        chat_completions_endpoint(
            "http://model.internal:1234",
            allow_insecure_endpoint=True,
        )
        == "http://model.internal:1234/v1/chat/completions"
    )


@pytest.mark.parametrize(
    "call",
    [
        lambda: _call_openai_compatible(
            "file:///tmp/provider",
            _TEST_CREDENTIAL,
            "model",
            [{"role": "user", "content": "memory"}],
            100,
            5,
        ),
        lambda: call_chat_completions_llm(
            "memory",
            model="model",
            base_url="file:///tmp/provider",
            api_key=_TEST_CREDENTIAL,
            timeout=5,
        ),
        lambda: call_codex_responses_llm(
            "memory",
            model="model",
            base_url="gopher://provider",
            api_key=_TEST_CREDENTIAL,
            timeout=5,
        ),
        lambda: call_anthropic_messages_llm(
            "memory",
            model="model",
            base_url="https://api.example.com",
            endpoint="file:///tmp/provider",
            api_key=_TEST_CREDENTIAL,
            timeout=5,
        ),
    ],
)
def test_all_llm_http_modes_reject_unsafe_endpoints_before_network(call: Any) -> None:
    with pytest.raises(UnsafeEndpointError):
        call()


def test_endpoint_policy_errors_are_not_retried() -> None:
    assert classify_llm_error(UnsafeEndpointError("blocked")) == (
        "endpoint_policy",
        False,
    )


def test_embedding_adapters_reject_unsafe_endpoints_before_network() -> None:
    with pytest.raises(UnsafeEndpointError):
        OpenAICompatibleEmbedder(
            api_key=_TEST_CREDENTIAL,
            base_url="file:///tmp/embedding",
        )

    with pytest.raises(UnsafeEndpointError):
        MiniMaxEmbedder(
            api_key=_TEST_CREDENTIAL,
            base_url="gopher://embedding",
        )


def test_openai_sdk_transport_disables_redirects_and_strips_http_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeHttpClient:
        def __init__(self, *, follow_redirects: bool, event_hooks: dict[str, list[Any]]) -> None:
            self.follow_redirects = follow_redirects
            self.event_hooks = event_hooks

    class FakeHttpx:
        Client = FakeHttpClient

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            self.embeddings = object()

    monkeypatch.setattr(embedders_module, "_httpx", FakeHttpx)
    monkeypatch.setattr(embedders_module, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(embedders_module, "DefaultHttpxClient", FakeHttpx.Client)

    embedder = OpenAICompatibleEmbedder(
        api_key=_TEST_CREDENTIAL,
        base_url="http://model.internal:1234/v1",
        allow_insecure_endpoint=True,
    )
    embedder._client_or_raise()

    http_client = captured["http_client"]
    assert http_client.follow_redirects is False
    assert captured["base_url"] == "http://model.internal:1234/v1"
    hook = http_client.event_hooks["request"][0]

    insecure_request = type(
        "Request",
        (),
        {
            "url": "http://model.internal:1234/v1/embeddings",
            "headers": {
                "authorization": _TEST_AUTHORIZATION,
                "x-api-key": _TEST_CREDENTIAL,
                "content-type": "application/json",
            },
        },
    )()
    hook(insecure_request)
    assert "authorization" not in insecure_request.headers
    assert "x-api-key" not in insecure_request.headers
    assert insecure_request.headers["content-type"] == "application/json"

    secure_request = type(
        "Request",
        (),
        {
            "url": "https://api.example.com/v1/embeddings",
            "headers": {"authorization": _TEST_AUTHORIZATION},
        },
    )()
    hook(secure_request)
    assert secure_request.headers["authorization"] == _TEST_AUTHORIZATION
