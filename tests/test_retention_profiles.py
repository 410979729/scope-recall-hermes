"""Tests for configurable semantic retention detail without transcript duplication."""

from __future__ import annotations

from types import SimpleNamespace

import scope_recall.capture_llm as capture_llm
from scope_recall.nightly_digest import build_prompt
from scope_recall.retention_profiles import (
    normalize_retention_profile,
    retention_profile_instruction,
)


def test_retention_profiles_are_distinct_and_invalid_values_fail_to_balanced() -> None:
    light = retention_profile_instruction("light")
    balanced = retention_profile_instruction("balanced")
    full = retention_profile_instruction("full")

    assert normalize_retention_profile("LIGHT") == "light"
    assert normalize_retention_profile("unknown") == "balanced"
    assert len({light, balanced, full}) == 3
    assert "minimal durable facts" in light
    assert "reasoning and reusable steps" in balanced
    assert (
        "decision rationale, alternatives, corrections, ordered steps, and verification context"
        in full
    )
    assert "Never copy the full transcript into durable memory" in full


def test_nightly_prompt_applies_requested_retention_profile() -> None:
    bundle = SimpleNamespace(is_task=True)

    prompt = build_prompt(
        bundle,
        "[message_id=1 role=user] preserve the rationale",
        [],
        retention_profile="full",
    )

    assert "Retention profile: full" in prompt
    assert (
        "decision rationale, alternatives, corrections, ordered steps, and verification context"
        in prompt
    )


def test_per_turn_capture_uses_journal_retention_profile(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_call(
        base_url,
        api_key,
        model,
        messages,
        max_tokens,
        timeout,
        **kwargs,
    ):
        del base_url, api_key, model, max_tokens, timeout, kwargs
        captured["messages"] = messages
        return "[]"

    monkeypatch.setattr(capture_llm, "_call_openai_compatible", fake_call)

    candidates = capture_llm.extract_capture_candidates(
        "Remember why the deployment choice was made.",
        "The durable rationale and verification steps were recorded.",
        {
            "capture_llm": {"enabled": True, "api_key": "test-only"},
            "journal": {"retention_profile": "full"},
        },
    )

    assert candidates == []
    messages = captured["messages"]
    assert isinstance(messages, list)
    system_prompt = messages[0]["content"]
    assert "Retention profile: full" in system_prompt
    assert "Never copy the full transcript into durable memory" in system_prompt


def test_per_turn_capture_sanitizes_external_llm_prompt(monkeypatch) -> None:
    """The external extraction boundary must not receive secrets or private paths."""

    captured: dict[str, object] = {}

    def fake_call(
        base_url,
        api_key,
        model,
        messages,
        max_tokens,
        timeout,
        **kwargs,
    ):
        del base_url, api_key, model, max_tokens, timeout, kwargs
        captured["messages"] = messages
        return "[]"

    monkeypatch.setattr(capture_llm, "_call_openai_compatible", fake_call)

    capture_llm.extract_capture_candidates(
        "Keep api_key=not-a-real-secret at /home/operator/private/model.",
        r"Verified from C:\Users\operator\private\receipt.txt.",
        {"capture_llm": {"enabled": True, "api_key": "test-only"}},
    )

    messages = captured["messages"]
    assert isinstance(messages, list)
    prompt = messages[1]["content"]
    assert "not-a-real-secret" not in prompt
    assert "/home/operator" not in prompt
    assert r"C:\Users\operator" not in prompt
    assert "[REDACTED_SECRET]" in prompt
    assert "[REDACTED_PATH]" in prompt


def test_capture_prompt_sanitization_is_bounded_across_send_cutoff() -> None:
    raw = (
        "x" * 2480
        + " api_key=not-a-real-secret "
        + "y" * 100_000
    )

    block = capture_llm._capture_prompt_block(raw)

    assert len(block) <= 2500
    assert "not-a-real-secret" not in block
    assert "[REDACTED_SECRET]" in block


def test_capture_prompt_redacts_unterminated_private_key_beyond_lookahead() -> None:
    """A private-key opener before the send cap must fail closed without END."""

    raw = (
        "x" * 2470
        + "\n-----BEGIN PRIVATE KEY-----\n"
        + "synthetic-key-material-" * 500
        + "\n-----END PRIVATE KEY-----"
    )

    block = capture_llm._capture_prompt_block(raw)

    assert len(block) <= 2500
    assert "-----BEGIN PRIVATE KEY-----" not in block
    assert "A" * 64 not in block
    assert "[REDACTED_SECRET]" in block

    pgp_begin = "-----BEGIN " + "PGP PRIVATE KEY BLOCK-----"
    pgp_block = capture_llm._capture_prompt_block(
        ("y" * 2470) + pgp_begin + ("B" * 5000)
    )
    assert pgp_begin not in pgp_block
    assert "B" * 64 not in pgp_block
    assert "[REDACTED_SECRET]" in pgp_block


def test_per_turn_capture_rejects_noncompliant_verbatim_transcript(monkeypatch) -> None:
    """Prompt instructions are advisory; the returned candidate needs a hard gate."""

    transcript = " ".join(
        f"neutral transcript sentence {index} records context and rationale."
        for index in range(80)
    )

    def fake_call(*_args, **_kwargs):
        import json

        return json.dumps(
            [
                {
                    "action": "store",
                    "content": transcript,
                    "target": "memory",
                    "memory_type": "summary",
                }
            ]
        )

    monkeypatch.setattr(capture_llm, "_call_openai_compatible", fake_call)

    candidates = capture_llm.extract_capture_candidates(
        transcript,
        "A short assistant acknowledgement that adds no durable detail.",
        {"capture_llm": {"enabled": True, "api_key": "test-only"}},
    )

    assert candidates == []
