"""Long source sentences must not suppress their unshown original text."""

import json
from dataclasses import replace

from scope_recall.core.consolidation_chunks import ChunkedSource, ConsolidationChunk
from scope_recall.core.summary_validity import summary_live
from test_claims import capture
from test_importance_summaries import app, build, scoped  # noqa: F401
from v11_support import source_event


def test_single_sentence_crossing_prefix_remains_a_raw_candidate(app):
    core, ctx = app
    content = "TEST-project " + "details " * 110 + "complete."
    event = source_event(
        source_event_key="TEST-long-single-sentence",
        role="assistant",
        origin="assistant_visible",
        content=content,
        occurred_at="2026-09-05T10:00:00Z",
    )
    saved = core.record_event(
        replace(ctx, actor_origin="assistant_visible"), event, scope_id="TEST-scope", remaining_seconds=10
    )

    class PrefixModel:
        def judge(self, messages, *, remaining_seconds):
            import json

            source = json.loads(messages[-1]["content"])["sources"][0]
            return json.dumps(
                dict(sentences=[dict(text=source["content"][:40], source_ref=source["ref"], source_revision=1)])
            )

    assert build(core, ctx, PrefixModel()).failed == 0
    assert any(item.ref == saved.event_refs[0].ref for item in scoped(core, ctx))


def test_expanded_sentence_must_fit_actual_page_window(app, monkeypatch):
    from types import SimpleNamespace

    from scope_recall.core import worker_summaries

    core, ctx = app
    content = "TEST " + "x" * 888 + "."
    assert len(content) == 894
    source = capture(core, ctx, content)

    values = dict(source.__dict__, event=dict(source.event, content=content[:874]))
    window = ChunkedSource(**values, consolidation_window=ConsolidationChunk(0, 874, 894))
    task = dict(ref="TEST-task", resume=dict(text=""))
    baseline = sum(len(m["content"].encode()) for m in worker_summaries.summary_messages((window,), episode_ref=task))
    task["resume"]["text"] = "r" * (12000 - baseline)
    episode = SimpleNamespace(ref=task["ref"], resume=task["resume"])
    member = dict(ref=source.ref, revision=source.revision, total=894)
    monkeypatch.setattr(worker_summaries, "unprocessed_ranges", lambda tx, group: [(0, 0, 894)])
    monkeypatch.setattr(worker_summaries, "obsolete_ranges", lambda tx, members: {})
    with core.storage.read(ctx) as tx:
        sources, _coverage, fitted, omitted = worker_summaries._read_page(
            tx, dict(manifest_json=json.dumps([member])), episode
        )
    assert sources[0].consolidation_window == ConsolidationChunk(0, 874, 894)
    assert fitted == task and not omitted

    def page(tx, group, episode):
        return sources, [[source.ref, source.revision, 0, 894, 894]], task, False

    class PrefixModel:
        def judge(self, messages, *, remaining_seconds):
            item = json.loads(messages[-1]["content"])["sources"][0]
            assert len(item["content"]) == 874
            return dict(sentences=[dict(text=item["content"][:40], source_ref=item["ref"], source_revision=1)])

    monkeypatch.setattr(worker_summaries, "_read_page", page)
    assert build(core, ctx, PrefixModel()).failed == 0
    with core.storage.read(ctx) as tx:
        parts = tx._check().execute("SELECT claim_ref,claim_revision FROM summary_parts").fetchall()
        assert parts and not any(summary_live(tx, *part) for part in parts)


def test_expanded_sentence_must_not_start_before_page_window(app, monkeypatch):
    from scope_recall.core import worker_summaries

    core, ctx = app
    content = "TEST original opening " + "x" * 200 + "."
    source = capture(core, ctx, content)
    values = dict(source.__dict__, event=dict(source.event, content=content[20:]))
    window = ChunkedSource(**values, consolidation_window=ConsolidationChunk(20, len(content), len(content)))
    monkeypatch.setattr(
        worker_summaries,
        "_read_page",
        lambda tx, group, episode: (
            (window,),
            [[source.ref, source.revision, 0, len(content), len(content)]],
            None,
            False,
        ),
    )

    class PrefixModel:
        def judge(self, messages, *, remaining_seconds):
            item = json.loads(messages[-1]["content"])["sources"][0]
            return dict(sentences=[dict(text=item["content"][:40], source_ref=item["ref"], source_revision=1)])

    assert build(core, ctx, PrefixModel()).failed == 0
    with core.storage.read(ctx) as tx:
        parts = tx._check().execute("SELECT claim_ref,claim_revision FROM summary_parts").fetchall()
        assert parts and not any(summary_live(tx, *part) for part in parts)
