"""A rejected payload says which field to fix.

Beta asked for a day's memory with ``mode=as_of`` and got ``INPUT_INVALID`` / ``format``:
the accepted spelling is UTC only, the name ``as_of`` was already in the error's own path,
and the keyword was reported instead.  The same field is what a failed derivation hands back
to the model for its one repair attempt, where ``required`` told it nothing either.
"""
from __future__ import annotations

import pytest

from scope_recall.contracts import ContractError, validate_payload
from tests.contract.test_rc33_recall_accuracy import _packet, _say  # noqa: F401  (helpers)
from tests.contract.test_v11_claims import app, capture  # noqa: F401  (fixture)
from tests.v11_support import recall_request


def _recall(**changes):
    return dict(recall_request(**changes), request_id="TEST-field")


def test_a_rejected_instant_names_the_field_that_holds_it():
    for spelling in ("2026-09-17", "2026-09-17 12:00:00", "2026-09-17T20:00:00+08:00", "yesterday"):
        with pytest.raises(ContractError) as rejected:
            validate_payload("recall_request", _recall(mode="as_of", as_of=spelling))
        assert rejected.value.code == "INPUT_INVALID"
        assert rejected.value.field == "as_of", spelling


def test_the_spellings_a_query_may_use_are_accepted():
    for spelling in ("2026-09-17T12:00:00Z", "2026-09-17T12:00:00+00:00", "2026-09-17T12:00:00.123456Z"):
        assert validate_payload("recall_request", _recall(mode="as_of", as_of=spelling))["as_of"] == spelling


def test_a_model_writes_the_instant_back_in_the_zone_it_was_shown(tmp_path):
    """Memory times reach a model in its host's zone; one it copies into a request is the same instant."""
    from scope_recall.contracts import validate_model_request
    from tests.v11_support import context

    trusted = context(tmp_path / "TEST-db")
    request = validate_model_request("recall_request", _recall(mode="as_of", as_of="2026-09-17T08:00:00.5-04:00"), trusted)
    assert request["as_of"] == "2026-09-17T12:00:00.5Z"
    revise = validate_model_request("revise_request", {
        "protocol_version": "1.1", "target_ref": "TEST-claim", "expected_revision": 1, "new_value": "TEST",
        "conditions": [], "source_evidence_refs": ["TEST-source@1"], "valid_from": "2026-09-17T20:00:00+08:00",
    }, trusted)
    assert revise["valid_from"] == "2026-09-17T12:00:00Z"
    for spelling in ("2026-09-17", "2026-09-17T08:00:00", "2026-09-17T08:00:00-00:00", "2026-09-17T08:00:00+24:00"):
        with pytest.raises(ContractError) as rejected:
            validate_model_request("recall_request", _recall(mode="as_of", as_of=spelling), trusted)
        assert (rejected.value.code, rejected.value.field) == ("INPUT_INVALID", "as_of"), spelling


def test_a_missing_companion_field_is_named_too():
    with pytest.raises(ContractError) as rejected:
        validate_payload("recall_request", _recall(mode="as_of"))
    assert (rejected.value.code, rejected.value.field) == ("INPUT_INVALID", "as_of")


def test_a_rejection_about_the_whole_payload_still_names_its_rule():
    with pytest.raises(ContractError) as rejected:
        validate_payload("recall_request", "not a json object")
    assert rejected.value.code == "INPUT_INVALID"


def test_the_recall_tool_tells_the_model_how_to_write_an_instant():
    from scope_recall.adapters.hermes.tool_surface import _TOOL_SCHEMAS

    recall = next(schema for schema in _TOOL_SCHEMAS if schema["name"] == "recall")
    as_of = recall["parameters"]["properties"]["as_of"]
    assert "2026-09-17T12:00:00Z" in as_of["description"]
    assert "description" in recall["parameters"]["properties"]["mode"]
