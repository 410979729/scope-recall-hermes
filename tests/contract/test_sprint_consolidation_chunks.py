"""Focused offline checks for long-source progress, transaction and authority fences."""
from dataclasses import replace
import json
import sqlite3

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.consolidate import consolidation_messages
from scope_recall.core.schema import SCHEMA_VERSION
from scope_recall.core.storage import SQLiteStorage
from scope_recall.core.work_storage import WorkItems
from test_v11_worker import (worker_app, app, capture, draft, consolidation_payload,
                             FakeConsolidation, _mark_embed_done)
from test_v11_deletion import authorize, request


FIRST = "TEST-project 配色 蓝色。"
LAST = "TEST-project 主题 深色。"


def long_source(core, ctx, content=None):
    source = capture(core, ctx, content or (FIRST + "\n" + "这是一段归档资料；" * 5000 + "\n" + LAST))
    _mark_embed_done(core)
    return source


def row(core, source):
    with sqlite3.connect(core.storage.path) as db:
        return db.execute("SELECT state,consolidation_offset,attempt FROM work_items WHERE work_type='consolidate' AND subject_ref=?",
                          (source.ref,)).fetchone()


def claims(core):
    with sqlite3.connect(core.storage.path) as db:
        return [(json.loads(payload), state) for payload, state in db.execute("SELECT payload_json,state FROM claim_versions")]


def proposal(page, quote, *, value="蓝色", predicate="配色"):
    return draft(page, value, predicate=predicate,
                 evidence_spans=[dict(source_ref=page.ref,source_revision=page.revision,quote=quote)])


def test_existing_1105_source_upgrades_explicitly_and_resumes_to_exact_end(worker_app):
    core, ctx, clock = worker_app
    padding = ("这是一段归档资料；" * 8000)[:65536-len(FIRST)-len(LAST)-2]
    source = long_source(core, ctx, FIRST + "\n" + padding + "\n" + LAST)
    assert len(source.event["content"]) == 65536
    with sqlite3.connect(core.storage.path) as db:
        for table in (
            "candidate_source_triggers", "candidate_evaluations", "candidate_trigger_terms",
            "candidate_evidence", "candidate_lifecycle", "candidate_scan_cursors",
            "capture_inbox", "work_error_details", "consolidation_fragments", "consolidation_outcomes",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("ALTER TABLE work_items DROP COLUMN consolidation_offset")
        db.execute("UPDATE instance_meta SET schema_version=1105")
        db.execute("PRAGMA user_version=1105")
    with pytest.raises(ContractError, match="SCHEMA_UNSUPPORTED"):
        core.status(ctx)
    foreign = SQLiteStorage(replace(ctx.binding, installation_id="TEST-unrelated-installation"))
    with pytest.raises(ContractError, match="IDENTITY_UNBOUND"):
        foreign.initialize()
    with sqlite3.connect(core.storage.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1105
        assert "consolidation_offset" not in {r[1] for r in db.execute("PRAGMA table_info(work_items)")}
    assert core.initialize().schema_version == SCHEMA_VERSION == 1108
    windows, contents = [], []

    def build(sources, **kwargs):
        assert len(sources) == 1
        page = sources[0]
        window = page.consolidation_window
        windows.append(window)
        contents.append(page.event["content"])
        messages = consolidation_messages(sources, episode_ref=kwargs["episode_ref"])
        assert sum(len(m["content"].encode("utf-8")) for m in messages) <= 16000
        assert (page.ref,page.revision) == (source.ref,source.revision)
        found = []
        if FIRST in page.event["content"]:
            found.append(proposal(page, FIRST))
        if LAST in page.event["content"]:
            found.append(proposal(page, LAST, value="深色", predicate="主题"))
        return consolidation_payload(page, claims=found)

    model = FakeConsolidation(build)
    first = core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=5)
    assert first.deferred == 1 and first.completed == 0
    assert row(core,source)[0] == "pending" and 0 < row(core,source)[1] < len(source.event["content"])
    # A new Core reads the durable cursor; no in-memory model/session state is required.
    core = MemoryCore(CoreConfig(ctx.binding),clock=clock)
    for _ in range(80):
        if row(core,source)[0] == "done":
            break
        result = core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=5)
        assert result.failed == result.retried == 0
    assert row(core,source) == ("done",len(source.event["content"]),1)
    assert "".join(contents) == source.event["content"]
    assert all(a.end == b.start for a,b in zip(windows, windows[1:]))
    assert {(p["value_text"],state) for p,state in claims(core)} == {("蓝色","active"),("深色","active")}
    assert core.source(ctx,source.ref,source.revision).event["content"] == source.event["content"]
    with sqlite3.connect(core.storage.path) as db:
        assert db.execute("SELECT MAX(processed_sequence) FROM episode_versions").fetchone()[0] in (None,0)


def test_chunk_claim_and_cursor_roll_back_together(worker_app, monkeypatch):
    core, ctx, _clock = worker_app
    source = long_source(core,ctx)
    real_advance = WorkItems.advance_consolidation

    def interrupted_commit(self, *args, **kwargs):
        real_advance(self,*args,**kwargs)
        raise ContractError("STORAGE_UNAVAILABLE", "synthetic_commit_failure")

    monkeypatch.setattr(WorkItems,"advance_consolidation",interrupted_commit)
    model = FakeConsolidation(lambda sources,**_: consolidation_payload(sources[0],claims=[proposal(sources[0],FIRST)]))
    result = core.drain_worker(ctx,consolidation=model,max_items=1,remaining_seconds=5)
    assert result.retried == 1 and result.completed == 0
    assert row(core,source) == ("pending",0,1)
    assert claims(core) == []


def test_fragment_evidence_cannot_escape_window_or_omit_original_negation(worker_app):
    core, ctx, clock = worker_app
    # The negative qualifier lies outside the final fragment, in the same
    # original sentence. The complete-source qualification must still see it.
    source = long_source(core,ctx,"不允许" + "长" * 9000 + FIRST)
    bad = FakeConsolidation(lambda sources,**_: consolidation_payload(sources[0],claims=[proposal(sources[0],FIRST)]))
    result = core.drain_worker(ctx,consolidation=bad,max_items=1,remaining_seconds=5)
    assert result.retried == 1 and row(core,source)[1] == 0 and claims(core) == []
    clock.advance(iso="2026-09-06T12:00:03Z")
    model = FakeConsolidation(lambda sources,**_: consolidation_payload(sources[0],
        claims=[proposal(sources[0],FIRST)] if FIRST in sources[0].event["content"] else []))
    for _ in range(30):
        if row(core,source)[0] == "done":
            break
        result = core.drain_worker(ctx,consolidation=model,max_items=1,remaining_seconds=5)
        assert result.failed == result.retried == 0
    assert row(core,source)[0] == "done"
    assert claims(core) and all(state == "proposed" for _p,state in claims(core))


def test_deletion_during_fragment_model_call_cannot_commit_progress(worker_app):
    core, ctx, _clock = worker_app
    source = long_source(core,ctx)

    def remove(sources,**_):
        authorize(core,ctx,source)
        core.forget(ctx,request(source),remaining_seconds=5)
        return consolidation_payload(sources[0],claims=[proposal(sources[0],FIRST)])

    result = core.drain_worker(ctx,consolidation=FakeConsolidation(remove),max_items=1,remaining_seconds=5)
    assert result.completed == 0 and row(core,source)[0] == "obsolete" and row(core,source)[1] == 0
    assert claims(core) == [] and core.source(ctx,source.ref,source.revision) is None


def test_oversized_repair_is_exact_once_and_short_resume_cannot_skip_long_source(worker_app):
    from scope_recall.core.episodes import source_watermark

    core, ctx, clock = worker_app
    source = long_source(core,ctx)
    short = capture(core,ctx,"请帮我完成 TEST 报告整理。")
    ordinary_invalid = capture(core,replace(ctx,session_id="TEST-other-session"),"TEST 普通无效结果。")
    _mark_embed_done(core)
    with sqlite3.connect(core.storage.path) as db:
        db.execute("UPDATE work_items SET state='failed',attempt=3,last_error_code='INPUT_INVALID' WHERE work_type='consolidate' AND subject_ref IN (?,?)",
                   (source.ref,ordinary_invalid.ref))
    with core.storage.write(ctx) as tx:
        assert tx.work.recover_oversized_consolidations(now=clock.utc_now(),formatter=consolidation_messages) == 1
    assert row(core,source) == ("pending",0,3)
    assert row(core,ordinary_invalid) == ("failed",0,3)
    # Make the short source the next lease while the repaired long source is
    # pending. It is excluded by the prompt budget, not falsely covered.
    with sqlite3.connect(core.storage.path) as db:
        db.execute("UPDATE work_items SET available_at='2026-09-06T11:59:59Z' WHERE work_type='consolidate' AND subject_ref=?",(short.ref,))

    def short_resume(sources,episode_ref=None):
        assert [s.ref for s in sources] == [short.ref]
        refs = [f"{short.ref}@{short.revision}"]
        resume = dict(episode_ref=episode_ref,goal=dict(text=short.event["content"],evidence_refs=refs),
                      decisions=[],verified_progress=[],open_items=[],blockers=[],next_step=None,
                      next_step_basis="unknown",artifact_refs=[],source_watermark=source_watermark(refs),evidence_refs=refs)
        return consolidation_payload(short,resume_proposals=[resume])

    assert core.drain_worker(ctx,consolidation=FakeConsolidation(short_resume),max_items=1,remaining_seconds=5).completed == 1
    assert row(core,source) == ("pending",0,3)
    with sqlite3.connect(core.storage.path) as db:
        processed = db.execute("SELECT MAX(processed_sequence) FROM episode_versions").fetchone()[0]
    assert processed == 0
    clock.advance(iso="2026-09-06T12:00:10Z")
    empty = FakeConsolidation(lambda sources,**_: consolidation_payload(*sources))
    assert core.drain_worker(ctx,consolidation=empty,max_items=1,remaining_seconds=5).deferred == 1
    assert row(core,source)[1] > 0
    # Force a deterministic input failure on the next page: the old repair
    # must never become an unbounded retry, even with an exhausted attempt count.
    class Invalid:
        def propose(self,*args,**kwargs):
            raise ContractError("INPUT_INVALID","synthetic_invalid_model_input")
    assert core.drain_worker(ctx,consolidation=Invalid(),max_items=1,remaining_seconds=5).failed == 1
    with sqlite3.connect(core.storage.path) as db:
        # Also verify the durable marker at offset zero, the repair predicate.
        db.execute("UPDATE work_items SET consolidation_offset=0 WHERE subject_ref=?",(source.ref,))
    with core.storage.write(ctx) as tx:
        assert tx.work.recover_oversized_consolidations(now=clock.utc_now(),formatter=consolidation_messages) == 0
    assert row(core,source)[0] == "failed"
