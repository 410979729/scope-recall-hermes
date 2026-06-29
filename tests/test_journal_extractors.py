from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import scope_recall.journal as journal_module
import scope_recall.journal_extractors as journal_extractors
from scope_recall.journal_store import JournalEntry
from scope_recall.models import RuntimeScope


def _scope() -> RuntimeScope:
    return RuntimeScope(
        platform="telegram",
        user_id="8176453077",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="default",
        agent_workspace="hermes",
        agent_context="primary",
    )


def test_journal_extractors_module_exports_identity_match_journal_reexports():
    for name in [
        "_parse_entry_timestamp",
        "_journal_session_bundles",
        "_journal_from_digest_candidate",
        "llm_journal_candidates",
        "_config_bool",
        "_runtime_config",
        "_journal_runtime_config",
        "_coerce_positive_int",
        "_coerce_nonnegative_float",
    ]:
        assert getattr(journal_module, name) is getattr(journal_extractors, name)


def test_journal_extractors_has_no_static_journal_import():
    assert journal_extractors.__file__ is not None
    source = Path(journal_extractors.__file__).read_text(encoding="utf-8")
    assert "from . import journal" not in source
    assert "from .journal import" not in source
    assert "from scope_recall import journal" not in source
    assert "import scope_recall.journal" not in source


def test_journal_extractors_config_coercion_helpers():
    assert journal_extractors._config_bool({"flag": True}, "flag") is True
    assert journal_extractors._config_bool({"flag": "true"}, "flag") is True
    assert journal_extractors._config_bool({"flag": "YES"}, "flag") is True
    assert journal_extractors._config_bool({"flag": "0"}, "flag") is False
    assert journal_extractors._config_bool({}, "missing", default=True) is True

    assert journal_extractors._coerce_positive_int(None, 5) == 5
    assert journal_extractors._coerce_positive_int("bad", 5) == 5
    assert journal_extractors._coerce_positive_int(0, 5) == 1
    assert journal_extractors._coerce_positive_int(-10, 5) == 1
    assert journal_extractors._coerce_positive_int("7", 5) == 7

    assert journal_extractors._coerce_nonnegative_float(None, 0.5) == 0.5
    assert journal_extractors._coerce_nonnegative_float("bad", 0.5) == 0.5
    assert journal_extractors._coerce_nonnegative_float(-2, 0.5) == 0.0
    assert journal_extractors._coerce_nonnegative_float("1.25", 0.5) == 1.25


def test_journal_extractors_session_bundles_sort_and_handle_tool_only():
    entries = [
        JournalEntry(2, "scope", "shared", "session-a", 2, "assistant", "验证已完成。", "2026-06-01T00:00:02+00:00"),
        JournalEntry(1, "scope", "shared", "session-a", 1, "user", "请验证 Scope Recall release gate。", "2026-06-01T00:00:01Z"),
        JournalEntry(
            3,
            "scope",
            "shared",
            "session-tool",
            1,
            "tool",
            "terminal output",
            "2026-06-01T00:00:03+00:00",
            metadata={"tool_name": "terminal"},
        ),
    ]

    bundles = journal_extractors._journal_session_bundles(entries)

    assert [bundle.id for bundle in bundles] == ["session-a", "session-tool"]
    assert bundles[0].source == "journal"
    assert [message.id for message in bundles[0].messages] == [1, 2]
    assert bundles[0].messages[0].role == "user"
    assert bundles[0].messages[0].timestamp > 0
    assert bundles[0].is_task is True
    assert bundles[0].completed is True
    assert bundles[1].source == "journal-tool-only"
    assert bundles[1].messages == []
    assert bundles[1].tool_names == ["terminal"]
    assert bundles[1].is_task is True


def test_journal_extractors_digest_candidate_conversion_defaults_and_evidence():
    raw = SimpleNamespace(
        content="LLM digest extracted a durable Scope Recall workflow.",
        target="",
        memory_type="",
        importance=None,
        confidence=None,
        entities=("scope-recall",),
        tags=["llm-digest"],
        reason="",
        message_ids=["10", 11],
        session_id="session-a",
    )

    candidate = journal_extractors._journal_from_digest_candidate(raw)

    assert candidate.content == raw.content
    assert candidate.target == "memory"
    assert candidate.memory_type == "summary"
    assert candidate.importance == 0.55
    assert candidate.confidence == 0.65
    assert candidate.entities == ["scope-recall"]
    assert candidate.tags == ["llm-digest", "journal-digest"]
    assert candidate.reason == "llm journal digest extraction"
    assert candidate.entry_ids == [10, 11]
    assert candidate.session_ids == ["session-a"]


def test_journal_collect_candidates_preserves_journal_llm_journal_candidates_monkeypatch(monkeypatch, tmp_path):
    called = {"count": 0}
    expected = journal_module.JournalDigestCandidate(
        content="patched candidate",
        target="memory",
        entry_ids=[1],
        session_ids=["session-a"],
    )

    def fake_llm_journal_candidates(*args, **kwargs):  # noqa: ARG001
        called["count"] += 1
        return [expected]

    monkeypatch.setattr(journal_module, "llm_journal_candidates", fake_llm_journal_candidates)
    conn = sqlite3.connect(":memory:")
    try:
        candidates, extractor_used, error = journal_module._collect_journal_candidates(
            conn,
            entries=[JournalEntry(1, "scope", "shared", "session-a", 1, "user", "please digest", "2026-06-01T00:00:00+00:00")],
            hermes_home=tmp_path,
            scope=_scope(),
            journal_config={},
            requested_extractor="llm",
        )
    finally:
        conn.close()

    assert called["count"] == 1
    assert candidates == [expected]
    assert extractor_used == "llm"
    assert error == ""
