"""Contract tests for the Responses-API consolidation route (``openai_responses``).

The route exists so a consolidation model can be reached through an endpoint
that speaks the OpenAI Responses API rather than chat completions.  It targets
the documented DeepSeek ``POST https://api.deepseek.com/responses`` contract:
non-streaming, ``reasoning.effort``, ``text.format`` and the documented
``output``/``usage`` shapes.  Everything it shares with the chat-completions
route -- the one budget ledger, the bounded HTTP transport, the request deadline,
the response byte cap, usage settlement and conservative billing -- is asserted
here, because a second wire dialect that quietly stopped using the ledger would
be worse than no second dialect.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from scope_recall.adapters.models import (
    RESPONSES_KIND,
    AuxiliaryModelError,
    ConsolidationRouteConfig,
    ResponsesRouteConfig,
)
from scope_recall.contracts import ContractError
from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig, build_auxiliary_runtime
from scope_recall.runtime.model_budget import (
    BudgetPolicy,
    ModelPricing,
    initialize_auxiliary_budget_ledger,
)

MODEL = "deepseek-flash"
ENDPOINT = "https://api.deepseek.com/responses"
CREDENTIAL_ENV = "SCOPE_RECALL_TEST_RESPONSES_KEY"


def _budget(*, cap_micro_usd: int = 2_000_000, total_call_cap: int = 8) -> BudgetPolicy:
    pricing = {MODEL: ModelPricing(Decimal("0.28"), Decimal("0.42"))}
    return BudgetPolicy(
        batch="TEST-RESPONSES-ROUTE",
        cap_micro_usd=cap_micro_usd,
        total_input_cap=64_000_000,
        total_output_cap=8_000_000,
        total_call_cap=total_call_cap,
        max_request_bytes=32_000,
        default_reserve_input=32_768,
        default_reserve_output=4_096,
        model_reserve_output={},
        model_token_caps={},
        pricing=pricing,
        approved_models=frozenset(pricing),
    )


def _route_overrides(**overrides) -> dict:
    route = {
        "kind": RESPONSES_KIND,
        "model": MODEL,
        "endpoint": ENDPOINT,
        "credential_env": CREDENTIAL_ENV,
        "max_output_tokens": 8192,
        "reasoning_effort": "none",
        "text_format": {"type": "json_object"},
        "stream": False,
    }
    route.update(overrides)
    return route


def _payload(tmp_path: Path, *, budget: BudgetPolicy | None = None, **route_overrides) -> dict:
    budget = budget or _budget()
    ledger = tmp_path / "auxiliary-budget.sqlite3"
    initialize_auxiliary_budget_ledger(ledger, budget)
    return {
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
                for model, rates in budget.pricing.items()
            },
        },
        "consolidation": _route_overrides(**route_overrides),
    }


class FakeTransport:
    """The one port the adapter may use; every call is captured, never sent."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []

    def post(self, url, *, body, headers, timeout_seconds, max_response_bytes):
        call = {
            "url": url,
            "body": json.loads(body.decode("utf-8")),
            "headers": dict(headers),
            "timeout_seconds": timeout_seconds,
            "max_response_bytes": max_response_bytes,
        }
        self.calls.append(call)
        return self.handler(**call)


def _runtime(payload: dict, transport: FakeTransport):
    return build_auxiliary_runtime(AuxiliaryRuntimeConfig.from_mapping(payload), transport=transport)


def _rows(ledger: Path) -> list[dict]:
    with sqlite3.connect(ledger) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM requests ORDER BY id")]


def _answer(text: str = '{"protocol_version":"1.1"}', *, status: str = "completed",
            usage: dict | None = None, reasoning: bool = False) -> bytes:
    output = []
    if reasoning:
        output.append({
            "type": "reasoning",
            "id": "rs_1",
            "status": "completed",
            "content": [{"type": "reasoning_text", "text": "thinking nobody asked to keep"}],
            "summary": [],
        })
    output.append({
        "type": "message",
        "id": "msg_1",
        "status": "completed" if status != "incomplete" else "incomplete",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    })
    payload = {"id": "resp_1", "object": "response", "status": status, "output": output, "store": False}
    payload["usage"] = usage if usage is not None else {
        "input_tokens": 22,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 29,
        "output_tokens_details": {"reasoning_tokens": 27},
        "total_tokens": 51,
    }
    return json.dumps(payload).encode()


# ---------------------------------------------------------------------------
# Request construction: instructions, item order, roles, and the explicit
# non-streaming store-less shape
# ---------------------------------------------------------------------------

def test_responses_route_sends_the_documented_non_streaming_request(tmp_path, monkeypatch):
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    transport = FakeTransport(lambda **kwargs: (200, _answer("{}")))
    runtime = _runtime(_payload(tmp_path), transport)

    assert runtime.consolidation.propose(
        [{"role": "system", "content": "propose only"}, {"role": "user", "content": "bounded input"}],
        remaining_seconds=2.0,
    ) == "{}"

    call = transport.calls[0]
    assert call["url"] == ENDPOINT
    assert call["headers"]["Authorization"] == "Bearer test-key"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["body"] == {
        "model": MODEL,
        "input": [{"type": "message", "role": "system",
                   "content": [{"type": "input_text", "text": "propose only"}]},
                  {"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": "bounded input"}]}],
        "max_output_tokens": 8192,
        "stream": False,
        "store": False,
        "reasoning": {"effort": "none"},
        "text": {"format": {"type": "json_object"}},
    }


def test_responses_input_keeps_message_order_and_role_semantics(tmp_path, monkeypatch):
    """All messages keep their positions, including an interleaved system item."""
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    transport = FakeTransport(lambda **kwargs: (200, _answer("{}")))
    runtime = _runtime(_payload(tmp_path), transport)

    runtime.consolidation.propose(
        [
            {"role": "system", "content": "first instruction"},
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "system", "content": "second instruction"},
            {"role": "user", "content": "three"},
        ],
        remaining_seconds=2.0,
    )

    body = transport.calls[0]["body"]
    assert "instructions" not in body
    assert [(item["role"], item["content"][0]["type"], item["content"][0]["text"]) for item in body["input"]] == [
        ("system", "input_text", "first instruction"),
        ("user", "input_text", "one"),
        ("assistant", "output_text", "two"),
        ("system", "input_text", "second instruction"),
        ("user", "input_text", "three"),
    ]
    assert all(item["type"] == "message" for item in body["input"])


def test_responses_route_preserves_a_system_only_input(tmp_path, monkeypatch):
    """A system-only request still carries an explicit, nonempty input list."""
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    transport = FakeTransport(lambda **kwargs: (200, _answer("{}")))
    runtime = _runtime(_payload(tmp_path), transport)

    runtime.consolidation.propose([{"role": "system", "content": "only instructions"}], remaining_seconds=2.0)

    body = transport.calls[0]["body"]
    assert "instructions" not in body
    assert body["input"] == [{"type": "message", "role": "system",
                              "content": [{"type": "input_text", "text": "only instructions"}]}]


def test_responses_route_reuses_the_deadline_and_response_cap(tmp_path, monkeypatch):
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    observed: dict = {}

    def handler(**kwargs):
        observed.update(kwargs)
        return 200, _answer("{}")

    runtime = _runtime(_payload(tmp_path), FakeTransport(handler))
    runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)

    assert 0 < observed["timeout_seconds"] <= 2.0
    assert observed["max_response_bytes"] == 1_048_576


@pytest.mark.parametrize("messages,error_type", [
    ([{"role": "tool", "content": "tool output has no Responses item shape here"}], "input_invalid"),
    ([{"role": "user", "content": "ok", "name": "extra"}], "input_invalid"),
    ([], "input_invalid"),
])
def test_responses_route_accepts_only_its_closed_message_shape(tmp_path, monkeypatch, messages, error_type):
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    transport = FakeTransport(lambda **kwargs: (200, _answer("{}")))
    runtime = _runtime(_payload(tmp_path), transport)
    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.consolidation.propose(messages, remaining_seconds=2.0)
    assert exc.value.error_type == error_type
    assert transport.calls == []


# ---------------------------------------------------------------------------
# Strict configuration: the route refuses what it cannot honestly serve
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("overrides,failure", [
    ({"stream": True}, "stream"),
    ({"stream": "false"}, "stream"),
    ({"reasoning_effort": "medium"}, "reasoning_effort"),
    ({"reasoning_effort": "xhigh"}, "reasoning_effort"),
    ({"reasoning_effort": 4}, "reasoning_effort"),
    ({"text_format": {"type": "text"}}, "text_format"),
    ({"text_format": {"type": "json_schema", "name": "p", "schema": {}}}, "text_format"),
    ({"text_format": ["json_object"]}, "text_format"),
    ({"max_output_tokens": 0}, "max_output_tokens"),
    ({"max_output_tokens": 131_073}, "max_output_tokens"),
    ({"max_output_tokens": True}, "max_output_tokens"),
    ({"max_output_tokens": "8192"}, "max_output_tokens"),
    ({"model": ""}, "model"),
    ({"endpoint": "http://api.deepseek.com/responses"}, "endpoint"),
    ({"endpoint": ""}, "endpoint"),
    ({"credential_env": "lowercase_name"}, "credential_env"),
    ({"thinking": {"type": "disabled"}}, "consolidation_unknown_config"),
])
def test_responses_route_refuses_configuration_it_cannot_serve(tmp_path, overrides, failure):
    """A route that silently ignored ``stream: true`` would buy a streamed body
    with a non-streaming reader; an unknown key would be a setting nobody reads."""
    with pytest.raises(ValueError) as exc:
        AuxiliaryRuntimeConfig.from_mapping(_payload(tmp_path, **overrides))
    assert str(exc.value) == failure


def test_responses_route_requires_the_output_limit_and_a_named_kind(tmp_path):
    with pytest.raises(ValueError) as exc:
        AuxiliaryRuntimeConfig.from_mapping(_payload(tmp_path, max_output_tokens=None))
    assert str(exc.value) == "max_output_tokens"
    base = dict(model=MODEL, endpoint=ENDPOINT, credential_env=CREDENTIAL_ENV, max_output_tokens=8192)
    with pytest.raises(ValueError) as exc:
        ResponsesRouteConfig(**base, kind="responses")
    assert str(exc.value) == "kind"
    assert ResponsesRouteConfig(**base).kind == RESPONSES_KIND


def test_an_unstated_kind_still_selects_the_chat_completions_route(tmp_path):
    payload = _payload(tmp_path)
    payload["consolidation"] = {
        "model": MODEL,
        "endpoint": "https://api.deepseek.com/chat/completions",
        "credential_env": CREDENTIAL_ENV,
        "output_limit_field": "max_tokens",
        "max_output_tokens": 8192,
    }
    config = AuxiliaryRuntimeConfig.from_mapping(payload)
    assert isinstance(config.consolidation, ConsolidationRouteConfig)
    assert not isinstance(config.consolidation, ResponsesRouteConfig)


@pytest.mark.parametrize("kind", ["responses", "openai-response", "OPENAI_RESPONSES"])
def test_an_unrelated_kind_is_still_refused(tmp_path, kind):
    with pytest.raises(ValueError) as exc:
        AuxiliaryRuntimeConfig.from_mapping(_payload(tmp_path, kind=kind))
    assert str(exc.value) == "consolidation_kind"


# ---------------------------------------------------------------------------
# The answer: only a completed assistant ``output_text`` is one
# ---------------------------------------------------------------------------

def test_responses_answer_is_the_completed_assistant_output_text(tmp_path, monkeypatch):
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    transport = FakeTransport(lambda **kwargs: (200, _answer('{"claims":[]}', reasoning=True)))
    runtime = _runtime(_payload(tmp_path), transport)

    assert runtime.consolidation.propose(
        [{"role": "user", "content": "bounded input"}], remaining_seconds=2.0
    ) == '{"claims":[]}'


def test_responses_route_joins_parts_within_a_message_and_separates_messages(tmp_path, monkeypatch):
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    payload = {
        "status": "completed",
        "output": [
            {"type": "message", "id": "msg_1", "status": "completed", "role": "assistant",
             "content": [{"type": "output_text", "text": '{"claims":["a"]', "annotations": []},
                         {"type": "output_text", "text": "}"}]},
            {"type": "message", "id": "msg_2", "status": "completed", "role": "assistant",
             "content": [{"type": "output_text", "text": "trailing"}]},
        ],
        "usage": {"input_tokens": 3, "output_tokens": 4},
    }
    transport = FakeTransport(lambda **kwargs: (200, json.dumps(payload).encode()))
    runtime = _runtime(_payload(tmp_path), transport)

    assert runtime.consolidation.propose(
        [{"role": "user", "content": "bounded input"}], remaining_seconds=2.0
    ) == '{"claims":["a"]}\n\ntrailing'


def test_responses_incomplete_is_the_named_truncated_derivation(tmp_path, monkeypatch):
    """A response cut off at ``max_output_tokens`` is a prefix, not an answer;
    the spent call is still settled with the usage the provider reported."""
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    usage = {"input_tokens": 40, "output_tokens": 512, "output_tokens_details": {"reasoning_tokens": 480}}
    key = '{"claims":["partial"'
    transport = FakeTransport(lambda **kwargs: (200, _answer(key, status="incomplete", usage=usage)))
    _payload_of, ledger = _payload(tmp_path), tmp_path / "auxiliary-budget.sqlite3"
    runtime = _runtime(_payload_of, transport)

    with pytest.raises(ContractError) as exc:
        runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert (exc.value.code, exc.value.field) == ("DERIVATION_INVALID", "model_output_truncated")
    assert [(row["status"], row["actual_input"], row["actual_output"]) for row in _rows(ledger)] == [
        ("http_200", 40, 512)
    ]


@pytest.mark.parametrize("payload_status,error", [
    ("failed", "response_status_failed"),
    ("in_progress", "unsupported_response_shape"),
    (None, "unsupported_response_shape"),
])
def test_non_completed_response_status_is_not_an_answer(tmp_path, monkeypatch, payload_status, error):
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    body = json.loads(_answer("{}").decode())
    body["status"] = payload_status
    transport = FakeTransport(lambda **kwargs: (200, json.dumps(body).encode()))
    ledger = tmp_path / "auxiliary-budget.sqlite3"
    runtime = _runtime(_payload(tmp_path), transport)

    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert exc.value.error_type == error
    assert _rows(ledger)[0]["status"] == "http_200"


def test_responses_refusal_part_is_not_an_answer(tmp_path, monkeypatch):
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    body = json.loads(_answer("{}").decode())
    body["output"][0]["content"] = [{"type": "refusal", "refusal": "cannot propose this"}]
    transport = FakeTransport(lambda **kwargs: (200, json.dumps(body).encode()))
    runtime = _runtime(_payload(tmp_path), transport)

    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert exc.value.error_type == "model_refused"


@pytest.mark.parametrize("output", [
    [{"type": "reasoning", "id": "rs_1", "status": "completed",
      "content": [{"type": "reasoning_text", "text": "only thinking was produced"}], "summary": []}],
    [{"type": "message", "id": "msg_1", "status": "completed", "role": "assistant", "content": []}],
    [{"type": "message", "id": "msg_1", "status": "completed", "role": "assistant",
      "content": [{"type": "output_text", "text": ""}]}],
    [],
])
def test_responses_without_answer_text_is_not_success(tmp_path, monkeypatch, output):
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    body = json.loads(_answer("{}").decode())
    body["output"] = output
    transport = FakeTransport(lambda **kwargs: (200, json.dumps(body).encode()))
    runtime = _runtime(_payload(tmp_path), transport)

    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert exc.value.error_type == "empty_output"


@pytest.mark.parametrize("item", [
    {"type": "function_call", "id": "fc_1", "status": "completed", "call_id": "fc_1", "name": "x", "arguments": "{}"},
    {"type": "web_search_call", "id": "ws_1", "status": "completed"},
])
def test_responses_tool_protocol_is_not_answer_text(tmp_path, monkeypatch, item):
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    body = json.loads(_answer("{}").decode())
    body["output"].insert(0, item)
    transport = FakeTransport(lambda **kwargs: (200, json.dumps(body).encode()))
    runtime = _runtime(_payload(tmp_path), transport)

    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert exc.value.error_type == "unsupported_response_shape"


def test_responses_user_message_in_output_is_not_answer_text(tmp_path, monkeypatch):
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    body = json.loads(_answer("{}").decode())
    body["output"][0]["role"] = "user"
    transport = FakeTransport(lambda **kwargs: (200, json.dumps(body).encode()))
    runtime = _runtime(_payload(tmp_path), transport)

    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert exc.value.error_type == "unsupported_response_shape"


def test_an_answer_item_marked_unfinished_contradicts_a_completed_response(tmp_path, monkeypatch):
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    body = json.loads(_answer("{}").decode())
    body["output"][0]["status"] = "incomplete"
    transport = FakeTransport(lambda **kwargs: (200, json.dumps(body).encode()))
    runtime = _runtime(_payload(tmp_path), transport)

    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert exc.value.error_type == "unsupported_response_shape"


def test_responses_route_returns_raw_content_without_json_repair(tmp_path, monkeypatch):
    """The proposal is judged by the existing validator, not by this adapter."""
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    transport = FakeTransport(lambda **kwargs: (200, _answer('{not valid json')))
    runtime = _runtime(_payload(tmp_path), transport)

    assert runtime.consolidation.propose(
        [{"role": "user", "content": "bounded input"}], remaining_seconds=2.0
    ) == "{not valid json"


# ---------------------------------------------------------------------------
# Usage and money: the same ledger, the same settlement, no double counting
# ---------------------------------------------------------------------------

def test_responses_usage_is_recorded_and_charged_without_billing_reasoning_twice(tmp_path, monkeypatch):
    """DeepSeek counts reasoning inside ``output_tokens``, so a thinking call is
    charged what it reported -- the chat route's "output billed outside
    completion_tokens" guard must not add those tokens again."""
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    budget = _budget()
    usage = {
        "input_tokens": 1000,
        "input_tokens_details": {"cached_tokens": 640},
        "output_tokens": 500,
        "output_tokens_details": {"reasoning_tokens": 400},
        "total_tokens": 1500,
    }
    transport = FakeTransport(lambda **kwargs: (200, _answer("{}", usage=usage)))
    ledger = tmp_path / "auxiliary-budget.sqlite3"
    runtime = _runtime(_payload(tmp_path, budget=budget), transport)

    runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)

    row = _rows(ledger)[0]
    assert (row["status"], row["actual_input"], row["actual_output"]) == ("http_200", 1000, 500)
    assert row["cached_input"] == 640
    assert row["unreported_output"] is None
    assert row["charge_micro_usd"] == budget.pricing[MODEL].charge_micro_usd(1000, 500)


@pytest.mark.parametrize("usage", [
    None,
    {},
    {"input_tokens": 10},
    {"input_tokens": 10.5, "output_tokens": 5},
    {"input_tokens": -1, "output_tokens": 5},
    {"input_tokens": "10", "output_tokens": 5},
])
def test_responses_missing_usage_keeps_the_reserved_charge(tmp_path, monkeypatch, usage):
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    budget = _budget()
    body = json.loads(_answer("{}").decode())
    if usage is None:
        body.pop("usage")
    else:
        body["usage"] = usage
    transport = FakeTransport(lambda **kwargs: (200, json.dumps(body).encode()))
    ledger = tmp_path / "auxiliary-budget.sqlite3"
    runtime = _runtime(_payload(tmp_path, budget=budget), transport)

    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert exc.value.error_type == "missing_usage"
    row = _rows(ledger)[0]
    assert row["status"] == "http_200_usage_unknown_reserved_charge_retained"
    assert (row["actual_input"], row["actual_output"]) == (None, None)
    # The reserved pair is what stays charged: the configured input floor plus
    # the route's output limit, priced exactly as the ledger reserved it.
    assert row["reserved_output"] == 8192
    assert row["reserved_input"] >= 32_768
    assert row["charge_micro_usd"] == budget.pricing[MODEL].charge_micro_usd(
        row["reserved_input"], row["reserved_output"]
    )


def test_a_cache_count_that_cannot_be_true_is_not_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    usage = {"input_tokens": 100, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 101}}
    transport = FakeTransport(lambda **kwargs: (200, _answer("{}", usage=usage)))
    ledger = tmp_path / "auxiliary-budget.sqlite3"
    runtime = _runtime(_payload(tmp_path), transport)

    runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)

    assert _rows(ledger)[0]["cached_input"] is None


def test_responses_http_200_error_body_is_billed_conservatively(tmp_path, monkeypatch):
    """A 200 that carries an error object instead of an answer is not a success
    and not a free call: the reservation stays charged."""
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    budget = _budget()
    transport = FakeTransport(
        lambda **kwargs: (200, json.dumps({"error": {"message": "upstream unavailable"}}).encode())
    )
    ledger = tmp_path / "auxiliary-budget.sqlite3"
    runtime = _runtime(_payload(tmp_path, budget=budget), transport)

    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert exc.value.error_type == "unsupported_response_shape"
    row = _rows(ledger)[0]
    assert row["status"] == "http_200_usage_unknown_reserved_charge_retained"
    assert row["charge_micro_usd"] > 0


def test_responses_provider_refusal_is_settled_by_its_own_code(tmp_path, monkeypatch):
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    transport = FakeTransport(
        lambda **kwargs: (429, json.dumps({"error": {"type": "RateLimitError", "message": "slow down"}}).encode())
    )
    ledger = tmp_path / "auxiliary-budget.sqlite3"
    runtime = _runtime(_payload(tmp_path), transport)

    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert (exc.value.error_type, exc.value.detail) == ("http_status", "429")
    # The provider's own symbol is kept; the reserved charge is retained behind
    # it, which is the suffix every unsettled-without-usage row carries.
    row = _rows(ledger)[0]
    assert row["status"].startswith("http_429:RateLimitError")
    assert row["charge_micro_usd"] > 0


def test_responses_reservation_precedes_the_network_call(tmp_path, monkeypatch):
    """The row is committed before the request leaves the process, so a crash
    between the two retains the reserved charge instead of losing it."""
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    ledger = tmp_path / "auxiliary-budget.sqlite3"
    seen: list[dict] = []
    sent: dict = {}

    def handler(**kwargs):
        sent["body"] = kwargs["body"]
        with sqlite3.connect(f"file:{ledger.as_posix()}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            seen.extend(dict(row) for row in db.execute("SELECT * FROM requests"))
        return 200, _answer("{}")

    runtime = _runtime(_payload(tmp_path), FakeTransport(handler))
    runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)

    assert [(row["status"], row["model"]) for row in seen] == [("reserved_before_network", MODEL)]
    assert seen[0]["reserved_output"] == 8192
    # The FakeTransport hands the parsed body on, and the adapter's own
    # serializer is a plain canonical dump, so the ledger's digest of the exact
    # bytes that were sent can be recomputed here.
    canonical = json.dumps(sent["body"], ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    assert seen[0]["request_bytes"] == len(canonical)
    assert seen[0]["body_sha256"] == hashlib.sha256(canonical).hexdigest()


def test_responses_route_spends_nothing_without_a_credential(tmp_path, monkeypatch):
    monkeypatch.delenv(CREDENTIAL_ENV, raising=False)
    transport = FakeTransport(lambda **kwargs: (200, _answer("{}")))
    ledger = tmp_path / "auxiliary-budget.sqlite3"
    runtime = _runtime(_payload(tmp_path), transport)

    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert exc.value.error_type == "credential_missing"
    assert transport.calls == []
    assert _rows(ledger) == []


def test_responses_route_refuses_an_unapproved_model_without_spending(tmp_path, monkeypatch):
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    transport = FakeTransport(lambda **kwargs: (200, _answer("{}")))
    ledger = tmp_path / "auxiliary-budget.sqlite3"
    runtime = _runtime(_payload(tmp_path, model="deepseek-v4-pro"), transport)

    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert exc.value.error_type == "budget_unavailable"
    assert transport.calls == []
    assert _rows(ledger) == []


def test_responses_route_shares_the_ledger_cap_of_the_installation(tmp_path, monkeypatch):
    """A second request against the same ledger sees the first one's spend."""
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    budget = _budget(total_call_cap=1)
    ledger = tmp_path / "auxiliary-budget.sqlite3"
    transport = FakeTransport(lambda **kwargs: (200, _answer("{}")))
    config = AuxiliaryRuntimeConfig.from_mapping(_payload(tmp_path, budget=budget))

    first = build_auxiliary_runtime(config, transport=transport)
    first.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert len(_rows(ledger)) == 1

    second = build_auxiliary_runtime(config, transport=transport)
    with pytest.raises(AuxiliaryModelError) as exc:
        second.consolidation.propose([{"role": "user", "content": "bounded input"}], remaining_seconds=2.0)
    assert exc.value.error_type == "budget_exhausted"
    assert len(_rows(ledger)) == 1
    assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# The chat-completions and codex_cli routes are untouched
# ---------------------------------------------------------------------------

def test_the_chat_completions_route_still_sends_its_own_body(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOPE_RECALL_TEST_CHAT_KEY", "test-key")
    budget = _budget()
    ledger = tmp_path / "auxiliary-budget.sqlite3"
    initialize_auxiliary_budget_ledger(ledger, budget)
    payload = {
        "external_embedding": False,
        "external_consolidation": True,
        "ledger_path": str(ledger),
        "budget": _payload(tmp_path, budget=budget)["budget"],
        "consolidation": {
            "model": MODEL,
            "endpoint": "https://api.deepseek.com/chat/completions",
            "credential_env": "SCOPE_RECALL_TEST_CHAT_KEY",
            "output_limit_field": "max_tokens",
            "max_output_tokens": 8192,
            "response_format": {"type": "json_object"},
        },
    }
    transport = FakeTransport(lambda **kwargs: (200, json.dumps({
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "choices": [{"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}],
    }).encode()))
    runtime = _runtime(payload, transport)

    assert runtime.consolidation.propose(
        [{"role": "system", "content": "propose only"}, {"role": "user", "content": "bounded input"}],
        remaining_seconds=2.0,
    ) == "{}"

    call = transport.calls[0]
    assert call["url"] == "https://api.deepseek.com/chat/completions"
    assert set(call["body"]) == {"model", "messages", "stream", "n", "max_tokens", "response_format"}
    assert call["body"]["messages"][0] == {"role": "system", "content": "propose only"}

