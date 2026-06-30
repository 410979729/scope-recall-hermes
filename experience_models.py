"""Dataclass models for Experience playbooks, feedback, promotion plans, and replay cases.

These models define the stable data vocabulary shared by storage, tooling, benchmarks, and operator reports."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

CAPABILITY_CLASSES = frozenset(
    {
        "read_only",
        "local_write",
        "service_control",
        "network_or_remote",
        "cross_instance",
        "credential_adjacent",
        "destructive_or_irreversible",
    }
)

RISKY_CAPABILITY_CLASSES = frozenset(
    {
        "service_control",
        "network_or_remote",
        "cross_instance",
        "credential_adjacent",
        "destructive_or_irreversible",
    }
)

PLAYBOOK_STATUSES = frozenset({"candidate", "reviewed", "promoted", "needs_review", "superseded", "quarantined"})
PLAYBOOK_SCHEMA_VERSION = "procedural_playbook.v1"


class ExperienceValidationError(ValueError):
    """Raised when an Experience Kernel payload is unsafe or malformed."""


@dataclass(frozen=True)
class PlaybookStep:
    number: int
    capability_class: str
    action: str
    evidence_required: str
    why: str = ""
    previous_mistakes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ProceduralPlaybook:
    schema_version: str
    task_class: str
    title: str
    trigger: str
    goal: str
    preconditions: tuple[Mapping[str, Any], ...]
    steps: tuple[PlaybookStep, ...]
    pitfalls: tuple[Mapping[str, Any], ...]
    verification: tuple[str, ...]
    cleanup: tuple[str, ...]
    reuse_policy: Mapping[str, Any]
    status: str = "candidate"
    confidence: float = 0.5
    requires_operator_review: bool = True


def _require_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ExperienceValidationError(f"{key} must be a non-empty string")
    return value.strip()


def _require_list(payload: Mapping[str, Any], key: str) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ExperienceValidationError(f"{key} must be a list")
    return value


def _text_tuple(values: Sequence[Any], *, field_name: str) -> tuple[str, ...]:
    normalized: list[str] = []
    for idx, value in enumerate(values, start=1):
        if not isinstance(value, str) or not value.strip():
            raise ExperienceValidationError(f"{field_name}[{idx}] must be a non-empty string")
        normalized.append(value.strip())
    return tuple(normalized)


def _mapping_tuple(values: Sequence[Any], *, field_name: str) -> tuple[Mapping[str, Any], ...]:
    normalized: list[Mapping[str, Any]] = []
    for idx, value in enumerate(values, start=1):
        if not isinstance(value, Mapping):
            raise ExperienceValidationError(f"{field_name}[{idx}] must be an object")
        normalized.append(dict(value))
    return tuple(normalized)


def _validate_steps(values: Sequence[Any]) -> tuple[PlaybookStep, ...]:
    steps: list[PlaybookStep] = []
    expected_number = 1
    for idx, value in enumerate(values, start=1):
        if not isinstance(value, Mapping):
            raise ExperienceValidationError(f"steps[{idx}] must be an object")
        raw_number = value.get("number")
        if not isinstance(raw_number, int) or raw_number != expected_number:
            raise ExperienceValidationError(f"steps[{idx}].number must be {expected_number}")
        capability_class = value.get("capability_class")
        if capability_class not in CAPABILITY_CLASSES:
            raise ExperienceValidationError(
                f"steps[{idx}].capability_class must be one of {sorted(CAPABILITY_CLASSES)}"
            )
        action = value.get("action")
        if not isinstance(action, str) or not action.strip():
            raise ExperienceValidationError(f"steps[{idx}].action must be a non-empty string")
        evidence_required = value.get("evidence_required")
        if not isinstance(evidence_required, str) or not evidence_required.strip():
            raise ExperienceValidationError(f"steps[{idx}].evidence_required must be a non-empty string")
        previous_mistakes = value.get("previous_mistakes", [])
        if not isinstance(previous_mistakes, list):
            raise ExperienceValidationError(f"steps[{idx}].previous_mistakes must be a list")
        steps.append(
            PlaybookStep(
                number=raw_number,
                capability_class=str(capability_class),
                action=action.strip(),
                evidence_required=evidence_required.strip(),
                why=str(value.get("why") or "").strip(),
                previous_mistakes=_text_tuple(previous_mistakes, field_name=f"steps[{idx}].previous_mistakes"),
            )
        )
        expected_number += 1
    if not steps:
        raise ExperienceValidationError("steps must contain at least one step")
    return tuple(steps)


def validate_procedural_playbook(payload: Mapping[str, Any]) -> ProceduralPlaybook:
    """Validate and normalize a procedural playbook candidate.

    This is intentionally deterministic: it checks shape, ordered steps,
    capability boundaries, and required evidence fields. It does not decide
    whether the procedure is semantically correct; MVP promotion still requires
    operator review.
    """

    schema_version = payload.get("schema_version", PLAYBOOK_SCHEMA_VERSION)
    if schema_version != PLAYBOOK_SCHEMA_VERSION:
        raise ExperienceValidationError(f"schema_version must be {PLAYBOOK_SCHEMA_VERSION}")

    task_class = _require_text(payload, "task_class")
    title = _require_text(payload, "title")
    trigger = _require_text(payload, "trigger")
    goal = _require_text(payload, "goal")
    preconditions = _mapping_tuple(_require_list(payload, "preconditions"), field_name="preconditions")
    if not preconditions:
        raise ExperienceValidationError("preconditions must contain at least one item")
    steps = _validate_steps(_require_list(payload, "steps"))
    pitfalls = _mapping_tuple(payload.get("pitfalls", []), field_name="pitfalls")
    verification = _text_tuple(_require_list(payload, "verification"), field_name="verification")
    if not verification:
        raise ExperienceValidationError("verification must contain at least one item")
    cleanup = _text_tuple(payload.get("cleanup", []), field_name="cleanup")
    reuse_policy = payload.get("reuse_policy", {})
    if not isinstance(reuse_policy, Mapping):
        raise ExperienceValidationError("reuse_policy must be an object")
    status = str(payload.get("status") or "candidate").strip()
    if status not in PLAYBOOK_STATUSES:
        raise ExperienceValidationError(f"status must be one of {sorted(PLAYBOOK_STATUSES)}")
    try:
        confidence = float(payload.get("confidence", 0.5))
    except (TypeError, ValueError) as exc:
        raise ExperienceValidationError("confidence must be numeric") from exc
    if not math.isfinite(confidence) or confidence < 0.0 or confidence > 1.0:
        raise ExperienceValidationError("confidence must be between 0.0 and 1.0")

    requires_operator_review = status != "promoted" or any(
        step.capability_class in RISKY_CAPABILITY_CLASSES for step in steps
    )
    return ProceduralPlaybook(
        schema_version=schema_version,
        task_class=task_class,
        title=title,
        trigger=trigger,
        goal=goal,
        preconditions=preconditions,
        steps=steps,
        pitfalls=pitfalls,
        verification=verification,
        cleanup=cleanup,
        reuse_policy=dict(reuse_policy),
        status=status,
        confidence=confidence,
        requires_operator_review=requires_operator_review,
    )
