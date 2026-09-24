"""An oversized source must still get a vector, not be silently unsearchable.

Covers ``core/embedding_budget.py`` and the bound it puts on
``encode_embedding_text``.  Six sources on alpha were permanently
unembeddable -- 16,505 to 65,536 characters, rejected with ``http_400``, which
is not auto-recoverable -- so their content was in SQLite and in the lexical
index but never in the vector index, for the life of the instance.  #125 found
the same for ordinary Chinese sources under the old character bound: providers
count tokens, and a Chinese character is about one.
"""
from __future__ import annotations

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core.embedding_budget import (
    ASCII_CHARS_PER_TOKEN,
    EMBEDDING_INPUT_TOKENS,
    TRUNCATION_MARKER,
    bounded_embedding_text,
    estimated_tokens,
)
from scope_recall.core.recall_policy import claim_embedding_text, encode_embedding_text

#: The observed failure floor on alpha.  The bound has to stay under it.
LOWEST_OBSERVED_FAILURE = 16505
#: The 99th percentile of bodies that already embed successfully.
SUCCESSFUL_P99 = 5536
#: Zhipu embedding-3's documented and measured limit per input (#125).
PROVIDER_TOKENS = 3072


def test_the_bound_sits_between_what_works_and_what_fails():
    """Not tuned to a guess: measured against both sides of the real boundary."""
    ascii_capacity = EMBEDDING_INPUT_TOKENS * ASCII_CHARS_PER_TOKEN
    assert SUCCESSFUL_P99 < ascii_capacity < LOWEST_OBSERVED_FAILURE
    assert EMBEDDING_INPUT_TOKENS < PROVIDER_TOKENS


def test_text_within_the_bound_is_untouched():
    text = "x" * (EMBEDDING_INPUT_TOKENS * ASCII_CHARS_PER_TOKEN)
    assert bounded_embedding_text(text) == (text, False)
    chinese = "测" * EMBEDDING_INPUT_TOKENS
    assert bounded_embedding_text(chinese) == (chinese, False)


def test_oversized_text_is_cut_rather_than_refused():
    body, truncated = bounded_embedding_text("x" * 70000)
    assert truncated is True
    assert estimated_tokens(body) <= EMBEDDING_INPUT_TOKENS
    assert body.endswith(TRUNCATION_MARKER), "a cut has to be visible in what was embedded"


def test_the_cut_is_recorded_not_silent():
    assert bounded_embedding_text("x" * 70000)[0].endswith(TRUNCATION_MARKER)
    assert bounded_embedding_text("short")[0] == "short"


@pytest.mark.parametrize("limit", [1, 32, 100])
def test_a_tiny_limit_still_produces_something_embeddable(limit):
    body, truncated = bounded_embedding_text("x" * 1000, limit=limit)
    assert truncated is True and body


@pytest.mark.parametrize("bad", [0, -1, 1.0, True, "8000"])
def test_the_limit_must_be_a_positive_whole_number(bad):
    with pytest.raises(ValueError):
        bounded_embedding_text("x", limit=bad)


def test_non_text_is_refused_rather_than_coerced():
    with pytest.raises(TypeError):
        bounded_embedding_text(None)


def _mixed(length: int, chinese_share: float) -> str:
    """``length`` characters with ``chinese_share`` of them Chinese, spread evenly."""
    every = round(1 / chinese_share) if chinese_share else 0
    return "".join("测" if every and index % every == 0 else "a" for index in range(length))


@pytest.mark.parametrize("length,chinese_share,largest_safe_prefix", [
    (8020, 0.0, 7121),     # work 34435: symbol-dense ASCII, 2.3 characters a token
    (8020, 0.04, 7721),    # work 34420
    (5937, 0.15, 5853),    # work 37702: failed while 12,271 ASCII characters passed
    (6800, 0.13, 5895),    # work 37714
    (8020, 0.31, 5941),    # work 34444
    (3068, 1.0, 3068),     # pure Chinese at the provider's 3,072 tokens
])
def test_what_the_bound_keeps_fits_under_every_measured_provider_limit(length, chinese_share, largest_safe_prefix):
    """#125's measurements against a 3,072-token provider: the old 8,000-character bound kept all of
    these whole and every one failed with http_400."""
    body, _truncated = bounded_embedding_text(_mixed(length, chinese_share))
    assert len(body.removesuffix(TRUNCATION_MARKER)) <= largest_safe_prefix
    assert estimated_tokens(body) <= EMBEDDING_INPUT_TOKENS


def test_the_estimate_knows_the_script():
    """The same number of characters is a different number of tokens; a character bound cannot see it."""
    assert estimated_tokens("测" * 3000) == 3 * estimated_tokens("a" * 3000) == 3000
    assert estimated_tokens("abc") == 1 and estimated_tokens("测试") == 2 and estimated_tokens("") == 0


# --------------------------------------------------------------------------
# Through the one encoder every embedded body passes
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["document", "query"])
def test_every_embedded_body_is_bounded(kind):
    """Source, claim and query all go through this one function."""
    encoded = encode_embedding_text("x" * 70000, kind=kind)
    assert len(encoded) < 70000
    assert encoded.endswith(TRUNCATION_MARKER)


def test_a_short_body_keeps_its_exact_encoding():
    """The bound must not disturb the prompt encoding the space digest pins."""
    assert encode_embedding_text("hello", kind="document") == "title: none | text: hello"
    assert encode_embedding_text("hello", kind="query") == \
        "task: question answering | query: hello"


def test_the_encoder_still_refuses_what_it_always_refused():
    with pytest.raises(ContractError):
        encode_embedding_text("x", kind="not-a-kind")
    with pytest.raises(ContractError):
        encode_embedding_text(None, kind="document")


def test_a_huge_claim_payload_is_bounded_too():
    payload = {"subject": "TEST-subject", "predicate": "是", "value_text": "v" * 70000,
               "conditions": []}
    encoded = encode_embedding_text(claim_embedding_text(payload), kind="document")
    assert len(encoded) < 70000 and encoded.endswith(TRUNCATION_MARKER)
