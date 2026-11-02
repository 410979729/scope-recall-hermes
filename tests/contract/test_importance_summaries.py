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
        assert already_injected(tx, "event", source.ref, source.revision)
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


def test_large_source_is_covered_by_consecutive_summary_fragments(app):
    import json

    class Excerpts:
        def judge(self, messages, *, remaining_seconds):
            body = json.loads(messages[-1]["content"])
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
    result = build(core, ctx, Excerpts())
    assert result.failed == 0
    with core.storage.read(ctx) as tx:
        group = tx._check().execute("SELECT * FROM summary_groups WHERE kind='session'").fetchone()
        assert group["covered_units"] == group["total_units"] == len(source.event["content"])
        parts = tx._check().execute("SELECT * FROM summary_parts ORDER BY ordinal").fetchall()
        assert len(parts) > 1
        assert all(summary_live(tx, p["claim_ref"], p["claim_revision"]) for p in parts)


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
