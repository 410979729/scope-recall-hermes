"""A missing resume slot gets exactly one directed, source-grounded followup."""
from dataclasses import replace
import time

from scope_recall.core.background_context import BACKGROUND_PREFIX
from scope_recall.core.recall import RetrievalPipeline
from scope_recall.core.retrieval import SearchContext
from scope_recall.core.retrieval_storage import RetrievalStorage
from tests.contract.test_autonomous_context import app, task
from tests.contract.test_p08_evidence_followup import LexicalBlockedStorage, RoundTrackingVectors
from tests.v11_support import recall_request


def test_resume_followup_supplies_unambiguous_task_without_model_loop(app):
    core, ctx = app
    ctx = replace(ctx, task_anchor="TEST-resume-task")
    episode = task(core, ctx, "TEST 海报排版还未完成")
    vectors = RoundTrackingVectors()
    core.recall_pipeline = RetrievalPipeline(core.storage, vector_port=vectors, clock=core.clock,
                                            storage_reader=LexicalBlockedStorage(clock=core.clock))
    result = core.recall(ctx, recall_request(query="继续上次的工作"))
    assert [item.ref for item in result.items if item.kind == "episode"] == [episode.ref]
    assert len(vectors.calls) == 2
    assert vectors.calls[1]["query"].endswith("未完成任务 下一步")
    assert "resume_state" not in result.unmet_needs
    assert all(not item.applicability.startswith(BACKGROUND_PREFIX) for item in result.items)


def test_resume_has_no_cross_task_guess_or_extra_rounds(app):
    core, ctx = app
    task(core, replace(ctx, task_anchor="TEST-a"), "TEST 海报工作还未完成")
    task(core, replace(ctx, task_anchor="TEST-b"), "TEST 报价工作还未完成")
    vectors = RoundTrackingVectors()
    core.recall_pipeline = RetrievalPipeline(core.storage, vector_port=vectors, clock=core.clock,
                                            storage_reader=LexicalBlockedStorage(clock=core.clock))
    result = core.recall(ctx, recall_request(query="继续"))
    assert result.items == ()
    assert "resume_state" in result.unmet_needs
    assert len(vectors.calls) == 2


def test_expired_deadline_does_not_load_background_or_start_followup(app):
    core, ctx = app
    vectors = RoundTrackingVectors()
    pipeline = RetrievalPipeline(core.storage, vector_port=vectors, clock=core.clock)
    context = SearchContext.from_request(recall_request(query="继续"), ctx,
                                         now=core.clock.utc_now(), deadline=core.clock.monotonic() - 1)
    result = pipeline.search(context)
    assert result.items == () and vectors.calls == []
    assert result.gaps == ("deadline_exceeded",)


def test_historical_resume_does_not_inject_current_task(app):
    core, ctx = app
    task(core, replace(ctx, task_anchor="TEST-today"), "TEST 当下任务还未完成")
    vectors = RoundTrackingVectors()
    core.recall_pipeline = RetrievalPipeline(core.storage, vector_port=vectors, clock=core.clock,
                                            storage_reader=LexicalBlockedStorage(clock=core.clock))
    result = core.recall(ctx, recall_request(query="继续过去任务", mode="as_of", as_of="2020-01-01T00:00:00Z"))
    assert result.items == ()


def test_a_slow_local_channel_does_not_starve_the_semantic_channel(app):
    """The remote search runs beside the local channels, not after them.

    Measured on this instance: a host prefetch spent 3.66 s of its five-second window and
    the search, run last, was handed what was left -- a transport timeout, and the recall
    lost its meaning channel.  Started with collection, it keeps a slice of its own.
    """
    core, ctx = app
    observed: dict[str, float] = {}

    class SlowLocal(RetrievalStorage):
        """A local channel that spends most of a short window before returning."""

        def lexical(self, tx, context: SearchContext, *, limit: int):
            time.sleep(0.45)
            return ()

        def recent(self, tx, context: SearchContext, *, limit: int):
            return ()

    class RecordingVectors:
        def search(self, context: SearchContext, *, limit: int, remaining_seconds: float):
            observed["remaining_seconds"] = remaining_seconds
            return ()

    pipeline = RetrievalPipeline(core.storage, vector_port=RecordingVectors(), clock=core.clock,
                                 storage_reader=SlowLocal(clock=core.clock))
    context = SearchContext.from_request(
        recall_request(query="TEST 慢本地通道"), ctx,
        now=core.clock.utc_now(), deadline=core.clock.monotonic() + 0.5,
    )
    result = pipeline.search(context)
    assert "vector_unavailable" not in result.gaps
    assert "deadline_exceeded_vector" not in result.gaps
    # Most of the window, not the crumbs a channel that ran after the local one would get.
    assert observed["remaining_seconds"] > 0.2, observed
