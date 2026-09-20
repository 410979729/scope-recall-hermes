"""Source-preserving cost admission with real isolated SQLite transactions."""
from dataclasses import replace
import itertools
import json
import sqlite3

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.admission import ADMISSION_KEY, AdmissionDecision, AdmissionPolicy, classify, store_decision
from v11_support import context, source_event


def app_at(tmp_path, policy=None):
    ctx = context(tmp_path / "TEST-admission")
    app = MemoryCore(CoreConfig(ctx.binding, admission_policy=policy or AdmissionPolicy()))
    app.initialize()
    return app, ctx


def counts(app):
    conn = sqlite3.connect(f"{app.storage.path.as_uri()}?mode=ro", uri=True)
    try:
        return {name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                for name in ("source_events", "lexical_postings", "work_items")}
    finally:
        conn.close()


def capture(app, ctx, key, text, **changes):
    return app.record_event(ctx, source_event(source_event_key=key, content=text, **changes), scope_id="TEST-scope", remaining_seconds=10)


@pytest.mark.parametrize("text", ["好", "好的！", "谢谢", "OK.", "got it", "hello"])
def test_ack_is_source_only_and_exact_occurrence_replay_remains_write_free(tmp_path, text):
    app, ctx = app_at(tmp_path)
    first = capture(app, ctx, "TEST-ack/1", text)
    second = capture(app, ctx, "TEST-ack/2", text)
    assert first.durability == "persisted" and first.semantic_state == "not_scheduled"
    assert first.gaps == () and first.admission == ("admission_source_only:acknowledgement",)
    assert counts(app)["source_events"] == 2 and counts(app)["work_items"] == 0
    assert first.event_refs[0].ref != second.event_refs[0].ref
    assert app.source(ctx, first.event_refs[0].ref, 1).event["content"] == text
    assert app.search_sources(ctx, text)
    before = app.storage.path.read_bytes()
    replay = capture(app, ctx, "TEST-ack/1", text, recorded_at="2026-09-07T12:00:00Z")
    assert replay.disposition == "duplicate" and replay.gaps == first.gaps and replay.admission == first.admission
    assert app.storage.path.read_bytes() == before


@pytest.mark.parametrize("text", ["好，以后都用中文。", "不要使用这个版本", "更正：实际日期为明天", "我喜欢蓝色", "决定采用方案 B", "TEST 未知但可能有用的内容", "好？"])
def test_substantive_unknown_or_important_content_is_never_trivial_filtered(tmp_path, text):
    app, ctx = app_at(tmp_path)
    receipt = capture(app, ctx, "TEST-important", text)
    assert receipt.semantic_state == "pending" and not receipt.gaps
    assert counts(app)["work_items"] == 2


@pytest.mark.parametrize("text,expected", [
    ('{"exit_code":0,"stdout":"","stderr":""}', "source_only"),
    ('{"status":"success","result":"new source evidence"}', "schedule"),
    ('{"exit_code":0,"stdout":"TEST build artifact at output.txt"}', "schedule"),
    ('{"exit_code":1,"stderr":"missing input"}', "schedule"),
    ('{"status":["unknown"]}', "schedule"),
    ('{"ok":true,"message":"remember new behavior"}', "schedule"),
])
def test_only_content_free_successful_tool_wrappers_are_cheap(text, expected):
    assert classify(source_event(content=text, role="tool", origin="tool_trusted")).disposition == expected


def test_backpressure_reserves_capacity_then_automatically_refills_after_drain(tmp_path):
    policy = AdmissionPolicy(max_pending_work=2, important_reserve=2)
    app, ctx = app_at(tmp_path, policy)
    a = capture(app, ctx, "TEST-first", "TEST plain substantive source")
    b = capture(app, ctx, "TEST-deferred", "TEST second substantive source")
    important = capture(app, ctx, "TEST-priority", "记住：TEST 选择蓝色")
    assert a.semantic_state == important.semantic_state == "pending"
    assert b.semantic_state == "not_scheduled" and b.admission == ("admission_deferred:queue_capacity",)
    assert b.gaps == ()
    assert counts(app)["work_items"] == 4
    assert app.search_sources(ctx, "second")[0].ref == b.event_refs[0].ref
    assert app.resume_deferred(ctx, remaining_seconds=10) == ()
    # Complete queued work through the real lease state machine (no models).
    with app.storage.write(ctx, remaining_seconds=10) as tx:
        while batch := tx.work.claim_next(owner="TEST-worker", now=app.clock.utc_now(), lease_seconds=10):
            for work in batch:
                tx.work.complete(work.work_id, work.lease_token, work.lease_owner, now=app.clock.utc_now())
    resumed = app.resume_deferred(ctx, remaining_seconds=10)
    assert len(resumed) == 1 and resumed[0].queued_work == 2
    assert app.source(ctx, b.event_refs[0].ref, 1).capture_gaps == ()
    assert app.resume_deferred(ctx, remaining_seconds=10) == ()


def test_memory_reinjection_is_source_only_at_capture_refill_and_on_demand(tmp_path):
    app, ctx = app_at(tmp_path)
    # Recall output routinely repeats the words that raise ordinary priority.
    text = "记住：TEST-ECHO-ANCHOR 决定采用蓝色方案。"
    echo = capture(app, replace(ctx, actor_origin="memory_reinjection"), "TEST-echo", text,
                   origin="memory_reinjection", role="tool")
    observed = capture(app, replace(ctx, actor_origin="tool_observation"), "TEST-observed", text,
                       origin="tool_observation", role="tool")
    ref = echo.event_refs[0].ref
    assert echo.durability == "persisted" and echo.semantic_state == "not_scheduled"
    assert echo.admission == ("admission_source_only:memory_reinjection",)
    assert observed.semantic_state == "pending" and observed.admission == ()
    conn = sqlite3.connect(f"{app.storage.path.as_uri()}?mode=ro", uri=True)
    try:
        work = conn.execute("SELECT subject_ref,work_type FROM work_items ORDER BY work_id").fetchall()
    finally:
        conn.close()
    assert work == [(observed.event_refs[0].ref, "consolidate"), (observed.event_refs[0].ref, "embed")]
    assert ref in {source.ref for source in app.search_sources(ctx, "TEST-ECHO-ANCHOR")}
    # An explicit request creates no work either, and writes nothing.
    before = app.storage.path.read_bytes()
    receipt = app.schedule_source(ctx, ref, 1, remaining_seconds=10)
    assert (receipt.disposition, receipt.reason, receipt.queued_work) == ("source_only", "memory_reinjection", 0)
    assert app.storage.path.read_bytes() == before
    # A row deferred for capacity before this rule settles on its first refill.
    with app.storage.write(ctx, remaining_seconds=10) as tx:
        store_decision(tx, ref, 1, AdmissionDecision("deferred", "queue_capacity", True))
    resumed = app.resume_deferred(ctx, remaining_seconds=10)
    assert [(item.ref, item.disposition, item.queued_work) for item in resumed] == [(ref, "source_only", 0)]
    assert app.resume_deferred(ctx, remaining_seconds=10) == ()
    assert counts(app)["work_items"] == 2
    status = app.status(ctx)
    assert status.source_only_sources == 1 and status.deferred_sources == 0


def test_on_demand_activation_is_idempotent_and_does_not_bypass_visibility(tmp_path):
    app, ctx = app_at(tmp_path)
    saved = capture(app, ctx, "TEST-reusable", "好的")
    ref = saved.event_refs[0].ref
    first = app.schedule_source(ctx, ref, 1, remaining_seconds=10)
    assert first.queued_work == 2 and first.disposition == "scheduled"
    before = app.storage.path.read_bytes()
    assert app.schedule_source(ctx, ref, 1, remaining_seconds=10).disposition == "unchanged"
    assert app.storage.path.read_bytes() == before
    with pytest.raises(ContractError, match="ACCESS_DENIED"):
        app.schedule_source(replace(ctx, actor_origin="memory_reinjection"), ref, 1)
    with pytest.raises(ContractError, match="ACCESS_DENIED"):
        app.schedule_source(replace(ctx, allowed_scope_ids=frozenset()), ref, 1)


@pytest.mark.parametrize("text", ["换成蓝色", "调整为蓝色", "不再使用蓝色", "不再采用蓝色", "停止使用蓝色", "停止采用蓝色", "弃用蓝色", "switch to blue", "discontinue blue"])
def test_correction_language_can_use_reserved_capacity(tmp_path, text):
    app, ctx = app_at(tmp_path, AdmissionPolicy(max_pending_work=2, important_reserve=2))
    capture(app, ctx, "TEST-fill", "TEST ordinary source")
    receipt = capture(app, ctx, "TEST-correction", text)
    assert receipt.semantic_state == "pending" and counts(app)["work_items"] == 4


def test_backpressure_and_scheduling_stay_in_original_project_and_branch(tmp_path):
    app, ctx = app_at(tmp_path, AdmissionPolicy(max_pending_work=2, important_reserve=0))
    first = capture(app, ctx, "TEST-global", "TEST global content")
    other = replace(ctx, project_id="TEST-project", branch_id="TEST-branch")
    saved = capture(app, other, "TEST-project-source", "TEST distinct project content")
    deferred = capture(app, other, "TEST-project-deferred", "TEST more project content")
    assert first.semantic_state == saved.semantic_state == "pending"
    assert deferred.gaps == () and deferred.admission == ("admission_deferred:queue_capacity",)
    assert app.resume_deferred(ctx) == ()
    with pytest.raises(ContractError, match="SOURCE_MISSING"):
        app.schedule_source(other, first.event_refs[0].ref, 1)
    with pytest.raises(ContractError, match="SOURCE_MISSING"):
        app.schedule_source(ctx, deferred.event_refs[0].ref, 1)


def test_schedule_does_not_resurrect_deleted_suppressed_or_old_revisions(tmp_path):
    app, ctx = app_at(tmp_path)
    saved = capture(app, ctx, "TEST-old", "好")
    capture(app, ctx, "TEST-old", "收到", source_revision=2)
    with pytest.raises(ContractError, match="SOURCE_MISSING"):
        app.schedule_source(ctx, saved.event_refs[0].ref, 1)
    with app.storage.write(ctx) as tx:
        tx._check(write=True).execute("UPDATE source_events SET read_blocked=1 WHERE event_id=?", (saved.event_refs[0].ref,))
    before = app.storage.path.read_bytes()
    with pytest.raises(ContractError, match="SOURCE_MISSING"):
        app.schedule_source(ctx, saved.event_refs[0].ref, 2)
    assert app.storage.path.read_bytes() == before


def test_policy_disabled_preserves_legacy_scheduling_without_source_changes(tmp_path):
    app, ctx = app_at(tmp_path, AdmissionPolicy(enabled=False))
    saved = capture(app, ctx, "TEST-compatible", "好")
    assert saved.semantic_state == "pending" and saved.gaps == ()
    assert counts(app)["work_items"] == 2


def test_full_backlog_does_not_demote_correction_evidence_or_block_immediate_revision(tmp_path):
    from tests.contract.test_v11_claims import initial

    app, ctx = app_at(tmp_path, AdmissionPolicy(max_pending_work=2, important_reserve=0))
    app.test_sequence = itertools.count(1)
    item, original = initial(app, ctx, value="H100", kind="fact")
    correction = capture(app, ctx, "TEST-full-correction", "TEST-project 配色换成 H200。",
                         occurred_at="2026-09-06T12:00:00Z")
    assert correction.semantic_state == "not_scheduled"
    assert correction.admission == ("admission_deferred:queue_capacity",)
    assert correction.gaps == () and correction.mutation == "revised"
    stored = app.source(ctx, correction.event_refs[0].ref, 1)
    assert stored.capture_gaps == () and stored.event["capture_state"] == "complete"
    assert ADMISSION_KEY not in stored.event
    assert app.current_claim(ctx, item.ref).payload["value_text"] == "H200"
    assert app.claim_history(ctx, item.ref)[0].payload["value_text"] == "H100"
    assert app.source(ctx, original.ref, original.revision) is not None


def test_input_cannot_spoof_internal_admission_metadata(tmp_path):
    app, ctx = app_at(tmp_path)
    before = app.storage.path.read_bytes()
    with pytest.raises(ContractError, match="INPUT_INVALID"):
        capture(app, ctx, "TEST-spoof", "TEST substantive evidence",
                **{ADMISSION_KEY: {"disposition": "source_only", "reason": "acknowledgement"}})
    assert app.storage.path.read_bytes() == before


def test_core_worker_uses_capture_policy_for_deferred_refill(tmp_path):
    app, ctx = app_at(tmp_path, AdmissionPolicy(max_pending_work=2, important_reserve=0))
    capture(app, ctx, "TEST-first", "TEST substantive evidence")
    saved = capture(app, ctx, "TEST-deferred", "TEST more evidence")
    assert saved.admission == ("admission_deferred:queue_capacity",)
    app.drain_worker(ctx, owner_id="TEST-worker", remaining_seconds=10)
    assert counts(app)["work_items"] == 2
    assert app.resume_deferred(ctx, remaining_seconds=10) == ()


def clocked_app(tmp_path, policy):
    from test_v11_worker import Clock

    app, ctx = app_at(tmp_path, policy)
    app.clock = Clock()
    app.clock.advance(iso="2026-09-05T12:00:00Z")
    return app, ctx, app.clock, replace(ctx, session_id="TEST-yesterday")


def work_for(app, ref):
    conn = sqlite3.connect(f"{app.storage.path.as_uri()}?mode=ro", uri=True)
    try:
        return dict(conn.execute("SELECT work_type,state FROM work_items WHERE subject_ref=?", (ref,)).fetchall())
    finally:
        conn.close()


def complete_work(app, ctx, clock, *, count=-1, **claim):
    """Complete ready work through the real lease state machine, no model; all of it by default."""
    with app.storage.write(ctx, remaining_seconds=10) as tx:
        while count and (batch := tx.work.claim_next("TEST-worker", clock.utc_now(), lease_seconds=10, **claim)):
            for work in batch:
                tx.work.complete(work.work_id, work.lease_token, work.lease_owner, now=clock.utc_now())
            count -= 1


def test_refill_gives_a_freed_slot_to_fresh_conversation_before_older_deferred_sources(tmp_path):
    app, ctx, clock, yesterday = clocked_app(tmp_path, AdmissionPolicy(max_pending_work=2, important_reserve=0))
    capture(app, yesterday, "TEST-old-admitted", "TEST yesterday admitted source")
    older = []
    for index in range(2):
        clock.advance(iso=f"2026-09-05T12:00:0{index + 1}Z")
        older.append(capture(app, yesterday, f"TEST-old-deferred/{index}", f"TEST yesterday deferred source {index}"))
    clock.advance(iso="2026-09-06T12:00:00Z")
    fresh = capture(app, ctx, "TEST-fresh", "TEST the message typed just now")
    assert {receipt.admission for receipt in (*older, fresh)} == {("admission_deferred:queue_capacity",)}
    complete_work(app, ctx, clock)
    resumed = app.resume_deferred(ctx, remaining_seconds=10)
    assert [(item.ref, item.disposition, item.queued_work) for item in resumed] == [
        (fresh.event_refs[0].ref, "scheduled", 2),
        (older[0].event_refs[0].ref, "deferred", 0),
        (older[1].event_refs[0].ref, "deferred", 0),
    ]


def test_fresh_message_at_queue_capacity_is_consolidated_within_one_pass(tmp_path):
    from test_v11_worker import FakeConsolidation, consolidation_payload

    # Two per type ordinarily, three with the reserve.
    app, ctx, clock, yesterday = clocked_app(tmp_path, AdmissionPolicy(max_pending_work=4, important_reserve=2))
    backlog = [capture(app, yesterday, f"TEST-backlog/{index}", f"TEST yesterday backlog source {index}")
               for index in range(2)]
    waiting = capture(app, yesterday, "TEST-backlog/waiting", "TEST yesterday waiting source")
    clock.advance(iso="2026-09-06T12:00:00Z")
    fresh = capture(app, ctx, "TEST-fresh", "TEST the message typed just now")
    assert waiting.admission == fresh.admission == ("admission_deferred:queue_capacity",)
    batches = []

    def record(sources, **_):
        batches.append([source.ref for source in sources])
        return consolidation_payload(*sources)

    receipt = app.drain_worker(ctx, owner_id="TEST-worker", consolidation=FakeConsolidation(record),
                               max_items=2, remaining_seconds=10)
    # The pass refills the fresh message into the reserve before it claims, so
    # the same pass consolidates it.  The older deferred source still waits.
    assert receipt.completed == 2
    assert batches == [[source.event_refs[0].ref for source in backlog], [fresh.event_refs[0].ref]]
    assert work_for(app, fresh.event_refs[0].ref) == {"consolidate": "done", "embed": "pending"}
    assert work_for(app, waiting.event_refs[0].ref) == {}


def test_freshness_lends_the_reserve_without_becoming_importance(tmp_path):
    app, ctx, clock, yesterday = clocked_app(tmp_path, AdmissionPolicy(max_pending_work=4, important_reserve=2))
    for index in range(2):
        capture(app, yesterday, f"TEST-backlog/{index}", f"TEST yesterday backlog source {index}")
    requested = capture(app, yesterday, "TEST-backlog/requested", "TEST yesterday requested source")
    assert app.schedule_source(ctx, requested.event_refs[0].ref, 1, remaining_seconds=10).queued_work == 2
    complete_work(app, ctx, clock, count=1, allowed_work_types=frozenset({"consolidate"}))
    clock.advance(iso="2026-09-06T12:00:00Z")
    fresh = capture(app, ctx, "TEST-fresh", "TEST the message typed just now")
    ref = fresh.event_refs[0].ref
    # Consolidation has a reserve slot left, embedding does not.
    [partial] = app.resume_deferred(ctx, remaining_seconds=10)
    assert (partial.ref, partial.disposition, partial.queued_work) == (ref, "partial", 1)
    conn = sqlite3.connect(f"{app.storage.path.as_uri()}?mode=ro", uri=True)
    try:
        marker = conn.execute("SELECT json_extract(extra_json,'$._scope_recall_admission') FROM source_events WHERE event_id=?",
                              (ref,)).fetchone()[0]
    finally:
        conn.close()
    assert json.loads(marker) == {"disposition": "deferred", "reason": "queue_capacity", "important": False}
    # Once it is no longer fresh it waits for the ordinary ceiling like any other source.
    complete_work(app, ctx, clock, count=1, allowed_work_types=frozenset({"embed"}))
    clock.advance(iso="2026-09-06T14:00:01Z")
    assert app.resume_deferred(ctx, remaining_seconds=10) == ()


def test_unavailable_embedding_never_starves_consolidation_or_its_deferred_refill(tmp_path):
    from test_v11_worker import FakeConsolidation, consolidation_payload

    app, ctx = app_at(tmp_path, AdmissionPolicy(max_pending_work=2, important_reserve=0))
    model = FakeConsolidation(lambda sources, **kw: consolidation_payload(*sources))
    # More than the refill LIMIT of 16 embedding-only deferred sources must
    # neither prevent new consolidation nor hide a later fully deferred source.
    for index in range(20):
        saved = capture(app, ctx, f"TEST-isolated/{index}", f"TEST substantive source number {index}")
        assert saved.durability == "persisted" and saved.gaps == ()
        receipt = app.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=10)
        assert receipt.completed == 1 and receipt.items[0].work_type == "consolidate"
    assert model.calls == 20
    capture(app, ctx, "TEST-fill-cons", "TEST occupy healthy consolidation slot")
    later = capture(app, ctx, "TEST-later-cons", "TEST pending healthy consolidation")
    assert later.admission == ("admission_deferred:queue_capacity",)
    app.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=10)
    resumed = app.resume_deferred(ctx, limit=1, remaining_seconds=10)
    assert len(resumed) == 1 and resumed[0].ref == later.event_refs[0].ref
    assert resumed[0].queued_work == 1 and resumed[0].disposition == "partial"
    # Simulate capability restoration by completing the existing embed via
    # its real lease. Only one old embedding is admitted into the freed slot.
    with app.storage.write(ctx, remaining_seconds=10) as tx:
        pending = tx._check().execute("SELECT work_type,count(*) FROM work_items WHERE state='pending' GROUP BY work_type").fetchall()
        assert dict(pending) == {"consolidate": 1, "embed": 1}
        work = tx.work.claim_next("TEST-embed", app.clock.utc_now(), lease_seconds=10,
                                  allowed_work_types=frozenset({"embed"}))[0]
        tx.work.complete(work.work_id, work.lease_token, work.lease_owner, now=app.clock.utc_now())
    catchup = app.resume_deferred(ctx, limit=16, remaining_seconds=10)
    assert sum(item.queued_work for item in catchup) == 1
    assert app.resume_deferred(ctx, limit=16, remaining_seconds=10) == ()


def test_a_repeated_tool_output_is_kept_as_a_source_only(tmp_path):
    """77% of one instance's 132,000 tool outputs were byte-identical to an
    earlier one, and each was embedded again.  The earlier copy carries the
    vector and the lexical index lists both; a person saying it again is new."""
    app, ctx = app_at(tmp_path)
    tool = replace(ctx, actor_origin="tool_observation")
    first = capture(app, tool, "TEST-tool/1", "TEST build log line 42", origin="tool_observation", role="tool")
    second = capture(app, tool, "TEST-tool/2", "TEST build log line 42", origin="tool_observation", role="tool")
    assert first.semantic_state == "pending" and first.admission == ()
    assert second.semantic_state == "not_scheduled"
    assert second.admission == ("admission_source_only:tool_output_repeat",)
    assert counts(app)["source_events"] == 2 and counts(app)["work_items"] == 2
    assert len(app.search_sources(ctx, "build log line")) == 2
    human = capture(app, ctx, "TEST-user/1", "TEST build log line 42")
    assert human.semantic_state == "pending" and human.admission == ()


@pytest.mark.parametrize("text", [
    "Tool execution summary (terminal): output omitted",
    "Tool execution summary (patch): output omitted [REDACTED_PATH]",
    "Tool execution summary (terminal): tool=terminal; output_chars=123; output_preview=omitted",
])
def test_a_withheld_tool_output_summary_is_kept_as_a_source_only(tmp_path, text):
    """The capture filter's placeholder for an output it withheld, and the 2.0
    release's form of it: nothing to search by meaning or to derive from."""
    app, ctx = app_at(tmp_path)
    receipt = capture(app, replace(ctx, actor_origin="tool_observation"), "TEST-summary", text,
                      origin="tool_observation", role="tool")
    assert receipt.semantic_state == "not_scheduled"
    assert receipt.admission == ("admission_source_only:tool_output_omitted",)
    assert counts(app)["source_events"] == 1 and counts(app)["work_items"] == 0
