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
from types import SimpleNamespace

import pytest

from scope_recall.adapters.models import (
    AuxiliaryModelError,
    EMBED_REQUEST_CONCURRENCY,
    MAX_EMBED_BATCH,
    build_gemini_embed_body,
)
from scope_recall.core.work_storage import MAX_CLAIM_PAGE, MAX_RECOVERY_PAGE
from scope_recall.core.worker import EMBED_BATCH_LIMIT, WorkerConfig
from scope_recall.runtime.instance import _COUNT_BOUNDS

from test_v11_claims import app, capture  # noqa: F401  (fixtures)


class Recording:
    """An adapter that records the shape of every request it is asked to make."""

    def __init__(self, *, max_request_bytes: int = 10**9) -> None:
        self.requests: list[int] = []
        self.bodies: list[int] = []
        self._ledger = SimpleNamespace(policy=SimpleNamespace(max_request_bytes=max_request_bytes))

    def _request_body(self, texts):
        return build_gemini_embed_body(texts)

    def _embed_many(self, texts, *, remaining_seconds):
        assert remaining_seconds > 0, "each request is bounded by what is left"
        assert len(texts) <= MAX_EMBED_BATCH, "a request must never exceed the provider's ceiling"
        self.requests.append(len(texts))
        self.bodies.append(len(self._request_body(texts)))
        return tuple((float(index), 0.0) for index in range(len(texts)))

    embed_texts = None  # replaced below by the real method under test


def _adapter(**options):
    from scope_recall.adapters.models import GeminiEmbeddingAdapter

    made = Recording(**options)
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


def test_reopening_failures_keeps_its_own_page(app):
    """A pass of a thousand cheap items must not reopen a thousand failures.

    Every bound that said ``max_items`` meant "one pass" while a pass was 32 items of the
    same kind.  Reopening a failure is a model call later, not an embedding now, so it keeps
    its own page -- and a pass larger than that page must still run, which is how this was
    found: a 600-item pass was rejected as ``INPUT_INVALID: retry_limit`` before it started.
    """
    core, ctx = app
    assert MAX_RECOVERY_PAGE < 1000, "the recovery page is not the pass bound"
    made = [capture(core, ctx, f"TEST 恢复分页第{index}条。", key=f"TEST-recover/{index}") for index in range(3)]
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='consolidate'")
        conn.commit()
    receipt = core.drain_worker(ctx, max_items=1000, remaining_seconds=30, owner_id="TEST-recover",
                               embed=GroupEmbed())
    assert receipt.completed == len(made), receipt


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


def test_a_group_asks_its_requests_at_the_same_time():
    """The last serial cost in a pass.

    A pass of a thousand documents is ten requests of a hundred; asked one at a time they were
    38 of its 55 seconds, while the provider allows three thousand requests a minute.  The
    answers must still come back in the order they were asked, and no more than
    ``EMBED_REQUEST_CONCURRENCY`` may be in flight, because each one is a helper process.
    """
    import threading
    import time

    class Concurrent(Recording):
        def __init__(self) -> None:
            super().__init__()
            self._lock = threading.Lock()
            self.live = 0
            self.most = 0

        def _embed_many(self, texts, *, remaining_seconds):
            with self._lock:
                self.live += 1
                self.most = max(self.most, self.live)
            try:
                time.sleep(0.05)                      # a request is mostly waiting
                return super()._embed_many(texts, remaining_seconds=remaining_seconds)
            finally:
                with self._lock:
                    self.live -= 1

    from scope_recall.adapters.models import GeminiEmbeddingAdapter

    adapter = Concurrent()
    adapter.embed_texts = GeminiEmbeddingAdapter.embed_texts.__get__(adapter, Concurrent)
    count = 6 * MAX_EMBED_BATCH
    began = time.perf_counter()
    vectors = adapter.embed_texts([f"TEST {index}" for index in range(count)], remaining_seconds=60)
    elapsed = time.perf_counter() - began
    assert len(vectors) == count
    assert adapter.most > 1, "the requests were still made one at a time"
    assert adapter.most <= EMBED_REQUEST_CONCURRENCY, f"{adapter.most} requests were in flight at once"
    assert elapsed < 6 * 0.05, f"six requests took {elapsed:.2f}s, which is serial"
    # Order is what lets a vector be matched to the source that asked for it.
    assert [int(first) for first, _second in vectors[:MAX_EMBED_BATCH]] == list(range(MAX_EMBED_BATCH))


def test_one_request_is_not_sent_through_a_pool():
    """A single chunk keeps the path the query leg and every small pass already use."""
    adapter = _adapter()
    assert len(adapter.embed_texts(["TEST one"], remaining_seconds=30)) == 1
    assert adapter.requests == [1]


def test_long_texts_are_split_to_stay_within_the_ledger_s_request_size():
    """The ledger refuses a body over its max_request_bytes before it is sent, and the worker then defers the
    whole group an hour.  A hundred long sources made about 600 KB against 128 KB, every pass, for eight hours
    on the pilot's store; the rebuild stalled on them without one request leaving."""
    limit = 131072
    adapter = _adapter(max_request_bytes=limit)
    texts = [f"TEST {index} " + "长文" * 1500 for index in range(MAX_EMBED_BATCH)]
    vectors = adapter.embed_texts(texts, remaining_seconds=60)
    assert len(adapter.requests) > 1 and sum(adapter.requests) == len(texts)
    assert max(adapter.bodies) <= limit, adapter.bodies
    assert len(vectors) == len(texts)


def test_a_text_too_long_for_any_request_still_goes_alone():
    adapter = _adapter(max_request_bytes=1000)
    adapter.embed_texts(["TEST short", "TEST " + "x" * 5000, "TEST also short"], remaining_seconds=60)
    assert adapter.requests == [1, 1, 1]

