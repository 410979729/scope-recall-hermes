"""An oversized source must still get a vector, not be silently unsearchable.

Covers ``core/embedding_budget.py`` and the bound it puts on
``encode_embedding_text``.  Six sources on tianshu were permanently
unembeddable -- 16,505 to 65,536 characters, rejected with ``http_400``, which
is not auto-recoverable -- so their content was in SQLite and in the lexical
index but never in the vector index, for the life of the instance.
"""
from __future__ import annotations

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core.embedding_budget import (
    EMBEDDING_INPUT_CHARS,
    TRUNCATION_MARKER,
    bounded_embedding_text,
)
from scope_recall.core.recall_policy import claim_embedding_text, encode_embedding_text

#: The observed failure floor on tianshu.  The bound has to stay under it.
LOWEST_OBSERVED_FAILURE = 16505
#: The 99th percentile of bodies that already embed successfully.
SUCCESSFUL_P99 = 5536


def test_the_bound_sits_between_what_works_and_what_fails():
    """Not tuned to a guess: measured against both sides of the real boundary."""
    assert SUCCESSFUL_P99 < EMBEDDING_INPUT_CHARS < LOWEST_OBSERVED_FAILURE


def test_text_within_the_bound_is_untouched():
    text = "x" * EMBEDDING_INPUT_CHARS
    assert bounded_embedding_text(text) == (text, False)


def test_oversized_text_is_cut_rather_than_refused():
    body, truncated = bounded_embedding_text("x" * 70000)
    assert truncated is True
    assert len(body) <= EMBEDDING_INPUT_CHARS
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
