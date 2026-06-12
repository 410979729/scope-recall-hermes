from __future__ import annotations

from scope_recall.scoring import reciprocal_rank_fusion
from scope_recall.graph import entity_distance_scores


def test_reciprocal_rank_fusion_rewards_cross_signal_results():
    fused = reciprocal_rank_fusion(
        {
            "lexical": ["lex-only", "cross", "late"],
            "vector": ["vec-only", "cross", "late"],
            "bm25": ["cross", "lex-only", "vec-only"],
        },
        weights={"lexical": 1.0, "vector": 1.0, "bm25": 1.0},
        k=10,
    )

    assert fused[0][0] == "cross"
    assert dict(fused)["cross"] > dict(fused)["lex-only"]
    assert dict(fused)["cross"] > dict(fused)["vec-only"]


def test_reciprocal_rank_fusion_does_not_boost_single_signal_items_by_default():
    fused = reciprocal_rank_fusion(
        {
            "lexical": ["weak-lexical-only"],
            "vector": [],
            "bm25": [],
        },
        weights={"lexical": 1.0, "vector": 1.0, "bm25": 1.0},
        k=10,
    )

    assert fused == []


def test_entity_distance_scores_prefers_neighboring_memories_over_unrelated_entities():
    query_entities = ["joy", "scope-recall"]
    memory_entities = {
        "direct": ["scope-recall"],
        "neighbor": ["journal-digest"],
        "far": ["pokemon"],
    }
    relations = {
        "scope-recall": ["journal-digest", "memory-governance"],
        "journal-digest": ["merge-upsert"],
    }

    scores = entity_distance_scores(query_entities, memory_entities, relations, max_depth=2)

    assert scores["direct"] > scores["neighbor"] > scores.get("far", 0.0)
