"""A pass embeds its ready sources in one request, and publishes each on its own fence.

Both embedding dialects take an array -- Google's endpoint is literally
``batchEmbedContents`` -- and the adapter sent arrays of one, so a store of 167,000 sources
was 167,000 requests to rebuild: days of wall clock for minutes of tokens.  Sharing the
request changes what the provider is asked and nothing else: every item is still read,
fenced, published and finished by itself, and a group that fails falls back to one request
each rather than inventing a failure of its own.
"""
from __future__ import annotations

import sqlite3

import pytest

from scope_recall.adapters.models import (
    AuxiliaryModelError,
    MAX_EMBED_BATCH,
    _embedding_vectors,
    build_gemini_embed_body,
    build_openai_embed_body,
)
from tests.contract.test_v11_claims import app, capture  # noqa: F401  (fixture)


def _sources(core, ctx, count, *, tag="batch"):
    made = [capture(core, ctx, f"TEST 向量批量第{index}条。", key=f"TEST-{tag}/{index}") for index in range(count)]
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='consolidate'")
        conn.commit()
    return made


def _embed_rows(core):
    with sqlite3.connect(core.storage.path) as conn:
        return {row[0]: row[1] for row in conn.execute(
            "SELECT subject_ref, state FROM work_items WHERE work_type='embed'")}


class Recording:
    """Answers both ways, and remembers how it was asked."""

    def __init__(self, *, batch=True, short=False, fail=False) -> None:
        self.groups: list[int] = []
        self.singles = 0
        self.published: list[str] = []
        self._short, self._fail = short, fail
        if not batch:
            # A port that offers no batch method at all, which is what every
            # port was until now.
            self.prepare_sources = None

    def prepare_sources(self, sources, *, remaining_seconds=1.0):
        self.groups.append(len(sources))
        if self._fail:
            raise AuxiliaryModelError("http_status", detail="429")
        prepared = [{"ref": source.ref, "revision": source.revision} for source in sources]
        return prepared[:-1] if self._short else prepared

    def prepare_source(self, source, *, remaining_seconds=1.0):
        self.singles += 1
        return {"ref": source.ref, "revision": source.revision}

    def publish_source(self, prepared, *, source, lease_token, lease_owner, lease_guard, remaining_seconds=1.0):
        assert prepared == {"ref": source.ref, "revision": source.revision}, "a vector reached the wrong source"
        assert lease_guard()
        self.published.append(source.ref)


def test_a_pass_asks_for_its_ready_sources_in_one_request(app):
    core, ctx = app
    made = _sources(core, ctx, 5)
    port = Recording()
    receipt = core.drain_worker(ctx, max_items=16, remaining_seconds=20, owner_id="batch", embed=port)
    assert port.groups == [5], port.groups
    assert port.singles == 0
    assert sorted(port.published) == sorted(source.ref for source in made)
    assert receipt.completed == len(made)
    assert set(_embed_rows(core).values()) == {"done"}


def test_a_group_that_fails_leaves_each_item_to_ask_for_itself(app):
    """A refused group is not a new way to fail: each item spends its own attempt."""
    core, ctx = app
    made = _sources(core, ctx, 4, tag="fail")
    port = Recording(fail=True)
    core.drain_worker(ctx, max_items=16, remaining_seconds=20, owner_id="batch-fail", embed=port)
    assert port.groups == [4] and port.singles == 4
    assert sorted(port.published) == sorted(source.ref for source in made)


def test_a_short_answer_is_not_matched_to_the_wrong_sources(app):
    """Publishing a vector against the wrong source is worse than not publishing."""
    core, ctx = app
    made = _sources(core, ctx, 4, tag="short")
    port = Recording(short=True)
    core.drain_worker(ctx, max_items=16, remaining_seconds=20, owner_id="batch-short", embed=port)
    assert port.groups == [4] and port.singles == 4, "the group was discarded whole"
    assert sorted(port.published) == sorted(source.ref for source in made)


def test_a_port_that_cannot_batch_is_unchanged(app):
    core, ctx = app
    made = _sources(core, ctx, 3, tag="single")
    port = Recording(batch=False)
    core.drain_worker(ctx, max_items=16, remaining_seconds=20, owner_id="batch-none", embed=port)
    assert port.singles == 3 and sorted(port.published) == sorted(source.ref for source in made)


def test_a_source_that_dies_inside_a_group_stops_only_itself(app):
    core, ctx = app
    made = _sources(core, ctx, 4, tag="dies")
    doomed = made[1]

    class Dying(Recording):
        def publish_source(self, prepared, *, source, **kwargs):
            if source.ref == doomed.ref:
                capture(core, ctx, "TEST 向量批量第1条。", key="TEST-dies/1", revision=2)
                kwargs["lease_guard"]()
                raise AssertionError("a dead subject must not reach publication")
            return super().publish_source(prepared, source=source, **kwargs)

    port = Dying()
    core.drain_worker(ctx, max_items=16, remaining_seconds=20, owner_id="batch-dies", embed=port)
    rows = _embed_rows(core)
    assert rows[doomed.ref] != "done"
    assert [ref for ref in rows if rows[ref] == "done"], "the rest of the group was published"


# -- the request and the answer ------------------------------------------------

def test_one_request_carries_every_text():
    import json

    gemini = json.loads(build_gemini_embed_body(["a", "b", "c"], model="m", dimensions=64))
    assert [entry["content"]["parts"][0]["text"] for entry in gemini["requests"]] == ["a", "b", "c"]
    assert json.loads(build_openai_embed_body(["a", "b"], model="m", dimensions=64))["input"] == ["a", "b"]
    # One text still reads exactly as it did.
    assert len(json.loads(build_gemini_embed_body("a", model="m", dimensions=64))["requests"]) == 1


def test_an_answer_of_the_wrong_length_is_refused():
    values = [1.0, 2.0]
    payload = {"embeddings": [{"values": values}, {"values": values}]}
    assert len(_embedding_vectors(payload, dialect="gemini", dimensions=2, count=2)) == 2
    for count in (1, 3):
        with pytest.raises(AuxiliaryModelError):
            _embedding_vectors(payload, dialect="gemini", dimensions=2, count=count)


def test_an_openai_answer_is_read_in_the_order_it_names():
    payload = {"data": [{"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}]}
    first, second = _embedding_vectors(payload, dialect="openai", dimensions=2, count=2)
    assert first == (1.0, 0.0) and second == (0.0, 1.0)


def test_the_batch_has_a_ceiling():
    with pytest.raises(AuxiliaryModelError):
        build_gemini_embed_body([])
    assert MAX_EMBED_BATCH >= 8


def test_the_runtime_boundary_offers_the_batch_it_bounds():
    """The worker probes the port it is handed, which is the bounded one.

    The first version of this change listed four methods on that wrapper and
    not the fifth, so the capability existed everywhere except where it is
    asked for, and every request carried one document.
    """
    from scope_recall.runtime.instance import _BoundedEmbed

    class Port:
        def prepare_sources(self, sources, *, remaining_seconds=1.0):
            return [("v", remaining_seconds) for _ in sources]

        def prepare_source(self, source, *, remaining_seconds=1.0):
            return ("v", remaining_seconds)

    bounded = _BoundedEmbed(Port(), 45.0)
    assert callable(getattr(bounded, "prepare_sources", None))
    assert bounded.prepare_sources([1, 2], remaining_seconds=120.0) == [("v", 45.0), ("v", 45.0)], "still clamped"
