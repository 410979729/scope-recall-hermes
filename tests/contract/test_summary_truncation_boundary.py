"""Long source sentences must not suppress their unshown original text."""

from dataclasses import replace

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
