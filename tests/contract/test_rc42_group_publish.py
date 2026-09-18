"""A pass writes its group's vectors in one fenced commit.

The store's write API is plural and the adapter used it one row at a time, so a pass paid a
lock-held handshake and a dataset commit per source -- measured at nine times the cost of one
commit carrying all 32, on a real store at real dimensions.

The fence is what must not move.  A grouped commit is guarded once for the whole group, the
guard answers for every member while the helper holds the lock, and a group it refuses writes
nothing and leaves each member to publish on its own fence exactly as before.
"""
from __future__ import annotations

import sqlite3

import pytest

from scope_recall.adapters.lance import LanceEmbedPort
from scope_recall.vector.process_store import ProcessLanceVectorStore

from test_v11_claims import Clock, app, capture  # noqa: F401  (fixtures)


class FixedEmbedding:
    """Marked TEST-only embedding: no network, and a batch answers in order."""

    def embed_source(self, source, *, remaining_seconds=1.0):
        return (0.25, 0.75)

    def embed_sources(self, sources, *, remaining_seconds=1.0):
        return tuple((0.25, 0.75) for _ in sources)


class CountingStore(ProcessLanceVectorStore):
    """The real native store, plus how often it was asked to commit and with how many rows."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.commits: list[int] = []

    def fenced_upsert_records(self, rows, *, guard, remaining_seconds):
        rows = list(rows)
        self.commits.append(len(rows))
        return super().fenced_upsert_records(rows, guard=guard, remaining_seconds=remaining_seconds)


def _sources(core, ctx, count, *, tag):
    made = [capture(core, ctx, f"TEST 分组提交第{index}条。", key=f"TEST-{tag}/{index}") for index in range(count)]
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='consolidate'")
        conn.commit()
    return made


def _port(store, ctx):
    return LanceEmbedPort(store, FixedEmbedding(), agent_id=ctx.binding.agent_id,
                          installation_id=ctx.binding.installation_id, embedding_space="TEST-rc42-space")


def _embed_states(core):
    """One row per (ref, revision): two revisions of one ref are two pieces of work."""
    with sqlite3.connect(core.storage.path) as conn:
        return {(row[0], row[1]): row[2] for row in conn.execute(
            "SELECT subject_ref, subject_revision, state FROM work_items WHERE work_type='embed'")}


@pytest.fixture
def store(tmp_path):
    made = CountingStore(tmp_path / "lance", table_name="TEST_vectors", dimensions=2)
    made.open()
    try:
        yield made
    finally:
        made.close()


def test_a_pass_commits_its_group_once(app, store):
    core, ctx = app
    core.clock = Clock()
    made = _sources(core, ctx, 6, tag="once")
    receipt = core.drain_worker(ctx, max_items=16, remaining_seconds=30, owner_id="rc42", embed=_port(store, ctx))
    assert receipt.completed == len(made)
    assert store.commits == [len(made)], store.commits
    assert set(_embed_states(core).values()) == {"done"}


def test_a_group_whose_commit_fails_leaves_each_member_its_own_fence(app, store):
    """A refused group is not a new way to fail: each member still publishes for itself."""
    core, ctx = app
    core.clock = Clock()
    made = _sources(core, ctx, 4, tag="refuse")
    port = _port(store, ctx)
    offered: list[int] = []

    def refuse_the_group(prepared, **kwargs):
        offered.append(len(prepared))
        raise AssertionError("TEST refuses this group")

    port.publish_sources = refuse_the_group
    core.drain_worker(ctx, max_items=16, remaining_seconds=30, owner_id="rc42-refuse", embed=port)
    assert offered == [len(made)], "the group was offered exactly once"
    assert store.commits == [1] * len(made), store.commits
    assert set(_embed_states(core).values()) == {"done"}


def test_a_source_superseded_before_the_commit_stops_the_group_not_the_rest(app, store):
    core, ctx = app
    core.clock = Clock()
    made = _sources(core, ctx, 4, tag="dies")
    doomed = made[1]
    port = _port(store, ctx)
    group_commit = port.publish_sources

    def supersede_then_commit(prepared, **kwargs):
        # A newer revision of one member lands between the group's read and its
        # commit, which is precisely what the guard is there to catch.
        capture(core, ctx, "TEST 分组提交第1条。", key="TEST-dies/1", revision=2)
        return group_commit(prepared, **kwargs)

    port.publish_sources = supersede_then_commit
    core.drain_worker(ctx, max_items=16, remaining_seconds=30, owner_id="rc42-dies", embed=port)
    states = _embed_states(core)
    assert states[(doomed.ref, 1)] != "done", "a superseded revision was published anyway"
    assert sum(1 for state in states.values() if state == "done") >= 2, "the live members still published"


def test_the_runtime_boundary_offers_the_group_commit_it_bounds():
    """The worker probes the port it is handed, which is the bounded one.

    rc40 shipped a batch the worker could not see because the wrapper's method
    list did not name it; the group commit must not repeat that.
    """
    from scope_recall.runtime.instance import _BoundedEmbed

    class Port:
        def publish_sources(self, prepared, *, sources, lease_tokens, lease_owner, lease_guard,
                            remaining_seconds=1.0):
            return ("published", remaining_seconds)

    bounded = _BoundedEmbed(Port(), 45.0)
    assert callable(getattr(bounded, "publish_sources", None))
    assert bounded.publish_sources((), sources=(), lease_tokens=(), lease_owner="TEST-owner",
                                   lease_guard=lambda: True, remaining_seconds=120.0) == ("published", 45.0)
