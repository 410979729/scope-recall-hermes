"""Local owner management preserves the normal content and version boundaries."""

import base64
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core import management_query
from scope_recall.core.management import MemoryManagement
from scope_recall.core.management_query import MemoryFilter
from scope_recall.core.restore import InstallationMaintenance
from scope_recall.core.retrieval import CandidateRef
from scope_recall.core.storage import Transaction
from test_candidate_lifecycle import Evaluator, _candidate, _candidate_rows, _finish_source_work
from test_claims import accept, app, capture, draft, initial
from test_deletion import sqlite_backup


def legacy_secret(core, context, monkeypatch, text="NameOne 密码：Xk9mP2qa"):
    """Capture synthetic legacy data with only ingress secret admission bypassed."""
    from scope_recall.core import events

    with monkeypatch.context() as patch:
        patch.setattr(events, "redact_secret_text", lambda value: (value, 0))
        return capture(core, context, text)


@pytest.mark.parametrize(
    "predicate,value",
    [
        ("偏好", "密码：Xk9mP2qa"),
        ("密码", "Xk9mP2qa"),
        ("**Password**", "Xk9mP2qa"),
        ("**refreshToken**", "Xk9mP2qa"),
        ("**token**", "Xk9mP2qa"),
    ],
)
def test_derived_secret_is_withheld_from_profile_entity_trace_and_background(app, monkeypatch, predicate, value):
    from scope_recall.core import mutate

    core, context = app
    source = legacy_secret(core, context, monkeypatch, f"TEST-project {predicate} {value}。")
    with monkeypatch.context() as patch:
        patch.setattr(mutate, "structure_contains_secret_like_text", lambda value: False)
        flagged = accept(
            core, context, draft(source, value, kind="preference", predicate=predicate, statement_kind="assertion")
        ).items[0]
    initial(core, context)
    profile = core.profile(
        context, dict(protocol_version="1.1", request_id="TEST-secret-profile", subject="TEST-project")
    )
    entity = core.entity(
        context,
        dict(
            protocol_version="1.1",
            request_id="TEST-secret-entity",
            subject="TEST-project",
            action="related",
            direction="both",
        ),
    )
    trace = core.trace(context, dict(protocol_version="1.1", request_id="TEST-secret-trace", subject="TEST-project"))
    packet = core.recall_packet(
        context,
        dict(
            protocol_version="1.1",
            request_id="TEST-secret-background",
            query="配色",
            mode="auto",
            max_items=6,
            budget_tokens=4096,
        ),
        deadline_seconds=10,
    )
    for result in (profile, entity, trace, packet):
        assert "Xk9mP2qa" not in json.dumps(result, ensure_ascii=False)
        assert any(gap.startswith("secrets_withheld:") for gap in result["gaps"])
    assert flagged.ref in {item["ref"] for item in management(core, context).secrets()["items"]}
    for view in (profile, entity):
        assert view["status"] == "partial"
        assert view["coverage"] == view["answerability"] == "partial"


def test_secret_inventory_includes_historical_and_muted_versions(app, monkeypatch):
    core, context = app
    flagged = legacy_secret(core, context, monkeypatch)
    capture(core, context, "NameOne uses green.", key=flagged.event["source_event_key"], revision=2)
    owner = management(core, context)
    owner.apply(owner.preview([flagged.ref], mode="suppress"))
    findings = owner.secrets()["items"]
    assert any(item["ref"] == flagged.ref and item["revision"] == 1 for item in findings)


def test_stored_alias_subject_does_not_escape_through_view_metadata(app, monkeypatch):
    from scope_recall.core import mutate
    from test_aliases import _alias

    core, context = app
    subject = "密码：Xk9mP2qa"
    source = legacy_secret(core, context, monkeypatch, f"{subject} 项目名称是 NameOne。")
    rename = legacy_secret(core, context, monkeypatch, f"{subject} 项目以后改名为 天一，内容不变，保留旧称 NameOne。")
    with monkeypatch.context() as patch:
        patch.setattr(mutate, "structure_contains_secret_like_text", lambda value: False)
        target = accept(
            core, context, draft(source, "NameOne", kind="fact", predicate="项目名称", subject=subject)
        ).items[0]
        alias = accept(core, context, _alias(rename, target, "天一") | {"subject": subject}).items[0]
    assert target.state == alias.state == "active"
    request = dict(protocol_version="1.1", request_id="TEST-alias-secret", subject="天一")
    for view in (core.profile(context, request), core.entity(context, dict(request, action="related"))):
        assert view["alias_resolution"] == "resolved"
        assert view["resolved_subject"] is None
        assert "Xk9mP2qa" not in json.dumps(view, ensure_ascii=False)
        assert any(gap.startswith("secrets_withheld:") for gap in view["gaps"])


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("max_items", [1, 6])
def test_stored_secret_is_withheld_from_recall_and_inspect_but_owner_keeps_it(app, monkeypatch, automatic, max_items):
    core, context = app
    flagged = legacy_secret(core, context, monkeypatch)
    clean = capture(core, context, "NameOne uses green.")
    packet = core.recall_packet(
        context,
        dict(
            protocol_version="1.1",
            request_id="TEST-secret-recall",
            query="NameOne",
            mode="auto" if automatic else "current",
            focus_refs=[f"{flagged.ref}@1", f"{clean.ref}@1"],
            budget_tokens=4096,
            max_items=max_items,
        ),
        deadline_seconds=10,
        background_without_evidence=automatic,
    )
    assert clean.ref in {item["ref"] for item in packet["items"]}
    assert flagged.ref not in {item["ref"] for item in packet["items"]}
    assert "secrets_withheld:1" in packet["gaps"]
    assert "Xk9mP2qa" not in json.dumps(packet, ensure_ascii=False)
    inspected = core.inspect_object(context, flagged.ref)
    assert inspected.value is None and inspected.gaps == ("secrets_withheld:1",)
    assert management(core, context).show(flagged.ref)["value"]["event"]["content"] == flagged.event["content"]


def test_owner_secret_scan_filters_sql_and_releases_each_read_before_scanning(app, monkeypatch):
    from contextlib import contextmanager

    from scope_recall.core import management_secrets

    core, context = app
    flagged = legacy_secret(core, context, monkeypatch)
    capture(core, context, "NameOne uses green.")
    capture(core, context, "密码错误，请重试。")
    original_read = core.storage.read
    original_scan = management_secrets.stored_item_pattern_names
    reads, active = [], []

    @contextmanager
    def read(*args, **kwargs):
        with original_read(*args, **kwargs) as tx:
            active.append(tx)
            reads.append(tx)
            try:
                yield tx
            finally:
                active.pop()

    def scan(value):
        assert not active, "the full scan must run outside a read transaction"
        return original_scan(value)

    monkeypatch.setattr(core.storage, "read", read)
    monkeypatch.setattr(management_secrets, "stored_item_pattern_names", scan)
    monkeypatch.setattr(management_secrets, "BATCH_SIZE", 1)
    with original_read(context) as tx:
        before = list(tx._check().iterdump())
        expected_rows = sum(
            tx._check().execute(f"SELECT count(*) FROM {spec[1]}").fetchone()[0] for spec in management_secrets._TABLES
        )
    result = management(core, context).secrets()
    with original_read(context) as tx:
        assert list(tx._check().iterdump()) == before
    assert result["rows_read"] == expected_rows and result["candidates_scanned"] == 2
    assert len(reads) >= 3 and len(result["items"]) == 1
    finding = result["items"][0]
    assert finding == dict(
        ref=flagged.ref,
        kind="event",
        revision=1,
        origin="human_direct",
        entry="local",
        session=flagged.session_id,
        time=flagged.event["occurred_at"],
        patterns=["chinese_credential_assignment"],
    )
    assert "Xk9mP2qa" not in json.dumps(result, ensure_ascii=False)


def test_secret_scan_windows_seek_the_primary_key_without_sorting_the_scope(app):
    from contextlib import contextmanager

    from scope_recall.core import management_secrets

    core, context = app
    statements = []

    @contextmanager
    def read(*args, **kwargs):
        with core.storage.read(*args, **kwargs) as tx:
            tx._check().set_trace_callback(statements.append)
            try:
                yield tx
            finally:
                tx._check().set_trace_callback(None)

    for spec in management_secrets._TABLES:
        statements.clear()
        management_secrets._batch(SimpleNamespace(read=read), context, spec, ("", 0))
        query = next(statement for statement in statements if statement.startswith("WITH key_window"))
        with core.storage.read(context) as tx:
            plan = [row[3].upper() for row in tx._check().execute("EXPLAIN QUERY PLAN " + query)]
        assert any("SEARCH V USING" in detail for detail in plan), (spec[0], plan)
        batch_scan = next(index for index, detail in enumerate(plan) if "SCAN BATCH" in detail)
        assert not any("TEMP B-TREE" in detail for detail in plan[:batch_scan]), (spec[0], plan)


def test_secret_scan_bounds_the_window_before_visibility_filters(app, monkeypatch):
    from scope_recall.core import management_secrets

    core, context = app
    sources = [capture(core, context, f"TEST bounded row {i}") for i in range(8)]
    sources.sort(key=lambda source: source.ref)
    with core.storage.write(context) as tx:
        connection = tx._check(write=True)
        connection.executemany(
            "UPDATE source_events SET read_blocked=1 WHERE event_id=?", [(s.ref,) for s in sources[:6]]
        )
        connection.execute(
            "UPDATE source_events SET extra_json=? WHERE event_id=?",
            (json.dumps(dict(predicate="密码", value_text="Synthet1c")), sources[-1].ref),
        )
        expected_rows = sum(
            connection.execute(f"SELECT count(*) FROM {spec[1]}").fetchone()[0] for spec in management_secrets._TABLES
        )
    monkeypatch.setattr(management_secrets, "BATCH_SIZE", 2)
    rows = management_secrets._batch(core.storage, context, management_secrets._TABLES[0], ("", 0))
    assert [row["ref"] for row in rows] == [source.ref for source in sources[:2]]
    assert all(row["scan_content"] is None for row in rows)
    findings = management(core, context).secrets()
    assert findings["rows_read"] == expected_rows and findings["candidates_scanned"] == 1
    assert [item["ref"] for item in findings["items"]] == [sources[-1].ref]


def management(core, context):
    return MemoryManagement(core, InstallationMaintenance(context))


def test_owner_can_browse_source_and_fact_in_project(app):
    core, context = app
    item, source = initial(core, context)
    owner = management(core, replace(context, project_id=None, branch_id=None))
    page = owner.list(MemoryFilter(query="蓝色"))
    assert {item.ref, source.ref} <= {row["ref"] for row in page["items"]}
    detail = owner.show(item.ref)
    assert detail["value"]["payload"]["value_text"] == "蓝色"
    assert owner.history(item.ref)["items"][0]["revision"] == 1


def test_filters_and_cursor_survive_capture(app):
    core, context = app
    _item, source = initial(core, context)
    owner = management(core, context)
    page = owner.list(MemoryFilter(kind="event", agent="TEST-agent", session="TEST-session"))
    assert page["items"][0]["ref"] == source.ref
    assert owner.list(MemoryFilter(agent="agent2"))["items"] == []
    assert owner.list(MemoryFilter(since="2026-09-02T00:00:00Z"))["items"] == []
    page = owner.list(limit=1)
    assert page["next_cursor"]
    capture(core, context, "NameOne uses green.")
    assert owner.list(limit=1, cursor=page["next_cursor"])["items"]
    for filters in (MemoryFilter(kind="event"), MemoryFilter(scope="TEST-other")):
        with pytest.raises(ContractError) as error:
            owner.list(filters, cursor=page["next_cursor"])
        assert error.value.code == "VERSION_CONFLICT"
    foreign = (
        replace(context, allowed_scope_ids=frozenset()),
        replace(context, binding=replace(context.binding, installation_id="TEST-other-installation")),
    )
    for other in foreign:
        with pytest.raises(ContractError) as error:
            management_query.decode_cursor(page["next_cursor"], management_query.page_binding(other, MemoryFilter()))
        assert error.value.code == "VERSION_CONFLICT"


def test_capture_during_scan_uses_one_snapshot(app, monkeypatch):
    core, context = app
    _item, source = initial(core, context)
    original = management_query.inventory
    captured = []

    def inventory(*args, **kwargs):
        rows = original(*args, **kwargs)
        if not captured:
            captured.append(capture(core, context, "TEST concurrent matching needle"))
        return rows

    monkeypatch.setattr(management_query, "inventory", inventory)
    filters = MemoryFilter(query="TEST concurrent matching needle")
    owner = management(core, context)
    page = owner.list(filters)
    assert captured and page["items"] == []
    assert captured[0].ref not in {item["ref"] for item in page["items"]}
    assert [item["ref"] for item in owner.list(filters)["items"]] == [captured[0].ref]


def test_search_reaches_oldest_match_across_inventory_batches(app, monkeypatch):
    core, context = app
    # Small batches cross the same inventory and hydration boundaries with a few captures.
    monkeypatch.setattr(management_query, "_SCAN_BATCH", 8)
    monkeypatch.setattr(management_query, "_HYDRATE_BATCH", 2)
    sources = [capture(core, context, f"TEST haystack {i}") for i in range(20)]
    last = max(sources, key=lambda source: source.ref)
    target = capture(core, context, "TEST oldest needle", key=last.event["source_event_key"], revision=2)
    page = management(core, context).list(MemoryFilter(kind="event", query="OLDEST needle"))
    assert [item["ref"] for item in page["items"]] == [target.ref]
    assert page["examined"] == 20
    assert page["scan_complete"] and page["next_cursor"] is None


def test_newest_first_across_kinds_and_unknown_occurrence(app, tmp_path):
    from test_episodes import apply, artifact, ref

    core, context = app
    item, _source = initial(core, context)
    core.clock.now = "2026-09-07T12:00:00Z"
    owner = management(core, context)
    owner.revise(item.ref, expected_revision=1, value="TEST revised")
    attachment, _witness, _path = artifact(core, context, tmp_path)
    core.clock.now = "2026-09-07T12:00:00.000001Z"
    proposal = dict(
        mention=attachment.label,
        candidate_refs=[f"{attachment.ref}@1"],
        resolved_ref=f"{attachment.ref}@1",
        resolution="resolved",
        evidence_refs=[ref(_witness)],
    )
    reference = apply(core, context, references=[proposal]).items[0]
    unknown = capture(core, context, "TEST unknown time", when=None, recorded_at="2026-09-08T00:00:00Z")
    page = owner.list()
    refs = [row["ref"] for row in page["items"]]
    assert refs[0] == unknown.ref
    assert (
        next(row["sort_time"] for row in page["items"] if row["ref"] == item.ref) == "2026-09-06T12:00:00.000000+00:00"
    )
    assert refs.index(attachment.ref) < refs.index(_source.ref)
    keys = [(row["sort_time"], row["ref"]) for row in page["items"]]
    assert keys == sorted(sorted(keys, key=lambda key: key[1]), key=lambda key: key[0], reverse=True)
    assert refs.index(reference.ref) < refs.index(attachment.ref)
    assert {"event", "claim", "episode", "artifact", "reference"} <= {row["kind"] for row in page["items"]}


def test_deleted_and_blocked_stay_absent_and_muted_visible_across_pages(app):
    core, context = app
    sources = [capture(core, context, f"TEST item {i}") for i in range(4)]
    owner = management(core, context)
    filters = MemoryFilter(kind="event")
    first = owner.list(filters, limit=1)
    pending = sorted((s for s in sources if s.ref != first["items"][0]["ref"]), key=lambda s: s.ref)
    deleted, blocked, muted = pending
    owner.apply(owner.preview([deleted.ref]))
    with core.storage.write(context) as tx:
        tx._check(write=True).execute("UPDATE source_events SET read_blocked=1 WHERE event_id=?", (blocked.ref,))
    owner.apply(owner.preview([muted.ref], mode="suppress"))
    page = owner.list(filters, cursor=first["next_cursor"])
    assert [row["ref"] for row in page["items"]] == [muted.ref]
    assert page["items"][0]["suppressed"] == 1


@pytest.mark.parametrize("when", ["2026-09-03T12:00:00Z", None])
def test_blocked_first_revision_time_stays_out_of_inventory_and_cursor(app, when):
    core, context = app
    blocked = capture(core, context, "TEST blocked first revision", when="2026-09-05T19:43:21Z")
    readable = capture(
        core,
        context,
        "TEST readable later revision",
        key=blocked.event["source_event_key"],
        revision=2,
        when=when,
        recorded_at="2026-09-03T12:00:00Z",
    )
    older = capture(core, context, "TEST older event", when="2026-09-01T12:00:00Z")
    with core.storage.write(context) as tx:
        tx._check(write=True).execute(
            "UPDATE source_events SET read_blocked=1 WHERE event_id=? AND source_revision=1", (blocked.ref,)
        )
    assert core.source(context, blocked.ref, 1) is None
    owner = management(core, context)
    filters = MemoryFilter(kind="event")
    page = owner.list(filters, limit=1)
    cursor = json.loads(base64.urlsafe_b64decode(page["next_cursor"] + "=" * (-len(page["next_cursor"]) % 4)))
    assert [row["ref"] for row in page["items"]] == [readable.ref]
    assert page["items"][0]["revision"] == 2
    assert "2026-09-05" not in json.dumps([page["items"], cursor])
    assert page["items"][0]["sort_time"] == "2026-09-03T12:00:00.000000+00:00"
    assert cursor["after"] == [page["items"][0]["sort_time"], readable.ref]
    assert [row["ref"] for row in owner.list(filters, cursor=page["next_cursor"])["items"]] == [older.ref]


@pytest.mark.parametrize(
    ("writable", "as_of", "states", "queries"),
    [
        (False, None, ("historical", "current", "current"), 0),
        (False, "2026-09-02T12:00:00Z", ("current", None, "current"), 2),
        (False, "2026-09-04T12:00:00Z", ("historical", "current", "current"), 3),
        (True, None, ("historical", "current", "current"), 3),
    ],
)
def test_prefetched_event_supersession_queries_follow_snapshot_and_time(app, writable, as_of, states, queries):
    core, context = app
    first = capture(core, context, "TEST first value")
    second = capture(
        core, context, "TEST later value", key=first.event["source_event_key"], revision=2, when="2026-09-03T12:00:00Z"
    )
    single = capture(core, context, "TEST unrevised value")
    candidates = tuple(
        CandidateRef("event", source.ref, source.revision, "exact_ref") for source in (first, second, single)
    )
    search = replace(management_query._search_context(context, core), as_of=as_of)
    reader = core.recall_pipeline.storage_reader
    transaction = core.storage.write if writable else core.storage.read
    with transaction(context) as tx:
        statements = []
        conn = tx._check()
        conn.set_trace_callback(statements.append)
        try:
            reader.prefetch(tx, candidates, search)
            objects = [reader.hydrate(tx, candidate, search) for candidate in candidates]
        finally:
            conn.set_trace_callback(None)
    assert tuple(obj.temporal_status if obj else None for obj in objects) == states
    newer_queries = [sql for sql in statements if sql.startswith("SELECT 1 FROM source_events newer WHERE")]
    assert len(newer_queries) == queries


@pytest.mark.parametrize("cursor", ["{}", "[]", "null", "bad", '{"after":"x","binding":"x","epoch":1}'])
def test_malformed_and_epoch_bound_cursors_are_invalid(app, cursor):
    core, context = app
    with pytest.raises(ContractError) as error:
        management(core, context).list(cursor=cursor)
    assert error.value.code == "INPUT_INVALID"


def test_time_budget_cursor_resumes_without_skipping_a_batch(app, monkeypatch):
    core, context = app
    sources = [capture(core, context, f"TEST haystack [{i}]") for i in range(40)]
    last = max(sources, key=lambda source: source.ref)
    filters = MemoryFilter(kind="event", query=last.event["content"])
    owner = management(core, context)
    monkeypatch.setattr(management_query, "SCAN_SECONDS", 0)
    page = owner.list(filters)
    assert page["items"] == [] and 0 < page["examined"] < 40 and not page["scan_complete"]
    assert page["stop_reason"] == "time_budget" and page["next_cursor"]
    monkeypatch.setattr(management_query, "SCAN_SECONDS", 30)
    resumed = owner.list(filters, cursor=page["next_cursor"])
    assert [row["ref"] for row in resumed["items"]] == [last.ref]
    assert resumed["examined"] + page["examined"] == 40 and resumed["scan_complete"]


def test_budget_stop_in_second_inventory_batch_bounds_prefetch_and_resumes(app, monkeypatch):
    core, context = app
    # Small batches put the budget stop inside the second inventory page with a few captures.
    monkeypatch.setattr(management_query, "_SCAN_BATCH", 8)
    monkeypatch.setattr(management_query, "_HYDRATE_BATCH", 2)
    target = capture(core, context, "TEST oldest needle", when="2026-01-01T00:00:00Z")
    for i in range(19):
        capture(core, context, f"TEST haystack {i}")
    filters = MemoryFilter(kind="event", query=target.event["content"])
    owner = management(core, context)
    clock = [0.0]
    monkeypatch.setattr(management_query, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    reader = core.recall_pipeline.storage_reader
    original = reader.prefetch
    batches = []

    def prefetch(tx, candidates, search):
        batches.append(len(candidates))
        result = original(tx, candidates, search)
        if sum(batches) > 8:
            clock[0] = 31.0
        return result

    monkeypatch.setattr(reader, "prefetch", prefetch)
    page = owner.list(filters)
    assert page["items"] == [] and 8 < page["examined"] < 20
    assert page["stop_reason"] == "time_budget" and page["next_cursor"]
    assert max(batches) <= 2
    monkeypatch.setattr(reader, "prefetch", original)
    resumed = owner.list(filters, cursor=page["next_cursor"])
    assert [row["ref"] for row in resumed["items"]] == [target.ref]
    assert resumed["examined"] + page["examined"] == 20 and resumed["scan_complete"]


def test_inventory_using_up_budget_still_advances_and_avoids_queue_counts(app, monkeypatch):
    core, context = app
    for i in range(40):
        capture(core, context, f"TEST haystack {i}")
    clock = [0.0]
    monkeypatch.setattr(management_query, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    original = management_query.inventory

    def inventory(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] += 31.0
        return result

    def status(*args, **kwargs):
        pytest.fail("inventory must read memory_epoch without queue diagnostics")

    monkeypatch.setattr(management_query, "inventory", inventory)
    monkeypatch.setattr(Transaction, "status", status)
    page = management(core, context).list(MemoryFilter(query="TEST absent needle"))
    assert page["examined"] > 0 and page["next_cursor"]


@pytest.mark.parametrize("returned", [False, True])
def test_event_revision_cannot_cross_cursor_in_either_direction(app, returned):
    core, context = app
    first = capture(core, context, "TEST first", when="2026-09-01T12:00:00Z")
    second = capture(core, context, "TEST second", when="2026-09-01T11:00:00Z")
    owner = management(core, context)
    filters = MemoryFilter(kind="event")
    page = owner.list(filters, limit=1)
    assert page["items"][0]["ref"] == first.ref
    target = first if returned else second
    capture(
        core,
        context,
        "TEST revised",
        key=target.event["source_event_key"],
        revision=2,
        when="2026-09-01T10:00:00Z" if returned else "2026-09-01T13:00:00Z",
    )
    resumed = owner.list(filters, cursor=page["next_cursor"])
    assert [row["ref"] for row in resumed["items"]] == [second.ref]
    assert resumed["scan_complete"]


def test_late_lower_source_revision_keeps_first_stored_order(app):
    core, context = app
    first = capture(core, context, "TEST first stored", revision=5, when="2026-09-01T12:00:00Z")
    second = capture(core, context, "TEST second", when="2026-09-01T11:00:00Z")
    capture(core, context, "TEST late lower revision", key=first.event["source_event_key"], when="2026-09-01T10:00:00Z")
    page = management(core, context).list(MemoryFilter(kind="event"))
    assert [row["ref"] for row in page["items"]] == [first.ref, second.ref]
    assert page["items"][0]["sort_time"] == "2026-09-01T12:00:00.000000+00:00"


def test_claim_revision_keeps_first_recording_time_between_pages(app):
    core, context = app
    item, _source = initial(core, context)
    core.clock.now = "2026-09-07T12:00:00Z"
    other = capture(core, context, "TEST-other-project 配色 蓝色。")
    newer = accept(core, context, draft(other, subject="TEST-other-project")).items[0]
    assert newer.state == "active"
    owner = management(core, context)
    filters = MemoryFilter(kind="claim")
    page = owner.list(filters, limit=1)
    assert page["items"][0]["ref"] == newer.ref
    core.clock.now = "2026-09-08T12:00:00Z"
    owner.revise(item.ref, expected_revision=1, value="TEST revised")
    resumed = owner.list(filters, cursor=page["next_cursor"])
    assert [row["ref"] for row in resumed["items"]] == [item.ref]
    assert resumed["items"][0]["revision"] == 2


def test_project_bound_owner_lists_all_installation_partitions(app):
    core, context = app
    source = capture(core, replace(context, project_id="TEST-other", branch_id="TEST-other"), "TEST other partition")
    owner = management(core, context)
    assert owner.show(source.ref)["ref"] == source.ref
    assert [row["ref"] for row in owner.list(MemoryFilter(kind="event"))["items"]] == [source.ref]


@pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_overflowing_cursor_position_is_input_invalid(app, stamp):
    core, context = app
    value = dict(after=[stamp, "event-TEST"], binding=management_query.page_binding(context, MemoryFilter()), version=1)
    token = base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    for cursor in (json.dumps(value), token):
        with pytest.raises(ContractError) as error:
            management(core, context).list(cursor=cursor)
        assert error.value.code == "INPUT_INVALID"


@pytest.mark.parametrize(
    "decoded", [[], None, "TEST", 3, {"after": None}, {"after": None, "binding": [], "version": 1}]
)
def test_cursor_decoded_shape_is_input_invalid(app, decoded):
    core, context = app
    cursor = base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip("=")
    with pytest.raises(ContractError) as error:
        management(core, context).list(cursor=cursor)
    assert error.value.code == "INPUT_INVALID"


@pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_inventory_orders_stored_timestamp_overflow_without_raising(app, stamp):
    core, context = app
    source = capture(core, context, "TEST extreme time")
    with core.storage.write(context) as tx:
        tx._check(write=True).execute("UPDATE source_events SET occurred_at=? WHERE event_id=?", (stamp, source.ref))
    with core.storage.read(context) as tx:
        rows = management_query.inventory(tx, MemoryFilter(kind="event")).fetchall()
    assert [row["ref"] for row in rows] == [source.ref]


@pytest.mark.parametrize("query", ["信用卡", "alpha beta", "fix alpha beta suff", "a+b.[x]", "ＳＥＴ"])
def test_search_matches_literal_substrings_of_rendered_text(app, query):
    core, context = app
    source = capture(core, context, "TEST 信用卡 prefix alpha beta suffix a+b.[x] ＳＥＴ")
    page = management(core, context).list(MemoryFilter(kind="event", query=query))
    assert [row["ref"] for row in page["items"]] == [source.ref]


def test_preview_then_delete_matches_closure_and_records_owner(app):
    core, context = app
    item, source = initial(core, context)
    owner = management(core, context)
    preview = owner.preview([item.ref], expected={item.ref: 1})
    assert {item.ref, source.ref} <= {row["ref"] for row in preview["affected"]}
    assert owner.show(source.ref)["value"]["event"]["content"]
    result = owner.apply(preview)
    assert sum(r["affected_objects"] for r in result["receipts"]) == len(preview["affected"])
    assert all(r["read_blocked"] for r in result["receipts"])
    assert owner.list()["items"] == []
    assert core.search_sources(context, "蓝色") == ()
    with core.storage.read(context) as tx:
        row = tx._check().execute("SELECT * FROM management_actions").fetchone()
        assert row["actor"] == "local_user_management"
        assert "蓝色" not in row["targets_json"]


def test_changed_inventory_refuses_deletion_and_keeps_everything(app):
    core, context = app
    item, _source = initial(core, context)
    owner = management(core, context)
    preview = owner.preview([item.ref])
    capture(core, context, "NameOne uses green.")
    with pytest.raises(ContractError) as error:
        owner.apply(preview)
    assert error.value.code == "VERSION_CONFLICT"
    assert owner.show(item.ref)["revision"] == 1


def test_session_preview_does_not_select_another_session(app):
    core, context = app
    _item, source = initial(core, context)
    other = capture(core, replace(context, session_id="TEST-other"), "NameOne uses green.")
    owner = management(core, context)
    preview = owner.forget_session(MemoryFilter(session=context.session_id))
    assert source.ref in {t["ref"] for t in preview["targets"]}
    assert other.ref not in {t["ref"] for t in preview["affected"]}


def test_session_selection_and_preview_share_snapshot_during_capture(app, monkeypatch):
    core, context = app
    sources = [capture(core, context, f"TEST session item {i}") for i in range(101)]
    owner = management(core, context)
    with core.storage.read(context) as tx:
        epoch = tx.memory_epoch()
    original = management_query._matching_item
    visited, captured = [], []

    def matching(*args):
        result = original(*args)
        visited.append(result)
        if len(visited) == 100:
            captured.append(capture(core, context, "TEST same session during selection", when="2026-09-02T12:00:00Z"))
        return result

    monkeypatch.setattr(management_query, "_matching_item", matching)
    preview = owner.forget_session(MemoryFilter(session=context.session_id))
    assert captured and {t["ref"] for t in preview["targets"]} == {s.ref for s in sources}
    assert preview["memory_epoch"] == epoch
    with pytest.raises(ContractError, match="VERSION_CONFLICT"):
        owner.apply(preview)
    assert len(owner.list(MemoryFilter(kind="event"), limit=100)["items"]) == 100


def test_ordinary_context_is_not_management_authority(app):
    core, context = app
    with pytest.raises(ContractError) as error:
        MemoryManagement(core, context)
    assert error.value.code == "ACCESS_DENIED"


def test_owner_revision_keeps_history_and_truthful_origin(app):
    core, context = app
    item, original = initial(core, context)
    owner = management(core, context)
    receipt = owner.revise(item.ref, expected_revision=1, value="绿色")
    assert receipt["item"]["revision"] == 2
    assert core.current_claim(context, item.ref).payload["value_text"] == "绿色"
    assert owner.history(item.ref)["items"][1]["value"]["payload"]["value_text"] == "蓝色"
    assert core.source(context, original.ref, 1).event["content"] == original.event["content"]
    source = core.source(context, receipt["source_ref"], 1)
    assert source.event["origin"] == "host_generated"
    assert source.event["source_context"]["platform"] == "local_management"
    with pytest.raises(ContractError) as error:
        owner.revise(item.ref, expected_revision=1, value="白色")
    assert error.value.code == "VERSION_CONFLICT"
    with pytest.raises(ContractError) as error:
        owner.revise(original.ref, expected_revision=1, value="白色")
    assert error.value.field == "source_text_immutable"


def test_manual_revision_survives_text_requalification(app):
    core, context = app
    item, _source = initial(core, context)
    owner = management(core, context)
    owner.revise(item.ref, expected_revision=1, value="不在线")
    core.operations.requalify_claims(context, dry_run=False)
    assert core.current_claim(context, item.ref).payload["value_text"] == "不在线"


def test_mute_unmute_then_mute_again(app):
    core, context = app
    item, _source = initial(core, context)
    owner = management(core, context)
    owner.apply(owner.preview([item.ref], mode="suppress"))
    assert core.search_sources(context, "蓝色", automatic=True) == ()
    assert owner.show(item.ref)["value"]["payload"]["value_text"] == "蓝色"
    owner.apply(owner.preview([item.ref], mode="unmute"))
    assert core.search_sources(context, "蓝色", automatic=True)
    owner.apply(owner.preview([item.ref], mode="suppress"))
    assert core.search_sources(context, "蓝色", automatic=True) == ()


def test_unmute_cannot_revive_deleted_content(app):
    core, context = app
    item, _source = initial(core, context)
    owner = management(core, context)
    owner.apply(owner.preview([item.ref]))
    with pytest.raises(ContractError) as error:
        owner.apply(owner.preview([item.ref], mode="unmute"))
    assert error.value.code == "ACCESS_DENIED"
    assert core.search_sources(context, "蓝色") == ()


def restore_snapshot(core, context, snapshot):
    """Restore a synthetic snapshot against the independent latest ledger."""
    from scope_recall.core.restore import begin_restore, export_deletion_ledger, ledger_digest, replay_deletion_ledger

    authority = InstallationMaintenance(context)
    ledger = export_deletion_ledger(core.storage, authority)
    begin_restore(core.storage, authority, expected_ledger_sha256=ledger_digest(ledger))
    sqlite_backup(snapshot, core.storage.path)
    replay_deletion_ledger(core.storage, authority, ledger)


def test_owner_can_retract_a_fact_without_rewriting_its_source(app):
    core, context = app
    item, original = initial(core, context)
    owner = management(core, context)
    receipt = owner.revise(item.ref, expected_revision=1, value=None)
    assert receipt["item"]["revision"] == 2
    assert owner.show(item.ref)["value"]["state"] == "retracted"
    assert core.source(context, original.ref, 1).event == original.event
    assert "蓝色" in core.source(context, receipt["source_ref"], 1).event["content"]


def test_restore_keeps_suppression_inherited_by_a_later_source(app, tmp_path):
    core, context = app
    item, _source = initial(core, context)
    owner = management(core, context)
    owner.apply(owner.preview([item.ref], mode="suppress"))
    fresh = capture(core, context, "我现在明确讨论TEST-project 配色 蓝色。", when="2026-09-06T12:00:00Z")
    assert fresh.suppressed
    snapshot = tmp_path / "TEST-inherited-suppression.sqlite3"
    sqlite_backup(core.storage.path, snapshot)
    restore_snapshot(core, context, snapshot)
    assert core.source(context, fresh.ref, 1).suppressed
    assert core.search_sources(context, "蓝色", automatic=True) == ()


@pytest.mark.parametrize("restore", [False, True])
def test_unmute_resumes_unstarted_evaluation_including_after_restore(app, tmp_path, restore):
    core, context = app
    item, _source, _proposal, registered = _candidate(core, context)
    assert registered.work_queued
    owner = management(core, context)
    owner.apply(owner.preview([item.ref], mode="suppress"))
    snapshot = tmp_path / "TEST-muted-candidate.sqlite3"
    sqlite_backup(core.storage.path, snapshot)
    owner.apply(owner.preview([item.ref], mode="unmute"))
    if restore:
        restore_snapshot(core, context, snapshot)
    lifecycle, evaluations, work = _candidate_rows(core)
    assert lifecycle[0]["processing_state"] == "pending_evaluation"
    assert evaluations[0]["state"] == "queued"
    assert work[0]["state"] == "pending"
    assert len(evaluations) == len(work) == 1
    _finish_source_work(core)
    core.clock.now = work[0]["available_at"]
    evaluator = Evaluator()
    core.drain_worker(context, max_items=8, remaining_seconds=10, consolidation=evaluator)
    assert evaluator.calls == 1


def test_unmute_does_not_repeat_an_already_attempted_evaluation(app):
    core, context = app
    item, _source, _proposal, _registered = _candidate(core, context)
    with core.storage.write(context) as tx:
        tx._check(write=True).execute("UPDATE candidate_evaluations SET model_attempted_at=?", (core.clock.utc_now(),))
    owner = management(core, context)
    owner.apply(owner.preview([item.ref], mode="suppress"))
    owner.apply(owner.preview([item.ref], mode="unmute"))
    lifecycle, evaluations, work = _candidate_rows(core)
    assert lifecycle[0]["processing_state"] == "waiting_evidence"
    assert evaluations[0]["state"] == work[0]["state"] == "obsolete"


def test_restore_keeps_a_missing_source_suppressed_when_it_is_replayed(app, tmp_path):
    core, context = app
    item, _source = initial(core, context)
    owner = management(core, context)
    owner.apply(owner.preview([item.ref], mode="suppress"))
    snapshot = tmp_path / "TEST-before-inherited-source.sqlite3"
    sqlite_backup(core.storage.path, snapshot)
    fresh = capture(core, context, "我现在明确讨论TEST-project 配色 蓝色。", key="TEST-late-restatement")
    assert fresh.suppressed
    owner.apply(owner.preview([item.ref], mode="unmute"))
    restore_snapshot(core, context, snapshot)
    core.record_event(context, fresh.event, scope_id=fresh.scope_id, remaining_seconds=10)
    assert core.source(context, fresh.ref, 1).suppressed


def test_restore_keeps_each_source_versions_actual_suppression(app, tmp_path):
    core, context = app
    item, _source = initial(core, context)
    owner = management(core, context)
    owner.apply(owner.preview([item.ref], mode="suppress"))
    first = capture(core, context, "我现在明确讨论TEST-project 配色 蓝色。", key="TEST-revised-source")
    event = dict(first.event, content="NameOne uses green.", source_revision=2)
    core.record_event(context, event, scope_id=first.scope_id, remaining_seconds=10)
    assert not core.source(context, first.ref, 2).suppressed
    snapshot = tmp_path / "TEST-per-version-suppression.sqlite3"
    sqlite_backup(core.storage.path, snapshot)
    restore_snapshot(core, context, snapshot)
    assert core.source(context, first.ref, 1).suppressed
    assert not core.source(context, first.ref, 2).suppressed
    owner.apply(owner.preview([first.ref], mode="unmute"))
    assert not core.source(context, first.ref, 1).suppressed


def test_transaction_view_cannot_gain_write_scopes_or_outlive_its_owner(app):
    core, context = app
    with core.storage.read(context) as tx:
        view = tx.with_context(context)
        with pytest.raises(ContractError, match="read_only"):
            tx.with_context(context, writable=True)
        with pytest.raises(ContractError, match="ACCESS_DENIED"):
            tx.with_context(replace(context, allowed_scope_ids=frozenset({"TEST-other-scope"})))
    with pytest.raises(ContractError, match="transaction_closed"):
        view.status()
