import json

import pytest

from scope_recall.contracts import ContractError, MAX_PAYLOAD_BYTES, SourceSnapshot, decode_payload, validate_payload, validate_proposal_references
from v11_support import context, proposal, recall_item, recall_packet, recall_request, source_event


@pytest.mark.parametrize("value", [b'\xff', '{"x":1,"x":2}', '{"nested":{"a":1,"a":2}}', '{"value":NaN}', '{"value":Infinity}', {"x":float("-inf")}, {"x":(1,2)}, {1:"invalid"}, [], "null", "{" , {"x":"\ud800"}])
def test_non_protocol_json_has_stable_sanitized_error(value):
    with pytest.raises(ContractError) as caught:
        decode_payload(value)
    assert caught.value.code == "INPUT_INVALID"
    assert len(str(caught.value)) < 100


def test_oversize_and_deep_payloads_are_rejected():
    with pytest.raises(ContractError, match="INPUT_INVALID"):
        decode_payload({"x":"中" * MAX_PAYLOAD_BYTES})
    with pytest.raises(ContractError, match="INPUT_INVALID"):
        decode_payload('{"x":' + '[' * 40 + '0' + ']' * 40 + '}')


@pytest.mark.parametrize("value", ["2026-02-30T12:00:00Z", "2026-09-05", "2026-09-05T12:00:00", "2026-09-05T12:00:00-04:00", "2026-09-05T25:00:00Z", "yesterday"])
def test_bad_or_non_utc_times_are_rejected(value):
    with pytest.raises(ContractError, match="INPUT_INVALID"):
        validate_payload("source_event", source_event(recorded_at=value))


def test_unknown_occurred_time_does_not_invent_past():
    event = source_event(occurred_at=None, time_precision="unknown")
    assert validate_payload("source_event", event)["occurred_at"] is None
    with pytest.raises(ContractError, match="time_precision"):
        validate_payload("source_event", source_event(occurred_at=None))


@pytest.mark.parametrize("changes", [dict(origin="user"), dict(role="developer"), dict(capture_state="done"), dict(source_revision=0), dict(source_revision=True), dict(content="x" * 65537), dict(protocol_version="1.0")])
def test_source_enums_lengths_and_revisions_are_explicit(changes):
    with pytest.raises(ContractError, match="INPUT_INVALID"):
        validate_payload("source_event", source_event(**changes))


def test_import_preserves_known_origin_without_guessing():
    imported = source_event(origin="imported", source_original_origin="human_direct")
    assert validate_payload("source_event", imported)["source_original_origin"] == "human_direct"
    assert "source_original_origin" not in validate_payload("source_event", source_event(origin="imported"))
    with pytest.raises(ContractError, match="source_original_origin"):
        validate_payload("source_event", source_event(source_original_origin="human_direct"))


@pytest.mark.parametrize("changes", [dict(budget_tokens=True), dict(budget_tokens=63), dict(budget_tokens=8001), dict(max_items=31), dict(mode="as_of"), dict(query=""), dict(mode="forever"), dict(focus_refs=["same"] * 2)])
def test_query_budgets_and_modes_are_bounded(changes):
    with pytest.raises(ContractError, match="INPUT_INVALID"):
        validate_payload("recall_request", recall_request(**changes))


def test_single_decoder_has_same_semantics_for_all_entry_encodings():
    original = recall_request(query="不是全局偏好，仅限 TEST 项目")
    encoded = json.dumps(original, ensure_ascii=False)
    values = [validate_payload("recall_request", v) for v in (original, encoded, encoded.encode("utf-8"))]
    assert values[0] == values[1] == values[2] == original
    values[0]["query"] = "changed"
    assert original["query"] != "changed"


@pytest.mark.parametrize("status,answer,coverage", [("ok","ambiguous","partial"), ("ok","partial","unknown"), ("ok","supported","complete_for_query"), ("partial","supported","partial")])
def test_availability_does_not_collapse_answerability_or_coverage(status, answer, coverage):
    packet = recall_packet(status=status, answerability=answer, coverage=coverage)
    assert validate_payload("recall_packet", packet) == packet


def test_unavailable_cannot_return_memory_or_supported_answer():
    for changes in (dict(status="unavailable"), dict(status="no_match"), dict(items=[], status="unavailable", answerability="supported"), dict(memory_epoch=None)):
        with pytest.raises(ContractError, match="INPUT_INVALID"):
            validate_payload("recall_packet", recall_packet(**changes))
    empty = recall_packet(status="unavailable", items=[], memory_epoch=None, answerability="unknown", coverage="unknown")
    assert validate_payload("recall_packet", empty)["items"] == []


@pytest.mark.parametrize("kind,field", [("procedure","procedure"), ("intention","intention"), ("alias","alias")])
def test_specific_claim_kind_requires_its_structure(kind, field):
    result = proposal()
    result["claim_proposals"][0]["kind"] = kind
    with pytest.raises(ContractError, match="INPUT_INVALID"):
        validate_payload("consolidation_result", result)


def test_proposals_cannot_assign_authority_or_reverse_valid_time():
    result = proposal()
    result["claim_proposals"][0]["state"] = "active"
    with pytest.raises(ContractError, match="INPUT_INVALID"):
        validate_payload("consolidation_result", result)
    result["claim_proposals"][0].pop("state")
    result["claim_proposals"][0].update(valid_from="2026-09-05T12:00:00Z", valid_to="2026-09-04T12:00:00Z")
    with pytest.raises(ContractError, match="valid_interval"):
        validate_payload("consolidation_result", result)


def test_evidence_must_quote_exact_accessible_source_revision(tmp_path):
    result = proposal()
    source = SourceSnapshot("TEST-event", 1, source_event()["content"], "TEST-scope", "human_direct")
    assert validate_proposal_references(result, (source,), context(tmp_path)) == result
    for bad in (SourceSnapshot("TEST-event", 2, source.content, source.scope_id, source.origin), SourceSnapshot("TEST-event", 1, source.content, "TEST-private", source.origin), SourceSnapshot("TEST-event", 1, source.content, source.scope_id, source.origin, True)):
        with pytest.raises(ContractError, match="SOURCE_MISSING"):
            validate_proposal_references(result, (bad,), context(tmp_path))
    result["claim_proposals"][0]["evidence_spans"][0]["quote"] = "不存在的引文"
    with pytest.raises(ContractError, match="evidence_quote"):
        validate_proposal_references(result, (source,), context(tmp_path))
    # A span naming a source the proposal never declared is a different finding
    # and says so, because the repair for one is no repair for the other.
    result["claim_proposals"][0]["evidence_spans"][0]["quote"] = source.content[:8]
    result["claim_proposals"][0]["evidence_spans"][0]["source_revision"] = 9
    with pytest.raises(ContractError, match="evidence_undeclared_source"):
        validate_proposal_references(result, (source,), context(tmp_path))


def test_reference_resolution_cannot_invent_candidate_or_collapse_ambiguity():
    binding = dict(mention="那个颜色", candidate_refs=["TEST-v1", "TEST-v2"], resolved_ref=None, resolution="ambiguous", evidence_refs=["TEST-event@1"])
    assert validate_payload("consolidation_result", proposal(reference_proposals=[binding]))
    for changes in (dict(resolution="resolved", resolved_ref="TEST-v3"), dict(candidate_refs=["TEST-v1"])):
        with pytest.raises(ContractError):
            validate_payload("consolidation_result", proposal(reference_proposals=[binding | changes]))


def test_delete_revisions_only_apply_to_requested_targets():
    value = dict(protocol_version="1.1", target_refs=["TEST-claim"], mode="delete", expected_revisions={"TEST-claim":2})
    assert validate_payload("forget_request", value) == value
    value["expected_revisions"]["TEST-other"] = 3
    with pytest.raises(ContractError, match="expected_revisions"):
        validate_payload("forget_request", value)


@pytest.mark.parametrize("segment,capture_state", [
    (dict(group_key="TEST-long", index=2, total=3, truncated=False), "complete"),
    (dict(group_key="TEST-long", index=2, total=None, truncated=False), "partial"),
    (dict(group_key="TEST-long", index=0, total=1, truncated=True), "partial"),
])
def test_segment_preserves_order_and_real_unknown_total(segment, capture_state):
    event = source_event(segment=segment, capture_state=capture_state)
    assert validate_payload("source_event", event)["segment"] == segment


@pytest.mark.parametrize("changes,capture_state", [
    (dict(index=3), "complete"), (dict(total=None), "complete"),
    (dict(truncated=True), "complete"), (dict(index=-1), "partial"),
    (dict(total=0), "partial"), (dict(index=True), "partial"),
    (dict(group_key=""), "partial"), (dict(extra="unbounded"), "partial"),
])
def test_invalid_segment_or_false_completeness_is_rejected(changes, capture_state):
    segment = dict(group_key="TEST-long", index=0, total=3, truncated=False) | changes
    with pytest.raises(ContractError):
        validate_payload("source_event", source_event(segment=segment, capture_state=capture_state))


def test_display_keeps_historical_order_and_duplicate_positions():
    snapshot = dict(order="observed", items=[dict(artifact_ref="TEST-A", revision=1), dict(artifact_ref="TEST-A", revision=2), dict(artifact_ref="TEST-A", revision=1)])
    event = source_event(artifact_refs=["TEST-A"], display_snapshot=snapshot)
    assert validate_payload("source_event", event)["display_snapshot"] == snapshot
    assert "display_snapshot" not in validate_payload("source_event", source_event())


@pytest.mark.parametrize("snapshot", [
    dict(order="observed", items=[dict(artifact_ref="TEST-A", revision="latest")]),
    dict(order="observed", items=[dict(artifact_ref="TEST-A", revision=1)] * 33),
    dict(order="guess", items=[]),
    dict(order="observed", items=[dict(artifact_ref="TEST-B", revision=1)]),
])
def test_display_requires_exact_versions_bounded_list_and_reference_membership(snapshot):
    with pytest.raises(ContractError):
        validate_payload("source_event", source_event(artifact_refs=["TEST-A"], display_snapshot=snapshot))


def test_assistant_repetition_cannot_verify_work_progress(tmp_path):
    supported = dict(text="TEST 已完成", evidence_refs=["TEST-event@1"])
    resume = dict(episode_ref=None, goal=supported, decisions=[], verified_progress=[supported],
                  open_items=[], blockers=[], next_step=None, next_step_basis="unknown",
                  artifact_refs=[], source_watermark="TEST-source-watermark", evidence_refs=["TEST-event@1"])
    value = proposal(claim_proposals=[], resume_proposals=[resume])
    source = SourceSnapshot("TEST-event", 1, "TEST 已完成", "TEST-scope", "assistant_visible")
    with pytest.raises(ContractError, match="verified_progress_origin"):
        validate_proposal_references(value, (source,), context(tmp_path))
    resume["verified_progress"] = []
    resume["open_items"] = [supported | {"evidence_refs":["TEST-missing@1"]}]
    with pytest.raises(ContractError, match="SOURCE_MISSING"):
        validate_proposal_references(value, (source,), context(tmp_path))


def test_pending_intention_is_representable_without_completion_or_authorization():
    value = proposal()
    claim = value["claim_proposals"][0]
    claim.update(kind="intention", statement_kind="request", intention=dict(cue="TEST 下次验收", target="提醒检查散热",
                 conditions=["发生交互时"], state="pending", state_evidence_refs=["TEST-event@1"]))
    validated = validate_payload("consolidation_result", value)
    assert validated["claim_proposals"][0]["intention"]["state"] == "pending"
    claim["intention"]["authorized_action"] = "publish"
    with pytest.raises(ContractError):
        validate_payload("consolidation_result", value)
