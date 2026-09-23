"""A group of embeddings is asked for, written and recorded as a group, claims included.

The pilot's rebuild of 32,000 imported embeddings measured where a pass spent its time.
Sources went a hundred to a request and one commit per group, yet a pass of five hundred
took thirty seconds and a pass of a thousand outlived its sixty-second lease: each member
was then recorded on its own in three transactions.  Claims were never grouped at all --
one request each, one after another, about two seconds a claim.  And members left to ask
for themselves were still asked after the lease they were claimed with had run out, their
vectors paid for and dropped as stale.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

from scope_recall.adapters.lance import LanceEmbedPort
from scope_recall.core.recall_policy import claim_embedding_text, encode_embedding_text

from test_v11_claims import accept, app, capture, draft  # noqa: F401  (fixtures)


def _queue_only_embeds(core) -> None:
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type<>'embed'")
        conn.commit()


def _sources(core, ctx, count, *, tag):
    made = [capture(core, ctx, f"TEST 分组记账第{index}条。", key=f"TEST-{tag}/{index}") for index in range(count)]
    _queue_only_embeds(core)
    return made


def _claims(core, ctx, count):
    """``count`` active claims, each with the source it was drawn from: two embeddings apiece."""
    made = []
    for index in range(count):
        source = capture(core, ctx, f"TEST-project 属性{index} 值{index}。", key=f"TEST-claim-source/{index}")
        item = accept(core, ctx, draft(source, f"值{index}", predicate=f"属性{index}")).items[0]
        assert item.state == "active"
        made.append(item)
    _queue_only_embeds(core)
    return made


def _embed_rows(core):
    with sqlite3.connect(core.storage.path) as conn:
        return conn.execute("SELECT subject_ref, state, attempt FROM work_items WHERE work_type='embed'").fetchall()


class GroupPort:
    """Answers groups of both kinds, and remembers how it was asked and what it committed."""

    def __init__(self) -> None:
        self.asked: dict[str, list[int]] = {"source": [], "claim": []}
        self.committed: dict[str, list[int]] = {"source": [], "claim": []}
        self.singles = 0

    def prepare_sources(self, sources, *, remaining_seconds=1.0):
        self.asked["source"].append(len(sources))
        return [("source", source.ref, source.revision) for source in sources]

    def prepare_claims(self, claims, *, remaining_seconds=1.0):
        self.asked["claim"].append(len(claims))
        return [("claim", claim.ref, claim.revision) for claim in claims]

    def prepare_source(self, source, *, remaining_seconds=1.0):
        self.singles += 1
        return ("source", source.ref, source.revision)

    def prepare_claim(self, claim, *, remaining_seconds=1.0):
        self.singles += 1
        return ("claim", claim.ref, claim.revision)

    def publish_sources(self, prepared, *, sources, lease_tokens, lease_owner, lease_guard, remaining_seconds=1.0):
        assert list(prepared) == [("source", s.ref, s.revision) for s in sources], "a vector reached the wrong source"
        assert lease_guard()
        self.committed["source"].append(len(prepared))

    def publish_claims(self, prepared, *, claims, lease_tokens, lease_owner, lease_guard, remaining_seconds=1.0):
        assert list(prepared) == [("claim", c.ref, c.revision) for c in claims], "a vector reached the wrong claim"
        assert lease_guard()
        self.committed["claim"].append(len(prepared))

    def publish_source(self, prepared, *, source, lease_token, lease_owner, lease_guard, remaining_seconds=1.0):
        raise AssertionError("a member of a written group was published again on its own")

    def publish_claim(self, prepared, *, claim, lease_token, lease_owner, lease_guard, remaining_seconds=1.0):
        raise AssertionError("a member of a written group was published again on its own")


def test_claims_are_asked_for_and_written_as_a_group_like_sources(app):
    core, ctx = app
    made = _claims(core, ctx, 5)
    kinds = sorted(ref.split("-", 1)[0] for ref, _state, _attempt in _embed_rows(core))
    assert kinds == ["claim"] * len(made) + ["event"] * len(made), kinds
    port = GroupPort()
    receipt = core.drain_worker(ctx, max_items=32, remaining_seconds=30, owner_id="TEST-claim-group", embed=port)
    assert port.asked == {"source": [5], "claim": [5]}, port.asked
    assert port.committed == {"source": [5], "claim": [5]}, port.committed
    assert port.singles == 0, "a claim was still asked for on its own"
    assert receipt.completed == 10, receipt
    assert {state for _ref, state, _attempt in _embed_rows(core)} == {"done"}


def test_a_written_group_is_recorded_in_a_few_transactions_not_three_per_member(app):
    core, ctx = app
    made = _sources(core, ctx, 40, tag="record")
    writes = []
    real_write = core.storage.write

    def counted_write(*args, **kwargs):
        writes.append(1)
        return real_write(*args, **kwargs)

    core.storage.write = counted_write
    receipt = core.drain_worker(ctx, max_items=64, remaining_seconds=30, owner_id="TEST-record", embed=GroupPort())
    assert receipt.completed == len(made), receipt
    assert len(writes) < 20, f"{len(writes)} write transactions for one group of {len(made)}"


class SteppingClock:
    """Monotonic seconds and UTC time that move only when a request says it took time."""

    def __init__(self) -> None:
        self.seconds = 0

    def monotonic(self):
        return float(self.seconds)

    def utc_now(self):
        moment = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc) + timedelta(seconds=self.seconds)
        return moment.isoformat().replace("+00:00", "Z")


class SlowSingles:
    """A port that cannot batch, and whose every request takes four seconds."""

    def __init__(self, clock) -> None:
        self.clock = clock
        self.asked: list[str] = []

    def prepare_source(self, source, *, remaining_seconds=1.0):
        self.asked.append(source.ref)
        self.clock.seconds += 4
        return ("source", source.ref, source.revision)

    def publish_source(self, prepared, *, source, lease_token, lease_owner, lease_guard, remaining_seconds=1.0):
        assert lease_guard(), "published against a lease that had run out"


def test_members_left_to_ask_for_themselves_go_back_before_their_lease_runs_out(app):
    """Twenty members at four seconds each is eighty seconds against a sixty-second lease.

    Asked regardless, the last of them were asked after the lease ran out and dropped as
    stale, then asked and paid for again by a later group.  Handed back while there is still
    time, each is asked exactly once, under a lease of its own.
    """
    core, ctx = app
    made = _sources(core, ctx, 20, tag="lease")
    clock = SteppingClock()
    core.clock = clock
    port = SlowSingles(clock)
    receipt = core.drain_worker(ctx, max_items=64, remaining_seconds=1000, owner_id="TEST-lease", embed=port)
    stale = [item for item in receipt.items if item.disposition == "stale"]
    assert not stale, f"{len(stale)} members were asked after their lease ran out"
    assert sorted(port.asked) == sorted(source.ref for source in made), "a member was asked for more than once"
    assert {(state, attempt) for _ref, state, attempt in _embed_rows(core)} == {("done", 1)}


def test_a_claim_group_is_encoded_exactly_as_one_claim_is():
    """One vector per claim whichever way it was asked for, or search finds two different claims."""

    class Claim:
        def __init__(self, index):
            self.ref, self.revision = f"claim-TEST-{index}", 1
            self.scope_id, self.project_id, self.branch_id = "TEST-scope", None, None
            self.payload = dict(kind="decision", subject="TEST-project", predicate=f"属性{index}",
                                value_text=f"值{index}", conditions=[])

    class Embedding:
        def __init__(self):
            self.texts: list[str] = []

        def embed_source(self, source, *, remaining_seconds=1.0):
            raise AssertionError("no source here")

        def embed_text(self, text, *, remaining_seconds=1.0):
            self.texts.append(encode_embedding_text(text, kind="document"))
            return (0.5, 0.5)

        def embed_texts(self, encoded, *, remaining_seconds=1.0):
            self.texts.extend(encoded)
            return tuple((0.5, 0.5) for _ in encoded)

    claims = [Claim(index) for index in range(3)]
    grouped, single = Embedding(), Embedding()
    kwargs = dict(agent_id="TEST-agent", installation_id="TEST-installation", embedding_space="TEST-space")
    LanceEmbedPort(object(), grouped, **kwargs).prepare_claims(claims, remaining_seconds=5)
    for claim in claims:
        LanceEmbedPort(object(), single, **kwargs).prepare_claim(claim, remaining_seconds=5)
    assert grouped.texts == single.texts
    assert grouped.texts[0] == encode_embedding_text(claim_embedding_text(claims[0].payload), kind="document")


def test_the_runtime_boundary_offers_the_claim_group_it_bounds():
    """The worker probes the bounded port; a group method it cannot see through it does not exist."""
    from scope_recall.runtime.instance import _BoundedEmbed

    class Port:
        def prepare_claims(self, claims, *, remaining_seconds=1.0):
            return ("prepared", remaining_seconds)

        def publish_claims(self, prepared, *, claims, lease_tokens, lease_owner, lease_guard, remaining_seconds=1.0):
            return ("published", remaining_seconds)

    bounded = _BoundedEmbed(Port(), 45.0)
    assert bounded.prepare_claims((), remaining_seconds=120.0) == ("prepared", 45.0)
    assert bounded.publish_claims((), claims=(), lease_tokens=(), lease_owner="TEST-owner",
                                  lease_guard=lambda: True, remaining_seconds=120.0) == ("published", 45.0)
