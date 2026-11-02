"""Host-facing JSON Schema without untyped fields or literal-only constraints.

The SDK's Pydantic model remains the input authority. Nullable primitives use JSON Schema type unions; optionality
stays in required. Open JSON values deliberately retain every JSON type, including nested containers and null.
"""

from __future__ import annotations

from copy import deepcopy

JSON_TYPES = ["null", "boolean", "number", "string", "array", "object"]


def host_schema(schema: dict) -> dict:
    """Return an equivalent declaration without changing defaults, required fields or runtime coercion."""
    result = deepcopy(schema)
    for key in ("properties", "$defs"):
        if key in result:
            result[key] = {name: host_schema(child) for name, child in result[key].items()}
    for key in ("items", "additionalProperties"):
        if isinstance(result.get(key), dict):
            result[key] = host_schema(result[key])
    if "const" in result:
        result["enum"] = [result.pop("const")]
    choices = result.get("anyOf")
    if choices and len(choices) == 2 and {"type": "null"} in choices:
        other = next(choice for choice in choices if choice != {"type": "null"})
        if isinstance(other.get("type"), str):
            result.pop("anyOf")
            result.update(host_schema(other))
            result["type"] = [other["type"], "null"]
    if "type" not in result and not any(key in result for key in ("anyOf", "oneOf", "$ref")):
        result["type"] = list(JSON_TYPES)
    return result
