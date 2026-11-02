"""Adopted decisions share ownership, salience and display rules with merged features."""

import json
from dataclasses import replace

import pytest
from scope_recall.core.adoption_storage import adoption_context
from scope_recall.core.management import MemoryManagement
from scope_recall.core.recall_restatement import redundant_summary_keys
from scope_recall.core.restore import InstallationMaintenance
from scope_recall.core.retrieval import CandidateRef, RetrievalResult, RetrievedObject, SearchContext
from scope_recall.core.worker_adoption import prepare_adoptions
from v11_support import recall_request

from tests.contract.test_adoption import assistant, prepared, publish
from tests.contract.test_adoption_revision import reply
from tests.contract.test_claims import app, capture


def adopted(core, ctx):
    plan = assistant(core, ctx)
    owner = capture(core, ctx, "好，就用方案二")
    items, _ = prepared(core, ctx, owner)
    return plan, owner, publish(core, ctx, items[0])


def test_adoption_requires_public_owner_resolution(app):
    core, ctx = app
    assistant(core, ctx)
    owner = capture(core, ctx, "好，就用方案二")
    denied = replace(ctx, owner_resolver=lambda entry, principal, memo: None)
    with core.storage.read(denied) as tx:
        assert adoption_context(tx, owner) is None


def test_adoption_importance_uses_decision_rules_and_owner_override(app):
    core, ctx = app
    assistant(core, ctx)
    owner = capture(core, ctx, "好，就用方案二")
    base = reply()

    def judge(messages, **kwargs):
        body = json.loads(messages[-1]["content"])
        if body.get("task") == "describe_adoption":
            assert "4为明确决定" in messages[0]["content"]
            return json.dumps(
                dict(topic="配置重启策略", decision="先备份配置，再平滑重启", importance=4), ensure_ascii=False
            )
        return base(messages, **kwargs)

    item = prepare_adoptions(core.storage, ctx, (owner,), judge, clock=core.clock, remaining_seconds=10)[0]
    mutation = publish(core, ctx, item)
    claim = core.current_claim(ctx, mutation.ref)
    assert claim.importance == 4
    management = MemoryManagement(core, InstallationMaintenance(ctx))
    management.importance(claim.ref, expected_revision=claim.revision, importance=2, pinned=False)
    assert core.current_claim(ctx, claim.ref).importance == 2


def displayed(ref, content, reason, kind="decision", quote="先备份配置。"):
    payload = dict(kind=kind, evidence_spans=[dict(source_ref="TEST-source", source_revision=1, quote=quote)])
    return RetrievedObject(
        ref,
        1,
        "claim",
        content,
        "human_direct",
        "current",
        "",
        ("TEST-source@1",),
        "direct_report",
        False,
        metadata=(("qualification_reason", reason), ("payload_json", json.dumps(payload, ensure_ascii=False))),
    )


@pytest.mark.parametrize("reason", ["owner_adopted_assistant_proposal", "owner_decision"])
def test_summary_deduplicates_only_quotes_visible_in_same_source_decision(reason):
    decision = displayed("TEST-decision", "主人决定：先备份配置。", reason)
    summary = displayed("TEST-summary", "assistant: 先备份配置。", "grounded_session_summary", kind="fact")
    assert redundant_summary_keys([decision, summary]) == {("claim", "TEST-summary", 1)}
    assert redundant_summary_keys([replace(decision, content="主人决定采用方案二。"), summary]) == set()
    assert (
        redundant_summary_keys(
            [
                decision,
                displayed(
                    "TEST-summary",
                    "extra",
                    "grounded_session_summary",
                    kind="fact",
                    quote="先备份配置。还有一次独立核验。",
                ),
            ]
        )
        == set()
    )


def test_overflow_never_expands_adopted_plan_to_unselected_original(app):
    from scope_recall.core.recall_overflow import _qualified, _representatives

    core, ctx = app
    plan, owner, claim = adopted(core, ctx)
    reader = core.recall_pipeline.storage_reader
    search = SearchContext.from_request(
        recall_request(query="配置方案"),
        replace(ctx, session_id="TEST-reader"),
        now=core.clock.utc_now(),
        deadline=core.clock.monotonic() + 10,
    )
    candidates = (
        CandidateRef("event", plan.ref, 1, "lexical"),
        CandidateRef("claim", claim.ref, claim.revision, "lexical"),
    )
    with core.storage.read(ctx) as tx:
        reader.prefetch(tx, candidates, search)
        decision = reader.hydrate(tx, candidates[1], search)
        assert not _qualified(decision)
        result = RetrievalResult(candidates, (), None, overflow_candidates=candidates)
        assert not _representatives(tx, reader, result, search, {"items": []}, core.clock)


@pytest.mark.parametrize("held", ["assistant", "owner"])
@pytest.mark.parametrize("state", ["quarantined", "pending_review"])
def test_held_adoption_sources_never_reach_model(app, held, state):
    from scope_recall.contracts import ContractError

    core, ctx = app
    plan = assistant(core, ctx)
    owner = capture(core, ctx, "好，就用方案二")
    source = plan if held == "assistant" else owner
    with core.storage.write(ctx) as tx:
        tx._check().execute(
            "INSERT OR REPLACE INTO source_safety VALUES(?,?,?,?,?,NULL)",
            (source.ref, 1, state, '["TEST-held"]', core.clock.utc_now()),
        )
    calls = []
    try:
        result = prepare_adoptions(
            core.storage, ctx, (owner,), lambda *a, **k: calls.append(a), clock=core.clock, remaining_seconds=10
        )
        assert result == ()
    except ContractError:
        pass
    assert calls == []


def test_summary_can_quote_adoption_evidence_without_overriding_decision(app):
    from scope_recall.core.adoption import fragments

    from tests.contract.test_importance_summaries import build

    core, ctx = app
    plan, owner, mutation = adopted(core, ctx)

    class Summary:
        def judge(self, messages, **kwargs):
            body = json.loads(messages[-1]["content"])
            return json.dumps(
                dict(
                    sentences=[
                        dict(
                            text=fragments(s["content"])[1]["text"] if s["role"] == "assistant" else s["content"],
                            source_ref=s["ref"],
                            source_revision=s["revision"],
                        )
                        for s in body["sources"]
                    ]
                ),
                ensure_ascii=False,
            )

    result = build(core, ctx, Summary())
    assert result.failed == 0
    with core.storage.read(ctx) as tx:
        rows = tx._check().execute("SELECT claim_ref,claim_revision FROM summary_parts").fetchall()
        assert rows
        summaries = [tx.claims.version(*row) for row in rows]
        assert all(s.basis == "derived_summary" for s in summaries)
        spans = [p for s in summaries for p in s.payload["evidence_spans"]]
        assert {p["source_ref"] for p in spans} == {plan.ref, owner.ref}
        assert any("先备份配置" in p["quote"] for p in spans)
        assert not any("直接覆盖" in p["quote"] for p in spans)
    assert core.current_claim(ctx, mutation.ref).reason == "owner_adopted_assistant_proposal"


def test_shared_cli_allowance_defers_summary_and_adoption_to_next_pass(app):
    from scope_recall.core.summary_storage import close_session
    from scope_recall.core.worker import WorkerConfig, drain_worker
    from scope_recall.core.worker_consolidation import build_consolidation_model
    from scope_recall.runtime.models import AuxiliaryModelError

    core, ctx = app
    assistant(core, ctx)
    capture(core, ctx, "好，就用方案二")
    with core.storage.write(ctx) as tx:
        close_session(tx, ctx.session_id, now=core.clock.utc_now())
        tx._check().execute("UPDATE work_items SET state='done' WHERE work_type IN ('embed','rebuild_projection')")
    log = []

    class Port:
        max_calls_per_pass = 1

        def __init__(self):
            self.used = 0

        def propose(self, messages, **kwargs):
            if self.used:
                raise AuxiliaryModelError("budget_exhausted")
            self.used += 1
            body = json.loads(messages[-1]["content"])
            task = body.get("task")
            log.append(
                task
                if isinstance(task, str)
                else "ordinary"
                if "source_refs" in body
                else "support"
                if "proposal" in body
                else "summary"
            )
            if isinstance(task, str):
                return reply()(messages, **kwargs)
            if "source_refs" in body:
                return json.dumps(
                    dict(
                        protocol_version="1.1",
                        source_refs=body["source_refs"],
                        claim_proposals=[],
                        resume_proposals=[],
                        reference_proposals=[],
                    )
                )
            if "proposal" in body:
                return json.dumps(dict(support="supported", action="new", target_ref=None, target_revision=None))
            if "sources" in body:
                return json.dumps(
                    dict(
                        sentences=[
                            dict(text=s["content"], source_ref=s["ref"], source_revision=s["revision"])
                            for s in body["sources"]
                        ]
                    )
                )
            raise AssertionError("unexpected model request")

    def step():
        port = Port()
        drain_worker(
            core.storage,
            core.clock,
            ctx,
            config=WorkerConfig("TEST-shared-budget", max_items=16, summary_capable=True),
            consolidation=build_consolidation_model(port),
            remaining_seconds=30,
        )
        assert port.used <= 1

    step()
    with core.storage.read(ctx) as tx:
        rows = (
            tx._check()
            .execute(
                "SELECT w.available_at,w.last_error_code FROM work_items w JOIN summary_parts p "
                "ON p.claim_ref=w.subject_ref WHERE w.state='pending'"
            )
            .fetchall()
        )
        assert rows and all(row[0] <= core.clock.utc_now() and row[1] == "pass_call_limit" for row in rows)
    for _ in range(10):
        step()
    assert log.count("ordinary") == 1 and log.count("locate_adoption") == 1 and log.count("describe_adoption") == 1
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT 1 FROM adoption_relations WHERE state='bound'").fetchone()
        assert tx._check().execute("SELECT 1 FROM summary_groups WHERE state='complete'").fetchone()
