"""Regression tests for health decisions over evidence, time, history and pagination."""

import hashlib
import json
import time

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core.claims import Qualification
from scope_recall.core.management_query import MemoryFilter
from scope_recall.core.reflection_validity import dependency_signature, reflection_live
from scope_recall.core.summary_validity import obsolete_ranges

from tests.contract.test_aliases import app
from tests.contract.test_claims import capture, draft
from tests.contract.test_memory_health import owner, separate_fact


def members(facts):
    return [
        dict(ref=span["source_ref"], revision=span["source_revision"], total=len(span["quote"]))
        for fact in facts
        for span in fact.payload["evidence_spans"]
    ]


def observations(core, ctx):
    versions = []
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
            manifest = [
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
                (version.ref, version.revision, json.dumps(manifest), dependency_signature(tx, manifest)),
            )
            versions.append(version)
    return versions


def test_merge_and_undo_preserve_valid_evidence_ranges(app):
    core, ctx = app
    first = separate_fact(core, ctx, "TEST-Object", "blue")
    second = separate_fact(core, ctx, "test-object", "blue")
    manifest = members((first, second))
    api = owner(app)
    with core.storage.read(ctx) as tx:
        assert obsolete_ranges(tx, manifest) == {}
    receipt = api.health_merge_facts(first.ref, first.revision, second.ref, second.revision)
    with core.storage.read(ctx) as tx:
        assert obsolete_ranges(tx, manifest) == {}
    api.health_undo(receipt["action_id"], receipt["versions"])
    with core.storage.read(ctx) as tx:
        assert obsolete_ranges(tx, manifest) == {}


def test_brief_tracks_health_merge_choice_and_correction(app):
    from scope_recall.core import lineage
    from scope_recall.core.session_brief import rebuild_scope

    core, ctx = app

    def decision(value, subject):
        source = capture(core, ctx, f"{subject} color {value}.")
        proposal = draft(source, value, subject=subject, predicate="color")
        with core.storage.write(ctx) as tx:
            return tx.claims.append(
                "TEST-scope",
                proposal,
                Qualification("active", "direct_report", "synthetic"),
                recorded_at=core.clock.utc_now(),
            )

    first = decision("blue", "TEST-project")
    with core.storage.write(ctx) as tx:
        rebuild_scope(tx, "TEST-scope", core.clock.utc_now())
    assert first.ref in core.session_brief(ctx)["text"]
    second = decision("blue", "test-project")
    with core.storage.write(ctx) as tx:
        rebuild_scope(tx, "TEST-scope", core.clock.utc_now())
    assert {item["ref"] for item in core.session_brief(ctx)["items"]} >= {first.ref, second.ref}
    api = owner(app)
    receipt = api.health_merge_facts(first.ref, first.revision, second.ref, second.revision)
    with core.storage.read(ctx) as tx:
        assert lineage.resolve_replacement(tx, second.ref, core.clock.utc_now()).ref == first.ref
    brief = core.session_brief(ctx)
    assert first.ref in brief["text"]
    assert second.ref not in brief["text"]
    assert brief["items"][0]["revision"] == receipt["versions"][first.ref]
    prepared = core.session_brief(ctx, deliver=True, defer_confirmation=True)
    capture(core, ctx, "TEST unrelated source changes the authority snapshot.")
    assert core.confirm_session_brief(ctx, prepared, remaining_seconds=1) == "brief_delivered"
    with core.storage.write(ctx) as tx:
        rebuild_scope(tx, "TEST-scope", core.clock.utc_now())
        span = second.payload["evidence_spans"][0]
        tx._check(write=True).execute(
            "INSERT INTO source_safety(source_ref,source_revision,state,rules_json,scanned_at) VALUES(?,?,?,?,?)",
            (span["source_ref"], span["source_revision"], "quarantined", '["TEST-held"]', core.clock.utc_now()),
        )
        assert rebuild_scope(tx, "TEST-scope", core.clock.utc_now())
    assert first.ref not in core.session_brief(ctx)["text"]
    with core.storage.write(ctx) as tx:
        tx._check(write=True).execute(
            "DELETE FROM source_safety WHERE source_ref=? AND source_revision=?",
            (span["source_ref"], span["source_revision"]),
        )
        assert rebuild_scope(tx, "TEST-scope", core.clock.utc_now())
    assert first.ref in core.session_brief(ctx)["text"]
    corrected = api.revise(first.ref, expected_revision=receipt["versions"][first.ref], value="green")
    assert first.ref not in core.session_brief(ctx)["text"]
    with core.storage.write(ctx) as tx:
        rebuild_scope(tx, "TEST-scope", core.clock.utc_now())
    assert "green" in core.session_brief(ctx)["text"]
    third = decision("red", "Test-project")
    with core.storage.write(ctx) as tx:
        rebuild_scope(tx, "TEST-scope", core.clock.utc_now())
    api.health_choose_fact(third.ref, third.revision, first.ref, corrected["item"]["revision"])
    with core.storage.read(ctx) as tx:
        assert lineage.resolve_replacement(tx, first.ref, core.clock.utc_now()).ref == third.ref
    assert first.ref not in core.session_brief(ctx)["text"]
    with core.storage.write(ctx) as tx:
        rebuild_scope(tx, "TEST-scope", core.clock.utc_now())
    brief = core.session_brief(ctx)
    assert third.ref in brief["text"] and first.ref not in brief["text"]


def test_equal_values_different_validity_are_not_mergeable(app):
    core, ctx = app
    first = separate_fact(core, ctx, "TEST-Object", "blue", valid_to="2026-11-03T00:00:00Z")
    second = separate_fact(core, ctx, "test-object", "blue", valid_to="2026-12-01T00:00:00Z")
    api = owner(app)
    page = api.health(limit=100, deadline_seconds=5)
    assert not any({first.ref, second.ref} == {v["ref"] for v in item["facts"]} for item in page["duplicates"])
    with pytest.raises(ContractError, match="valid"):
        api.health_merge_facts(first.ref, first.revision, second.ref, second.revision)


def test_undo_observation_does_not_reauthorize_changed_dependencies(app):
    core, ctx = app
    fact = separate_fact(core, ctx, "TEST-Object", "blue")
    first, second = observations(core, ctx)
    # Attach an existing fact to the original observation's member so its later
    # correction changes that observation's dependency signature.
    with core.storage.write(ctx) as tx:
        row = (
            tx._check()
            .execute(
                "SELECT manifest_json FROM observations WHERE claim_ref=? AND claim_revision=?",
                (first.ref, first.revision),
            )
            .fetchone()
        )
        manifest = json.loads(row[0])
        source = tx.source(fact.payload["evidence_spans"][0]["source_ref"], 1)
        position = tx._check().execute("SELECT rowid FROM source_events WHERE event_id=?", (source.ref,)).fetchone()[0]
        manifest.append(
            dict(
                ref=source.ref,
                revision=1,
                position=position,
                digest=hashlib.sha256(source.event["content"].encode()).hexdigest(),
                total=len(source.event["content"]),
                session_id=source.session_id,
                entry_id=source.entry_id,
                occurred_at=source.event["occurred_at"],
            )
        )
        tx._check(write=True).execute(
            "UPDATE observations SET manifest_json=?,signature=? WHERE claim_ref=? AND claim_revision=?",
            (json.dumps(manifest), dependency_signature(tx, manifest), first.ref, first.revision),
        )
    with core.storage.read(ctx) as tx:
        original_signature = (
            tx._check()
            .execute(
                "SELECT signature FROM observations WHERE claim_ref=? AND claim_revision=?", (first.ref, first.revision)
            )
            .fetchone()[0]
        )
    api = owner(app)
    receipt = api.health_merge_facts(first.ref, first.revision, second.ref, second.revision)
    api.revise(fact.ref, expected_revision=fact.revision, value="green")
    undo = api.health_undo(receipt["action_id"], receipt["versions"])
    with core.storage.read(ctx) as tx:
        restored = tx.claims.version(first.ref, undo["versions"][first.ref])
        signature = (
            tx._check()
            .execute(
                "SELECT signature FROM observations WHERE claim_ref=? AND claim_revision=?",
                (first.ref, restored.revision),
            )
            .fetchone()[0]
        )
        assert signature == original_signature
        assert not reflection_live(tx, restored)


def test_superseded_observation_remains_inspectable_and_exportable(app, tmp_path):
    core, ctx = app
    first, second = observations(core, ctx)
    api = owner(app)
    api.health_merge_facts(first.ref, first.revision, second.ref, second.revision)
    inspected = api.show(second.ref, revision=second.revision)
    assert inspected.get("historical") is True
    with api.export_reader(MemoryFilter()) as reader:
        records = [(entry.current, list(entry.history)) for entry in reader.iter_claims()]
    assert any(
        any(
            v["ref"] == second.ref and v["revision"] == second.revision and v["kind"] == "fact_history" for v in history
        )
        for _, history in records
    )

    from scope_recall.maintenance.memory_export import write_export

    out = tmp_path / "TEST-observation-export"
    with api.export_reader(MemoryFilter()) as reader:
        write_export(reader, out)
    history = [json.loads(line) for line in (out / "fact_history.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(row["ref"] == second.ref and row["revision"] == second.revision for row in history)
    assert "Non-current version" in (out / "readable" / "facts.md").read_text(encoding="utf-8")


def test_replacement_chain_follows_choices_merges_and_undo(app):
    from scope_recall.core import lineage

    core, ctx = app
    a = separate_fact(core, ctx, "TEST-Object", "red")
    b = separate_fact(core, ctx, "test-object", "blue")
    c = separate_fact(core, ctx, "TeSt-Object", "blue")
    api = owner(app)
    first = api.health_choose_fact(b.ref, b.revision, a.ref, a.revision)
    second = api.health_merge_facts(c.ref, c.revision, b.ref, first["versions"][b.ref])
    with core.storage.read(ctx) as tx:
        assert lineage.resolve_replacement(tx, a.ref, core.clock.utc_now()).ref == c.ref
        assert lineage.resolve_replacement(tx, b.ref, core.clock.utc_now()).ref == c.ref
    api.health_undo(second["action_id"], second["versions"])
    with core.storage.read(ctx) as tx:
        assert lineage.resolve_replacement(tx, a.ref, core.clock.utc_now()).ref == b.ref


def test_replacement_cycle_is_rejected(app):
    from scope_recall.core import lineage

    core, ctx = app
    a = separate_fact(core, ctx, "TEST-Object", "red")
    b = separate_fact(core, ctx, "test-object", "blue")
    api = owner(app)
    receipt = api.health_choose_fact(b.ref, b.revision, a.ref, a.revision)
    with core.storage.write(ctx) as tx:
        tx._check(write=True).execute(
            "UPDATE claim_versions SET state='superseded',qualification_reason='health_superseded' "
            "WHERE claim_id=? AND revision=?",
            (b.ref, b.revision),
        )
        targets = [
            dict(ref=b.ref, revision=b.revision, replacement_ref=a.ref, replacement_revision=receipt["versions"][a.ref])
        ]
        tx._check(write=True).execute(
            "INSERT INTO management_actions VALUES (?, 'local_user_management', ?, ?, ?, ?)",
            ("TEST-cycle", "health_fact_choose", json.dumps(targets), core.clock.utc_now(), tx.status().memory_epoch),
        )
    with core.storage.read(ctx) as tx, pytest.raises(ContractError):
        lineage.resolve_replacement(tx, a.ref, core.clock.utc_now())


def test_slow_log_scan_leaves_time_for_paginated_findings(app, monkeypatch):
    from scope_recall.core import health

    core, ctx = app
    separate_fact(core, ctx, "TEST-Object", "blue")
    separate_fact(core, ctx, "test-object", "blue")
    separate_fact(core, ctx, "TeSt-Object", "red")

    def slow_log(core, deadline):
        while time.monotonic() < deadline:
            pass
        return dict(
            days=30, start=None, end=core.clock.utc_now(), incomplete=True, eligible=False, used=set(), timed_out=True
        )

    monkeypatch.setattr(health, "observation", slow_log)
    api = owner(app)
    cursor = None
    found = set()
    for _ in range(12):
        page = api.health(limit=1, cursor=cursor, deadline_seconds=0.5)
        found.update(category for category in ("duplicates", "contradictions") if page[category])
        assert not page["unused_complete"]
        next_cursor = page["next_cursor"]
        assert next_cursor != cursor or next_cursor is None
        cursor = next_cursor
        if cursor is None:
            break
    assert found == {"duplicates", "contradictions"}


def test_two_choices_keep_earlier_source_values_historical(app):
    from scope_recall.core import lineage

    from tests.v11_support import recall_request

    core, ctx = app
    a = separate_fact(core, ctx, "TEST-Object", "red")
    b = separate_fact(core, ctx, "test-object", "blue")
    c = separate_fact(core, ctx, "TeSt-Object", "green")
    api = owner(app)
    first = api.health_choose_fact(b.ref, b.revision, a.ref, a.revision)
    second = api.health_choose_fact(c.ref, c.revision, b.ref, first["versions"][b.ref])
    with core.storage.read(ctx) as tx:
        assert lineage.resolve_replacement(tx, a.ref, core.clock.utc_now()).ref == c.ref
        assert lineage.resolve_replacement(tx, b.ref, core.clock.utc_now()).ref == c.ref
    packet = core.recall_packet(ctx, recall_request(query="TEST-Object color red blue green", mode="current"))
    assert any("green" in item["content"].lower() for item in packet["items"])
    assert all(
        item["temporal_status"] != "current"
        for item in packet["items"]
        if "color red" in item["content"].lower() or "color blue" in item["content"].lower()
    )
    api.health_undo(second["action_id"], second["versions"])
    with core.storage.read(ctx) as tx:
        assert lineage.resolve_replacement(tx, a.ref, core.clock.utc_now()).ref == b.ref


def test_fact_statement_identity_ignores_nonapplicability_metadata(app):
    from copy import deepcopy

    from scope_recall.core.claims import statement_key

    core, ctx = app
    fact = separate_fact(core, ctx, "TEST-Object", "blue")
    changed = deepcopy(fact.payload)
    changed["statement_kind"] = "decision"
    changed["occurred_at"] = "2026-09-02T00:00:00Z"
    assert statement_key(changed) == statement_key(fact.payload)


def test_replacement_chain_does_not_disclose_muted_target(app):
    from scope_recall.core import lineage

    core, ctx = app
    a = separate_fact(core, ctx, "TEST-Object", "red")
    b = separate_fact(core, ctx, "test-object", "blue")
    api = owner(app)
    api.health_choose_fact(b.ref, b.revision, a.ref, a.revision)
    api.apply(api.preview([b.ref], mode="suppress"))
    with core.storage.read(ctx) as tx:
        assert lineage.resolve_replacement(tx, a.ref, core.clock.utc_now()) is None


def test_historical_observation_does_not_bypass_private_mode(app):
    core, ctx = app
    first, second = observations(core, ctx)
    api = owner(app)
    api.health_merge_facts(first.ref, first.revision, second.ref, second.revision)
    api.private_on(target="session", session=ctx.session_id, mode="full")
    with pytest.raises(ContractError):
        api.show(second.ref, revision=second.revision)
    with api.export_reader(MemoryFilter()) as reader:
        records = [(entry.current, list(entry.history)) for entry in reader.iter_claims()]
    assert not any(any(v["ref"] == second.ref for v in history) for _, history in records)


def test_replacement_depth_limit_fails_closed(app, monkeypatch):
    from scope_recall.core import lineage

    core, ctx = app
    a = separate_fact(core, ctx, "TEST-Object", "red")
    b = separate_fact(core, ctx, "test-object", "blue")
    owner(app).health_choose_fact(b.ref, b.revision, a.ref, a.revision)
    monkeypatch.setattr(lineage, "MAX_REPLACEMENT_DEPTH", 1)
    with core.storage.read(ctx) as tx, pytest.raises(ContractError, match="replacement_depth"):
        lineage.resolve_replacement(tx, a.ref, core.clock.utc_now())
