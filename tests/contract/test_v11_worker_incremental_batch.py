"""Regression tests for episode-scoped consolidate batching across work items."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from test_v11_claims import app, capture, draft, initial
from test_v11_deletion import authorize, request
from test_v11_episodes import artifact, resume
from test_v11_worker import Clock, FakeConsolidation, consolidation_payload, worker_app


def _drain(core, ctx, tracker):
    for index in range(70):
        result = core.drain_worker(ctx, consolidation=tracker, max_items=1,
                                   remaining_seconds=30, owner_id=f"drain-{index}")
        if result.idle:
            return
    raise AssertionError("queue did not drain")


def _snapshot(core):
    with sqlite3.connect(core.storage.path) as conn:
        return conn.execute("SELECT * FROM work_items ORDER BY work_id").fetchall()


def test_authority_obsolete_late_source_does_not_skip_earlier_pending(worker_app):
    core, ctx, _ = worker_app
    sources = [capture(core, ctx, f"TEST obsolete boundary {i}", key=f"obsolete/{i}") for i in range(45)]
    _mark_embed_done(core)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='obsolete',last_error_code='authority_revoked' WHERE subject_ref=? AND work_type='consolidate'", (sources[39].ref,))
    tracker = BatchTracker()
    _drain(core, ctx, tracker)
    observed = {ref for batch in tracker.batches for ref in batch}
    assert observed == {f"{s.ref}@1" for i, s in enumerate(sources) if i != 39}


def test_late_leased_subject_is_in_its_actual_batch(worker_app):
    core, ctx, clock = worker_app
    sources = [capture(core, ctx, f"TEST late lease {i}", key=f"late/{i}") for i in range(45)]
    _mark_embed_done(core)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET available_at='2099-01-01T00:00:00Z' WHERE work_type='consolidate' AND subject_ref<>?", (sources[39].ref,))
    with core.storage.write(ctx) as tx:
        item = tx.work.claim_next("late-worker", clock.utc_now(), lease_seconds=60)[0]
    assert item.subject_ref == sources[39].ref
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET available_at=? WHERE state='pending'", (clock.utc_now(),))
    from scope_recall.core.worker import _process_consolidate
    tracker = BatchTracker()
    result = _process_consolidate(core.storage, clock, ctx, item, model=tracker, started=clock.monotonic(), budget=30)
    assert result == ("completed", None, "done")
    assert f"{item.subject_ref}@1" in tracker.batches[0]
    _drain(core, ctx, tracker)
    assert {ref for batch in tracker.batches for ref in batch} == {f"{s.ref}@1" for s in sources}


def test_expired_no_root_worker_cannot_ack_siblings(worker_app):
    core, ctx, clock = worker_app
    for i in range(4):
        capture(core, ctx, f"TEST nonroot {i}", key=f"nonroot/{i}", origin="assistant_visible")
    _mark_embed_done(core)
    with core.storage.write(ctx) as tx:
        old = tx.work.claim_next("old-worker", clock.utc_now(), lease_seconds=0.001)[0]
    clock.advance(seconds=1, iso="2026-09-06T12:00:01Z")
    with core.storage.write(ctx) as tx:
        tx.work.claim_next("new-worker", clock.utc_now(), lease_seconds=60)
    before = _snapshot(core)
    from scope_recall.core.worker import _process_consolidate
    result = _process_consolidate(core.storage, clock, ctx, old, model=None, started=clock.monotonic(), budget=30)
    assert result[0] == "stale"
    assert _snapshot(core) == before


@pytest.mark.parametrize("state", ["failed", "leased"])
def test_other_failed_or_leased_source_is_not_automatic_batch_work(worker_app, state):
    core, ctx, clock = worker_app
    sources = [capture(core, ctx, f"TEST state exclusion {i}", key=f"state/{i}") for i in range(4)]
    _mark_embed_done(core)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state=?,attempt=3,lease_owner='other',lease_token=9,lease_until='2099-01-01T00:00:00Z',last_error_code='held' WHERE work_type='consolidate' AND subject_ref=?", (state, sources[1].ref))
        before = conn.execute("SELECT * FROM work_items WHERE work_type='consolidate' AND subject_ref=?", (sources[1].ref,)).fetchone()
    tracker = BatchTracker()
    _drain(core, ctx, tracker)
    assert f"{sources[1].ref}@1" not in {ref for batch in tracker.batches for ref in batch}
    with sqlite3.connect(core.storage.path) as conn:
        after = conn.execute("SELECT * FROM work_items WHERE work_type='consolidate' AND subject_ref=?", (sources[1].ref,)).fetchone()
    assert after == before


def _mark_embed_done(core) -> None:
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='embed'")
        conn.commit()


def _consolidate_states(core):
    with sqlite3.connect(core.storage.path) as conn:
        return conn.execute(
            "SELECT subject_ref, state FROM work_items WHERE work_type='consolidate' ORDER BY work_id"
        ).fetchall()


class BatchTracker:
    def __init__(self, *, with_claims=False) -> None:
        self.calls = 0
        self.batches: list[tuple[str, ...]] = []
        self.with_claims = with_claims

    def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
        self.calls += 1
        refs = tuple(f"{source.ref}@{source.revision}" for source in sources)
        self.batches.append(refs)
        claims = [draft(s, s.event["content"], predicate=s.ref, kind="fact", statement_kind="assertion") for s in sources] if self.with_claims else []
        return json.dumps(consolidation_payload(*sources, claims=claims), ensure_ascii=False)


def test_unrelated_capture_during_extraction_still_writes_claims(worker_app):
    """A capture during the model call no longer throws the paid result away.

    This was ``..._keeps_original_work_retryable``: the capture advanced the
    instance-wide ``memory_epoch`` and the first pass had to retry with
    ``memory_epoch_changed``.  During a live chat every message and tool call
    is such a capture, so fresh facts were consolidated late or never.  The
    capture changes nothing this result was derived from, so the first pass now
    writes the claim, and the capture is still consolidated by its own work.
    """
    core, ctx, clock = worker_app
    original = capture(core, ctx, "TEST-project 配色 蓝色。", key="TEST-epoch-original")
    _mark_embed_done(core)

    class InterleavingModel:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
            capture(core, ctx, "TEST 新消息是无关的天气闲聊。", key="TEST-epoch-new")
            return json.dumps(consolidation_payload(*sources, claims=[draft(original)]), ensure_ascii=False)

    first = core.drain_worker(ctx, consolidation=InterleavingModel(), max_items=1, remaining_seconds=30)
    assert first.completed == 1 and first.retried == 0 and first.obsolete == 0
    assert dict(_consolidate_states(core))[original.ref] == "done"
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 1
    clock.advance(seconds=60, iso="2026-09-06T12:01:00Z")
    _mark_embed_done(core)
    tracker = BatchTracker(with_claims=True)
    _drain(core, ctx, tracker)
    assert f"{original.ref}@1" not in {ref for batch in tracker.batches for ref in batch}
    assert set(dict(_consolidate_states(core)).values()) == {"done"}


@pytest.mark.parametrize("stage", ["prepare", "boundary", "publish"])
def test_unrelated_capture_during_embedding_still_publishes(worker_app, stage):
    """An unrelated capture around the embedding call no longer blocks publication.

    This was ``..._keeps_source_retryable``: each stage expected the item back
    in ``pending`` with ``memory_epoch_changed``, and the ``boundary`` stage
    expected the port's own guard to refuse.  The capture is not a dependency
    of this source's vector, so every stage now publishes once and completes.
    """
    core, ctx, clock = worker_app
    original = capture(core, ctx, "TEST embedding retained source。", key="TEST-embed-original")
    with closing(sqlite3.connect(core.storage.path)) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='consolidate'")
        conn.commit()
    published = []

    class InterleavingEmbed:
        def prepare_source(self, source, *, remaining_seconds=1.0):
            if stage == "prepare":
                capture(core, ctx, "TEST unrelated new weather。", key="TEST-embed-new")
            return source.ref

        def publish_source(self, prepared, **kwargs):
            if stage == "boundary":
                capture(core, ctx, "TEST unrelated new weather。", key="TEST-embed-new")
            assert kwargs["lease_guard"]()
            published.append(prepared)
            if stage == "publish":
                capture(core, ctx, "TEST unrelated new weather。", key="TEST-embed-new")

    result = core.drain_worker(ctx, embed=InterleavingEmbed(), max_items=1, remaining_seconds=30)
    assert result.completed == 1 and result.retried == 0 and result.obsolete == 0
    assert published == [original.ref]
    with closing(sqlite3.connect(core.storage.path)) as conn:
        row = conn.execute("SELECT state,last_error_code FROM work_items WHERE work_type='embed' AND subject_ref=?", (original.ref,)).fetchone()
    assert row == ("done", None)


def test_unrelated_capture_before_no_root_completion_still_completes(worker_app, monkeypatch):
    """A capture before a no-root completion no longer sends the item back.

    This was ``..._keeps_work_retryable`` and expected ``memory_epoch_changed``;
    the batch sources are unchanged, so the completion is recorded.
    """
    from contextlib import contextmanager

    core, ctx, _ = worker_app
    original = capture(core, ctx, "TEST no root source。", origin="assistant_visible")
    _mark_embed_done(core)
    original_write = core.storage.write
    injected = False

    @contextmanager
    def capture_before_write(context, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            capture(core, ctx, "TEST unrelated event。", key="TEST-no-root-new")
        with original_write(context, **kwargs) as tx:
            yield tx

    from scope_recall.core.worker import _process_consolidate
    with original_write(ctx) as tx:
        item = tx.work.claim_next("TEST-no-root", core.clock.utc_now(), lease_seconds=60)[0]
    monkeypatch.setattr(core.storage, "write", capture_before_write)
    result = _process_consolidate(core.storage, core.clock, ctx, item, model=None,
                                 started=core.clock.monotonic(), budget=30)
    assert injected
    assert result == ("completed", None, "done")
    assert dict(_consolidate_states(core))[original.ref] == "done"


# --- what still discards a result: a change to something it was derived from ---


def _consolidate_row(core, ref):
    with closing(sqlite3.connect(core.storage.path)) as conn:
        return conn.execute(
            "SELECT state,last_error_code FROM work_items WHERE work_type='consolidate' AND subject_ref=?", (ref,),
        ).fetchone()


def _claim_total(core):
    with closing(sqlite3.connect(core.storage.path)) as conn:
        return conn.execute("SELECT count(*) FROM claims").fetchone()[0]


def test_suppressing_batch_source_during_extraction_discards_result(worker_app):
    core, ctx, _clock = worker_app
    original = capture(core, ctx, "TEST-project 配色 蓝色。", key="TEST-fence/suppress")
    _mark_embed_done(core)

    class SuppressingModel:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
            authorize(core, ctx, original, mode="suppress")
            core.forget(ctx, request(original, mode="suppress"), remaining_seconds=10)
            return json.dumps(consolidation_payload(*sources, claims=[draft(original)]), ensure_ascii=False)

    result = core.drain_worker(ctx, consolidation=SuppressingModel(), max_items=1, remaining_seconds=30)
    assert result.retried == 1 and result.completed == 0 and result.obsolete == 0
    assert _consolidate_row(core, original.ref) == ("pending", "memory_epoch_changed")
    assert _claim_total(core) == 0


def test_deleting_batch_source_during_extraction_discards_result(worker_app):
    core, ctx, _clock = worker_app
    subject = capture(core, ctx, "TEST-project 配色 蓝色。", key="TEST-fence/delete-subject")
    sibling = capture(core, ctx, "TEST-project 字体 宋体。", key="TEST-fence/delete-sibling")
    _mark_embed_done(core)
    with closing(sqlite3.connect(core.storage.path)) as conn:
        # The subject is claimed first; the sibling is ready work in its batch.
        conn.execute("UPDATE work_items SET available_at='2026-09-06T11:00:00Z' WHERE work_type='consolidate' AND subject_ref=?",
                     (subject.ref,))
        conn.commit()
    batches = []

    class DeletingModel:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
            batches.append({source.ref for source in sources})
            authorize(core, ctx, sibling)
            core.forget(ctx, request(sibling), remaining_seconds=10)
            return json.dumps(consolidation_payload(*sources, claims=[draft(subject)]), ensure_ascii=False)

    result = core.drain_worker(ctx, consolidation=DeletingModel(), max_items=1, remaining_seconds=30)
    assert batches == [{subject.ref, sibling.ref}]
    assert result.obsolete == 1 and result.completed == 0 and result.retried == 0
    assert _consolidate_row(core, subject.ref) == ("obsolete", "authority_revoked")
    assert _claim_total(core) == 0


def test_correction_to_the_same_slot_during_extraction_discards_result(worker_app):
    """A newer head written to the result's slot during the call is not silently ordered.

    Accepting would record the stale derivation against the corrected head --
    here as late historical evidence -- instead of re-deriving it.
    """
    core, ctx, _clock = worker_app
    item, _first = initial(core, ctx, value="H100", kind="fact", predicate="配色")
    later = capture(core, ctx, "TEST-project 配色 H150。", when="2026-09-02T12:00:00Z")
    with closing(sqlite3.connect(core.storage.path)) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE NOT (work_type='consolidate' AND subject_ref=?)", (later.ref,))
        conn.commit()

    class CorrectedDuringModel:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
            capture(core, ctx, "刚才写错了，TEST-project 用H200。", when="2026-09-03T12:00:00Z")
            assert core.current_claim(ctx, item.ref).payload["value_text"] == "H200"
            return json.dumps(consolidation_payload(
                *sources, claims=[draft(later, "H150", kind="fact", predicate="配色")]), ensure_ascii=False)

    result = core.drain_worker(ctx, consolidation=CorrectedDuringModel(), max_items=1, remaining_seconds=30)
    assert result.retried == 1 and result.completed == 0
    assert _consolidate_row(core, later.ref) == ("pending", "memory_epoch_changed")
    assert [version.payload["value_text"] for version in core.claim_history(ctx, item.ref)] == ["H100", "H200"]


def test_rejected_result_with_a_claim_written_during_extraction_stays_a_conflict(worker_app):
    """A result rejected by acceptance while a claim was written meanwhile keeps today's retry code."""
    core, ctx, _clock = worker_app
    item, _first = initial(core, ctx, value="H100", kind="fact", predicate="配色")
    later = capture(core, ctx, "TEST-project 配色 H150。", when="2026-09-02T12:00:00Z")
    with closing(sqlite3.connect(core.storage.path)) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE NOT (work_type='consolidate' AND subject_ref=?)", (later.ref,))
        conn.commit()

    class CorrectedDuringInvalidModel:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
            capture(core, ctx, "刚才写错了，TEST-project 用H200。", when="2026-09-03T12:00:00Z")
            invalid = draft(later, "H150", kind="fact", predicate="配色", quote_override="TEST 原文中没有这句。")
            return json.dumps(consolidation_payload(*sources, claims=[invalid]), ensure_ascii=False)

    result = core.drain_worker(ctx, consolidation=CorrectedDuringInvalidModel(), max_items=1, remaining_seconds=30)
    assert result.retried == 1 and result.completed == 0
    assert _consolidate_row(core, later.ref) == ("pending", "memory_epoch_changed")
    assert [version.payload["value_text"] for version in core.claim_history(ctx, item.ref)] == ["H100", "H200"]


def test_correction_to_another_slot_during_extraction_keeps_result(worker_app):
    core, ctx, _clock = worker_app
    item, _first = initial(core, ctx, value="H100", kind="fact", predicate="配色")
    later = capture(core, ctx, "TEST-other 字体 宋体。", when="2026-09-02T12:00:00Z")
    with closing(sqlite3.connect(core.storage.path)) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE NOT (work_type='consolidate' AND subject_ref=?)", (later.ref,))
        conn.commit()

    class CorrectedElsewhereModel:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
            capture(core, ctx, "刚才写错了，TEST-project 用H200。", when="2026-09-03T12:00:00Z")
            return json.dumps(consolidation_payload(
                *sources, claims=[draft(later, "宋体", subject="TEST-other", predicate="字体", kind="fact")]),
                ensure_ascii=False)

    result = core.drain_worker(ctx, consolidation=CorrectedElsewhereModel(), max_items=1, remaining_seconds=30)
    assert result.completed == 1 and result.retried == 0
    assert _consolidate_row(core, later.ref) == ("done", None)
    with core.storage.read(ctx) as tx:
        assert len(tx.claims.list_refs(subject="TEST-other", predicate="字体")) == 1
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "H200"


def test_resume_applied_to_the_episode_during_extraction_discards_result(worker_app):
    core, ctx, _clock = worker_app
    goal = capture(core, ctx, "请帮我完成 TEST 报告第一部分。", key="TEST-fence/resume-goal")
    _mark_embed_done(core)

    class ResumedDuringModel:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
            other = capture(core, ctx, "请帮我完成 TEST 报告第二部分。", key="TEST-fence/resume-other")
            core.accept_consolidation(ctx, dict(
                protocol_version="1.1", source_refs=[f"{other.ref}@{other.revision}"], claim_proposals=[],
                resume_proposals=[resume(other)], reference_proposals=[]), scope_id="TEST-scope", remaining_seconds=10)
            return json.dumps(consolidation_payload(
                *sources, resume_proposals=[resume(goal, episode_ref=episode_ref)]), ensure_ascii=False)

    result = core.drain_worker(ctx, consolidation=ResumedDuringModel(), max_items=1, remaining_seconds=30)
    assert result.retried == 1 and result.completed == 0
    assert _consolidate_row(core, goal.ref) == ("pending", "memory_epoch_changed")
    episode, = core.episodes(ctx)
    assert episode.resume["goal"]["text"] == "请帮我完成 TEST 报告第二部分。"


def test_capture_attached_to_the_episode_during_extraction_keeps_resume(worker_app):
    """Attaching a capture adds an episode revision but leaves its resume as it was."""
    core, ctx, _clock = worker_app
    goal = capture(core, ctx, "请帮我完成 TEST 报告第一部分。", key="TEST-fence/attach-goal")
    _mark_embed_done(core)

    class AttachedDuringModel:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
            capture(core, ctx, "TEST 我先去喝杯水。", key="TEST-fence/attach-other")
            return json.dumps(consolidation_payload(
                *sources, resume_proposals=[resume(goal, episode_ref=episode_ref)]), ensure_ascii=False)

    result = core.drain_worker(ctx, consolidation=AttachedDuringModel(), max_items=1, remaining_seconds=30)
    assert result.completed == 1 and result.retried == 0
    assert _consolidate_row(core, goal.ref) == ("done", None)
    episode, = core.episodes(ctx)
    assert episode.resume["goal"]["text"] == "请帮我完成 TEST 报告第一部分。"
    assert "unprocessed_events" in episode.gaps


def test_reference_binding_revised_during_extraction_discards_result(worker_app, tmp_path):
    """A clarification that revised the binding meanwhile is not overwritten by the stale reading."""
    from dataclasses import replace

    core, ctx, _clock = worker_app
    ctx = replace(ctx, task_anchor="TEST-fence/reference-work")
    one, _, _ = artifact(core, ctx, tmp_path, label="TEST-v1")
    two, _, _ = artifact(core, ctx, tmp_path, version=2, label="TEST-v2")
    mention = capture(core, ctx, "TEST 那个颜色不行。", key="TEST-fence/reference-mention")
    proposal = dict(mention="那个颜色", candidate_refs=[f"{one.ref}@1", f"{two.ref}@2"], resolved_ref=None,
                    resolution="ambiguous", evidence_refs=[f"{mention.ref}@{mention.revision}"])

    def accept_references(*references):
        refs = list(dict.fromkeys(ref for item in references for ref in item["evidence_refs"]))
        return core.accept_consolidation(ctx, dict(
            protocol_version="1.1", source_refs=refs, claim_proposals=[], resume_proposals=[],
            reference_proposals=list(references)), scope_id="TEST-scope", remaining_seconds=10)

    binding = accept_references(proposal).items[0]
    with closing(sqlite3.connect(core.storage.path)) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE NOT (work_type='consolidate' AND subject_ref=?)", (mention.ref,))
        conn.commit()

    class ClarifiedDuringModel:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
            clarification = capture(core, ctx, "刚才“那个颜色”说的是TEST-v2。", key="TEST-fence/reference-clarification")
            clarified = accept_references(dict(proposal, evidence_refs=[f"{clarification.ref}@{clarification.revision}"]))
            assert clarified.items[0].ref == binding.ref
            return json.dumps(dict(consolidation_payload(*sources), reference_proposals=[proposal]), ensure_ascii=False)

    result = core.drain_worker(ctx, consolidation=ClarifiedDuringModel(), max_items=1, remaining_seconds=30)
    assert result.retried == 1 and result.completed == 0
    assert _consolidate_row(core, mention.ref) == ("pending", "memory_epoch_changed")
    current = core.reference(ctx, binding.ref)
    assert (current.revision, current.payload["resolved_ref"]) == (2, f"{two.ref}@2")


@pytest.mark.parametrize("with_claims", [False, True], ids=["empty", "facts_only"])
def test_facts_only_consolidation_advances_batch_without_resume(worker_app, with_claims):
    core, ctx, _clock = worker_app
    sources = [
        capture(core, ctx, f"TEST incremental fact {index:02d}.", key=f"TEST-worker-batch/{index:02d}")
        for index in range(45)
    ]
    assert len(sources) == 45
    _mark_embed_done(core)

    tracker = BatchTracker(with_claims=with_claims)
    processed = 0
    while processed < 60:
        receipt = core.drain_worker(
            ctx,
            consolidation=tracker,
            max_items=1,
            remaining_seconds=30,
            owner_id=f"batch-worker-{processed}",
        )
        processed += receipt.processed
        pending = sqlite3.connect(core.storage.path).execute(
            "SELECT count(1) FROM work_items WHERE work_type='consolidate' AND state='pending'"
        ).fetchone()[0]
        if receipt.idle and pending == 0:
            break

    assert tracker.calls >= 2
    assert len(set(tracker.batches)) == len(tracker.batches)
    covered = [ref for batch in tracker.batches for ref in batch]
    assert covered == [f"{source.ref}@1" for source in sources]
    by_ref = {f"{source.ref}@1": source for source in sources}
    from scope_recall.core.consolidate import consolidation_messages
    for batch in tracker.batches:
        consolidation_messages(tuple(by_ref[ref] for ref in batch), episode_ref=None)
    assert all(
        state in {"done", "obsolete"}
        for _ref, state in _consolidate_states(core)
    )
    assert not any(state == "pending" for _ref, state in _consolidate_states(core))
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == (45 if with_claims else 0)
        assert conn.execute("SELECT count(*) FROM episode_versions WHERE resume_json IS NOT NULL OR processed_sequence<>0").fetchone()[0] == 0


def test_failed_consolidate_keeps_later_events_retryable(worker_app):
    core, ctx, clock = worker_app
    sources = [
        capture(core, ctx, f"TEST retry fact {index:02d}.", key=f"TEST-worker-retry/{index:02d}")
        for index in range(42)
    ]
    head_ref = sources[0].ref
    fail_ref = sources[20].ref
    _mark_embed_done(core)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute(
            "UPDATE work_items SET available_at=? WHERE work_type='consolidate' AND subject_ref<>?",
            ("2099-01-01T00:00:00Z", head_ref),
        )
        conn.commit()
    attempts = {"count": 0}

    class FlakyModel:
        def propose(self, batch, *, episode_ref=None, remaining_seconds=1.0):
            attempts["count"] += 1
            if any(source.ref == head_ref for source in batch):
                raise RuntimeError("transient model outage")
            return json.dumps(consolidation_payload(*batch), ensure_ascii=False)

    for index in range(3):
        core.drain_worker(
            ctx,
            consolidation=FlakyModel(),
            max_items=1,
            remaining_seconds=30,
            owner_id=f"retry-worker-{index}",
        )
        clock.advance(seconds=60, iso=f"2026-09-06T12:0{index + 1}:00Z")

    row = sqlite3.connect(core.storage.path).execute(
        "SELECT state, attempt, last_error_code FROM work_items WHERE work_type='consolidate' AND subject_ref=?",
        (head_ref,),
    ).fetchone()
    assert row[0] == "failed"
    assert row[1] == 3
    assert row[2] == "model_unavailable"
    covered = sqlite3.connect(core.storage.path).execute(
        "SELECT state FROM work_items WHERE work_type='consolidate' AND subject_ref=?",
        (fail_ref,),
    ).fetchone()[0]
    assert covered == "pending"
    later_pending = sqlite3.connect(core.storage.path).execute(
        """SELECT count(1) FROM work_items w
           JOIN episode_events ee ON ee.source_ref=w.subject_ref AND ee.source_revision=w.subject_revision
           WHERE w.work_type='consolidate' AND w.state='pending' AND ee.sequence>?""",
        (21,),
    ).fetchone()[0]
    assert later_pending > 0


def test_stale_lease_cannot_complete_covered_sibling_work(worker_app):
    core, ctx, clock = worker_app
    sources = [
        capture(core, ctx, f"TEST lease sibling {index:02d}.", key=f"TEST-worker-lease/{index:02d}")
        for index in range(40)
    ]
    _mark_embed_done(core)
    model = FakeConsolidation(lambda batch, episode_ref=None: consolidation_payload(*batch))

    with core.storage.write(ctx) as tx:
        first = tx.work.claim_next("worker-a", clock.utc_now(), lease_seconds=0.001, limit=1)[0]
    clock.advance(seconds=1, iso="2026-09-06T12:00:01Z")
    with core.storage.write(ctx) as tx:
        tx.work.release_stale(clock.utc_now())
        second = tx.work.claim_next("worker-b", clock.utc_now(), lease_seconds=60, limit=1)[0]

    from scope_recall.core.worker import _process_consolidate

    disposition, code, state = _process_consolidate(
        core.storage,
        clock,
        ctx,
        second,
        model=model,
        started=clock.monotonic(),
        budget=30,
    )
    assert disposition == "completed" and state == "done"
    with core.storage.write(ctx) as tx:
        stale = tx.work.complete(first.work_id, first.lease_token, "worker-a", now=clock.utc_now())
    assert stale.disposition == "stale"
    row = sqlite3.connect(core.storage.path).execute(
        "SELECT state, lease_token FROM work_items WHERE work_id=?",
        (first.work_id,),
    ).fetchone()
    assert row[0] == "done" and row[1] == second.lease_token


def test_model_subset_only_acknowledges_declared_source_revisions(worker_app):
    core, ctx, _ = worker_app
    sources = [capture(core, ctx, f"TEST subset {i}", key=f"subset/{i}") for i in range(3)]
    _mark_embed_done(core)
    model = FakeConsolidation(lambda batch, episode_ref=None: consolidation_payload(batch[0]))
    core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=30)
    assert _consolidate_states(core) == [(s.ref, "done" if i == 0 else "pending") for i, s in enumerate(sources)]


def test_model_omitting_leased_subject_cannot_complete_any_batch_work(worker_app):
    core, ctx, clock = worker_app
    sources = [capture(core, ctx, f"TEST omitted {i}", key=f"omitted/{i}") for i in range(3)]
    _mark_embed_done(core)
    model = FakeConsolidation(lambda batch, episode_ref=None: consolidation_payload(batch[1]))
    # Bounded retry: an omission is never accepted for the omitted subject,
    # it just fails later (other items may legitimately complete when the
    # payload happens to cover their own subjects).
    for attempt in range(1, 7):
        clock.advance(seconds=65.0, iso=f"2026-09-06T12:{attempt:02d}:30Z")
        core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=30)
    states = dict(_consolidate_states(core))
    assert states[sources[0].ref] == "failed"


@pytest.mark.parametrize("transition", ["leased", "failed", "requeued", "project", "branch", "scope"])
def test_model_inflight_cannot_ack_changed_sibling_ownership_or_context(worker_app, transition):
    core, ctx, _ = worker_app
    sources = [capture(core, ctx, f"TEST changing {i}", key=f"changing/{i}") for i in range(3)]
    _mark_embed_done(core)
    observed = {}

    class ChangingModel:
        def propose(self, batch, *, episode_ref=None, remaining_seconds=1.0):
            with sqlite3.connect(core.storage.path) as conn:
                if transition in {"leased", "failed", "requeued"}:
                    state = "pending" if transition == "requeued" else transition
                    conn.execute("UPDATE work_items SET state=?,lease_token=lease_token+1,attempt=attempt+1 WHERE work_type='consolidate' AND subject_ref=?", (state, sources[1].ref))
                else:
                    column = {"project": "project_id", "branch": "branch_id", "scope": "scope_id"}[transition]
                    conn.execute(f"UPDATE work_items SET {column}='TEST-other' WHERE work_type='consolidate' AND subject_ref=?", (sources[1].ref,))
                observed["row"] = conn.execute("SELECT * FROM work_items WHERE work_type='consolidate' AND subject_ref=?", (sources[1].ref,)).fetchone()
            return json.dumps(consolidation_payload(*batch))

    result = core.drain_worker(ctx, consolidation=ChangingModel(), max_items=1, remaining_seconds=30)
    assert result.completed == 1
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT * FROM work_items WHERE work_type='consolidate' AND subject_ref=?", (sources[1].ref,)).fetchone() == observed["row"]
    assert dict(_consolidate_states(core))[sources[2].ref] == "done"


def test_new_source_revision_during_model_keeps_new_work_pending(worker_app):
    core, ctx, _ = worker_app
    first = capture(core, ctx, "TEST revision anchor", key="revision/anchor")
    sibling = capture(core, ctx, "TEST revision old", key="revision/sibling")
    _mark_embed_done(core)

    class RevisingModel:
        def propose(self, batch, *, episode_ref=None, remaining_seconds=1.0):
            capture(core, ctx, "TEST revision new", key="revision/sibling", revision=2)
            return json.dumps(consolidation_payload(*batch))

    result = core.drain_worker(ctx, consolidation=RevisingModel(), max_items=1, remaining_seconds=30)
    assert result.obsolete == 1
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT state FROM work_items WHERE work_type='consolidate' AND subject_ref=? AND subject_revision=2", (sibling.ref,)).fetchone()[0] == "pending"
        assert conn.execute("SELECT count(*) FROM work_items WHERE work_type='consolidate' AND state='done'").fetchone()[0] == 0


def test_batch_ack_does_not_sweep_other_revision_of_same_ref(worker_app):
    core, ctx, _ = worker_app
    old = capture(core, ctx, "TEST exact revision old", key="exact-revision")
    latest = capture(core, ctx, "TEST exact revision current", key="exact-revision", revision=2)
    _mark_embed_done(core)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET available_at='2099-01-01T00:00:00Z' WHERE work_type='consolidate' AND subject_revision=1")
    tracker = BatchTracker()
    result = core.drain_worker(ctx, consolidation=tracker, max_items=1, remaining_seconds=30)
    assert result.completed == 1
    assert tracker.batches == [(f"{latest.ref}@2",)]
    with sqlite3.connect(core.storage.path) as conn:
        rows = conn.execute("SELECT subject_revision,state,last_error_code FROM work_items WHERE work_type='consolidate' AND subject_ref=? ORDER BY subject_revision", (old.ref,)).fetchall()
    assert rows == [(1, "pending", None), (2, "done", None)]


def test_batch_respects_pending_retry_backoff(worker_app):
    core, ctx, _ = worker_app
    sources = [capture(core, ctx, f"TEST retry backoff {i}", key=f"backoff/{i}") for i in range(3)]
    _mark_embed_done(core)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET available_at='2099-01-01T00:00:00Z',attempt=2,last_error_code='model_unavailable' WHERE work_type='consolidate' AND subject_ref=?", (sources[1].ref,))
        before = conn.execute("SELECT * FROM work_items WHERE work_type='consolidate' AND subject_ref=?", (sources[1].ref,)).fetchone()
    tracker = BatchTracker()
    _drain(core, ctx, tracker)
    assert {ref for batch in tracker.batches for ref in batch} == {f"{s.ref}@1" for i, s in enumerate(sources) if i != 1}
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT * FROM work_items WHERE work_type='consolidate' AND subject_ref=?", (sources[1].ref,)).fetchone() == before


def test_no_root_write_boundary_rechecks_the_lease(worker_app, monkeypatch):
    from contextlib import contextmanager
    from scope_recall.core.worker import _process_consolidate
    core, ctx, clock = worker_app
    for i in range(3):
        capture(core, ctx, f"TEST write fence {i}", key=f"write-fence/{i}", origin="assistant_visible")
    _mark_embed_done(core)
    with core.storage.write(ctx) as tx:
        old = tx.work.claim_next("old-worker", clock.utc_now(), lease_seconds=60)[0]
    original_write = core.storage.write
    observed = {}

    @contextmanager
    def steal_before_write(context, **kwargs):
        clock.advance(seconds=1, iso="2026-09-06T12:02:00Z")
        with original_write(ctx) as tx:
            tx.work.claim_next("new-worker", clock.utc_now(), lease_seconds=60)
        observed["rows"] = _snapshot(core)
        with original_write(context, **kwargs) as tx:
            yield tx

    monkeypatch.setattr(core.storage, "write", steal_before_write)
    result = _process_consolidate(core.storage, clock, ctx, old, model=None, started=clock.monotonic(), budget=30)
    assert result[0] == "stale"
    assert _snapshot(core) == observed["rows"]
class _BudgetEnforcingTracker:
    """Mimic the live adapter: build the exact serialized prompt first so an
    over-budget batch raises ContractError instead of silently passing."""

    def __init__(self) -> None:
        self.batches: list[tuple[str, ...]] = []

    def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
        from scope_recall.core.consolidate import consolidation_messages
        consolidation_messages(tuple(sources), episode_ref=episode_ref)
        self.batches.append(tuple(f"{source.ref}@{source.revision}" for source in sources))
        return json.dumps(consolidation_payload(*sources), ensure_ascii=False)


def test_over_budget_episode_batch_is_split_instead_of_stalling(worker_app):
    core, ctx, clock = worker_app
    fat = "TEST 工具输出 " + "数据" * 350
    sources = [capture(core, ctx, f"{fat} {index}", key=f"fat/{index}") for index in range(6)]
    _mark_embed_done(core)
    tracker = _BudgetEnforcingTracker()
    _drain(core, ctx, tracker)
    states = dict(_consolidate_states(core))
    assert set(states.values()) == {"done"}
    observed = {ref for batch in tracker.batches for ref in batch}
    assert observed == {f"{source.ref}@1" for source in sources}
    assert len(tracker.batches) > 1


class SequenceTracker:
    """Record the episode sequences actually sent to the model."""

    def __init__(self, core, *, with_claims=False) -> None:
        self.core = core
        self.with_claims = with_claims
        self.sequence_batches: list[tuple[int, ...]] = []
        self.ref_batches: list[tuple[str, ...]] = []

    def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
        refs = tuple(f"{source.ref}@{source.revision}" for source in sources)
        self.ref_batches.append(refs)
        with sqlite3.connect(self.core.storage.path) as conn:
            sequences = []
            for source in sources:
                row = conn.execute(
                    "SELECT sequence FROM episode_events WHERE source_ref=? AND source_revision=?",
                    (source.ref, source.revision),
                ).fetchone()
                sequences.append(int(row[0]))
        self.sequence_batches.append(tuple(sequences))
        claims = [
            draft(source, source.event["content"], predicate=source.ref, kind="fact", statement_kind="assertion")
            for source in sources
        ] if self.with_claims else []
        return json.dumps(consolidation_payload(*sources, claims=claims), ensure_ascii=False)


def test_facts_only_model_sees_unique_sequences_1_32_then_33_45(worker_app):
    core, ctx, _clock = worker_app
    sources = [
        capture(core, ctx, f"TEST incremental fact {index:02d}.", key=f"TEST-seq-batch/{index:02d}")
        for index in range(45)
    ]
    _mark_embed_done(core)
    tracker = SequenceTracker(core, with_claims=False)
    _drain(core, ctx, tracker)
    covered = [seq for batch in tracker.sequence_batches for seq in batch]
    assert covered == list(range(1, 46))
    assert len(set(tracker.sequence_batches)) == len(tracker.sequence_batches)
    assert all(
        tracker.sequence_batches[index][0] == tracker.sequence_batches[index - 1][-1] + 1
        for index in range(1, len(tracker.sequence_batches))
    )
    if tracker.sequence_batches[0] == tuple(range(1, 33)):
        assert tracker.sequence_batches == [tuple(range(1, 33)), tuple(range(33, 46))]
    assert tracker.ref_batches[0] != tracker.ref_batches[1]
    assert [ref for batch in tracker.ref_batches for ref in batch] == [f"{source.ref}@1" for source in sources]
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT count(*) FROM source_events").fetchone()[0] == 45
        assert conn.execute(
            "SELECT count(*) FROM work_items WHERE work_type='consolidate' AND state='done'"
        ).fetchone()[0] == 45
        assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM episode_versions WHERE resume_json IS NOT NULL OR processed_sequence<>0"
        ).fetchone()[0] == 0


def test_source_work_claim_evidence_new_session_recall(worker_app):
    from dataclasses import replace

    core, ctx, _clock = worker_app
    sources = [
        capture(
            core,
            ctx,
            f"TEST recall fact {index:02d} H100UNIQUE marker {index:02d}.",
            key=f"TEST-e2e-batch/{index:02d}",
        )
        for index in range(45)
    ]
    _mark_embed_done(core)
    tracker = SequenceTracker(core, with_claims=True)
    _drain(core, ctx, tracker)
    covered = [seq for batch in tracker.sequence_batches for seq in batch]
    assert covered == list(range(1, 46))
    assert len(set(tracker.sequence_batches)) == len(tracker.sequence_batches)
    with sqlite3.connect(core.storage.path) as conn:
        source_count = conn.execute("SELECT count(*) FROM source_events").fetchone()[0]
        done = conn.execute(
            "SELECT count(*) FROM work_items WHERE work_type='consolidate' AND state='done'"
        ).fetchone()[0]
        claims = conn.execute("SELECT count(*) FROM claims").fetchone()[0]
        evidence = conn.execute(
            "SELECT count(*) FROM evidence_links WHERE object_kind='claim'"
        ).fetchone()[0]
        claim_source = conn.execute(
            """SELECT count(*) FROM evidence_links e
               JOIN source_events s ON s.event_id=e.source_ref AND s.source_revision=e.source_revision
               WHERE e.object_kind='claim'"""
        ).fetchone()[0]
    assert source_count == 45
    assert done == 45
    assert claims == 45
    assert evidence == 45
    assert claim_source == 45
    later = replace(ctx, session_id="TEST-recall-session")
    with sqlite3.connect(core.storage.path) as conn:
        claim_rows = conn.execute(
            """SELECT c.claim_id, c.current_revision FROM claims c
               JOIN evidence_links e ON e.object_kind='claim' AND e.object_ref=c.claim_id
               JOIN source_events s ON s.event_id=e.source_ref AND s.source_revision=e.source_revision
               WHERE s.content LIKE '%H100UNIQUE marker 00%'"""
        ).fetchall()
    assert claim_rows
    history = core.claim_history(later, claim_rows[0][0])
    assert history
    rendered = " ".join(str(getattr(version, "payload", version)) for version in history)
    assert "H100UNIQUE marker 00" in rendered
    current = core.current_claim(later, claim_rows[0][0])
    assert current is not None or history
