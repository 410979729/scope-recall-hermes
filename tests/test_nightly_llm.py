from __future__ import annotations

import json
from datetime import date

from scope_recall.nightly_digest import DigestOptions


def test_nightly_llm_module_owns_llm_config_and_call_surfaces():
    from scope_recall import nightly_digest, nightly_llm

    assert nightly_digest.resolve_llm_config is nightly_llm.resolve_llm_config
    assert nightly_digest.call_llm is nightly_llm.call_llm
    assert nightly_digest._call_llm_with_retries is nightly_llm.call_llm_with_retries
    assert nightly_digest._classify_llm_error is nightly_llm.classify_llm_error
    assert nightly_digest._call_codex_responses_llm is nightly_llm.call_codex_responses_llm
    assert nightly_digest._decode_responses_body is nightly_llm.decode_responses_body
    assert nightly_digest._responses_endpoint is nightly_llm.responses_endpoint


def test_nightly_llm_direct_call_chat_completions(monkeypatch):
    from scope_recall import nightly_llm

    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "[]"}}]}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(nightly_llm.urllib.request, "urlopen", fake_urlopen)

    raw = nightly_llm.call_llm(
        "extract this",
        model="gpt-4o-mini",
        base_url="https://api.openai.com",
        api_key="openai-key",
        timeout=12,
        api_mode="chat_completions",
    )

    assert raw == "[]"
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer openai-key"
    assert captured["body"]["messages"][1]["content"] == "extract this"
    assert captured["timeout"] == 12


def test_nightly_llm_retry_reports_actual_attempt_count_for_non_retryable_errors(monkeypatch):
    import scope_recall.nightly_llm as nightly_llm

    calls = {"count": 0}

    def fake_call_llm(*args, **kwargs):  # noqa: ARG001
        calls["count"] += 1
        raise RuntimeError("401 unauthorized")

    monkeypatch.setattr(nightly_llm, "call_llm", fake_call_llm)

    try:
        nightly_llm.call_llm_with_retries(
            "prompt",
            model="test-model",
            base_url="https://example.invalid",
            api_key="",
            timeout=1,
            api_mode="chat_completions",
            max_attempts=3,
            retry_delay=0,
        )
    except RuntimeError as exc:
        message = str(exc)
    else:  # pragma: no cover - defensive, fake_call_llm always raises
        raise AssertionError("call_llm_with_retries should raise after auth error")

    assert calls["count"] == 1
    assert "auth after 1 attempt(s)" in message
    assert "auth after 3 attempt(s)" not in message


def test_nightly_llm_resolve_config_accepts_digest_options_shape(tmp_path):
    from scope_recall.nightly_llm import resolve_llm_config

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / ".env").write_text("DEEPSEEK_API_KEY=deepseek-test-key\n", encoding="utf-8")
    (hermes_home / "config.yaml").write_text(
        """
model:
  provider: openai-codex
  default: gpt-5.5
  base_url: https://chatgpt.com/backend-api/codex
providers:
  deepseek:
    base_url: https://api.deepseek.com
    default_model: deepseek-v4-pro
    key_env: DEEPSEEK_API_KEY
scope_recall_nightly_digest:
  provider: deepseek
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = resolve_llm_config(hermes_home, DigestOptions(hermes_home=hermes_home, digest_date=date(2026, 6, 13)))

    assert config["provider"] == "deepseek"
    assert config["model"] == "deepseek-v4-pro"
    assert config["base_url"] == "https://api.deepseek.com"
    assert config["api_key"] == "deepseek-test-key"
    assert config["api_mode"] == "chat_completions"


def test_nightly_llm_options_provider_override_prevents_codex_mode_leakage(tmp_path):
    from scope_recall.nightly_llm import resolve_llm_config

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / ".env").write_text("DEEPSEEK_API_KEY=deepseek-test-key\n", encoding="utf-8")
    (hermes_home / "config.yaml").write_text(
        """
model:
  provider: openai-codex
  default: gpt-5.5
  base_url: https://chatgpt.com/backend-api/codex
  api_mode: codex_responses
providers:
  deepseek:
    api_mode: chat_completions
    base_url: https://api.deepseek.com
    default_model: deepseek-v4-pro
    key_env: DEEPSEEK_API_KEY
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = resolve_llm_config(
        hermes_home,
        DigestOptions(
            hermes_home=hermes_home,
            digest_date=date(2026, 6, 13),
            provider="deepseek",
            api_mode="chat_completions",
            api_key_env="DEEPSEEK_API_KEY",
        ),
    )

    assert config["provider"] == "deepseek"
    assert config["model"] == "deepseek-v4-pro"
    assert config["base_url"] == "https://api.deepseek.com"
    assert config["api_key"] == "deepseek-test-key"
    assert config["api_mode"] == "chat_completions"
