"""A connection that failed in milliseconds must not cost the semantic channel.

Covers ``runtime/embedding_retry.py``.  The query embedding is one network call
with no second chance: when it fails, ``_vector_candidates`` records
``vector_unavailable`` and the packet is assembled from the lexical and recent
channels alone.  A live instance recorded that twice in one day.
"""
from __future__ import annotations

import pytest

from scope_recall.adapters.models import AuxiliaryModelError
from scope_recall.runtime.embedding_retry import (
    MINIMUM_RETRY_FRACTION,
    TRANSIENT_EMBEDDING_ERRORS,
    embed_with_one_retry,
    retry_budget,
    transient,
)


# --------------------------------------------------------------------------
# What counts as worth another attempt
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", sorted(TRANSIENT_EMBEDDING_ERRORS))
def test_a_connection_failure_is_transient(kind):
    assert transient(AuxiliaryModelError(kind)) is True


@pytest.mark.parametrize("kind", ["input_invalid", "endpoint_invalid", "credential_missing",
                                  "request_invalid", "vector_dimension_mismatch",
                                  "budget_exhausted", "sensitive_request"])
def test_a_rejected_request_is_not_retried(kind):
    """It would fail the same way and spend the recall's deadline doing so."""
    assert transient(AuxiliaryModelError(kind)) is False


def test_a_timeout_is_not_retried():
    """A call that used its whole budget has none left to use again."""
    assert transient(AuxiliaryModelError("timeout")) is False
    assert "timeout" not in TRANSIENT_EMBEDDING_ERRORS


@pytest.mark.parametrize("error", [RuntimeError("boom"), ValueError("x"), TypeError("y")])
def test_an_error_without_a_kind_is_not_retried(error):
    assert transient(error) is False


# --------------------------------------------------------------------------
# The budget a second attempt may use
# --------------------------------------------------------------------------

def test_a_full_budget_allows_the_retry():
    assert retry_budget(4.0, 4.0) == 4.0


def test_the_retry_is_capped_by_what_is_left():
    assert retry_budget(4.0, 3.0) == 3.0


def test_too_little_left_refuses_the_retry():
    """A second attempt that cannot finish turns a degraded packet into a late
    one, and the deadline belongs to the caller."""
    assert retry_budget(4.0, 4.0 * MINIMUM_RETRY_FRACTION - 0.01) == 0.0


@pytest.mark.parametrize("budget,remaining", [(0, 5), (-1, 5), (5, 0), (5, -1)])
def test_a_spent_or_nonsense_budget_refuses_the_retry(budget, remaining):
    assert retry_budget(budget, remaining) == 0.0


@pytest.mark.parametrize("bad", [None, "4", object(), float("nan")])
def test_a_non_numeric_budget_refuses_rather_than_raises(bad):
    """A string that happens to parse is a caller bug; coercing it hides one."""
    assert retry_budget(bad, 4.0) == 0.0
    assert retry_budget(4.0, bad) == 0.0


# --------------------------------------------------------------------------
# The call itself
# --------------------------------------------------------------------------

def test_a_first_attempt_that_works_is_not_repeated():
    calls = []

    def embed(seconds):
        calls.append(seconds)
        return [0.1, 0.2]

    assert embed_with_one_retry(embed, budget_seconds=4.0, remaining=lambda: 4.0) == [0.1, 0.2]
    assert calls == [4.0]


def test_a_connection_failure_is_tried_once_more_and_can_succeed():
    calls = []

    def embed(seconds):
        calls.append(seconds)
        if len(calls) == 1:
            raise AuxiliaryModelError("transport_unavailable")
        return [1.0]

    assert embed_with_one_retry(embed, budget_seconds=4.0, remaining=lambda: 3.0) == [1.0]
    assert calls == [4.0, 3.0], "the retry is budgeted from what the first attempt left"


def test_it_never_tries_a_third_time():
    calls = []

    def embed(seconds):
        calls.append(seconds)
        raise AuxiliaryModelError("network_error")

    with pytest.raises(AuxiliaryModelError):
        embed_with_one_retry(embed, budget_seconds=4.0, remaining=lambda: 4.0)
    assert len(calls) == 2


def test_a_rejected_request_raises_from_the_first_attempt():
    calls = []

    def embed(seconds):
        calls.append(seconds)
        raise AuxiliaryModelError("input_invalid")

    with pytest.raises(AuxiliaryModelError):
        embed_with_one_retry(embed, budget_seconds=4.0, remaining=lambda: 4.0)
    assert len(calls) == 1


def test_an_exhausted_deadline_raises_the_original_failure():
    def embed(seconds):
        raise AuxiliaryModelError("transport_unavailable")

    with pytest.raises(AuxiliaryModelError) as caught:
        embed_with_one_retry(embed, budget_seconds=4.0, remaining=lambda: 0.0)
    assert caught.value.error_type == "transport_unavailable"


def test_the_remaining_budget_is_read_after_the_failure_not_before():
    """Otherwise the retry is budgeted from time the first attempt already spent."""
    reads = []

    def remaining():
        reads.append(len(reads))
        return 3.0

    def embed(seconds):
        if not reads:
            raise AuxiliaryModelError("transport_worker")
        return [1.0]

    embed_with_one_retry(embed, budget_seconds=4.0, remaining=remaining)
    assert reads, "remaining() was never consulted"


# --------------------------------------------------------------------------
# The provider's own word for why it refused
# --------------------------------------------------------------------------

def test_the_refusal_type_is_kept_and_the_message_is_not():
    """The body is free text and may carry account identifiers; the type is a
    short symbol from the provider's vocabulary, and it is the difference
    between "the model is failing" and "the quota is exhausted until the 27th"."""
    from scope_recall.adapters.models import provider_refusal_code

    real = (b'{"type":"error","error":{"type":"GoUsageLimitError","message":'
            b'"Monthly usage limit reached. Resets in 13 days. '
            b'https://opencode.ai/workspace/wrk_01KS2VRX/go"},'
            b'"metadata":{"limitName":"monthly"}}')
    assert provider_refusal_code(real) == "GoUsageLimitError"


@pytest.mark.parametrize("raw", [
    b'{"error":{"message":"contact support at https://example.test/abc"}}',
    b"error code: 1010",
    b"", b"not json at all", None, 42,
    b'{"error":{"type":"has space"}}',
    b'{"error":{"type":"' + b"x" * 200 + b'"}}',
])
def test_nothing_unbounded_escapes(raw):
    from scope_recall.adapters.models import provider_refusal_code

    assert provider_refusal_code(raw) is None


def test_a_code_field_is_accepted_when_there_is_no_type():
    from scope_recall.adapters.models import provider_refusal_code

    assert provider_refusal_code(b'{"error":{"code":"insufficient_quota"}}') == "insufficient_quota"
