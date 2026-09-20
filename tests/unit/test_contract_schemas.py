"""Every property in every contract declares its type.

Kimi Code's k3 endpoint validates a tool's parameters as "moonshot flavored
json schema": an enum or const without an explicit type is refused, and since
every tool travels with every request, one untyped property failed all chat
after the upgrade that added the trace tool (#89).  The rule is checked for
every contract, not only the ones that reach a provider today.
"""
import json
from pathlib import Path

import pytest

CONTRACTS = sorted((Path(__file__).resolve().parents[2] / "contracts").glob("*.schema.json"))


def untyped_properties(schema, trail=()):
    """Properties without an explicit type, the way a strict provider reads a schema."""
    found = []
    if not isinstance(schema, dict):
        return found
    for name, spec in (schema.get("properties") or {}).items():
        if isinstance(spec, dict) and "type" not in spec and not any(key in spec for key in ("anyOf", "oneOf", "allOf", "$ref")):
            found.append("/".join((*trail, name)))
        found.extend(untyped_properties(spec, (*trail, name)))
    for key in ("items", "additionalProperties"):
        if isinstance(schema.get(key), dict):
            found.extend(untyped_properties(schema[key], (*trail, key)))
    for key in ("anyOf", "oneOf", "allOf"):
        for index, sub in enumerate(schema.get(key) or ()):
            found.extend(untyped_properties(sub, (*trail, f"{key}[{index}]")))
    for name, spec in (schema.get("$defs") or schema.get("definitions") or {}).items():
        found.extend(untyped_properties(spec, (*trail, "$defs", name)))
    return found


@pytest.mark.parametrize("path", CONTRACTS, ids=[path.name for path in CONTRACTS])
def test_every_property_declares_a_type(path):
    assert untyped_properties(json.loads(path.read_text(encoding="utf-8"))) == []


def test_the_trace_tool_direction_is_a_typed_enum():
    """The property Kimi named."""
    from scope_recall.core.trace import trace_tool_schema

    properties = trace_tool_schema()["parameters"]["properties"]
    assert properties["direction"]["type"] == "string" and properties["protocol_version"]["type"] == "string"
