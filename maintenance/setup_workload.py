"""Shared planning estimates for model budgets; estimates never enable spending."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import ceil

from ..runtime.embedding_models import EMBED_RESERVE_FLOOR
from ..runtime.model_budget import default_budget_policy


@dataclass(frozen=True)
class Workload:
    """A declared scenario per hundred turns, with reserved rather than billed tokens."""

    task: str
    calls: int | None
    input_tokens: int | None
    output_tokens: int | None
    basis: str


def recommended_workload() -> list[dict]:
    """Return a single catalog used by setup and doctor; unmeasured tasks have no allowance."""
    policy = default_budget_policy()
    # The extraction report estimates 45 calls for twelve examples. Scale that planning
    # estimate and use transport reservations because the report supplies no token meter.
    extraction_calls = ceil(100 * 45 / 12)
    rows = [
        Workload(
            "extraction",
            extraction_calls,
            extraction_calls * policy.default_reserve_input,
            extraction_calls * policy.default_reserve_output,
            "Planning estimate: 45 calls / 12 examples; tokens are request reservations, not billed usage.",
        ),
        Workload(
            "entity_resolution",
            10,
            10 * policy.default_reserve_input,
            10 * policy.default_reserve_output,
            "Planning scenario: one fact per turn and the measured 5,000 judgments / 50,000 facts name reuse ratio.",
        ),
        Workload(
            "embedding",
            300,
            300 * EMBED_RESERVE_FLOOR,
            0,
            (
                "Planning scenario: two source messages and one query per turn, "
                "without batching savings; reserved input tokens."
            ),
        ),
        Workload("summary_importance", None, None, None, "No installed task estimate."),
        Workload("adoption", None, None, None, "No installed task estimate."),
    ]
    return [asdict(row) for row in rows]


def recommended_totals() -> dict[str, int]:
    """Sum measured or declared scenarios; unmeasured tasks never receive an invented allowance."""
    rows = recommended_workload()
    return {
        "total_call_cap": sum(row["calls"] or 0 for row in rows),
        "total_input_cap": sum(row["input_tokens"] or 0 for row in rows),
        "total_output_cap": sum(row["output_tokens"] or 0 for row in rows),
    }
