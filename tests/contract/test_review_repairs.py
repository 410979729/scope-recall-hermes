"""Review publication, restoration and final release share one owner boundary."""

import json
from dataclasses import replace

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core import review_list, review_sources
from scope_recall.core.aliases import entity_alias_proof
from scope_recall.core.read_views import resolve_subject
from scope_recall.core.retrieval import CandidateRef, SearchContext
from scope_recall.core.visibility import ObjectRef, release_objects
from test_claims import accept, app, capture, draft
from test_review_list import owner
from v11_support import recall_request


@pytest.mark.parametrize("update", [False, True])
@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_automatic_entity_alias_waits_for_review_and_preserves_proof(app, update, decision):
    from test_entity_resolution import fact, merge

    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "species", "cat")
    source = fact(core, ctx, "TEST-Ricecake", "home", "harbor")
    if update:
        merge(core, ctx, source, target)
        source = fact(core, ctx, "TEST-Ricecake", "home", "hill")
    api = owner(app)
    api.set_review_policy("TEST-scope", True, expected_version=0)
    pending = merge(core, ctx, source, target)
    assert pending.state == "proposed"
    search = SearchContext.from_request(
        recall_request(), ctx, now=core.clock.utc_now(), deadline=core.clock.monotonic() + 10
    )
    with core.storage.read(ctx) as tx:
        assert resolve_subject(tx, "TEST-Ricecake", core.clock.utc_now()).subject == "TEST-Ricecake"
        assert (
            core.recall_pipeline.storage_reader.hydrate(
                tx, CandidateRef("claim", pending.ref, pending.revision, "lexical"), search
            )
            is None
        )
    item = next(i for i in api.reviews(source="fact")["items"] if i["key"][1] == pending.ref)
    receipt = api.review_decide(item["key"], decision, expected_version=item["version"])
    with core.storage.read(ctx) as tx:
        resolved = resolve_subject(tx, "TEST-Ricecake", core.clock.utc_now()).subject
        assert resolved == ("TEST-Nian" if decision == "approve" else "TEST-Ricecake")
        if decision == "approve":
            head = tx.claims.version(pending.ref, tx.claims.current_revision(pending.ref))
            assert entity_alias_proof(tx, head, target, core.clock.utc_now())
            assert head.basis == "derived_summary"
    api.review_undo(receipt["action_id"], expected_version=receipt["version"])
    assert any(i["key"][1] == pending.ref for i in api.reviews(source="fact")["items"])
    assert merge(core, ctx, source, target) is None
    with core.storage.read(ctx) as tx:
        row = (
            tx._check().execute("SELECT status FROM entity_names WHERE name=?", (source.payload["subject"],)).fetchone()
        )
        assert row is None or row[0] != "withdrawn"


@pytest.mark.parametrize("old_active", [False, True])
@pytest.mark.parametrize("reject", [False, True])
def test_importance_replay_keeps_pending_or_rejected_native_version(app, old_active, reject):
    core, ctx = app
    api = owner(app)
    if old_active:
        accept(
            core,
            ctx,
            draft(
                capture(core, ctx, "TEST-project palette blue."),
                "blue",
                predicate="palette",
                kind="fact",
                statement_kind="assertion",
            ),
        )
    api.set_review_policy("TEST-scope", True, expected_version=0)
    proposal = draft(
        capture(core, ctx, "TEST-project palette green.", when="2026-09-05T12:00:00Z"),
        "green",
        predicate="palette",
        kind="fact",
        statement_kind="assertion",
        importance=4,
    )
    pending = accept(core, ctx, proposal).items[0]
    if reject:
        item = api.reviews(source="fact")["items"][0]
        api.review_decide(item["key"], "reject", expected_version=item["version"])
    api.set_review_policy("TEST-scope", False, expected_version=1)
    replay = accept(core, ctx, proposal).items[0]
    assert (replay.ref, replay.revision, replay.state) == (pending.ref, pending.revision, "proposed")
    head = core.current_claim(ctx, pending.ref)
    assert (head.payload["value_text"] if head else None) == ("blue" if old_active else None)
    assert len(api.reviews(source="fact")["items"]) == (0 if reject else 1)


def adoption_with_ordinary_decision(app):
    """Create an ordinary choice and its supported, still-unpublished adoption."""
    from scope_recall.core.worker_adoption import prepare_adoptions
    from test_adoption import assistant, selected
    from test_adoption_revision import reply

    core, ctx = app
    assistant(core, ctx)
    clause = "好，就用方案二"
    source = capture(core, ctx, clause)
    proposal = draft(source, "方案二", predicate="采纳选项")
    proposal["evidence_spans"][0].update(start=0, end=len(clause))
    from scope_recall.core.claims import Qualification

    # Represent an already accepted, source-positioned decision. Its earlier
    # qualification is independent of the adoption publication being tested.
    with core.storage.write(ctx) as tx:
        ordinary = tx.claims.append(
            "TEST-scope",
            proposal,
            Qualification("active", "direct_report", "owner_decision"),
            recorded_at=core.clock.utc_now(),
        )
    prepared = prepare_adoptions(
        core.storage,
        ctx,
        (source,),
        reply(selected() | {"confirmation_quote": clause}),
        clock=core.clock,
        remaining_seconds=10,
    )[0]
    return source, ordinary, prepared


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_adoption_duplicate_effects_wait_for_approval_and_undo_together(app, decision):
    from test_adoption import publish

    core, ctx = app
    _, ordinary, prepared = adoption_with_ordinary_decision(app)
    ref = ordinary.ref
    before = core.current_claim(ctx, ref)
    api = owner(app)
    api.set_review_policy("TEST-scope", True, expected_version=0)
    pending = publish(core, ctx, prepared)
    assert pending.state == "proposed"
    assert core.current_claim(ctx, ref).state == "active"
    item = next(i for i in api.reviews(source="fact")["items"] if i["key"][1] == pending.ref)
    receipt = api.review_decide(item["key"], decision, expected_version=item["version"])
    assert (core.current_claim(ctx, ref) is None) == (decision == "approve")
    api.review_undo(receipt["action_id"], expected_version=receipt["version"])
    restored = core.current_claim(ctx, ref)
    assert restored.state == before.state and restored.payload == before.payload


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_observation_undo_restores_active_preimage_after_policy_change(app, decision):
    from test_reflection import ReflectionModel, observation, populate, run

    core, ctx = app
    populate(core, ctx)
    run(core, ctx, ReflectionModel())
    original = observation(core, ctx)
    api = owner(app)
    api.set_review_policy("TEST-scope", True, expected_version=0)
    item = api.reviews(source="observation")["items"][0]
    receipt = api.review_decide(item["key"], decision, expected_version=item["version"])
    api.review_undo(receipt["action_id"], expected_version=receipt["version"])
    with core.storage.read(ctx) as tx:
        restored = tx.claims.version(original.ref, tx.claims.current_revision(original.ref))
        assert (restored.state, restored.basis, restored.reason) == (original.state, original.basis, original.reason)
        rows = [
            tuple(
                tx._check()
                .execute(
                    "SELECT manifest_json,signature,confirmed FROM observations WHERE claim_ref=? AND claim_revision=?",
                    (original.ref, rev),
                )
                .fetchone()
            )
            for rev in (original.revision, restored.revision)
        ]
        assert rows[0] == rows[1]


def test_history_release_withholds_review_items_but_owner_management_can_read(app):
    core, ctx = app
    api = owner(app)
    api.set_review_policy("TEST-scope", True, expected_version=0)
    pending = accept(
        core, ctx, draft(capture(core, ctx, "TEST-project palette green."), "green", predicate="palette")
    ).items[0]
    with pytest.raises(ContractError, match="SOURCE_MISSING"):
        release_objects(
            core.storage,
            core.clock,
            ctx,
            (ObjectRef("claim", pending.ref, pending.revision),),
            expected_epoch=core.memory_epoch(ctx),
            automatic=False,
            history=True,
        )
    assert api.show(pending.ref, revision=pending.revision)["value"]["payload"]["value_text"] == "green"


@pytest.mark.parametrize("operation,expected", [("list", 1), ("decide", 2), ("actions", 1), ("undo", 1)])
def test_review_operations_reuse_transaction_snapshot(app, monkeypatch, operation, expected):
    core, ctx = app
    api = owner(app)
    api.set_review_policy("TEST-scope", True, expected_version=0)
    accept(core, ctx, draft(capture(core, ctx, "TEST-project palette green."), "green", predicate="palette"))
    item = api.reviews(source="fact")["items"][0]
    receipt = None
    if operation in {"actions", "undo"}:
        receipt = api.review_decide(item["key"], "reject", expected_version=item["version"])
    calls = []
    original = review_sources.snapshot

    def counted(*args, **kwargs):
        calls.append(json.dumps(args[2]))
        return original(*args, **kwargs)

    monkeypatch.setattr(review_sources, "snapshot", counted)
    monkeypatch.setattr(review_list, "snapshot", counted)
    if operation == "list":
        api.reviews(source="fact")
    elif operation == "decide":
        api.review_decide(item["key"], "reject", expected_version=item["version"])
    elif operation == "actions":
        api.review_actions()
    else:
        api.review_undo(receipt["action_id"], expected_version=receipt["version"])
    assert len(calls) == expected


def test_owner_exception_requires_bound_capability_and_never_automatic_release(app):
    from scope_recall.core.restore import InstallationMaintenance

    core, ctx = app
    api = owner(app)
    api.set_review_policy("TEST-scope", True, expected_version=0)
    pending = accept(
        core, ctx, draft(capture(core, ctx, "TEST-project palette green."), "green", predicate="palette")
    ).items[0]
    refs = (ObjectRef("claim", pending.ref, pending.revision),)
    with pytest.raises(ContractError, match="ACCESS_DENIED"):
        release_objects(
            core.storage,
            core.clock,
            replace(ctx, session_id="TEST-other"),
            refs,
            expected_epoch=core.memory_epoch(ctx),
            automatic=False,
            history=True,
            owner_authority=InstallationMaintenance(ctx),
        )
    with pytest.raises(ContractError, match="SOURCE_MISSING"):
        release_objects(
            core.storage,
            core.clock,
            ctx,
            refs,
            expected_epoch=core.memory_epoch(ctx),
            automatic=True,
            history=True,
            owner_authority=InstallationMaintenance(ctx),
        )


def test_duplicate_changed_after_approval_prevents_combined_undo(app):
    from test_adoption import publish

    core, ctx = app
    _, ordinary, prepared = adoption_with_ordinary_decision(app)
    api = owner(app)
    api.set_review_policy("TEST-scope", True, expected_version=0)
    pending = publish(core, ctx, prepared)
    item = next(i for i in api.reviews(source="fact")["items"] if i["key"][1] == pending.ref)
    receipt = api.review_decide(item["key"], "approve", expected_version=item["version"])
    revision = api.show(ordinary.ref)["value"]["revision"]
    api.revise(ordinary.ref, expected_revision=revision, value="方案三")
    with pytest.raises(ContractError, match="VERSION_CONFLICT"):
        api.review_undo(receipt["action_id"], expected_version=receipt["version"])


@pytest.mark.parametrize("delivered", ["claim", "event"])
def test_pending_correction_waits_then_notifies_only_after_approval(app, delivered):
    from scope_recall.core.session_brief import mark_delivered

    core, ctx = app
    old_source = capture(core, ctx, "TEST-project palette blue.")
    old = accept(
        core, ctx, draft(old_source, "blue", predicate="palette", kind="fact", statement_kind="assertion")
    ).items[0]
    with core.storage.write(ctx) as tx:
        mark_delivered(
            tx,
            ctx,
            [dict(kind=delivered, ref=old.ref if delivered == "claim" else old_source.ref, revision=1)],
            core.clock.utc_now(),
            0,
            "question",
        )
    api = owner(app)
    api.set_review_policy("TEST-scope", True, expected_version=0)
    new_source = capture(core, ctx, "TEST-project palette green.", when="2026-09-05T12:00:00Z")
    accept(core, ctx, draft(new_source, "green", predicate="palette", kind="fact", statement_kind="assertion"))
    prepared = core.prepare_session_injection(ctx, "")
    assert prepared["_injection"]["notices"] == []
    item = api.reviews(source="fact")["items"][0]
    api.review_decide(item["key"], "approve", expected_version=item["version"])
    notices = core.prepare_session_injection(ctx, "")["_injection"]["notices"]
    assert len(notices) == 1 and notices[0]["corrected"] and "green" in notices[0]["text"]


def test_notice_rechecks_native_review_marker_before_rendering(app):
    from types import SimpleNamespace

    from scope_recall.core import session_injection

    core, ctx = app
    api = owner(app)
    api.set_review_policy("TEST-scope", True, expected_version=0)
    held = accept(
        core, ctx, draft(capture(core, ctx, "TEST-project palette green."), "green", predicate="palette")
    ).items[0]
    replacement = SimpleNamespace(kind="claim", ref=held.ref, revision=held.revision, temporal_status="current")
    row = dict(object_kind="claim", object_ref=held.ref, revision=held.revision, notice_marker=None)
    with core.storage.read(ctx) as tx:
        assert session_injection._make_notice(tx, row, "claim:" + held.ref + "@1", replacement) is None


def test_host_comparison_never_receives_held_facts(app, monkeypatch):
    from scope_recall.core import recall_packet
    from scope_recall.core.visibility import REVIEW_REASONS

    core, ctx = app
    api = owner(app)
    old = accept(
        core,
        ctx,
        draft(
            capture(core, ctx, "TEST-project palette blue."),
            "blue",
            predicate="palette",
            kind="fact",
            statement_kind="assertion",
        ),
    ).items[0]
    api.set_review_policy("TEST-scope", True, expected_version=0)
    accept(
        core,
        ctx,
        draft(
            capture(core, ctx, "TEST-project palette green.", when="2026-09-05T12:00:00Z"),
            "green",
            predicate="palette",
            kind="fact",
            statement_kind="assertion",
        ),
    )
    compared = []
    original = recall_packet.host_memory_duplicate

    def compare(obj, entries):
        if obj.kind == "claim":
            with core.storage.read(ctx) as tx:
                native = tx.claims.version(obj.ref, obj.revision)
                assert native.state == "active" and native.reason not in REVIEW_REASONS
            compared.append(obj.ref)
        return original(obj, entries)

    monkeypatch.setattr(recall_packet, "host_memory_duplicate", compare)
    core.recall_packet(
        ctx,
        recall_request(query="TEST-project palette", mode="auto"),
        host_memory_entries=("TEST-project palette green",),
    )
    assert old.ref in compared
