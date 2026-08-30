"""Tests for journal LLM retry, parse, quarantine, and classified failure behavior.

They prevent model failures from becoming silent empty digest runs."""

from __future__ import annotations

from pathlib import Path

import pytest

import scope_recall.journal as journal_module
import scope_recall.journal_llm as journal_llm
import scope_recall.nightly_llm as nightly_llm


def test_journal_llm_module_exports_identity_match_journal_reexports():
    assert journal_module.JournalDigestLLMError is journal_llm.JournalDigestLLMError
    assert journal_module._call_llm_with_retries is journal_llm._call_llm_with_retries
    assert journal_module._quarantine_classification is journal_llm._quarantine_classification
    assert journal_module._classify_llm_digest_error is nightly_llm.classify_llm_error
    assert journal_llm._classify_llm_digest_error is nightly_llm.classify_llm_error


def test_journal_llm_retry_preserves_journal_call_llm_monkeypatch_and_sanitization(monkeypatch):
    attempts = {"count": 0}

    def fake_call_llm(*args, **kwargs):  # noqa: ARG001
        attempts["count"] += 1
        raise TimeoutError("provider timeout with api_key=sk-" + "D" * 24 + " at /tmp/hermes-secret-output.log")

    monkeypatch.setattr(journal_module, "call_llm", fake_call_llm)

    with pytest.raises(journal_llm.JournalDigestLLMError) as excinfo:
        journal_llm._call_llm_with_retries(
            "prompt",
            model="test-model",
            base_url="https://example.invalid",
            api_key="",
            timeout=1,
            api_mode="chat_completions",
            endpoint="/v1/chat/completions",
            append_v1=False,
            max_attempts=2,
            retry_delay=0,
        )

    assert attempts["count"] == 2
    error = excinfo.value
    assert error.attempts == 2
    assert error.error_kind == "timeout"
    assert error.retryable is True
    message = str(error)
    assert "timeout after 2 attempt" in message
    assert "[REDACTED_SECRET]" in message
    assert "[REDACTED_PATH]" in message
    assert "sk-" not in message
    assert "/tmp/hermes-secret-output.log" not in message


def test_journal_llm_missing_api_key_is_non_retryable_without_sleep(monkeypatch):
    calls = {"count": 0}
    sleeps: list[float] = []

    def missing_key(*_args, **_kwargs):
        calls["count"] += 1
        # A generic legacy/cross-package hook must classify the same as the
        # typed production error; Hermes test installs can load both package
        # identities in one process.
        raise RuntimeError("API key not found for nightly digest")

    monkeypatch.setattr(journal_module, "call_llm", missing_key)
    monkeypatch.setattr(journal_llm.time, "sleep", lambda seconds: sleeps.append(seconds))

    with pytest.raises(journal_llm.JournalDigestLLMError) as excinfo:
        journal_llm._call_llm_with_retries(
            "prompt",
            model="test-model",
            base_url="https://api.openai.com",
            api_key="",
            timeout=60,
            api_mode="chat_completions",
            max_attempts=3,
            retry_delay=1,
        )

    assert excinfo.value.attempts == 1
    assert excinfo.value.error_kind == "auth"
    assert excinfo.value.retryable is False
    assert calls["count"] == 1
    assert sleeps == []


def test_journal_llm_passes_explicit_insecure_endpoint_opt_in(monkeypatch):
    captured: dict[str, object] = {}

    def fake_call_llm(_prompt: str, **kwargs: object) -> str:
        captured.update(kwargs)
        return "[]"

    monkeypatch.setattr(journal_module, "call_llm", fake_call_llm)

    result = journal_llm._call_llm_with_retries(
        "prompt",
        model="test-model",
        base_url="http://model.internal:1234",
        api_key="",
        timeout=1,
        api_mode="chat_completions",
        allow_insecure_endpoint=True,
        max_attempts=1,
        retry_delay=0,
    )

    assert result == "[]"
    assert captured["allow_insecure_endpoint"] is True


def test_journal_llm_threads_configured_thinking_to_call_llm(monkeypatch):
    captured: dict[str, object] = {}

    def fake_call_llm(_prompt: str, **kwargs: object) -> str:
        captured.update(kwargs)
        return "[]"

    monkeypatch.setattr(journal_module, "call_llm", fake_call_llm)

    result = journal_llm._call_llm_with_retries(
        "prompt",
        model="test-model",
        base_url="https://example.invalid",
        api_key="",
        timeout=1,
        api_mode="chat_completions",
        thinking={"type": "enabled", "budget_tokens": 1024},
        max_attempts=1,
        retry_delay=0,
    )

    assert result == "[]"
    assert captured["thinking"] == {"type": "enabled", "budget_tokens": 1024}



def test_journal_llm_threads_explicit_system_prompt_to_transport(monkeypatch):
    captured: dict[str, object] = {}

    def fake_call_llm(_prompt: str, **kwargs: object) -> str:
        captured.update(kwargs)
        return "{}"

    monkeypatch.setattr(journal_module, "call_llm", fake_call_llm)

    result = journal_llm._call_llm_with_retries(
        "structured-data-envelope",
        model="test-model",
        base_url="https://example.invalid",
        api_key="",
        timeout=1,
        api_mode="chat_completions",
        system_prompt="trusted-adjudication-policy",
        max_attempts=1,
        retry_delay=0,
    )

    assert result == "{}"
    assert captured["system_prompt"] == "trusted-adjudication-policy"

def test_journal_llm_omits_thinking_kwarg_for_legacy_hooks(monkeypatch):
    def legacy_call_llm(
        _prompt: str,
        *,
        model: str,  # noqa: ARG001
        base_url: str,  # noqa: ARG001
        api_key: str,  # noqa: ARG001
        timeout: float,  # noqa: ARG001
        api_mode: str,  # noqa: ARG001
        endpoint: str,  # noqa: ARG001
        append_v1: bool,  # noqa: ARG001
    ) -> str:
        return "[]"

    monkeypatch.setattr(journal_module, "call_llm", legacy_call_llm)

    result = journal_llm._call_llm_with_retries(
        "prompt",
        model="test-model",
        base_url="https://example.invalid",
        api_key="",
        timeout=1,
        api_mode="chat_completions",
        thinking=None,
        max_attempts=1,
        retry_delay=0,
    )

    assert result == "[]"


def test_journal_llm_rejects_non_boolean_insecure_endpoint_before_transport(
    monkeypatch,
):
    calls = {"count": 0}

    def fake_call_llm(*_args, **_kwargs):
        calls["count"] += 1
        return "[]"

    monkeypatch.setattr(journal_module, "call_llm", fake_call_llm)

    with pytest.raises(journal_llm.JournalDigestLLMError) as excinfo:
        journal_llm._call_llm_with_retries(
            "prompt",
            model="test-model",
            base_url="http://model.internal:1234",
            api_key="",
            timeout=1,
            api_mode="chat_completions",
            allow_insecure_endpoint="false",  # type: ignore[arg-type]
            max_attempts=3,
            retry_delay=0,
        )

    assert calls["count"] == 0
    assert excinfo.value.attempts == 1
    assert excinfo.value.error_kind == "endpoint_policy"
    assert excinfo.value.retryable is False


def test_journal_llm_quarantine_classification_preserves_metadata():
    retry_error = journal_llm.JournalDigestLLMError(
        "timeout at /tmp/hermes-secret-output.log",
        attempts=2,
        error_kind="timeout",
        retryable=True,
    )
    reason, metadata = journal_llm._quarantine_classification(retry_error)
    assert reason == "retry-exhausted:timeout"
    assert metadata == {
        "classification": "retry_exhausted",
        "kind": "timeout",
        "retryable": True,
        "attempts": 2,
        "message": "timeout at [REDACTED_PATH]",
    }

    auth_error = journal_llm.JournalDigestLLMError(
        "auth failed with api_key=sk-" + "A" * 24,
        attempts=1,
        error_kind="auth",
        retryable=False,
    )
    reason, metadata = journal_llm._quarantine_classification(auth_error)
    assert reason == "dead-letter:auth"
    assert metadata["classification"] == "dead_letter"
    assert metadata["kind"] == "auth"
    assert metadata["retryable"] is False
    assert metadata["attempts"] == 1
    assert "[REDACTED_SECRET]" in metadata["message"]
    assert "sk-" not in metadata["message"]

    reason, metadata = journal_llm._quarantine_classification(TimeoutError("temporary timeout"))
    assert reason == "retry-exhausted:timeout"
    assert metadata["classification"] == "retry_exhausted"
    assert metadata["kind"] == "timeout"
    assert metadata["retryable"] is True
    assert metadata["attempts"] == 1


def test_journal_llm_has_no_static_journal_import():
    assert journal_llm.__file__ is not None
    source = Path(journal_llm.__file__).read_text(encoding="utf-8")
    assert "from . import journal" not in source
    assert "from scope_recall import journal" not in source
    assert "import scope_recall.journal" not in source
