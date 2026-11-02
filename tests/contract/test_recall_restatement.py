"""Restatement safety over synthetic sources, complete bodies and scoped evidence."""

import json
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest
from scope_recall.adapters.clients import CodexHookHandler
from scope_recall.adapters.clients.identity import resolve_runtime_audience, trusted_context
from scope_recall.adapters.hermes.shared_entries import attach_shared_record, client_entry_record, read_shared_payload
from scope_recall.adapters.runtime_wiring import render_host_recall_context
from scope_recall.contracts import TrustedSourcePrincipal
from scope_recall.core import recall_restatement
from scope_recall.core.deadline import RequestDeadline, using_request_deadline
from scope_recall.core.recall_packet import RecallPacketCompiler, packet_item, render_recall_packet_context
from scope_recall.core.recall_restatement import mark_restatement, prefer_originals
from scope_recall.core.retrieval import CandidateRef, SearchContext

from tests.contract.test_claims import draft
from tests.contract.test_recall_selection import restatement_store
from tests.host.codex.test_shared_client import store as client_store
from tests.v11_support import recall_request, source_event


def _record(store, text, *, entry=0, scope="TEST-scope", principal="owner", refs=(), when=None, **context_fields):
    core, contexts = store
    actor = contexts[entry]
    origin = "human_direct" if entry == 0 else "assistant_visible"
    kind = "human" if entry == 0 else "assistant"
    identity_ref = (
        "principal:" + kind + ":TEST-" + (actor.entry_id if entry and principal == "owner" else str(principal))
    )
    identity = None if principal is None else TrustedSourcePrincipal(kind, "verified", identity_ref)
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
        _search(restatement_store, source_principal=None, owner_resolver=None)
        if case == "unknown_reader"
        else _search(restatement_store)
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
def test_proper_substring_retelling_is_kept(restatement_store, echo):
    core, _ = restatement_store
    first = _record(restatement_store, "Atlas deploy uses blue icons for Atlas deployment and blue icons.")
    repeat = _record(restatement_store, echo, entry=1)
    search = _search(restatement_store)
    with core.storage.read(search.trusted_context) as tx:
        result = prefer_originals(tx, _pairs(core, tx, search, first, repeat), search)
    assert [obj.ref for _, obj in result] == [first.ref, repeat.ref]
    assert result[1][1].basis == "derived_summary"


@pytest.mark.parametrize(
    "original,echo",
    [
        ("不要发布蓝色图标", "发布蓝色图标"),
        ("Only after approval: Atlas deploy uses blue icons", "Atlas deploy uses blue icons"),
        ("未经批准，不要 deploy Atlas blue icons", "deploy Atlas blue icons"),
    ],
)
def test_retelling_that_drops_original_control_clause_is_kept(restatement_store, original, echo):
    core, _ = restatement_store
    first = _record(restatement_store, original)
    repeat = _record(restatement_store, echo, entry=1, refs=(f"{first.ref}@1",))
    search = _search(restatement_store)
    with core.storage.read(search.trusted_context) as tx:
        result = prefer_originals(tx, _pairs(core, tx, search, first, repeat), search)
    assert [obj.ref for _, obj in result] == [first.ref, repeat.ref]
    assert result[1][1].content == echo
    assert result[1][1].basis == "derived_summary"


@pytest.mark.parametrize("host", ["hermes", "codex", "claude-code", "workbuddy", "dsh"])
def test_rendered_retelling_names_original_without_changing_evidence(restatement_store, host):

    core, contexts = restatement_store
    first = _record(restatement_store, "TEST-project 配色使用蓝色图标，背景使用白色。")
    repeat = _record(restatement_store, "你之前说过：" + first.event["content"] + "需要附上色样。", entry=1)
    packet = core.recall_packet(contexts[2], recall_request(query="TEST-project 配色"), deadline_seconds=10)
    item = next(row for row in packet["items"] if row["ref"] == repeat.ref)
    rendered = render_recall_packet_context(packet, installation_id="TEST-render", session_id="TEST-render")
    injected = render_host_recall_context(rendered.canonical_text, context=rendered.context, entry=(host, host))
    body = json.loads(injected.split("\n", 1)[1])
    visible = next(row for row in body["items"] if row["ref"] == repeat.ref)
    assert f"{first.ref}@1" in visible["qualification_reason"]
    assert "entry-b retelling" in visible["qualification_reason"].lower()
    assert visible["entries"] == [{"id": "entry-b", "name": "entry-b"}]
    assert len(visible["qualification_reason"]) <= 120
    assert visible["content"] == item["content"] == repeat.event["content"]
    assert visible["evidence_refs"] == item["evidence_refs"] == [f"{repeat.ref}@1"]
    assert "restatement_of" not in item


def test_expiry_keeps_completed_pair_and_all_bodies(restatement_store, monkeypatch):
    core, _ = restatement_store
    first = _record(restatement_store, "Atlas deploy uses blue icons")
    repeat = _record(restatement_store, first.event["content"], entry=1)
    other = _record(restatement_store, "Beacon database recovery keeps queued snapshots")
    later = _record(restatement_store, "You said: " + other.event["content"], entry=1)
    search = _search(restatement_store)
    now = [core.clock.monotonic()]
    calls = []
    original = recall_restatement._Pool.compatible

    def expire_second(pool, *args):
        value = original(pool, *args)
        calls.append(value)
        if len(calls) == 2:
            now[0] += 1
        return value

    monkeypatch.setattr(recall_restatement._Pool, "compatible", expire_second)
    with core.storage.read(search.trusted_context) as tx:
        one = _pairs(core, tx, search, first, repeat)
        two = _pairs(core, tx, search, other, later)
        pairs = [one[0], two[0], one[1], two[1]]
        result = prefer_originals(tx, pairs, search, clock=lambda: now[0])
    assert [obj.ref for _, obj in result] == [first.ref, repeat.ref, later.ref, other.ref]
    assert result[1][1].basis == "derived_summary"
    assert result[2][1].basis == "observed"


@pytest.mark.parametrize("origin", ["human_direct", "assistant_visible"])
def test_one_sided_pool_skips_pairing_setup(restatement_store, monkeypatch, origin):
    core, _ = restatement_store
    first = _record(restatement_store, "Atlas deploy uses blue icons")
    repeat = _record(restatement_store, "You said: Atlas deploy uses blue icons", entry=1)
    search = _search(restatement_store)

    def forbidden(*args, **kwargs):
        pytest.fail("one-sided candidate pool entered pairing setup")

    monkeypatch.setattr(recall_restatement._Pool, "__init__", forbidden)
    with core.storage.read(search.trusted_context) as tx:
        pairs = [pair for pair in _pairs(core, tx, search, first, repeat) if pair[1].origin == origin]
        assert prefer_originals(tx, pairs, search) == pairs


@pytest.mark.parametrize("reader_entry", [0, 1])
@pytest.mark.parametrize("reader_origin", ["human_direct", "memory_reinjection"])
def test_real_client_capture_pairs_assistant_under_same_installation_owner(
    client_store, tmp_path, monkeypatch, reader_entry, reader_origin
):
    from scope_recall.adapters.clients import CodexHookHandler
    from scope_recall.adapters.clients.identity import resolve_runtime_audience, trusted_context
    from scope_recall.adapters.shared_entries import (
        attach_shared_record,
        client_entry_record,
        read_shared_payload,
    )

    root, _, client, scope = client_store
    config = read_shared_payload(root)
    codex = tmp_path / "TEST-codex-entry"
    attach_shared_record(
        root,
        client_entry_record(
            host="codex",
            home=codex,
            entry_id="test-codex",
            display_name="入口 B",
            attached_at="2026-09-24T20:00:00Z",
            allowed_scope_ids=[scope],
            writable_scope_ids=[scope],
            capture_scope_id=scope,
        ),
        now="2026-09-24T20:00:00Z",
    )
    assert config["installation_id"]
    a = CodexHookHandler.from_home(str(client), "claude-code")
    b = CodexHookHandler.from_home(str(codex), "codex")
    original = "TEST-project 配色使用蓝色，图标和背景统一采用蓝色。"
    echo = "你之前说过：" + original + "印刷另附纸张色样。"
    try:
        for hook in (a, b):
            monkeypatch.setattr(hook, "ensure_runtime", lambda *args, **kwargs: None)
        a.handle_payload(
            dict(
                hook_event_name="UserPromptSubmit",
                session_id="TEST-first",
                prompt_id="TEST-a",
                prompt=original,
                cwd=str(tmp_path),
            )
        )
        b.handle_payload(
            dict(
                hook_event_name="Stop",
                session_id="TEST-second",
                turn_id="TEST-b",
                last_assistant_message=echo,
                cwd=str(tmp_path),
            )
        )
        contexts = [
            trusted_context(hook.config, resolve_runtime_audience(hook.config, None), session_id="TEST-reader")
            for hook in (a, b)
        ]
        assert contexts[0].source_principal.principal_ref != contexts[1].source_principal.principal_ref
        hook = (a, b)[reader_entry]
        reader = trusted_context(
            hook.config,
            resolve_runtime_audience(hook.config, None),
            session_id="TEST-reader",
            actor_origin=reader_origin,
        )
        search = SearchContext.from_request(
            recall_request(query="TEST-project 配色", mode="current"),
            reader,
            now=b.core.clock.utc_now(),
            deadline=b.core.clock.monotonic() + 5,
        )
        with b.core.storage.read(reader) as tx:
            ids = {
                row["content"]: row["event_id"]
                for row in tx._check().execute("SELECT event_id,content FROM source_events")
            }
            first, repeat = tx.source(ids[original], 1), tx.source(ids[echo], 1)
            assert first.event["source_principal"]["kind"] == "human"
            assert repeat.event["source_principal"]["kind"] == "assistant"
            assert not repeat.event["evidence_refs"]
            from scope_recall.adapters import shared_entries

            read_json, reads = shared_entries._read_json, []

            def counted_read(path, **kwargs):
                reads.append(str(path))
                return read_json(path, **kwargs)

            monkeypatch.setattr(shared_entries, "_read_json", counted_read)
            # Owner identity is a functional assertion; deadline fallback is checked
            # separately below with the actual packet's production clock and gaps.
            stamp = b.core.clock.monotonic()
            result = prefer_originals(tx, _pairs(b.core, tx, search, first, repeat), search, clock=lambda: stamp)
            assert len(reads) == 1, reads
            # Reusing a trusted context must not carry grants into a later request.
            reads.clear()
            prefer_originals(tx, _pairs(b.core, tx, search, first, repeat), search, clock=lambda: stamp)
            assert len(reads) == 1, reads
        assert [obj.ref for _, obj in result] == [first.ref, repeat.ref]
        assert result[1][1].basis == "derived_summary"
        packet = b.core.recall_packet(reader, recall_request(query="TEST-project 配色"), deadline_seconds=10)
        rendered = render_recall_packet_context(packet, installation_id="TEST-render", session_id="TEST-third")
        injected = render_host_recall_context(rendered.canonical_text, context=rendered.context)
        visible = next(item for item in json.loads(injected.split("\n", 1)[1])["items"] if item["ref"] == repeat.ref)
        if "qualification_reason" in visible:
            assert f"{first.ref}@1" in visible["qualification_reason"]
            assert "test-codex retelling" in visible["qualification_reason"]
        else:
            assert "restatement_pairing_incomplete" in packet["gaps"], packet
            assert first.ref in {item["ref"] for item in packet["items"]}
        assert visible["content"] == echo
        assert visible["evidence_refs"] == [f"{repeat.ref}@1"]
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("echo", ["Atlas deploy uses blue icons", "  Atlas\tdeploy uses\nblue icons  "])
def test_only_whitespace_normalized_body_equality_can_be_omitted(restatement_store, echo):
    core, _ = restatement_store
    first = _record(restatement_store, "Atlas deploy uses blue icons")
    repeat = _record(restatement_store, echo, entry=1)
    search = _search(restatement_store)
    with core.storage.read(search.trusted_context) as tx:
        result = prefer_originals(tx, _pairs(core, tx, search, first, repeat), search)
    assert [obj.ref for _, obj in result] == [first.ref]


def test_release_removes_attribution_when_no_original_remains_visible(restatement_store):
    core, _ = restatement_store
    first = _record(restatement_store, "Atlas deploy uses blue icons")
    hidden = _record(restatement_store, "Atlas deploy uses blue icons", scope="TEST-group-a")
    repeat = _record(restatement_store, "You said: Atlas deploy uses blue icons", entry=1)
    search = _search(restatement_store, allowed_scope_ids=frozenset({"TEST-scope"}))
    compiler = RecallPacketCompiler(core.recall_pipeline.storage_reader, clock=core.clock)
    with core.storage.read(search.trusted_context) as tx:
        candidate, obj = _pairs(core, tx, search, first, repeat)[0]
        old = mark_restatement(obj, (f"{hidden.ref}@1", "TEST-missing@1"))
        fresh = compiler._recheck(tx, search, candidate, old, [])
    item = packet_item(fresh)
    assert item["basis"] == "observed"
    assert "qualification_reason" not in item
    assert fresh.content == repeat.event["content"]


def test_attribution_keeps_existing_qualification_within_schema_limit(restatement_store):
    core, _ = restatement_store
    first = _record(restatement_store, "Atlas deploy uses blue icons")
    repeat = _record(restatement_store, "You said: Atlas deploy uses blue icons", entry=1)
    search = _search(restatement_store)
    with core.storage.read(search.trusted_context) as tx:
        _, obj = _pairs(core, tx, search, first, repeat)[0]
    labels = json.dumps([{"id": "e" + "a" * 31, "name": "TEST long entry"}])
    obj = replace(
        obj,
        metadata=tuple((key, value) for key, value in obj.metadata if key != "entries")
        + (("entries", labels), ("qualification_reason", "awaiting_owner_adoption")),
    )
    labelled = mark_restatement(obj, (f"{first.ref}@1",))
    reason = packet_item(labelled)["qualification_reason"]
    assert f"{first.ref}@1" in reason
    assert "awaiting_owner_adoption" in reason
    assert len(reason) <= 120
    assert mark_restatement(obj, ("TEST-" + "a" * 200 + "@1",)) is obj


@pytest.mark.parametrize("case", ["missing_resolver", "different_owner", "unbound_echo_entry"])
def test_owner_binding_is_required_even_with_visible_evidence_links(restatement_store, case):
    core, contexts = restatement_store
    first = _record(restatement_store, "Atlas deploy uses blue icons")
    repeat = _record(restatement_store, "You said: Atlas deploy uses blue icons", entry=1, refs=(f"{first.ref}@1",))
    existing = contexts[2].owner_resolver

    def resolve(entry, principal, memo):
        if case == "unbound_echo_entry" and entry == "entry-b":
            return None
        if case == "different_owner" and entry == "entry-a":
            return "TEST-other-owner"
        return existing(entry, principal, memo)

    search = _search(restatement_store, owner_resolver=None if case == "missing_resolver" else resolve)
    with core.storage.read(search.trusted_context) as tx:
        pairs = _pairs(core, tx, search, first, repeat)
        assert prefer_originals(tx, pairs, search) == pairs


@pytest.mark.parametrize("kind", [None, "host", "assistant"])
def test_reader_entry_owner_is_independent_of_reader_speaker_kind(restatement_store, kind):
    core, _ = restatement_store
    first = _record(restatement_store, "Atlas deploy uses blue icons")
    repeat = _record(restatement_store, "You said: Atlas deploy uses blue icons", entry=1)
    principal = None if kind is None else TrustedSourcePrincipal(kind, "verified", "principal:TEST-reader")
    search = _search(restatement_store, source_principal=principal)
    with core.storage.read(search.trusted_context) as tx:
        result = prefer_originals(tx, _pairs(core, tx, search, first, repeat), search)
    assert [obj.ref for _, obj in result] == [first.ref, repeat.ref]
    assert search.trusted_context.source_principal is principal


def test_owner_manifest_failure_is_cached_only_until_the_next_request(client_store, monkeypatch):
    from scope_recall.adapters import host_identity as identity
    from scope_recall.adapters.clients.config import load_shared_client

    _, _, client, _ = client_store
    config = load_shared_client(client, "claude-code")
    reader = trusted_context(config, resolve_runtime_audience(config, None), session_id="TEST-owner-cache")
    read, calls = identity.read_shared_payload, []
    available = False

    def counted_read(root):
        calls.append(root)
        if not available:
            raise OSError("TEST unreadable manifest")
        return read(root)

    monkeypatch.setattr(identity, "read_shared_payload", counted_read)
    resolve, memo = reader.owner_resolver, {}
    assert resolve(reader.entry_id, None, memo) is None
    available = True
    assert resolve(reader.entry_id, None, memo) is None
    assert len(calls) == 1
    assert resolve(reader.entry_id, None, {}) == reader.binding.installation_id
    assert len(calls) == 2
