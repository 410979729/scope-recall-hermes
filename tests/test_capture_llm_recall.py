"""Cross-session recall regressions for LLM-extracted durable memories."""

from __future__ import annotations

import json

from plugins.memory import load_memory_provider

from scope_recall.capture_llm import Candidate


def _write_config(hermes_home) -> None:
    config_path = hermes_home / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "auto_capture": True,
                "auto_recall": True,
                "vector": {"enabled": False},
                "capture_llm": {
                    "enabled": True,
                    "min_user_chars": 1,
                    "min_assistant_chars": 1,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _initialize_provider(hermes_home, *, session_id: str, chat_id: str):
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        session_id,
        hermes_home=str(hermes_home),
        platform="telegram",
        user_id="test-user",
        chat_id=chat_id,
        agent_context="primary",
        agent_identity="test-agent",
        agent_workspace="test-workspace",
    )
    return plugin


def test_llm_extracted_durable_memory_auto_recalls_in_new_session_and_chat(
    tmp_path,
    monkeypatch,
) -> None:
    """A promoted extraction must survive both session and chat-window changes."""

    _write_config(tmp_path)
    sentinel = (
        "The operator requires Nebula deployments to use uv run nebula-release and "
        "verify canary metrics before rollout."
    )

    def extracted_candidate(_user, _assistant, _config):
        return [
            Candidate(
                content=sentinel,
                target="user",
                memory_type="procedure",
                entities=["Nebula"],
                tags=["deploy", "canary"],
            )
        ]

    first = _initialize_provider(
        tmp_path,
        session_id="session-capture-a",
        chat_id="chat-a",
    )
    try:
        monkeypatch.setitem(
            first.sync_turn.__globals__,
            "extract_capture_candidates",
            extracted_candidate,
        )
        first.sync_turn(
            "Remember the exact Nebula deployment procedure for future sessions.",
            "I will retain the durable deployment procedure.",
        )
        assert first.flush(timeout=2.0) is True
        with first._lock:
            row = (
                first._require_conn()
                .execute(
                    "SELECT source, target, content FROM memories WHERE source = ?",
                    ("turn-llm-extracted",),
                )
                .fetchone()
            )
        assert row is not None
        assert row["target"] == "user"
        assert row["content"] == sentinel
    finally:
        first.shutdown()

    second = _initialize_provider(
        tmp_path,
        session_id="session-capture-b",
        chat_id="chat-b",
    )
    try:
        query = (
            "Which command and canary verification does the operator require "
            "for Nebula deployments?"
        )
        second.on_turn_start(1, query)
        recalled = second.prefetch(query)
    finally:
        second.shutdown()

    assert "uv run nebula-release" in recalled
    assert "canary metrics" in recalled
