"""A pass carries as much work as its per-pass costs deserve, in as few requests as allowed.

Every pass pays for its own process start and its own queue report whatever it carries, so a
pass of 32 paid those twice as often as a pass of 64 and six times as often as one of 200.  The
bound was 32 because each source was its own request and each vector its own commit; once a
group shares both, the bound is only deciding how often the fixed costs are paid.

The provider takes a hundred texts per request and refuses more -- measured, not assumed: 32
answered in 2.6s, 100 in 3.8s, 250 refused with HTTP 400 -- so a longer group is sent as
consecutive full requests instead of being refused for its shape.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from scope_recall.adapters.models import (
    AuxiliaryModelError,
    MAX_EMBED_BATCH,
    build_gemini_embed_body,
)
from scope_recall.core.work_storage import MAX_CLAIM_PAGE
from scope_recall.core.worker import EMBED_BATCH_LIMIT, WorkerConfig
from scope_recall.runtime.instance import _COUNT_BOUNDS

from test_v11_claims import app, capture  # noqa: F401  (fixtures)


class Recording:
    """An adapter that records the shape of every request it is asked to make."""

    def __init__(self) -> None:
        self.requests: list[int] = []

    def _embed_many(self, texts, *, remaining_seconds):
        assert remaining_seconds > 0, "each request is bounded by what is left"
        assert len(texts) <= MAX_EMBED_BATCH, "a request must never exceed the provider's ceiling"
        self.requests.append(len(texts))
        return tuple((float(index), 0.0) for index in range(len(texts)))

    embed_texts = None  # replaced below by the real method under test


def _adapter():
    from scope_recall.adapters.models import GeminiEmbeddingAdapter

    made = Recording()
    made.embed_texts = GeminiEmbeddingAdapter.embed_texts.__get__(made, Recording)
    return made


@pytest.mark.parametrize("count,expected", [
    (1, [1]),
    (MAX_EMBED_BATCH, [MAX_EMBED_BATCH]),
    (MAX_EMBED_BATCH + 1, [MAX_EMBED_BATCH, 1]),
    (2 * MAX_EMBED_BATCH, [MAX_EMBED_BATCH, MAX_EMBED_BATCH]),
    (EMBED_BATCH_LIMIT, [MAX_EMBED_BATCH] * (EMBED_BATCH_LIMIT // MAX_EMBED_BATCH)),
])
def test_a_group_is_sent_as_full_requests(count, expected):
    adapter = _adapter()
    vectors = adapter.embed_texts([f"TEST 第{index}条" for index in range(count)], remaining_seconds=60)
    assert adapter.requests == expected, adapter.requests
    assert len(vectors) == count, "every text is answered, in order, across requests"


def test_no_text_is_lost_or_reordered_across_requests():
    adapter = _adapter()
    vectors = adapter.embed_texts([f"TEST {index}" for index in range(MAX_EMBED_BATCH + 7)],
                                  remaining_seconds=60)
    # The recording adapter answers with the index inside each request, so the
    # sequence proves the chunks were concatenated in order and none was dropped.
    assert [int(first) for first, _second in vectors] == list(range(MAX_EMBED_BATCH)) + list(range(7))


def test_an_empty_group_asks_for_nothing():
    adapter = _adapter()
    assert adapter.embed_texts([], remaining_seconds=60) == ()
    assert adapter.requests == []
    with pytest.raises(AuxiliaryModelError):
        build_gemini_embed_body([])


def test_one_request_still_carries_every_text_it_is_given():
    body = json.loads(build_gemini_embed_body(["a", "b", "c"], model="m", dimensions=64))
    assert [entry["content"]["parts"][0]["text"] for entry in body["requests"]] == ["a", "b", "c"]


def test_a_pass_may_be_as_large_as_the_core_allows():
    """The runtime's bound, the core's, and the claim page's are one number, not three that drift."""
    low, high = _COUNT_BOUNDS["max_items"]
    assert (low, high) == (1, 1000)
    assert MAX_CLAIM_PAGE == high, "a pass claims as much of itself as it may process"
    WorkerConfig(owner_id="TEST-owner", max_items=high, embed_batch_limit=EMBED_BATCH_LIMIT)
    with pytest.raises(ValueError):
        WorkerConfig(owner_id="TEST-owner", max_items=high + 1)
    with pytest.raises(ValueError):
        WorkerConfig(owner_id="TEST-owner", embed_batch_limit=EMBED_BATCH_LIMIT + 1)


def test_a_full_pass_claims_and_finishes_more_than_one_claim_page(app):
    """The bound that a bigger pass actually meets, through the seam a pass goes through.

    Raising the pass bound alone was not enough: the claim page had its own 32, so the first
    real pass of more than 32 embed items was rejected as ``INPUT_INVALID: work_claim`` before
    it embedded anything.  A bound that is only tested where it is declared is not tested.
    """
    core, ctx = app
    made = [capture(core, ctx, f"TEST 一趟多拿第{index}条。", key=f"TEST-page/{index}") for index in range(40)]
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='consolidate'")
        conn.commit()
    port = GroupEmbed()
    receipt = core.drain_worker(ctx, max_items=200, remaining_seconds=60, owner_id="TEST-page", embed=port)
    assert receipt.completed == len(made), receipt
    assert port.groups and max(port.groups) > 32, f"the group stayed inside the old page: {port.groups}"


class GroupEmbed:
    """A port that answers a whole group at once and remembers the group sizes."""

    def __init__(self) -> None:
        self.groups: list[int] = []

    def prepare_sources(self, sources, *, remaining_seconds=1.0):
        self.groups.append(len(sources))
        return [{"ref": source.ref, "revision": source.revision} for source in sources]

    def prepare_source(self, source, *, remaining_seconds=1.0):
        return {"ref": source.ref, "revision": source.revision}

    def publish_source(self, prepared, *, source, lease_token, lease_owner, lease_guard, remaining_seconds=1.0):
        assert prepared == {"ref": source.ref, "revision": source.revision}
        assert lease_guard()
