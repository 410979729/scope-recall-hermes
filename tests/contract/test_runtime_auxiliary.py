"""Contract tests for the production auxiliary-model runtime boundary."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import hashlib
import json
import sqlite3
from pathlib import Path
from threading import Barrier

import pytest

from scope_recall.adapters.models import (
    AuxiliaryModelError,
    ConsolidationRouteConfig,
    EmbeddingRouteConfig,
    GeminiEmbeddingAdapter,
    OpenAIConsolidationAdapter,
    build_gemini_embed_body,
    validate_embedding_vector,
)
from scope_recall.contracts import SourceEvent
from scope_recall.core.recall_policy import EMBEDDING_SPACE, encode_embedding_text
from scope_recall.core.storage import StoredSource
from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig, auxiliary_runtime_status, build_auxiliary_runtime
from scope_recall.runtime.model_budget import (
    AuxiliaryBudgetLedger,
    BudgetPolicy,
    ModelPricing,
    initialize_auxiliary_budget_ledger,
    read_auxiliary_budget_status,
)


def _source(content: str = "标题 A") -> StoredSource:
    event: SourceEvent = {
        "protocol_version": "1.1",
        "origin": "human_direct",
        "role": "user",
        "content": content,
        "occurred_at": "2026-09-06T12:00:00Z",
        "artifact_refs": [],
    }
    return StoredSource(
        ref="src-1",
        revision=1,
        scope_id="scope-a",
        session_id="sess-a",
        project_id=None,
        branch_id=None,
        event=event,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        suppressed=False,
    )


def _approved_budget(*, cap_micro_usd: int = 2_000_000, total_call_cap: int = 8) -> BudgetPolicy:
    pricing = {
        EMBEDDING_SPACE["model"]: ModelPricing(Decimal("0.20"), Decimal("0")),
        "deepseek-v4-flash": ModelPricing(Decimal("0.44"), Decimal("1.32")),
        "mimo-v2.5": ModelPricing(Decimal("0.14"), Decimal("0.28")),
    }
    return BudgetPolicy(
        batch="TEST-RUNTIME-CURSOR",
        cap_micro_usd=cap_micro_usd,
        total_input_cap=64_000_000,
        total_output_cap=8_000_000,
        total_call_cap=total_call_cap,
        max_request_bytes=32_000,
        default_reserve_input=32_768,
        default_reserve_output=4_096,
        model_reserve_output={"mimo-v2.5": 131_072},
        model_token_caps={},
        pricing=pricing,
        approved_models=frozenset(pricing),
    )


def _runtime_config(tmp_path: Path, **overrides):
    budget = _approved_budget()
    ledger = tmp_path / "auxiliary-budget.sqlite3"
    initialize_auxiliary_budget_ledger(ledger, budget)
    payload = {
        "external_embedding": True,
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
                for model, rates in budget.pricing.items()
            },
        },
        "embedding": {"credential_env": "SCOPE_RECALL_TEST_EMBED_KEY"},
        "consolidation": {
            "model": "deepseek-v4-flash",
            "endpoint": "https://example.test/v1/chat/completions",
            "credential_env": "SCOPE_RECALL_TEST_CHAT_KEY",
            "output_limit_field": "max_tokens",
            "max_output_tokens": 512,
            "thinking": {"type": "disabled"},
        },
    }
    payload.update(overrides)
    return AuxiliaryRuntimeConfig.from_mapping(payload), ledger, budget


class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls = 0

    def post(self, url, *, body, headers, timeout_seconds, max_response_bytes):
        self.calls += 1
        return self.handler(url=url, body=body, headers=headers, timeout_seconds=timeout_seconds, max_response_bytes=max_response_bytes)


def _vector(count: int = 3072, *, nan_at: int | None = None, length: int | None = None) -> list[float]:
    size = length if length is not None else count
    values = [0.01] * size
    if nan_at is not None and 0 <= nan_at < len(values):
        values[nan_at] = float("nan")
    return values


def test_import_and_readonly_status_without_network(tmp_path):
    config, ledger, _ = _runtime_config(tmp_path, external_embedding=False, external_consolidation=False)
    runtime = build_auxiliary_runtime(config)
    assert runtime.source_embedding is None
    assert runtime.consolidation is None
    assert "external_embedding_not_approved" in runtime.capability_gaps
    assert "external_consolidation_not_approved" in runtime.capability_gaps
    status = auxiliary_runtime_status(config)
    assert status["budget"]["ledger_exists"] is True
    assert status["budget"]["requests"] == 0
    missing = read_auxiliary_budget_status(tmp_path / "missing.sqlite3")
    assert missing["ledger_exists"] is False


def test_unauthorized_routes_do_not_read_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "secret-should-not-be-read")
    config = AuxiliaryRuntimeConfig.from_mapping({"external_embedding": False, "external_consolidation": False})
    runtime = build_auxiliary_runtime(config)
    assert runtime.source_embedding is None
    assert runtime.consolidation is None


def test_gemini_body_matches_frozen_role_encoding():
    raw_query = "设计 A"
    raw_source = "标题 A"
    encoded_query = encode_embedding_text(raw_query, kind="query")
    encoded_source = encode_embedding_text(raw_source, kind="document")
    query_body = json.loads(build_gemini_embed_body(encoded_query))
    source_body = json.loads(build_gemini_embed_body(encoded_source))
    request = query_body["requests"][0]
    assert request["model"] == f"models/{EMBEDDING_SPACE['model']}"
    assert request["content"]["parts"][0]["text"] == "task: question answering | query: 设计 A"
    assert request["embedContentConfig"] == {"outputDimensionality": 3072, "autoTruncate": False}
    assert source_body["requests"][0]["content"]["parts"][0]["text"] == "title: none | text: 标题 A"
    assert "taskType" not in request


def test_secret_rejection_before_transport(tmp_path):
    config, ledger, budget = _runtime_config(tmp_path)
    transport = FakeTransport(lambda **kwargs: (200, b"{}"))
    runtime = build_auxiliary_runtime(config, transport=transport)
    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.query_embedding.embed_query("sk-ant-api03-" + ("A" * 32), remaining_seconds=2.0)
    assert exc.value.error_type == "sensitive_request"
    assert transport.calls == 0
    ledger_obj = AuxiliaryBudgetLedger(ledger, budget)
    consolidation = OpenAIConsolidationAdapter(
        ConsolidationRouteConfig(
            model="deepseek-v4-flash",
            endpoint="https://example.test/v1/chat/completions",
            credential_env="SCOPE_RECALL_TEST_CHAT_KEY",
            output_limit_field="max_tokens",
            max_output_tokens=512,
            thinking={"type": "disabled"},
        ),
        ledger=ledger_obj,
        reserve_input=32_768,
        transport=transport,
    )
    with pytest.raises(AuxiliaryModelError) as exc2:
        consolidation.propose(
            [{"role": "user", "content": "api_key=supersecretvalue1234567890"}],
            remaining_seconds=2.0,
        )
    assert exc2.value.error_type == "sensitive_request"


def test_malformed_and_invalid_vectors(tmp_path, monkeypatch):
    config, _, _ = _runtime_config(tmp_path)
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "test-key")
    transport = FakeTransport(
        lambda **kwargs: (
            200,
            json.dumps({"embeddings": [{"values": _vector(nan_at=0)}], "usageMetadata": {"promptTokenCount": 10}}).encode(),
        )
    )
    runtime = build_auxiliary_runtime(config, transport=transport)
    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.query_embedding.embed_query("hello", remaining_seconds=2.0)
    assert exc.value.error_type == "vector_nonfinite"
    transport.handler = lambda **kwargs: (
        200,
        json.dumps({"embeddings": [{"values": _vector(length=10)}], "usageMetadata": {"promptTokenCount": 10}}).encode(),
    )
    with pytest.raises(AuxiliaryModelError) as exc2:
        runtime.query_embedding.embed_query("hello", remaining_seconds=2.0)
    assert exc2.value.error_type == "vector_dimension_mismatch"
    with pytest.raises(AuxiliaryModelError):
        validate_embedding_vector([0.0] * 3072)


def test_consolidation_returns_raw_content_without_json_repair(tmp_path, monkeypatch):
    config, _, _ = _runtime_config(tmp_path)
    monkeypatch.setenv("SCOPE_RECALL_TEST_CHAT_KEY", "test-key")
    transport = FakeTransport(
        lambda **kwargs: (
            200,
            json.dumps(
                {
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    "choices": [{"message": {"role": "assistant", "content": "{not valid json"}, "finish_reason": "stop"}],
                }
            ).encode(),
        )
    )
    runtime = build_auxiliary_runtime(config, transport=transport)
    content = runtime.consolidation.propose(
        [{"role": "user", "content": "bounded input"}],
        remaining_seconds=2.0,
    )
    assert content == "{not valid json"


def test_consolidation_route_options_are_explicit(tmp_path, monkeypatch):
    config, _, _ = _runtime_config(tmp_path)
    monkeypatch.setenv("SCOPE_RECALL_TEST_CHAT_KEY", "test-key")
    captured = {}

    def handler(**kwargs):
        captured["body"] = json.loads(kwargs["body"].decode())
        return (
            200,
            json.dumps(
                {
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    "choices": [{"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}],
                }
            ).encode(),
        )

    transport = FakeTransport(handler)
    runtime = build_auxiliary_runtime(config, transport=transport)
    runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert captured["body"]["model"] == "deepseek-v4-flash"
    assert captured["body"]["max_tokens"] == 512
    assert captured["body"]["thinking"] == {"type": "disabled"}
    assert captured["body"]["stream"] is False
    assert "response_format" not in captured["body"]
    assert "reasoning_effort" not in captured["body"]


def test_consolidation_route_includes_reasoning_effort_when_configured(tmp_path, monkeypatch):
    config, _, _ = _runtime_config(
        tmp_path,
        consolidation={
            "model": "deepseek-v4-flash",
            "endpoint": "https://example.test/v1/chat/completions",
            "credential_env": "SCOPE_RECALL_TEST_CHAT_KEY",
            "output_limit_field": "max_tokens",
            "max_output_tokens": 512,
            "thinking": {"type": "disabled"},
            "reasoning_effort": "high",
        },
    )
    monkeypatch.setenv("SCOPE_RECALL_TEST_CHAT_KEY", "test-key")
    captured = {}

    def handler(**kwargs):
        captured["body"] = json.loads(kwargs["body"].decode())
        return (
            200,
            json.dumps(
                {
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    "choices": [{"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}],
                }
            ).encode(),
        )

    runtime = build_auxiliary_runtime(config, transport=FakeTransport(handler))
    runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert captured["body"]["reasoning_effort"] == "high"


def test_reasoning_none_is_allowed_only_for_opencode_go_route():
    base = dict(model="deepseek-v4-flash", credential_env="SCOPE_RECALL_TEST_CHAT_KEY",
                output_limit_field="max_tokens", max_output_tokens=512)
    route = ConsolidationRouteConfig(**base, endpoint="https://opencode.ai/zen/go/v1/chat/completions",
                                     reasoning_effort="none")
    assert route.reasoning_effort == "none"
    for endpoint in ("https://api.deepseek.com/chat/completions",
                     "https://opencode.ai/zen/v1/chat/completions",
                     "https://example.test/v1/chat/completions"):
        with pytest.raises(ValueError, match="reasoning_effort"):
            ConsolidationRouteConfig(**base, endpoint=endpoint, reasoning_effort="none")
        assert ConsolidationRouteConfig(**base, endpoint=endpoint, reasoning_effort="high").reasoning_effort == "high"


def test_opencode_go_serializes_reasoning_none_with_original_bounds(tmp_path, monkeypatch):
    config, _, _ = _runtime_config(tmp_path, consolidation={
        "model": "deepseek-v4-flash", "endpoint": "https://opencode.ai/zen/go/v1/chat/completions",
        "credential_env": "SCOPE_RECALL_TEST_CHAT_KEY", "output_limit_field": "max_tokens",
        "max_output_tokens": 512, "thinking": {"type": "disabled"}, "reasoning_effort": "none",
        "response_format": {"type": "json_object"},
    })
    monkeypatch.setenv("SCOPE_RECALL_TEST_CHAT_KEY", "test-key")
    bodies = []
    def handler(**kwargs):
        bodies.append(json.loads(kwargs["body"].decode()))
        return 200, json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "choices": [{"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}]}).encode()
    runtime = build_auxiliary_runtime(config, transport=FakeTransport(handler))
    assert runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0) == "{}"
    assert len(bodies) == 1
    assert bodies[0]["reasoning_effort"] == "none"
    assert bodies[0]["thinking"] == {"type": "disabled"}
    assert bodies[0]["response_format"] == {"type": "json_object"}
    assert bodies[0]["max_tokens"] == 512 and bodies[0]["stream"] is False and bodies[0]["n"] == 1


def test_consolidation_route_static_headers_are_sent_and_explicit_keys_win(tmp_path, monkeypatch):
    config, _, _ = _runtime_config(
        tmp_path,
        consolidation={
            "model": "deepseek-v4-flash",
            "endpoint": "https://example.test/v1/chat/completions",
            "credential_env": "SCOPE_RECALL_TEST_CHAT_KEY",
            "output_limit_field": "max_tokens",
            "max_output_tokens": 512,
            "headers": {"x-opencode-session": "scope-recall-test-suite"},
        },
    )
    monkeypatch.setenv("SCOPE_RECALL_TEST_CHAT_KEY", "test-key")
    captured = {}

    def handler(**kwargs):
        captured["headers"] = kwargs["headers"]
        return (
            200,
            json.dumps(
                {
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    "choices": [{"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}],
                }
            ).encode(),
        )

    runtime = build_auxiliary_runtime(config, transport=FakeTransport(handler))
    runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert captured["headers"]["x-opencode-session"] == "scope-recall-test-suite"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["headers"]["User-Agent"] == "ScopeRecall-AuxiliaryConsolidation/1.1"


def test_opencode_go_consolidation_sends_session_header_when_unconfigured(tmp_path, monkeypatch):
    config, _, _ = _runtime_config(
        tmp_path,
        consolidation={
            "model": "mimo-v2.5",
            "endpoint": "https://opencode.ai/zen/go/v1/chat/completions",
            "credential_env": "SCOPE_RECALL_TEST_CHAT_KEY",
            "output_limit_field": "max_completion_tokens",
            "max_output_tokens": 512,
        },
    )
    monkeypatch.setenv("SCOPE_RECALL_TEST_CHAT_KEY", "test-key")
    monkeypatch.delenv("SCOPE_RECALL_TEST_OPENCODE_SESSION", raising=False)
    monkeypatch.delenv("SCOPE_RECALL_P11_TEST_CONTEXT", raising=False)
    captured = {}

    def handler(**kwargs):
        captured["headers"] = kwargs["headers"]
        return (
            200,
            json.dumps(
                {
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    "choices": [{"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}],
                }
            ).encode(),
        )

    runtime = build_auxiliary_runtime(config, transport=FakeTransport(handler))
    runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert captured["headers"]["x-opencode-session"] == "scope-recall-auxiliary-consolidation"


def test_opencode_go_consolidation_keeps_configured_session_header(tmp_path, monkeypatch):
    config, _, _ = _runtime_config(
        tmp_path,
        consolidation={
            "model": "mimo-v2.5",
            "endpoint": "https://opencode.ai/zen/go/v1/chat/completions",
            "credential_env": "SCOPE_RECALL_TEST_CHAT_KEY",
            "output_limit_field": "max_completion_tokens",
            "max_output_tokens": 512,
            "headers": {"x-opencode-session": "scope-recall-test-configured"},
        },
    )
    monkeypatch.setenv("SCOPE_RECALL_TEST_CHAT_KEY", "test-key")
    monkeypatch.setenv("SCOPE_RECALL_TEST_OPENCODE_SESSION", "scope-recall-test-env")
    captured = {}

    def handler(**kwargs):
        captured["headers"] = kwargs["headers"]
        return (
            200,
            json.dumps(
                {
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    "choices": [{"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}],
                }
            ).encode(),
        )

    runtime = build_auxiliary_runtime(config, transport=FakeTransport(handler))
    runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert captured["headers"]["x-opencode-session"] == "scope-recall-test-configured"


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer smuggled-second-credential"},
        {"authorization": "Bearer smuggled-lowercase"},
        {"content-type": "text/plain"},
        {"User-Agent": "spoofed-client"},
        {"Host": "example.test"},
        {"x-multiline": "line\r\nbreak"},
        {"x-empty": "   "},
        {"bad name": "v"},
        {f"x-{index}": "v" for index in range(9)},
    ],
)
def test_consolidation_route_rejects_unsafe_static_headers(tmp_path, headers):
    with pytest.raises(ValueError):
        _runtime_config(
            tmp_path,
            consolidation={
                "model": "deepseek-v4-flash",
                "endpoint": "https://example.test/v1/chat/completions",
                "credential_env": "SCOPE_RECALL_TEST_CHAT_KEY",
                "output_limit_field": "max_tokens",
                "max_output_tokens": 512,
                "headers": headers,
            },
        )


@pytest.mark.parametrize(
    "reasoning_effort",
    [
        "medium",
        "",
        1,
        True,
        ["high"],
    ],
)
def test_consolidation_reasoning_effort_rejects_invalid_values(reasoning_effort):
    base = {
        "model": "deepseek-v4-flash",
        "endpoint": "https://example.test/v1/chat/completions",
        "credential_env": "SCOPE_RECALL_TEST_CHAT_KEY",
        "output_limit_field": "max_tokens",
        "max_output_tokens": 512,
    }
    with pytest.raises(ValueError, match="reasoning_effort"):
        ConsolidationRouteConfig(**base, reasoning_effort=reasoning_effort)


def test_consolidation_reasoning_effort_mapping_rejects_invalid_values(tmp_path):
    base = {
        "external_embedding": False,
        "external_consolidation": True,
        "consolidation": {
            "model": "deepseek-v4-flash",
            "endpoint": "https://example.test/v1/chat/completions",
            "credential_env": "SCOPE_RECALL_TEST_CHAT_KEY",
            "output_limit_field": "max_tokens",
            "max_output_tokens": 512,
            "reasoning_effort": "medium",
        },
    }
    with pytest.raises(ValueError, match="reasoning_effort"):
        AuxiliaryRuntimeConfig.from_mapping(base)


def test_consolidation_reasoning_effort_is_compatible_with_json_object_response_format(tmp_path, monkeypatch):
    config, _, _ = _runtime_config(
        tmp_path,
        consolidation={
            "model": "mimo-v2.5",
            "endpoint": "https://example.test/v1/chat/completions",
            "credential_env": "SCOPE_RECALL_TEST_CHAT_KEY",
            "output_limit_field": "max_tokens",
            "max_output_tokens": 512,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "reasoning_effort": "low",
        },
    )
    monkeypatch.setenv("SCOPE_RECALL_TEST_CHAT_KEY", "test-key")
    captured = {}

    def handler(**kwargs):
        captured["body"] = json.loads(kwargs["body"].decode())
        return (
            200,
            json.dumps(
                {
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    "choices": [{"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}],
                }
            ).encode(),
        )

    runtime = build_auxiliary_runtime(config, transport=FakeTransport(handler))
    runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["body"]["reasoning_effort"] == "low"


def test_budget_exhaustion_and_meter_breach(tmp_path, monkeypatch):
    tight = _approved_budget(cap_micro_usd=1, total_call_cap=1)
    ledger = tmp_path / "tight.sqlite3"
    initialize_auxiliary_budget_ledger(ledger, tight)
    config, _, _ = _runtime_config(
        tmp_path,
        ledger_path=str(ledger),
        budget={
            "batch": tight.batch,
            "cap_micro_usd": tight.cap_micro_usd,
            "total_input_cap": tight.total_input_cap,
            "total_output_cap": tight.total_output_cap,
            "total_call_cap": tight.total_call_cap,
            "max_request_bytes": tight.max_request_bytes,
            "approved_models": sorted(tight.approved_models),
            "pricing": {
                model: {
                    "input_usd_per_million": str(rates.input_usd_per_million),
                    "output_usd_per_million": str(rates.output_usd_per_million),
                }
                for model, rates in tight.pricing.items()
            },
        },
    )
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "test-key")
    transport = FakeTransport(
        lambda **kwargs: (
            200,
            json.dumps(
                {
                    "embeddings": [{"values": _vector()}],
                    "usageMetadata": {"promptTokenCount": 10},
                }
            ).encode(),
        )
    )
    runtime = build_auxiliary_runtime(config, transport=transport)
    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.query_embedding.embed_query("hello", remaining_seconds=2.0)
    assert exc.value.error_type == "budget_exhausted"


def test_meter_breach_blocks_after_overrun(tmp_path, monkeypatch):
    config, ledger, budget = _runtime_config(tmp_path)
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "test-key")
    transport = FakeTransport(
        lambda **kwargs: (
            200,
            json.dumps(
                {
                    "embeddings": [{"values": _vector()}],
                    "usageMetadata": {"promptTokenCount": 999_999},
                }
            ).encode(),
        )
    )
    runtime = build_auxiliary_runtime(config, transport=transport)
    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.query_embedding.embed_query("hello", remaining_seconds=2.0)
    assert exc.value.error_type == "meter_breach"
    ledger_obj = AuxiliaryBudgetLedger(ledger, budget)
    body = build_gemini_embed_body(encode_embedding_text("blocked", kind="query"))
    with pytest.raises(ValueError, match="budget_exhausted_or_meter_breach"):
        ledger_obj.reserve(
            EMBEDDING_SPACE["model"],
            body,
            reserved_input=8192,
            reserved_output=0,
        )


def test_http_status_failure_is_distinct(tmp_path, monkeypatch):
    config, _, _ = _runtime_config(tmp_path)
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "test-key")
    transport = FakeTransport(lambda **kwargs: (503, b"{}"))
    runtime = build_auxiliary_runtime(config, transport=transport)
    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.query_embedding.embed_query("hello", remaining_seconds=2.0)
    assert exc.value.error_type == "http_status"


def test_unknown_usage_keeps_reserve(tmp_path, monkeypatch):
    config, ledger, budget = _runtime_config(tmp_path)
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "test-key")
    transport = FakeTransport(
        lambda **kwargs: (
            200,
            json.dumps({"embeddings": [{"values": _vector()}]}).encode(),
        )
    )
    runtime = build_auxiliary_runtime(config, transport=transport)
    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.query_embedding.embed_query("hello", remaining_seconds=2.0)
    assert exc.value.error_type == "missing_usage"
    status = read_auxiliary_budget_status(ledger)
    assert status["requests"] == 1
    assert status["charge_micro_usd"] > 0


def test_concurrent_reservations_are_atomic(tmp_path, monkeypatch):
    config, ledger, budget = _runtime_config(tmp_path)
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "test-key")
    transport = FakeTransport(
        lambda **kwargs: (
            200,
            json.dumps(
                {
                    "embeddings": [{"values": _vector()}],
                    "usageMetadata": {"promptTokenCount": 100},
                }
            ).encode(),
        )
    )
    runtime = build_auxiliary_runtime(config, transport=transport)
    barrier = Barrier(2)

    def worker():
        barrier.wait(timeout=2)
        return runtime.query_embedding.embed_query("hello", remaining_seconds=5.0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker)
        second = pool.submit(worker)
        results = [first.result(), second.result()]
    assert all(len(item) == 3072 for item in results)
    assert read_auxiliary_budget_status(ledger)["requests"] == 2


def test_timeout_is_distinct(tmp_path, monkeypatch):
    config, _, _ = _runtime_config(tmp_path)
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "test-key")

    def slow(**kwargs):
        if kwargs["timeout_seconds"] <= 0:
            raise TimeoutError
        raise TimeoutError

    transport = FakeTransport(slow)
    runtime = build_auxiliary_runtime(config, transport=transport)
    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.query_embedding.embed_query("hello", remaining_seconds=0.0)
    assert exc.value.error_type == "timeout"


def test_source_embedding_uses_document_encoding(tmp_path, monkeypatch):
    config, _, _ = _runtime_config(tmp_path)
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "test-key")
    captured = {}

    def handler(**kwargs):
        captured["body"] = json.loads(kwargs["body"].decode())
        return (
            200,
            json.dumps(
                {
                    "embeddings": [{"values": _vector()}],
                    "usageMetadata": {"promptTokenCount": 100},
                }
            ).encode(),
        )

    runtime = build_auxiliary_runtime(config, transport=FakeTransport(handler))
    runtime.source_embedding.embed_source(_source("正文 B"), remaining_seconds=2.0)
    text = captured["body"]["requests"][0]["content"]["parts"][0]["text"]
    assert text == encode_embedding_text("正文 B", kind="document")


def test_import_modules_in_either_order():
    import subprocess
    import sys

    # Import-order checks must not evict the running suite's modules: retained
    # class globals would otherwise bypass later transport monkeypatches.
    root = Path(__file__).resolve().parents[2]
    bootstrap = (
        "import importlib, importlib.util, sys; "
        f"root={str(root)!r}; "
        "spec=importlib.util.spec_from_file_location('scope_recall',root+'/__init__.py',submodule_search_locations=[root]); "
        "package=importlib.util.module_from_spec(spec); sys.modules['scope_recall']=package; spec.loader.exec_module(package); "
    )
    for order in (("adapters.models", "runtime.auxiliary"), ("runtime.auxiliary", "adapters.models")):
        program = bootstrap + (
            f"[importlib.import_module('scope_recall.'+name) for name in {order!r}]; "
            "models=importlib.import_module('scope_recall.adapters.models'); "
            "auxiliary=importlib.import_module('scope_recall.runtime.auxiliary'); "
            "runtime_pkg=importlib.import_module('scope_recall.runtime'); "
            "assert models.AuxiliaryModelError is not None; "
            "assert auxiliary.build_auxiliary_runtime is not None; "
            "assert runtime_pkg.AuxiliaryRuntimeConfig is auxiliary.AuxiliaryRuntimeConfig"
        )
        result = subprocess.run([sys.executable, "-I", "-B", "-c", program],
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr


def test_credential_sk_key_is_header_only(tmp_path, monkeypatch):
    synthetic_key = "sk-proj-" + ("A" * 40)
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", synthetic_key)
    config, _, _ = _runtime_config(tmp_path)
    captured: dict[str, object] = {}

    def handler(**kwargs):
        captured["headers"] = dict(kwargs["headers"])
        captured["body"] = kwargs["body"].decode("utf-8")
        return (
            200,
            json.dumps(
                {
                    "embeddings": [{"values": _vector()}],
                    "usageMetadata": {"promptTokenCount": 1},
                }
            ).encode(),
        )

    runtime = build_auxiliary_runtime(config, transport=FakeTransport(handler))
    runtime.query_embedding.embed_query("hello", remaining_seconds=2.0)
    assert captured["headers"]["x-goog-api-key"] == synthetic_key
    assert synthetic_key not in captured["body"]


def test_primary_error_not_suppressed_on_malformed_success_body(tmp_path, monkeypatch):
    config, _, _ = _runtime_config(tmp_path)
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "test-key")
    transport = FakeTransport(
        lambda **kwargs: (
            200,
            json.dumps({"embeddings": [{"values": _vector(nan_at=0)}], "usageMetadata": {"promptTokenCount": 10}}).encode(),
        )
    )
    runtime = build_auxiliary_runtime(config, transport=transport)
    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.query_embedding.embed_query("hello", remaining_seconds=2.0)
    assert exc.value.error_type == "vector_nonfinite"
    assert exc.value is not None


def test_json_non_object_root_is_distinct(tmp_path, monkeypatch):
    config, _, _ = _runtime_config(tmp_path)
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "test-key")
    transport = FakeTransport(lambda **kwargs: (200, b"[]"))
    runtime = build_auxiliary_runtime(config, transport=transport)
    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.query_embedding.embed_query("hello", remaining_seconds=2.0)
    assert exc.value.error_type == "unsupported_response_shape"


def test_invalid_utf8_response_is_unicode_error(tmp_path, monkeypatch):
    config, _, _ = _runtime_config(tmp_path)
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "test-key")
    transport = FakeTransport(lambda **kwargs: (200, b"\xff\xfe"))
    runtime = build_auxiliary_runtime(config, transport=transport)
    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.query_embedding.embed_query("hello", remaining_seconds=2.0)
    assert exc.value.error_type == "unicode_error"


def test_http_status_is_not_replaced_by_missing_usage(tmp_path, monkeypatch):
    config, ledger, _ = _runtime_config(tmp_path)
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "test-key")
    transport = FakeTransport(lambda **kwargs: (503, b"{}"))
    runtime = build_auxiliary_runtime(config, transport=transport)
    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.query_embedding.embed_query("hello", remaining_seconds=2.0)
    assert exc.value.error_type == "http_status"
    status = read_auxiliary_budget_status(ledger)
    assert status["requests"] == 1
    assert status["charge_micro_usd"] > 0


def test_budget_mapping_rejects_bool_coercion(tmp_path):
    budget = _approved_budget()
    ledger = tmp_path / "ledger.sqlite3"
    initialize_auxiliary_budget_ledger(ledger, budget)
    with pytest.raises(ValueError, match="cap_micro_usd"):
        AuxiliaryRuntimeConfig.from_mapping(
            {
                "external_embedding": False,
                "external_consolidation": False,
                "ledger_path": str(ledger),
                "budget": {
                    "batch": budget.batch,
                    "cap_micro_usd": True,
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
                        for model, rates in budget.pricing.items()
                    },
                },
            }
        )


def test_relative_ledger_path_rejected():
    with pytest.raises(ValueError, match="ledger_path_must_be_absolute"):
        AuxiliaryRuntimeConfig.from_mapping(
            {
                "external_embedding": False,
                "external_consolidation": False,
                "ledger_path": "relative/auxiliary-budget.sqlite3",
            }
        )


def test_consolidation_route_includes_json_object_response_format_when_configured(tmp_path, monkeypatch):
    config, _, _ = _runtime_config(
        tmp_path,
        consolidation={
            "model": "mimo-v2.5",
            "endpoint": "https://example.test/v1/chat/completions",
            "credential_env": "SCOPE_RECALL_TEST_CHAT_KEY",
            "output_limit_field": "max_tokens",
            "max_output_tokens": 512,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
        },
    )
    monkeypatch.setenv("SCOPE_RECALL_TEST_CHAT_KEY", "test-key")
    captured = {}

    def handler(**kwargs):
        captured["body"] = json.loads(kwargs["body"].decode())
        return (
            200,
            json.dumps(
                {
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    "choices": [{"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}],
                }
            ).encode(),
        )

    runtime = build_auxiliary_runtime(config, transport=FakeTransport(handler))
    runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert captured["body"]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(
    "response_format",
    [
        "json_object",
        {"type": "text"},
        {"type": "json_object", "schema": {}},
        {"schema": "json_object"},
        {},
    ],
)
def test_consolidation_response_format_rejects_invalid_shapes(response_format):
    base = {
        "model": "mimo-v2.5",
        "endpoint": "https://example.test/v1/chat/completions",
        "credential_env": "SCOPE_RECALL_TEST_CHAT_KEY",
        "output_limit_field": "max_tokens",
        "max_output_tokens": 512,
    }
    with pytest.raises(ValueError, match="response_format"):
        ConsolidationRouteConfig(**base, response_format=response_format)


def test_consolidation_response_format_mapping_rejects_invalid_shapes(tmp_path):
    base = {
        "external_embedding": False,
        "external_consolidation": True,
        "consolidation": {
            "model": "mimo-v2.5",
            "endpoint": "https://example.test/v1/chat/completions",
            "credential_env": "SCOPE_RECALL_TEST_CHAT_KEY",
            "output_limit_field": "max_tokens",
            "max_output_tokens": 512,
            "response_format": {"type": "text"},
        },
    }
    with pytest.raises(ValueError, match="response_format"):
        AuxiliaryRuntimeConfig.from_mapping(base)


def test_consolidation_json_object_route_returns_fenced_content_unchanged(tmp_path, monkeypatch):
    fenced = '```json\n{"goal":{"text":"keep verbatim"}}\n```'
    config, _, _ = _runtime_config(
        tmp_path,
        consolidation={
            "model": "mimo-v2.5",
            "endpoint": "https://example.test/v1/chat/completions",
            "credential_env": "SCOPE_RECALL_TEST_CHAT_KEY",
            "output_limit_field": "max_tokens",
            "max_output_tokens": 512,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
        },
    )
    monkeypatch.setenv("SCOPE_RECALL_TEST_CHAT_KEY", "test-key")
    transport = FakeTransport(
        lambda **kwargs: (
            200,
            json.dumps(
                {
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    "choices": [{"message": {"role": "assistant", "content": fenced}, "finish_reason": "stop"}],
                }
            ).encode(),
        )
    )
    runtime = build_auxiliary_runtime(config, transport=transport)
    content = runtime.consolidation.propose(
        [{"role": "user", "content": "bounded input"}],
        remaining_seconds=2.0,
    )
    assert content == fenced


def test_consolidation_route_requires_stream_false_and_n_one():
    base = {
        "model": "deepseek-v4-flash",
        "endpoint": "https://example.test/v1/chat/completions",
        "credential_env": "SCOPE_RECALL_TEST_CHAT_KEY",
        "output_limit_field": "max_tokens",
        "max_output_tokens": 512,
    }
    with pytest.raises(ValueError, match="stream"):
        ConsolidationRouteConfig(**base, stream=True, n=1)
    with pytest.raises(ValueError, match="^n$"):
        ConsolidationRouteConfig(**base, stream=False, n=2)


def test_mimo_output_reserve_uses_model_floor(tmp_path, monkeypatch):
    config, ledger, _ = _runtime_config(
        tmp_path,
        consolidation={
            "model": "mimo-v2.5",
            "endpoint": "https://example.test/v1/chat/completions",
            "credential_env": "SCOPE_RECALL_TEST_CHAT_KEY",
            "output_limit_field": "max_tokens",
            "max_output_tokens": 512,
            "thinking": {"type": "disabled"},
        },
    )
    monkeypatch.setenv("SCOPE_RECALL_TEST_CHAT_KEY", "test-key")
    transport = FakeTransport(lambda **kwargs: (503, b"{}"))
    runtime = build_auxiliary_runtime(config, transport=transport)
    with pytest.raises(AuxiliaryModelError):
        runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    with sqlite3.connect(ledger) as db:
        reserved_output = db.execute("SELECT reserved_output FROM requests ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert reserved_output == 131_072


def test_response_metadata_does_not_hide_assistant_content(tmp_path, monkeypatch):
    config, _, _ = _runtime_config(tmp_path)
    monkeypatch.setenv("SCOPE_RECALL_TEST_CHAT_KEY", "test-key")
    transport = FakeTransport(
        lambda **kwargs: (
            200,
            json.dumps(
                {
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    "choices": [{"message": {"role": "assistant", "content": "ok", "reasoning": "hidden"}}],
                }
            ).encode(),
        )
    )
    runtime = build_auxiliary_runtime(config, transport=transport)
    assert runtime.consolidation.propose(
        [{"role": "user", "content": "bounded input"}], remaining_seconds=2.0
    ) == "ok"


@pytest.mark.parametrize(
    "extra",
    [
        {"tool_calls": [{"id": "call-1"}]},
        {"function_call": {"name": "ensure_file"}},
    ],
)
def test_nonempty_tool_protocol_is_not_answer_text(tmp_path, monkeypatch, extra):
    config, _, _ = _runtime_config(tmp_path)
    monkeypatch.setenv("SCOPE_RECALL_TEST_CHAT_KEY", "test-key")
    message = {"role": "assistant", "content": "ok", **extra}
    transport = FakeTransport(
        lambda **kwargs: (
            200,
            json.dumps(
                {"usage": {"prompt_tokens": 1, "completion_tokens": 1}, "choices": [{"message": message}]}
            ).encode(),
        )
    )
    runtime = build_auxiliary_runtime(config, transport=transport)
    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert exc.value.error_type == "unsupported_response_shape"


def test_nonassistant_response_is_rejected(tmp_path, monkeypatch):
    config, _, _ = _runtime_config(tmp_path)
    monkeypatch.setenv("SCOPE_RECALL_TEST_CHAT_KEY", "test-key")
    transport = FakeTransport(
        lambda **kwargs: (
            200,
            json.dumps(
                {"usage": {"prompt_tokens": 1, "completion_tokens": 1}, "choices": [{"message": {"role": "user", "content": "ok"}}]}
            ).encode(),
        )
    )
    runtime = build_auxiliary_runtime(config, transport=transport)
    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert exc.value.error_type == "unsupported_response_shape"


def test_nonfinite_remaining_seconds_is_timeout(tmp_path, monkeypatch):
    config, _, _ = _runtime_config(tmp_path)
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "test-key")
    runtime = build_auxiliary_runtime(config, transport=FakeTransport(lambda **kwargs: (200, b"{}")))
    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.query_embedding.embed_query("hello", remaining_seconds=float("nan"))
    assert exc.value.error_type == "timeout"


def test_transport_timeout_is_reduced_after_local_setup(tmp_path, monkeypatch):
    config, _, _ = _runtime_config(tmp_path)
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "test-key")
    observed: dict[str, float] = {}

    def handler(**kwargs):
        observed["timeout_seconds"] = kwargs["timeout_seconds"]
        return (
            200,
            json.dumps(
                {
                    "embeddings": [{"values": _vector()}],
                    "usageMetadata": {"promptTokenCount": 1},
                }
            ).encode(),
        )

    runtime = build_auxiliary_runtime(config, transport=FakeTransport(handler))
    runtime.query_embedding.embed_query("hello", remaining_seconds=2.0)
    assert 0 < observed["timeout_seconds"] <= 2.0


def _reserve(ledger_path: Path, policy: BudgetPolicy, model: str, *, body: bytes = b"x") -> int:
    return AuxiliaryBudgetLedger(ledger_path, policy).reserve(
        model=model, body=body, reserved_input=1_000, reserved_output=100,
    )


def test_cumulative_caps_are_optional_and_zero_still_denies(tmp_path):
    """None switches a lifetime cap off; 0 keeps meaning "no spend at all".

    The four cumulative caps are lifetime totals over the whole ledger, so an
    enforced one eventually stops the derived layer for good. Opting out has to
    be expressible without turning the unconfigured default (0) into "allow
    everything", which would flip a fail-closed install to fail-open.
    """
    pricing = {"deepseek-v4-flash": ModelPricing(Decimal("0.44"), Decimal("1.32"))}
    def policy(**caps):
        base = dict(cap_micro_usd=None, total_input_cap=None,
                    total_output_cap=None, total_call_cap=None)
        base.update(caps)
        return BudgetPolicy(
            batch="TEST-CAPS", max_request_bytes=32_000, default_reserve_input=32_768,
            default_reserve_output=4_096, model_reserve_output={}, model_token_caps={},
            pricing=pricing, approved_models=frozenset(pricing), **base,
        )

    uncapped = policy()
    ledger = tmp_path / "uncapped.sqlite3"
    initialize_auxiliary_budget_ledger(ledger, uncapped)
    # Far past every number the old fixed caps used; none of them binds now.
    for _ in range(12):
        assert _reserve(ledger, uncapped, "deepseek-v4-flash") > 0

    # 0 is unchanged: an unconfigured policy still permits nothing.
    denied = policy(cap_micro_usd=0, total_call_cap=0, total_input_cap=0, total_output_cap=0)
    zero_ledger = tmp_path / "zero.sqlite3"
    initialize_auxiliary_budget_ledger(zero_ledger, denied)
    with pytest.raises(ValueError, match="budget_exhausted_or_meter_breach"):
        _reserve(zero_ledger, denied, "deepseek-v4-flash")

    # A stated positive cap still binds exactly as before.
    two = policy(total_call_cap=2)
    small = tmp_path / "two.sqlite3"
    initialize_auxiliary_budget_ledger(small, two)
    _reserve(small, two, "deepseek-v4-flash")
    _reserve(small, two, "deepseek-v4-flash")
    with pytest.raises(ValueError, match="budget_exhausted_or_meter_breach"):
        _reserve(small, two, "deepseek-v4-flash")


def test_uncapped_policy_still_refuses_a_metered_breach(tmp_path):
    """Removing volume caps must not remove the anomaly guard.

    meter_breach is the check that actually matters for a runaway: it fires when
    a recorded request is marked as having burned more than it reserved. It is
    independent of the cumulative caps and must keep failing closed when they
    are all None.
    """
    pricing = {"deepseek-v4-flash": ModelPricing(Decimal("0.44"), Decimal("1.32"))}
    uncapped = BudgetPolicy(
        batch="TEST-BREACH", cap_micro_usd=None, total_input_cap=None,
        total_output_cap=None, total_call_cap=None, max_request_bytes=32_000,
        default_reserve_input=32_768, default_reserve_output=4_096,
        model_reserve_output={}, model_token_caps={}, pricing=pricing,
        approved_models=frozenset(pricing),
    )
    ledger = tmp_path / "breach.sqlite3"
    initialize_auxiliary_budget_ledger(ledger, uncapped)
    assert _reserve(ledger, uncapped, "deepseek-v4-flash") > 0
    with sqlite3.connect(ledger) as db:
        db.execute("UPDATE requests SET status='meter_breach'")
    with pytest.raises(ValueError, match="budget_exhausted_or_meter_breach"):
        _reserve(ledger, uncapped, "deepseek-v4-flash")
