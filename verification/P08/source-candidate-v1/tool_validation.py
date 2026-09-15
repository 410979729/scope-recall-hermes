"""Executable JSON-Schema validation for Scope Recall tool arguments.

Hermes uses the same schemas to describe tools to models, but direct provider
calls can bypass platform-side validation. This module makes those declarations
an in-process boundary and returns redacted diagnostics that never echo values.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from . import schemas as schema_definitions


@dataclass(frozen=True)
class ToolArgumentIssue:
    """One stable, redacted validation failure for a public tool call."""

    field: str
    constraint: str
    bound: int | float | None = None
    minimum: int | float | None = None
    maximum: int | float | None = None
    expected_type: str = ""

    def public_message(self) -> str:
        """Render a stable message without including the rejected value."""

        label = "query instant" if self.field in {"at", "known_at"} else self.field
        if self.constraint == "type":
            expected = {
                "boolean": "a boolean",
                "integer": "an integer",
                "number": "numeric",
                "string": "a string",
                "array": "an array",
                "object": "an object",
            }.get(self.expected_type, "the declared type")
            return f"{label} must be {expected}"
        if self.constraint == "required":
            return f"{label} is required"
        if self.constraint == "enum":
            return f"{label} must be one of the supported values"
        if self.constraint == "maxLength" and self.bound is not None:
            return f"{label} exceeds {self.bound} characters"
        if self.constraint == "maxItems" and self.bound is not None:
            return f"{label} exceeds {self.bound} entries"
        if (
            self.constraint in {"minimum", "maximum"}
            and self.minimum is not None
            and self.maximum is not None
        ):
            return f"{label} must be between {self.minimum} and {self.maximum}"
        if self.constraint == "minimum" and self.bound is not None:
            return f"{label} must be at least {self.bound}"
        if self.constraint == "maximum" and self.bound is not None:
            return f"{label} exceeds maximum {self.bound}"
        return f"invalid arguments for {label}"


def _parameter_schemas() -> dict[str, dict[str, Any]]:
    registry: dict[str, dict[str, Any]] = {}
    for symbol, value in vars(schema_definitions).items():
        if not symbol.startswith("SCOPE_RECALL_") or not symbol.endswith("_SCHEMA"):
            continue
        if not isinstance(value, dict):
            continue
        name = value.get("name")
        parameters = value.get("parameters")
        if isinstance(name, str) and isinstance(parameters, dict):
            registry[name] = parameters
    return registry


_PARAMETER_SCHEMAS = _parameter_schemas()
_VALIDATORS = {
    name: Draft202012Validator(parameter_schema)
    for name, parameter_schema in _PARAMETER_SCHEMAS.items()
}


def _error_sort_key(
    error: ValidationError,
) -> tuple[int, tuple[str, ...], str]:
    path = tuple(str(part) for part in error.absolute_path)
    return len(path), path, str(error.validator or "")


def _error_field(error: ValidationError) -> str:
    if error.validator == "required" and isinstance(error.instance, dict):
        required = error.validator_value
        if isinstance(required, list):
            missing = [str(key) for key in required if key not in error.instance]
            if missing:
                return sorted(missing)[0]
    path = [str(part) for part in error.absolute_path]
    return ".".join(path) if path else "$"


def validate_tool_arguments(
    tool_name: str, args: Any
) -> ToolArgumentIssue | None:
    """Validate one direct tool invocation against its declared parameters.

    Unknown schema names are left to the dispatcher so feature-gated or unknown
    tools retain their existing errors. Only field paths and validator names are
    returned; user-provided values and schema error messages are deliberately
    omitted from public receipts.
    """

    validator = _VALIDATORS.get(str(tool_name or ""))
    if validator is None:
        return None
    if not isinstance(args, dict):
        return ToolArgumentIssue(field="$", constraint="type", expected_type="object")
    errors = sorted(validator.iter_errors(args), key=_error_sort_key)
    if not errors:
        return None
    error = errors[0]
    validator_value = error.validator_value
    bound = (
        validator_value
        if error.validator in {"minimum", "maximum", "maxLength", "maxItems"}
        and isinstance(validator_value, (int, float))
        and not isinstance(validator_value, bool)
        else None
    )
    expected_type = (
        str(validator_value)
        if error.validator == "type" and isinstance(validator_value, str)
        else ""
    )
    schema = error.schema if isinstance(error.schema, dict) else {}
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    numeric_minimum = (
        minimum
        if isinstance(minimum, (int, float)) and not isinstance(minimum, bool)
        else None
    )
    numeric_maximum = (
        maximum
        if isinstance(maximum, (int, float)) and not isinstance(maximum, bool)
        else None
    )
    return ToolArgumentIssue(
        field=_error_field(error),
        constraint=str(error.validator or "schema"),
        bound=bound,
        minimum=numeric_minimum,
        maximum=numeric_maximum,
        expected_type=expected_type,
    )
