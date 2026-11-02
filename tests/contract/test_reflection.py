"""Cross-session observations remain disposable, grounded and owner controlled."""

import json
from dataclasses import replace

import pytest
from scope_recall.core.reflection_storage import queue_reflections
from scope_recall.core.reflection_validity import reflection_live
from scope_recall.core.worker import WorkerConfig, drain_worker
from test_claims import capture, initial
from test_importance_summaries import app  # noqa: F401


class ReflectionModel:
    """Count the real generation/support protocol using synthetic source quotes."""

    def __init__(self, *, supported=True, empty=False):
        self.calls = []
        self.supported = supported
        self.empty = empty

    def judge(self, messages, *, remaining_seconds):
        self.calls.append(messages)
        body = json.loads(messages[-1]["content"])
        if body.get("task") == "reflect":
            sources = body["sources"]
            return json.dumps(
                dict(
                    observations=[]
                    if self.empty
                    else [
                        dict(
                            subject="TEST-project",
                            predicate="recurring failure",
                            sessions=sorted(
                                {
                                    list(dict.fromkeys(s["session_id"] for s in sources)).index(s["session_id"]) + 1
                                    for s in (sources[0], sources[-1])
                                }
                            ),
                            value_text="TEST-project repeatedly encounters connection timeouts.",
                            evidence_spans=[
                                dict(
                                    source_ref=s["ref"],
                                    source_revision=s["revision"],
                                    quote=s["content"],
                                    start=0,
                                    end=len(s["content"]),
                                )
                                for s in (sources[0], sources[-1])
                            ],
                        )
                    ]
                )
            )
        return json.dumps(
            dict(
                support="supported" if self.supported else "unsupported",
                action="new",
                target_ref=None,
                target_revision=None,
            )
        )


def populate(core, ctx, count=8):
    sources = []
    for i in range(count):
        session = replace(ctx, session_id=f"TEST-session-{i % 3}")
        sources.append(capture(core, session, "TEST-project encounters connection timeouts."))
    with core.storage.write(ctx) as tx:
        tx._check().execute("UPDATE work_items SET state='done'")
    return sources


def run(core, ctx, model, enabled=True):
    return drain_worker(
        core.storage,
        core.clock,
        ctx,
        config=WorkerConfig("TEST-reflection", reflection_capable=enabled),
        consolidation=model,
        remaining_seconds=30,
    )


def observation(core, ctx):
    with core.storage.read(ctx) as tx:
        row = (
            tx._check()
            .execute("SELECT claim_ref,claim_revision FROM observations ORDER BY rowid DESC LIMIT 1")
            .fetchone()
        )
        return tx.claims.version(*row) if row else None


def test_idle_worker_generates_supported_cross_session_observation(app):
    core, ctx = app
    sources = populate(core, ctx)
    model = ReflectionModel()
    run(core, ctx, model)
    item = observation(core, ctx)
    assert item is not None and item.payload["kind"] == "observation"
    assert item.basis == "derived_summary" and item.reason == "grounded_observation"
    assert len(model.calls) == 2
    assert {s["source_ref"] for s in item.payload["evidence_spans"]} == {sources[0].ref, sources[-1].ref}
    with core.storage.read(ctx) as tx:
        assert reflection_live(tx, item)
    run(core, ctx, model)
    assert len(model.calls) == 2


@pytest.mark.parametrize(
    "count,enabled,empty,supported",
    [(1, True, False, True), (8, False, False, True), (8, True, True, True), (8, True, False, False)],
)
def test_no_budget_chatter_and_unsupported_patterns_publish_nothing(app, count, enabled, empty, supported):
    core, ctx = app
    populate(core, ctx, count)
    model = ReflectionModel(empty=empty, supported=supported)
    run(core, ctx, model, enabled)
    assert observation(core, ctx) is None
    if not enabled or count < 8:
        assert not model.calls


def test_observation_is_unreadable_after_root_correction_and_across_scope(app):
    core, ctx = app
    fact, _source = initial(core, ctx)
    populate(core, ctx)
    run(core, ctx, ReflectionModel())
    item = observation(core, ctx)
    assert item is not None
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    owner.revise(fact.ref, expected_revision=fact.revision, value="green")
    with core.storage.read(ctx) as tx:
        assert not reflection_live(tx, item)
    with core.storage.read(replace(ctx, allowed_scope_ids=frozenset({"TEST-other"}))) as tx:
        assert not reflection_live(tx, item)


def test_private_mode_makes_zero_requests(app):
    core, ctx = app
    populate(core, ctx)
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    owner.private_on(target="global")
    model = ReflectionModel()
    run(core, ctx, model)
    assert not model.calls and observation(core, ctx) is None


def owner_api(core, ctx):
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    return MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))


@pytest.mark.parametrize("action", ["delete", "suppress", "quarantined", "pending_review"])
def test_every_root_withdrawal_blocks_inspection_and_recall(app, action):
    from scope_recall.contracts import ContractError

    core, ctx = app
    roots = populate(core, ctx)
    run(core, ctx, ReflectionModel())
    item = observation(core, ctx)
    api = owner_api(core, ctx)
    if action in {"delete", "suppress"}:
        api.apply(api.preview([roots[0].ref], mode=action))
    else:
        with core.storage.write(ctx) as tx:
            tx._check().execute(
                "INSERT INTO source_safety VALUES(?,?,?,'[]',?,NULL)",
                (roots[0].ref, roots[0].revision, action, core.clock.utc_now()),
            )
    with core.storage.read(ctx) as tx:
        assert not reflection_live(tx, item)
    with pytest.raises(ContractError):
        api.show(item.ref)


def test_owner_review_preserves_inductive_basis_and_original_dependencies(app):
    core, ctx = app
    roots = populate(core, ctx)
    run(core, ctx, ReflectionModel())
    item = observation(core, ctx)
    api = owner_api(core, ctx)
    pending = api.quarantine()
    assert any(r["ref"] == item.ref and r["category"] == "observation" for r in pending["items"])
    api.revise(item.ref, expected_revision=item.revision, value=item.payload["value_text"])
    reviewed = api.show(item.ref)["value"]
    assert reviewed["basis"] == "derived_summary" and reviewed["reason"] == "confirmed_observation"
    assert not any(r["ref"] == item.ref for r in api.quarantine()["items"])
    api.revise(
        item.ref, expected_revision=reviewed["revision"], value="TEST-project has recurrent timeouts in these sessions."
    )
    changed = api.show(item.ref)["value"]
    assert changed["payload"]["kind"] == "observation" and changed["basis"] == "derived_summary"
    assert len(changed["payload"]["evidence_spans"]) >= 2
    api.apply(api.preview([roots[-1].ref], mode="delete"))
    from scope_recall.contracts import ContractError

    with pytest.raises(ContractError):
        api.show(item.ref)


def test_later_owner_denial_blocks_induction_without_replacing_facts(app):
    core, ctx = app
    populate(core, ctx)
    run(core, ctx, ReflectionModel())
    item = observation(core, ctx)
    capture(core, ctx, "TEST-project does not repeatedly encounter connection timeouts.")
    with core.storage.read(ctx) as tx:
        assert not reflection_live(tx, item)


def test_cli_shared_allowance_resumes_support_without_regenerating(app):
    core, ctx = app
    populate(core, ctx)
    delegate = ReflectionModel()

    class Once:
        def __init__(self):
            self.remaining_calls = 1

        def judge(self, messages, *, remaining_seconds):
            assert self.remaining_calls == 1
            self.remaining_calls = 0
            return delegate.judge(messages, remaining_seconds=remaining_seconds)

    run(core, ctx, Once())
    assert len(delegate.calls) == 1 and observation(core, ctx) is None
    run(core, ctx, Once())
    assert len(delegate.calls) == 2 and observation(core, ctx) is not None


def test_observation_never_qualifies_as_a_fact_or_evidence_root(app):
    from scope_recall.contracts import ContractError
    from scope_recall.core.claims import qualify

    core, ctx = app
    populate(core, ctx)
    run(core, ctx, ReflectionModel())
    item = observation(core, ctx)
    with core.storage.read(ctx) as tx:
        assert (
            qualify(
                item.payload,
                tx.claims.roots(
                    tuple(f"{s['source_ref']}@{s['source_revision']}" for s in item.payload["evidence_spans"])
                ),
            ).state
            == "proposed"
        )
        with pytest.raises(ContractError):
            tx.claims.require_live_source(item.ref, item.revision)


def test_recall_displays_induction_label_and_full_evidence(app, record_property):
    core, ctx = app
    populate(core, ctx)
    run(core, ctx, ReflectionModel())
    item = observation(core, ctx)
    from scope_recall.core.retrieval import CandidateRef, SearchContext, SearchLimits
    from scope_recall.core.retrieval_storage import RetrievalStorage

    search = SearchContext(
        "TEST-project timeouts",
        "current",
        None,
        (),
        SearchLimits(),
        core.clock.monotonic() + 30,
        core.clock.utc_now(),
        ctx,
    )
    with core.storage.read(ctx) as tx:
        value = RetrievalStorage(clock=core.clock).hydrate(
            tx, CandidateRef("claim", item.ref, item.revision, "exact_ref"), search
        )
    assert value is not None and value.origin == "inductive_observation"
    assert "not the owner's words" in value.content and len(value.evidence_refs) >= 2
    record_property("rendered_observation", json.dumps(dict(content=value.content, evidence_refs=value.evidence_refs)))


def test_observation_redundancy_keeps_only_information_missing_from_originals():
    from scope_recall.core.recall_restatement import redundant_summary_keys
    from scope_recall.core.retrieval import RetrievedObject

    original = RetrievedObject(
        "event-test",
        1,
        "event",
        "Connection timeouts recur across three sessions.",
        "human_direct",
        "current",
        "TEST",
        ("event-test@1",),
        "direct_report",
        True,
    )
    payload = dict(kind="observation", value_text="Connection timeouts recur across three sessions.", evidence_spans=[])
    observation_item = replace(
        original,
        ref="claim-test",
        kind="claim",
        origin="inductive_observation",
        content="Inductive observation: " + payload["value_text"],
        basis="derived_summary",
        metadata=(("qualification_reason", "grounded_observation"), ("payload_json", json.dumps(payload))),
    )
    assert ("claim", "claim-test", 1) in redundant_summary_keys([original, observation_item])
    extra = replace(
        observation_item,
        metadata=(
            ("qualification_reason", "grounded_observation"),
            (
                "payload_json",
                json.dumps(dict(payload, value_text=payload["value_text"] + " Usually after deployment.")),
            ),
        ),
    )
    assert ("claim", "claim-test", 1) not in redundant_summary_keys([original, extra])


def test_later_affirmative_owner_correction_holds_observation(app):
    core, ctx = app
    populate(core, ctx)
    run(core, ctx, ReflectionModel())
    item = observation(core, ctx)
    capture(core, ctx, "Correct TEST-project recurring failure to DNS resolution failures after recent incidents.")
    with core.storage.read(ctx) as tx:
        assert any(item.ref in update["candidate_refs"] for update in tx.claims.unresolved_updates())
        assert not reflection_live(tx, item)


def test_observation_validity_reloads_head_after_owner_review(app):
    core, ctx = app
    populate(core, ctx)
    run(core, ctx, ReflectionModel())
    item = observation(core, ctx)
    api = owner_api(core, ctx)
    api.revise(item.ref, expected_revision=item.revision, value=item.payload["value_text"])
    with core.storage.read(ctx) as tx:
        assert not reflection_live(tx, item)


def test_idle_discovery_eventually_visits_groups_beyond_first_page(app):
    core, ctx = app
    with core.storage.write(ctx) as tx:
        for index in range(64):
            tx._check().execute(
                "INSERT INTO reflection_groups(group_key,scope_id,project_id,branch_id,subject) VALUES(?,?,?,?,?)",
                (f"TEST-inactive-{index}", "TEST-scope", ctx.project_id, ctx.branch_id, "TEST-inactive"),
            )
    populate(core, ctx)
    model = ReflectionModel()
    run(core, ctx, model)
    run(core, ctx, model)
    assert observation(core, ctx) is not None


@pytest.mark.parametrize("positions", ["missing", "wrong", "ambiguous"])
def test_quote_alignment_keeps_positions_authoritative(app, positions):
    from scope_recall.contracts import ContractError
    from scope_recall.core.reflection_storage import decode_observations

    core, ctx = app
    sources = populate(core, ctx)
    with core.storage.write(ctx) as tx:
        queue_reflections(tx, now=core.clock.utc_now(), enabled=True)
        row = tx._check().execute("SELECT * FROM reflection_groups WHERE job_ref IS NOT NULL").fetchone()
        members = json.loads(row["manifest_json"])
        original = [tx.source(s.ref, s.revision) for s in sources]
    body = ReflectionModel().judge(
        [
            dict(
                content=json.dumps(
                    dict(
                        task="reflect",
                        sources=[dict(m, content=s.event["content"]) for m, s in zip(members, original, strict=True)],
                    )
                )
            )
        ],
        remaining_seconds=10,
    )
    value = json.loads(body)
    for span in value["observations"][0]["evidence_spans"]:
        if positions == "wrong":
            span["end"] -= 1
        else:
            del span["start"]
            del span["end"]
    if positions == "ambiguous":
        original = [replace(s, event=dict(s.event, content=s.event["content"] * 2)) for s in original]
    if positions == "missing":
        result = decode_observations(value, members, original)
        assert all("start" in span for span in result[0]["evidence_spans"])
    else:
        rejected = []
        assert decode_observations(value, members, original, rejections=rejected) == []
        assert rejected


def test_owner_can_resolve_a_pending_observation_correction(app):
    core, ctx = app
    populate(core, ctx)
    run(core, ctx, ReflectionModel())
    item = observation(core, ctx)
    capture(core, ctx, "Correct TEST-project recurring failure to DNS resolution failures after recent incidents.")
    api = owner_api(core, ctx)
    api.revise(
        item.ref, expected_revision=item.revision, value="TEST-project recurring failures involved DNS resolution."
    )
    assert api.show(item.ref)["value"]["basis"] == "derived_summary"
    with core.storage.read(ctx) as tx:
        assert not tx.claims.unresolved_updates()


def test_review_pagination_continues_past_invalid_observations(app):
    from copy import deepcopy

    core, ctx = app
    populate(core, ctx)

    class Several(ReflectionModel):
        def judge(self, messages, *, remaining_seconds):
            raw = super().judge(messages, remaining_seconds=remaining_seconds)
            value = json.loads(raw)
            if "observations" in value:
                original = value["observations"][0]
                value["observations"] = [dict(deepcopy(original), predicate="recurrence " + str(i)) for i in range(3)]
            return json.dumps(value)

    run(core, ctx, Several())
    with core.storage.write(ctx) as tx:
        tx._check().execute("UPDATE observations SET signature='TEST-invalid' WHERE rowid>1")
    api = owner_api(core, ctx)
    first = api.quarantine(limit=1)
    assert first["next_before"] is not None
    second = api.quarantine(limit=1, before=first["next_before"])
    if not second["items"]:
        second = api.quarantine(limit=1, before=second["next_before"])
    assert second["items"] and second["items"][0]["category"] == "observation"


def test_historical_discovery_keeps_the_supervisor_awake_until_all_sources_are_seen(app):
    from scope_recall.core.reflection_storage import next_reflection_due

    core, ctx = app
    for index in range(11):
        capture(
            core, replace(ctx, session_id="TEST-old-" + str(index // 8)), "TEST-project encounters connection timeouts."
        )
    with core.storage.write(ctx) as tx:
        tx._check().execute("UPDATE work_items SET state='done'")
    model = ReflectionModel()
    run(core, ctx, model)
    with core.storage.read(ctx) as tx:
        due = next_reflection_due(tx, now=core.clock.utc_now())
        assert due > core.clock.utc_now()
    core.clock.now = due
    run(core, ctx, model)
    assert observation(core, ctx) is not None
    with core.storage.read(ctx) as tx:
        assert next_reflection_due(tx, now=core.clock.utc_now()) is None


def test_agent_revision_confirms_edits_and_retracts_without_changing_basis(app):
    from scope_recall.contracts import validate_capture
    from scope_recall.core.mutate import revise
    from v11_support import source_event

    core, ctx = app
    populate(core, ctx)
    run(core, ctx, ReflectionModel())
    item = observation(core, ctx)
    for raw, value in (
        (f"Confirm this observation {item.ref}.", {"confirmed": True}),
        (
            f"Correct {item.ref}: TEST-project has recurring DNS lookup failures.",
            "TEST-project has recurring DNS lookup failures.",
        ),
        (f"Withdraw {item.ref}.", None),
    ):
        # The tool is tested against a captured source before the optional
        # conversational correction fast path applies the same request.
        with core.storage.write(ctx) as tx:
            event = validate_capture(source_event(source_event_key=f"TEST-tool-{item.revision}", content=raw), ctx)
            saved = tx.sources.put_source(event, scope_id="TEST-scope", persisted_at=core.clock.utc_now())
            source = tx.source(saved.ref, saved.revision)
        receipt = revise(
            core.storage,
            core.clock,
            ctx,
            dict(
                protocol_version="1.1",
                target_ref=item.ref,
                expected_revision=item.revision,
                new_value=value,
                conditions=[],
                source_evidence_refs=[f"{source.ref}@{source.revision}"],
                valid_from=None,
            ),
        )
        with core.storage.read(ctx) as tx:
            item = tx.claims.version(receipt.items[0].ref, receipt.items[0].revision)
            assert item.payload["kind"] == "observation" and item.basis == "derived_summary"
    assert item.state == "retracted"


def test_personal_observations_keep_distinct_verified_principals(app):
    from scope_recall.contracts import TrustedSourcePrincipal

    core, ctx = app

    class Person(ReflectionModel):
        def judge(self, messages, *, remaining_seconds):
            raw = super().judge(messages, remaining_seconds=remaining_seconds)
            value = json.loads(raw)
            body = json.loads(messages[-1]["content"])
            if "observations" in value:
                value["observations"][0]["subject"] = body["subject"]
            return json.dumps(value)

    for name in ("TEST-one", "TEST-two"):
        person = replace(
            ctx,
            project_id=None,
            branch_id=None,
            source_principal=TrustedSourcePrincipal("human", "verified", "principal:human:" + name, "TEST-owner"),
        )
        populate(core, person)
        run(core, person, Person())
    with core.storage.read(replace(ctx, project_id=None, branch_id=None)) as tx:
        subjects = {
            tx.claims.version(*row).payload["subject"]
            for row in tx._check().execute("SELECT claim_ref,claim_revision FROM observations")
        }
    assert subjects == {"principal:human:TEST-one", "principal:human:TEST-two"}


def test_reflection_observations_are_excluded_from_session_brief(app):
    from scope_recall.core.session_brief import rebuild_scope

    core, ctx = app
    populate(core, ctx)
    run(core, ctx, ReflectionModel())
    item = observation(core, ctx)
    assert item is not None
    with core.storage.write(ctx) as tx:
        rebuild_scope(tx, "TEST-scope", core.clock.utc_now())
        manifest = json.loads(
            tx._check().execute("SELECT items_json FROM session_briefs WHERE scope_id=?", ("TEST-scope",)).fetchone()[0]
        )
    assert all(entry["ref"] != item.ref for entry in manifest)
    assert item.ref not in core.session_brief(ctx)["text"]
