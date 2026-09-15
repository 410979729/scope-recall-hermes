from dataclasses import replace

import pytest

from scope_recall.core.retrieval import CandidateRef, SearchContext, SearchLimits
from scope_recall.core.retrieval_storage import RetrievalStorage
from test_v11_claims import app, capture
from test_v11_episodes import apply, artifact, ref
from v11_support import recall_request


@pytest.fixture
def versions(app, tmp_path):
    core, ctx = app
    ctx = replace(ctx, task_anchor="TEST-history-states")
    one, _, _ = artifact(core, ctx, tmp_path, label="TEST-v1")
    two, _, _ = artifact(core, ctx, tmp_path, version=2, label="TEST-v2")
    source = capture(core, ctx, "TEST 那个颜色是TEST-v1。")
    proposal = dict(mention="那个颜色", candidate_refs=[ref(one), ref(two)],
                    resolved_ref=ref(one), resolution="resolved", evidence_refs=[ref(source)])
    initial = apply(core, ctx, references=[proposal]).items[0]
    assert core.reference(ctx, initial.ref, 1).payload["resolution"] == "resolved"
    clarification = capture(core, ctx, "刚才“那个颜色”说的是TEST-v2。")
    final = apply(core, ctx, references=[dict(proposal, evidence_refs=[ref(clarification)])]).items[0]
    assert initial.ref == final.ref and final.revision == 2
    episode, = core.episodes(ctx)
    return core, ctx, {"artifact": (one.ref, two.revision),
                       "episode": (episode.ref, episode.revision),
                       "reference": (initial.ref, final.revision)}


@pytest.mark.parametrize("kind", ["artifact", "episode", "reference"])
@pytest.mark.parametrize("mode", ["history", "as_of"])
def test_old_object_version_never_claims_to_be_the_current_head(versions, kind, mode):
    core, ctx, objects = versions
    identity, head = objects[kind]
    assert head > 1
    as_of = "2026-09-07T12:00:00Z" if mode == "as_of" else None
    search = SearchContext("TEST", mode, as_of, (f"{identity}@1",), SearchLimits(),
                           core.clock.monotonic() + 10, core.clock.utc_now(), ctx)
    with core.storage.read(ctx) as tx:
        old = RetrievalStorage().hydrate(tx, CandidateRef(kind, identity, 1, "exact_ref"), search)
        current = RetrievalStorage().hydrate(tx, CandidateRef(kind, identity, head, "exact_ref"), search)
    assert old is not None and old.temporal_status == "historical"
    assert current is not None and current.temporal_status == "current"
    packet = core.recall_packet(ctx, recall_request(query="TEST", mode=mode, **({"as_of": as_of} if as_of else {}),
        focus_refs=[f"{identity}@1", f"{identity}@{head}"], max_items=16, budget_tokens=4000), deadline_seconds=10)
    states = {(item["ref"], item["revision"]): item["temporal_status"] for item in packet["items"]}
    assert states[(identity, 1)] == "historical"
    assert states[(identity, head)] == "current"
