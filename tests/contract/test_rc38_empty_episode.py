"""An episode with nothing to say does not take a packet slot.

Every plain chat turn opens an episode, and its resume stays unwritten until consolidation
summarises it.  Until then the body is ``{"state": "unknown"}``, which reached recall as an
item of its own: a slot spent saying nothing.  Asked for by name it is still delivered,
because then the caller wanted that object.
"""
from __future__ import annotations

from tests.contract.test_rc33_recall_accuracy import _packet  # noqa: F401  (helper)
from tests.contract.test_v11_claims import app, capture  # noqa: F401  (fixture)
from tests.v11_support import recall_request


def test_an_episode_with_no_summary_does_not_take_a_packet_slot(app):
    """Recall it by name and it is still delivered; that is the caller's own ask."""
    core, ctx = app
    source = capture(core, ctx, "TEST-project 发布窗口定在周五晚上十点。", when="2026-09-02T09:00:00Z")
    items = _packet(core, ctx, "TEST-project 发布窗口")["items"]
    assert any(item["ref"] == source.ref for item in items)
    assert not [item for item in items if item["kind"] == "episode" and "unknown" in item["content"]]

    with core.storage.read(ctx) as tx:
        episode = tx.episodes.source_episode(source.ref, source.revision)
    assert episode is not None, "the turn did open one"
    asked = core.recall_packet(ctx, recall_request(query="TEST-project 发布窗口", mode="current", max_items=6,
                                                   focus_refs=[f"{episode.ref}@{episode.revision}"]),
                               deadline_seconds=30, background_without_evidence=False)
    assert any(item["ref"] == episode.ref for item in asked["items"])
