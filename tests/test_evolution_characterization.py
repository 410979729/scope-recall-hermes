"""Characterize the legacy fact path preserved when Fact Evolution is disabled.

These assertions describe the 1.7.2 behavior that remains the default-off fallback
in 1.8.0. Opt-in integration tests cover structured action preservation and
same-slot evolution separately.
"""

from __future__ import annotations

import json

import pytest

from scope_recall.governance import is_conflicting, merge_memory_text
from scope_recall.journal_extractors import _journal_from_digest_candidate
from scope_recall.nightly_digest import (
    MessageRecord,
    SessionBundle,
    _parse_llm_candidates_with_status,
)


def _bundle() -> SessionBundle:
    return SessionBundle(
        id="characterization-session",
        source="test",
        user_id="primary-user",
        messages=[
            MessageRecord(
                id=1,
                session_id="characterization-session",
                role="user",
                content="Please keep the corrected durable fact.",
                timestamp=1.0,
            )
        ],
    )


def _candidate_json(action: str, content: str) -> str:
    return json.dumps(
        [
            {
                "action": action,
                "content": content,
                "target": "user",
                "memory_type": "factual",
                "importance": 0.9,
                "confidence": 0.95,
                "entities": ["Asha"],
                "tags": ["location"],
                "reason": "The user directly corrected the durable fact.",
                "existing_hint": "Asha lives in Mumbai.",
                "target_ids": ["memory-old-location"],
                "evidence_message_ids": [1],
            }
        ]
    )


@pytest.mark.parametrize("action", ["update", "delete", "unexpected_action"])
def test_characterization_non_skip_llm_actions_are_discarded(action):
    content = (
        "Asha now lives in Bangalore and explicitly corrected the previous "
        "Mumbai location as a durable personal fact."
    )

    candidates, status = _parse_llm_candidates_with_status(
        _candidate_json(action, content),
        bundle=_bundle(),
    )

    assert status == "parsed"
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.content == content
    assert not hasattr(candidate, "action")
    assert not hasattr(candidate, "existing_hint")
    assert not hasattr(candidate, "target_ids")


def test_characterization_skip_is_the_only_preserved_llm_action():
    candidates, status = _parse_llm_candidates_with_status(
        _candidate_json(
            "skip",
            "This sufficiently long candidate is ignored only because its action is the one recognized action.",
        ),
        bundle=_bundle(),
    )

    assert candidates == []
    assert status == "explicit_skip"


def test_characterization_journal_conversion_cannot_recover_discarded_action():
    candidates, status = _parse_llm_candidates_with_status(
        _candidate_json(
            "update",
            "Asha now lives in Bangalore and explicitly replaced the previous Mumbai location fact.",
        ),
        bundle=_bundle(),
    )
    assert status == "parsed"

    journal_candidate = _journal_from_digest_candidate(candidates[0])

    assert journal_candidate.content.startswith("Asha now lives in Bangalore")
    assert not hasattr(journal_candidate, "action")
    assert not hasattr(journal_candidate, "existing_hint")
    assert not hasattr(journal_candidate, "target_ids")


@pytest.mark.parametrize(
    ("existing", "candidate"),
    [
        ("Asha lives in Mumbai.", "Asha lives in Bangalore."),
        ("星河运行在云服务器。", "星河运行在新家本地电脑。"),
    ],
)
def test_characterization_same_slot_positive_replacement_is_not_a_conflict(
    existing,
    candidate,
):
    assert is_conflicting(existing, candidate) is False


def test_characterization_fallback_merge_keeps_old_and_new_single_values_together():
    existing = "Asha lives in Mumbai."
    candidate = "Asha lives in Bangalore."

    merged = merge_memory_text(existing, candidate)

    assert "Mumbai" in merged
    assert "Bangalore" in merged
    assert "\n§\n" in merged
    assert " / " not in merged
