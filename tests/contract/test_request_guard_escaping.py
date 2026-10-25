"""The request guard, through the seam a worker uses: ``propose`` on both consolidation routes.

Each text a request carries is scanned once, in the form it was written in.  The first
repair of the escaped-line-break refusal was verified by calling the scanner on a string
and looked complete; the adapter scanned the serialised body as well, one layer of escaping
further in, and went on refusing every such request.  These tests go through the adapter.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from scope_recall.core.consolidate import candidate_evaluation_messages, consolidation_messages
from scope_recall.core.secret_patterns import contains_secret_like_text, structure_contains_secret_like_text
from scope_recall.runtime.models import AuxiliaryModelError
from test_candidate_evidence_window import _candidate, _source
from test_responses_consolidation import CREDENTIAL_ENV, MODEL, FakeTransport, _answer, _payload, _runtime

BACKSLASH = chr(92)
#: A source as it sits inside a consolidation request: a JSON document whose line breaks are
#: the two characters backslash and n, with a credential slot a template left empty.
TEMPLATE_IN_JSON = json.dumps(
    {"sources": [{"content": "飞书应用配置\nAppSecret:\nRedirect URL: https://example.invalid/cb"}]}, ensure_ascii=False
)
#: The same, from a tool output that was itself JSON holding JSON: the break is escaped twice.
TEMPLATE_IN_JSON_TWICE = json.dumps({"tool_output": TEMPLATE_IN_JSON}, ensure_ascii=False)
REAL_ASSIGNMENT = "deploy notes" + chr(10) + "password: hunter2-not-a-placeholder"


def _chat_answer(text: str = "{}") -> bytes:
    return json.dumps(
        {
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        }
    ).encode()


def _chat_payload(tmp_path, **route) -> dict:
    payload = _payload(tmp_path)
    payload["consolidation"] = {
        "model": MODEL,
        "endpoint": "https://api.deepseek.com/chat/completions",
        "credential_env": CREDENTIAL_ENV,
        "output_limit_field": "max_tokens",
        "max_output_tokens": 8192,
        **route,
    }
    return payload


ROUTES = {
    "chat": (_chat_payload, _chat_answer, lambda body: [m["content"] for m in body["messages"]]),
    "responses": (
        _payload,
        lambda text="{}": _answer(text),
        lambda body: [i["content"][0]["text"] for i in body["input"]],
    ),
}


@pytest.mark.parametrize("route", sorted(ROUTES))
@pytest.mark.parametrize("content", [TEMPLATE_IN_JSON, TEMPLATE_IN_JSON_TWICE], ids=["escaped_once", "escaped_twice"])
def test_an_empty_credential_slot_before_an_escaped_break_is_sent(tmp_path, monkeypatch, route, content):
    assert BACKSLASH + "nAppSecret:" + BACKSLASH in content, "the fixture holds the shape that was refused"
    build, answer, sent_contents = ROUTES[route]
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    transport = FakeTransport(lambda **kwargs: (200, answer()))
    runtime = _runtime(build(tmp_path), transport)

    assert (
        runtime.consolidation.propose(
            [{"role": "system", "content": "propose only"}, {"role": "user", "content": content}], remaining_seconds=2.0
        )
        == "{}"
    )

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


#: Sources capture admits whose JSON encoding in a request reads like a credential: a value the scan exempts only
#: before a space or the end of the text, at the end of a source, where the encoding puts the next field; and a
#: quoted placeholder, whose quotes the encoding escapes.
ADMITTED = {
    "value_at_the_end": "TEST note" + chr(10) + "api_key: null",
    "prose_at_the_end": "TEST note" + chr(10) + "the password is required.",
    "quoted_placeholder": "TEST config" + chr(10) + 'api_key: "<your-api-key>"' + chr(10) + "TEST more",
}


#: A request budget no slow runner runs out of: these tests check the guard, not the deadline.
BUDGET = 30.0


def _sent(tmp_path, monkeypatch, route, messages):
    build, answer, _contents = ROUTES[route]
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    transport = FakeTransport(lambda **kwargs: (200, answer()))
    runtime = _runtime(build(tmp_path), transport)
    assert runtime.consolidation.propose(messages, remaining_seconds=BUDGET) == "{}"
    return transport


@pytest.mark.parametrize("route", sorted(ROUTES))
@pytest.mark.parametrize("name", sorted(ADMITTED))
def test_a_source_capture_admits_is_sent_whatever_its_encoding_makes_of_it(tmp_path, monkeypatch, route, name):
    content = ADMITTED[name]
    assert not contains_secret_like_text(content), "capture admits the source as written"
    transport = _sent(tmp_path, monkeypatch, route, consolidation_messages([_source(content)]))
    assert len(transport.calls) == 1


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_a_candidate_is_read_as_the_fields_it_carries(tmp_path, monkeypatch, route):
    source = _source("TEST-build result ready")
    candidate = _candidate(source, [])
    candidate = replace(candidate, payload={**candidate.payload, "conditions": ["TEST deploy with api_key: null"]})
    transport = _sent(tmp_path, monkeypatch, route, candidate_evaluation_messages(candidate, (source,)))
    assert len(transport.calls) == 1


@pytest.mark.parametrize("route", sorted(ROUTES))
@pytest.mark.parametrize("label", ["password", "密码", "accessToken", "**Password**"])
def test_empty_credential_objects_keep_the_structured_policy_at_the_model_boundary(tmp_path, monkeypatch, route, label):
    value = {label: {}}
    assert not structure_contains_secret_like_text(value)
    transport = _sent(
        tmp_path, monkeypatch, route, [{"role": "user", "content": json.dumps(value, ensure_ascii=False)}]
    )
    assert len(transport.calls) == 1


def _candidate_with_a_secret_value():
    source = _source("TEST-build result ready")
    candidate = _candidate(source, [])
    candidate = replace(candidate, payload={**candidate.payload, "value_text": "password: hunter2-not-a-placeholder"})
    return candidate_evaluation_messages(candidate, (source,))


def _candidate_with_a_credential_predicate():
    source = _source("TEST-build result ready")
    candidate = _candidate(source, [])
    candidate = replace(candidate, payload={**candidate.payload, "predicate": "密码", "value_text": "Synthet1c"})
    return candidate_evaluation_messages(candidate, (source,))


def _user(content):
    return lambda: [{"role": "user", "content": content}]


REFUSED = {
    "in_a_field_beside_claim_fields": _user(
        '{"predicate":"state","value_text":"ready","claim_assignments":"password: Synthet1c"}'
    ),
    "in_related_candidate_fields": _candidate_with_a_credential_predicate,
    "in_nested_claim_fields": _user(json.dumps({"sources": [{"predicate": "密码", "value_text": "Synthet1c"}]})),
    "behind_duplicate_predicates": _user('{"predicate":"密码","predicate":"state","value_text":"Synthet1c"}'),
    "behind_duplicate_values": _user('{"predicate":"密码","value_text":"null","value_text":"Synthet1c"}'),
    "in_a_source": lambda: consolidation_messages([_source(REAL_ASSIGNMENT)]),
    "in_a_candidate": _candidate_with_a_secret_value,
    # A credential under a key that names it, whatever holds it, and a label as the key of what follows it.
    "under_a_key_naming_it": _user(json.dumps({"sources": [{"password": "hunter2-not-a-placeholder"}]})),
    "a_number_under_a_key_naming_it": _user(json.dumps({"password": 12345678})),
    "a_list_under_a_key_naming_it": _user(json.dumps({"password": ["hunter2-not-a-placeholder"]})),
    "after_a_label_naming_it": _user("password=" + json.dumps(["hunter2-not-a-placeholder"])),
    # The whole quoted value is the value: an exempt word before a space does not end it.
    "behind_an_exempt_word": _user(json.dumps({"password": "null hunter2-not-a-placeholder"})),
}


@pytest.mark.parametrize("route", sorted(ROUTES))
@pytest.mark.parametrize("where", sorted(REFUSED))
def test_a_secret_a_request_carries_is_refused_however_it_is_encoded(tmp_path, monkeypatch, route, where):
    build, answer, _sent_contents = ROUTES[route]
    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    transport = FakeTransport(lambda **kwargs: (200, answer()))
    runtime = _runtime(build(tmp_path), transport)
    with pytest.raises(AuxiliaryModelError, match="sensitive_request"):
        runtime.consolidation.propose(REFUSED[where](), remaining_seconds=BUDGET)
    assert transport.calls == []


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_a_content_nested_too_deep_to_read_as_json_is_read_as_text(tmp_path, monkeypatch, route):
    deep = "[" * 5000 + "0" + "]" * 5000
    transport = _sent(tmp_path, monkeypatch, route, [{"role": "user", "content": deep}])
    assert len(transport.calls) == 1


def test_the_body_gate_still_guards_what_is_not_a_content(tmp_path, monkeypatch):
    """Blanking the contents must not blank the gate: a route field that reads like a secret is refused."""
    from scope_recall.runtime import models

    monkeypatch.setenv(CREDENTIAL_ENV, "test-key")
    transport = FakeTransport(lambda **kwargs: (200, _chat_answer()))
    runtime = _runtime(_chat_payload(tmp_path), transport)
    adapter = runtime.consolidation
    original = adapter._chat_body

    def body_with_a_leaking_field(messages):
        body = json.loads(original(messages).decode("utf-8"))
        body["metadata"] = {"note": "password: hunter2-not-a-placeholder"}
        return models.json_bytes(body)

    monkeypatch.setattr(adapter, "_chat_body", body_with_a_leaking_field)
    with pytest.raises(AuxiliaryModelError, match="sensitive_request"):
        adapter.propose([{"role": "user", "content": "nothing sensitive here"}], remaining_seconds=2.0)
    assert transport.calls == []
