"""Strict reflection synthesis parsing over a citation allowlist."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import scope_recall.reflection_tooling as reflection_tooling
from scope_recall.reflection import ReflectionEvidence, ReflectionEvidencePack
from scope_recall.reflection_llm import (
    ReflectionLLMError,
    ReflectionTransport,
    build_reflection_prompt,
    parse_reflection_response,
    synthesize_reflection,
)


def _pack() -> ReflectionEvidencePack:
    evidence = (
        ReflectionEvidence(
            evidence_id="claim:claim-current",
            kind="fact_current",
            memory_id="memory-current",
            claim_id="claim-current",
            scope_id="scope-a",
            content="Joy currently lives in Tokyo.",
            summary="joy lives in: Tokyo",
            source="fact-executor",
            target="user",
            score=0.98,
            updated_at="2026-07-14T00:00:00+00:00",
            metadata={"status": "current", "confidence": 0.98},
        ),
        ReflectionEvidence(
            evidence_id="memory:project-aurora",
            kind="memory",
            memory_id="project-aurora",
            claim_id=None,
            scope_id="scope-a",
            content="Project Aurora uses PostgreSQL.",
            summary="Aurora database choice",
            source="tool-store",
            target="project",
            score=0.91,
            updated_at="2026-07-13T00:00:00+00:00",
            metadata={"memory_type": "project"},
        ),
    )
    return ReflectionEvidencePack(
        query="What do we know about Joy and Aurora?",
        intent="current",
        evidence=evidence,
        char_count=600,
        trace={"write_delta": 0},
    )


def _valid_payload() -> dict[str, object]:
    return {
        "observations": [
            {
                "text": "Joy currently lives in Tokyo.",
                "citations": ["claim:claim-current"],
            }
        ],
        "inferences": [
            {
                "text": "Aurora is likely part of Joy's active work context.",
                "citations": ["memory:project-aurora", "claim:claim-current"],
            }
        ],
        "uncertainties": [
            {
                "text": "The evidence does not state Joy's role in Aurora.",
                "citations": ["memory:project-aurora"],
            }
        ],
        "answer": "Joy lives in Tokyo, and Aurora uses PostgreSQL; her exact Aurora role is unknown.",
        "citations": ["claim:claim-current", "memory:project-aurora"],
        "followup_queries": ["Joy role in Project Aurora"],
    }


def test_parse_reflection_response_accepts_valid_strict_json() -> None:
    result = parse_reflection_response(
        json.dumps(_valid_payload()),
        allowed_citations=_pack().citation_ids,
    )

    assert result.answer.startswith("Joy lives in Tokyo")
    assert result.citations == ("claim:claim-current", "memory:project-aurora")
    assert result.followup_queries == ("Joy role in Project Aurora",)
    assert result.observations[0].citations == ("claim:claim-current",)
    assert result.inferences[0].text.startswith("Aurora is likely")
    assert result.uncertainties[0].text.startswith("The evidence does not")


def test_synthesize_reflection_uses_injected_transport_once() -> None:
    calls: list[str] = []

    def transport(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(_valid_payload())

    result = synthesize_reflection(_pack(), transport=transport)

    assert result.answer
    assert len(calls) == 1
    assert "claim:claim-current" in calls[0]
    assert "memory:project-aurora" in calls[0]
    assert "Output one JSON object only" in calls[0]


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda payload: payload.update(citations=["claim:forged"]),
            "unknown citation",
        ),
        (
            lambda payload: payload["observations"][0].update(  # type: ignore[index,union-attr]
                citations=["memory:forged"]
            ),
            "unknown citation",
        ),
        (
            lambda payload: payload.update(extra="not allowed"),
            "top-level fields",
        ),
        (
            lambda payload: payload.update(followup_queries=["one", "two"]),
            "followup_queries",
        ),
        (
            lambda payload: payload["inferences"][0].update(citations=[]),  # type: ignore[index,union-attr]
            "citation",
        ),
    ],
)
def test_parse_reflection_response_fails_closed_on_contract_violations(
    mutator,
    message: str,
) -> None:
    payload = _valid_payload()
    mutator(payload)

    with pytest.raises(ReflectionLLMError, match=message):
        parse_reflection_response(
            json.dumps(payload),
            allowed_citations=_pack().citation_ids,
        )


def test_parse_reflection_response_rejects_markdown_fence_and_trailing_prose() -> None:
    raw = "```json\n" + json.dumps(_valid_payload()) + "\n```\nLooks good"

    with pytest.raises(ReflectionLLMError, match="strict JSON"):
        parse_reflection_response(raw, allowed_citations=_pack().citation_ids)


def test_reflection_response_rejects_duplicate_and_out_of_budget_fields() -> None:
    duplicate = _valid_payload()
    duplicate["citations"] = ["claim:claim-current", "claim:claim-current"]
    with pytest.raises(ReflectionLLMError, match="duplicate"):
        parse_reflection_response(
            json.dumps(duplicate),
            allowed_citations=_pack().citation_ids,
        )

    oversized = _valid_payload()
    oversized["answer"] = "x" * 8_001
    with pytest.raises(ReflectionLLMError, match="answer"):
        parse_reflection_response(
            json.dumps(oversized),
            allowed_citations=_pack().citation_ids,
        )


def test_empty_evidence_pack_does_not_call_transport() -> None:
    empty = ReflectionEvidencePack(
        query="unknown",
        intent="current",
        evidence=(),
        char_count=0,
        trace={"write_delta": 0},
    )
    called = False

    def transport(prompt: str) -> str:
        nonlocal called
        called = True
        return "{}"

    with pytest.raises(ReflectionLLMError, match="no evidence"):
        synthesize_reflection(empty, transport=transport)
    assert called is False


def test_reflection_transport_overrides_digest_system_prompt(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_call(prompt: str, **kwargs) -> str:
        captured["prompt"] = prompt
        captured.update(kwargs)
        return json.dumps(_valid_payload())

    monkeypatch.setattr("scope_recall.reflection_llm.call_llm_with_retries", fake_call)
    transport = ReflectionTransport(
        model="fixture-model",
        base_url="https://example.invalid",
        api_key="fixture-key",
    )

    transport("reflection prompt")

    assert captured["prompt"] == "reflection prompt"
    assert "grounded reflection" in str(captured["system_prompt"])
    assert "durable memory" not in str(captured["system_prompt"])


@pytest.mark.parametrize("raw_value", ("true", [False]))
def test_reflection_resolver_keeps_malformed_endpoint_opt_in_fail_closed(
    monkeypatch,
    tmp_path,
    raw_value,
) -> None:
    monkeypatch.setattr(
        reflection_tooling,
        "resolve_llm_config",
        lambda *_args, **_kwargs: {
            "model": "fixture-model",
            "base_url": "http://model.internal:1234",
            "api_key": "fixture-key",
            "api_mode": "chat_completions",
            "endpoint": "",
            "append_v1": True,
            "allow_insecure_endpoint": raw_value,
        },
    )
    provider = SimpleNamespace(_hermes_home=tmp_path)

    transport = reflection_tooling._resolve_transport(
        provider,
        {"provider": "fixture", "model": "fixture-model"},
    )

    assert isinstance(transport, ReflectionTransport)
    assert transport.allow_insecure_endpoint is False


def test_build_reflection_prompt_rejects_non_read_only_pack() -> None:
    pack = _pack()
    unsafe = ReflectionEvidencePack(
        query=pack.query,
        intent=pack.intent,
        evidence=pack.evidence,
        char_count=pack.char_count,
        trace={"write_delta": 1},
    )
    with pytest.raises(ReflectionLLMError, match="read-only"):
        build_reflection_prompt(unsafe)
