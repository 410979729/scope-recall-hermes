"""Synthetic owner health checks and reversible fact decisions."""

import json
import sqlite3

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core.claims import Qualification
from scope_recall.core.management import MemoryManagement
from scope_recall.core.recall_log_storage import LogPolicy, RecallLogStore
from scope_recall.core.restore import InstallationMaintenance

from tests.contract.test_aliases import app
from tests.contract.test_claims import accept, capture, draft
from tests.contract.test_entity_resolution import fact, view
from tests.contract.test_importance_summaries import app as multi_scope_app
from tests.v11_support import recall_request


def owner(app):
    core, ctx = app
    return MemoryManagement(core, InstallationMaintenance(ctx))


def test_health_page_is_bounded_and_does_not_report_unobserved_history_as_unused(app):
    core, ctx = app
    item = fact(core, ctx, "TEST-Nian", "species", "cat")
    page = owner(app).health(limit=10, deadline_seconds=1)
    assert page["observation_days"] == 30
    assert all(item.ref not in str(issue) for issue in page["unused"])
    assert "complete" in page


def test_entity_selection_checks_versions_and_can_be_withdrawn(app):
    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "species", "cat")
    source = fact(core, ctx, "TEST-Ricecake", "home", "harbor")
    api = owner(app)
    with pytest.raises(ContractError, match="VERSION_CONFLICT"):
        api.health_merge_entity(source.ref, source.revision + 1, target.ref, target.revision)
    receipt = api.health_merge_entity(source.ref, source.revision, target.ref, target.revision)
    alias = core.current_claim(ctx, receipt["ref"])
    assert alias.state == "active"
    assert len(view(core, ctx, "TEST-Ricecake")) == 2
    undone = api.health_undo(receipt["action_id"], receipt["versions"])
    with core.storage.read(ctx) as tx:
        assert tx.claims.version(alias.ref, undone["versions"][alias.ref]).state == "retracted"
    assert len(view(core, ctx, "TEST-Ricecake")) == 1


def test_entity_selection_does_not_replace_an_existing_alias(app):
    core, ctx = app
    first = fact(core, ctx, "TEST-Nian", "species", "cat")
    second = fact(core, ctx, "TEST-Moon", "species", "cat")
    source = fact(core, ctx, "TEST-Ricecake", "home", "harbor")
    api = owner(app)
    receipt = api.health_merge_entity(source.ref, source.revision, first.ref, first.revision)
    with pytest.raises(ContractError, match="VERSION_CONFLICT"):
        api.health_merge_entity(source.ref, source.revision, second.ref, second.revision)
    with core.storage.read(ctx) as tx:
        assert tx.claims.current_revision(receipt["ref"]) == receipt["versions"][receipt["ref"]]


def test_entity_selection_uses_the_reviewed_candidate_name(app):
    from scope_recall.core.read_views import resolve_subject

    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "species", "cat")
    source = capture(core, ctx, "TEST-Nian, also called TEST-Ricecake, home harbor.")
    item = accept(
        core,
        ctx,
        draft(
            source,
            "harbor",
            kind="fact",
            statement_kind="assertion",
            subject="TEST-Nian",
            predicate="home",
            entity_names=["TEST-Ricecake"],
        ),
    ).items[0]
    receipt = owner(app).health_merge_entity(item.ref, item.revision, target.ref, target.revision, name="TEST-Ricecake")
    with core.storage.read(ctx) as tx:
        assert resolve_subject(tx, "TEST-Ricecake", core.clock.utc_now()).subject == "TEST-Nian"
        alias = tx.claims.version(receipt["ref"], receipt["versions"][receipt["ref"]])
        assert alias.payload["alias"]["name"] == "TEST-Ricecake"


def test_entity_undo_after_source_replacement(app):
    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "species", "cat")
    source_event = capture(core, ctx, "TEST-Ricecake home harbor.", key="TEST-alias-source")
    source = accept(
        core,
        ctx,
        draft(
            source_event, "harbor", kind="fact", statement_kind="assertion", subject="TEST-Ricecake", predicate="home"
        ),
    ).items[0]
    api = owner(app)
    receipt = api.health_merge_entity(source.ref, source.revision, target.ref, target.revision)
    capture(core, ctx, "TEST-Ricecake home dock.", key="TEST-alias-source", revision=2)
    api.health_undo(receipt["action_id"], receipt["versions"])
    assert len(view(core, ctx, "TEST-Ricecake")) <= 1


@pytest.mark.parametrize("mode", ["full", "read-only"])
@pytest.mark.parametrize("private_target", ["global", "session"])
def test_entity_undo_respects_private_mode(app, mode, private_target):
    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "species", "cat")
    source = fact(core, ctx, "TEST-Ricecake", "home", "harbor")
    api = owner(app)
    receipt = api.health_merge_entity(source.ref, source.revision, target.ref, target.revision)
    api.private_on(target=private_target, session="TEST-session" if private_target == "session" else None, mode=mode)
    with pytest.raises(ContractError, match="ACCESS_DENIED"):
        api.health_undo(receipt["action_id"], receipt["versions"])


@pytest.mark.parametrize("mode", ["full", "read-only"])
def test_entity_merge_respects_private_mode(app, mode):
    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "species", "cat")
    source = fact(core, ctx, "TEST-Ricecake", "home", "harbor")
    api = owner(app)
    api.private_on(target="session", session="TEST-session", mode=mode)
    with pytest.raises(ContractError, match="ACCESS_DENIED"):
        api.health_merge_entity(source.ref, source.revision, target.ref, target.revision)


def separate_fact(core, ctx, subject, value, **changes):
    source = capture(core, ctx, f"{subject} color {value}.")
    proposal = draft(
        source, value, kind="fact", statement_kind="assertion", subject=subject, predicate="color", **changes
    )
    with core.storage.write(ctx) as tx:
        return tx.claims.append(
            "TEST-scope",
            proposal,
            Qualification("active", "direct_report", "synthetic"),
            recorded_at=core.clock.utc_now(),
        )


@pytest.mark.parametrize("action", ["health_merge_facts", "health_choose_fact"])
def test_fact_action_is_version_checked_and_reversible(app, action):
    core, ctx = app
    first = separate_fact(core, ctx, "TEST-Object", "blue")
    second = separate_fact(core, ctx, "test-object", "blue" if action == "health_merge_facts" else "red")
    api = owner(app)
    with pytest.raises(ContractError, match="VERSION_CONFLICT"):
        getattr(api, action)(first.ref, first.revision + 1, second.ref, second.revision)
    receipt = getattr(api, action)(first.ref, first.revision, second.ref, second.revision)
    with core.storage.read(ctx) as tx:
        head = tx.claims.version(second.ref, receipt["versions"][second.ref])
        assert head.state == "superseded"
        assert tx.claims.version(second.ref, second.revision).state == "active"
    restored = api.health_undo(receipt["action_id"], receipt["versions"])
    with core.storage.read(ctx) as tx:
        for original in (first, second):
            head = tx.claims.version(original.ref, restored["versions"][original.ref])
            assert head.state == original.state
            assert head.payload == original.payload


def test_chosen_fact_does_not_recall_the_replaced_claim(app):
    core, ctx = app
    first = separate_fact(core, ctx, "TEST-Object", "blue")
    second = separate_fact(core, ctx, "test-object", "red")
    receipt = owner(app).health_choose_fact(first.ref, first.revision, second.ref, second.revision)
    result = core.recall_packet(ctx, recall_request(query="test-object color red", mode="current"))
    assert all(item["ref"] != second.ref for item in result["items"])
    assert core.current_claim(ctx, second.ref) is None
    owner(app).health_undo(receipt["action_id"], receipt["versions"])
    assert core.current_claim(ctx, second.ref).state == "active"


def test_observation_merge_retains_dependencies_and_undo(app):
    import hashlib

    from scope_recall.core.reflection_validity import dependency_signature, reflection_live

    core, ctx = app
    facts = []
    for name in ("TEST-Object", "test-object"):
        source = capture(core, ctx, f"{name} color blue.")
        proposal = draft(
            source, "blue", kind="observation", statement_kind="assertion", subject=name, predicate="color"
        )
        with core.storage.write(ctx) as tx:
            version = tx.claims.append(
                "TEST-scope",
                proposal,
                Qualification("active", "derived_summary", "grounded_observation"),
                recorded_at=core.clock.utc_now(),
            )
            position = (
                tx._check()
                .execute(
                    "SELECT rowid FROM source_events WHERE event_id=? AND source_revision=?",
                    (source.ref, source.revision),
                )
                .fetchone()[0]
            )
            members = [
                dict(
                    ref=source.ref,
                    revision=source.revision,
                    position=position,
                    digest=hashlib.sha256(source.event["content"].encode()).hexdigest(),
                    total=len(source.event["content"]),
                    session_id=source.session_id,
                    entry_id=source.entry_id,
                    occurred_at=source.event["occurred_at"],
                )
            ]
            tx._check(write=True).execute(
                "INSERT INTO observations VALUES(?,?,?,?,0)",
                (version.ref, version.revision, json.dumps(members), dependency_signature(tx, members)),
            )
            facts.append(version)
    api = owner(app)
    receipt = api.health_merge_facts(facts[0].ref, facts[0].revision, facts[1].ref, facts[1].revision)
    with core.storage.read(ctx) as tx:
        merged = tx.claims.version(facts[0].ref, receipt["versions"][facts[0].ref])
        assert reflection_live(tx, merged)
    undone = api.health_undo(receipt["action_id"], receipt["versions"])
    with core.storage.read(ctx) as tx:
        assert all(reflection_live(tx, tx.claims.version(v.ref, undone["versions"][v.ref])) for v in facts)


def test_duplicate_candidates_are_not_lost_after_the_lookup_limit(app):
    core, ctx = app
    facts = []
    for number in range(20):
        subject = "TEST-" + "".join(c.upper() if number & (1 << i) else c.lower() for i, c in enumerate("object"))
        facts.append(separate_fact(core, ctx, subject, "blue"))
    api = owner(app)
    cursor = None
    pairs = set()
    for _ in range(200):
        page = api.health(limit=100, cursor=cursor, deadline_seconds=10)
        pairs.update(tuple(sorted(f["ref"] for f in item["facts"])) for item in page["duplicates"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(pairs) == len(facts) * (len(facts) - 1) // 2


def test_duplicate_fact_merge_and_undo_keep_evidence_and_history(app):
    core, ctx = app
    first = separate_fact(core, ctx, "TEST-Object", "BLUE")
    second = separate_fact(core, ctx, "test-object", "blue")
    assert first.ref != second.ref
    api = owner(app)
    page = api.health(limit=20, deadline_seconds=5)
    assert any({first.ref, second.ref} == {v["ref"] for v in issue["facts"]} for issue in page["duplicates"])
    receipt = api.health_merge_facts(first.ref, first.revision, second.ref, second.revision)
    merged = core.current_claim(ctx, first.ref)
    assert len(merged.payload["evidence_spans"]) == 2
    with core.storage.read(ctx) as tx:
        assert tx.claims.version(second.ref, receipt["versions"][second.ref]).state == "superseded"
    with pytest.raises(ContractError, match="VERSION_CONFLICT"):
        api.health_merge_facts(first.ref, first.revision, second.ref, second.revision)
    api.health_undo(receipt["action_id"], receipt["versions"])
    assert core.current_claim(ctx, first.ref).payload == first.payload
    assert core.current_claim(ctx, second.ref).state == "active"


def test_different_conditions_and_disjoint_periods_are_not_contradictions(app):
    core, ctx = app
    first = separate_fact(
        core, ctx, "TEST-Object", "blue", valid_from="2026-01-01T00:00:00Z", valid_to="2026-02-01T00:00:00Z"
    )
    second = separate_fact(
        core, ctx, "test-object", "red", valid_from="2026-02-01T00:00:00Z", valid_to="2026-03-01T00:00:00Z"
    )
    conditional = separate_fact(core, ctx, "TeSt-Object", "green", conditions=["at home"])
    assert len({first.ref, second.ref, conditional.ref}) == 3
    page = owner(app).health(limit=20, deadline_seconds=5)
    assert page["contradictions"] == []


def test_choose_conflicting_fact_and_undo_restore_both_heads(app):
    core, ctx = app
    first = separate_fact(core, ctx, "TEST-Object", "blue")
    second = separate_fact(core, ctx, "test-object", "red")
    api = owner(app)
    page = api.health(limit=20, deadline_seconds=5)
    assert any({first.ref, second.ref} == {v["ref"] for v in issue["facts"]} for issue in page["contradictions"])
    receipt = api.health_choose_fact(first.ref, first.revision, second.ref, second.revision)
    with core.storage.read(ctx) as tx:
        assert tx.claims.version(second.ref, receipt["versions"][second.ref]).state == "superseded"
        assert tx.claims.version(second.ref, second.revision).payload["value_text"] == "red"
    api.health_undo(receipt["action_id"], receipt["versions"])
    with core.storage.read(ctx) as tx:
        assert tx.claims.version(second.ref, tx.claims.current_revision(second.ref)).state == "active"


def test_capacity_rotated_history_does_not_prove_fact_unused(app):
    core, ctx = app
    item = separate_fact(core, ctx, "TEST-Lighthouse", "blue")
    core.clock.now = "2026-10-06T12:00:00Z"
    store = RecallLogStore(core.recall_diagnostics.path, LogPolicy(max_bytes=262144))
    for index in range(300):
        store.write(
            dict(
                ref=f"TEST-attempt-{index}",
                timestamp="2026-09-20T12:00:00Z",
                installation_id=ctx.binding.installation_id,
                session_id="TEST-session",
                request_id=f"TEST-{index}",
                padding="x" * 900,
            )
        )
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM attempts").fetchone()[0] < 300
        assert conn.execute("SELECT capacity_loss_at FROM retention").fetchone()[0] == "2026-09-20T12:00:00Z"
    core.recall_diagnostics.writer._persist_health()
    page = owner(app).health(limit=20, deadline_seconds=5)
    assert page["records_incomplete"]
    assert all(item.ref not in str(issue) for issue in page["unused"])


def test_observed_but_unused_fact_is_listed(app):
    core, ctx = app
    item = separate_fact(core, ctx, "TEST-Lighthouse", "blue")
    core.clock.now = "2026-10-06T12:00:00Z"
    store = core.recall_diagnostics.store
    for day in ("2026-09-20", "2026-10-05"):
        store.write(
            dict(
                ref="TEST-" + day,
                timestamp=day + "T12:00:00Z",
                installation_id=ctx.binding.installation_id,
                session_id="TEST-session",
                request_id="TEST-" + day,
                items=[],
            )
        )
    core.recall_diagnostics.writer._persist_health()
    page = owner(app).health(limit=20, deadline_seconds=5)
    assert page["observation_start"] == "2026-09-20T12:00:00Z"
    assert not page["records_incomplete"]
    assert any(issue["facts"][0]["ref"] == item.ref for issue in page["unused"])


def test_normal_retention_expiry_is_not_capacity_loss(app):
    core, ctx = app
    item = separate_fact(core, ctx, "TEST-Canal", "blue")
    core.clock.now = "2026-10-06T12:00:00Z"
    store = core.recall_diagnostics.store
    for day in ("2026-08-31", "2026-09-20", "2026-10-05"):
        store.write(
            dict(
                ref="TEST-" + day,
                timestamp=day + "T12:00:00Z",
                installation_id=ctx.binding.installation_id,
                session_id="TEST-session",
                request_id="TEST-" + day,
                items=[],
            )
        )
    core.recall_diagnostics.writer._persist_health()
    page = owner(app).health(limit=20, deadline_seconds=5)
    assert not page["records_incomplete"]
    assert any(issue["facts"][0]["ref"] == item.ref for issue in page["unused"])


def test_prior_log_loss_outside_observation_does_not_disable_unused(app):
    core, ctx = app
    item = separate_fact(core, ctx, "TEST-Canal", "blue")
    core.clock.now = "2026-10-06T12:00:00Z"
    store = core.recall_diagnostics.store
    for day in ("2026-09-20", "2026-10-05"):
        store.write(
            dict(
                ref="TEST-" + day,
                timestamp=day + "T12:00:00Z",
                installation_id=ctx.binding.installation_id,
                session_id="TEST-session",
                request_id="TEST-" + day,
                items=[],
            )
        )
    store.path.with_suffix(".health.json").write_text(
        json.dumps(
            dict(
                dropped_full=1,
                write_failures=0,
                written=2,
                queued=0,
                loss_tracking_since="2026-09-01T00:00:00Z",
                last_loss_at="2026-09-02T00:00:00Z",
            )
        ),
        encoding="utf-8",
    )
    page = owner(app).health(limit=20, deadline_seconds=5)
    assert not page["records_incomplete"]
    assert any(issue["facts"][0]["ref"] == item.ref for issue in page["unused"])


def test_health_api_and_cli_apply_the_same_decision(app):
    from scope_recall.maintenance.memory import execute, parser
    from scope_recall.maintenance.ui_api import UIApi

    core, ctx = app
    first = separate_fact(core, ctx, "TEST-Object", "blue")
    second = separate_fact(core, ctx, "test-object", "red")
    api = owner(app)
    web = UIApi(api)
    page = web.read("health", {"limit": "20"})
    assert page["contradictions"]
    request = dict(
        action="health-choose",
        first_ref=first.ref,
        first_revision=first.revision,
        second_ref=second.ref,
        second_revision=second.revision,
    )
    plan = web.preview(request)
    result = web.confirm(dict(id=plan["id"], confirmed=True))["result"]
    reopened = UIApi(api).read("health-actions", {})
    assert any(item["action_id"] == result["action_id"] for item in reopened["items"])
    args = parser().parse_args(["health-undo", result["action_id"], json.dumps(result["versions"])])
    execute(api, args)
    assert core.current_claim(ctx, second.ref).state == "active"


def test_health_actions_reject_muted_evidence(app):
    core, ctx = app
    first = separate_fact(core, ctx, "TEST-Object", "blue")
    second = separate_fact(core, ctx, "test-object", "blue")
    api = owner(app)
    ref = second.payload["evidence_spans"][0]["source_ref"]
    api.apply(api.preview([ref], mode="suppress"))
    with pytest.raises(ContractError):
        api.health_merge_facts(first.ref, first.revision, second.ref, second.revision)


def test_health_does_not_list_expired_current_heads(app):
    core, ctx = app
    first = separate_fact(
        core, ctx, "TEST-Object", "blue", valid_from="2019-01-01T00:00:00Z", valid_to="2020-01-01T00:00:00Z"
    )
    second = separate_fact(
        core, ctx, "test-object", "blue", valid_from="2019-01-01T00:00:00Z", valid_to="2020-01-01T00:00:00Z"
    )
    page = owner(app).health(limit=100, deadline_seconds=5)
    assert not any({first.ref, second.ref} == {v["ref"] for v in issue["facts"]} for issue in page["duplicates"])


def test_health_lists_pending_alternate_entity_name(app):
    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "species", "cat")
    source = capture(core, ctx, "TEST-Nian, also called TEST-Ricecake, home harbor.")
    item = accept(
        core,
        ctx,
        draft(
            source,
            "harbor",
            kind="fact",
            statement_kind="assertion",
            subject="TEST-Nian",
            predicate="home",
            entity_names=["TEST-Ricecake"],
        ),
    ).items[0]
    page = owner(app).health(limit=100, deadline_seconds=5)
    assert any(
        issue["name"] == "TEST-Ricecake" and {target.ref, item.ref} == {v["ref"] for v in issue["facts"]}
        for issue in page["entities"]
    )


def test_chosen_fact_does_not_return_losing_evidence_as_current(app):
    core, ctx = app
    first = separate_fact(core, ctx, "TEST-Object", "blue")
    second = separate_fact(core, ctx, "test-object", "red")
    receipt = owner(app).health_choose_fact(first.ref, first.revision, second.ref, second.revision)
    result = core.recall_packet(ctx, recall_request(query="test-object color red", mode="current"))
    assert any("blue" in item["content"].lower() for item in result["items"])
    assert all(
        "color red" not in item["content"].lower() or item["temporal_status"] == "historical"
        for item in result["items"]
    )
    owner(app).health_undo(receipt["action_id"], receipt["versions"])
    restored = core.recall_packet(ctx, recall_request(query="test-object color red", mode="current"))
    assert any(
        "color red" in item["content"].lower() and item["temporal_status"] == "current" for item in restored["items"]
    )


def test_unpublished_writer_loss_is_not_hidden_by_older_aggregate(app):
    core, ctx = app
    separate_fact(core, ctx, "TEST-Lighthouse", "blue")
    core.clock.now = "2026-10-06T12:00:00Z"
    store = core.recall_diagnostics.store
    for day in ("2026-09-20", "2026-10-05"):
        store.write(
            dict(
                ref="TEST-" + day,
                timestamp=day + "T12:00:00Z",
                installation_id=ctx.binding.installation_id,
                session_id="TEST-session",
                request_id="TEST-" + day,
                items=[],
            )
        )
    store.path.with_suffix(".health.json").write_text(
        json.dumps(dict(dropped_full=10, write_failures=0, written=2, queued=0, last_loss_at="2026-09-01T00:00:00Z")),
        encoding="utf-8",
    )
    core.recall_diagnostics.writer.dropped_full = 1
    page = owner(app).health(limit=20, deadline_seconds=5)
    assert page["records_incomplete"]
    assert page["unused"] == []


def test_health_can_resolve_a_name_from_one_source_fact(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-Nian, also called TEST-Ricecake, home harbor.")
    item = accept(
        core,
        ctx,
        draft(
            source,
            "harbor",
            kind="fact",
            statement_kind="assertion",
            subject="TEST-Nian",
            predicate="home",
            entity_names=["TEST-Ricecake"],
        ),
    ).items[0]
    api = owner(app)
    page = api.health(limit=100, deadline_seconds=5)
    assert any(
        issue["name"] == "TEST-Ricecake" and all(v["ref"] == item.ref for v in issue["facts"])
        for issue in page["entities"]
    )
    result = api.health_merge_entity(item.ref, item.revision, item.ref, item.revision, name="TEST-Ricecake")
    api.health_undo(result["action_id"], result["versions"])


def test_equal_values_under_different_conditions_are_not_duplicates(app):
    core, ctx = app
    first = separate_fact(core, ctx, "TEST-Object", "blue", conditions=["at home"])
    second = separate_fact(core, ctx, "test-object", "blue", conditions=["at work"])
    page = owner(app).health(limit=100, deadline_seconds=5)
    pairs = [issue["facts"] for category in ("duplicates", "contradictions") for issue in page[category]]
    assert not any({first.ref, second.ref} == {v["ref"] for v in pair} for pair in pairs)


def test_scope_boundaries_exclude_cross_scope_fact_pairs(multi_scope_app):
    from dataclasses import replace

    from tests.v11_support import source_event

    core, ctx = multi_scope_app
    ctx = replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)
    first = separate_fact(core, ctx, "TEST-Object", "blue")
    event = source_event(source_event_key="TEST-other-scope", content="test-object color blue.")
    saved = core.record_event(ctx, event, scope_id="TEST-other", remaining_seconds=10)
    source = core.source(ctx, saved.event_refs[0].ref, 1)
    proposal = draft(source, "blue", kind="fact", statement_kind="assertion", subject="test-object", predicate="color")
    with core.storage.write(ctx) as tx:
        second = tx.claims.append(
            "TEST-other",
            proposal,
            Qualification("active", "direct_report", "synthetic"),
            recorded_at=core.clock.utc_now(),
        )
    api = owner((core, ctx))
    page = api.health(limit=100, deadline_seconds=5)
    assert not any({first.ref, second.ref} == {v["ref"] for v in issue["facts"]} for issue in page["duplicates"])
    with pytest.raises(ContractError, match="ACCESS_DENIED"):
        api.health_merge_facts(first.ref, first.revision, second.ref, second.revision)


def test_same_name_alone_does_not_nominate_two_people_as_duplicates(app):
    core, ctx = app
    first = fact(core, ctx, "TEST-Alex", "workplace", "school")
    second = fact(core, ctx, "TEST-Alex", "occupation", "architect")
    page = owner(app).health(limit=100, deadline_seconds=5)
    assert not any({first.ref, second.ref} == {v["ref"] for v in issue["facts"]} for issue in page["entities"])


def test_delivered_fact_is_not_listed_as_unused(app):
    core, ctx = app
    item = separate_fact(core, ctx, "TEST-Lighthouse", "blue")
    core.clock.now = "2026-10-06T12:00:00Z"
    store = core.recall_diagnostics.store
    for day, items in (("2026-09-20", []), ("2026-10-05", [dict(kind="claim", ref=item.ref, reason="delivered")])):
        store.write(
            dict(
                ref="TEST-" + day,
                timestamp=day + "T12:00:00Z",
                installation_id=ctx.binding.installation_id,
                session_id="TEST-session",
                request_id="TEST-" + day,
                items=items,
            )
        )
    core.recall_diagnostics.writer._persist_health()
    page = owner(app).health(limit=100, deadline_seconds=5)
    assert not page["records_incomplete"]
    assert all(issue["facts"][0]["ref"] != item.ref for issue in page["unused"])


def test_incomplete_health_counters_cannot_prove_an_unused_window(app):
    core, ctx = app
    separate_fact(core, ctx, "TEST-Lighthouse", "blue")
    core.clock.now = "2026-10-06T12:00:00Z"
    store = core.recall_diagnostics.store
    for day in ("2026-09-20", "2026-10-05"):
        store.write(
            dict(
                ref="TEST-" + day,
                timestamp=day + "T12:00:00Z",
                installation_id=ctx.binding.installation_id,
                session_id="TEST-session",
                request_id="TEST-" + day,
                items=[],
            )
        )
    store.path.with_suffix(".health.json").write_text(json.dumps(dict(queued=0)), encoding="utf-8")
    page = owner(app).health(limit=20, deadline_seconds=5)
    assert page["records_incomplete"]
    assert page["unused"] == []


def test_short_observation_window_does_not_list_unused(app):
    core, ctx = app
    separate_fact(core, ctx, "TEST-Lighthouse", "blue")
    core.clock.now = "2026-10-06T12:00:00Z"
    store = core.recall_diagnostics.store
    store.write(
        dict(
            ref="TEST-recent",
            timestamp="2026-10-05T12:00:00Z",
            installation_id=ctx.binding.installation_id,
            session_id="TEST-session",
            request_id="TEST-recent",
            items=[],
        )
    )
    core.recall_diagnostics.writer._persist_health()
    page = owner(app).health(limit=20, deadline_seconds=5)
    assert page["observation_start"] == "2026-10-05T12:00:00Z"
    assert page["unused"] == []


@pytest.mark.parametrize("counter", ["dropped_full", "write_failures"])
def test_loss_during_observation_disables_unused_group(app, counter):
    core, ctx = app
    separate_fact(core, ctx, "TEST-Lighthouse", "blue")
    core.clock.now = "2026-10-06T12:00:00Z"
    store = core.recall_diagnostics.store
    for day in ("2026-09-20", "2026-10-05"):
        store.write(
            dict(
                ref="TEST-" + day,
                timestamp=day + "T12:00:00Z",
                installation_id=ctx.binding.installation_id,
                session_id="TEST-session",
                request_id="TEST-" + day,
                items=[],
            )
        )
    health = dict(dropped_full=0, write_failures=0, written=2, queued=0, last_loss_at="2026-09-25T00:00:00Z")
    health[counter] = 1
    store.path.with_suffix(".health.json").write_text(json.dumps(health), encoding="utf-8")
    page = owner(app).health(limit=20, deadline_seconds=5)
    assert page["records_incomplete"]
    assert page["unused"] == []


@pytest.mark.parametrize("action", ["health_merge_facts", "health_choose_fact"])
def test_fact_undo_rejects_concurrent_head_change(app, action):
    core, ctx = app
    first = separate_fact(core, ctx, "TEST-Object", "blue")
    second = separate_fact(core, ctx, "test-object", "blue" if action == "health_merge_facts" else "red")
    api = owner(app)
    receipt = getattr(api, action)(first.ref, first.revision, second.ref, second.revision)
    api.revise(first.ref, expected_revision=receipt["versions"][first.ref], value="green")
    with pytest.raises(ContractError, match="VERSION_CONFLICT"):
        api.health_undo(receipt["action_id"], receipt["versions"])


def test_evidence_only_merge_preserves_dependency_signatures(app):
    from scope_recall.core.summary_validity import group_signature

    core, ctx = app
    first = separate_fact(core, ctx, "TEST-Object", "blue")
    second = separate_fact(core, ctx, "test-object", "blue")
    members = [
        dict(ref=span["source_ref"], revision=span["source_revision"], total=len(span["quote"]))
        for fact in (first, second)
        for span in fact.payload["evidence_spans"]
    ]
    with core.storage.read(ctx) as tx:
        before = group_signature(tx, members)
    api = owner(app)
    receipt = api.health_merge_facts(first.ref, first.revision, second.ref, second.revision)
    with core.storage.read(ctx) as tx:
        merged = group_signature(tx, members)
    assert merged == before
    api.health_undo(receipt["action_id"], receipt["versions"])
    with core.storage.read(ctx) as tx:
        assert group_signature(tx, members) == before


def test_health_action_history_excludes_muted_facts(app):
    core, ctx = app
    first = separate_fact(core, ctx, "TEST-Object", "blue")
    second = separate_fact(core, ctx, "test-object", "red")
    api = owner(app)
    receipt = api.health_choose_fact(first.ref, first.revision, second.ref, second.revision)
    api.apply(api.preview([first.ref], mode="suppress"))
    assert all(item["action_id"] != receipt["action_id"] for item in api.health_actions()["items"])


def test_inline_private_source_is_filtered_before_health_reads(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-Object color blue. <private>TEST-health-hidden</private>")
    proposal = draft(source, "blue", kind="fact", statement_kind="assertion", subject="TEST-Object", predicate="color")
    with core.storage.write(ctx) as tx:
        item = tx.claims.append(
            "TEST-scope",
            proposal,
            Qualification("active", "direct_report", "synthetic"),
            recorded_at=core.clock.utc_now(),
        )
    separate_fact(core, ctx, "test-object", "red")
    page = owner(app).health(limit=100, deadline_seconds=5)
    assert item.ref in json.dumps(page)
    assert "TEST-health-hidden" not in json.dumps(page)
    assert "TEST-health-hidden" not in source.event["content"]
