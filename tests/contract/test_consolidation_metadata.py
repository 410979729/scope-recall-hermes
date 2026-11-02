"""Trusted request metadata is reconstructed before the strict storage contract."""

import json
from copy import deepcopy
from dataclasses import replace

import jsonschema
import pytest
from scope_recall.contracts import ContractError, validate_payload
from scope_recall.core.consolidate import candidate_evaluation_messages
from scope_recall.core.episodes import source_watermark
from scope_recall.core.language_resources import model_consolidation_schema
from scope_recall.core.worker_consolidation import decode_consolidation_result
from test_candidate_evidence_window import _candidate
from test_claims import app, capture, draft
from test_episodes import resume
from test_worker import FakeConsolidation, consolidation_payload, worker_app


@pytest.mark.parametrize("mode", ["missing", "wrong", "reversed", "null"])
def test_batch_sources_come_from_the_request_in_its_order(app, mode):
    core, ctx = app
    first = capture(core, ctx, "TEST first source.")
    second = capture(core, ctx, "TEST second source.")
    sources = (second, first)
    value = consolidation_payload(*sources)
    expected = value["source_refs"][:]
    if mode == "missing":
        del value["source_refs"]
    else:
        value["source_refs"] = {"wrong": ["TEST-other@9"], "reversed": expected[::-1], "null": None}[mode]
    decoded = decode_consolidation_result(json.dumps(value), sources)
    assert decoded["source_refs"] == expected
    validate_payload("consolidation_result", decoded)


@pytest.mark.parametrize("field", ["evidence_refs", "source_watermark", "episode_ref"])
@pytest.mark.parametrize("missing", [True, False])
def test_resume_metadata_is_bound_to_the_request(app, field, missing):
    core, ctx = app
    first = capture(core, ctx, "TEST 整理验收报告。")
    second = capture(core, ctx, "TEST 检查输出。")
    sources = (second, first)
    proposal = resume(first)
    if missing:
        del proposal[field]
    else:
        proposal[field] = {"evidence_refs": ["TEST-other@9"], "source_watermark": "wrong", "episode_ref": "TEST-other"}[
            field
        ]
    value = dict(consolidation_payload(*sources), resume_proposals=[proposal])
    context = {"episode_ref": "TEST-bound-episode"} if field == "episode_ref" else {}
    decoded = decode_consolidation_result(json.dumps(value), sources, **context)
    refs = [f"{s.ref}@{s.revision}" for s in sources]
    saved = decoded["resume_proposals"][0]
    assert saved["evidence_refs"] == refs
    assert saved["source_watermark"] == source_watermark(refs)
    assert saved["episode_ref"] == context.get("episode_ref")
    assert saved["goal"] == proposal["goal"]


def test_copy_fields_are_absent_from_the_model_schema():
    schema = model_consolidation_schema()
    assert not {"protocol_version", "source_refs"} & schema["properties"].keys()
    resume_schema = schema["properties"]["resume_proposals"]["items"]
    assert not {"evidence_refs", "source_watermark", "episode_ref"} & resume_schema["properties"].keys()
    assert "evidence_refs" in schema["$defs"]["evidence_text"]["properties"]


@pytest.mark.parametrize("missing", [True, False])
def test_candidate_identity_and_unused_outputs_are_code_owned(app, missing):
    core, ctx = app
    source = capture(core, ctx, "TEST-build result ready.")
    proposal = draft(source, kind="fact", subject="TEST-build", predicate="result", value="ready")
    candidate = _candidate(source, proposal["evidence_spans"])
    if missing:
        for field in ("kind", "subject", "predicate"):
            del proposal[field]
    else:
        proposal.update(kind="decision", subject="TEST-other", predicate="other")
    value = consolidation_payload(source, claims=[proposal])
    value["resume_proposals"] = None
    value["reference_proposals"] = None
    decoded = decode_consolidation_result(json.dumps(value), (source,), candidate=candidate)
    saved = decoded["claim_proposals"][0]
    assert {k: saved[k] for k in ("kind", "subject", "predicate")} == {
        k: candidate.payload[k] for k in ("kind", "subject", "predicate")
    }
    assert decoded["resume_proposals"] == decoded["reference_proposals"] == []
    schema = json.loads(candidate_evaluation_messages(candidate, (source,))[0]["content"].split("Schema: ", 1)[1])
    assert set(schema["properties"]) == {"claim_proposals"}
    assert not {"kind", "subject", "predicate"} & schema["properties"]["claim_proposals"]["items"]["properties"].keys()


def test_supplied_metadata_does_not_grant_authority_to_another_source(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 配色蓝色。")
    proposal = draft(source)
    proposal["evidence_spans"][0]["source_ref"] = "TEST-not-supplied"
    with pytest.raises(ContractError):
        decode_consolidation_result(json.dumps(consolidation_payload(source, claims=[proposal])), (source,))


def test_bad_source_copy_does_not_cost_a_worker_retry(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST-project 配色蓝色。")
    value = consolidation_payload(source, claims=[draft(source)])
    value["source_refs"] = ["TEST-invented@1"]
    model = FakeConsolidation(lambda sources, **kwargs: deepcopy(value))
    receipt = core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=20)
    assert model.calls == 1 and receipt.completed == 1 and receipt.retried == receipt.failed == 0


def test_resume_copy_fields_do_not_cost_a_worker_retry(worker_app):
    core, ctx, clock = worker_app
    ctx = replace(ctx, task_anchor="TEST-report")
    source = capture(core, ctx, "TEST 整理验收报告。")
    value = consolidation_payload(source)
    value["resume_proposals"] = [
        resume(source, episode_ref="TEST-wrong", evidence_refs=["TEST-wrong@9"], source_watermark="wrong")
    ]
    model = FakeConsolidation(lambda sources, **kwargs: deepcopy(value))
    receipt = core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=20)
    assert model.calls == 1 and receipt.completed == 1 and receipt.retried == receipt.failed == 0
    episode = core.episodes(ctx)[0]
    assert episode.resume["episode_ref"] == episode.ref
    assert episode.resume["evidence_refs"] == [f"{source.ref}@{source.revision}"]
    assert episode.resume["source_watermark"] == source_watermark(episode.resume["evidence_refs"])


@pytest.mark.parametrize("kind", ["fact", "procedure", "intention", "alias"])
def test_candidate_schema_retains_its_kind_specific_constraints(app, kind):
    core, ctx = app
    source = capture(core, ctx, "TEST-build result ready.")
    proposal = draft(source)
    for field in ("kind", "subject", "predicate"):
        del proposal[field]
    schema = model_consolidation_schema(candidate_kind=kind)
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors({"claim_proposals": [proposal]}))
    assert (not errors) == (kind == "fact")
    if errors:
        assert any(error.validator == "required" and kind in error.message for error in errors)


@pytest.mark.parametrize("candidate_request", [False, True])
@pytest.mark.parametrize("mode", ["empty", "claims_only", "claim_proposals", "resume_proposals", "reference_proposals"])
def test_omitted_and_null_arrays_mean_no_proposals(app, candidate_request, mode):
    core, ctx = app
    source = capture(core, ctx, "TEST no durable proposal.")
    value = {} if mode == "empty" else {"claim_proposals": []} if mode == "claims_only" else {mode: None}
    candidate = _candidate(source, []) if candidate_request else None
    decoded = decode_consolidation_result(json.dumps(value), (source,), candidate=candidate)
    assert decoded == consolidation_payload(source)


def test_a_partial_envelope_keeps_valid_claims(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 配色蓝色。")
    value = decode_consolidation_result(json.dumps({"claim_proposals": [draft(source)]}), (source,))
    assert len(value["claim_proposals"]) == 1
    assert value["claim_proposals"][0]["value_text"] == "蓝色"
    assert value["resume_proposals"] == value["reference_proposals"] == []
    validate_payload("consolidation_result", value)


@pytest.mark.parametrize("kind", [None, "fact"])
def test_model_proposal_arrays_are_optional_with_empty_defaults(kind):
    schema = model_consolidation_schema(candidate_kind=kind)
    for field, prop in schema["properties"].items():
        assert field not in schema["required"]
        assert prop["default"] == []
    jsonschema.Draft202012Validator(schema).validate({})


@pytest.mark.parametrize("candidate_request", [False, True])
@pytest.mark.parametrize("field", ["claim_proposals", "resume_proposals", "reference_proposals"])
@pytest.mark.parametrize("wrong", ["TEST-invalid-array", "", False, 0, {}])
def test_wrong_proposal_array_types_are_still_refused(app, field, candidate_request, wrong):
    core, ctx = app
    source = capture(core, ctx, "TEST no durable proposal.")
    candidate = _candidate(source, []) if candidate_request else None
    value = dict(consolidation_payload(source), **{field: wrong})
    with pytest.raises(ContractError) as failure:
        decode_consolidation_result(json.dumps(value), (source,), candidate=candidate)
    assert failure.value.code == "INPUT_INVALID" and failure.value.field == field


@pytest.mark.parametrize("candidate_request", [False, True])
@pytest.mark.parametrize("field", ["claim_proposals", "resume_proposals", "reference_proposals"])
def test_empty_items_in_nonempty_proposal_arrays_are_still_refused(app, field, candidate_request):
    core, ctx = app
    source = capture(core, ctx, "TEST no durable proposal.")
    candidate = _candidate(source, []) if candidate_request else None
    value = dict(consolidation_payload(source), **{field: [{}]})
    with pytest.raises(ContractError) as failure:
        decode_consolidation_result(json.dumps(value), (source,), candidate=candidate)
    assert failure.value.field.startswith(field + "[0].")


@pytest.mark.parametrize("raw", ["", "TEST not JSON", '{"claim_proposals":[', '"TEST scalar"', "[]"])
def test_non_json_truncated_and_nonobject_results_are_still_refused(app, raw):
    core, ctx = app
    source = capture(core, ctx, "TEST no durable proposal.")
    with pytest.raises((ContractError, ValueError)):
        decode_consolidation_result(raw, (source,))


@pytest.mark.parametrize("field", ["claim_proposals", "resume_proposals", "reference_proposals"])
@pytest.mark.parametrize("missing", [False, True])
def test_storage_still_requires_complete_proposal_arrays(app, field, missing):
    core, ctx = app
    source = capture(core, ctx, "TEST no durable proposal.")
    value = consolidation_payload(source)
    if missing:
        del value[field]
    else:
        value[field] = None
    with pytest.raises(ContractError) as failure:
        validate_payload("consolidation_result", value)
    assert failure.value.field == field


@pytest.mark.parametrize(
    "value", [{}, {"claim_proposals": None}, {"resume_proposals": None, "reference_proposals": None}]
)
def test_empty_model_results_complete_without_decode_retry(worker_app, value):
    core, ctx, clock = worker_app
    capture(core, ctx, "TEST suppose I lived in an imaginary city.")
    model = FakeConsolidation(lambda sources, **kwargs: deepcopy(value))
    receipt = core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=20)
    assert model.calls == 1 and receipt.completed == 1 and receipt.retried == receipt.failed == 0
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM work_error_details WHERE stage='decode'").fetchone()[0] == 0
        assert tx._check().execute("SELECT count(*) FROM claim_versions").fetchone()[0] == 0


def test_empty_candidate_result_keeps_the_candidate_without_decode_retry(app):
    from test_candidate_lifecycle import _candidate as stored_candidate
    from test_candidate_lifecycle import _finish_source_work

    core, ctx = app
    saved, source, proposal, registration = stored_candidate(core, ctx)
    _finish_source_work(core)

    class EmptyCandidate:
        calls = 0

        def evaluate_candidate(self, candidate, sources, **kwargs):
            self.calls += 1
            return "{}"

    model = EmptyCandidate()
    receipt = core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=20)
    assert model.calls == 1 and receipt.completed == 1 and receipt.retried == receipt.failed == 0
    assert [claim.state for claim in core.claim_history(ctx, saved.ref)] == ["proposed"]


@pytest.mark.parametrize(
    ("field", "nested"),
    [("claim_proposals", "conditions"), ("resume_proposals", "decisions"), ("reference_proposals", "candidate_refs")],
)
def test_nested_null_arrays_are_not_defaulted(app, field, nested):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 配色蓝色。")
    item = {
        "claim_proposals": draft(source),
        "resume_proposals": resume(source),
        "reference_proposals": dict(
            mention="TEST item",
            candidate_refs=[],
            resolved_ref=None,
            resolution="unresolved",
            evidence_refs=[f"{source.ref}@1"],
        ),
    }[field]
    item[nested] = None
    value = dict(consolidation_payload(source), **{field: [item]})
    with pytest.raises(ContractError) as failure:
        decode_consolidation_result(json.dumps(value), (source,))
    assert failure.value.field == f"{field}[0].{nested}"
