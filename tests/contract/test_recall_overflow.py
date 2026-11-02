"""Neutral overflow records keep ordinary full evidence intact."""

import json
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest
from scope_recall.contracts import TrustedSourcePrincipal
from scope_recall.core.management import MemoryManagement
from scope_recall.core.recall_budget import canonical_render_json, estimate_tokens
from scope_recall.core.recall_packet import RecallPacketCompiler, render_recall_packet_context
from scope_recall.core.restore import InstallationMaintenance
from scope_recall.core.retrieval import SearchContext

from tests.contract.test_claims import app, capture
from tests.contract.test_deletion import authorize, request
from tests.contract.test_recall_selection import restatement_store
from tests.v11_support import recall_request, source_event

LABEL = "Other-session matches"


def sources(store, count=12, language="en", long=False):
    core, ctx = store
    core.clock.now = "2026-09-30T12:00:00Z"
    result = []
    for i in range(count):
        actor = replace(
            ctx,
            session_id=f"TEST-record-{i}",
            source_principal=TrustedSourcePrincipal("human", "verified", "principal:human:TEST-owner"),
        )
        text = (
            f"Orchard deployment record {i}; "
            + ("safe status details " * 20 if long else "")
            + f"database port {6100 + i}."
            if language == "en"
            else f"测试果园部署记录{i}，" + ("安全状态详情" * 30 if long else "") + f"数据库端口{6100 + i}。"
        )
        result.append(capture(core, actor, text, when=f"2026-09-{i + 1:02d}T12:00:00Z"))
    return result


def overflow(packet):
    return [item for item in packet["items"] if item.get("qualification_reason") == LABEL]


def ordinary(core, ctx, query, monkeypatch):
    from scope_recall.core import recall_packet as module

    with monkeypatch.context() as patch:
        if hasattr(module, "append_overflow"):
            patch.setattr(module, "append_overflow", lambda storage, context, result, packet, *args: packet)
        return core.recall_packet(ctx, query, deadline_seconds=10)


@pytest.mark.parametrize("language", ["en", "zh"])
def test_overflow_appends_without_changing_any_selected_item(app, monkeypatch, language):
    core, ctx = app
    captured = sources(app, language=language)
    query = recall_request(query="Orchard deployment port" if language == "en" else "测试果园 部署 数据库端口")
    base = ordinary(core, ctx, query, monkeypatch)
    packet = core.recall_packet(ctx, query, deadline_seconds=10)
    assert len(base["items"]) == 6
    assert packet["items"][:6] == base["items"]
    assert len(overflow(packet)) == 1
    refs = {ref for item in packet["items"] for ref in item["evidence_refs"]}
    assert refs == {f"{source.ref}@1" for source in captured}
    assert all(packet[k] == base[k] for k in packet if k not in {"items", "diagnostic_ref"})
    schema = json.loads((Path(__file__).parents[2] / "contracts/recall_packet.schema.json").read_text())
    jsonschema.validate(packet, schema)
    rendered = render_recall_packet_context(packet, installation_id="TEST", session_id="TEST-overflow")
    assert all(ref in rendered.canonical_text for ref in refs)
    assert estimate_tokens(canonical_render_json(packet)) <= 4096


def test_windows_never_hide_selected_port_answers(app, monkeypatch):
    core, ctx = app
    captured = sources(app, long=True)
    query = recall_request(query="Orchard deployment port")
    base = ordinary(core, ctx, query, monkeypatch)
    packet = core.recall_packet(ctx, query, deadline_seconds=10)
    assert packet["items"][: len(base["items"])] == base["items"]
    assert overflow(packet)
    view = overflow(packet)[0]
    assert "Excerpt" in view["content"] and "characters" in view["content"] and "clipped" in view["content"]
    assert "inspect" in view["content"]
    assert not set(view["evidence_refs"]).intersection(ref for item in base["items"] for ref in item["evidence_refs"])
    for item in base["items"]:
        source = next(source for source in captured if item["ref"] == source.ref)
        assert item["content"] == source.event["content"]


def test_overflow_does_not_claim_common_words_are_the_same_event(app, monkeypatch):
    core, ctx = app
    sources(app, count=6)
    for i, text in enumerate(["我参观了北京水族馆展区", "Alice没有参观北京水族馆展区", "我计划维修北京水族馆展区"]):
        capture(
            core,
            replace(
                ctx,
                session_id=f"TEST-other-{i}",
                source_principal=TrustedSourcePrincipal("human", "verified", "principal:human:TEST-owner"),
            ),
            "Orchard deployment " + text,
        )
    packet = core.recall_packet(ctx, recall_request(query="Orchard deployment"), deadline_seconds=10)
    assert overflow(packet)
    text = overflow(packet)[0]["content"]
    assert "not deduplicated" in text
    assert "Cross-session evidence" not in text and "time range" not in text


def test_no_selected_packet_stays_empty(app, monkeypatch):
    core, ctx = app
    sources(app)
    packet = core.recall_packet(ctx, recall_request(query="TEST-volcano telescope calibration"), deadline_seconds=10)
    assert packet["items"] == []


@pytest.mark.parametrize("budget", [512, 1024, 2048, 4096, 8000])
def test_overflow_uses_only_spare_budget(app, monkeypatch, budget):
    core, ctx = app
    sources(app, count=18, long=True)
    query = recall_request(query="Orchard deployment port", budget_tokens=budget)
    base = ordinary(core, ctx, query, monkeypatch)
    packet = core.recall_packet(ctx, query, deadline_seconds=10)
    assert packet["items"][: len(base["items"])] == base["items"]
    assert estimate_tokens(canonical_render_json(packet)) <= budget
    for view in overflow(packet):
        assert len(view["evidence_refs"]) <= 12
        assert estimate_tokens(canonical_render_json(view)) <= min(1024, budget // 4)


@pytest.mark.parametrize("mode", ["delete", "suppress", "private", "correct", "isolation_exit"])
def test_tail_release_uses_current_visibility_and_versions(app, monkeypatch, mode):
    core, ctx = app
    captured = sources(app)
    search = SearchContext.from_request(
        recall_request(query="Orchard deployment port"),
        ctx,
        now=core.clock.utc_now(),
        deadline=core.clock.monotonic() + 20,
    )
    result = core.recall_pipeline.search(search)
    target = captured[0]
    if mode == "private":
        MemoryManagement(core, InstallationMaintenance(ctx)).private_on(
            target="session", session=target.session_id, mode="full"
        )
    elif mode == "correct":
        capture(
            core,
            replace(ctx, session_id=target.session_id),
            "The corrected record is about TEST-library books.",
            key=target.event["source_event_key"],
            revision=2,
        )
    elif mode == "isolation_exit":
        reader = core.recall_pipeline.storage_reader
        hydrate = reader.hydrate
        monkeypatch.setattr(
            reader,
            "hydrate",
            lambda tx, candidate, context: None if candidate.ref == target.ref else hydrate(tx, candidate, context),
        )
        assert core.source(ctx, target.ref, 1) is not None
    else:
        authorize(core, ctx, target, mode=mode)
        core.forget(ctx, request(target, mode=mode), remaining_seconds=10)
    packet = RecallPacketCompiler(core.recall_pipeline.storage_reader, clock=core.clock).compile(
        search, result, core.storage
    )
    assert f"{target.ref}@1" not in {ref for item in overflow(packet) for ref in item["evidence_refs"]}
    fresh = core.recall_packet(ctx, recall_request(query="Orchard deployment port"), deadline_seconds=10)
    assert overflow(fresh)
    assert f"{target.ref}@1" not in {ref for item in overflow(fresh) for ref in item["evidence_refs"]}


def test_one_session_has_one_row_and_restatements_do_not_add_rows(restatement_store):
    core, contexts = restatement_store
    ctx = contexts[0]
    captured = sources((core, ctx))
    capture(core, replace(ctx, session_id=captured[0].session_id), "Orchard deployment another matching detail.")
    actor = replace(
        contexts[1],
        actor_origin="assistant_visible",
        source_principal=TrustedSourcePrincipal("assistant", "verified", "principal:assistant:TEST-entry-b"),
    )
    echoed = core.record_event(
        actor,
        source_event(
            source_event_key="TEST-echo",
            role="assistant",
            origin="assistant_visible",
            content=captured[0].event["content"],
            evidence_refs=[f"{captured[0].ref}@1"],
        ),
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    packet = core.recall_packet(contexts[2], recall_request(query="Orchard deployment port"), deadline_seconds=10)
    assert overflow(packet)
    assert f"{echoed.event_refs[0].ref}@1" not in overflow(packet)[0]["evidence_refs"]
    refs = overflow(packet)[0]["evidence_refs"]
    sessions = [core.source(contexts[2], ref.rsplit("@", 1)[0], int(ref.rsplit("@", 1)[1])).session_id for ref in refs]
    assert len(set(sessions)) == len(sessions)


@pytest.mark.parametrize("count,max_items,listed", [(3, 6, False), (8, 6, False), (8, 3, True)])
@pytest.mark.parametrize("language", ["en", "zh"])
def test_small_session_distributions_obey_the_overflow_threshold(app, count, max_items, listed, language):
    core, ctx = app
    sources(app, count=count, language=language)
    query = "List all Orchard deployment ports." if language == "en" else "列出测试果园部署的所有数据库端口"
    packet = core.recall_packet(ctx, recall_request(query=query, max_items=max_items), deadline_seconds=10)
    assert bool(overflow(packet)) is listed


def test_combined_lifecycle_scope_and_restatement_leave_only_valid_session_rows(restatement_store):
    core, contexts = restatement_store
    ctx = contexts[0]
    captured = sources((core, ctx), count=8)
    capture(
        core,
        replace(ctx, session_id=captured[0].session_id),
        "The corrected record concerns TEST-library.",
        key=captured[0].event["source_event_key"],
        revision=2,
    )
    for index, mode in [(1, "delete"), (2, "suppress")]:
        authorize(core, ctx, captured[index], mode=mode)
        core.forget(ctx, request(captured[index], mode=mode), remaining_seconds=10)
    MemoryManagement(core, InstallationMaintenance(ctx)).private_on(
        target="session", session=captured[3].session_id, mode="full"
    )
    actor = replace(
        contexts[1],
        actor_origin="assistant_visible",
        source_principal=TrustedSourcePrincipal("assistant", "verified", "principal:assistant:TEST-entry-b"),
    )
    echo = core.record_event(
        actor,
        source_event(
            source_event_key="TEST-echo",
            role="assistant",
            origin="assistant_visible",
            content=captured[4].event["content"],
            evidence_refs=[f"{captured[4].ref}@1"],
        ),
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    hidden = core.record_event(
        replace(ctx, session_id="TEST-hidden"),
        source_event(source_event_key="TEST-hidden", content="Orchard deployment database port 9999."),
        scope_id="TEST-group-a",
        remaining_seconds=10,
    )
    reader = replace(contexts[2], allowed_scope_ids=frozenset({"TEST-scope"}))
    packet = core.recall_packet(
        reader, recall_request(query="Orchard deployment port", max_items=1), deadline_seconds=10
    )
    assert overflow(packet)
    refs = {ref for item in packet["items"] for ref in item["evidence_refs"]}
    assert refs == {f"{source.ref}@1" for source in captured[4:]}
    assert f"{hidden.event_refs[0].ref}@1" not in refs and f"{echo.event_refs[0].ref}@1" not in refs


def test_optional_reader_failure_cannot_revoke_the_finalized_packet(app, monkeypatch):
    from scope_recall.core import recall_packet as module

    core, ctx = app
    captured = sources(app)
    search = SearchContext.from_request(
        recall_request(query="Orchard deployment port"),
        ctx,
        now=core.clock.utc_now(),
        deadline=core.clock.monotonic() + 20,
    )
    result = core.recall_pipeline.search(search)
    reader = core.recall_pipeline.storage_reader
    compiler = RecallPacketCompiler(reader, clock=core.clock)
    with monkeypatch.context() as patch:
        patch.setattr(module, "append_overflow", lambda storage, context, result, packet, *args: packet)
        base = compiler.compile(search, result, core.storage)
    hydrate = reader.hydrate

    def failure(tx, candidate, context):
        if candidate.ref == captured[0].ref:
            raise OSError("TEST optional reader failure")
        return hydrate(tx, candidate, context)

    monkeypatch.setattr(reader, "hydrate", failure)
    assert compiler.compile(search, result, core.storage) == base


def test_three_original_port_records_stay_complete_without_a_list(app):
    core, ctx = app
    captured = sources(app, count=3, long=True)
    packet = core.recall_packet(ctx, recall_request(query="Orchard deployment port"), deadline_seconds=10)
    assert not overflow(packet)
    assert {item["content"] for item in packet["items"]} == {source.event["content"] for source in captured}


def test_thirteen_real_fact_slots_preserve_selected_thirteenth_original(app, monkeypatch):
    from tests.contract.test_claims import accept, draft

    core, ctx = app
    core.clock.now = "2026-09-30T12:00:00Z"
    captured, claims = [], []
    for i in range(13):
        actor = replace(
            ctx,
            session_id=f"TEST-fact-{i}",
            source_principal=TrustedSourcePrincipal("human", "verified", "principal:human:TEST-owner"),
        )
        source = capture(
            core,
            actor,
            f"For record {i}, Orchard uses database port {6100 + i}.",
            when=f"2026-09-{i + 1:02d}T12:00:00Z",
        )
        captured.append(source)
        claim = accept(
            core,
            actor,
            draft(
                source,
                str(6100 + i),
                subject="Orchard",
                predicate="database port",
                kind="fact",
                statement_kind="assertion",
                conditions=[f"record {i}"],
            ),
        ).items[0]
        assert claim.state == "active"
        claims.append(claim)
    rank = core.recall_pipeline._rank_hydrated
    preferred = [claim.ref for claim in claims[:5]] + [captured[-1].ref]

    def ranked(hydrated, context=None):
        return sorted(
            rank(hydrated, context), key=lambda pair: preferred.index(pair[0].ref) if pair[0].ref in preferred else 99
        )

    monkeypatch.setattr(core.recall_pipeline, "_rank_hydrated", ranked)
    query = recall_request(query="Orchard database port")
    base = ordinary(core, ctx, query, monkeypatch)
    assert captured[-1].ref in {item["ref"] for item in base["items"]}
    packet = core.recall_packet(ctx, query, deadline_seconds=10)
    assert packet["items"][:6] == base["items"]
    assert overflow(packet)
    assert (
        next(item for item in packet["items"] if item["ref"] == captured[-1].ref)["content"]
        == captured[-1].event["content"]
    )


def test_other_records_from_selected_sessions_remain_eligible(app):
    core, ctx = app
    core.clock.now = "2026-09-30T12:00:00Z"
    for i in range(12):
        capture(
            core,
            replace(ctx, session_id=f"TEST-shared-session-{i % 3}"),
            f"Orchard deployment record {i}, database port {6100 + i}.",
            when=f"2026-09-{i + 1:02d}T12:00:00Z",
        )
    packet = core.recall_packet(ctx, recall_request(query="Orchard deployment port"), deadline_seconds=10)
    assert overflow(packet)
    refs = overflow(packet)[0]["evidence_refs"]
    assert len(refs) == 3
    assert not set(refs).intersection(ref for item in packet["items"][:-1] for ref in item["evidence_refs"])


def test_identical_words_in_other_sessions_are_records_not_an_event_identity(app):
    core, ctx = app
    core.clock.now = "2026-09-30T12:00:00Z"
    captured = [
        capture(
            core,
            replace(ctx, session_id=f"TEST-identical-{i}"),
            "Orchard deployment database port 6100.",
            when=f"2026-09-{i + 1:02d}T12:00:00Z",
        )
        for i in range(8)
    ]
    packet = core.recall_packet(ctx, recall_request(query="Orchard deployment port"), deadline_seconds=10)
    assert len([item for item in packet["items"] if item.get("qualification_reason") != LABEL]) == 1
    assert overflow(packet)
    assert {ref for item in packet["items"] for ref in item["evidence_refs"]} == {
        f"{source.ref}@1" for source in captured
    }


def test_full_legacy_item_ceiling_keeps_the_original_packet(app, monkeypatch):
    core, ctx = app
    core.clock.now = "2026-09-30T12:00:00Z"
    for i in range(36):
        capture(
            core,
            replace(ctx, session_id=f"TEST-ceiling-{i}"),
            f"Orchard deployment record {i}; database port {6100 + i}.",
        )
    query = recall_request(query="Orchard deployment port", mode="current", max_items=30, budget_tokens=8000)
    base = ordinary(core, ctx, query, monkeypatch)
    assert len(base["items"]) == 30
    packet = core.recall_packet(ctx, query, deadline_seconds=10)
    assert packet["items"] == base["items"]
    assert not overflow(packet)
    schema = json.loads((Path(__file__).parents[2] / "contracts/recall_packet.schema.json").read_text())
    jsonschema.validate(packet, schema)
