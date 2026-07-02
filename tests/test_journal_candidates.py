"""Tests for heuristic journal candidate extraction and noise rejection.

They prevent transcript-shaped or generic session text from becoming durable memories."""

from __future__ import annotations

from pathlib import Path

import scope_recall.journal as journal_module
import scope_recall.journal_candidates as journal_candidates
from scope_recall.journal_store import JournalEntry


def test_journal_candidates_module_exports_identity_match_journal_reexports():
    for name in [
        "JournalDigestCandidate",
        "_unique",
        "_entry_entities",
        "_GENERIC_TOPIC_ENTITIES",
        "_topic_entities",
        "_topic_tags",
        "_topic_label",
        "_DOMAIN_TOPIC_HINTS",
        "_topic_signature",
        "_segment_session_entries",
        "_classify_target_and_type",
        "_looks_like_historical_template_noise",
        "_digest_role_summary",
        "_heuristic_candidate_content",
        "heuristic_journal_candidates",
        "candidate_metadata",
    ]:
        assert getattr(journal_module, name) is getattr(journal_candidates, name)


def test_journal_candidates_has_no_static_journal_import():
    assert journal_candidates.__file__ is not None
    source = Path(journal_candidates.__file__).read_text(encoding="utf-8")
    assert "from . import journal" not in source
    assert "from .journal import" not in source
    assert "from scope_recall import journal" not in source
    assert "import scope_recall.journal" not in source
    assert "journal_llm" not in source


def test_candidate_metadata_normalizes_memory_type_and_keeps_journal_evidence():
    candidate = journal_candidates.JournalDigestCandidate(
        content="Scope Recall H5 candidate metadata should preserve journal evidence.",
        target="memory",
        memory_type="not-a-real-type",
        importance=2.5,
        confidence=-1,
        entities=["Scope Recall"],
        tags=["architecture", "journal-digest"],
        reason="test coverage",
        entry_ids=[1, 2, 3],
        session_ids=["session-a"],
    )

    metadata = journal_candidates.candidate_metadata(candidate, "run-h5")

    assert metadata["memory_type"] == "summary"
    assert metadata["importance"] == 1.0
    assert metadata["confidence"] == 0.0
    assert metadata["entities"] == ["Scope Recall"]
    assert metadata["tags"] == ["architecture", "journal-digest"]
    assert metadata["journal_run_id"] == "run-h5"
    assert metadata["journal_entry_ids"] == [1, 2, 3]
    assert metadata["journal_session_ids"] == ["session-a"]
    assert metadata["journal_reason"] == "test coverage"


def test_heuristic_candidates_split_unrelated_user_topics_and_skip_tool_only():
    entries = [
        JournalEntry(1, "scope", "shared", "session-a", 1, "user", "Scope Recall release gate 要先跑 pytest。", "2026-06-01T00:00:00+00:00"),
        JournalEntry(2, "scope", "shared", "session-a", 2, "assistant", "已验证 release gate 通过。", "2026-06-01T00:00:01+00:00"),
        JournalEntry(3, "scope", "shared", "session-a", 3, "user", "Tailscale firewall 远程网络排障流程需要保留。", "2026-06-01T00:00:02+00:00"),
        JournalEntry(4, "scope", "shared", "session-a", 4, "assistant", "记录可复用网络排障流程。", "2026-06-01T00:00:03+00:00"),
        JournalEntry(5, "scope", "shared", "session-tool", 1, "tool", "raw terminal output should not be memory", "2026-06-01T00:00:04+00:00"),
    ]

    candidates = journal_candidates.heuristic_journal_candidates(entries)

    assert len(candidates) == 2
    assert all(candidate.entry_ids for candidate in candidates)
    assert {tuple(candidate.session_ids) for candidate in candidates} == {("session-a",)}
    assert any("release" in candidate.content.lower() for candidate in candidates)
    assert any("tailscale" in candidate.content.lower() or "网络" in candidate.content for candidate in candidates)
