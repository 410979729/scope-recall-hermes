"""Tests for scoring helpers, temporal policy, and metadata/entity boosts.

They keep ranking explainable and bounded."""

from __future__ import annotations

import json

from plugins.memory import load_memory_provider

from scope_recall.aliases import canonicalize_alias
from scope_recall.scoring import lexical_score



def test_canonicalize_alias_covers_shared_recall_vocabulary():
    assert canonicalize_alias("responses") == "reply"
    assert canonicalize_alias("tone") == "style"
    assert canonicalize_alias("likes") == "prefer"
    assert canonicalize_alias("restarts") == "restart"



def test_lexical_score_does_not_double_count_alias_overlap():
    score = lexical_score(
        query="response style joy prefer",
        content="Joy likes warm concise replies.",
        summary="",
        source="tool-store",
        target="user",
    )
    assert round(score, 2) == 0.70



def test_freshness_substrings_do_not_trigger_recency_bonus(tmp_path):
    config_path = tmp_path / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "retrieval": {"mode": "lexical", "min_score": 0.18},
                "vector": {"enabled": False},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-recency-substring",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    try:
        service = plugin._recall_service
        assert service._freshness_weight("What do we know about production deploy?") == 0.0
        assert service._freshness_weight("What day do we deploy prod?") == 0.0
        assert service._freshness_weight("How do we find the updated deploy guide?") > 0.0
    finally:
        plugin.shutdown()
