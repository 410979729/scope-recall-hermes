"""Static compatibility checks for llama.cpp-style tool schema conversion.

llama.cpp accepts valid JSON Schema but has had conversion/parser limits for a
few legal shapes (see ggml-org/llama.cpp issues #25746 and #25923). These
checks cover the known shapes that can make an entire tool request fail before
model inference. Runtime validation remains the source of truth for security
and storage bounds.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

_LONG_NESTED_STRING_THRESHOLD = 2_000
_PCRE_SHORTHAND = re.compile(r"\\[dDsSwWbB]")
_SCHEMA_SHAPE_KEYS = frozenset(
    {
        "type",
        "enum",
        "const",
        "$ref",
        "anyOf",
        "oneOf",
        "allOf",
        "not",
    }
)


def find_llama_cpp_schema_issues(
    tool_schemas: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Return known llama.cpp conversion hazards in LLM-facing tool schemas.

    The checker intentionally targets reproducible upstream failure classes,
    not every unsupported JSON Schema keyword. Paths are stable enough for CI
    diagnostics without exposing schema values or other runtime data.
    """

    issues: list[dict[str, str]] = []
    for tool_schema in tool_schemas:
        tool_name = str(tool_schema.get("name") or "<unnamed>")
        parameters = tool_schema.get("parameters")
        if not isinstance(parameters, Mapping):
            continue
        _scan_node(
            parameters,
            tool_name=tool_name,
            path=("parameters",),
            object_depth=0,
            property_node=False,
            issues=issues,
        )
    return issues


def _scan_node(
    node: Any,
    *,
    tool_name: str,
    path: tuple[str, ...],
    object_depth: int,
    property_node: bool,
    issues: list[dict[str, str]],
) -> None:
    if not isinstance(node, Mapping):
        if isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
            for index, item in enumerate(node):
                _scan_node(
                    item,
                    tool_name=tool_name,
                    path=(*path, str(index)),
                    object_depth=object_depth,
                    property_node=False,
                    issues=issues,
                )
        return

    node_type = node.get("type")
    if (
        node_type == "string"
        and object_depth >= 2
        and isinstance(node.get("maxLength"), int)
        and int(node["maxLength"]) >= _LONG_NESTED_STRING_THRESHOLD
    ):
        _add_issue(
            issues,
            code="nested_string_max_length",
            tool_name=tool_name,
            path=path,
            detail=(
                "nested string maxLength >= 2000 can generate an unparseable "
                "repetition grammar"
            ),
        )

    pattern = node.get("pattern")
    if (
        node_type == "string"
        and isinstance(pattern, str)
        and _PCRE_SHORTHAND.search(pattern)
    ):
        _add_issue(
            issues,
            code="pcre_shorthand_pattern",
            tool_name=tool_name,
            path=path,
            detail="PCRE shorthand escapes are not valid GBNF character classes",
        )

    if property_node and not any(key in node for key in _SCHEMA_SHAPE_KEYS):
        _add_issue(
            issues,
            code="typeless_property",
            tool_name=tool_name,
            path=path,
            detail="property schema has no type, union, enum, const, or reference",
        )

    if node_type == "object":
        properties = node.get("properties")
        has_named_properties = isinstance(properties, Mapping) and bool(properties)
        additional_properties = node.get("additionalProperties")
        has_additional_rule = additional_properties is True or isinstance(
            additional_properties, Mapping
        )
        if not has_named_properties and not has_additional_rule:
            _add_issue(
                issues,
                code="empty_object_without_additional_properties",
                tool_name=tool_name,
                path=path,
                detail=(
                    "empty object schemas emit invalid adjacent-space GBNF unless "
                    "free-form additional properties are explicit"
                ),
            )

    next_depth = object_depth + (1 if node_type == "object" else 0)
    properties = node.get("properties")
    if isinstance(properties, Mapping):
        for key, value in properties.items():
            _scan_node(
                value,
                tool_name=tool_name,
                path=(*path, "properties", str(key)),
                object_depth=next_depth,
                property_node=True,
                issues=issues,
            )

    for key in ("items", "additionalProperties", "anyOf", "oneOf", "allOf", "not"):
        if key not in node:
            continue
        _scan_node(
            node[key],
            tool_name=tool_name,
            path=(*path, key),
            object_depth=next_depth,
            property_node=False,
            issues=issues,
        )


def _add_issue(
    issues: list[dict[str, str]],
    *,
    code: str,
    tool_name: str,
    path: tuple[str, ...],
    detail: str,
) -> None:
    issues.append(
        {
            "code": code,
            "tool": tool_name,
            "path": ".".join(path),
            "detail": detail,
        }
    )
