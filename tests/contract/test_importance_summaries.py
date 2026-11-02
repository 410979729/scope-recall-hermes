"""Importance metadata and grounded summaries preserve ordinary source authority."""

import itertools
import json
from dataclasses import replace
from datetime import UTC

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.importance import backfill_importance
from scope_recall.core.management import MemoryManagement
from scope_recall.core.restore import InstallationMaintenance
from scope_recall.core.summary_storage import close_session, queue_summaries
from scope_recall.core.summary_validity import summary_live
from scope_recall.core.worker import WorkerConfig, drain_worker
from test_claims import Clock, accept, capture, draft, initial
from v11_support import context


@pytest.fixture
def app(tmp_path):
    original = context(tmp_path / "TEST-summaries")
    binding = replace(original.binding, scope_ids=frozenset({"TEST-scope", "TEST-other"}))
    ctx = replace(
        original,
        binding=binding,
        allowed_scope_ids=frozenset({"TEST-scope"}),
        project_id="TEST-project",
        branch_id="TEST-main",
    )
    core = MemoryCore(CoreConfig(binding), clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    return core, ctx


class SummaryModel:
    """Select exact source sentences without a network request."""

    def judge(self, messages, *, remaining_seconds):
        import json

        body = json.loads(messages[-1]["content"])
        return json.dumps(
            {
                "sentences": [
                    dict(text=s["content"], source_ref=s["ref"], source_revision=s["revision"]) for s in body["sources"]
                ]
            }
        )


def build(core, ctx, model=None):
    with core.storage.write(ctx) as tx:
        close_session(tx, ctx.session_id, now=core.clock.utc_now())
        queue_summaries(tx, now=core.clock.utc_now(), limit=128)
        tx._check().execute("UPDATE work_items SET state='done' WHERE work_type!='rebuild_projection'")
    return drain_worker(
        core.storage,
        core.clock,
        ctx,
        config=WorkerConfig("TEST-summary", max_items=32, summary_capable=True),
        consolidation=model or SummaryModel(),
        remaining_seconds=30,
    )


def test_worker_prepares_a_brief_from_existing_facts_without_a_model(app):
    """An existing decision is ready at session start without invoking a model."""
    core, ctx = app
    item, _source = initial(core, ctx, when="2026-09-05T12:00:00Z")
    drain_worker(
        core.storage,
        core.clock,
        ctx,
        config=WorkerConfig("TEST-brief", max_items=32),
        remaining_seconds=30,
    )
    brief = core.session_brief(ctx)
    assert item.ref in brief["text"]
    assert "蓝色" in brief["text"]
    assert brief["items"][0]["ref"] == item.ref


@pytest.mark.parametrize("action", ["correct", "delete", "suppress", "quarantine", "private"])
def test_brief_rechecks_sources_before_background_rebuild(app, action):
    """A cached decision cannot survive a withdrawal or a newer fact version."""
    from scope_recall.core import private

    core, ctx = app
    item, source = initial(core, ctx, when="2026-09-05T12:00:00Z")
    drain_worker(core.storage, core.clock, ctx, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    assert item.ref in core.session_brief(ctx)["text"]
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    if action == "correct":
        owner.revise(item.ref, expected_revision=item.revision, value="green")
    elif action in {"delete", "suppress"}:
        owner.apply(owner.preview([source.ref], mode=action))
    elif action == "quarantine":
        with core.storage.write(ctx) as tx:
            tx._check(write=True).execute(
                "INSERT INTO source_safety(source_ref,source_revision,state,rules_json,scanned_at) VALUES(?,?,?,?,?)",
                (source.ref, source.revision, "quarantined", '["TEST-held"]', core.clock.now),
            )
    else:
        private.open_interval(core.storage, core.clock, ctx, target="global")
    assert item.ref not in core.session_brief(ctx)["text"]


def test_brief_omits_sources_private_in_another_session(app):
    from scope_recall.core import private

    core, writer = app
    item, _source = initial(core, writer, when="2026-09-05T12:00:00Z")
    drain_worker(core.storage, core.clock, writer, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    reader = replace(writer, session_id="TEST-private-reader")
    assert item.ref in core.session_brief(reader)["text"]
    private.open_interval(core.storage, core.clock, writer, target="session", session=writer.session_id)
    assert item.ref not in core.session_brief(reader)["text"]


def test_brief_does_not_cross_scopes(app):
    core, ctx = app
    item, _source = initial(core, ctx, when="2026-09-05T12:00:00Z")
    drain_worker(core.storage, core.clock, ctx, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    assert item.ref in core.session_brief(ctx)["text"]
    assert core.session_brief(replace(ctx, allowed_scope_ids=frozenset({"TEST-other"})))["items"] == []


def test_brief_manifest_version_tracks_evidence_state(app):
    from scope_recall.core.session_brief import rebuild_scope

    core, ctx = app
    _item, source = initial(core, ctx, when="2026-09-05T12:00:00Z")
    with core.storage.write(ctx) as tx:
        assert rebuild_scope(tx, "TEST-scope", core.clock.now)
        assert not rebuild_scope(tx, "TEST-scope", core.clock.now)
        tx._check(write=True).execute(
            "INSERT INTO source_safety(source_ref,source_revision,state,rules_json,scanned_at) VALUES(?,?,?,?,?)",
            (source.ref, source.revision, "quarantined", '["TEST-held"]', core.clock.now),
        )
        assert rebuild_scope(tx, "TEST-scope", core.clock.now)
        assert tx._check().execute("SELECT version FROM session_briefs WHERE scope_id='TEST-scope'").fetchone()[0] == 2


def test_brief_delivery_is_deduplicated_until_compaction(app):
    from scope_recall.core.session_brief import already_injected

    core, ctx = app
    item, source = initial(core, ctx, when="2026-09-05T12:00:00Z")
    drain_worker(core.storage, core.clock, ctx, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    assert item.ref in core.session_brief(ctx, deliver=True)["text"]
    assert core.session_brief(ctx, deliver=True)["text"] == ""
    with core.storage.read(ctx) as tx:
        assert already_injected(tx, "claim", item.ref, item.revision)
        assert not already_injected(tx, "event", source.ref, source.revision)
    assert item.ref in core.session_brief(ctx, deliver=True, compact=True)["text"]


def test_automatic_recall_does_not_repeat_briefed_evidence(app):
    from v11_support import recall_request

    core, writer = app
    item, source = initial(core, writer, when="2026-09-05T12:00:00Z")
    drain_worker(core.storage, core.clock, writer, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    reader = replace(writer, session_id="TEST-new-session")
    request = recall_request(query="TEST-project 配色 蓝色", mode="auto")
    before = core.recall_packet(reader, request, deadline_seconds=10)
    assert any(row["ref"] in {item.ref, source.ref} for row in before["items"])
    assert item.ref in core.session_brief(reader, deliver=True)["text"]
    after = core.recall_packet(reader, request, deadline_seconds=10)
    assert all(row["ref"] not in {item.ref, source.ref} for row in after["items"])
    core.reset_session_injections(reader, remaining_seconds=1)
    after_compaction = core.recall_packet(reader, request, deadline_seconds=10)
    assert any(row["ref"] in {item.ref, source.ref} for row in after_compaction["items"])


def test_session_totals_keep_brief_and_question_usage_separate(app):
    core, ctx = app
    initial(core, ctx, when="2026-09-05T12:00:00Z")
    drain_worker(core.storage, core.clock, ctx, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    opening = core.session_brief(ctx, deliver=True)
    core.record_session_recall(ctx, tokens=23, elapsed_ms=17, remaining_seconds=1)
    stats = core.session_injection_stats(ctx)
    assert stats["brief_tokens"] == opening["tokens"]
    assert stats["recall_tokens"] == 23
    assert stats["total_tokens"] == opening["tokens"] + 23
    assert stats["total_elapsed_ms"] == stats["brief_elapsed_ms"] + 17


def test_compaction_clears_session_injection_usage(app):
    """Usage belongs to the context that is discarded by host compaction."""
    core, ctx = app
    initial(core, ctx, when="2026-09-05T12:00:00Z")
    drain_worker(core.storage, core.clock, ctx, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    assert core.session_brief(ctx, deliver=True)["tokens"] > 0
    core.record_session_recall(ctx, tokens=23, elapsed_ms=17, remaining_seconds=1)
    core.reset_session_injections(ctx, remaining_seconds=1)
    assert core.session_injection_stats(ctx)["total_tokens"] == 0


def test_compaction_sequence_clears_usage_only_when_it_advances(app):
    """A replayed compaction must not erase usage in the new context."""
    core, ctx = app
    core.record_session_recall(ctx, tokens=23, elapsed_ms=17, remaining_seconds=1)
    compacted = replace(ctx, compaction_seq=1, compaction_epoch="TEST-epoch")
    assert core.sync_session_compaction(compacted, remaining_seconds=1)
    assert core.session_injection_stats(ctx)["total_tokens"] == 0
    core.record_session_recall(compacted, tokens=31, elapsed_ms=5, remaining_seconds=1)
    assert core.sync_session_compaction(compacted, remaining_seconds=1)
    assert core.session_injection_stats(ctx)["total_tokens"] == 31


def test_brief_caps_and_orders_recent_decisions_and_important_preferences(app):
    core, ctx = app
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    decisions, preferences = [], []
    for index in range(7):
        source = capture(core, ctx, f"TEST-project choice{index} value{index}.", when=f"2026-09-05T0{index}:00:00Z")
        item = accept(core, ctx, draft(source, f"value{index}", predicate=f"choice{index}")).items[0]
        assert item.state == "active"
        owner.importance(item.ref, expected_revision=item.revision, importance=5 if index == 0 else 4)
        decisions.append(item.ref)
    for index in range(10):
        source = capture(core, ctx, f"TEST-project preference{index} value{index}.")
        item = accept(
            core,
            ctx,
            draft(
                source, f"value{index}", kind="preference", predicate=f"preference{index}", statement_kind="assertion"
            ),
        ).items[0]
        assert item.state == "active"
        owner.importance(item.ref, expected_revision=item.revision, importance=4 if index < 9 else 2, pinned=index == 9)
        preferences.append(item.ref)
    drain_worker(core.storage, core.clock, ctx, config=WorkerConfig("TEST-brief", max_items=1), remaining_seconds=30)
    items = core.session_brief(ctx)["items"]
    chosen_decisions = [row["ref"] for row in items if row["kind"] == "decision"]
    chosen_preferences = [row["ref"] for row in items if row["kind"] == "preference"]
    assert len(chosen_decisions) == 5 and chosen_decisions[0] == decisions[0]
    assert len(chosen_preferences) == 8 and chosen_preferences[0] == preferences[9]


def test_brief_limits_unfinished_task_resumes(app):
    from scope_recall.core.episodes import source_watermark

    core, ctx = app
    episodes = []
    for index in range(4):
        task = replace(ctx, session_id=f"TEST-task-session-{index}", task_anchor=f"TEST-task-{index}")
        goal = f"Continue TEST-project task {index}."
        source = capture(core, task, goal, when=core.clock.now)
        refs = [f"{source.ref}@{source.revision}"]
        resume = {
            "episode_ref": None,
            "goal": {"text": goal, "evidence_refs": refs},
            "decisions": [],
            "verified_progress": [],
            "open_items": [],
            "blockers": [],
            "next_step": None,
            "next_step_basis": "unknown",
            "artifact_refs": [],
            "source_watermark": source_watermark(refs),
            "evidence_refs": refs,
        }
        receipt = core.records.accept_consolidation(
            task,
            {
                "protocol_version": "1.1",
                "source_refs": refs,
                "claim_proposals": [],
                "resume_proposals": [resume],
                "reference_proposals": [],
            },
            scope_id="TEST-scope",
            remaining_seconds=10,
        )
        episodes.append(receipt.items[0].ref)
    drain_worker(core.storage, core.clock, ctx, config=WorkerConfig("TEST-brief", max_items=1), remaining_seconds=30)
    tasks = [row for row in core.session_brief(ctx)["items"] if row["kind"] == "task"]
    assert len(tasks) == 3
    assert {row["ref"] for row in tasks} <= set(episodes)
    core.session_brief(ctx, deliver=True)
    owner = replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)
    attempts = MemoryManagement(core, InstallationMaintenance(owner)).recalls(session=ctx.session_id)["items"]
    logged = [row for attempt in attempts for row in attempt["items"]]
    assert {row.get("ref") for row in logged if row.get("source_type") == "task"} == {row["ref"] for row in tasks}
    finished = replace(ctx, session_id="TEST-task-session-0", task_anchor="TEST-task-0")
    capture(core, finished, "TEST-project task 0 is completed.", when=core.clock.now)
    drain_worker(core.storage, core.clock, ctx, config=WorkerConfig("TEST-brief", max_items=1), remaining_seconds=30)
    tasks = [row for row in core.session_brief(ctx)["items"] if row["kind"] == "task"]
    assert episodes[0] not in {row["ref"] for row in tasks}


def test_brief_switch_and_token_limit_do_not_require_a_model(app):
    core, ctx = app
    initial(core, ctx, when="2026-09-05T12:00:00Z")
    drain_worker(core.storage, core.clock, ctx, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    assert core.session_brief(ctx, token_limit=1)["text"] == ""
    assert core.session_brief(ctx, token_limit=1024)["text"]
    core.config = replace(core.config, session_brief_enabled=False)
    assert core.session_brief(ctx, deliver=True)["text"] == ""


def test_empty_safety_hold_set_does_not_walk_source_lineage(app):
    from scope_recall.core.injection_guard import blocked_refs

    core, ctx = app
    _item, source = initial(core, ctx)
    statements = []
    with core.storage.read(ctx) as tx:
        conn = tx._check()
        conn.set_trace_callback(statements.append)
        try:
            assert blocked_refs(tx, "event", (source.ref,)) == frozenset()
        finally:
            conn.set_trace_callback(None)
    assert not any("WITH RECURSIVE lineage" in statement for statement in statements)


def test_safety_hold_added_in_a_write_transaction_is_immediately_enforced(app):
    from scope_recall.core.injection_guard import blocked_refs

    core, ctx = app
    _item, source = initial(core, ctx)
    with core.storage.write(ctx) as tx:
        assert blocked_refs(tx, "event", (source.ref,)) == frozenset()
        tx._check(write=True).execute(
            "INSERT INTO source_safety(source_ref,source_revision,state,rules_json,scanned_at) VALUES(?,?,?,?,?)",
            (source.ref, source.revision, "quarantined", '["TEST-held"]', core.clock.now),
        )
        assert blocked_refs(tx, "event", (source.ref,)) == frozenset({source.ref})


def test_safety_hold_lookup_uses_evidence_and_source_group_indexes(app):
    from scope_recall.core.injection_guard import held_roots_sql

    core, ctx = app
    _item, source = initial(core, ctx)
    with core.storage.write(ctx) as tx:
        tx._check(write=True).execute(
            "INSERT INTO source_safety(source_ref,source_revision,state,rules_json,scanned_at) VALUES(?,?,?,?,?)",
            (source.ref, source.revision, "quarantined", '["TEST-held"]', core.clock.now),
        )
    with core.storage.read(ctx) as tx:
        sql = "EXPLAIN QUERY PLAN " + held_roots_sql("SELECT value,?,value FROM json_each(?)")
        details = [row[3] for row in tx._check().execute(sql, ("event", json.dumps([source.ref])))]
    assert not any(detail.startswith("SCAN h") for detail in details), details
    assert any("SEARCH h USING INDEX" in detail and "source_group_key" in detail for detail in details), details


def test_importance_defaults_backfill_owner_override_and_pin(app):
    core, ctx = app
    item, _source = initial(core, ctx)
    with core.storage.write(ctx) as tx:
        assert backfill_importance(tx, limit=1) == 1
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    owner.importance(item.ref, expected_revision=item.revision, importance=2, pinned=True)
    shown = owner.show(item.ref)["value"]
    assert shown["importance"] == 5 and shown["pinned"] is True
    owner.importance(item.ref, expected_revision=item.revision, importance=2, pinned=False)
    assert owner.show(item.ref)["value"]["importance"] == 2
    with pytest.raises(ContractError):
        owner.importance(item.ref, expected_revision=item.revision, importance=6)


def test_session_summary_is_a_claim_and_requires_complete_live_coverage(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project chose blue.")
    capture(core, ctx, "TEST-project checked the build.")
    result = build(core, ctx)
    assert result.failed == 0
    with core.storage.read(ctx) as tx:
        rows = tx._check().execute("SELECT claim_ref,claim_revision FROM summary_parts").fetchall()
        assert rows and all(summary_live(tx, *row) for row in rows)
        summary = tx.claims.version(*rows[0])
        assert summary.basis == "derived_summary"
        assert source.ref in str(summary.payload)
    denied = replace(ctx, allowed_scope_ids=frozenset({"TEST-other"}))
    with core.storage.read(denied) as tx:
        assert not summary_live(tx, *rows[0])


def test_new_source_and_fact_correction_invalidate_previous_summary(app):
    core, ctx = app
    item, _source = initial(core, ctx)
    build(core, ctx)
    with core.storage.read(ctx) as tx:
        row = tx._check().execute("SELECT claim_ref,claim_revision FROM summary_parts LIMIT 1").fetchone()
        assert summary_live(tx, *row)
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    owner.revise(item.ref, expected_revision=item.revision, value="green")
    with core.storage.read(ctx) as tx:
        assert not summary_live(tx, *row)


def test_open_task_and_active_session_keep_raw_sources(app):
    core, ctx = app
    core.clock.now = "2026-09-06T12:00:00Z"
    capture(
        core,
        replace(ctx, task_anchor="TEST-task"),
        "Implement TEST-project and run its checks.",
        when=core.clock.now,
        recorded_at=core.clock.now,
    )
    with core.storage.write(ctx) as tx:
        queue_summaries(tx, now=core.clock.utc_now(), limit=128)
        assert tx._check().execute("SELECT count(*) FROM summary_parts").fetchone()[0] == 0
        close_session(tx, ctx.session_id, now=core.clock.utc_now())


def scoped(core, ctx, query="昨天做了什么"):
    from datetime import timezone

    from scope_recall.core.recall_scope import query_scope
    from scope_recall.core.retrieval import SearchContext, SearchLimits
    from scope_recall.core.retrieval_storage import RetrievalStorage

    search = SearchContext(
        query,
        "auto",
        None,
        (),
        SearchLimits(),
        core.clock.monotonic() + 10,
        core.clock.utc_now(),
        ctx,
        scope=query_scope(query, now=core.clock.utc_now(), zone=UTC, entries={}),
    )
    with core.storage.read(ctx) as tx:
        return RetrievalStorage(clock=core.clock).scoped(tx, search, limit=6)


def test_dated_channel_uses_only_complete_summaries_and_retains_raw_fallback(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project decided to use blue.", when="2026-09-05T10:00:00Z")
    capture(core, ctx, "TEST-project checked the blue design.", when="2026-09-05T11:00:00Z")
    assert any(c.ref == source.ref for c in scoped(core, ctx))
    build(core, ctx)
    assert all(c.kind == "claim" for c in scoped(core, ctx))
    with core.storage.write(ctx) as tx:
        tx._check().execute("UPDATE summary_groups SET state='partial'")
    assert any(c.ref == source.ref for c in scoped(core, ctx))


@pytest.mark.parametrize("mode", ["suppress", "delete"])
def test_source_withdrawal_blocks_summary_before_worker_rebuild(app, mode):
    core, ctx = app
    source = capture(core, ctx, "TEST-project chose blue.")
    build(core, ctx)
    with core.storage.read(ctx) as tx:
        ref, revision = tx._check().execute("SELECT claim_ref,claim_revision FROM summary_parts LIMIT 1").fetchone()
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    owner.apply(owner.preview([source.ref], mode=mode))
    with core.storage.read(ctx) as tx:
        assert not summary_live(tx, ref, revision)
    with pytest.raises(ContractError):
        owner.show(ref)


def test_task_completion_queues_task_summary_and_idle_does_not_complete_open_task(app):
    core, ctx = app
    task = replace(ctx, task_anchor="TEST-build-task")
    capture(core, task, "Continue TEST-project and run its checks.")
    with core.storage.read(ctx) as tx:
        assert (
            tx._check().execute("SELECT state FROM episode_versions ORDER BY revision DESC LIMIT 1").fetchone()[0]
            == "open"
        )
    core.clock.now = "2026-09-06T13:00:00Z"
    build(core, task)
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM summary_parts").fetchone()[0] == 0
    capture(core, task, "The task is completed.", when="2026-09-06T13:00:00Z")
    build(core, task)
    with core.storage.read(ctx) as tx:
        groups = tx._check().execute("SELECT kind,state,covered_units,total_units FROM summary_groups").fetchall()
        assert any(g[0] == "task" and g[1] == "complete" and g[2] == g[3] for g in groups)


def test_importance_tool_requires_current_owner_words_and_preserves_content(app):
    from test_claims import revise_request

    core, ctx = app
    item, _source = initial(core, ctx)
    source = capture(core, ctx, f"更正 {item.ref} 的重要度为 2，取消置顶。", when=core.clock.utc_now())
    result = core.revise(ctx, revise_request(item, source, {"importance": 2, "pinned": False}))
    assert result.items[0].revision == item.revision
    with core.storage.read(ctx) as tx:
        head = tx.claims.version(item.ref, item.revision)
        assert head.importance == 2 and head.payload["value_text"] == "蓝色"


def test_previous_conversation_uses_its_summary_without_losing_raw_fallback(app):
    from scope_recall.core.retrieval import SearchContext, SearchLimits
    from scope_recall.core.retrieval_storage import RetrievalStorage
    from scope_recall.core.summary_storage import previous_conversation_scope

    core, ctx = app
    source = capture(core, ctx, "TEST-project decided to use blue.")
    build(core, ctx)
    reader = replace(ctx, session_id="TEST-next-session")
    with core.storage.read(reader) as tx:
        scope = previous_conversation_scope(tx, "上次聊到哪", now=core.clock.utc_now())
        assert scope is not None and scope.session_ids == (ctx.session_id,)
        search = SearchContext(
            "上次聊到哪",
            "auto",
            None,
            (),
            SearchLimits(),
            core.clock.monotonic() + 10,
            core.clock.utc_now(),
            reader,
            scope=scope,
        )
        assert any(c.kind == "claim" for c in RetrievalStorage(clock=core.clock).scoped(tx, search, limit=6))
    with core.storage.write(ctx) as tx:
        tx._check().execute("UPDATE summary_groups SET state='partial'")
    with core.storage.read(reader) as tx:
        assert any(c.ref == source.ref for c in RetrievalStorage(clock=core.clock).scoped(tx, search, limit=6))


def test_additive_upgrade_preserves_sources_and_returns_neutral_importance(app):
    from scope_recall.core.schema import SCHEMA_VERSION
    from v11_support import downgrade_store

    core, ctx = app
    item, source = initial(core, ctx)
    before = source.event.copy()
    downgrade_store(core.storage.path, 1119)
    assert core.initialize().schema_version == SCHEMA_VERSION
    assert core.source(ctx, source.ref, source.revision).event == before
    with core.storage.write(ctx) as tx:
        assert tx.claims.version(item.ref, item.revision).importance == 3
        assert backfill_importance(tx, limit=1) == 1


def test_incomplete_model_output_preserves_raw_fallback(app):
    class Incomplete:
        def judge(self, messages, *, remaining_seconds):
            return '{"sentences":[]}'

    core, ctx = app
    source = capture(core, ctx, "TEST-project decided to use blue.", when="2026-09-05T10:00:00Z")
    build(core, ctx, Incomplete())
    assert any(c.ref == source.ref for c in scoped(core, ctx))
    with core.storage.read(ctx) as tx:
        assert all(
            not summary_live(tx, *r) for r in tx._check().execute("SELECT claim_ref,claim_revision FROM summary_parts")
        )


def test_large_source_prefix_is_processed_without_replacing_the_original(app):
    import json

    from scope_recall.core.summary_storage import fully_quoted_sources

    class Excerpts:
        def __init__(self):
            self.pages = []

        def judge(self, messages, *, remaining_seconds):
            body = json.loads(messages[-1]["content"])
            self.pages.append(body["sources"])
            return json.dumps(
                dict(
                    sentences=[
                        dict(
                            text=s["content"].split(". ")[0].strip() + ".",
                            source_ref=s["ref"],
                            source_revision=s["revision"],
                        )
                        for s in body["sources"]
                    ]
                )
            )

    core, ctx = app
    source = capture(core, ctx, "TEST-project keeps original evidence. " * 500)
    model = Excerpts()
    result = build(core, ctx, model)
    assert result.failed == 0
    assert model.pages[0][0]["content"] == source.event["content"][:1000] + (
        f"（已截断，全文 {len(source.event['content'])} 字符）"
    )
    with core.storage.read(ctx) as tx:
        group = tx._check().execute("SELECT * FROM summary_groups WHERE kind='session'").fetchone()
        assert group["covered_units"] == group["total_units"] == len(source.event["content"])
        parts = tx._check().execute("SELECT * FROM summary_parts ORDER BY ordinal").fetchall()
        assert len(parts) == 1
        assert all(summary_live(tx, p["claim_ref"], p["claim_revision"]) for p in parts)
        versions = [tx.claims.version(p["claim_ref"], p["claim_revision"]) for p in parts]
        assert not fully_quoted_sources(tx, versions, {(source.ref, source.revision)})


def test_unrelated_tool_capture_does_not_strand_a_complete_session_summary(app):
    from scope_recall.core.summary_storage import next_summary_due

    core, ctx = app
    capture(core, ctx, "TEST-project chose blue.")
    build(core, ctx)
    capture(core, ctx, "TEST tool returned 0.", origin="tool_observation")
    with core.storage.write(ctx) as tx:
        for _ in range(3):
            queue_summaries(tx, now=core.clock.utc_now(), limit=128)
    with core.storage.read(ctx) as tx:
        parts = tx._check().execute("SELECT claim_ref,claim_revision FROM summary_parts").fetchall()
        assert parts and all(summary_live(tx, *p) for p in parts)
        assert next_summary_due(tx, now=core.clock.utc_now()) is None


def test_as_of_conversation_keeps_raw_sources_instead_of_undated_summary(app):
    from datetime import timezone

    from scope_recall.core.recall_scope import query_scope
    from scope_recall.core.retrieval import SearchContext, SearchLimits
    from scope_recall.core.retrieval_storage import RetrievalStorage

    core, ctx = app
    source = capture(core, ctx, "TEST-project decided to use blue.", when="2026-09-05T10:00:00Z")
    build(core, ctx)
    search = SearchContext(
        "昨天做了什么",
        "as_of",
        core.clock.utc_now(),
        (),
        SearchLimits(),
        core.clock.monotonic() + 10,
        core.clock.utc_now(),
        ctx,
        scope=query_scope("昨天做了什么", now=core.clock.utc_now(), zone=UTC, entries={}),
    )
    reader = RetrievalStorage(clock=core.clock)
    with core.storage.read(ctx) as tx:
        found = reader.scoped(tx, search, limit=6)
        assert any(c.ref == source.ref for c in found)
        assert all(reader.hydrate(tx, c, search) is not None for c in found)


@pytest.mark.parametrize(
    "content,text",
    [
        ("TEST-owner does not prefer blue.", "prefer blue."),
        ("Do not deploy TEST-project unless owner approves.", "deploy TEST-project"),
        ("未经批准，不得发布TEST-project。", "发布TEST-project。"),
    ],
)
def test_summary_excerpt_keeps_negation_and_conditions(app, content, text):
    from scope_recall.core.consolidation_summary import grounded_sentences

    core, ctx = app
    source = capture(core, ctx, content)
    sentences, _spans = grounded_sentences(
        dict(sentences=[dict(text=text, source_ref=source.ref, source_revision=source.revision)]), (source,)
    )
    assert sentences == [content]


def test_summary_enforces_the_shared_claim_text_budget(app):
    from scope_recall.core.consolidation_summary import grounded_sentences

    core, ctx = app
    source = capture(core, ctx, "X" * 3000)
    sentences, _spans = grounded_sentences(
        dict(sentences=[dict(text=source.event["content"], source_ref=source.ref, source_revision=source.revision)]),
        (source,),
    )
    assert sentences == []


def test_obsolete_summary_error_cannot_change_a_newer_generation(app):
    core, ctx = app
    capture(core, ctx, "TEST-project chose blue.")

    class Superseded:
        def judge(self, messages, *, remaining_seconds):
            with core.storage.write(ctx) as tx:
                tx._check().execute("UPDATE summary_groups SET generation=generation+1,state='complete'")
            return '{"sentences":[]}'

    build(core, ctx, Superseded())
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT state FROM summary_groups").fetchone()[0] == "complete"
        assert (
            tx._check().execute("SELECT state FROM work_items WHERE work_type='rebuild_projection'").fetchone()[0]
            == "obsolete"
        )


def test_extracted_importance_has_one_current_metadata_source(app):
    from test_claims import accept, draft

    core, ctx = app
    source = capture(core, ctx, "TEST-project 配色 蓝色。")
    item = accept(core, ctx, draft(source, importance=4)).items[0]
    with core.storage.read(ctx) as tx:
        head = tx.claims.version(item.ref, item.revision)
        assert head.importance == 4 and "importance" not in head.payload


def test_other_derived_fact_versions_remain_summary_dependencies(app):
    from scope_recall.core.claims import Qualification

    core, ctx = app
    item, _source = initial(core, ctx)
    with core.storage.write(ctx) as tx:
        head = tx.claims.version(item.ref, item.revision)
        derived = tx.claims.append(
            head.scope_id,
            head.payload,
            Qualification("active", "derived_summary", "verified_first_hand_report"),
            previous=head,
            recorded_at=core.clock.utc_now(),
        )
    build(core, ctx)
    with core.storage.read(ctx) as tx:
        old_part = tuple(tx._check().execute("SELECT claim_ref,claim_revision FROM summary_parts LIMIT 1").fetchone())
        assert summary_live(tx, *old_part)
    with core.storage.write(ctx) as tx:
        head = tx.claims.version(derived.ref, derived.revision)
        tx.claims.append(
            head.scope_id,
            head.payload,
            Qualification("active", "derived_summary", "verified_first_hand_report"),
            previous=head,
            recorded_at=core.clock.utc_now(),
        )
        assert not summary_live(tx, *old_part)
        for _ in range(3):
            queue_summaries(tx, now=core.clock.utc_now(), limit=128)
    with core.storage.read(ctx) as tx:
        # An unchanged assertion is safe to renew after a fresh dependency check.
        assert summary_live(tx, *old_part)


def test_runtime_summary_capability_requires_an_explicit_model_and_budget(app):
    from decimal import Decimal
    from types import SimpleNamespace

    from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig, summary_budget_enabled
    from scope_recall.runtime.model_budget import ModelPricing

    core, _ctx = app
    empty = AuxiliaryRuntimeConfig.from_mapping(dict(external_embedding=False, external_consolidation=False))
    assert not summary_budget_enabled(empty)
    route = SimpleNamespace(model="TEST-summary-model")
    configured = replace(
        empty,
        external_consolidation=True,
        consolidation=route,
        ledger_path=core.storage.path.parent / "TEST-budget.sqlite3",
    )
    assert not summary_budget_enabled(configured)
    budget = replace(
        configured.budget,
        total_input_cap=10000,
        total_output_cap=10000,
        total_call_cap=10,
        approved_models=frozenset({route.model}),
        pricing={route.model: ModelPricing(Decimal(0), Decimal(0))},
    )
    assert summary_budget_enabled(replace(configured, budget=budget))
    assert not summary_budget_enabled(replace(configured, budget=replace(budget, total_call_cap=0)))


def test_short_acknowledgements_do_not_hide_an_otherwise_complete_summary(app):
    core, ctx = app
    capture(core, ctx, "TEST-project decided to use blue.", when="2026-09-05T10:00:00Z")
    capture(core, ctx, "好的", when="2026-09-05T11:00:00Z")
    build(core, ctx)
    assert scoped(core, ctx) and all(c.kind == "claim" for c in scoped(core, ctx))


def test_configured_runtime_automatically_queues_an_ended_session(app):
    import json
    from decimal import Decimal

    from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig
    from scope_recall.runtime.consolidation_models import ConsolidationRouteConfig
    from scope_recall.runtime.instance import build_runtime_instance
    from scope_recall.runtime.instance_config import RuntimeInstanceConfig
    from scope_recall.runtime.model_budget import ModelPricing

    core, ctx = app
    capture(core, ctx, "TEST-project decided to use blue.")
    with core.storage.write(ctx) as tx:
        close_session(tx, ctx.session_id, now=core.clock.utc_now())
        tx._check().execute("UPDATE work_items SET state='done'")
    base = AuxiliaryRuntimeConfig.from_mapping(dict(external_embedding=False, external_consolidation=False))
    route = ConsolidationRouteConfig(
        "TEST-summary-model",
        "http://127.0.0.1:9/v1/chat/completions",
        "TEST_UNUSED",
        "max_tokens",
        1024,
        allow_insecure_endpoint=True,
    )
    budget = replace(
        base.budget,
        total_input_cap=1000000,
        total_output_cap=1000000,
        total_call_cap=100,
        approved_models=frozenset({route.model}),
        pricing={route.model: ModelPricing(Decimal(0), Decimal(0))},
    )
    auxiliary = replace(
        base,
        external_consolidation=True,
        consolidation=route,
        budget=budget,
        ledger_path=core.storage.path.parent / "TEST-budget.sqlite3",
    )
    config = RuntimeInstanceConfig(
        ctx.binding,
        ctx.session_id,
        ctx.allowed_scope_ids,
        project_id=ctx.project_id,
        branch_id=ctx.branch_id,
        auxiliary=auxiliary,
        supervisor_enabled=False,
    )

    class Port:
        def propose(self, messages, *, remaining_seconds):
            assert "sources" in json.loads(messages[-1]["content"])
            return SummaryModel().judge(messages, remaining_seconds=remaining_seconds)

    instance = build_runtime_instance(config, consolidation=Port())
    try:
        assert instance.drain(remaining_seconds=20).failed == 0
    finally:
        instance.close()
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT state FROM summary_groups").fetchone()[0] == "complete"


def test_management_page_preview_applies_importance_and_pinning(app):
    from scope_recall.maintenance.ui_api import UIApi

    core, ctx = app
    item, _source = initial(core, ctx)
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    api = UIApi(owner)
    preview = api.preview(
        dict(action="importance", ref=item.ref, expected_revision=item.revision, importance=2, pinned=True)
    )
    api.confirm(dict(id=preview["id"], confirmed=True))
    value = owner.show(item.ref)["value"]
    assert value["importance"] == 5 and value["pinned"]


def test_importance_cli_uses_the_owner_management_boundary(app):
    from scope_recall.maintenance.memory import execute, parser

    core, ctx = app
    item, _source = initial(core, ctx)
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    args = parser().parse_args(
        ["importance", item.ref, "--expected-revision", str(item.revision), "--score", "2", "--unpin"]
    )
    value = execute(owner, args)["value"]
    assert value["importance"] == 2 and value["pinned"] is False


def test_summary_waits_while_source_entry_is_paused(app):
    core, ctx = app
    capture(core, ctx, "TEST paused session summary source.")
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    owner.set_entry_state("local", "paused", actor="TEST-owner")

    class CountingSummary(SummaryModel):
        calls = 0

        def judge(self, messages, *, remaining_seconds):
            self.calls += 1
            return super().judge(messages, remaining_seconds=remaining_seconds)

    model = CountingSummary()
    build(core, ctx, model)
    assert model.calls == 0
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM summary_parts WHERE coverage_json != '[]'").fetchone()[0] == 0
        assert (
            tx._check().execute("SELECT count(*) FROM work_items WHERE last_error_code='entry_paused'").fetchone()[0]
            >= 1
        )


def test_summary_discards_result_when_entry_was_paused_during_call(app):
    core, ctx = app
    capture(core, ctx, "TEST session summary state transition.")
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))

    class TransitionSummary(SummaryModel):
        def judge(self, messages, *, remaining_seconds):
            owner.set_entry_state("local", "paused", actor="TEST-owner")
            owner.set_entry_state("local", "normal", actor="TEST-owner")
            return super().judge(messages, remaining_seconds=remaining_seconds)

    build(core, ctx, TransitionSummary())
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM summary_parts WHERE coverage_json != '[]'").fetchone()[0] == 0
        assert (
            tx._check().execute("SELECT count(*) FROM work_items WHERE last_error_code='entry_paused'").fetchone()[0]
            >= 1
        )


def test_brief_fact_does_not_hide_unshown_source_details(app):
    from v11_support import recall_request

    core, writer = app
    source = capture(core, writer, "TEST-project color blue. Delivery deadline Friday.", when=core.clock.now)
    item = accept(
        core, writer, draft(source, "blue", predicate="color", quote_override="TEST-project color blue")
    ).items[0]
    drain_worker(core.storage, core.clock, writer, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    reader = replace(writer, session_id="TEST-details-reader")
    request = recall_request(query="Delivery deadline Friday", mode="auto")
    before = core.recall_packet(reader, request, deadline_seconds=10)
    assert any(row["ref"] == source.ref for row in before["items"])
    brief = core.session_brief(reader, deliver=True)
    assert item.ref in brief["text"] and "Friday" not in brief["text"]
    after = core.recall_packet(reader, request, deadline_seconds=10)
    assert any(row["ref"] == source.ref for row in after["items"])


def test_brief_task_goal_does_not_hide_resume_details(app):
    from scope_recall.core.retrieval import CandidateRef, SearchContext
    from v11_support import recall_request

    from tests.contract.test_episodes import apply, ref, resume

    core, writer = app
    task = replace(writer, session_id="TEST-task-writer", task_anchor="TEST-task-details")
    source = capture(
        core, task, "TEST task goal launch; progress foundation done; next step deliver Friday.", when=core.clock.now
    )
    proposal = resume(
        source,
        goal=dict(text="TEST task goal launch", evidence_refs=[ref(source)]),
        verified_progress=[dict(text="foundation done", evidence_refs=[ref(source)])],
        next_step="deliver Friday",
        next_step_basis="user_requested",
    )
    episode = apply(core, task, resumes=[proposal]).items[0]
    drain_worker(core.storage, core.clock, writer, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    reader = replace(writer, session_id="TEST-task-reader")
    brief = core.session_brief(reader, deliver=True)
    assert episode.ref in brief["text"] and "Friday" not in brief["text"]
    request = recall_request(query="TEST task deliver Friday", mode="auto")
    search = SearchContext.from_request(request, reader, now=core.clock.utc_now(), deadline=core.clock.monotonic() + 10)
    with core.storage.read(reader) as tx:
        candidate = CandidateRef("episode", episode.ref, episode.revision, "lexical")
        hydrated = core.recall_pipeline._hydrate_admit(tx, candidate, search)
    assert hydrated is not None and "Friday" in hydrated.content


def test_brief_signature_tracks_earlier_task_evidence_deletion(app):
    from scope_recall.core.session_brief import rebuild_scope

    from tests.contract.test_episodes import apply, ref, resume

    core, writer = app
    task = replace(writer, session_id="TEST-signature-task", task_anchor="TEST-signature-task")
    first = capture(core, task, "TEST task first detail", when=core.clock.now)
    apply(core, task, resumes=[resume(first)])
    second = capture(core, task, "TEST task second detail", when=core.clock.now)
    refs = [ref(first), ref(second)]
    from scope_recall.core.episodes import source_watermark

    apply(
        core,
        task,
        resumes=[resume(second, evidence_refs=refs, source_watermark=source_watermark(refs))],
        source_refs=refs,
    )
    with core.storage.write(writer) as tx:
        assert rebuild_scope(tx, "TEST-scope", core.clock.now)
        item = json.loads(
            tx._check().execute("SELECT items_json FROM session_briefs WHERE scope_id='TEST-scope'").fetchone()[0]
        )[0]
        earlier = (
            tx._check()
            .execute(
                "SELECT object_revision FROM evidence_links WHERE object_kind='episode' "
                "AND object_ref=? AND source_ref=?",
                (item["ref"], first.ref),
            )
            .fetchone()[0]
        )
        assert earlier < item["revision"]
        tx._check(write=True).execute("UPDATE source_events SET read_blocked=1 WHERE event_id=?", (first.ref,))
        assert rebuild_scope(tx, "TEST-scope", core.clock.now)


def test_deleting_earlier_task_evidence_rebuilds_manifest(app):
    from scope_recall.core.episodes import source_watermark
    from scope_recall.core.session_brief import rebuild_scope

    from tests.contract.test_episodes import apply, ref, resume

    core, writer = app
    task = replace(writer, session_id="TEST-delete-task", task_anchor="TEST-delete-task")
    first = capture(core, task, "TEST task first detail", when=core.clock.now)
    episode = apply(core, task, resumes=[resume(first)]).items[0]
    second = capture(core, task, "TEST task next detail", when=core.clock.now)
    refs = [ref(first), ref(second)]
    apply(
        core,
        task,
        resumes=[resume(second, evidence_refs=refs, source_watermark=source_watermark(refs))],
        source_refs=refs,
    )
    with core.storage.write(writer) as tx:
        assert rebuild_scope(tx, "TEST-scope", core.clock.now)
        before = tx._check().execute("SELECT version FROM session_briefs WHERE scope_id='TEST-scope'").fetchone()[0]
    owner = MemoryManagement(core, InstallationMaintenance(replace(writer, allowed_scope_ids=writer.binding.scope_ids)))
    owner.apply(owner.preview([first.ref], mode="delete"))
    with core.storage.write(writer) as tx:
        assert rebuild_scope(tx, "TEST-scope", core.clock.now)
        after = tx._check().execute("SELECT version FROM session_briefs WHERE scope_id='TEST-scope'").fetchone()[0]
    assert after > before
    assert episode.ref not in core.session_brief(writer)["text"]


def test_brief_returns_without_delivery_mark_when_writer_is_busy(app):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError

    core, writer = app
    item, _source = initial(core, writer, when=core.clock.now)
    drain_worker(core.storage, core.clock, writer, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    reader = replace(writer, session_id="TEST-locked-brief")
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        with core.storage.write(writer):
            future = executor.submit(core.session_brief, reader, deliver=True, remaining_seconds=2)
            try:
                result = future.result(timeout=0.3)
            except TimeoutError:
                pytest.fail("brief waited for the worker's write lock")
            assert item.ref in result["text"]
            assert result.get("delivery_reason") == "brief_delivery_unconfirmed"
        assert item.ref in core.session_brief(reader, deliver=True)["text"]
    finally:
        executor.shutdown(wait=True)


def test_prepared_brief_is_discarded_if_session_becomes_private(app):
    from scope_recall.core import private

    core, ctx = app
    item, _source = initial(core, ctx, when="2026-09-05T12:00:00Z")
    drain_worker(core.storage, core.clock, ctx, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    brief = core.session_brief(ctx, deliver=True, defer_confirmation=True)
    assert item.ref in brief["text"]
    private.open_interval(core.storage, core.clock, ctx, target="session", session=ctx.session_id)
    assert core.confirm_session_brief(ctx, brief, remaining_seconds=1) == "brief_invalidated"


def test_same_response_question_fact_is_not_also_in_brief(app):
    core, ctx = app
    item, _source = initial(core, ctx, when="2026-09-05T12:00:00Z")
    drain_worker(core.storage, core.clock, ctx, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    brief = core.session_brief(ctx, deliver=True, defer_confirmation=True)
    core.remove_question_overlaps(brief, f"question recall fact {item.ref}")
    assert item.ref not in brief["text"]
    assert not brief["items"]


def test_compaction_clears_delivery_even_during_full_privacy(app):
    from scope_recall.core import private
    from scope_recall.core.session_brief import already_injected

    core, ctx = app
    item, _source = initial(core, ctx, when="2026-09-05T12:00:00Z")
    drain_worker(core.storage, core.clock, ctx, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    assert item.ref in core.session_brief(ctx, deliver=True)["text"]
    private.open_interval(core.storage, core.clock, ctx, target="session", session=ctx.session_id)
    assert not core.session_brief(ctx, deliver=True, compact=True)["text"]
    with core.storage.read(ctx) as tx:
        assert not already_injected(tx, "claim", item.ref, item.revision)


def test_persisted_compaction_is_used_without_a_host_mark(app):
    from scope_recall.core.session_brief import already_injected

    core, writer = app
    item, _source = initial(core, writer, when=core.clock.now)
    drain_worker(core.storage, core.clock, writer, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    reader = replace(writer, session_id="TEST-lost-compaction")
    assert item.ref in core.session_brief(reader, deliver=True)["text"]
    compacted = replace(reader, compacted_at="2026-09-07T12:00:00Z", compaction_seq=1, compaction_epoch="TEST-epoch")
    assert core.sync_session_compaction(compacted)
    with core.storage.write(reader) as tx:
        tx._check(write=True).execute(
            "INSERT INTO session_brief_injections(entry_id,session_id,object_kind,object_ref,revision,"
            "delivered_at,delivery_compacted_at) VALUES(?,?,?,?,?,?,?)",
            (
                reader.binding.installation_id,
                reader.session_id,
                "claim",
                item.ref,
                item.revision,
                "2026-09-06T12:00:00.000000+00:00",
                None,
            ),
        )
    with core.storage.read(reader) as tx:
        assert not already_injected(tx, "claim", item.ref, item.revision)
    assert item.ref in core.session_brief(reader, deliver=True)["text"]


def test_older_inflight_brief_cannot_confirm_after_newer_compaction(app):
    core, writer = app
    item, _source = initial(core, writer, when=core.clock.now)
    drain_worker(core.storage, core.clock, writer, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    reader = replace(writer, session_id="TEST-overlapping-compaction")
    assert item.ref in core.session_brief(reader, deliver=True)["text"]
    older = replace(reader, compacted_at="2026-09-07T12:00:00Z", compaction_seq=1, compaction_epoch="TEST-epoch")
    newer = replace(reader, compacted_at="2026-09-08T12:00:00Z", compaction_seq=2, compaction_epoch="TEST-epoch")
    earlier_brief = core.session_brief(older, deliver=True, compact=True, defer_confirmation=True)
    later_brief = core.session_brief(newer, deliver=True, compact=True, defer_confirmation=True)
    assert core.confirm_session_brief(newer, later_brief, remaining_seconds=1) == "brief_delivered"
    assert core.confirm_session_brief(older, earlier_brief, remaining_seconds=1) == "brief_invalidated"


def test_brief_delivery_is_visible_in_recall_history(app):
    core, ctx = app
    item, _source = initial(core, ctx, when=core.clock.now)
    drain_worker(core.storage, core.clock, ctx, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    brief = core.session_brief(ctx, deliver=True)
    assert item.ref in brief["text"]
    owner = replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)
    history = MemoryManagement(core, InstallationMaintenance(owner)).recalls(session=ctx.session_id)
    rows = [row for attempt in history["items"] for row in attempt["items"] if row.get("ref") == item.ref]
    assert any(
        "session_brief" in row["channels"] and row["reason"] == "delivered" and row["source_type"] == "decision"
        for row in rows
    )


def test_brief_unconfirmed_is_recorded_without_claiming_delivery(app):
    from concurrent.futures import ThreadPoolExecutor

    core, ctx = app
    item, _source = initial(core, ctx, when=core.clock.now)
    drain_worker(core.storage, core.clock, ctx, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with core.storage.write(ctx):
            brief = executor.submit(core.session_brief, ctx, deliver=True, remaining_seconds=2).result(timeout=1)
    assert brief["delivery_reason"] == "brief_delivery_unconfirmed"
    owner = replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)
    history = MemoryManagement(core, InstallationMaintenance(owner)).recalls(session=ctx.session_id)
    rows = [row for attempt in history["items"] for row in attempt["items"] if row.get("ref") == item.ref]
    assert any("session_brief" in row["channels"] and row["reason"] == "brief_delivery_unconfirmed" for row in rows)


@pytest.mark.parametrize("close", [False, True])
def test_brief_diagnostic_respects_private_interval_opened_after_preparation(app, close):
    from scope_recall.core import private

    core, ctx = app
    initial(core, ctx, when=core.clock.now)
    drain_worker(core.storage, core.clock, ctx, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    brief = core.session_brief(ctx, deliver=True, defer_confirmation=True)
    private.open_interval(core.storage, core.clock, ctx, target="session", session=ctx.session_id, mode="read-only")
    if close:
        private.close_interval(core.storage, core.clock, ctx, target="session", session=ctx.session_id)
    core.confirm_session_brief(ctx, brief)
    core.recall_diagnostics.flush()
    assert not core.recall_diagnostics.store.list(session_id=ctx.session_id)


def test_constraint_brief_keeps_its_category_in_recall_history(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project constraint stay offline.", when=core.clock.now)
    proposal = draft(source, "stay offline", kind="constraint", predicate="network", statement_kind="assertion")
    item = accept(core, ctx, proposal).items[0]
    owner = replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)
    management = MemoryManagement(core, InstallationMaintenance(owner))
    management.importance(item.ref, expected_revision=item.revision, importance=4, pinned=False)
    drain_worker(core.storage, core.clock, ctx, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    assert item.ref in core.session_brief(ctx, deliver=True)["text"]
    rows = [row for attempt in management.recalls(session=ctx.session_id)["items"] for row in attempt["items"]]
    assert any(row.get("ref") == item.ref and row["source_type"] == "constraint" for row in rows)


def test_prepared_brief_with_expired_safety_budget_is_not_released(app):
    core, ctx = app
    item, source = initial(core, ctx, when=core.clock.now)
    drain_worker(core.storage, core.clock, ctx, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    brief = core.session_brief(ctx, deliver=True, defer_confirmation=True)
    assert item.ref in brief["text"]
    with core.storage.write(ctx) as tx:
        tx._check(write=True).execute(
            "INSERT INTO source_safety(source_ref,source_revision,state,rules_json,scanned_at) VALUES(?,?,?,?,?)",
            (source.ref, source.revision, "quarantined", '["TEST-held"]', core.clock.now),
        )
    assert core.confirm_session_brief(ctx, brief, remaining_seconds=0) == "brief_safety_unconfirmed"


def test_compaction_counter_advances_when_wall_clock_moves_back(tmp_path):
    from scope_recall.adapters.clients.session_marks import clear_compaction, mark_compaction

    config = {"state_dir": tmp_path / "TEST-client"}
    first = mark_compaction(config, "TEST-session", "2026-09-06T12:00:00Z")
    clear_compaction(config, "TEST-session", first)
    second = mark_compaction(config, "TEST-session", "2026-09-06T11:59:59Z")
    assert second > first


def test_old_compaction_ack_cannot_clear_a_new_counter_epoch(tmp_path):
    from scope_recall.adapters.clients.session_marks import (
        clear_compaction,
        compaction_marker,
        mark_compaction,
        mark_path,
    )

    config = {"state_dir": tmp_path / "TEST-client"}
    session = "TEST-new-epoch"
    mark_compaction(config, session, "2026-09-06T12:00:00Z")
    old = compaction_marker(config, session)
    mark_path(config, "compaction-counters", session).unlink()
    mark_compaction(config, session, "2026-09-06T11:59:59Z")
    new = compaction_marker(config, session)
    assert new["epoch"] != old["epoch"]
    clear_compaction(config, session, old["seq"], old["epoch"])
    assert compaction_marker(config, session) == new


def test_replayed_old_compaction_epoch_does_not_invalidate_new_delivery(app):
    from scope_recall.core.session_brief import already_injected

    core, writer = app
    item, _source = initial(core, writer, when=core.clock.now)
    drain_worker(core.storage, core.clock, writer, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    reader = replace(writer, session_id="TEST-epoch-replay")
    first = replace(reader, compacted_at="2026-09-06T12:00:00Z", compaction_seq=1, compaction_epoch="TEST-first")
    second = replace(reader, compacted_at="2026-09-06T11:59:59Z", compaction_seq=1, compaction_epoch="TEST-second")
    assert item.ref in core.session_brief(first, deliver=True, compact=True)["text"]
    assert item.ref in core.session_brief(second, deliver=True, compact=True)["text"]
    assert core.sync_session_compaction(first)
    with core.storage.read(reader) as tx:
        assert already_injected(tx, "claim", item.ref, item.revision)
    assert core.session_brief(first, deliver=True, compact=True)["text"] == ""


def _render_delivery_packet(core, ctx, packet):
    """Keep the real core's entry fence while choosing synthetic delivery contents."""
    from v11_support import recall_request

    registered = core.recall_packet(ctx, recall_request(query="TEST-project"))
    registered.update(packet)
    return core.prepare_recall_render(ctx, registered)


def _question_text(core, ctx, request_id="TEST-delivery"):
    from scope_recall.adapters.runtime_wiring import render_host_recall_context
    from v11_support import recall_request

    packet = core.recall_packet(ctx, recall_request(query="TEST-project 配色", mode="auto", request_id=request_id))
    prepared = core.prepare_recall_render(ctx, packet)
    return render_host_recall_context(prepared.canonical_text, context=prepared.context)


def _deliver_question(core, ctx):
    prepared = core.prepare_session_injection(ctx, _question_text(core, ctx))
    assert core.confirm_session_brief(ctx, prepared, remaining_seconds=1) == "brief_delivered"
    return prepared


def test_question_delivery_preparation_is_read_only_and_counts_final_text(app):
    from scope_recall.core.recall_budget import estimate_tokens

    core, ctx = app
    item, _ = initial(core, ctx)
    prepared = core.prepare_session_injection(ctx, _question_text(core, ctx))
    with core.storage.read(ctx) as tx:
        assert not tx._check().execute("SELECT 1 FROM session_brief_injections").fetchone()
    assert core.confirm_session_brief(ctx, prepared, remaining_seconds=1) == "brief_delivered"
    assert core.session_injection_stats(ctx)["total_tokens"] == estimate_tokens(prepared["_injection"]["text"])
    with core.storage.read(ctx) as tx:
        assert (
            tx._check()
            .execute("SELECT source FROM session_brief_injections WHERE object_ref=?", (item.ref,))
            .fetchone()[0]
            == "question"
        )


@pytest.mark.parametrize("change", ["correct", "delete", "suppress", "quarantine", "private"])
def test_delivered_changes_notify_once_without_old_content(app, change):
    from scope_recall.core import private

    core, ctx = app
    item, source = initial(core, ctx)
    _deliver_question(core, ctx)
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    if change == "correct":
        owner.revise(item.ref, expected_revision=item.revision, value="green")
    elif change in {"delete", "suppress"}:
        owner.apply(owner.preview([item.ref], mode=change))
    elif change == "quarantine":
        with core.storage.write(ctx) as tx:
            tx._check(write=True).execute(
                "INSERT INTO source_safety(source_ref,source_revision,state,rules_json,scanned_at) VALUES(?,?,?,?,?)",
                (source.ref, source.revision, "quarantined", "[]", core.clock.utc_now()),
            )
    else:
        private.open_interval(core.storage, core.clock, ctx, target="session", session=source.session_id)
    pending = core.prepare_session_injection(ctx, "")
    text = pending["_injection"]["text"]
    assert item.ref in text and "蓝色" not in text
    assert ("green" in text) if change == "correct" else ("withdrawn" in text)
    assert core.prepare_session_injection(ctx, "")["_injection"]["text"] == text
    assert core.confirm_session_brief(ctx, pending, remaining_seconds=1) == "brief_delivered"
    assert not core.prepare_session_injection(ctx, "")["_injection"]["notices"]


def test_notice_reserve_survives_exhaustion_and_repeated_corrections(app):
    from scope_recall.core.recall_budget import estimate_tokens

    core, ctx = app
    item, _ = initial(core, ctx)
    _deliver_question(core, ctx)
    core.config = replace(core.config, injection_token_cap=300, session_token_cap=0)
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    for revision, value in ((1, "green"), (2, "red")):
        owner.revise(item.ref, expected_revision=revision, value=value)
        prepared = core.prepare_session_injection(ctx, _question_text(core, ctx, f"TEST-change-{revision}"))
        injection = prepared["_injection"]
        assert injection["reason"] == "session_injection_budget_exhausted"
        assert value in injection["text"] and not injection["items"]
        assert estimate_tokens(injection["text"]) <= 300
        assert core.confirm_session_brief(ctx, prepared, remaining_seconds=1) == "brief_delivered"


def test_cached_question_is_fenced_after_correction(app):
    core, ctx = app
    item, _ = initial(core, ctx)
    old = _question_text(core, ctx)
    _deliver_question(core, replace(ctx, session_id="TEST-cache-reader"))
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    owner.revise(item.ref, expected_revision=item.revision, value="green")
    prepared = core.prepare_session_injection(ctx, old)
    assert "蓝色" not in prepared["_injection"]["text"]


def test_concurrent_correction_invalidates_prepared_notice(app):
    core, ctx = app
    item, _ = initial(core, ctx)
    _deliver_question(core, ctx)
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    owner.revise(item.ref, expected_revision=1, value="green")
    prepared = core.prepare_session_injection(ctx, "")
    owner.revise(item.ref, expected_revision=2, value="red")
    assert core.confirm_session_brief(ctx, prepared, remaining_seconds=1) == "brief_invalidated"
    assert "red" in core.prepare_session_injection(ctx, "")["_injection"]["text"]


def test_question_confirmation_rechecks_receiving_session_privacy(app):
    from scope_recall.core import private

    core, writer = app
    initial(core, writer)
    reader = replace(writer, session_id="TEST-private-recipient")
    prepared = core.prepare_session_injection(reader, _question_text(core, reader))
    assert prepared["_injection"]["items"]
    private.open_interval(core.storage, core.clock, writer, target="session", session=reader.session_id)
    assert core.confirm_session_brief(reader, prepared, remaining_seconds=1) == "brief_invalidated"


def _separate_delivery_claims(core, ctx, count):
    from scope_recall.adapters.runtime_wiring import render_host_recall_context
    from v11_support import recall_item, recall_packet

    claims = []
    for n in range(count):
        source = capture(core, ctx, f"TEST-project-{n} 配色选蓝色。")
        claims.append(accept(core, ctx, draft(source, subject=f"TEST-project-{n}")).items[0])
    packet = recall_packet(items=[recall_item(ref=item.ref, revision=item.revision) for item in claims])
    rendered = _render_delivery_packet(core, ctx, packet)
    pending = core.prepare_session_injection(ctx, render_host_recall_context(rendered.canonical_text))
    assert core.confirm_session_brief(ctx, pending, remaining_seconds=1) == "brief_delivered"
    return claims


def test_simultaneous_notices_are_bounded_and_unsent_notices_remain(app):
    from scope_recall.core.recall_budget import estimate_tokens

    core, ctx = app
    claims = _separate_delivery_claims(core, ctx, 4)
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    for item in claims:
        owner.revise(item.ref, expected_revision=1, value="green")
    first = core.prepare_session_injection(ctx, "")
    assert len(first["_injection"]["notices"]) == 3
    assert estimate_tokens(first["_injection"]["text"]) <= 300
    assert core.confirm_session_brief(ctx, first, remaining_seconds=1) == "brief_delivered"
    second = core.prepare_session_injection(ctx, "")
    assert len(second["_injection"]["notices"]) == 1
    assert core.confirm_session_brief(ctx, second, remaining_seconds=1) == "brief_delivered"
    assert not core.prepare_session_injection(ctx, "")["_injection"]["notices"]


def test_notice_text_truncates_new_value_and_retains_reference(app):
    from scope_recall.core.recall_budget import estimate_tokens

    core, ctx = app
    item, _ = initial(core, ctx)
    _deliver_question(core, ctx)
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    owner.revise(item.ref, expected_revision=1, value="green " * 300)
    pending = core.prepare_session_injection(ctx, "")
    assert item.ref in pending["_injection"]["text"]
    assert "green" in pending["_injection"]["text"]
    assert estimate_tokens(pending["_injection"]["text"]) <= 300


def test_busy_notice_confirmation_does_not_acknowledge_delivery(app):
    from concurrent.futures import ThreadPoolExecutor

    core, ctx = app
    item, _ = initial(core, ctx)
    _deliver_question(core, ctx)
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    owner.revise(item.ref, expected_revision=1, value="green")
    pending = core.prepare_session_injection(ctx, "")
    before = core.session_injection_stats(ctx)["total_tokens"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        with core.storage.write(ctx):
            result = pool.submit(core.confirm_session_brief, ctx, pending, remaining_seconds=1).result(timeout=0.4)
            assert result == "injection_delivery_unconfirmed"
    assert core.session_injection_stats(ctx)["total_tokens"] == before
    assert core.prepare_session_injection(ctx, "")["_injection"]["text"] == pending["_injection"]["text"]
    assert core.confirm_session_brief(ctx, pending, remaining_seconds=1) == "brief_delivered"
    assert not core.prepare_session_injection(ctx, "")["_injection"]["text"]


def test_source_and_fact_share_one_correction_notice(app):
    from scope_recall.adapters.runtime_wiring import render_host_recall_context
    from v11_support import recall_item, recall_packet

    core, ctx = app
    item, source = initial(core, ctx)
    packet = recall_packet(
        items=[
            recall_item(ref=item.ref),
            recall_item(
                kind="event", ref=source.ref, content=source.event["content"], evidence_refs=[f"{source.ref}@1"]
            ),
        ]
    )
    rendered = _render_delivery_packet(core, ctx, packet)
    first = core.prepare_session_injection(ctx, render_host_recall_context(rendered.canonical_text))
    assert core.confirm_session_brief(ctx, first, remaining_seconds=1) == "brief_delivered"
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    owner.revise(item.ref, expected_revision=1, value="green")
    update = core.prepare_session_injection(ctx, "")
    assert len(update["_injection"]["notices"]) == 1
    assert core.confirm_session_brief(ctx, update, remaining_seconds=1) == "brief_delivered"
    assert not core.prepare_session_injection(ctx, "")["_injection"]["notices"]


def test_private_recipient_gets_no_new_value_in_correction_notice(app):
    from scope_recall.core import private

    core, writer = app
    item, _ = initial(core, writer)
    reader = replace(writer, session_id="TEST-private-notice-recipient")
    _deliver_question(core, reader)
    owner = MemoryManagement(core, InstallationMaintenance(replace(writer, allowed_scope_ids=writer.binding.scope_ids)))
    owner.revise(item.ref, expected_revision=1, value="green")
    pending = core.prepare_session_injection(reader, "")
    private.open_interval(core.storage, core.clock, writer, target="session", session=reader.session_id)
    assert core.confirm_session_brief(reader, pending, remaining_seconds=1) == "brief_invalidated"
    private_pending = core.prepare_session_injection(reader, "")
    assert "green" not in private_pending["_injection"]["text"]
    assert item.ref in private_pending["_injection"]["text"]
    assert "withdrawn" in private_pending["_injection"]["text"]


def _audit_source_claims(core, ctx):
    source = capture(core, ctx, "TEST-project A OLD_A; TEST-project B OLD_B", when=core.clock.utc_now())
    claims = accept(
        core, ctx, draft(source, value="OLD_A", predicate="A"), draft(source, value="OLD_B", predicate="B")
    ).items
    return source, claims


def _deliver_source_only(core, ctx, source):
    from scope_recall.adapters.runtime_wiring import render_host_recall_context
    from v11_support import recall_item, recall_packet

    packet = recall_packet(
        items=[
            recall_item(
                kind="event",
                ref=source.ref,
                revision=source.revision,
                content=source.event["content"],
                evidence_refs=[f"{source.ref}@1"],
            )
        ]
    )
    rendered = _render_delivery_packet(core, ctx, packet)
    pending = core.prepare_session_injection(ctx, render_host_recall_context(rendered.canonical_text))
    assert core.confirm_session_brief(ctx, pending, remaining_seconds=1) == "brief_delivered"


def test_full_privacy_discards_brief_text_prepared_before_final_assembly(app):
    from scope_recall.core import private

    core, ctx = app
    initial(core, ctx, value="OLD_BRIEF", when=core.clock.utc_now())
    drain_worker(core.storage, core.clock, ctx, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    brief = core.session_brief(ctx, deliver=True, defer_confirmation=True)
    old_text = brief["text"]
    assert "OLD_BRIEF" in old_text
    private.open_interval(core.storage, core.clock, ctx, target="session", session=ctx.session_id)
    pending = core.prepare_session_injection(ctx, old_text, brief=brief)
    assert "OLD_BRIEF" not in pending["_injection"]["text"]
    assert "not being remembered" in pending["_injection"]["text"]
    assert core.confirm_session_brief(ctx, pending, remaining_seconds=1) != "brief_delivered"
    with core.storage.read(ctx) as tx:
        assert not tx._check().execute("SELECT 1 FROM session_brief_injections").fetchone()


@pytest.mark.parametrize("mode", ["suppress", "private"])
def test_withdrawn_original_never_follows_readable_corrected_claim(app, mode):
    from scope_recall.core import private

    core, writer = app
    source, claims = _audit_source_claims(core, writer)
    reader = replace(writer, session_id="TEST-reader-withdrawal")
    _deliver_source_only(core, reader, source)
    owner = MemoryManagement(core, InstallationMaintenance(replace(writer, allowed_scope_ids=writer.binding.scope_ids)))
    for item, value in zip(claims, ("NEW_A", "NEW_B"), strict=True):
        owner.revise(item.ref, expected_revision=1, value=value)
    if mode == "private":
        private.open_interval(core.storage, core.clock, writer, target="session", session=writer.session_id)
    else:
        with core.storage.write(writer) as tx:
            tx._check(write=True).execute("UPDATE source_events SET suppressed=1 WHERE event_id=?", (source.ref,))
    text = core.prepare_session_injection(reader, "")["_injection"]["text"]
    assert source.ref in text and "withdrawn" in text
    assert "NEW_A" not in text and "NEW_B" not in text and "OLD_" not in text


@pytest.mark.parametrize("scenario", ["only_a", "both", "a_then_b"])
def test_source_tracks_each_corrected_fact_independently(app, scenario):
    core, writer = app
    source, claims = _audit_source_claims(core, writer)
    reader = replace(writer, session_id="TEST-reader-multiple")
    _deliver_source_only(core, reader, source)
    owner = MemoryManagement(core, InstallationMaintenance(replace(writer, allowed_scope_ids=writer.binding.scope_ids)))
    owner.revise(claims[0].ref, expected_revision=1, value="NEW_A")
    if scenario == "both":
        owner.revise(claims[1].ref, expected_revision=1, value="NEW_B")
    pending = core.prepare_session_injection(reader, "")
    assert "NEW_A" in pending["_injection"]["text"]
    if scenario == "both":
        assert "NEW_B" in pending["_injection"]["text"]
    assert core.confirm_session_brief(reader, pending, remaining_seconds=1) == "brief_delivered"
    if scenario == "a_then_b":
        owner.revise(claims[1].ref, expected_revision=1, value="NEW_B")
        pending = core.prepare_session_injection(reader, "")
        assert "NEW_B" in pending["_injection"]["text"] and "NEW_A" not in pending["_injection"]["text"]
    else:
        assert not core.prepare_session_injection(reader, "")["_injection"]["notices"]


def test_notice_preparation_reuses_rules_and_confirmation_reads_only_selected_rows(app, monkeypatch):
    from scope_recall.core import private, session_injection

    core, ctx = app
    claims = _separate_delivery_claims(core, ctx, 4)
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    owner.revise(claims[0].ref, expected_revision=1, value="green")
    calls = []
    original = private.active_intervals

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(private, "active_intervals", counted)
    pending = core.prepare_session_injection(ctx, "")
    assert len(calls) <= 2
    scans = []
    original_notices = session_injection._notices

    def scan(*args, **kwargs):
        scans.append(1)
        return original_notices(*args, **kwargs)

    monkeypatch.setattr(session_injection, "_notices", scan)
    assert core.confirm_session_brief(ctx, pending, remaining_seconds=1) == "brief_delivered"
    assert not scans


def test_injection_uses_explicit_shared_brief_interfaces():
    import ast
    import inspect

    from scope_recall.core import session_brief, session_injection

    for name in (
        "session_entry",
        "compaction_cutoff",
        "apply_compaction",
        "clear_session",
        "mark_delivered",
        "record_brief_usage",
        "validate_prepared",
        "visible_object",
    ):
        assert callable(getattr(session_brief, name, None))
    tree = ast.parse(inspect.getsource(session_injection))
    assert not any(
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "briefing"
        and node.attr.startswith("_")
        for node in ast.walk(tree)
    )


def test_compaction_without_sequence_discards_old_notice_targets(app):
    core, ctx = app
    item, _ = initial(core, ctx)
    _deliver_question(core, ctx)
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    owner.revise(item.ref, expected_revision=1, value="green")
    brief = core.session_brief(ctx, token_limit=0, compact=True, deliver=True, defer_confirmation=True)
    pending = core.prepare_session_injection(ctx, brief["text"], brief=brief)
    assert core.confirm_session_brief(ctx, pending, remaining_seconds=1) == "brief_delivered"
    assert not pending["_injection"]["notices"]
    assert core.session_injection_stats(ctx)["total_tokens"] == 0


def test_one_source_defers_each_unconfirmed_fact_beyond_notice_limit(app):
    core, writer = app
    source = capture(core, writer, "; ".join(f"TEST-project {key} OLD_{key}" for key in "ABCD"))
    claims = accept(core, writer, *(draft(source, value=f"OLD_{key}", predicate=key) for key in "ABCD")).items
    reader = replace(writer, session_id="TEST-many-facts")
    _deliver_source_only(core, reader, source)
    owner = MemoryManagement(core, InstallationMaintenance(replace(writer, allowed_scope_ids=writer.binding.scope_ids)))
    for claim, key in zip(claims, "ABCD", strict=True):
        owner.revise(claim.ref, expected_revision=1, value=f"NEW_{key}")
    for count in (3, 1):
        pending = core.prepare_session_injection(reader, "")
        assert len(pending["_injection"]["notices"]) == count
        assert core.confirm_session_brief(reader, pending, remaining_seconds=1) == "brief_delivered"
    assert not core.prepare_session_injection(reader, "")["_injection"]["notices"]


def test_confirmation_does_not_rehydrate_acknowledged_sibling_facts(app, monkeypatch):
    from scope_recall.core import session_injection

    core, writer = app
    source, claims = _audit_source_claims(core, writer)
    reader = replace(writer, session_id="TEST-sibling-reader")
    _deliver_source_only(core, reader, source)
    owner = MemoryManagement(core, InstallationMaintenance(replace(writer, allowed_scope_ids=writer.binding.scope_ids)))
    owner.revise(claims[0].ref, expected_revision=1, value="NEW_A")
    first = core.prepare_session_injection(reader, "")
    assert core.confirm_session_brief(reader, first, remaining_seconds=1) == "brief_delivered"
    owner.revise(claims[1].ref, expected_revision=1, value="NEW_B")
    second = core.prepare_session_injection(reader, "")
    reads = []
    original = session_injection._current

    def observe(tx, search, kind, ref, revision, batch=None):
        reads.append(ref)
        return original(tx, search, kind, ref, revision, batch)

    monkeypatch.setattr(session_injection, "_current", observe)
    assert core.confirm_session_brief(reader, second, remaining_seconds=1) == "brief_delivered"
    assert claims[0].ref not in reads


def test_inline_private_source_is_filtered_before_brief_preparation(app):
    from scope_recall.core.claims import Qualification

    core, ctx = app
    source = capture(
        core,
        ctx,
        "TEST-project \u914d\u8272 \u84dd\u8272\u3002 <private>TEST-brief-hidden</private>",
        when="2026-09-05T12:00:00Z",
    )
    with core.storage.write(ctx) as tx:
        item = tx.claims.append(
            "TEST-scope",
            draft(source, "\u84dd\u8272"),
            Qualification("active", "direct_report", "synthetic"),
            recorded_at=core.clock.utc_now(),
        )
    drain_worker(core.storage, core.clock, ctx, config=WorkerConfig("TEST-brief"), remaining_seconds=30)
    brief = core.session_brief(ctx)
    assert item.ref in brief["text"] and "\u84dd\u8272" in brief["text"]
    assert "TEST-brief-hidden" not in json.dumps(brief)
    with core.storage.read(ctx) as tx:
        saved = tx._check().execute("SELECT items_json FROM session_briefs").fetchall()
    assert "TEST-brief-hidden" not in str(saved)


@pytest.mark.parametrize("change", ["merge", "choose", "evidence"])
@pytest.mark.parametrize("delivered", ["claim", "event"])
def test_health_decisions_notify_replaced_facts_but_not_evidence(app, change, delivered):
    from scope_recall.core.session_brief import mark_delivered

    from tests.contract.test_memory_health import separate_fact

    core, ctx = app
    first = separate_fact(core, ctx, "TEST-Object", "blue")
    second = separate_fact(core, ctx, "test-object", "red" if change == "choose" else "blue")
    old = first if change == "evidence" else second
    span = old.payload["evidence_spans"][0]
    item = (
        dict(kind="claim", ref=old.ref, revision=old.revision)
        if delivered == "claim"
        else dict(kind="event", ref=span["source_ref"], revision=span["source_revision"])
    )
    with core.storage.write(ctx) as tx:
        mark_delivered(tx, ctx, [item], core.clock.utc_now(), 0, "question")
    api = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    if change == "choose":
        api.health_choose_fact(first.ref, first.revision, second.ref, second.revision)
    else:
        api.health_merge_facts(first.ref, first.revision, second.ref, second.revision)
    pending = core.prepare_session_injection(ctx, "")
    notices = pending["_injection"]["notices"]
    if change == "evidence":
        assert not notices
    else:
        assert len(notices) == 1 and notices[0]["corrected"]
        assert "blue" in notices[0]["text"] and item["ref"] in notices[0]["text"]
        assert core.confirm_session_brief(ctx, pending, remaining_seconds=1) == "brief_delivered"
        assert not core.prepare_session_injection(ctx, "")["_injection"]["notices"]


def test_paused_entry_defers_notices_and_usage_until_resumed(app):
    core, ctx = app
    item, _source = initial(core, ctx)
    _deliver_question(core, ctx)
    api = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    before = core.session_injection_stats(ctx)
    api.set_entry_state("local", "paused", actor="TEST-owner")
    api.revise(item.ref, expected_revision=1, value="green")
    pending = core.prepare_session_injection(ctx, "TEST ordinary body")
    assert not pending["_injection"]["text"]
    assert core.confirm_session_brief(ctx, pending, remaining_seconds=1) == "entry_state:paused"
    assert core.session_injection_stats(ctx) == before
    api.set_entry_state("local", "normal", actor="TEST-owner")
    resumed = core.prepare_session_injection(ctx, "")
    assert "green" in resumed["_injection"]["text"]
    assert core.confirm_session_brief(ctx, resumed, remaining_seconds=1) == "brief_delivered"


def test_entry_pause_after_injection_preparation_prevents_accounting(app):
    core, ctx = app
    initial(core, ctx)
    pending = core.prepare_session_injection(ctx, _question_text(core, ctx))
    before = core.session_injection_stats(ctx)
    api = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    api.set_entry_state("local", "paused", actor="TEST-owner")
    api.set_entry_state("local", "normal", actor="TEST-owner")
    assert core.confirm_session_brief(ctx, pending, remaining_seconds=1) == "entry_state:paused"
    assert core.session_injection_stats(ctx) == before


def test_acknowledged_correction_does_not_repeat_after_evidence_merge(app):
    from scope_recall.core.session_brief import mark_delivered

    from tests.contract.test_memory_health import separate_fact

    core, ctx = app
    first = separate_fact(core, ctx, "TEST-Object", "blue")
    with core.storage.write(ctx) as tx:
        mark_delivered(tx, ctx, [dict(kind="claim", ref=first.ref, revision=1)], core.clock.utc_now(), 0, "question")
    api = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    revised = api.revise(first.ref, expected_revision=1, value="green")
    pending = core.prepare_session_injection(ctx, "")
    assert core.confirm_session_brief(ctx, pending, remaining_seconds=1) == "brief_delivered"
    second = separate_fact(
        core,
        ctx,
        "test-object",
        "green",
        valid_from=core.current_claim(ctx, first.ref).valid_from,
        valid_to=core.current_claim(ctx, first.ref).valid_to,
    )
    api.health_merge_facts(first.ref, revised["item"]["revision"], second.ref, second.revision)
    assert not core.prepare_session_injection(ctx, "")["_injection"]["notices"]


def test_paused_entry_does_not_commit_pending_compaction(app):
    core, ctx = app
    initial(core, ctx)
    _deliver_question(core, ctx)
    before = core.session_injection_stats(ctx)
    api = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    api.set_entry_state("local", "paused", actor="TEST-owner")
    compact = replace(ctx, compaction_seq=1, compaction_epoch="TEST-epoch", compacted_at=core.clock.utc_now())
    assert not core.sync_session_compaction(compact)
    assert core.session_injection_stats(ctx) == before


def test_distinct_health_replacements_keep_separate_old_references(app):
    from scope_recall.core.session_brief import mark_delivered

    from tests.contract.test_memory_health import separate_fact

    core, ctx = app
    kept = separate_fact(core, ctx, "TEST-Object", "blue")
    dropped = [separate_fact(core, ctx, name, "blue") for name in ("test-object", "Test-object")]
    with core.storage.write(ctx) as tx:
        mark_delivered(
            tx,
            ctx,
            [dict(kind="claim", ref=item.ref, revision=1) for item in dropped],
            core.clock.utc_now(),
            0,
            "question",
        )
    api = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    revision = kept.revision
    for item in dropped:
        receipt = api.health_merge_facts(kept.ref, revision, item.ref, item.revision)
        revision = receipt["versions"][kept.ref]
    pending = core.prepare_session_injection(ctx, "")
    assert len(pending["_injection"]["notices"]) == 2
    assert all(item.ref in pending["_injection"]["text"] for item in dropped)
    assert core.confirm_session_brief(ctx, pending, remaining_seconds=1) == "brief_delivered"
    assert not core.prepare_session_injection(ctx, "")["_injection"]["notices"]
