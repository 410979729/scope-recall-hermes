"""Owner review gates publication without replacing source-owned state."""

import json
from dataclasses import replace

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core.management import MemoryManagement
from scope_recall.core.management_query import MemoryFilter
from scope_recall.core.restore import InstallationMaintenance
from test_claims import accept, app, capture, draft


def owner(app):
    return MemoryManagement(app[0], InstallationMaintenance(app[1]))


def test_review_policy_is_versioned_and_defaults_off(app):
    api = owner(app)
    assert api.review_policy("TEST-scope")["enabled"] is False
    saved = api.set_review_policy("TEST-scope", True, expected_version=0)
    assert saved["version"] == 1
    with pytest.raises(ContractError, match="VERSION_CONFLICT"):
        api.set_review_policy("TEST-scope", False, expected_version=0)


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_fact_review_preserves_existing_fact_and_undo_restores_pending(app, decision):
    core, ctx = app
    api = owner(app)
    old = accept(core, ctx, draft(capture(core, ctx, "TEST-project 配色蓝色。"))).items[0]
    api.set_review_policy("TEST-scope", True, expected_version=0)
    source = capture(core, ctx, "TEST-project 配色绿色。", when="2026-09-05T12:00:00Z")
    pending = accept(core, ctx, draft(source, "绿色")).items[0]
    assert pending.state == "proposed"
    assert core.current_claim(ctx, old.ref).payload["value_text"] == "蓝色"
    item = api.reviews(source="fact")["items"][0]
    receipt = api.review_decide(item["key"], decision, expected_version=item["version"])
    with pytest.raises(ContractError, match="VERSION_CONFLICT"):
        api.review_decide(item["key"], decision, expected_version=item["version"])
    expected = "绿色" if decision == "approve" else "蓝色"
    assert core.current_claim(ctx, old.ref).payload["value_text"] == expected
    api.review_undo(receipt["action_id"], expected_version=receipt["version"])
    assert core.current_claim(ctx, old.ref).payload["value_text"] == "蓝色"
    assert len(api.reviews(source="fact")["items"]) == 1


def test_turning_policy_off_does_not_publish_held_facts(app):
    core, ctx = app
    api = owner(app)
    api.set_review_policy("TEST-scope", True, expected_version=0)
    proposal = draft(capture(core, ctx, "TEST-project 配色蓝色。"))
    pending = accept(core, ctx, proposal).items[0]
    api.set_review_policy("TEST-scope", False, expected_version=1)
    assert accept(core, ctx, proposal).items[0].state == "proposed"
    assert core.current_claim(ctx, pending.ref) is None
    with api.export_reader(MemoryFilter(kind="claim")) as reader:
        assert all(item.current is None for item in reader.iter_claims())
    assert len(api.reviews(source="fact")["items"]) == 1


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_import_decision_and_undo_use_original_admission(app, decision):
    core, ctx = app
    api = owner(app)
    source = capture(core, ctx, "TEST-project 配色蓝色。", origin="imported", key="memory-import:TEST-summary")
    item = api.reviews(source="import")["items"][0]
    receipt = api.review_decide(item["key"], decision, expected_version=item["version"])
    assert api.reviews(source="import")["items"] == []
    assert_stale(api, item, decision)
    assert core.source(ctx, source.ref, 1).event["origin"] == "imported"
    api.review_undo(receipt["action_id"], expected_version=receipt["version"])
    assert len(api.reviews(source="import")["items"]) == 1


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_adoption_decision_requires_exact_owner_selection_and_is_reversible(app, decision):
    from test_adoption import assistant, prepared, publish

    core, ctx = app
    assistant(core, ctx)
    assistant(core, ctx)
    confirmation = capture(core, ctx, "好，就用方案二")
    pending, _ = prepared(core, ctx, confirmation)
    publish(core, ctx, pending[0])
    api = owner(app)
    item = api.reviews(source="adoption")["items"][0]
    receipt = api.review_decide(
        item["key"], decision, expected_version=item["version"], selection={"fragment_ids": [2]}
    )
    if decision == "reject":
        publish(core, ctx, pending[0])
    assert api.reviews(source="adoption")["items"] == []
    assert_stale(api, item, decision)
    assert all(i["category"] != "adoption" for i in api.quarantine()["items"])
    api.review_undo(receipt["action_id"], expected_version=receipt["version"])
    assert len(api.reviews(source="adoption")["items"]) == 1


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_observation_decision_preserves_inductive_identity_and_undo(app, decision):
    from test_reflection import ReflectionModel, observation, populate, run

    core, ctx = app
    populate(core, ctx)
    run(core, ctx, ReflectionModel())
    original = observation(core, ctx)
    api = owner(app)
    item = api.reviews(source="observation")["items"][0]
    receipt = api.review_decide(item["key"], decision, expected_version=item["version"])
    assert api.reviews(source="observation")["items"] == []
    assert_stale(api, item, decision)
    assert all(i["category"] != "observation" for i in api.quarantine()["items"])
    api.review_undo(receipt["action_id"], expected_version=receipt["version"])
    restored = api.reviews(source="observation")["items"][0]
    with core.storage.read(ctx) as tx:
        head = tx.claims.version(restored["key"][1], restored["key"][2])
        assert head.basis == original.basis == "derived_summary"


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_health_decision_and_undo_share_original_health_view(app, decision):
    from test_entity_resolution import fact

    core, ctx = app
    fact(core, ctx, "TEST-Nian", "species", "cat")
    # Separate raw slots with the same normalized identity are health candidates.
    fact(core, ctx, "TEST-Nian", "Species", "cat")
    api = owner(app)
    item = next(i for i in api.reviews(source="health")["items"] if i["category"] == "duplicates")
    receipt = api.review_decide(item["key"], decision, expected_version=item["version"])
    assert api.health()["duplicates"] == []
    assert_stale(api, item, decision)
    api.review_undo(receipt["action_id"], expected_version=receipt["version"])
    assert api.health()["duplicates"]


def test_pending_new_fact_undo_approval_does_not_revive_approved_history(app):
    core, ctx = app
    api = owner(app)
    api.set_review_policy("TEST-scope", True, expected_version=0)
    pending = accept(core, ctx, draft(capture(core, ctx, "TEST-project 配色蓝色。"))).items[0]
    item = api.reviews(source="fact")["items"][0]
    receipt = api.review_decide(item["key"], "approve", expected_version=item["version"])
    api.review_undo(receipt["action_id"], expected_version=receipt["version"])
    assert core.current_claim(ctx, pending.ref) is None
    assert len(api.reviews(source="fact")["items"]) == 1


def test_pending_fact_is_absent_from_query_packet_and_session_brief(app):
    from v11_support import recall_request

    core, ctx = app
    api = owner(app)
    api.set_review_policy("TEST-scope", True, expected_version=0)
    pending = accept(core, ctx, draft(capture(core, ctx, "TEST-project 配色蓝色。"))).items[0]
    entry = replace(ctx, session_id="TEST-next-review")
    packet = core.recall_packet(entry, recall_request(query="TEST-project 配色", mode="auto"), deadline_seconds=10)
    explicit = core.recall_packet(entry, recall_request(query="TEST-project 配色", mode="current"), deadline_seconds=10)
    brief = core.session_brief(entry)
    assert all(item["ref"] != pending.ref for result in (packet, explicit) for item in result["items"])
    assert pending.ref not in json.dumps(brief)
    profile = core.profile(entry, dict(protocol_version="1.1", request_id="TEST-profile", subject="TEST-project"))
    entity = core.entity(
        entry,
        dict(
            protocol_version="1.1", request_id="TEST-entity", subject="TEST-project", action="related", direction="both"
        ),
    )
    trace = core.trace(entry, dict(protocol_version="1.1", request_id="TEST-trace", subject="TEST-project"))
    assert all(pending.ref not in json.dumps(result) for result in (profile, entity, trace))


def assert_stale(api, item, decision):
    """A native decision cannot be replayed from the same reviewed snapshot."""
    with pytest.raises(ContractError, match="VERSION_CONFLICT"):
        api.review_decide(item["key"], decision, expected_version=item["version"])


def test_owner_can_inspect_pending_fact_but_agent_inspection_withholds_it(app):
    core, ctx = app
    api = owner(app)
    api.set_review_policy("TEST-scope", True, expected_version=0)
    pending = accept(core, ctx, draft(capture(core, ctx, "TEST-project 配色蓝色。"))).items[0]
    assert core.inspect_object(ctx, pending.ref).value is None
    assert api.show(pending.ref)["value"]["state"] == "proposed"


def test_current_source_edit_and_stale_undo_cannot_overwrite_newer_fact(app):
    core, ctx = app
    api = owner(app)
    api.set_review_policy("TEST-scope", True, expected_version=0)
    pending = accept(core, ctx, draft(capture(core, ctx, "TEST-project 配色蓝色。"))).items[0]
    item = api.reviews(source="fact")["items"][0]
    receipt = api.review_decide(item["key"], "approve", expected_version=item["version"])
    current = core.current_claim(ctx, pending.ref)
    api.revise(current.ref, expected_revision=current.revision, value="绿色")
    with pytest.raises(ContractError, match="VERSION_CONFLICT"):
        api.review_undo(receipt["action_id"], expected_version=receipt["version"])
    assert not api.review_actions()["items"]


def test_cli_and_management_api_use_same_review_contract(app):
    from scope_recall.maintenance.memory import execute, parser
    from scope_recall.maintenance.ui_api import UIApi

    core, ctx = app
    api = owner(app)
    args = parser().parse_args(["review-policy-set", "on", "--scope", "TEST-scope", "--expected-version", "0"])
    execute(api, args)
    accept(core, ctx, draft(capture(core, ctx, "TEST-project 配色蓝色。")))
    web = UIApi(api)
    item = web.read("reviews", {"source": "fact"})["items"][0]
    args = parser().parse_args(
        ["review-decide", json.dumps(item["key"]), "reject", "--expected-version", item["version"]]
    )
    receipt = execute(api, args)
    preview = web.preview(
        dict(action="review-undo", action_id=receipt["action_id"], expected_version=receipt["version"])
    )
    web.confirm(dict(id=preview["id"], confirmed=True))
    assert api.reviews(source="fact")["items"]


def test_real_concurrent_review_has_one_winner(app):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    core, ctx = app
    api = owner(app)
    api.set_review_policy("TEST-scope", True, expected_version=0)
    accept(core, ctx, draft(capture(core, ctx, "TEST-project 配色蓝色。")))
    item = api.reviews(source="fact")["items"][0]
    barrier = Barrier(2)

    def decide():
        barrier.wait(5)
        try:
            api.review_decide(item["key"], "approve", expected_version=item["version"])
            return "approved"
        except ContractError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: decide(), range(2)))
    assert sorted(results) == ["VERSION_CONFLICT", "approved"]


def test_review_policy_also_holds_new_inductive_observations(app):
    from test_reflection import ReflectionModel, observation, populate, run
    from v11_support import recall_request

    core, ctx = app
    api = owner(app)
    api.set_review_policy("TEST-scope", True, expected_version=0)
    populate(core, ctx)
    run(core, ctx, ReflectionModel())
    held = observation(core, ctx)
    assert held.state == "proposed"
    item = api.reviews(source="observation")["items"][0]
    packet = core.recall_packet(ctx, recall_request(query="connection timeouts", mode="current"), deadline_seconds=10)
    assert all(i["ref"] != held.ref for i in packet["items"])
    receipt = api.review_decide(item["key"], "approve", expected_version=item["version"])
    assert core.current_claim(ctx, held.ref).state == "active"
    api.review_undo(receipt["action_id"], expected_version=receipt["version"])
    assert core.current_claim(ctx, held.ref) is None
    assert api.reviews(source="observation")["items"]


def test_import_undo_cancels_only_work_scheduled_by_approval(app):
    core, ctx = app
    api = owner(app)
    capture(core, ctx, "TEST imported memory material.", origin="imported", key="memory-import:TEST-queue")
    item = api.reviews(source="import")["items"][0]
    receipt = api.review_decide(item["key"], "approve", expected_version=item["version"])
    api.review_undo(receipt["action_id"], expected_version=receipt["version"])
    with core.storage.read(ctx) as tx:
        assert (
            not tx._check()
            .execute(
                "SELECT 1 FROM work_items WHERE subject_ref=? AND work_type='consolidate' AND state "
                "IN ('pending','leased')",
                (item["key"][1],),
            )
            .fetchall()
        )


def test_import_undo_conflicts_after_worker_advances_queue(app):
    core, ctx = app
    api = owner(app)
    capture(core, ctx, "TEST imported memory material.", origin="imported", key="memory-import:TEST-queue")
    item = api.reviews(source="import")["items"][0]
    receipt = api.review_decide(item["key"], "approve", expected_version=item["version"])
    with core.storage.write(ctx) as tx:
        tx._check().execute(
            "UPDATE work_items SET state='done' WHERE subject_ref=? AND work_type='consolidate'", (item["key"][1],)
        )
    with pytest.raises(ContractError, match="VERSION_CONFLICT"):
        api.review_undo(receipt["action_id"], expected_version=receipt["version"])


def test_approved_ambiguous_adoption_resumes_real_worker_stages(app):
    from types import SimpleNamespace

    from test_adoption import assistant, model, prepared, publish

    core, ctx = app
    assistant(core, ctx)
    assistant(core, ctx)
    confirmation = capture(core, ctx, "好，就用方案二")
    pending, _ = prepared(core, ctx, confirmation)
    publish(core, ctx, pending[0])
    with core.storage.write(ctx) as tx:
        tx._check().execute("UPDATE work_items SET state='done'")
    api = owner(app)
    item = api.reviews(source="adoption")["items"][0]
    receipt = api.review_decide(
        item["key"], "approve", expected_version=item["version"], selection={"fragment_ids": [2]}
    )
    judge, calls = model()
    core.drain_worker(ctx, consolidation=SimpleNamespace(judge=judge), max_items=10, remaining_seconds=30)
    with core.storage.read(ctx) as tx:
        relation = tx._check().execute("SELECT * FROM adoption_relations").fetchone()
        assert relation["state"] == "bound" and relation["claim_ref"] is not None
        head = tx.claims.version(relation["claim_ref"], relation["claim_revision"])
        assert head.state == "active"
        assert "先备份" in head.payload["value_text"]
    assert calls
    with pytest.raises(ContractError, match="VERSION_CONFLICT"):
        api.review_undo(receipt["action_id"], expected_version=receipt["version"])


def test_source_purge_removes_adoption_review_preimage(app):
    from test_adoption import assistant, prepared, publish, selected
    from test_deletion import authorize, request

    core, ctx = app
    assistant(core, ctx)
    assistant(core, ctx)
    confirmation = capture(core, ctx, "好，就用方案二")
    pending, _ = prepared(core, ctx, confirmation, selected() | {"confirmation_quote": "好，就用方案二"})
    publish(core, ctx, pending[0])
    api = owner(app)
    item = api.reviews(source="adoption")["items"][0]
    receipt = api.review_decide(item["key"], "reject", expected_version=item["version"])
    authorize(core, ctx, confirmation)
    deletion = core.forget(ctx, request(confirmation), remaining_seconds=10)
    with core.storage.write(ctx, remaining_seconds=10) as tx:
        tx.deletions.purge_sqlite(deletion["operation_id"])
        assert (
            not tx._check()
            .execute("SELECT 1 FROM management_actions WHERE action_id=?", (receipt["action_id"],))
            .fetchall()
        )


def test_import_approval_survives_projection_repair(app):
    from scope_recall.core.source_admission import admit_source

    core, ctx = app
    api = owner(app)
    capture(core, ctx, "TEST imported material.", origin="imported", key="memory-import:TEST-repair")
    item = api.reviews(source="import")["items"][0]
    api.review_decide(item["key"], "approve", expected_version=item["version"])
    with core.storage.write(ctx) as tx:
        admit_source(tx, core.clock, item["key"][1], item["key"][2], core.config.admission_policy)
    assert not api.reviews(source="import")["items"]


def test_review_gate_preserves_quote_summary_identity_and_dependency_fence(app):
    from scope_recall.core.claims import Qualification

    core, ctx = app
    api = owner(app)
    api.set_review_policy("TEST-scope", True, expected_version=0)
    source = capture(core, ctx, "TEST-project summary exact quotation.")
    payload = draft(source, "exact quotation", kind="fact", predicate="summary", statement_kind="assertion")
    with core.storage.write(ctx) as tx:
        head = tx.claims.append(
            "TEST-scope",
            payload,
            Qualification("proposed", "derived_summary", "summary_pending"),
            recorded_at=core.clock.utc_now(),
        )
        saved = tx.claims.append(
            "TEST-scope",
            payload,
            Qualification("active", "derived_summary", "grounded_session_summary"),
            recorded_at=core.clock.utc_now(),
            previous=head,
        )
        assert saved.state == "active" and saved.reason == "grounded_session_summary"
    assert core.current_claim(ctx, saved.ref) is not None
    # A source-view summary still needs its summary manifest before delivery.
    from v11_support import recall_request

    packet = core.recall_packet(ctx, recall_request(query="exact quotation", mode="current"), deadline_seconds=10)
    assert all(i["ref"] != saved.ref for i in packet["items"])
