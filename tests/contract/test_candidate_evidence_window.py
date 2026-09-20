"""Synthetic regressions for candidate evidence selection, not admission."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core.candidate_lifecycle import (
    CandidateSnapshot, candidate_evaluation_messages, evidence_window,
)
from scope_recall.core.storage import StoredSource


def _source(content, *, ref="src:TEST-window", revision=1):
    return StoredSource(ref, revision, "TEST-scope", "TEST-session", None, None,
                        {"content": content, "origin": "tool_observation", "occurred_at": "2026-01-01T00:00:00Z"},
                        "TEST-hash", False)


def _candidate(source, spans):
    return CandidateSnapshot("clm:TEST-window", 1, source.scope_id, None, None, "proposed",
                             {"kind": "fact", "subject": "TEST-build", "predicate": "result",
                              "value_text": "ready", "evidence_spans": spans},
                             "pending_evaluation", "new_evidence", "r1-candidate-v1")


def _span(source, quote):
    return {"source_ref": source.ref, "source_revision": source.revision, "quote": quote}


def _record(candidate, source, **kwargs):
    messages = candidate_evaluation_messages(candidate, (source,), **kwargs)
    return json.loads(messages[2]["content"])["sources"][0]


def test_saved_exact_quote_wins_over_an_earlier_repeated_value():
    quote = "TEST-build result ready: verified by the synthetic check."
    source = _source("unrelated ready\n" + "x" * 4000 + quote + "z" * 4000)
    candidate = _candidate(source, [_span(source, quote)])
    record = _record(candidate, source)
    assert quote in record["content"]
    window = record["source_window"]
    assert source.event["content"][window["start"]:window["end"]] == record["content"]
    assert window["coverage"] == "fragment_only"
    assert window["total"] == len(source.event["content"])
    assert len(record["content"]) <= 3000 + len(quote)
    assert candidate.fact_state == "proposed"


@pytest.mark.parametrize("change", [
    {"source_ref": "src:TEST-other"}, {"source_revision": 2},
    {"quote": "not in the source"}, {"quote": "TEST build result ready"},
    {"quote": ""}, {"quote": None}, {"quote": 42},
])
def test_unverified_span_falls_back_to_the_existing_value_window(change):
    quote = "TEST-build result ready"
    source = _source("ready" + "x" * 4000 + quote + "z" * 4000)
    candidate = _candidate(source, [{**_span(source, quote), **change}])
    assert _record(candidate, source) == _record(_candidate(source, []), source)


@pytest.mark.parametrize("spans", [[], None, [None, {}, "invalid"]])
def test_missing_or_malformed_spans_keep_the_subject_fallback(spans):
    source = _source("x" * 4000 + "TEST-build" + "z" * 4000)
    record = _record(_candidate(source, spans), source)
    assert record["source_window"]["start"] == 2500
    assert "TEST-build" in record["content"]


def test_multiple_spans_choose_first_valid_quote_without_joining_distant_fragments():
    first, second = "TEST-build first result ready", "TEST-build second result ready"
    source = _source("x" * 4000 + first + "y" * 5000 + second + "z" * 4000)
    spans = [_span(source, "absent"), _span(source, first), _span(source, second)]
    record = _record(_candidate(source, spans), source)
    assert first in record["content"] and second not in record["content"]
    assert len(record["content"]) == 3000 + len(first)
    window = record["source_window"]
    assert record["content"] == source.event["content"][window["start"]:window["end"]]


def test_quote_matching_is_per_supplied_source_version():
    quote = "TEST-build result ready"
    content = "ready" + "x" * 4000 + quote + "z" * 4000
    source = _source(content)
    other = _source(content, ref="src:TEST-other")
    newer = _source(content, revision=2)
    candidate = _candidate(source, [_span(source, quote)])
    messages = candidate_evaluation_messages(candidate, (source, other, newer))
    records = json.loads(messages[2]["content"])["sources"]
    assert [record["ref"] for record in records] == [f"{s.ref}@{s.revision}" for s in (source, other, newer)]
    assert quote in records[0]["content"]
    assert all(quote not in record["content"] for record in records[1:])


def test_short_and_already_windowed_sources_are_not_rewindowed():
    short = _source("TEST-build result ready")
    assert evidence_window(short, ("ready",), [_span(short, "ready")]) is short
    long = _source("ready" + "x" * 5000)
    windowed = evidence_window(long, ("ready",))
    assert evidence_window(windowed, (), [_span(long, "x" * 20)]) is windowed


@pytest.mark.parametrize("at_end", [False, True])
def test_exact_unicode_quote_at_source_boundary_is_complete(at_end):
    quote = "TEST-build result ready：合成验证。"
    content = "x" * 5000 + quote if at_end else quote + "x" * 5000
    source = _source(content)
    record = _record(_candidate(source, [_span(source, quote)]), source)
    assert quote in record["content"]
    assert len(record["content"]) == 1500 + len(quote)


def test_whole_request_budget_still_counts_candidate_and_utf8_evidence():
    quote = "TEST-build result ready：" + "合成证据" * 500
    source = _source("ready" + "x" * 4000 + quote + "z" * 4000)
    candidate = _candidate(source, [_span(source, quote)])
    messages = candidate_evaluation_messages(candidate, (source,))
    size = sum(len(message["content"].encode("utf-8")) for message in messages)
    assert candidate_evaluation_messages(candidate, (source,), budget=size) == messages
    with pytest.raises(ContractError) as failure:
        candidate_evaluation_messages(candidate, (source,), budget=size - 1)
    assert failure.value.field == "consolidation_input_budget"


def test_nearby_saved_quotes_share_the_same_contiguous_window():
    first, second = "TEST first evidence", "TEST second evidence"
    source = _source("ready" + "x" * 4000 + first + " " * 50 + second + "z" * 4000)
    record = _record(_candidate(source, [_span(source, first), _span(source, second)]), source)
    assert first in record["content"] and second in record["content"]
    assert len(record["content"]) <= 3000 + len(first)


def test_oversized_saved_quote_does_not_bypass_the_default_budget():
    quote = "TEST synthetic " + "证据" * 12000
    source = _source("ready" + "x" * 4000 + quote)
    with pytest.raises(ContractError) as failure:
        candidate_evaluation_messages(_candidate(source, [_span(source, quote)]), (source,))
    assert failure.value.field == "consolidation_input_budget"


def test_window_selection_does_not_mutate_candidate_identity_or_source():
    quote = "TEST-build result ready"
    source = _source("ready" + "x" * 4000 + quote + "z" * 4000)
    candidate = _candidate(source, [_span(source, quote)])
    before = json.dumps(candidate.payload, sort_keys=True)
    messages = candidate_evaluation_messages(candidate, (source,))
    payload = json.loads(messages[-1]["content"].removeprefix("candidate="))
    for field in ("kind", "subject", "predicate", "value_text"):
        assert payload[field] == candidate.payload[field]
    assert json.dumps(candidate.payload, sort_keys=True) == before
    assert len(source.event["content"]) > 8000
    assert "Return zero claim_proposals when evidence is insufficient" in messages[0]["content"]
    # The model-safe principal rule also survives selecting a quote window.
    principal = "TEST-private-principal"
    identified = replace(source, event={**source.event, "source_principal": {
        "kind": "human", "resolution": "verified", "principal_ref": principal}})
    human = replace(candidate, payload={**candidate.payload, "subject": principal})
    messages = candidate_evaluation_messages(human, (identified,))
    assert principal not in "".join(message["content"] for message in messages)
    assert json.loads(messages[-1]["content"].removeprefix("candidate="))["subject"] == "current_user"
