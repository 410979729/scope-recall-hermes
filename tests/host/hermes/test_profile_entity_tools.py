"""Hermes dispatch for the shared profile/entity read views."""
from __future__ import annotations

import inspect
import json
from pathlib import Path

from scope_recall.adapters.hermes.provider import _TOOL_NAMES


def test_hermes_profile_entity_dispatch_and_invalid_args(adapter):
    provider, _clock = adapter
    from scope_recall.adapters.hermes import provider as provider_mod

    imported = Path(provider_mod.__file__).resolve()
    expected = Path(__file__).resolve().parents[3] / "adapters" / "hermes" / "provider.py"
    assert imported == expected
    fallback = inspect.getsource(provider_mod._memory_provider_base)
    assert "except ImportError:" in fallback
    assert "PermissionError" not in fallback

    schemas = provider.get_tool_schemas()
    names = [schema["name"] for schema in schemas]
    assert names == ["recall", "inspect", "profile", "entity", "revise", "forget", "status", "trace"]
    assert _TOOL_NAMES == frozenset(names)
    by_name = {schema["name"]: schema for schema in schemas}
    assert "read-only" in by_name["profile"]["description"].lower() or "Read-only" in by_name["profile"]["description"]
    assert "read-only" in by_name["entity"]["description"].lower() or "Read-only" in by_name["entity"]["description"]
    assert by_name["profile"]["parameters"]["additionalProperties"] is False
    assert by_name["entity"]["parameters"]["additionalProperties"] is False
    assert by_name["trace"]["parameters"]["additionalProperties"] is False

    core = provider._core
    identity = provider._identity
    context = identity.trusted_context()
    provider.observe_pre_llm(session_id="TEST-session-1", turn_id="human-1", user_message="我喜欢茶。")
    source_ref, source_revision = provider._current_source_refs[-1].rsplit("@", 1)
    claim = core.accept_claim_proposals(
        context,
        {
            "protocol_version": "1.1",
            "source_refs": [f"{source_ref}@{source_revision}"],
            "claim_proposals": [{
                "kind": "preference", "subject": "我", "predicate": "喜欢",
                "value_text": "茶", "conditions": [], "statement_kind": "assertion",
                "valid_from": None, "valid_to": None,
                "evidence_spans": [{
                    "source_ref": source_ref, "source_revision": int(source_revision),
                    "quote": "我喜欢茶。",
                }],
            }],
            "resume_proposals": [], "reference_proposals": [],
        },
        scope_id=identity.local_scope_id,
        remaining_seconds=5,
    ).items[0]

    profile = json.loads(provider.handle_tool_call("profile", {
        "protocol_version": "1.1", "subject": "我", "request_id": "hermes-profile",
    }))
    assert "result" in profile, profile
    assert profile["result"]["sections"]["preferences"][0]["value_text"] == "茶"
    assert profile["result"]["sections"]["preferences"][0]["ref"] == claim.ref
    entity = json.loads(provider.handle_tool_call("entity", {
        "protocol_version": "1.1", "subject": "我", "action": "probe", "request_id": "hermes-entity",
    }))
    assert entity["result"]["action"] == "probe"
    related = json.loads(provider.handle_tool_call("entity", {
        "protocol_version": "1.1", "subject": "我", "action": "related",
        "direction": "outgoing", "request_id": "hermes-related",
    }))
    assert related["result"]["statements"][0]["value_text"] == "茶"
    trace = json.loads(provider.handle_tool_call("trace", {
        "protocol_version": "1.1", "subject": "我", "request_id": "hermes-trace",
    }))
    assert trace["result"]["facts_written"] == 0
    assert trace["result"]["paths"] == []  # Preferences are not relationship edges.
    forged_trace = json.loads(provider.handle_tool_call("trace", {
        "protocol_version": "1.1", "subject": "我", "scope_id": "private",
    }))
    assert forged_trace["error"]["code"] == "INPUT_INVALID"

    forged = json.loads(provider.handle_tool_call("profile", {
        "protocol_version": "1.1", "subject": "我", "scope_id": "private",
    }))
    assert forged["error"] == {"code": "INPUT_INVALID", "field": "unknown_field"}
    bool_int = json.loads(provider.handle_tool_call("entity", {
        "protocol_version": "1.1", "subject": "我", "action": "related", "max_items": True,
    }))
    assert bool_int["error"]["code"] == "INPUT_INVALID"
    bad_enum = json.loads(provider.handle_tool_call("entity", {
        "protocol_version": "1.1", "subject": "我", "action": "walk",
    }))
    assert bad_enum["error"]["code"] == "INPUT_INVALID"
    path_arg = json.loads(provider.handle_tool_call("profile", {
        "protocol_version": "1.1", "subject": "我", "data_directory": "C:/secret",
    }))
    assert path_arg["error"] == {"code": "INPUT_INVALID", "field": "unknown_field"}
