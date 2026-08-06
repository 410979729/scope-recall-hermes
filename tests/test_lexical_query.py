"""CJK supplemental query normalization and relevance tests."""

from scope_recall.lexical_query import (
    cjk_query_ngrams,
    cjk_substring_score,
    trigram_fts_query,
)


def test_cjk_ngrams_are_bounded_and_prioritize_trigrams_before_bigrams():
    terms = cjk_query_ngrams("生产库切换前需要做什么", limit=12)

    assert terms[:3] == ["生产库", "产库切", "库切换"]
    assert len(terms) == 12
    assert "生产" in terms


def test_cjk_substring_score_rejects_single_generic_bigram():
    assert cjk_substring_score("数据库迁移", "只有数据指标", "") == 0.0
    assert cjk_substring_score("生产库切换", "生产数据库需要切换窗口", "") > 0.0
    assert cjk_substring_score("数据库迁移", "数据库迁移方案", "") > 0.0


def test_trigram_query_is_bounded_and_quotes_fts_terms():
    query = trigram_fts_query('数据库迁移"方案', ["oauth", "redirect"])

    assert " OR " in query
    assert '"oauth"' in query
    assert '"redirect"' in query
    assert query.count(" OR ") < 24
