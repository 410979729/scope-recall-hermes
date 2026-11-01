"""Model quotes become exact source slices before publication and support checks."""

import json
import sqlite3

import pytest
from scope_recall.contracts import ContractError, validate_evidence_positions
from scope_recall.core.consolidate import consolidation_messages
from scope_recall.core.consolidation_chunks import ChunkedSource, ConsolidationChunk
from scope_recall.core.worker_consolidation import decode_consolidation_result
from test_claims import capture, draft
from test_worker import FakeConsolidation, app, consolidation_payload, worker_app


@pytest.mark.parametrize("offset", ["near", "utf8", "utf16", "partial", "none"])
def test_model_offsets_are_hints_and_stored_positions_are_unicode(app, offset):
    core, ctx = app
    prefix, quote = "TEST 中文😀前缀。", "TEST-project 配色蓝色。"
    source = capture(core, ctx, prefix + quote)
    proposal = draft(source, subject="TEST-project", predicate="配色", value="蓝色")
    span = proposal["evidence_spans"][0]
    span["quote"] = quote
    starts = {"near": len(prefix) + 2, "utf8": len(prefix.encode()), "utf16": len(prefix.encode("utf-16-le")) // 2}
    if offset in starts:
        span.update(start=starts[offset], end=starts[offset] + len(quote))
    elif offset == "partial":
        span["start"] = -1
    result = decode_consolidation_result(json.dumps(consolidation_payload(source, claims=[proposal])), (source,))
    saved = result["claim_proposals"][0]
    assert saved["evidence_spans"][0] == dict(span, start=len(prefix), end=len(prefix + quote))
    validate_evidence_positions(saved, {(source.ref, source.revision): source.event["content"]})


def test_window_offsets_and_repeated_quotes_prefer_the_authorized_window(app):
    core, ctx = app
    quote = "TEST-project 配色蓝色。"
    prefix = quote + "TEST 归档😀。"
    source = capture(core, ctx, prefix + quote)
    page = ChunkedSource(
        **dict(source.__dict__, event=dict(source.event, content=quote)),
        consolidation_window=ConsolidationChunk(len(prefix), len(prefix + quote), len(prefix + quote)),
    )
    proposal = draft(source, subject="TEST-project", predicate="配色", value="蓝色")
    proposal["evidence_spans"][0].update(quote=quote, start=0, end=len(quote))
    result = decode_consolidation_result(json.dumps(consolidation_payload(source, claims=[proposal])), (page,))
    assert result["claim_proposals"][0]["evidence_spans"][0]["start"] == len(prefix)


@pytest.mark.parametrize(
    "quote,content",
    [
        ("TEST-project 配色 蓝色。", "TEST-project\t配色\n  蓝色。"),
        ("TEST ＡＢＣ ﬁnish。", "TEST ABC finish。"),
        ("TEST café。", "TEST cafe\u0301。"),
    ],
)
def test_normalized_quote_maps_back_to_the_literal_source(app, quote, content):
    core, ctx = app
    source = capture(core, ctx, content)
    proposal = draft(source)
    proposal["evidence_spans"][0].update(quote=quote, start=90, end=100)
    result = decode_consolidation_result(json.dumps(consolidation_payload(source, claims=[proposal])), (source,))
    assert result["claim_proposals"][0]["evidence_spans"][0] == dict(
        source_ref=source.ref, source_revision=1, quote=content, start=0, end=len(content)
    )


def test_repeated_quote_uses_first_match_unless_a_valid_hint_selects_another(app):
    core, ctx = app
    quote = "TEST-project 配色蓝色。"
    source = capture(core, ctx, quote + quote)
    for hint in (None, len(quote)):
        proposal = draft(source)
        span = proposal["evidence_spans"][0]
        span["quote"] = quote
        if hint is not None:
            span.update(start=hint, end=hint + len(quote))
        result = decode_consolidation_result(json.dumps(consolidation_payload(source, claims=[proposal])), (source,))
        assert result["claim_proposals"][0]["evidence_spans"][0]["start"] == (hint or 0)


@pytest.mark.parametrize("version", [None, "wrong", 9])
def test_protocol_constant_is_owned_by_code(app, version):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 配色蓝色。")
    value = consolidation_payload(source)
    if version is None:
        del value["protocol_version"]
    else:
        value["protocol_version"] = version
    assert decode_consolidation_result(json.dumps(value), (source,))["protocol_version"] == "1.1"


def test_model_schema_requests_quotes_without_code_owned_fields(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 配色蓝色。")
    text = consolidation_messages((source,))[0]["content"]
    schema = json.loads(text.split("Schema: ", 1)[1])
    assert "protocol_version" not in schema["properties"]
    assert not {"start", "end"} & schema["$defs"]["evidence_span"]["properties"].keys()


def test_nonexistent_quote_is_rejected_without_losing_a_good_batch_item(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST-project 配色蓝色。")
    good = draft(source, subject="TEST-project", predicate="配色", value="蓝色")
    bad = dict(
        good,
        value_text="红色",
        evidence_spans=[dict(good["evidence_spans"][0], quote="TEST-project 配色红色。", start=0, end=18)],
    )
    model = FakeConsolidation(lambda sources, **kw: consolidation_payload(*sources, claims=[bad, good]))
    core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=20)
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 1
        errors = conn.execute("SELECT stage,error_code,error_field FROM work_error_details").fetchall()
    assert ("claim", "DERIVATION_INVALID", "claim_proposals[0].evidence_spans[0].quote") in errors
    with pytest.raises(ContractError, match="claim_proposals\\[0\\].evidence_spans\\[0\\].quote"):
        decode_consolidation_result(json.dumps(consolidation_payload(source, claims=[bad])), (source,))


def test_acceptance_failure_retries_with_the_field_and_same_budget(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST-project 配色蓝色。")
    proposal = draft(source, subject="TEST-project", predicate="配色", value="蓝色")

    class RepairModel(FakeConsolidation):
        def propose(self, sources, **kwargs):
            self.calls += 1
            feedback = kwargs.get("validation_feedback")
            self.feedbacks.append(feedback)
            repaired = feedback == {"code": "INPUT_INVALID", "field": "claim_proposals[0].value_text"}
            return json.dumps(
                consolidation_payload(*sources, claims=[dict(proposal, value_text="蓝色" if repaired else [])])
            )

    model = RepairModel(None)
    core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=20)
    clock.advance(seconds=65, iso="2026-09-06T12:01:05Z")
    core.drain_worker(ctx, consolidation=model, max_items=2, remaining_seconds=20)
    assert model.calls == 2
    assert model.feedbacks == [None, {"code": "INPUT_INVALID", "field": "claim_proposals[0].value_text"}]
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 1


def test_schema_failure_reports_the_proposal_index(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 配色蓝色。")
    proposal = draft(source)
    proposal["value_text"] = []
    with pytest.raises(ContractError) as failure:
        decode_consolidation_result(json.dumps(consolidation_payload(source, claims=[proposal])), (source,))
    assert failure.value.field == "claim_proposals[0].value_text"


def test_repair_keeps_original_indices_after_dropping_bad_evidence(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 配色蓝色。")
    bad_quote = draft(source)
    bad_quote["evidence_spans"][0]["quote"] = "TEST 不在原文中的片段。"
    bad_shape = dict(draft(source), value_text=[])
    with pytest.raises(ContractError) as failure:
        decode_consolidation_result(
            json.dumps(consolidation_payload(source, claims=[bad_quote, bad_shape])), (source,), rejected_evidence=[]
        )
    assert failure.value.field == "claim_proposals[1].value_text"


def test_serialized_quote_transport_recovery_also_ignores_bad_offsets(app):
    core, ctx = app
    source = capture(core, ctx, r"TEST-project 路径 C:\\example。")
    proposal = draft(source, subject="TEST-project", predicate="路径", value=r"C:\example")
    proposal["evidence_spans"][0].update(quote=r"TEST-project 路径 C:\example。", start=900, end=901)
    result = decode_consolidation_result(json.dumps(consolidation_payload(source, claims=[proposal])), (source,))
    span = result["claim_proposals"][0]["evidence_spans"][0]
    assert span["quote"] == source.event["content"] and span["start"] == 0


@pytest.mark.parametrize("missing", ["source_ref", "source_revision"])
def test_unavailable_evidence_version_only_rejects_its_proposal(worker_app, missing):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST-project 配色蓝色。")
    good = draft(source)
    span = dict(good["evidence_spans"][0])
    span[missing] = "TEST-not-authorized" if missing == "source_ref" else 2
    bad = dict(good, evidence_spans=[span])
    model = FakeConsolidation(lambda sources, **kw: consolidation_payload(*sources, claims=[bad, good]))
    core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=20)
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM work_error_details WHERE stage='claim'").fetchone()[0] == 1


def test_literal_negative_state_keeps_its_meaning_when_positions_are_added(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST-project 状态不可用。")
    proposal = draft(source, subject="TEST-project", predicate="状态", value="不可用", kind="fact")
    core.drain_worker(
        ctx,
        consolidation=FakeConsolidation(lambda sources, **kw: consolidation_payload(*sources, claims=[proposal])),
        max_items=1,
        remaining_seconds=20,
    )
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT state FROM claim_versions").fetchone()[0] == "active"


def test_literal_update_without_a_semantic_judge_keeps_the_latest_value(worker_app):
    core, ctx, clock = worker_app

    def build(sources, **kwargs):
        return consolidation_payload(
            *sources, claims=[draft(s, value="银色" if "银色" in s.event["content"] else "蓝色") for s in sources]
        )

    model = FakeConsolidation(build)
    capture(core, ctx, "TEST-project 配色蓝色。", when="2026-09-01T12:00:00Z")
    core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=20)
    capture(core, ctx, "TEST-project 配色银色。", when="2026-09-02T12:00:00Z")
    core.drain_worker(ctx, consolidation=model, max_items=2, remaining_seconds=20)
    with sqlite3.connect(core.storage.path) as conn:
        rows = conn.execute(
            "SELECT v.state,v.payload_json FROM claims c JOIN claim_versions v "
            "ON v.claim_id=c.claim_id AND v.revision=c.current_revision"
        ).fetchall()
    assert len(rows) == 1 and rows[0][0] == "active" and json.loads(rows[0][1])["value_text"] == "银色"


def test_positions_preserve_a_known_literal_refusal_when_no_judge_can_run(worker_app):
    from scope_recall.core.claims import evidence_refs, qualify

    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST-project freezes its code on Wednesday.")
    proposal = draft(source, subject="TEST-project", predicate="code freeze day", value="Wednesday", kind="fact")
    with core.storage.read(ctx) as tx:
        expected = qualify(proposal, tx.claims.roots(evidence_refs(proposal)), _legacy=True)
    assert expected.state == "proposed"
    core.drain_worker(
        ctx,
        consolidation=FakeConsolidation(lambda sources, **kw: consolidation_payload(*sources, claims=[proposal])),
        max_items=1,
        remaining_seconds=20,
    )
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT qualification_reason FROM claim_versions").fetchone()[0] == expected.reason


@pytest.mark.parametrize("part", ["claim", "reference"])
def test_cross_field_repairs_are_indexed_without_changing_public_errors(app, part):
    from scope_recall.contracts import validate_payload

    core, ctx = app
    source = capture(core, ctx, "TEST-project 配色蓝色。")
    value = consolidation_payload(source)
    if part == "claim":
        good = draft(source)
        value["claim_proposals"] = [
            good,
            dict(good, valid_from="2026-09-02T00:00:00Z", valid_to="2026-09-01T00:00:00Z"),
        ]
        field, indexed = "valid_interval", "claim_proposals[1].valid_interval"
    else:
        good = dict(
            mention="TEST item",
            candidate_refs=[],
            resolved_ref=None,
            resolution="unresolved",
            evidence_refs=value["source_refs"],
        )
        value["reference_proposals"] = [
            good,
            dict(good, candidate_refs=["TEST-artifact@1"], resolved_ref="TEST-other@1", resolution="resolved"),
        ]
        field, indexed = "resolved_ref", "reference_proposals[1].resolved_ref"
    with pytest.raises(ContractError) as public:
        validate_payload("consolidation_result", value)
    assert public.value.field == field
    with pytest.raises(ContractError) as model:
        decode_consolidation_result(json.dumps(value), (source,))
    assert model.value.field == indexed
