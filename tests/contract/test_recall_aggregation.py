"""Cross-session evidence uses synthetic, independently captured sources."""

import json
import sys
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest
from scope_recall.contracts import TrustedSourcePrincipal
from scope_recall.core.management import MemoryManagement
from scope_recall.core.recall_aggregation import MAX_MEMBERS, MAX_UNITS
from scope_recall.core.recall_budget import canonical_render_json, estimate_tokens
from scope_recall.core.recall_packet import RecallPacketCompiler, render_recall_packet_context
from scope_recall.core.restore import InstallationMaintenance
from scope_recall.core.retrieval import SearchContext

from tests.contract.test_claims import accept, app, capture, draft
from tests.contract.test_deletion import authorize, request
from tests.contract.test_recall_selection import restatement_store
from tests.v11_support import recall_request, source_event


def _sources(store, count, language="en"):
    core, ctx = store
    core.clock.now = "2026-09-20T12:00:00Z"
    sources = []
    for index in range(count):
        actor = replace(
            ctx,
            session_id=f"TEST-visit-{index}",
            source_principal=TrustedSourcePrincipal("human", "verified", "principal:human:TEST-owner"),
        )
        text = (
            f"I visited TEST-aquarium on September {index + 1}, and saw exhibit {index}."
            if language == "en"
            else f"我在九月{index + 1}日参观了TEST水族馆，看了展区{index}。"
        )
        sources.append(capture(core, actor, text, when=f"2026-09-{index + 1:02d}T12:00:00Z"))
    return sources


def _summaries(packet):
    return [item for item in packet["items"] if item.get("qualification_reason") == "Cross-session evidence"]


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("count", [3, 8])
@pytest.mark.parametrize("wording", ["count", "list", "topic"])
def test_sessions_fold_before_the_six_slot_limit(app, language, count, wording):
    core, ctx = app
    sources = _sources(app, count, language)
    query = {
        "en": {
            "count": "How many times have I visited TEST-aquarium?",
            "list": "List all the times I visited TEST-aquarium.",
            "topic": "TEST-aquarium visited",
        },
        "zh": {
            "count": "我一共参观了TEST水族馆几次？",
            "list": "列出我参观TEST水族馆的所有记录",
            "topic": "参观 TEST水族馆",
        },
    }[language][wording]
    packet = core.recall_packet(ctx, recall_request(query=query), deadline_seconds=10)
    summaries = _summaries(packet)
    assert len(summaries) == 1, packet
    summary = summaries[0]
    assert set(summary["evidence_refs"]) == {f"{source.ref}@1" for source in sources}
    assert f"{count} records" in summary["content"]
    assert "2026-09-01" in summary["content"]
    assert f"2026-09-{count:02d}" in summary["content"]
    assert summary["basis"] == "derived_summary"
    rendered = render_recall_packet_context(packet, installation_id="TEST", session_id="TEST-render")
    for source in sources:
        assert f"{source.ref}@1" in rendered.canonical_text
    schema = json.loads((Path(__file__).parents[2] / "contracts/recall_packet.schema.json").read_text())
    jsonschema.validate(packet, schema)


def test_unrelated_query_does_not_create_a_summary(app):
    core, ctx = app
    _sources(app, 8)
    packet = core.recall_packet(ctx, recall_request(query="TEST-volcano telescope calibration"), deadline_seconds=10)
    assert not _summaries(packet)


def test_one_session_does_not_become_cross_session_evidence(app):
    core, ctx = app
    for index in range(8):
        capture(core, ctx, f"I visited TEST-aquarium exhibit {index}.")
    packet = core.recall_packet(ctx, recall_request(query="TEST-aquarium visited"), deadline_seconds=10)
    assert not _summaries(packet)


@pytest.mark.parametrize("mode", ["delete", "suppress", "private"])
def test_withdrawn_member_is_excluded_and_cached_view_is_fenced(app, mode):
    core, ctx = app
    sources = _sources(app, 8)
    search = SearchContext.from_request(
        recall_request(query="TEST-aquarium visited"),
        ctx,
        now=core.clock.utc_now(),
        deadline=core.clock.monotonic() + 10,
    )
    result = core.recall_pipeline.search(search)
    assert any(dict(obj.metadata).get("aggregation_members") for obj in result.items)
    target = sources[0]
    if mode == "private":
        owner = MemoryManagement(core, InstallationMaintenance(ctx))
        core.clock.now = target.event["occurred_at"]
        owner.private_on(target="session", session=target.session_id, mode="full")
        core.clock.now = "2026-09-20T12:00:00Z"
    else:
        authorize(core, ctx, target, mode=mode)
        core.forget(ctx, request(target, mode=mode), remaining_seconds=10)
    released = RecallPacketCompiler(core.recall_pipeline.storage_reader, clock=core.clock).compile(
        search, result, core.storage
    )
    assert not _summaries(released)
    packet = core.recall_packet(ctx, recall_request(query="TEST-aquarium visited"), deadline_seconds=10)
    assert f"{target.ref}@1" not in {ref for item in packet["items"] for ref in item["evidence_refs"]}
    assert "7 records" in _summaries(packet)[0]["content"]


@pytest.mark.parametrize("boundary", ["project_id", "branch_id", "principal"])
def test_other_subject_or_audience_is_never_folded_into_the_group(app, boundary):
    core, ctx = app
    sources = _sources(app, 4)
    actor = replace(
        ctx,
        session_id="TEST-other",
        source_principal=TrustedSourcePrincipal("human", "verified", "principal:human:TEST-owner"),
    )
    actor = (
        replace(actor, source_principal=TrustedSourcePrincipal("human", "verified", "principal:human:TEST-other"))
        if boundary == "principal"
        else replace(actor, **{boundary: "TEST-other"})
    )
    other = capture(core, actor, "I visited TEST-aquarium and saw exhibit other.")
    packet = core.recall_packet(ctx, recall_request(query="TEST-aquarium visited"), deadline_seconds=10)
    summary = _summaries(packet)[0]
    assert set(summary["evidence_refs"]) == {f"{source.ref}@1" for source in sources}
    assert other.ref not in summary["content"]


def test_member_limit_and_budget_are_visible(app):
    core, ctx = app
    _sources(app, 20)
    packet = core.recall_packet(ctx, recall_request(query="TEST-aquarium visited"), deadline_seconds=10)
    summary = _summaries(packet)[0]
    assert len(summary["evidence_refs"]) <= MAX_MEMBERS
    assert estimate_tokens(canonical_render_json(summary)) <= MAX_UNITS
    assert "of 20 matching candidate records" in summary["content"]
    assert "aggregation_truncated" in packet["gaps"]
    assert estimate_tokens(canonical_render_json(packet)) <= 4096


def test_duplicate_body_and_cross_entry_restatement_do_not_add_to_count(restatement_store):
    core, contexts = restatement_store
    ctx = contexts[0]
    sources = _sources((core, ctx), 4)
    actor = replace(
        ctx,
        session_id="TEST-copy",
        source_principal=TrustedSourcePrincipal("human", "verified", "principal:human:TEST-owner"),
    )
    capture(core, actor, sources[0].event["content"])
    capture(
        core,
        replace(
            actor,
            entry_id="entry-b",
            source_principal=TrustedSourcePrincipal("assistant", "verified", "principal:assistant:TEST-entry-b"),
        ),
        sources[0].event["content"],
        origin="assistant_visible",
        evidence_refs=[f"{sources[0].ref}@1"],
    )
    packet = core.recall_packet(ctx, recall_request(query="TEST-aquarium visited"), deadline_seconds=10)
    assert "4 records" in _summaries(packet)[0]["content"]


def test_normalized_fact_group_uses_current_versions(app):
    core, ctx = app
    sources = _sources(app, 4)
    claims = []
    for index, source in enumerate(sources):
        actor = replace(ctx, session_id=source.session_id)
        claim = accept(
            core,
            actor,
            draft(
                source,
                str(index),
                subject="TEST-aquarium",
                predicate="visited",
                kind="decision",
                statement_kind="assertion",
                conditions=[f"exhibit {index}"],
            ),
        ).items[0]
        claims.append(claim)
    packet = core.recall_packet(ctx, recall_request(query="TEST-aquarium visited"), deadline_seconds=10)
    summary = _summaries(packet)[0]
    assert "4 records" in summary["content"]
    assert set(summary["evidence_refs"]) == {f"{source.ref}@1" for source in sources}
    for claim in claims:
        assert f"{claim.ref}@{claim.revision}" in summary["content"]


def test_corrected_source_version_is_not_counted_or_released(app):
    core, ctx = app
    sources = _sources(app, 8)
    search = SearchContext.from_request(
        recall_request(query="TEST-aquarium visited"),
        ctx,
        now=core.clock.utc_now(),
        deadline=core.clock.monotonic() + 10,
    )
    result = core.recall_pipeline.search(search)
    old = sources[0]
    actor = replace(
        ctx,
        session_id=old.session_id,
        source_principal=TrustedSourcePrincipal("human", "verified", "principal:human:TEST-owner"),
    )
    capture(
        core, actor, "The corrected record concerns TEST-library books.", key=old.event["source_event_key"], revision=2
    )
    packet = RecallPacketCompiler(core.recall_pipeline.storage_reader, clock=core.clock).compile(
        search, result, core.storage
    )
    assert not _summaries(packet)
    packet = core.recall_packet(ctx, recall_request(query="TEST-aquarium visited"), deadline_seconds=10)
    assert "7 records" in _summaries(packet)[0]["content"]
    assert old.ref not in _summaries(packet)[0]["content"]


@pytest.mark.parametrize("mode", ["history", "as_of"])
def test_historical_reads_keep_individual_records(app, mode):
    core, ctx = app
    _sources(app, 8)
    query = recall_request(query="TEST-aquarium visited", mode=mode)
    if mode == "as_of":
        query["as_of"] = core.clock.utc_now()
    packet = core.recall_packet(ctx, query, deadline_seconds=10)
    assert not _summaries(packet)


def test_scopes_are_separate_even_when_the_reader_can_read_both(restatement_store):
    core, contexts = restatement_store
    sources = _sources((core, contexts[0]), 4)
    hidden = []
    for index in range(3):
        actor = replace(contexts[0], session_id=f"TEST-hidden-{index}")
        receipt = core.record_event(
            actor,
            source_event(
                source_event_key=f"TEST-hidden/{index}",
                content=f"I visited TEST-aquarium and saw secret exhibit {index}.",
                occurred_at="2026-09-02T12:00:00Z",
            ),
            scope_id="TEST-group-a",
            remaining_seconds=10,
        )
        hidden.append(f"{receipt.event_refs[0].ref}@1")
    reader = replace(contexts[2], allowed_scope_ids=frozenset({"TEST-scope"}))
    packet = core.recall_packet(reader, recall_request(query="TEST-aquarium visited"), deadline_seconds=10)
    assert set(_summaries(packet)[0]["evidence_refs"]) == {f"{source.ref}@1" for source in sources}
    assert not set(hidden).intersection(ref for item in packet["items"] for ref in item["evidence_refs"])
    broad = core.recall_packet(contexts[2], recall_request(query="TEST-aquarium visited"), deadline_seconds=10)
    for item in _summaries(broad):
        assert not (
            set(item["evidence_refs"]).intersection(hidden)
            and set(item["evidence_refs"]).intersection(f"{source.ref}@1" for source in sources)
        )


def test_ungrouped_information_keeps_a_separate_packet_slot(app):
    core, ctx = app
    _sources(app, 8)
    note = capture(core, ctx, "TEST-aquarium visited exhibit guide costs ten credits.", origin="assistant_visible")
    packet = core.recall_packet(ctx, recall_request(query="TEST-aquarium visited", max_items=2), deadline_seconds=10)
    assert _summaries(packet)
    assert note.ref in {item["ref"] for item in packet["items"]}


def test_additional_query_terms_do_not_split_one_event_class(app):
    core, ctx = app
    core.clock.now = "2026-09-20T12:00:00Z"
    for index in range(8):
        actor = replace(
            ctx,
            session_id=f"TEST-mixed-{index}",
            source_principal=TrustedSourcePrincipal("human", "verified", "principal:human:TEST-owner"),
        )
        exhibit = "reef" if index % 2 else "turtles"
        capture(core, actor, f"I visited TEST-aquarium and saw the {exhibit} exhibit {index}.")
    packet = core.recall_packet(ctx, recall_request(query="TEST-aquarium visited reef turtles"), deadline_seconds=10)
    summaries = _summaries(packet)
    assert len(summaries) == 1
    assert "8 records" in summaries[0]["content"]


def test_lifecycle_and_restatement_preserve_every_remaining_original(restatement_store):
    core, contexts = restatement_store
    ctx = contexts[0]
    sources = _sources((core, ctx), 8)
    old = sources[0]
    capture(
        core,
        replace(ctx, session_id=old.session_id),
        "The corrected record concerns TEST-library books.",
        key=old.event["source_event_key"],
        revision=2,
    )
    for index, mode in [(1, "delete"), (4, "suppress")]:
        authorize(core, ctx, sources[index], mode=mode)
        core.forget(ctx, request(sources[index], mode=mode), remaining_seconds=10)
    owner = MemoryManagement(core, InstallationMaintenance(ctx))
    owner.private_on(target="session", session=sources[3].session_id, mode="full")
    actor = replace(
        contexts[1],
        actor_origin="assistant_visible",
        source_principal=TrustedSourcePrincipal("assistant", "verified", "principal:assistant:TEST-entry-b"),
    )
    core.record_event(
        actor,
        source_event(
            source_event_key="TEST-echo",
            role="assistant",
            origin="assistant_visible",
            content=sources[2].event["content"],
            evidence_refs=[f"{sources[2].ref}@1"],
        ),
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    packet = core.recall_packet(
        contexts[2], recall_request(query="How many times have I visited TEST-aquarium?"), deadline_seconds=10
    )
    summary = _summaries(packet)[0]
    expected = {f"{sources[i].ref}@1" for i in (2, 5, 6, 7)}
    assert set(summary["evidence_refs"]) == expected
    assert "4 records" in summary["content"]


@pytest.mark.parametrize("budget", [512, 1024, 2048, 4096, 8000])
def test_aggregation_respects_small_and_large_packet_budgets(app, budget):
    core, ctx = app
    _sources(app, 8)
    packet = core.recall_packet(
        ctx, recall_request(query="TEST-aquarium visited", budget_tokens=budget), deadline_seconds=10
    )
    grouped_units = sum(estimate_tokens(canonical_render_json(item)) for item in _summaries(packet))
    assert grouped_units <= min(MAX_UNITS, budget // 2)
    assert estimate_tokens(canonical_render_json(packet)) <= budget


def test_group_compaction_keeps_evidence_already_selected_under_a_small_budget(app, monkeypatch):
    core, ctx = app
    core.clock.now = "2026-09-20T12:00:00Z"
    owner = TrustedSourcePrincipal("human", "verified", "principal:human:TEST-owner")
    for index in range(15):
        actor = replace(ctx, session_id="TEST-dense" if index < 12 else f"TEST-other-{index}", source_principal=owner)
        capture(
            core,
            actor,
            f"I visited TEST-aquarium and saw exhibit {index}.",
            when="2026-09-10T12:00:00Z" if index < 12 else "2026-09-01T12:00:00Z",
        )
    query = recall_request(query="TEST-aquarium visited", budget_tokens=1400)
    with monkeypatch.context() as patch:
        patch.setattr(
            sys.modules["scope_recall.core.recall"], "aggregate_evidence", lambda tx, ranked, *args, **kwargs: ranked
        )
        baseline = core.recall_packet(ctx, query, deadline_seconds=10)
    assert len(baseline["items"]) == 6
    packet = core.recall_packet(ctx, query, deadline_seconds=10)
    old_refs = {ref for item in baseline["items"] for ref in item["evidence_refs"]}
    new_refs = {ref for item in packet["items"] for ref in item["evidence_refs"]}
    assert old_refs <= new_refs


@pytest.mark.parametrize("close_again", [False, True])
def test_private_rules_opened_after_request_time_invalidate_the_inflight_view(app, close_again):
    core, ctx = app
    sources = _sources(app, 8)
    search = SearchContext.from_request(
        recall_request(query="TEST-aquarium visited"),
        ctx,
        now=core.clock.utc_now(),
        deadline=core.clock.monotonic() + 10,
    )
    result = core.recall_pipeline.search(search)
    assert any(dict(obj.metadata).get("aggregation_members") for obj in result.items)
    owner = MemoryManagement(core, InstallationMaintenance(ctx))
    core.clock.now = "2026-09-21T12:00:00Z"
    owner.private_on(target="session", session=sources[0].session_id, mode="full")
    if close_again:
        core.clock.now = "2026-09-22T12:00:00Z"
        owner.private_off(target="session", session=sources[0].session_id)
    packet = RecallPacketCompiler(core.recall_pipeline.storage_reader, clock=core.clock).compile(
        search, result, core.storage
    )
    assert not _summaries(packet)


def test_query_judgement_uses_original_members_instead_of_summary_caption(app, monkeypatch):
    core, ctx = app
    sources = _sources(app, 4)
    judged = []
    original = core.recall_pipeline._result

    def observe(working, epoch, ranked, query_items, *args):
        judged.extend(query_items)
        return original(working, epoch, ranked, query_items, *args)

    monkeypatch.setattr(core.recall_pipeline, "_result", observe)
    packet = core.recall_packet(ctx, recall_request(query="TEST-aquarium visited"), deadline_seconds=10)
    assert _summaries(packet)
    assert {obj.ref for obj in judged} == {source.ref for source in sources}
    assert all("aggregation_members" not in dict(obj.metadata) for obj in judged)
