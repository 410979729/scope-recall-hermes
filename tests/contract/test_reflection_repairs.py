"""Regression boundaries for private, durable and bounded inductive observations."""

import json
from dataclasses import replace

import pytest
from scope_recall.core.claims import Qualification
from scope_recall.core.reflection_storage import decode_observations, next_reflection_due, queue_reflections
from scope_recall.core.reflection_validity import reflection_live
from test_claims import capture, draft
from test_importance_summaries import app  # noqa: F401
from test_reflection import ReflectionModel, observation, owner_api, populate, run


def test_private_dependency_blocks_read_hydration_and_release(app):
    from scope_recall.contracts import ContractError
    from scope_recall.core.visibility import ObjectRef, _released_claim

    core, ctx = app
    populate(core, ctx)
    run(core, ctx, ReflectionModel())
    item = observation(core, ctx)
    owner_api(core, ctx).private_on(target="session", session="TEST-session-1")
    with core.storage.read(ctx) as tx:
        assert not reflection_live(tx, item)
        with pytest.raises(ContractError):
            _released_claim(tx, core.clock, ObjectRef("claim", item.ref, item.revision), automatic=True, history=False)
    packet = core.recall_packet(
        ctx,
        dict(
            protocol_version="1.1",
            request_id="TEST-private-observation",
            query="connection timeouts",
            mode="current",
            max_items=10,
            budget_tokens=4096,
        ),
    )
    assert not any(row["ref"] == item.ref for row in packet["items"])


def test_unrelated_backlog_preserves_observation_until_actual_conflict(app):
    core, ctx = app
    populate(core, ctx)
    run(core, ctx, ReflectionModel())
    item = observation(core, ctx)
    for _ in range(260):
        capture(core, ctx, "TEST-owner mentions an unrelated weather report.")
    with core.storage.read(ctx) as tx:
        assert reflection_live(tx, item)
    # Each idle write advances only a bounded conflict page; reads retain the
    # last conclusion while the remaining source history is still being checked.
    for _ in range(4):
        with core.storage.write(ctx) as tx:
            from scope_recall.core.reflection_conflicts import advance_conflicts

            advance_conflicts(tx)
    capture(core, ctx, "TEST-project does not repeatedly encounter connection timeouts.")
    with core.storage.read(ctx) as tx:
        assert not reflection_live(tx, item)


def test_invalid_observation_slot_accepts_new_supported_revision(app):
    core, ctx = app
    roots = populate(core, ctx)
    run(core, ctx, ReflectionModel())
    old = observation(core, ctx)
    owner_api(core, ctx).apply(owner_api(core, ctx).preview([roots[0].ref], mode="suppress"))
    with core.storage.read(ctx) as tx:
        assert not reflection_live(tx, old)
    populate(core, ctx)
    run(core, ctx, ReflectionModel())
    new = observation(core, ctx)
    assert new.ref == old.ref and new.revision > old.revision
    with core.storage.read(ctx) as tx:
        assert reflection_live(tx, new) and not reflection_live(tx, old)
        assert tx._check().execute("SELECT count(*) FROM observations WHERE claim_ref=?", (old.ref,)).fetchone()[0] == 2


@pytest.mark.parametrize("confirmed", [False, True])
def test_text_requalification_preserves_observation_state(app, confirmed):
    from scope_recall.core.requalify import requalify_claims

    core, ctx = app
    populate(core, ctx)
    run(core, ctx, ReflectionModel())
    item = observation(core, ctx)
    if confirmed:
        owner_api(core, ctx).revise(item.ref, expected_revision=item.revision, value=item.payload["value_text"])
    with core.storage.write(ctx) as tx:
        before = tx.claims.versions(item.ref)[-1]
        requalify_claims(tx, now=core.clock.utc_now(), dry_run=False, limit=16)
        after = tx.claims.versions(item.ref)[-1]
        assert (after.revision, after.state, after.reason) == (before.revision, before.state, before.reason)
        assert reflection_live(tx, after)


def test_obsolete_buffer_is_persisted_and_stops_immediate_wakes(app):
    core, ctx = app
    roots = populate(core, ctx)
    with core.storage.write(ctx) as tx:
        from scope_recall.core.reflection_storage import _discover

        _discover(tx, limit=8)
        original = tx.claims.append(
            "TEST-scope",
            draft(roots[0]),
            Qualification("active", "direct_report", "TEST-direct"),
            recorded_at=core.clock.utc_now(),
        )
        tx.claims.append(
            "TEST-scope",
            original.payload,
            Qualification("retracted", "direct_report", "TEST-withdrawn"),
            previous=original,
            recorded_at=core.clock.utc_now(),
        )
        assert queue_reflections(tx, now=core.clock.utc_now(), enabled=True) == 0
        assert tx._check().execute("SELECT json_array_length(buffered_json) FROM reflection_groups").fetchone()[0] == 7
        assert next_reflection_due(tx, now=core.clock.utc_now()) is None


def decoded_scene(core, ctx):
    roots = populate(core, ctx)
    with core.storage.write(ctx) as tx:
        queue_reflections(tx, now=core.clock.utc_now(), enabled=True)
        row = tx._check().execute("SELECT * FROM reflection_groups WHERE job_ref IS NOT NULL").fetchone()
        members = json.loads(row["manifest_json"])
    raw = ReflectionModel().judge(
        [
            dict(
                content=json.dumps(
                    dict(
                        task="reflect",
                        sources=[dict(m, content=s.event["content"]) for m, s in zip(members, roots, strict=True)],
                    )
                )
            )
        ],
        remaining_seconds=10,
    )
    value = json.loads(raw)
    value["observations"][0]["sessions"] = [1, 2]
    return roots, members, value


def test_invalid_candidate_does_not_discard_valid_sibling(app):
    core, ctx = app
    roots, members, value = decoded_scene(core, ctx)
    bad = dict(
        value["observations"][0],
        value_text="unsupported TEST value",
        evidence_spans=[dict(value["observations"][0]["evidence_spans"][0], quote="absent TEST quote")],
    )
    good = value["observations"][0]
    value["observations"] = [bad, good]
    assert len(decode_observations(value, members, roots)) == 1


@pytest.mark.parametrize("declared", [[1], [1, 99], [1, 2, 3]])
def test_declared_sessions_must_equal_actual_distinct_citations(app, declared):
    core, ctx = app
    roots, members, value = decoded_scene(core, ctx)
    value["observations"][0]["sessions"] = declared
    assert decode_observations(value, members, roots) == []


def test_scope_daily_reflection_cap_is_independent_and_visible(app):
    core, ctx = app
    model = ReflectionModel(empty=True)
    for _ in range(6):
        populate(core, ctx)
        run(core, ctx, model)
    assert len(model.calls) <= 4
    from scope_recall.core.reflection_policy import reflection_usage

    with core.storage.read(ctx) as tx:
        usage = reflection_usage(tx, now=core.clock.utc_now())
        assert usage[0]["limit"] == 4 and usage[0]["calls"] == len(model.calls)


def test_backfill_scans_one_page_before_cooldown(app):
    core, ctx = app
    populate(core, ctx, 40)
    model = ReflectionModel(empty=True)
    for _ in range(6):
        run(core, ctx, model)
    with core.storage.read(ctx) as tx:
        position = (
            tx._check()
            .execute("SELECT position FROM reflection_cursors WHERE audience NOT LIKE 'groups:%'")
            .fetchone()[0]
        )
    assert position == 8 and len(model.calls) <= 1
    with core.storage.read(ctx) as tx:
        assert next_reflection_due(tx, now=core.clock.utc_now()) > core.clock.utc_now()


def test_empty_groups_wait_for_more_new_evidence(app):
    core, ctx = app
    model = ReflectionModel(empty=True)
    for _ in range(2):
        populate(core, ctx)
        run(core, ctx, model)
    assert len(model.calls) == 2
    populate(core, ctx)
    run(core, ctx, model)
    assert len(model.calls) == 2


def test_valid_sibling_reaches_support_and_rejection_is_durable(app):
    core, ctx = app
    populate(core, ctx)

    class Mixed(ReflectionModel):
        def judge(self, messages, *, remaining_seconds):
            value = json.loads(super().judge(messages, remaining_seconds=remaining_seconds))
            if "observations" in value:
                good = value["observations"][0]
                bad = dict(good, evidence_spans=[dict(good["evidence_spans"][0], quote="absent TEST phrase")])
                value["observations"] = [bad, good]
            return json.dumps(value)

    model = Mixed()
    run(core, ctx, model)
    assert observation(core, ctx) is not None and len(model.calls) == 2
    with core.storage.read(ctx) as tx:
        rejected = tx._check().execute("SELECT candidate,reason FROM reflection_rejections").fetchall()
        assert len(rejected) == 1 and rejected[0][0] == 0 and rejected[0][1]


def test_private_rule_opened_and_closed_during_support_invalidates_result(app):
    core, ctx = app
    populate(core, ctx)
    api = owner_api(core, ctx)

    class ChangesPrivacy(ReflectionModel):
        def judge(self, messages, *, remaining_seconds):
            result = super().judge(messages, remaining_seconds=remaining_seconds)
            if json.loads(messages[-1]["content"]).get("task") != "reflect":
                api.private_on(target="session", session="TEST-session-2")
                api.private_off(target="session", session="TEST-session-2")
            return result

    run(core, ctx, ChangesPrivacy())
    assert observation(core, ctx) is None


def test_owner_suppressed_observation_is_not_renewed_by_model(app):
    core, ctx = app
    populate(core, ctx)
    run(core, ctx, ReflectionModel())
    old = observation(core, ctx)
    api = owner_api(core, ctx)
    api.apply(api.preview([old.ref], mode="suppress"))
    populate(core, ctx)
    run(core, ctx, ReflectionModel())
    with core.storage.read(ctx) as tx:
        assert tx.claims.current_revision(old.ref) == old.revision
        assert not reflection_live(tx, tx.claims.version(old.ref, old.revision))


def test_daily_counter_is_shared_across_projects_and_resets_only_at_utc_midnight(app):
    from scope_recall.core.reflection_policy import reflection_usage, reserve_call

    core, ctx = app
    with core.storage.write(ctx) as tx:
        assert [reserve_call(tx, "TEST-scope", now=core.clock.utc_now()) for _ in range(5)] == [
            True,
            True,
            True,
            True,
            False,
        ]
    other = replace(ctx, project_id="TEST-another-project")
    with core.storage.write(other) as tx:
        assert not reserve_call(tx, "TEST-scope", now=core.clock.utc_now())
        assert reflection_usage(tx, now=core.clock.utc_now())[0]["calls"] == 4
        assert reserve_call(tx, "TEST-scope", now="2026-09-07T00:00:00Z")


def test_doctor_reports_independent_scope_reflection_usage(app):
    from datetime import UTC, datetime

    from scope_recall.core.reflection_policy import reserve_call
    from scope_recall.maintenance.doctor_report import DoctorReport
    from scope_recall.maintenance.doctor_store import _derived_progress

    core, ctx = app
    now = datetime.now(UTC).isoformat()
    report = DoctorReport(host="hermes", status="ok")
    with core.storage.write(ctx) as tx:
        assert reserve_call(tx, "TEST-scope", now=now)
    with core.storage.read(ctx) as tx:
        _derived_progress(report, tx, ctx.binding)
    assert report.reflections["daily"][0]["calls"] == 1
    assert report.reflections["daily"][0]["limit"] == 4


def test_exhausted_scope_does_not_borrow_another_scopes_immediate_wake(app):
    from scope_recall.core.reflection_policy import reserve_call
    from scope_recall.core.reflection_storage import _discover

    core, ctx = app
    ctx = replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)
    populate(core, ctx)
    with core.storage.write(ctx) as tx:
        for _ in range(4):
            assert reserve_call(tx, "TEST-scope", now=core.clock.utc_now())
        _discover(tx, limit=8)
        assert next_reflection_due(tx, now=core.clock.utc_now()) > core.clock.utc_now()


def test_another_authorized_audience_does_not_recount_old_sources(app):
    core, ctx = app
    populate(core, ctx)
    model = ReflectionModel(empty=True)
    run(core, ctx, model)
    assert len(model.calls) == 1
    run(core, replace(ctx, allowed_scope_ids=ctx.binding.scope_ids), model)
    assert len(model.calls) == 1


def test_buffered_available_scope_wakes_immediately(app):
    from scope_recall.core.reflection_storage import _discover

    core, ctx = app
    populate(core, ctx)
    with core.storage.write(ctx) as tx:
        _discover(tx, limit=8)
        assert next_reflection_due(tx, now=core.clock.utc_now()) == core.clock.utc_now()
