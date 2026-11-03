"""Focused regressions for bounded selection; no model or host calls."""

import hashlib
import itertools
import json
import sqlite3
from dataclasses import replace

import pytest
from scope_recall.contracts import TrustedSourcePrincipal
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.background_context import background_candidates
from scope_recall.core.recall import RetrievalPipeline
from scope_recall.core.recall_packet import render_recall_packet_context
from scope_recall.core.retrieval import CandidateRef, RetrievedObject, SearchContext, SearchLimits

from tests.contract.test_claims import Clock, accept, app, capture, draft
from tests.contract.test_shared_store import shared_binding, shared_context
from tests.v11_support import recall_request, source_event


def _search(core, ctx, query):
    return SearchContext.from_request(
        recall_request(query=query), ctx, now=core.clock.utc_now(), deadline=core.clock.monotonic() + 5
    )


@pytest.mark.parametrize("operation", ["INSERT", "INSERT OR REPLACE"])
def test_copied_body_verdict_does_not_attest_a_rewritten_original(app, operation):
    from scope_recall.core import model_content

    core, ctx = app
    source = capture(core, ctx, "TEST palette amber")
    body = "api_key: TEST9-secret-value"
    with core.storage.write(ctx) as tx:
        conn = tx._check(write=True)
        row = dict(conn.execute("SELECT * FROM source_events WHERE event_id=?", (source.ref,)).fetchone())
        if operation == "INSERT":
            row.update(
                event_id="event-TEST-copied-verdict",
                source_id=row["source_id"] + 1,
                source_event_key="TEST-copied-verdict",
                source_group_key="TEST-copied-verdict",
            )
        row.update(content=body, content_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest())
        conn.execute(
            f"{operation} INTO source_events ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
            tuple(row.values()),
        )
    with core.storage.read(ctx) as tx:
        obj = core.recall_pipeline.storage_reader.hydrate(
            tx, CandidateRef("event", row["event_id"], 1, "lexical"), _search(core, ctx, "TEST palette")
        )
    assert obj is not None
    model_content._retrieved_pattern_names.cache_clear()
    assert model_content.stored_item_pattern_names(obj) == ("api_key_assignment",)
    gaps = []
    assert model_content.withhold_secret_items([obj], gaps) == []
    assert gaps == ["secrets_withheld:1"]


@pytest.mark.parametrize("receipt", [None, "corrected", "old_rules", "wrong_input"])
def test_release_cache_recognizes_revoked_or_corrected_body_receipts(receipt):
    from scope_recall.core import model_content
    from scope_recall.core.secret_patterns import SECRET_RULES_VERSION

    body = "api_key: TEST9-secret-value"
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    obj = RetrievedObject(
        "event-TEST-cache-revocation",
        1,
        "event",
        body,
        "human_direct",
        "current",
        "TEST scope",
        (),
        "direct_report",
        True,
        content_secret_scan=(SECRET_RULES_VERSION, digest, ()),
    )
    model_content._retrieved_pattern_names.cache_clear()
    assert model_content.stored_item_pattern_names(obj) == ()
    revised = {
        None: None,
        "corrected": (SECRET_RULES_VERSION, digest, ("api_key_assignment",)),
        "old_rules": ("TEST-old-rules", digest, ()),
        "wrong_input": (SECRET_RULES_VERSION, "0" * 64, ()),
    }[receipt]
    changed = replace(obj, content_secret_scan=revised)
    assert model_content.stored_item_pattern_names(changed) == ("api_key_assignment",)
    gaps = []
    assert model_content.withhold_secret_items([changed], gaps) == []
    assert gaps == ["secrets_withheld:1"]


def test_first_release_reuses_the_persisted_body_verdict_but_scans_metadata(app, monkeypatch):
    from scope_recall.core import model_content

    core, ctx = app
    source = capture(core, ctx, "TEST palette amber. " * 2000)
    with core.storage.read(ctx) as tx:
        obj = core.recall_pipeline.storage_reader.hydrate(
            tx, CandidateRef("event", source.ref, 1, "lexical"), _search(core, ctx, "palette amber")
        )
    calls = []
    original = model_content.secret_structure_pattern_names

    def counted(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(model_content, "secret_structure_pattern_names", counted)
    model_content._retrieved_pattern_names.cache_clear()
    assert model_content.withhold_secret_items([obj], []) == [obj]
    assert calls and all(source.event["content"] not in str(value) for value in calls)
    unsafe_metadata = replace(
        obj, metadata=(*obj.metadata, ("source_context", json.dumps({"api_key": "TEST9-secret-value"})))
    )
    gaps = []
    assert model_content.withhold_secret_items([unsafe_metadata], gaps) == []
    assert gaps == ["secrets_withheld:1"]


@pytest.mark.parametrize("verdict", ["missing", "old_rules", "changed_body"])
def test_body_verdict_fallback_preserves_secret_withholding(app, monkeypatch, verdict):
    from scope_recall.core import model_content

    core, ctx = app
    source = capture(core, ctx, "TEST palette amber.")
    with core.storage.read(ctx) as tx:
        obj = core.recall_pipeline.storage_reader.hydrate(
            tx, CandidateRef("event", source.ref, 1, "lexical"), _search(core, ctx, "palette amber")
        )
    if verdict == "missing":
        obj = replace(obj, content_secret_scan=None)
    elif verdict == "old_rules":
        monkeypatch.setattr(model_content, "SECRET_RULES_VERSION", "TEST-next-rules")
    else:
        obj = replace(obj, content="api_key: TEST9-secret-value")
    calls = []
    original = model_content.secret_structure_pattern_names

    def counted(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(model_content, "secret_structure_pattern_names", counted)
    model_content._retrieved_pattern_names.cache_clear()
    gaps = []
    kept = model_content.withhold_secret_items([obj], gaps)
    assert any(obj.content in str(value) for value in calls)
    assert bool(kept) == (verdict != "changed_body")
    assert gaps == ([] if kept else ["secrets_withheld:1"])


@pytest.mark.parametrize(
    "body,body_rules",
    [
        ("TEST amber palette", ()),
        ("api_key: <your-api-key>", ()),
        ("api_key: null", ()),
        ("api_key: TEST9-secret-value", ("api_key_assignment",)),
        ("password: TEST9-secret-value", ("api_key_assignment",)),
        ("api_key: TEST\u200b9-secret-value", ("api_key_assignment",)),
        ("TEST before password: TEST9-secret-value after", ("api_key_assignment",)),
    ],
)
@pytest.mark.parametrize("sensitive_metadata", [False, True])
def test_persisted_and_live_body_verdicts_withhold_identical_items(
    app, body, body_rules, sensitive_metadata, monkeypatch
):
    from scope_recall.core import model_content
    from scope_recall.core.source_storage import backfill_secret_scans

    core, ctx = app
    source = capture(core, ctx, "TEST palette amber")
    with core.storage.read(ctx) as tx:
        obj = core.recall_pipeline.storage_reader.hydrate(
            tx, CandidateRef("event", source.ref, 1, "lexical"), _search(core, ctx, "palette amber")
        )
    with core.storage.write(ctx) as tx:
        tx._check(write=True).execute(
            "UPDATE source_events SET content=?,content_sha256=? WHERE event_id=?",
            (body, hashlib.sha256(body.encode("utf-8")).hexdigest(), source.ref),
        )
        backfill_secret_scans(tx, now=core.clock.utc_now())
        row = (
            tx._check()
            .execute(
                "SELECT secret_scan_version,secret_scan_input_sha256,secret_scan_names "
                "FROM source_events WHERE event_id=?",
                (source.ref,),
            )
            .fetchone()
        )
    metadata = (
        (*obj.metadata, ("source_context", json.dumps({"api_key": "TEST9-secret-value"})))
        if sensitive_metadata
        else obj.metadata
    )
    saved = replace(
        obj, content=body, metadata=metadata, content_secret_scan=(row[0], row[1], tuple(json.loads(row[2])))
    )
    live = replace(saved, content_secret_scan=None)
    calls = []
    original = model_content.secret_structure_pattern_names

    def counted(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(model_content, "secret_structure_pattern_names", counted)
    expected = ("api_key_assignment",) if sensitive_metadata else body_rules
    model_content._retrieved_pattern_names.cache_clear()
    assert model_content.stored_item_pattern_names(saved) == expected
    calls.clear()
    model_content._retrieved_pattern_names.cache_clear()
    assert model_content.stored_item_pattern_names(live) == expected
    assert any(isinstance(value, dict) and value.get("content") == body for value in calls)
    saved_gaps, live_gaps = [], []
    model_content._retrieved_pattern_names.cache_clear()
    saved_kept = model_content.withhold_secret_items([saved], saved_gaps)
    model_content._retrieved_pattern_names.cache_clear()
    live_kept = model_content.withhold_secret_items([live], live_gaps)
    assert bool(saved_kept) == bool(live_kept) == (not expected)
    assert [(item.ref, item.revision) for item in saved_kept] == [(item.ref, item.revision) for item in live_kept]
    assert saved_gaps == live_gaps == (["secrets_withheld:1"] if expected else [])


@pytest.mark.parametrize("case", ["pending_after_retraction", "procedure_changed", "valid_to_changed"])
def test_superseded_support_follows_truth_head_and_structured_assertion(app, case):
    from scope_recall.core.lineage import _withdrawn_support

    core, ctx = app
    source = capture(core, ctx, "TEST-project color amber.")
    item = accept(core, ctx, draft(source, "amber", predicate="color")).items[0]
    old = replace(core.current_claim(ctx, item.ref), current_revision=2)
    new = replace(old, revision=2, replaces_revision=1, state="retracted", reason="explicit_retraction")
    versions = (old, new)
    if case == "pending_after_retraction":
        versions += (replace(new, revision=3, replaces_revision=None, state="proposed"),)
    elif case == "procedure_changed":
        old = replace(old, payload={**old.payload, "kind": "procedure", "procedure": {"steps": ["old"]}})
        new = replace(new, state="active", payload={**old.payload, "procedure": {"steps": ["new"]}})
        versions = (old, new)
    else:
        new = replace(
            new,
            state="active",
            valid_to="2027-01-01T00:00:00Z",
            payload={**old.payload, "valid_to": "2027-01-01T00:00:00Z"},
        )
        versions = (old, new)
    assert _withdrawn_support(versions, 1, core.clock.utc_now())


def test_withheld_count_survives_the_public_gap_window(app):
    from scope_recall.core.recall_packet import public_gaps
    from scope_recall.core.retrieval_hydration import note_superseded_evidence

    core, ctx = app
    gaps = [f"TEST-gap-{i}" for i in range(16)]
    seen = set()
    with core.storage.read(ctx) as tx:
        tx.withheld_evidence.add(("TEST-original", 1))
        note_superseded_evidence(tx, gaps, seen)
        note_superseded_evidence(tx, gaps, seen)
    assert "superseded_evidence_withheld:1" in public_gaps(gaps)


@pytest.mark.parametrize("reason", ["adoption_duplicate", "canonical_frame_reconciled", "fact_entailment_unproved"])
def test_retracted_extraction_without_owner_withdrawal_keeps_its_original(app, reason):
    from scope_recall.core.claims import Qualification

    core, ctx = app
    source = capture(core, ctx, "TEST-project color amber.")
    item = accept(core, ctx, draft(source, "amber", predicate="color")).items[0]
    with core.storage.write(ctx) as tx:
        head = tx.claims.version(item.ref, 1)
        tx.claims.append(
            head.scope_id,
            head.payload,
            Qualification("retracted", "direct_report", reason),
            previous=head,
            expected_revision=1,
            recorded_at=core.clock.utc_now(),
        )
    with core.storage.read(ctx) as tx:
        obj = core.recall_pipeline.storage_reader.hydrate(
            tx, CandidateRef("event", source.ref, 1, "lexical"), _search(core, ctx, "TEST-project color")
        )
    assert obj is not None and obj.content == source.event["content"]


@pytest.mark.parametrize("value", ["silver", None])
@pytest.mark.parametrize("mode", ["auto", "current"])
def test_owner_replaced_evidence_is_withheld_only_from_current_content(app, value, mode):
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    core, ctx = app
    source = capture(core, ctx, "TEST-project color amber.")
    item = accept(core, ctx, draft(source, "amber", predicate="color")).items[0]
    management = MemoryManagement(core, InstallationMaintenance(ctx))
    management.revise(item.ref, expected_revision=1, value=value)
    candidate = CandidateRef("event", source.ref, 1, "lexical")
    reader = core.recall_pipeline.storage_reader
    search = replace(_search(core, ctx, "TEST-project color"), mode=mode)
    with core.storage.read(ctx) as tx:
        assert reader.hydrate(tx, candidate, search) is None
    packet = core.recall_packet(ctx, recall_request(query="TEST-project color", mode=mode), deadline_seconds=10)
    assert source.ref not in {obj["ref"] for obj in packet["items"]}
    assert any(gap.startswith("superseded_evidence_withheld:") for gap in packet["gaps"]), packet


def test_restoring_a_retired_extraction_does_not_turn_its_original_into_a_withdrawal(app):
    from scope_recall.core.claims import Qualification
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    core, ctx = app
    source = capture(core, ctx, "TEST-project color amber.")
    with core.storage.write(ctx) as tx:
        old = tx.claims.append(
            "TEST-scope",
            draft(source, "amber", predicate="color", statement_kind="assertion"),
            Qualification("retracted", "direct_report", "adoption_duplicate"),
            recorded_at=core.clock.utc_now(),
        )
    MemoryManagement(core, InstallationMaintenance(ctx)).revise(old.ref, expected_revision=1, value="amber")
    with core.storage.read(ctx) as tx:
        obj = core.recall_pipeline.storage_reader.hydrate(
            tx, CandidateRef("event", source.ref, 1, "lexical"), _search(core, ctx, "TEST-project color")
        )
    assert obj is not None and obj.content == source.event["content"]


def test_shared_current_evidence_and_unrelated_older_message_still_readable(app):
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    core, ctx = app
    source = capture(core, ctx, "TEST-project color amber and format Markdown.")
    unrelated = capture(core, ctx, "TEST-project color amber.")
    item = accept(core, ctx, draft(source, "amber", predicate="color")).items[0]
    accept(core, ctx, draft(source, "Markdown", predicate="format"))
    management = MemoryManagement(core, InstallationMaintenance(ctx))
    management.revise(item.ref, expected_revision=1, value="silver")
    with core.storage.read(ctx) as tx:
        for event in (source, unrelated):
            assert (
                core.recall_pipeline.storage_reader.hydrate(
                    tx, CandidateRef("event", event.ref, 1, "lexical"), _search(core, ctx, "TEST-project color")
                )
                is not None
            )


@pytest.mark.parametrize("mode", ["history", "as_of"])
def test_historical_packet_and_inspect_explain_superseded_source(app, mode):
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    core, ctx = app
    source = capture(core, ctx, "TEST-project color amber.")
    item = accept(core, ctx, draft(source, "amber", predicate="color")).items[0]
    MemoryManagement(core, InstallationMaintenance(ctx)).revise(item.ref, expected_revision=1, value="silver")
    request = recall_request(query="TEST-project color", mode=mode, max_items=30, budget_tokens=8000)
    if mode == "as_of":
        request["as_of"] = "2026-09-02T12:00:00Z"
    packet = core.recall_packet(ctx, request, deadline_seconds=10)
    event = next(obj for obj in packet["items"] if obj["ref"] == source.ref)
    assert event["content"] == source.event["content"]
    assert event["qualification_reason"] == "superseded_evidence"
    inspected = core.inspect_object(ctx, source.ref, 1)
    assert inspected.value.event == source.event
    assert inspected.historical
    assert f"superseded_evidence:{item.ref}@1" in inspected.gaps


def test_compile_rechecks_owner_correction_and_keeps_unique_withholding_count(app):
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.recall_packet import RecallPacketCompiler, bounded_packet, envelope_tokens
    from scope_recall.core.restore import InstallationMaintenance

    core, ctx = app
    source = capture(core, ctx, "TEST-project color amber.")
    item = accept(core, ctx, draft(source, "amber", predicate="color")).items[0]
    request = recall_request(query="TEST-project color")
    result = core.recall(ctx, request)
    MemoryManagement(core, InstallationMaintenance(ctx)).revise(item.ref, expected_revision=1, value="silver")
    search = _search(core, ctx, "TEST-project color")
    packet = RecallPacketCompiler(core.recall_pipeline.storage_reader, clock=core.clock).compile(
        search, result, core.storage
    )
    assert not any(obj["ref"] == source.ref for obj in packet["items"])
    assert "superseded_evidence_withheld:1" in packet["gaps"]
    constrained = bounded_packet({**packet, "unmet_needs": ["TEST-long " * 1000]}, 256)
    assert envelope_tokens(constrained) <= 256
    assert "superseded_evidence_withheld:1" in constrained["gaps"]


def test_current_source_status_stops_at_the_first_unreplaced_support(app, monkeypatch):
    from scope_recall.core.lineage import source_evidence_status

    core, ctx = app
    source = capture(core, ctx, "TEST-project color amber.")
    item = accept(core, ctx, draft(source, "amber", predicate="color")).items[0]
    with core.storage.write(ctx) as tx:
        conn = tx._check(write=True)
        for table, key in (("claims", "claim_id"), ("claim_versions", "claim_id"), ("evidence_links", "object_ref")):
            row = dict(conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (item.ref,)).fetchone())
            names = list(row)
            for index in range(400):
                row[key] = f"TEST-current-support-{index:04}"
                if table == "claims":
                    row["slot_key"] = row[key]
                conn.execute(
                    f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                    [row[name] for name in names],
                )
    with core.storage.read(ctx) as tx:
        repository = type(tx.claims)
        native = repository.versions
        calls = []

        def versions(self, ref):
            calls.append(ref)
            return native(self, ref)

        monkeypatch.setattr(repository, "versions", versions)
        assert source_evidence_status(tx, source.ref, 1, core.clock.utc_now()) == ((), False)
    assert len(calls) <= 2, len(calls)


def test_source_content_use_probe_seeks_only_named_sources(app):
    from scope_recall.core import lineage

    core, ctx = app
    source = capture(core, ctx, "TEST-project amber.")
    with core.storage.write(ctx) as tx:
        for i in range(5000):
            lineage.link(tx._check(write=True), "artifact", f"TEST-unrelated-artifact-{i}", 1, source.ref, 1)
    steps = []
    with core.storage.read(ctx) as tx:
        conn = tx._check()
        conn.set_progress_handler(lambda: steps.append(1) or 0, 100)
        assert lineage.source_content_uses(conn, (("TEST-missing-source", 1),)) == {}
        conn.set_progress_handler(None, 0)
    assert len(steps) * 100 < 2000, len(steps) * 100


def test_profile_work_does_not_follow_unrelated_fact_head_count(app):
    """A shared scope's ordinary facts cannot increase profile head scanning."""
    from scope_recall.core.background_context import _profile_rows

    core, ctx = app
    source = capture(core, ctx, "TEST-project Markdown and blue.")
    accept(core, ctx, draft(source, "Markdown", kind="preference", predicate="format"))
    ordinary = accept(core, ctx, draft(source, "blue", kind="fact", predicate="color")).items[0]
    search = _search(core, ctx, "Markdown")

    def measured():
        steps = []
        with core.storage.read(ctx) as tx:
            conn = tx._check()
            conn.set_progress_handler(lambda: steps.append(1) or 0, 100)
            rows = _profile_rows(tx, search)
            conn.set_progress_handler(None, 0)
        return rows, len(steps) * 100

    before, baseline_steps = measured()
    with core.storage.write(ctx) as tx:
        conn = tx._check(write=True)
        for table in ("claims", "claim_versions"):
            row = dict(conn.execute(f"SELECT * FROM {table} WHERE claim_id=?", (ordinary.ref,)).fetchone())
            names = list(row)
            for index in range(3000):
                row["claim_id"] = f"TEST-unrelated-profile-{index:04}"
                if table == "claims":
                    row["slot_key"] = row["claim_id"]
                conn.execute(
                    f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                    [row[name] for name in names],
                )
    after, expanded_steps = measured()
    assert after == before
    assert expanded_steps <= baseline_steps + 10000, (baseline_steps, expanded_steps)


def test_old_relevant_preference_survives_busy_recent_window(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 输出格式 Markdown。")
    older = accept(core, ctx, draft(source, "Markdown", kind="preference", predicate="输出格式")).items[0]
    for index in range(26):
        source = capture(core, ctx, f"TEST-project 配色{index} 蓝色。", when="2026-09-03T12:00:00Z")
        accept(core, ctx, draft(source, "蓝色", kind="preference", predicate=f"配色{index}"))
    search = _search(core, ctx, "输出格式如何选择")
    with core.storage.read(ctx) as tx:
        selected = background_candidates(
            tx, search, core.recall_pipeline.storage_reader, core.clock, query_evidence=True
        )
    assert older.ref in {candidate.ref for candidate, _ in selected}
    assert len(selected) <= 2


def test_shared_profile_evidence_is_matched_once_before_claim_fanout(app):
    from scope_recall.core.background_context import _profile_rows

    core, ctx = app
    words = " ".join(f"TESTword{i}" for i in range(40))
    source = capture(core, ctx, "TEST-project Markdown. " + words)
    item = accept(core, ctx, draft(source, "Markdown", kind="preference", predicate="format")).items[0]
    with core.storage.write(ctx) as tx:
        conn = tx._check(write=True)
        for table, key in (("claims", "claim_id"), ("claim_versions", "claim_id"), ("evidence_links", "object_ref")):
            row = dict(conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (item.ref,)).fetchone())
            names = list(row)
            for i in range(300):
                row[key] = f"TEST-profile-fanout-{i:04}"
                if table == "claims":
                    row["slot_key"] = row[key]
                conn.execute(
                    f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                    [row[name] for name in names],
                )
    steps = []
    with core.storage.read(ctx) as tx:
        conn = tx._check()
        conn.set_progress_handler(lambda: steps.append(1) or 0, 100)
        rows = _profile_rows(tx, _search(core, ctx, words))
        conn.set_progress_handler(None, 0)
    assert len(rows) == 24
    assert len(steps) * 100 < 350_000, len(steps) * 100


def test_the_profile_term_query_deduplicates_evidence_before_matching(app):
    """Only authorized profile evidence enters the term probe, once per distinct source."""
    core, ctx = app
    source = capture(core, ctx, "TEST-project 输出格式 Markdown。")
    accept(core, ctx, draft(source, "Markdown", kind="preference", predicate="输出格式"))
    statements = []
    with core.storage.read(ctx) as tx:
        connection = tx._check()
        connection.set_trace_callback(statements.append)
        selected = background_candidates(
            tx,
            _search(core, ctx, "输出格式如何选择"),
            core.recall_pipeline.storage_reader,
            core.clock,
            query_evidence=True,
        )
        connection.set_trace_callback(None)
    assert selected
    term_queries = [sql for sql in statements if "COUNT(DISTINCT t.term) AS hits" in sql and "claims c" in sql]
    assert len(term_queries) == 1, statements
    with sqlite3.connect(core.storage.path) as db:
        plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + term_queries[0])]
    assert any("MATERIALIZE matched" in step for step in plan), plan
    assert not any(step.startswith("SCAN e") for step in plan), plan


def test_matching_condition_selects_exception_instead_of_general_value(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 表达偏好 简洁。")
    general = accept(core, ctx, draft(source, "简洁", kind="preference", predicate="表达偏好")).items[0]
    source = capture(core, ctx, "只在写文案时 TEST-project 表达偏好 详细。")
    exception = accept(
        core, ctx, draft(source, "详细", kind="preference", predicate="表达偏好", conditions=["写文案时"])
    ).items[0]
    search = _search(core, ctx, "现在帮我写文案")
    with core.storage.read(ctx) as tx:
        selected = background_candidates(
            tx, search, core.recall_pipeline.storage_reader, core.clock, query_evidence=True
        )
        ordinary = background_candidates(
            tx,
            replace(search, query="现在不是写文案，只核对报价"),
            core.recall_pipeline.storage_reader,
            core.clock,
            query_evidence=True,
        )
    assert [candidate.ref for candidate, _ in selected] == [exception.ref]
    assert [candidate.ref for candidate, _ in ordinary] == [general.ref]


def _pair(ref, text, root, score=0.016):
    content = json.dumps(dict(subject="TEST-project", predicate="state", value_text=text, conditions=[]))
    obj = RetrievedObject(
        ref, 1, "claim", content, "human_direct", "current", "trusted scope", (root,), "direct_report", True
    )
    return CandidateRef("claim", ref, 1, "lexical", fusion_score=score), obj


def test_conflicting_matching_conditions_do_not_get_arbitrary_precedence(app):
    core, ctx = app
    items = []
    for condition, value in (("写文案时", "详细"), ("用中文时", "简洁")):
        source = capture(core, ctx, f"只在{condition} TEST-project 表达偏好 {value}。")
        items.append(
            accept(
                core, ctx, draft(source, value, kind="preference", predicate="表达偏好", conditions=[condition])
            ).items[0]
        )
    search = _search(core, ctx, "现在用中文写文案")
    with core.storage.read(ctx) as tx:
        selected = background_candidates(
            tx, search, core.recall_pipeline.storage_reader, core.clock, query_evidence=True
        )
    assert not {item.ref for item in items}.intersection(candidate.ref for candidate, _ in selected)


def test_ranking_covers_another_part_of_question_before_duplicate_evidence(app):
    core, ctx = app
    first = _pair("claim-a", "deployment ready", "event-a@1")
    repeat = _pair("claim-b", "deployment ready", "event-a@1")
    other = _pair("claim-c", "rollback ready", "event-c@1", score=0.015)
    search = _search(core, ctx, "deployment rollback")
    selected = core.recall_pipeline._rank_hydrated([first, repeat, other], search)
    assert [candidate.ref for candidate, _ in selected[:2]] == ["claim-a", "claim-c"]
    historical = core.recall_pipeline._rank_hydrated([first, repeat, other], replace(search, mode="history"))
    assert [candidate.ref for candidate, _ in historical] == ["claim-a", "claim-b", "claim-c"]


def test_large_middle_candidate_does_not_hide_a_small_later_candidate():
    first = _pair("claim-a", "short", "event-a@1")
    large = _pair("claim-b", "x" * 3000, "event-b@1")
    last = _pair("claim-c", "short", "event-c@1")
    kept = RetrievalPipeline(None)._apply_budget([first, large, last], SearchLimits(budget_tokens=128))
    assert [candidate.ref for candidate, _ in kept] == ["claim-a", "claim-c"]


def test_oversized_fallback_scans_only_until_enough_safe_items(app, monkeypatch):
    from scope_recall.core import recall

    core, ctx = app
    pairs = [_pair(f"claim-{i}", "x" * 65_000, f"event-{i}@1") for i in range(200)]
    original, scanned = recall.withhold_secret_items, []

    def scan(items, gaps):
        scanned.extend(items)
        return original(items, gaps)

    monkeypatch.setattr(recall, "withhold_secret_items", scan)
    kept = core.recall_pipeline._apply_budget(pairs, SearchLimits(max_items=1, budget_tokens=128))
    assert kept == pairs[:1] and len(scanned) == 1


def test_oversized_fallback_checks_deadline_between_withheld_candidates(app, monkeypatch):
    from scope_recall.core import recall

    core, ctx = app
    pairs = [_pair(f"claim-{i}", "x" * 65_000, f"event-{i}@1") for i in range(3)]
    search, scanned, gaps = _search(core, ctx, "TEST"), [], []
    expired = False

    def scan(items, gaps):
        nonlocal expired
        scanned.extend(items)
        expired = True
        return []

    monkeypatch.setattr(recall, "withhold_secret_items", scan)
    monkeypatch.setattr(core.recall_pipeline, "_remaining", lambda context: 0 if expired else 1)
    kept = core.recall_pipeline._apply_budget(pairs, SearchLimits(max_items=1, budget_tokens=128), gaps, context=search)
    assert kept == [] and len(scanned) == 1 and "deadline_exceeded" in gaps


def restatement_owner(entry, principal, memo):
    """Synthetic entry-owner attestation, separate from each source's speaker kind."""
    if entry not in {"entry-a", "entry-b", "entry-c"}:
        return None
    if principal is None:
        return "TEST-installation-owner"
    if principal.get("resolution") != "verified":
        return None
    if (principal.get("kind"), principal.get("principal_ref")) in {
        ("human", "principal:human:TEST-owner"),
        ("assistant", "principal:assistant:TEST-" + entry),
    }:
        return "TEST-installation-owner"
    return None


def restatement_assistant(context):
    """The entry's normal assistant actor, without impersonating the owner."""
    return replace(
        context,
        actor_origin="assistant_visible",
        source_principal=TrustedSourcePrincipal(
            "assistant", "verified", "principal:assistant:TEST-" + context.entry_id
        ),
    )


@pytest.fixture
def restatement_store(tmp_path):
    """Three independent sessions and entries share one isolated truth store."""
    binding = shared_binding(tmp_path / "TEST-restatement")
    ctx = replace(
        shared_context(binding),
        project_id="TEST-project",
        branch_id="TEST-main",
        source_principal=TrustedSourcePrincipal("human", "verified", "principal:human:TEST-owner"),
        owner_resolver=restatement_owner,
    )
    core = MemoryCore(CoreConfig(binding), clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    with core.storage.write(ctx) as tx:
        for entry in ("entry-a", "entry-b", "entry-c"):
            tx.registry.register_entry(entry, entry, "hermes", now=core.clock.utc_now())
    contexts = [
        replace(ctx, entry_id=entry, session_id=entry + "-session") for entry in ("entry-a", "entry-b", "entry-c")
    ]
    return core, contexts


def _restatement_packet(store, mode, original, echo, *, linked=False, fact=False):
    core, (a, b, c) = store
    source = capture(core, a, original) if original else None
    claim = accept(core, a, draft(source, "蓝色", predicate="配色")).items[0] if fact else None
    refs = [f"{(claim or source).ref}@1"] if linked and source else []
    actor = restatement_assistant(b)
    saved = core.record_event(
        actor,
        source_event(
            source_event_key="TEST-echo",
            content=echo,
            origin="assistant_visible",
            role="assistant",
            evidence_refs=refs,
            occurred_at="2026-09-02T12:00:00Z",
        ),
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    repeat = core.source(b, saved.event_refs[0].ref, 1)
    packet = core.recall_packet(c, recall_request(query="TEST-project 配色", mode=mode), deadline_seconds=10)
    return packet, source, repeat


@pytest.mark.parametrize("mode", ["auto", "current"])
@pytest.mark.parametrize("linked,fact", [(False, False), (True, False), (True, True)])
def test_original_precedes_attributed_cross_entry_restatement(restatement_store, mode, linked, fact):
    original = "TEST-project 配色使用蓝色，图标和背景统一采用同一种蓝色。"
    packet, source, repeat = _restatement_packet(
        restatement_store, mode, original, "你之前说过：" + original, linked=linked, fact=fact
    )
    assert packet["items"]
    assert packet["items"][0]["origin"] == "human_direct", packet
    repeat_item = next(item for item in packet["items"] if item["ref"] == repeat.ref)
    assert repeat_item["basis"] == "derived_summary", packet
    assert any(f"{source.ref}@1" in item["evidence_refs"] for item in packet["items"])


@pytest.mark.parametrize("mode", ["auto", "current"])
@pytest.mark.parametrize("extra", ["印刷交付另用银色专色油墨，并附上纸张色样。", "备用银色。"])
def test_restatement_with_additional_information_is_labelled(restatement_store, mode, extra):
    original = "TEST-project 配色使用蓝色，图标和背景统一采用同一种蓝色。"
    packet, source, repeat = _restatement_packet(
        restatement_store,
        mode,
        original,
        "用户偏好：" + original + extra,
        linked=True,
    )
    refs = [item["ref"] for item in packet["items"]]
    assert refs.index(source.ref) < refs.index(repeat.ref), packet
    item = next(item for item in packet["items"] if item["ref"] == repeat.ref)
    assert "restatement_of" not in item, packet
    assert item["basis"] == "derived_summary", packet
    assert item["entries"] == [{"id": "entry-b", "name": "entry-b"}], packet
    assert "银色" in item["content"]
    rendered = render_recall_packet_context(packet, installation_id="TEST-installation", session_id="TEST-render")
    assert rendered.canonical_text and '"derived_summary"' in rendered.canonical_text
    assert '"restatement_of"' not in rendered.canonical_text
    assert '"entry-b"' in rendered.canonical_text


@pytest.mark.parametrize("mode", ["auto", "current"])
def test_unrelated_assistant_information_keeps_its_rank(restatement_store, mode):
    packet, source, repeat = _restatement_packet(
        restatement_store,
        mode,
        "TEST-project 配色使用蓝色。",
        "TEST-project 配色校验器的运行命令是 palette inspect，输出保存到 TEST-samples。",
    )
    item = next(item for item in packet["items"] if item["ref"] == repeat.ref)
    assert item["basis"] == "observed"


@pytest.mark.parametrize("mode", ["auto", "current"])
def test_assistant_only_information_stays_available(restatement_store, mode):
    packet, _, repeat = _restatement_packet(restatement_store, mode, None, "TEST-project 配色使用蓝色。")
    assert repeat.ref in {item["ref"] for item in packet["items"]}, packet


@pytest.mark.parametrize("mode", ["auto", "current"])
def test_correction_precedes_restatement_of_the_old_value(restatement_store, mode):
    core, (a, b, c) = restatement_store
    original = capture(core, a, "TEST-project 配色使用蓝色，图标和背景统一采用同一种蓝色。")
    claim = accept(core, a, draft(original, "蓝色", predicate="配色")).items[0]
    saved = core.record_event(
        restatement_assistant(b),
        source_event(
            source_event_key="TEST-old-echo",
            content="你之前说过：" + original.event["content"],
            origin="assistant_visible",
            role="assistant",
            evidence_refs=[f"{claim.ref}@1"],
            occurred_at="2026-09-02T12:00:00Z",
        ),
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    correction = capture(core, a, "TEST-project 配色改为银色。", when="2026-09-03T12:00:00Z")
    assert core.current_claim(a, claim.ref).payload["value_text"] == "银色"
    packet = core.recall_packet(c, recall_request(query="TEST-project 配色", mode=mode), deadline_seconds=10)
    items = packet["items"]
    assert "银色" in items[0]["content"], packet
    assert any(f"{correction.ref}@1" in item["evidence_refs"] for item in items), packet
    old_echo = next(item for item in items if item["ref"] == saved.event_refs[0].ref)
    assert old_echo["basis"] == "derived_summary", packet


@pytest.mark.parametrize("mode", ["history", "as_of"])
def test_historical_reads_keep_the_restatement(restatement_store, mode):
    core, (a, b, c) = restatement_store
    original = capture(core, a, "TEST-project 配色使用蓝色，图标和背景统一采用同一种蓝色。")
    saved = core.record_event(
        restatement_assistant(b),
        source_event(
            source_event_key="TEST-history-echo",
            content="你之前说过：" + original.event["content"],
            origin="assistant_visible",
            role="assistant",
        ),
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    request = recall_request(query="TEST-project 配色", mode=mode)
    if mode == "as_of":
        request["as_of"] = "2026-09-06T12:00:00Z"
    packet = core.recall_packet(c, request, deadline_seconds=10)
    assert saved.event_refs[0].ref in {item["ref"] for item in packet["items"]}, packet


@pytest.mark.parametrize("mode", ["auto", "current"])
def test_size_admission_cannot_put_a_long_restatement_first(restatement_store, mode):
    original = "TEST-project 配色使用蓝色，图标和背景统一采用同一种蓝色。" * 20
    packet, source, _ = _restatement_packet(
        restatement_store, mode, original, "用户偏好：" + original + "备用银色。", linked=True
    )
    core, (_, _, c) = restatement_store
    packet = core.recall_packet(
        c, recall_request(query="TEST-project 配色", mode=mode, max_items=1), deadline_seconds=10
    )
    assert packet["items"][0]["ref"] == source.ref, packet


def test_diversity_ranking_reads_each_evidence_root_once(app, monkeypatch):
    from scope_recall.core import recall

    core, ctx = app
    pairs = []
    for index in range(30):
        ref = f"event-TEST-linear-roots-{index}"
        candidate = CandidateRef("event", ref, 1, "lexical", fusion_score=0.01)
        obj = RetrievedObject(
            ref,
            1,
            "event",
            "Orchard deployment amber",
            "human_direct",
            "current",
            "applicable",
            (f"{ref}@1",),
            "direct_report",
            False,
        )
        pairs.append((candidate, obj))
    calls = []
    native = recall.evidence_roots

    def counted(obj):
        calls.append(obj.ref)
        return native(obj)

    monkeypatch.setattr(recall, "evidence_roots", counted)
    result = core.recall_pipeline._rank_hydrated(pairs, _search(core, ctx, "Orchard deployment amber"))
    assert len(result) == len(pairs)
    assert len(calls) == len(pairs)
