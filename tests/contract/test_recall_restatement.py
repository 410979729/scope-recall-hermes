"""Restatement safety over synthetic sources, complete bodies and scoped evidence."""

import json
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest
from scope_recall.contracts import TrustedSourcePrincipal
from scope_recall.core import recall_restatement
from scope_recall.core.deadline import RequestDeadline, using_request_deadline
from scope_recall.core.recall_packet import RecallPacketCompiler, packet_item
from scope_recall.core.recall_restatement import mark_restatement, prefer_originals
from scope_recall.core.retrieval import CandidateRef, SearchContext

from tests.contract.test_claims import draft
from tests.contract.test_recall_selection import restatement_store
from tests.v11_support import recall_request, source_event


def _record(store, text, *, entry=0, scope="TEST-scope", principal="owner", refs=(), when=None, **context_fields):
    core, contexts = store
    actor = contexts[entry]
    identity = (
        None if principal is None else TrustedSourcePrincipal("human", "verified", "principal:human:TEST-" + principal)
    )
    origin = "human_direct" if entry == 0 else "assistant_visible"
    actor = replace(actor, actor_origin=origin, source_principal=identity, **context_fields)
    saved = core.record_event(
        actor,
        source_event(
            source_event_key=f"TEST-restatement/{next(core.test_sequence)}",
            content=text,
            origin=origin,
            role="user" if entry == 0 else "assistant",
            evidence_refs=list(refs),
            occurred_at=when or ("2026-09-01T12:00:00Z" if entry == 0 else "2026-09-02T12:00:00Z"),
        ),
        scope_id=scope,
        remaining_seconds=10,
    )
    return core.source(actor, saved.event_refs[0].ref, 1)


def _search(store, **changes):
    core, contexts = store
    return SearchContext.from_request(
        recall_request(query="TEST-project 配色", mode="current"),
        replace(contexts[2], **changes),
        now=core.clock.utc_now(),
        deadline=core.clock.monotonic() + 5,
    )


def _pairs(core, tx, search, original, echo):
    found = []
    for source, score in ((echo, 0.03), (original, 0.01)):
        candidate = CandidateRef("event", source.ref, source.revision, "lexical", fusion_score=score)
        obj = core.recall_pipeline.storage_reader.hydrate(tx, candidate, search)
        assert obj is not None
        found.append((candidate, obj))
    return found


@pytest.mark.parametrize(
    "original,echo",
    [
        ("发布蓝色图标", "不发布蓝色图标"),
        ("发布蓝色图标", "仅测试时，发布蓝色图标"),
        ("项目重试次数为3次", "项目重试次数为8次"),
        ("Atlas deploy uses blue icons", "Only after approval: Atlas deploy uses blue icons"),
        ("Atlas deploy uses blue icons", "Never: Atlas deploy uses blue icons"),
        ("Atlas deploy uses blue icons", "2026-09-02: Atlas deploy uses blue icons"),
        ("Alice pays Bob for Atlas delivery", "Bob pays Alice for Atlas delivery"),
        ("Atlas deploy uses x blue icons", "Atlas deploy uses y blue icons"),
        ("Atlas deploy uses blue icons", "Atlas deploy uses blue blue icons"),
        ("Atlas uses Polish fonts", "Atlas uses polish fonts"),
    ],
)
def test_added_polarity_condition_number_order_single_character_and_repetition_survive(
    restatement_store, original, echo
):
    core, _ = restatement_store
    first = _record(restatement_store, original)
    repeat = _record(restatement_store, echo, entry=1)
    search = _search(restatement_store)
    with core.storage.read(search.trusted_context) as tx:
        pairs = _pairs(core, tx, search, first, repeat)
        result = prefer_originals(tx, pairs, search)
    assert [obj.ref for _, obj in result] == [first.ref, repeat.ref]
    assert result[1][1].basis == "derived_summary"


def test_tail_after_query_term_limit_is_not_discarded(restatement_store):
    core, _ = restatement_store
    original = " ".join(f"word{i}" for i in range(140))
    first = _record(restatement_store, original)
    repeat = _record(restatement_store, original + " safety deadline changed", entry=1)
    search = _search(restatement_store)
    with core.storage.read(search.trusted_context) as tx:
        result = prefer_originals(tx, _pairs(core, tx, search, first, repeat), search)
    assert [obj.ref for _, obj in result] == [first.ref, repeat.ref]


def test_body_beyond_query_length_limit_does_not_empty_recall(restatement_store):
    core, contexts = restatement_store
    original = "TEST-project 配色. " + "Atlas deploy uses blue icons. " * 330
    _record(restatement_store, original)
    _record(restatement_store, "用户偏好：" + original, entry=1)
    packet = core.recall_packet(
        contexts[2], recall_request(query="TEST-project 配色", budget_tokens=8000), deadline_seconds=10
    )
    assert packet["items"], packet
    assert not any("INPUT_INVALID" in gap for gap in packet["gaps"])


def test_incomplete_body_comparison_cannot_remove_content(restatement_store):
    core, _ = restatement_store
    original = "Atlas deploy uses blue icons. " * 1500
    first = _record(restatement_store, original)
    repeat = _record(restatement_store, original, entry=1)
    search = _search(restatement_store)
    with core.storage.read(search.trusted_context) as tx:
        result = prefer_originals(tx, _pairs(core, tx, search, first, repeat), search)
    echo = next(obj for _, obj in result if obj.ref == repeat.ref)
    assert dict(echo.metadata).get("restatement_comparison") == "incomplete"


@pytest.mark.parametrize("boundary", ["scope", "project", "branch"])
def test_unreadable_claim_and_its_sources_do_not_enter_roots(restatement_store, boundary):
    core, contexts = restatement_store
    changes = (
        {"project_id": "TEST-hidden"}
        if boundary == "project"
        else {"branch_id": "TEST-hidden"}
        if boundary == "branch"
        else {}
    )
    scope = "TEST-group-a" if boundary == "scope" else "TEST-scope"
    hidden = _record(restatement_store, "TEST-project 配色 蓝色。", scope=scope, **changes)
    creator = replace(contexts[0], **changes)
    payload = draft(hidden, "蓝色", predicate="配色")
    receipt = core.records.accept_claim_proposals(
        creator,
        dict(
            protocol_version="1.1",
            source_refs=[f"{hidden.ref}@1"],
            claim_proposals=[payload],
            resume_proposals=[],
            reference_proposals=[],
        ),
        scope_id=scope,
        remaining_seconds=10,
    )
    claim = receipt.items[0]
    repeat = _record(restatement_store, "TEST-project 配色 蓝色。", entry=1, refs=(f"{claim.ref}@1", "TEST-missing@1"))
    reader = replace(contexts[2], allowed_scope_ids=frozenset({"TEST-scope"}))
    with core.storage.read(reader) as tx:
        assert not tx.claims.versions(claim.ref)
        assert tx.source(hidden.ref, 1) is None
        candidate = CandidateRef("event", repeat.ref, 1, "lexical")
        obj = core.recall_pipeline.storage_reader.hydrate(
            tx, candidate, _search(restatement_store, allowed_scope_ids=reader.allowed_scope_ids)
        )
        roots = recall_restatement._roots(tx, obj)
    assert roots == {f"{repeat.ref}@1"}


@pytest.mark.parametrize(
    "case", ["other_scope", "other_person", "other_echo_person", "unknown_reader", "unknown_original", "unknown_echo"]
)
def test_missing_audience_or_person_proof_preserves_order_and_content(restatement_store, case):
    core, _ = restatement_store
    first = _record(
        restatement_store,
        "Atlas deploy uses blue icons",
        scope="TEST-group-a" if case == "other_scope" else "TEST-scope",
        principal="other" if case == "other_person" else None if case == "unknown_original" else "owner",
    )
    repeat = _record(
        restatement_store,
        "You said: Atlas deploy uses blue icons",
        entry=1,
        principal=None if case == "unknown_echo" else "other" if case == "other_echo_person" else "owner",
    )
    search = (
        _search(restatement_store, source_principal=None) if case == "unknown_reader" else _search(restatement_store)
    )
    with core.storage.read(search.trusted_context) as tx:
        pairs = _pairs(core, tx, search, first, repeat)
        assert prefer_originals(tx, pairs, search) == pairs


def test_cross_scope_pair_needs_visible_explicit_evidence(restatement_store):
    core, _ = restatement_store
    first = _record(restatement_store, "Atlas deploy uses blue icons", scope="TEST-group-a")
    repeat = _record(restatement_store, "You said: Atlas deploy uses blue icons", entry=1, refs=(f"{first.ref}@1",))
    search = _search(restatement_store)
    with core.storage.read(search.trusted_context) as tx:
        result = prefer_originals(tx, _pairs(core, tx, search, first, repeat), search)
    assert [obj.ref for _, obj in result] == [first.ref, repeat.ref]
    assert result[1][1].basis == "derived_summary"


def test_lexical_prescreen_avoids_unrelated_lineage_reads(restatement_store, monkeypatch):
    core, _ = restatement_store
    first = _record(restatement_store, "Atlas deploy uses blue icons")
    repeat = _record(restatement_store, "Atlas database recovery uses queued transaction snapshots", entry=1)
    search = _search(restatement_store)
    calls = []
    original = recall_restatement._roots

    def track(*args, **kwargs):
        calls.append(args[1].ref)
        return original(*args, **kwargs)

    monkeypatch.setattr(recall_restatement, "_roots", track)
    with core.storage.read(search.trusted_context) as tx:
        pairs = _pairs(core, tx, search, first, repeat)
        assert prefer_originals(tx, pairs, search) == pairs
    assert calls == []


def test_expired_pairing_restores_original_order(restatement_store):
    core, _ = restatement_store
    first = _record(restatement_store, "Atlas deploy uses blue icons")
    repeat = _record(restatement_store, "You said: Atlas deploy uses blue icons", entry=1)
    search = _search(restatement_store)
    with core.storage.read(search.trusted_context) as tx:
        pairs = _pairs(core, tx, search, first, repeat)
        with using_request_deadline(RequestDeadline.from_budget(0)):
            assert prefer_originals(tx, pairs, search) == pairs


def test_current_correction_leads_when_old_original_is_not_a_candidate(restatement_store):
    core, contexts = restatement_store
    first = _record(restatement_store, "TEST-project 配色使用蓝色，图标和背景统一采用蓝色。")
    payload = draft(first, "蓝色", predicate="配色")
    receipt = core.records.accept_claim_proposals(
        contexts[0],
        dict(
            protocol_version="1.1",
            source_refs=[f"{first.ref}@1"],
            claim_proposals=[payload],
            resume_proposals=[],
            reference_proposals=[],
        ),
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    repeat = _record(
        restatement_store, "你之前说过：" + first.event["content"], entry=1, refs=(f"{receipt.items[0].ref}@1",)
    )
    correction = _record(restatement_store, "TEST-project 配色改为银色。", when="2026-09-03T12:00:00Z")
    assert core.current_claim(contexts[0], receipt.items[0].ref).payload["value_text"] == "银色"
    search = _search(restatement_store)
    with core.storage.read(search.trusted_context) as tx:
        pairs = _pairs(core, tx, search, correction, repeat)
        assert first.ref not in {obj.ref for _, obj in pairs}
        result = prefer_originals(tx, pairs, search)
    assert result[0][1].ref == correction.ref
    assert result[1][1].ref == repeat.ref


def test_release_rechecks_restatement_reference_visibility(restatement_store):
    core, _ = restatement_store
    first = _record(restatement_store, "Atlas deploy uses blue icons")
    hidden = _record(restatement_store, "Atlas deploy uses blue icons", scope="TEST-group-a")
    repeat = _record(restatement_store, "You said: Atlas deploy uses blue icons", entry=1)
    search = _search(restatement_store, allowed_scope_ids=frozenset({"TEST-scope"}))
    compiler = RecallPacketCompiler(core.recall_pipeline.storage_reader, clock=core.clock)
    with core.storage.read(search.trusted_context) as tx:
        candidate, obj = _pairs(core, tx, search, first, repeat)[0]
        old = mark_restatement(obj, (f"{first.ref}@1", f"{hidden.ref}@1", "TEST-missing@1"))
        fresh = compiler._recheck(tx, search, candidate, old, [])
    assert json.loads(dict(fresh.metadata)["restatement_of"]) == [f"{first.ref}@1"]


def test_unnegotiated_packet_uses_existing_provenance_fields(restatement_store):
    core, _ = restatement_store
    first = _record(restatement_store, "Atlas deploy uses blue icons")
    repeat = _record(restatement_store, "You said: Atlas deploy uses blue icons", entry=1)
    search = _search(restatement_store)
    with core.storage.read(search.trusted_context) as tx:
        _, obj = _pairs(core, tx, search, first, repeat)[0]
        item = packet_item(mark_restatement(obj, (f"{first.ref}@1",)))
    assert "restatement_of" not in item
    assert item["basis"] == "derived_summary"
    schema = json.loads((Path(__file__).resolve().parents[2] / "contracts/recall_packet.schema.json").read_text())
    old_item_schema = schema["properties"]["items"]["items"]
    old_item_schema["properties"].pop("restatement_of", None)
    jsonschema.Draft202012Validator(old_item_schema).validate(item)


@pytest.mark.parametrize("echo", ["Atlas deploy uses blue icons", "blue icons for Atlas deployment"])
def test_only_a_complete_ordered_word_run_can_be_omitted(restatement_store, echo):
    core, _ = restatement_store
    first = _record(restatement_store, "Atlas deploy uses blue icons for Atlas deployment and blue icons.")
    repeat = _record(restatement_store, echo, entry=1)
    search = _search(restatement_store)
    with core.storage.read(search.trusted_context) as tx:
        result = prefer_originals(tx, _pairs(core, tx, search, first, repeat), search)
    assert [obj.ref for _, obj in result] == [first.ref]
