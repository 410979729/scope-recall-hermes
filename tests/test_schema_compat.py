"""Compatibility contracts for llama.cpp and LM Studio tool schemas."""

from __future__ import annotations

from typing import Any

import scope_recall.schemas as schemas
from scope_recall.schema_compat import find_llama_cpp_schema_issues


def _named_tool_schemas() -> list[dict[str, Any]]:
    """Return every canonical schema that can be exposed as a tool."""

    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in vars(schemas).values():
        if not isinstance(value, dict):
            continue
        name = value.get("name")
        parameters = value.get("parameters")
        if not isinstance(name, str) or not isinstance(parameters, dict):
            continue
        if name in seen:
            continue
        seen.add(name)
        output.append(value)
    return output


def test_llama_cpp_checker_flags_known_schema_conversion_hazards() -> None:
    dangerous = [
        {
            "name": "nested_long_string",
            "parameters": {
                "type": "object",
                "properties": {
                    "claim": {
                        "type": "object",
                        "properties": {
                            "value": {"type": "string", "maxLength": 2000},
                        },
                    }
                },
            },
        },
        {
            "name": "pcre_pattern",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "pattern": r"^\d+$"},
                },
            },
        },
        {
            "name": "typeless_property",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {"description": "Arbitrary JSON value."},
                },
            },
        },
        {
            "name": "empty_object",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    ]

    issues = find_llama_cpp_schema_issues(dangerous)

    assert {item["code"] for item in issues} == {
        "empty_object_without_additional_properties",
        "nested_string_max_length",
        "pcre_shorthand_pattern",
        "typeless_property",
    }


def test_llama_cpp_checker_allows_explicit_free_form_objects() -> None:
    safe = [
        {
            "name": "free_form_object",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            },
        }
    ]

    assert find_llama_cpp_schema_issues(safe) == []


def test_all_scope_recall_tool_schemas_avoid_known_llama_cpp_hazards() -> None:
    assert find_llama_cpp_schema_issues(_named_tool_schemas()) == []


def test_llama_cpp_compatibility_does_not_remove_fact_capabilities() -> None:
    store_properties = schemas.SCOPE_RECALL_STORE_SCHEMA["parameters"]["properties"]
    memory_properties = schemas.SCOPE_RECALL_MEMORY_SCHEMA["parameters"]["properties"]

    assert {"freshness", "claim", "evolution"}.issubset(store_properties)
    assert {"claim", "evolution"}.issubset(memory_properties)
