"""The request guard, through the seam a worker uses: ``propose`` on both consolidation routes.

Each text a request carries is scanned once, in the form it was written in.  The first
repair of the escaped-line-break refusal was verified by calling the scanner on a string
and looked complete; the adapter scanned the serialised body as well, one layer of escaping
further in, and went on refusing every such request.  These tests go through the adapter.
"""
from __future__ import annotations

import json

import pytest

from scope_recall.adapters.models import AuxiliaryModelError
from test_responses_consolidation import CREDENTIAL_ENV, MODEL, FakeTransport, _answer, _payload, _runtime

BACKSLASH = chr(92)
#: A source as it sits inside a consolidation request: a JSON document whose line breaks are
#: the two characters backslash and n, with a credential slot a template left empty.
TEMPLATE_IN_JSON = json.dumps({"sources": [{"content": "飞书应用配置\nAppSecret:\nRedirect URL: https://example.invalid/cb"}]},
                              ensure_ascii=False)
#: The same, from a tool output that was itself JSON holding JSON: the break is escaped twice.
TEMPLATE_IN_JSON_TWICE = json.dumps({"tool_output": TEMPLATE_IN_JSON}, ensure_ascii=False)
REAL_ASSIGNMENT = "deploy notes" + chr(10) + "password: hunter2-not-a-placeholder"


def _chat_answer(text: str = "{}") -> bytes:
    return json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 5},
                       "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}).encode()


def _chat_payload(tmp_path, **route) -> dict:
    payload = _payload(tmp_path)
    payload["consolidation"] = {"model": MODEL, "endpoint": "https://api.deepseek.com/chat/completions",
                                "credential_env": CREDENTIAL_ENV, "output_limit_field": "max_tokens",
                                "max_output_tokens": 8192, **route}
    return payload


ROUTES = {
    "chat": (_chat_payload, _chat_answer, lambda body: [m["content"] for m in body["messages"]]),
    "responses": (_payload, lambda text="{}": _answer(text), lambda body: [i["content"][0]["text"] for i in body["input"]]),
}


@pytest.mark.parametrize("route", sorted(ROUTES))
@pytest.mark.parametrize("content", [TEMPLATE_IN_JSON, TEMPLATE_IN_JSON_TWICE], ids=["escaped_once", "escaped_twice"])
def test_an_empty_credential_slot_before_an_escaped_break_is_sent(tmp_path, monkeypatch, route, content):
    assert BACKSLASH + "nAppSecret:" + BACKSLASH in content, "the fixture holds the shape that was refused"
    build, answer, sent_contents = ROUTES[route]
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    transport = FakeTransport(lambda **kwargs: (200, answer()))
    runtime = _runtime(build(tmp_path), transport)

    assert runtime.consolidation.propose([{"role": "system", "content": "propose only"}, {"role": "user", "content": content}],
                                         remaining_seconds=2.0) == "{}"

    assert len(transport.calls) == 1, "the request went out"
    assert sent_contents(transport.calls[0]["body"]) == ["propose only", content], "and went out exactly as written"


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_a_secret_in_a_content_is_refused_before_anything_is_sent(tmp_path, monkeypatch, route):
    build, answer, _sent = ROUTES[route]
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    transport = FakeTransport(lambda **kwargs: (200, answer()))
    runtime = _runtime(build(tmp_path), transport)

    with pytest.raises(AuxiliaryModelError, match="sensitive_request"):
        runtime.consolidation.propose([{"role": "user", "content": REAL_ASSIGNMENT}], remaining_seconds=2.0)
    assert transport.calls == []


def test_the_body_gate_still_guards_what_is_not_a_content(tmp_path, monkeypatch):
    """Blanking the contents must not blank the gate: a route field that reads like a secret is refused."""
    from scope_recall.adapters import models

    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    transport = FakeTransport(lambda **kwargs: (200, _chat_answer()))
    runtime = _runtime(_chat_payload(tmp_path), transport)
    adapter = runtime.consolidation
    original = adapter._chat_body

    def body_with_a_leaking_field(messages):
        body = json.loads(original(messages).decode("utf-8"))
        body["metadata"] = {"note": "password: hunter2-not-a-placeholder"}
        return models._json_bytes(body)

    monkeypatch.setattr(adapter, "_chat_body", body_with_a_leaking_field)
    with pytest.raises(AuxiliaryModelError, match="sensitive_request"):
        adapter.propose([{"role": "user", "content": "nothing sensitive here"}], remaining_seconds=2.0)
    assert transport.calls == []
