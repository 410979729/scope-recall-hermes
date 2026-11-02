"""Name-level work, indexed nomination and the default runtime identity boundary."""

import json
from dataclasses import replace
from difflib import SequenceMatcher

import pytest
from scope_recall.core.claims import Qualification
from scope_recall.core.entity_resolution import queue_entity_backfill
from scope_recall.core.read_views import resolve_subject
from scope_recall.runtime.instance import build_runtime_instance
from scope_recall.runtime.instance_config import RuntimeInstanceConfig

from tests.contract.test_aliases import _alias, _identity, app
from tests.contract.test_claims import accept, capture, draft
from tests.contract.test_entity_resolution import RecordedModel, drain_entities, fact, merge, withdraw


def named_fact(core, ctx, name, predicate):
    source = capture(core, ctx, f"{name} {predicate} TEST-value.")
    receipt = accept(
        core,
        ctx,
        draft(
            source,
            "TEST-value",
            kind="fact",
            subject=name,
            predicate=predicate,
            statement_kind="assertion",
            entity_names=[name],
        ),
    )
    assert receipt.items[0].state == "active"
    return core.current_claim(ctx, receipt.items[0].ref)


def test_candidate_scoring_is_bounded_before_python_similarity(app, monkeypatch):
    import difflib

    core, ctx = app
    for number in range(180):
        fact(core, ctx, f"TEST-person-{number:03d}", "category", "person")
    calls = []

    def score(*args, **kwargs):
        calls.append(1)
        return SequenceMatcher(*args, **kwargs)

    monkeypatch.setattr(difflib, "SequenceMatcher", score)
    monkeypatch.setattr("scope_recall.core.entity_names.SequenceMatcher", score)
    with core.storage.read(ctx) as tx:
        assert not tx.claims._entity_lookup("TEST-scope", "𓀀𓀁𓀂𓀃", (), "")
        assert tx.claims._entity_lookup("TEST-scope", "TEST-person-999", (), "")
    assert 1 <= len(calls) <= 128


def test_candidates_rank_nearer_names_before_early_repeated_subjects(app):
    core, ctx = app
    for number in range(24):
        fact(core, ctx, "TEST-Alexander", f"tag{number}", "person")
    target = fact(core, ctx, "TEST-Alexandria", "category", "person")
    with core.storage.read(ctx) as tx:
        refs = tx.claims._entity_lookup("TEST-scope", "TEST-Alexandriya", (), "")
        subjects = [tx.claims.version(ref, tx.claims.current_revision(ref)).payload["subject"] for ref in refs]
    assert refs[0] == target.ref
    assert len(subjects) == len(set(subjects))


def test_followup_facts_with_checked_names_do_not_call_the_model(app):
    core, ctx = app
    named_fact(core, ctx, "TEST-Ricecake", "home")
    model = RecordedModel(False)
    drain_entities(core, ctx, model)
    assert len(model.calls) == 1
    named_fact(core, ctx, "TEST-Ricecake", "color")
    drain_entities(core, ctx, model)
    assert len(model.calls) == 1


def test_one_name_keeps_one_alias_when_another_fact_supplies_the_evidence(app):
    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "category", "cat")
    first = fact(core, ctx, "TEST-Ricecake", "home", "TEST-Harbor")
    second = fact(core, ctx, "TEST-Ricecake", "color", "TEST-white")
    left, right = merge(core, ctx, first, target), merge(core, ctx, second, target)
    assert right is None or right.ref == left.ref
    with core.storage.read(ctx) as tx:
        assert len(tx.claims.list_refs(alias_name="TEST-Ricecake")) == 1


def revise_endpoint(core, ctx, target):
    with core.storage.write(ctx) as tx:
        return tx.claims.append(
            target.scope_id,
            target.payload,
            Qualification("active", "direct_report", "supported"),
            recorded_at=core.clock.utc_now(),
            previous=target,
        )


def test_invalidated_name_reenters_bounded_work_but_withdrawn_relation_does_not(app):
    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "category", "cat")
    source = fact(
        core, ctx, "TEST-Ricecake", "home", "TEST-Harbor", "TEST-Ricecake is TEST-Nian. TEST-Ricecake home TEST-Harbor."
    )
    model = RecordedModel()
    drain_entities(core, ctx, model)
    target = revise_endpoint(core, ctx, target)
    with core.storage.write(ctx) as tx:
        queue_entity_backfill(tx, now=core.clock.utc_now())
        state = (
            tx._check()
            .execute(
                "SELECT state FROM work_items WHERE work_type='rebuild_projection' "
                "AND subject_ref=? AND subject_revision=?",
                (source.ref, source.revision),
            )
            .fetchone()[0]
        )
    assert state == "pending"
    drain_entities(core, ctx, model)
    with core.storage.read(ctx) as tx:
        resolution = resolve_subject(tx, "TEST-Ricecake", core.clock.utc_now())
        assert resolution.subject == "TEST-Nian"
        alias = next(
            tx.claims.version(item.ref, item.revision)
            for item in resolution.fence
            if tx.claims.version(item.ref, item.revision).payload["kind"] == "alias"
        )
    withdraw(core, ctx, alias)
    revise_endpoint(core, ctx, target)
    with core.storage.write(ctx) as tx:
        queue_entity_backfill(tx, now=core.clock.utc_now())
        state = (
            tx._check()
            .execute(
                "SELECT state FROM work_items WHERE work_type='rebuild_projection' "
                "AND subject_ref=? AND subject_revision=?",
                (source.ref, source.revision),
            )
            .fetchone()[0]
        )
    assert state == "done"
    drain_entities(core, ctx, model)
    with core.storage.read(ctx) as tx:
        assert resolve_subject(tx, "TEST-Ricecake", core.clock.utc_now()).status == "literal"


def test_query_name_lookup_does_not_apply_instr_to_stored_rows(app):
    core, ctx = app
    target = _identity(core, ctx)
    source = capture(core, ctx, "TEST-mist 项目改名为 TEST暮光，保留旧称。")
    assert accept(core, ctx, _alias(source, target, "TEST暮光")).items[0].state == "active"
    with core.storage.read(ctx) as tx:

        def reject_scan(*_args):
            raise AssertionError("stored-row substring scan")

        tx._check().create_function("instr", 2, reject_scan)
        assert tx.claims.aliases_for_query("TEST暮光 配色")
        assert tx.claims.entity_names_for_query("TEST-mist 配色", matches=("TEST-mist",))


def test_default_runtime_drains_entity_judgements_through_the_clamped_port(app):
    core, ctx = app
    fact(core, ctx, "TEST-Nian", "category", "cat")
    fact(
        core, ctx, "TEST-Ricecake", "home", "TEST-Harbor", "TEST-Ricecake is TEST-Nian. TEST-Ricecake home TEST-Harbor."
    )
    with core.storage.write(ctx) as tx:
        tx._check(write=True).execute("UPDATE work_items SET state='done'")

    class RecordingPort:
        calls = []

        def propose(self, messages, *, remaining_seconds):
            self.calls.append(remaining_seconds)
            body = json.loads(messages[-1]["content"])
            target = next(item for item in body["candidates"] if item["payload"]["subject"] == "TEST-Nian")
            return json.dumps(
                dict(
                    support="supported",
                    action="duplicate",
                    target_ref=target["ref"],
                    target_revision=target["revision"],
                )
            )

    port = RecordingPort()
    config = RuntimeInstanceConfig(
        ctx.binding,
        ctx.session_id,
        ctx.allowed_scope_ids,
        project_id=ctx.project_id,
        branch_id=ctx.branch_id,
        request_seconds=0.25,
        drain_seconds=20,
        supervisor_enabled=False,
    )
    instance = build_runtime_instance(config, consolidation=port)
    try:
        receipt = instance.drain(remaining_seconds=20)
        assert receipt.failed == 0
        assert port.calls and max(port.calls) <= 0.25
        with core.storage.read(ctx) as tx:
            assert tx._check().execute("SELECT count(*) FROM entity_alias_proofs").fetchone()[0] == 1
    finally:
        instance.close()


def test_two_fact_leases_cannot_judge_the_same_pending_name(app):
    from scope_recall.core.worker_projection import process_rebuild_projection

    core, ctx = app
    named_fact(core, ctx, "TEST-Ricecake", "home")
    named_fact(core, ctx, "TEST-Ricecake", "color")
    with core.storage.write(ctx) as tx:
        tx._check(write=True).execute("UPDATE work_items SET state='done'")
        queue_entity_backfill(tx, now=core.clock.utc_now())
        jobs = tx.work.claim_next(
            "TEST-name-owner",
            core.clock.utc_now(),
            lease_seconds=60,
            limit=2,
            allowed_work_types=frozenset({"rebuild_projection"}),
        )
    nested = []

    class ReentrantModel(RecordedModel):
        def judge(self, messages, **kwargs):
            answer = super().judge(messages, **kwargs)
            if len(self.calls) == 1:
                nested.append(
                    process_rebuild_projection(
                        core.storage, core.clock, ctx, jobs[1], model=self, started=core.clock.monotonic(), budget=10
                    )
                )
            return answer

    model = ReentrantModel(False)
    result = process_rebuild_projection(
        core.storage, core.clock, ctx, jobs[0], model=model, started=core.clock.monotonic(), budget=10
    )
    assert result[2] == "done"
    assert nested == [("deferred", "entity_name_leased", "pending")]
    assert len(model.calls) == 1
    with core.storage.read(ctx) as tx:
        assert (
            tx._check().execute("SELECT attempt FROM work_items WHERE work_id=?", (jobs[1].work_id,)).fetchone()[0] == 0
        )


def test_candidate_evidence_matches_a_name_before_sentence_punctuation(app):
    core, ctx = app
    target = fact(core, ctx, "TEST-mapzjirkrhee", "category", "person")
    with core.storage.read(ctx) as tx:
        assert target.ref in tx.claims._entity_lookup("TEST-scope", "TEST-称呼", (), "TEST-称呼 is TEST-mapzjirkrhee.")


def test_deleted_alias_endpoint_returns_the_surviving_name_to_pending(app):
    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "category", "cat")
    source = named_fact(core, ctx, "TEST-Ricecake", "home")
    merge(core, ctx, source, target)
    capture(core, ctx, f"删除 {target.ref}。")
    core.forget(
        ctx,
        dict(
            protocol_version="1.1",
            target_refs=[target.ref],
            mode="delete",
            expected_revisions={target.ref: target.revision},
        ),
        remaining_seconds=10,
    )
    with core.storage.write(ctx) as tx:
        assert (
            tx._check().execute("SELECT status FROM entity_names WHERE name='TEST-Ricecake'").fetchone()[0] == "pending"
        )
        queue_entity_backfill(tx, now=core.clock.utc_now())
        assert (
            tx._check()
            .execute(
                "SELECT entity_resolved FROM claim_versions WHERE claim_id=? AND revision=?",
                (source.ref, source.revision),
            )
            .fetchone()[0]
            == 0
        )
    model = RecordedModel(False)
    drain_entities(core, ctx, model)
    assert len(model.calls) == 1


def test_alias_graph_joins_use_identity_keys_with_stale_namespace_statistics(app):
    core, ctx = app
    target = _identity(core, ctx)
    source = capture(core, ctx, "TEST-mist 项目改名为 TEST暮光，保留旧称。")
    assert accept(core, ctx, _alias(source, target, "TEST暮光")).items[0].state == "active"
    with core.storage.write(ctx) as tx:
        conn = tx._check(write=True)
        conn.execute("ANALYZE")
        conn.execute("UPDATE sqlite_stat1 SET stat='100000 1 1 1 1' WHERE idx='entity_alias_scope'")
        conn.execute("ANALYZE sqlite_schema")
        statements = []
        conn.set_trace_callback(statements.append)
        assert tx.claims.list_refs(alias_name="TEST暮光")
        conn.set_trace_callback(None)
        plans = [
            tuple(row)
            for sql in statements
            if sql.startswith("SELECT DISTINCT c.claim_id")
            for row in conn.execute("EXPLAIN QUERY PLAN " + sql)
        ]
        assert any("c USING INDEX" in row[3] and "(claim_id=?)" in row[3] for row in plans), plans
