"""Deletion, historical proofs, catalogue readiness and frozen recall identity."""

import hashlib
import sqlite3

import pytest
from scope_recall.core.claims import claim_slot
from scope_recall.core.entity_names import grams
from scope_recall.core.entity_resolution import entity_progress, queue_entity_backfill
from scope_recall.core.read_views import resolve_subject
from scope_recall.core.worker_projection import process_rebuild_projection

from tests.contract.test_aliases import app
from tests.contract.test_claims import capture
from tests.contract.test_entity_resolution import RecordedModel, fact, merge, recall_signature, withdraw
from tests.contract.test_entity_scaling import revise_endpoint
from tests.v11_support import downgrade_store, source_event


def _proposal(name, predicate, value):
    return dict(
        kind="fact",
        subject=name,
        predicate=predicate,
        value_text=value,
        conditions=[],
        statement_kind="assertion",
        valid_from=None,
        valid_to=None,
        evidence_spans=[],
    )


def reversed_pair(core, ctx):
    """Admit the older identity first while its claim id sorts after its later spelling variant."""
    scope = next(iter(ctx.allowed_scope_ids))

    def ref(payload):
        slot = claim_slot(scope, ctx.project_id, ctx.branch_id, payload)
        return "claim-" + hashlib.sha256((ctx.binding.installation_id + ":" + slot).encode()).hexdigest()

    for number in range(1000):
        target = _proposal(f"TEST-Paterson-{number}", "category", "person")
        source = _proposal(f"TEST-Patterson-{number}", "home", "TEST-Harbor")
        if ref(source) < ref(target):
            break
    else:
        raise AssertionError("synthetic reverse ordering not found")
    versions = []
    for payload in (target, source):
        quote = f"{payload['subject']} {payload['predicate']} {payload['value_text']}."
        event = source_event(source_event_key="TEST-catalog/" + payload["subject"], content=quote)
        receipt = core.record_event(ctx, event, scope_id=scope, remaining_seconds=10)
        span = dict(source_ref=receipt.event_refs[0].ref, source_revision=1, quote=quote)
        payload["evidence_spans"] = [span]
        result = core.records.accept_claim_proposals(
            ctx,
            dict(
                protocol_version="1.1",
                source_refs=[span["source_ref"] + "@1"],
                claim_proposals=[payload],
                resume_proposals=[],
                reference_proposals=[],
            ),
            scope_id=scope,
            remaining_seconds=10,
        )
        assert result.items[0].state == "active"
        versions.append(core.current_claim(ctx, result.items[0].ref))
    assert versions[1].ref < versions[0].ref
    return tuple(versions)


def finish_catalog(core, ctx, model, *, page=1):
    """Drain bounded index and judgement pages through the real projection processor."""
    for _ in range(12):
        with core.storage.write(ctx) as tx:
            queue_entity_backfill(tx, now=core.clock.utc_now(), limit=page)
            jobs = tx.work.claim_next(
                "TEST-catalog",
                core.clock.utc_now(),
                lease_seconds=60,
                limit=8,
                allowed_work_types=frozenset({"rebuild_projection"}),
            )
        for job in jobs:
            outcome = process_rebuild_projection(
                core.storage,
                core.clock,
                ctx,
                job,
                model=model,
                started=core.clock.monotonic(),
                budget=10,
            )
            assert outcome[2] in {"done", "obsolete"} or outcome[1] == "entity_index_pending", outcome
        with core.storage.read(ctx) as tx:
            if entity_progress(tx)["pending"] == 0:
                return
    raise AssertionError("synthetic catalogue did not finish")


@pytest.mark.parametrize("shared", [False, True])
def test_purge_removes_name_mentions_and_fts_unless_another_fact_supports_them(app, shared):
    core, ctx = app
    name = "TEST-Obliterate-XZQ"
    target = fact(core, ctx, "TEST-Anchor-BRM", "category", "person")
    source = fact(core, ctx, name, "home", "TEST-Harbor")
    other = fact(core, ctx, name, "color", "blue") if shared else None
    merge(core, ctx, source, target)
    capture(core, ctx, f"删除 {source.ref}。")
    operation = core.forget(
        ctx,
        dict(
            protocol_version="1.1",
            target_refs=[source.ref],
            mode="delete",
            expected_revisions={source.ref: source.revision},
        ),
        remaining_seconds=10,
    )
    with core.storage.read(ctx) as tx:
        assert tx.claims.version(source.ref, source.revision) is None
        assert source.ref not in tx.claims._entity_lookup("TEST-scope", name + "x", (), "")
        assert resolve_subject(tx, name, core.clock.utc_now()).status == "literal"
    receipt = core.operations.purge_sqlite(ctx, operation["operation_id"], remaining_seconds=10)
    with sqlite3.connect(core.storage.path) as conn:
        assert not conn.execute("SELECT 1 FROM entity_name_mentions WHERE claim_ref=?", (source.ref,)).fetchone()
        names = conn.execute("SELECT name FROM entity_names WHERE name=?", (name,)).fetchall()
        assert names == ([(name,)] if shared else [])
        conn.execute("CREATE VIRTUAL TABLE temp.name_terms USING fts5vocab(main,entity_name_search,row)")
        term = grams("XZQ")[0]
        assert bool(conn.execute("SELECT 1 FROM name_terms WHERE term=?", (term,)).fetchone()) == shared
        if other:
            assert conn.execute("SELECT claim_ref FROM entity_name_mentions WHERE claim_ref=?", (other.ref,)).fetchone()
    expected = "shared_authorized_copy_retained" if shared else "removed"
    assert receipt["layers"]["entity_name_fts"] == expected
    assert core.operations.purge_sqlite(ctx, operation["operation_id"], remaining_seconds=10) == receipt


def test_source_revision_refresh_fences_only_the_accepted_alias_revision(app):
    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "species", "cat")
    source = fact(core, ctx, "TEST-Ricecake", "home", "TEST-Harbor")
    alias = merge(core, ctx, source, target)
    revised = revise_endpoint(core, ctx, source)
    refreshed = merge(core, ctx, revised, target)
    assert refreshed.ref == alias.ref and refreshed.revision > alias.revision
    with core.storage.read(ctx) as tx:
        resolution = resolve_subject(tx, "TEST-Ricecake", core.clock.utc_now())
        assert (source.ref, source.revision) not in {(item.ref, item.revision) for item in resolution.fence}
    assert any(ref == target.ref for ref, _, _ in recall_signature(core, ctx, "TEST-Ricecake species"))


def test_index_backfill_precedes_judgement_in_bounded_pages(app):
    core, ctx = app
    reversed_pair(core, ctx)
    for number in range(8):
        fact(core, ctx, f"TEST-catalog-filler-{number}", "category", "object")
    with core.storage.write(ctx) as tx:
        tx._check(write=True).execute("UPDATE work_items SET state='done'")
    downgrade_store(core.storage.path, 1112)
    core.storage.initialize()
    with core.storage.write(ctx) as tx:
        assert queue_entity_backfill(tx, now=core.clock.utc_now(), limit=1) == 1
        jobs = tx.work.claim_next(
            "TEST-catalog",
            core.clock.utc_now(),
            lease_seconds=60,
            limit=1,
            allowed_work_types=frozenset({"rebuild_projection"}),
        )
    model = RecordedModel()
    outcome = process_rebuild_projection(
        core.storage,
        core.clock,
        ctx,
        jobs[0],
        model=model,
        started=core.clock.monotonic(),
        budget=10,
    )
    assert outcome == ("deferred", "entity_index_pending", "pending")
    assert not model.calls
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM entity_name_mentions").fetchone()[0] == 8
        assert entity_progress(tx)["completed"] == 0
        assert tx._check().execute("SELECT count(*) FROM entity_names WHERE status='checked'").fetchone()[0] == 0


def test_legacy_backfill_finds_older_identity_with_reversed_claim_ids_and_no_vectors(app):
    core, ctx = app
    target, source = reversed_pair(core, ctx)
    with core.storage.write(ctx) as tx:
        tx._check(write=True).execute("UPDATE work_items SET state='done'")
    downgrade_store(core.storage.path, 1112)
    core.storage.initialize()
    model = RecordedModel()
    finish_catalog(core, ctx, model)
    with core.storage.read(ctx) as tx:
        assert resolve_subject(tx, source.payload["subject"], core.clock.utc_now()).subject == target.payload["subject"]
    assert model.calls and any(candidate["ref"] == target.ref for candidate in model.calls[0]["candidates"])


def test_packet_freezes_negative_identity_in_the_existing_private_read(app, monkeypatch):
    from scope_recall.core import entity_queries, private

    core, ctx = app
    target = fact(core, ctx, "TEST-Nian", "species", "cat")
    source = fact(core, ctx, "TEST-Ricecake", "home", "TEST-Harbor")
    mapping, state = entity_queries._query_mapping, private.recall_state
    reads, private_reads, aliases, searches = [], [], [], []

    def private_state(tx, *args, **kwargs):
        private_reads.append(tx)
        return state(tx, *args, **kwargs)

    def map_then_create(tx, *args, **kwargs):
        reads.append(tx)
        result = mapping(tx, *args, **kwargs)
        if len(reads) == 1:
            aliases.append(merge(core, ctx, source, target))
        return result

    pipeline = type(core.recall_pipeline)
    prefetch = pipeline._prefetch_query

    def observe_search(self, context):
        searches.append(context)
        return prefetch(self, context)

    compiler = type(core.recall_packet_compiler)
    compile_packet = compiler.compile

    def withdraw_before_compile(self, context, result, storage):
        withdraw(core, ctx, aliases[0])
        return compile_packet(self, context, result, storage)

    monkeypatch.setattr(private, "recall_state", private_state)
    monkeypatch.setattr(entity_queries, "_query_mapping", map_then_create)
    monkeypatch.setattr(pipeline, "_prefetch_query", observe_search)
    monkeypatch.setattr(compiler, "compile", withdraw_before_compile)
    recall_signature(core, ctx, "TEST-Ricecake species")
    assert len(reads) == 1
    assert reads[0] is private_reads[0]
    assert searches and all(item.query == "TEST-Ricecake species" for item in searches)
    assert all(getattr(item, "entity_names_resolved", False) and not item.entity_fence for item in searches)


@pytest.mark.parametrize("retracted", [False, True])
def test_purge_requires_a_current_name_mention_even_when_an_alias_row_survives(app, retracted):
    from copy import deepcopy

    from scope_recall.core.claims import Qualification
    from scope_recall.core.entity_resolution import publish_entity_merge
    from scope_recall.core.evidence_support import SupportVerdict

    from tests.contract.test_entity_queries import normalized

    core, ctx = app
    name = "TEST-Old-XZQ"
    source = normalized(
        core,
        ctx,
        f"TEST-Source-BRM category cat. TEST-Source-BRM is also called {name}.",
        value="cat",
        kind="fact",
        subject="TEST-Source-BRM",
        predicate="category",
        statement_kind="assertion",
        entity_names=[name],
    )
    with core.storage.write(ctx) as tx:
        alias = publish_entity_merge(
            tx,
            source,
            source,
            SupportVerdict("", "supported", source.ref, source.revision, "duplicate"),
            now=core.clock.utc_now(),
            name=name,
        )
    other = fact(core, ctx, name, "home", "TEST-Harbor")
    if retracted:
        withdraw(core, ctx, alias)
    proposal = deepcopy(source.payload)
    proposal["entity_names"] = []
    with core.storage.write(ctx) as tx:
        changed = tx.claims.append(
            source.scope_id,
            proposal,
            Qualification("active", "direct_report", "source_names_changed"),
            recorded_at=core.clock.utc_now(),
            previous=source,
        )
        assert changed.revision > source.revision and not changed.payload["entity_names"]
    capture(core, ctx, f"删除 {other.ref}。")
    operation = core.forget(
        ctx,
        dict(
            protocol_version="1.1",
            target_refs=[other.ref],
            mode="delete",
            expected_revisions={other.ref: other.revision},
        ),
        remaining_seconds=10,
    )
    receipt = core.operations.purge_sqlite(ctx, operation["operation_id"], remaining_seconds=10)
    assert receipt["layers"]["entity_name_fts"] == "removed"
    with core.storage.read(ctx) as tx:
        assert not tx._check().execute("SELECT 1 FROM entity_names WHERE name=?", (name,)).fetchone()
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("CREATE VIRTUAL TABLE temp.name_terms USING fts5vocab(main,entity_name_search,row)")
        assert not conn.execute("SELECT 1 FROM name_terms WHERE term=?", (grams("XZQ")[0],)).fetchone()
    fact(core, ctx, name, "color", "blue")
    finish_catalog(core, ctx, RecordedModel())
    with core.storage.read(ctx) as tx:
        result = resolve_subject(tx, name, core.clock.utc_now())
        assert result.status == ("literal" if retracted else "resolved")
        if retracted:
            assert tx.claims.version(alias.ref, alias.revision).state == "active"
            assert tx.claims.versions(alias.ref)[-1].state == "retracted"


@pytest.mark.parametrize("endpoint", ["source", "target", "alias"])
def test_purge_collects_cached_alias_names_after_all_mentions_disappear(app, endpoint):
    from copy import deepcopy

    from scope_recall.core.claims import Qualification
    from scope_recall.core.entity_resolution import publish_entity_merge
    from scope_recall.core.evidence_support import SupportVerdict

    from tests.contract.test_entity_queries import normalized

    core, ctx = app
    name = "TEST-Cached-XZQ"
    target = fact(core, ctx, "TEST-Target-BRM", "category", "person")
    source = normalized(
        core,
        ctx,
        f"TEST-Source-PQL category person. TEST-Source-PQL is called {name}, the same person as TEST-Target-BRM.",
        value="person",
        kind="fact",
        subject="TEST-Source-PQL",
        predicate="category",
        statement_kind="assertion",
        entity_names=[name],
    )
    with core.storage.write(ctx) as tx:
        alias = publish_entity_merge(
            tx,
            source,
            target,
            SupportVerdict("", "supported", target.ref, target.revision, "duplicate"),
            now=core.clock.utc_now(),
            name=name,
        )
        proposal = deepcopy(source.payload)
        proposal["entity_names"] = []
        source = tx.claims.append(
            source.scope_id,
            proposal,
            Qualification("active", "direct_report", "source_names_changed"),
            recorded_at=core.clock.utc_now(),
            previous=source,
        )
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT 1 FROM entity_names WHERE name=?", (name,)).fetchone()
        assert (
            not tx._check()
            .execute(
                "SELECT 1 FROM entity_name_mentions m JOIN entity_names n USING(name_id) WHERE n.name=?",
                (name,),
            )
            .fetchone()
        )
    victim = {"source": source, "target": target, "alias": alias}[endpoint]
    capture(core, ctx, f"删除 {victim.ref}。")
    operation = core.forget(
        ctx,
        dict(
            protocol_version="1.1",
            target_refs=[victim.ref],
            mode="delete",
            expected_revisions={victim.ref: victim.revision},
        ),
        remaining_seconds=10,
    )
    core.operations.purge_sqlite(ctx, operation["operation_id"], remaining_seconds=10)
    with core.storage.read(ctx) as tx:
        assert not tx._check().execute("SELECT 1 FROM entity_names WHERE name=?", (name,)).fetchone()


def test_entity_worker_waits_for_paused_source_entry(app):
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    core, ctx = app
    item = fact(core, ctx, "TEST-EntryEntity", "home", "TEST-Harbor")
    owner = MemoryManagement(core, InstallationMaintenance(ctx))
    owner.set_entry_state("local", "paused", actor="TEST-owner")
    with core.storage.write(ctx) as tx:
        queue_entity_backfill(tx, now=core.clock.utc_now(), limit=10)
        jobs = tx.work.claim_next(
            "TEST-entry-entity",
            core.clock.utc_now(),
            lease_seconds=60,
            limit=8,
            allowed_work_types=frozenset({"rebuild_projection"}),
        )
    job = next(job for job in jobs if job.subject_ref == item.ref)
    outcome = process_rebuild_projection(
        core.storage, core.clock, ctx, job, model=RecordedModel(), started=core.clock.monotonic(), budget=10
    )
    assert outcome[1] == "entry_paused" and outcome[2] == "pending"
