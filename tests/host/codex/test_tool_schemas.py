"""The schemas actually advertised by the SDK retain the internal argument contract."""

from __future__ import annotations

import asyncio

import jsonschema
import pytest
from scope_recall.adapters.clients.mcp_server import build_server

VALUES = [None, "", "text", 0, 1, True, [], ["ref@1"], {}, {"nested": [None, {"number": 1}]}]


def _nodes(schema):
    yield schema
    for key in ("properties", "$defs"):
        for child in schema.get(key, {}).values():
            yield from _nodes(child)
    for key in ("items", "additionalProperties"):
        if isinstance(schema.get(key), dict):
            yield from _nodes(schema[key])
    for key in ("anyOf", "oneOf", "allOf"):
        for child in schema.get(key, []):
            yield from _nodes(child)


def test_advertised_tools_have_typed_parameters_and_preserve_nullable_inputs(installed):
    config, core, _clock, project = installed
    adapter = build_server(config, workspace=project, core=core)
    tools = asyncio.run(adapter.server.list_tools())
    assert len(tools) == 13
    for listed in tools:
        external = listed.input_schema
        internal = adapter.server._tool_manager.get_tool(listed.name).fn_metadata.arg_model.model_json_schema()
        assert external["required"] == internal["required"] if "required" in internal else "required" not in external
        for node in _nodes(external):
            assert "const" not in node
            assert "type" in node, (listed.name, node)
            assert "anyOf" not in node
        for name, prop in internal["properties"].items():
            advertised = external["properties"][name]
            for value in VALUES:
                assert jsonschema.Draft202012Validator(prop).is_valid(value) == jsonschema.Draft202012Validator(
                    advertised
                ).is_valid(value), (listed.name, name, value)


@pytest.mark.parametrize("name", ["as_of", "focus_refs", "request_id"])
@pytest.mark.parametrize("value", [None, "value", []])
def test_nullable_parameter_contract_is_not_rewritten(installed, name, value):
    config, core, _clock, project = installed
    adapter = build_server(config, workspace=project, core=core)
    model = adapter.server._tool_manager.get_tool("recall").fn_metadata.arg_model
    base = dict(protocol_version="1.1", query="synthetic", mode="current", max_items=6)
    assert model.model_validate(base).model_dump()[name] is None
    # Both omission and explicit null reach the existing optional-field handling unchanged.
    if value is None:
        assert model.model_validate(dict(base, **{name: value})).model_dump()[name] is None
