"""Synthetic entity identities, reversible aliases and partition boundaries."""

import json
from dataclasses import replace

import pytest
from scope_recall.core.entity_queries import resolved_query
from scope_recall.core.entity_resolution import entity_progress, publish_entity_merge, queue_entity_backfill
from scope_recall.core.evidence_support import SupportVerdict
from scope_recall.core.read_views import resolve_subject
from scope_recall.core.worker import WorkerConfig, drain_worker

from tests.contract.test_aliases import app
from tests.contract.test_claims import accept, capture, draft


def fact(core, ctx, name, predicate, value, text=None):
    source = capture(core, ctx, text or f"{name} {predicate} {value}.")
    item = accept(
        core, ctx, draft(source, value, kind="fact", statement_kind="assertion", subject=name, predicate=predicate)
    )
    return core.current_claim(ctx, item.items[0].ref)


def merge(core, ctx, source, target):
    decision = SupportVerdict("", "supported", target.ref, target.revision, "duplicate")
    with core.storage.write(ctx) as tx:
        return publish_entity_merge(tx, source, target, decision, now=core.clock.utc_now())


def test_entity_merge_is_a_sourced_alias_and_does_not_rewrite_facts(app):
    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "species", "cat")
    source = fact(
        core,
        ctx,
        "TEST-Ricecake",
        "home",
        "TEST-Harbor",
        "TEST-Ricecake is TEST-Nian, the cat. TEST-Ricecake home TEST-Harbor.",
    )
    alias = merge(core, ctx, source, target)
    assert alias.payload["kind"] == "alias"
    assert alias.payload["alias"]["target_ref"] == target.ref
    assert core.current_claim(ctx, source.ref).payload == source.payload
    with core.storage.read(ctx) as tx:
        assert resolve_subject(tx, source.payload["subject"], core.clock.utc_now()).subject == target.payload["subject"]


@pytest.mark.parametrize(
    "field,value", [("scope_id", "TEST-other"), ("project_id", "TEST-other"), ("branch_id", "TEST-other")]
)
def test_entity_merge_rejects_a_different_partition(app, field, value):
    from scope_recall.contracts import ContractError

    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "species", "cat")
    source = fact(core, ctx, "TEST-Ricecake", "home", "TEST-Harbor")
    with pytest.raises(ContractError):
        merge(core, ctx, source, replace(target, **{field: value}))


def view(core, ctx, name):
    result = core.entity(ctx, dict(protocol_version="1.1", request_id="TEST-entity", subject=name, action="related"))
    return [(s["ref"], s["subject"], s["predicate"], s["value_text"]) for s in result["statements"]]


def withdraw(core, ctx, alias):
    from scope_recall.core.claims import Qualification

    with core.storage.write(ctx) as tx:
        tx.claims.append(
            alias.scope_id,
            alias.payload,
            Qualification("retracted", "direct_report", "user_withdrawal"),
            recorded_at=core.clock.utc_now(),
            previous=alias,
        )


def test_retraction_restores_entity_and_query_mapping_exactly(app):
    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "species", "cat")
    source = fact(core, ctx, "TEST-Ricecake", "home", "TEST-Harbor")
    before = view(core, ctx, "TEST-Ricecake")
    alias = merge(core, ctx, source, target)
    assert len(view(core, ctx, "TEST-Ricecake")) == 2
    with core.storage.read(ctx) as tx:
        assert resolved_query(tx, "Where is TEST-Ricecake?", core.clock.utc_now()) == "Where is TEST-Nian?"
    withdraw(core, ctx, alias)
    assert view(core, ctx, "TEST-Ricecake") == before
    with core.storage.read(ctx) as tx:
        assert resolved_query(tx, "Where is TEST-Ricecake?", core.clock.utc_now()) == "Where is TEST-Ricecake?"
    assert merge(core, ctx, source, target) is None


def test_withdrawn_endpoint_disables_its_entity_alias(app):
    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "species", "cat")
    source = fact(core, ctx, "TEST-Ricecake", "home", "TEST-Harbor")
    merge(core, ctx, source, target)
    withdraw(core, ctx, source)
    with core.storage.read(ctx) as tx:
        assert resolve_subject(tx, "TEST-Ricecake", core.clock.utc_now()).status == "literal"


class RecordedModel:
    def __init__(self, same=True):
        self.same = same
        self.calls = []

    def judge(self, messages, **kwargs):
        body = json.loads(messages[-1]["content"])
        self.calls.append(body)
        target = body["candidates"][0] if self.same else None
        return json.dumps(
            dict(
                support="supported" if target else "uncertain",
                action="duplicate" if target else "new",
                target_ref=target["ref"] if target else None,
                target_revision=target["revision"] if target else None,
            )
        )


def drain_entities(core, ctx, model):
    with core.storage.write(ctx) as tx:
        tx._check(write=True).execute("UPDATE work_items SET state='done'")
        queue_entity_backfill(tx, now=core.clock.utc_now())
    return drain_worker(
        core.storage,
        core.clock,
        ctx,
        config=WorkerConfig("TEST-entities", max_items=8),
        consolidation=model,
        remaining_seconds=20,
    )


@pytest.mark.parametrize("same", [True, False])
def test_worker_backfills_with_recorded_identity_answers(app, same):
    core, ctx = app
    fact(core, ctx, "TEST-Nian", "species", "cat")
    fact(
        core, ctx, "TEST-Ricecake", "home", "TEST-Harbor", "TEST-Ricecake is TEST-Nian. TEST-Ricecake home TEST-Harbor."
    )
    model = RecordedModel(same)
    receipt = drain_entities(core, ctx, model)
    assert receipt.failed == 0
    assert len(model.calls) == 1
    with core.storage.read(ctx) as tx:
        assert entity_progress(tx) == dict(total=2, completed=2, pending=0)
        assert tx._check().execute("SELECT count(*) FROM entity_alias_proofs").fetchone()[0] == int(same)


def test_same_name_different_people_and_spelling_only_never_auto_merge(app):
    core, ctx = app
    fact(core, ctx, "TEST-Alex Lee (teacher)", "workplace", "TEST-School")
    fact(core, ctx, "TEST-Alex Lee (architect)", "workplace", "TEST-Studio")
    drain_entities(core, ctx, RecordedModel(False))
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM entity_alias_proofs").fetchone()[0] == 0


def test_backfill_waits_while_a_candidate_evaluation_is_ready(app):
    core, ctx = app
    item = fact(core, ctx, "TEST-Person-waiting", "workplace", "TEST-School")
    ready = frozenset({"evaluate_candidate"})
    now = core.clock.utc_now()
    with core.storage.write(ctx) as tx:
        conn = tx._check(write=True)
        conn.execute("UPDATE work_items SET state='done'")
        conn.execute(
            "INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,project_id,branch_id,available_at) "
            "SELECT 'evaluate_candidate','TEST-candidate',1,scope_id,project_id,branch_id,? "
            "FROM claims WHERE claim_id=?",
            (now, item.ref),
        )
        assert queue_entity_backfill(tx, now=now, yield_to=ready) == 0
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='evaluate_candidate'")
        assert queue_entity_backfill(tx, now=now, yield_to=ready) == 1


def test_backfill_is_bounded_and_literal_reads_work_without_a_model(app):
    core, ctx = app
    for n in range(5):
        fact(core, ctx, f"TEST-Person-{n}", "workplace", f"TEST-School-{n}")
    with core.storage.write(ctx) as tx:
        tx._check(write=True).execute("UPDATE work_items SET state='done'")
        assert queue_entity_backfill(tx, now=core.clock.utc_now(), limit=2) == 2
    assert view(core, ctx, "TEST-Person-3")
    with core.storage.read(ctx) as tx:
        assert entity_progress(tx)["pending"] == 5


def test_canonical_extraction_preserves_evidenced_surface_names(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-Nian, also called TEST-Ricecake, species cat.")
    item = accept(
        core,
        ctx,
        draft(
            source,
            "cat",
            kind="fact",
            statement_kind="assertion",
            subject="TEST-Nian",
            predicate="species",
            entity_type="object",
            entity_names=["TEST-Ricecake"],
        ),
    )
    assert item.items[0].state == "active"
    drain_entities(core, ctx, RecordedModel())
    with core.storage.read(ctx) as tx:
        assert resolve_subject(tx, "TEST-Ricecake", core.clock.utc_now()).subject == "TEST-Nian"
        assert (
            resolved_query(tx, "prefixTEST-Ricecake TEST-Ricecake", core.clock.utc_now())
            == "prefixTEST-Ricecake TEST-Nian"
        )


def test_vector_candidates_use_the_existing_search_port_and_recheck_audience(app):
    from scope_recall.core.retrieval import CandidateRef

    core, ctx = app
    target = fact(core, ctx, "TEST-Cedar", "identifier", "TEST-501")
    fact(core, ctx, "TEST-Acorn", "identifier", "TEST-501")

    class Vectors:
        calls = 0

        def search(self, context, *, limit, remaining_seconds):
            self.calls += 1
            assert context.trusted_context.project_id == ctx.project_id
            return (CandidateRef("claim", target.ref, target.revision, "vector", 1, vector_score=0.99),)

    vectors = Vectors()
    core.vectors = vectors
    drain_entities(core, ctx, RecordedModel())
    assert vectors.calls == 2
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM entity_alias_proofs").fetchone()[0] == 1


@pytest.mark.parametrize("field", ["project_id", "branch_id"])
def test_actual_other_audience_never_enters_model_candidates(app, field):
    core, ctx = app
    other = replace(ctx, **{field: "TEST-other"})
    fact(core, other, "TEST-Nian", "species", "cat")
    fact(core, ctx, "TEST-Ricecake", "species", "cat", "TEST-Ricecake is TEST-Nian. TEST-Ricecake species cat.")
    model = RecordedModel()
    drain_entities(core, ctx, model)
    assert not model.calls
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM entity_alias_proofs").fetchone()[0] == 0


def test_model_budget_refusal_leaves_backfill_pending_without_spending_an_attempt(app):
    core, ctx = app
    fact(core, ctx, "TEST-Nian", "species", "cat")
    source = fact(
        core, ctx, "TEST-Ricecake", "home", "TEST-Harbor", "TEST-Ricecake is TEST-Nian. TEST-Ricecake home TEST-Harbor."
    )

    class Refusal(RuntimeError):
        error_type = "budget_exhausted"

    class Budgeted:
        def judge(self, messages, **kwargs):
            raise Refusal("TEST budget exhausted before transport")

    receipt = drain_entities(core, ctx, Budgeted())
    assert receipt.deferred == 1
    with core.storage.read(ctx) as tx:
        assert entity_progress(tx)["pending"] == 1
        state, attempt, code = (
            tx._check()
            .execute(
                "SELECT state,attempt,last_error_code FROM work_items WHERE "
                "work_type='rebuild_projection' AND subject_ref=?",
                (source.ref,),
            )
            .fetchone()
        )
        assert (state, attempt, code) == ("pending", 0, "budget_exhausted")


def recall_signature(core, ctx, query):
    packet = core.recall_packet(
        ctx,
        dict(
            protocol_version="1.1",
            request_id="TEST-entities",
            mode="current",
            query=query,
            max_items=10,
            budget_tokens=6000,
        ),
    )
    return [(i["ref"], i["revision"], i["content"]) for i in packet["items"]]


def test_retraction_restores_recall_results_and_trace_paths(app):
    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "species", "cat")
    source = fact(core, ctx, "TEST-Ricecake", "home", "TEST-Harbor")
    query = "TEST-Ricecake species"
    request = dict(protocol_version="1.1", request_id="TEST-trace-entity", subject="TEST-Ricecake")
    before = recall_signature(core, ctx, query)
    paths = core.trace(ctx, request)["paths"]
    alias = merge(core, ctx, source, target)
    after = recall_signature(core, ctx, query)
    assert any(ref == target.ref for ref, _revision, _content in after)
    assert core.trace(ctx, request)["paths"] != paths
    withdraw(core, ctx, alias)
    assert recall_signature(core, ctx, query) == before
    assert core.trace(ctx, request)["paths"] == paths


@pytest.mark.parametrize("endpoint", ["source", "target"])
def test_deleting_either_endpoint_cascades_to_the_derived_alias(app, endpoint):
    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "species", "cat")
    source = fact(core, ctx, "TEST-Ricecake", "home", "TEST-Harbor")
    alias = merge(core, ctx, source, target)
    removed = source if endpoint == "source" else target
    capture(core, ctx, f"删除 {removed.ref}。")
    core.forget(
        ctx,
        dict(
            protocol_version="1.1",
            target_refs=[removed.ref],
            mode="delete",
            expected_revisions={removed.ref: removed.revision},
        ),
        remaining_seconds=10,
    )
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT read_blocked FROM claims WHERE claim_id=?", (alias.ref,)).fetchone()[0] == 1


def test_unrelated_aliases_cannot_hide_a_mentioned_name_before_the_limit(app):
    from scope_recall.core.claims import Qualification

    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "species", "cat")
    source = fact(core, ctx, "TEST-Ricecake", "home", "TEST-Harbor")
    merge(core, ctx, source, target)
    other = fact(core, ctx, "TEST-noise-project", "name", "TEST-noise-project")
    clauses = [f"TEST-noise-project renamed to TEST-noise-{n}." for n in range(201)]
    raw = capture(core, ctx, " ".join(clauses))
    with core.storage.write(ctx) as tx:
        for n, quote in enumerate(clauses):
            payload = dict(
                kind="alias",
                subject="TEST-noise-project",
                predicate=f"alias-{n}",
                value_text=f"TEST-noise-{n}",
                conditions=[],
                statement_kind="assertion",
                valid_from=None,
                valid_to=None,
                evidence_spans=[dict(source_ref=raw.ref, source_revision=raw.revision, quote=quote)],
                alias=dict(name=f"TEST-noise-{n}", target_ref=other.ref, scope_description="Same project"),
            )
            tx.claims.append(
                "TEST-scope",
                payload,
                Qualification("active", "direct_report", "explicit_scoped_source"),
                recorded_at=core.clock.utc_now(),
            )
    with core.storage.read(ctx) as tx:
        assert resolved_query(tx, "TEST-Ricecake species", core.clock.utc_now()) == "TEST-Nian species"


def test_private_mode_opened_during_judgement_prevents_entity_publication(app, monkeypatch):
    from scope_recall.core import private

    core, ctx = app
    fact(core, ctx, "TEST-Nian", "species", "cat")
    fact(
        core, ctx, "TEST-Ricecake", "home", "TEST-Harbor", "TEST-Ricecake is TEST-Nian. TEST-Ricecake home TEST-Harbor."
    )
    blocked = [False]
    original = private.source_blocked
    monkeypatch.setattr(private, "source_blocked", lambda rules, source: blocked[0] or original(rules, source))

    class ClosingModel(RecordedModel):
        def judge(self, messages, **kwargs):
            answer = super().judge(messages, **kwargs)
            blocked[0] = True
            return answer

    receipt = drain_entities(core, ctx, ClosingModel())
    assert receipt.deferred == 1
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM entity_alias_proofs").fetchone()[0] == 0


def test_identical_names_in_two_real_scopes_remain_separate(tmp_path):
    import itertools

    from scope_recall.core import CoreConfig, MemoryCore

    from tests.contract.test_claims import Clock
    from tests.contract.test_normalized_evidence import envelope
    from tests.v11_support import context, source_event

    original = context(tmp_path / "TEST-two-scopes")
    binding = replace(original.binding, scope_ids=frozenset({"TEST-scope", "TEST-other"}))
    ctx = replace(original, binding=binding, allowed_scope_ids=binding.scope_ids)
    core = MemoryCore(CoreConfig(binding), clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    for scope in binding.scope_ids:
        event = source_event(source_event_key=f"TEST-identity/{scope}", content=f"TEST-Alex workplace {scope}.")
        receipt = core.record_event(ctx, event, scope_id=scope, remaining_seconds=10)
        source = core.source(ctx, receipt.event_refs[0].ref, 1)
        proposal = draft(
            source, scope, kind="fact", statement_kind="assertion", subject="TEST-Alex", predicate="workplace"
        )
        core.records.accept_claim_proposals(ctx, envelope(proposal), scope_id=scope, remaining_seconds=10)
    model = RecordedModel()
    drain_entities(core, ctx, model)
    assert not model.calls
    with core.storage.read(ctx) as tx:
        assert resolve_subject(tx, "TEST-Alex", core.clock.utc_now()).status == "ambiguous"
        assert tx._check().execute("SELECT count(*) FROM entity_alias_proofs").fetchone()[0] == 0


def test_endpoint_revision_invalidates_then_refreshes_a_live_alias(app):
    from scope_recall.core.claims import Qualification

    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "species", "cat")
    source = fact(core, ctx, "TEST-Ricecake", "home", "TEST-Harbor")
    old = merge(core, ctx, source, target)
    with core.storage.write(ctx) as tx:
        updated = tx.claims.append(
            target.scope_id,
            target.payload,
            Qualification("active", "direct_report", "explicit_scoped_source"),
            recorded_at=core.clock.utc_now(),
            previous=target,
        )
    with core.storage.read(ctx) as tx:
        assert resolve_subject(tx, "TEST-Ricecake", core.clock.utc_now()).status == "literal"
    refreshed = merge(core, ctx, source, updated)
    assert refreshed.ref == old.ref and refreshed.revision == old.revision + 1
    with core.storage.read(ctx) as tx:
        assert resolve_subject(tx, "TEST-Ricecake", core.clock.utc_now()).subject == "TEST-Nian"


def test_model_cannot_publish_an_unevidenced_surface_name(app):
    core, ctx = app
    raw = capture(core, ctx, "TEST-Nian species cat.")
    accept(
        core,
        ctx,
        draft(
            raw,
            "cat",
            kind="fact",
            statement_kind="assertion",
            subject="TEST-Nian",
            predicate="species",
            entity_type="object",
            entity_names=["TEST-invented-name"],
        ),
    )
    receipt = drain_entities(core, ctx, RecordedModel())
    assert receipt.failed == 1
    with core.storage.read(ctx) as tx:
        assert entity_progress(tx)["pending"] == 1
        assert tx._check().execute("SELECT count(*) FROM entity_alias_proofs").fetchone()[0] == 0


def test_endpoint_changed_during_judgement_cannot_publish_a_stale_merge(app):
    from scope_recall.core.claims import Qualification

    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "species", "cat")
    fact(
        core, ctx, "TEST-Ricecake", "home", "TEST-Harbor", "TEST-Ricecake is TEST-Nian. TEST-Ricecake home TEST-Harbor."
    )

    class RevisingModel(RecordedModel):
        def judge(self, messages, **kwargs):
            answer = super().judge(messages, **kwargs)
            with core.storage.write(ctx) as tx:
                tx.claims.append(
                    target.scope_id,
                    target.payload,
                    Qualification("active", "direct_report", "explicit_scoped_source"),
                    recorded_at=core.clock.utc_now(),
                    previous=target,
                )
            return answer

    drain_entities(core, ctx, RevisingModel())
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM entity_alias_proofs").fetchone()[0] == 0


def test_recall_release_rechecks_a_withdrawn_entity_proof(app, monkeypatch):
    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "species", "cat")
    source = fact(core, ctx, "TEST-Ricecake", "home", "TEST-Harbor")
    alias = merge(core, ctx, source, target)
    compiler = core.recall_packet_compiler
    fence = compiler._fence

    def withdraw_before_release(_compiler, storage, draft, verified):
        withdraw(core, ctx, alias)
        return fence(storage, draft, verified)

    monkeypatch.setattr(type(compiler), "_fence", withdraw_before_release)
    packet = core.recall_packet(
        ctx,
        dict(
            protocol_version="1.1",
            request_id="TEST-fenced-entity",
            mode="current",
            query="TEST-Ricecake species",
            max_items=10,
            budget_tokens=6000,
        ),
    )
    assert not packet["items"]
    assert "entity_identity_changed" in packet["gaps"]


def test_entity_progress_stays_in_the_current_project(app):
    core, ctx = app
    fact(core, ctx, "TEST-local", "category", "cat")
    fact(core, replace(ctx, project_id="TEST-other"), "TEST-foreign", "category", "cat")
    with core.storage.read(ctx) as tx:
        assert entity_progress(tx)["total"] == 1
        assert entity_progress(tx, include_all_projects=True)["total"] == 2


def test_alias_evidence_cannot_exceed_the_claim_contract(app):
    from scope_recall.contracts import ContractError
    from scope_recall.core.claims import Qualification

    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "category", "cat")
    source = fact(core, ctx, "TEST-Ricecake", "home", "TEST-Harbor")
    enriched = []
    for entity in (target, source):
        payload = dict(entity.payload)
        payload["evidence_spans"] = []
        for index in range(9):
            text = f"{entity.payload['subject']} evidence TEST-{index}."
            evidence = capture(core, ctx, text)
            payload["evidence_spans"].append(
                dict(source_ref=evidence.ref, source_revision=evidence.revision, quote=text)
            )
        with core.storage.write(ctx) as tx:
            enriched.append(
                tx.claims.append(
                    entity.scope_id,
                    payload,
                    Qualification("active", "direct_report", "supported"),
                    recorded_at=core.clock.utc_now(),
                    previous=entity,
                )
            )
    with pytest.raises(ContractError):
        merge(core, ctx, enriched[1], enriched[0])
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM entity_alias_proofs").fetchone()[0] == 0


def test_recall_carries_entity_proof_across_search_and_compilation(app, monkeypatch):
    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "species", "cat")
    source = fact(core, ctx, "TEST-Ricecake", "home", "TEST-Harbor")
    alias = merge(core, ctx, source, target)
    compiler_type = type(core.recall_packet_compiler)
    compile_packet = compiler_type.compile

    def withdraw_before_compile(compiler, context, result, storage):
        withdraw(core, ctx, alias)
        return compile_packet(compiler, context, result, storage)

    monkeypatch.setattr(compiler_type, "compile", withdraw_before_compile)
    packet = core.recall_packet(
        ctx,
        dict(
            protocol_version="1.1",
            request_id="TEST-cross-stage-proof",
            mode="current",
            query="TEST-Ricecake species",
            max_items=10,
            budget_tokens=6000,
        ),
    )
    assert not packet["items"]
    assert "entity_identity_changed" in packet["gaps"]


def test_verified_opaque_principal_cannot_be_rebound_by_entity_matching(app):
    from scope_recall.contracts import ContractError, TrustedSourcePrincipal

    core, ctx = app
    owner = replace(ctx, source_principal=TrustedSourcePrincipal("human", "verified", "TEST-opaque-principal"))
    target = fact(core, owner, "TEST-person", "category", "person")
    source = fact(core, owner, "TEST-opaque-principal", "home", "TEST-Harbor")
    with pytest.raises(ContractError, match="entity_identity"):
        merge(core, owner, source, target)


def test_owner_management_can_withdraw_entity_merge_without_rewriting_facts(app):
    from tests.contract.test_memory_admin import management

    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "category", "cat")
    source = fact(core, ctx, "TEST-Ricecake", "home", "TEST-Harbor")
    before = view(core, ctx, "TEST-Ricecake")
    alias = merge(core, ctx, source, target)
    receipt = management(core, ctx).revise(alias.ref, expected_revision=alias.revision, value=None)
    assert receipt["item"]["state"] == "retracted"
    assert view(core, ctx, "TEST-Ricecake") == before
    assert core.current_claim(ctx, source.ref).payload == source.payload
    assert core.current_claim(ctx, target.ref).payload == target.payload
    assert merge(core, ctx, source, target) is None
