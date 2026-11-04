"""Measured planning usage and separate in-flight headroom; recommendations never enable spending."""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal
from math import ceil
from typing import Any

from ..runtime.embedding_models import EMBED_RESERVE_FLOOR
from ..runtime.model_budget import default_budget_policy

# One hundred user/assistant exchanges across four idle-ended sessions, with a
# completed task, six adoptions and one explicit query per exchange. Importance
# is returned in extraction and adoption descriptions, with no separate call.
# Tokens count request message contents, using the runtime's token estimator.
MEASURED_100 = {
    "extraction": (109, 534916, 10098),
    "entity_resolution": (9, 19109, 351),
    "embedding": (230, 72917, 0),
    "session_summary": (16, 43119, 1078),
    "task_summary": (2, 3426, 132),
    "locate_adoption": (96, 48943, 1110),
    "describe_adoption": (6, 2508, 216),
    "adoption_support": (6, 4678, 234),
}
# Each recorded suite contains twenty-four cases. These are server usage totals,
# not reservation sizes. Aggregate usage cannot establish a stage-specific tokenizer ratio.
SERVER_CALIBRATION = (
    (32, 79861, 3885),
    (34, 81259, 4274),
    (36, 82106, 4591),
    (35, 84122, 4946),
    (35, 84119, 4281),
    (36, 85017, 5019),
)
SAFETY_FACTOR = Decimal("1.5")


def recommended_workload() -> list[dict[str, Any]]:
    """Expose measured usage plus an aggregate server-usage floor for chat requests, never reservations."""
    calls = sum(row[0] for row in SERVER_CALIBRATION)
    input_floor = ceil(sum(row[1] for row in SERVER_CALIBRATION) / calls)
    output_floor = ceil(sum(row[2] for row in SERVER_CALIBRATION) / calls)
    rows = []
    for task, (count, inputs, outputs) in MEASURED_100.items():
        chat = task != "embedding"
        rows.append(
            dict(
                task=task,
                calls=count,
                input_tokens=inputs,
                output_tokens=outputs,
                calibrated_input_tokens=max(inputs, count * input_floor) if chat else inputs,
                calibrated_output_tokens=max(outputs, count * output_floor) if chat else outputs,
                basis=(
                    "Production worker with synthetic transport, 100 exchanges; chat uses the larger "
                    "of estimated request tokens "
                    "and a six-suite server-usage mean floor."
                ),
            )
        )
    return rows


def recommended_totals(
    *,
    daily_turns: int = 20,
    period: str = "monthly",
    days: int = 31,
    concurrency: int = 2,
    safety_factor: Decimal = SAFETY_FACTOR,
    model: str | None = None,
) -> dict[str, Any]:
    """Scale usage to an explicit horizon, add 50% workload variation, then only concurrent reservations.

    A monthly horizon uses 31 days to cover the longest calendar month. A
    lifetime budget needs an explicit planning horizon; it never renews itself.
    Two in-flight requests cover overlapping worker/query calls by default.
    """
    if any(type(value) is not int or value < 1 for value in (daily_turns, days, concurrency)) or period not in {
        "monthly",
        "lifetime",
    }:
        raise ValueError("positive workload and a supported budget period required")
    factor = Decimal(str(safety_factor))
    if not factor.is_finite() or factor < 1:
        raise ValueError("workload safety factor must be at least one")
    rows, policy = recommended_workload(), default_budget_policy()
    turns = daily_turns * days
    scale = Decimal(turns) / 100 * factor

    def scaled(field):
        return int((sum(row[field] or 0 for row in rows) * scale).to_integral_value(rounding=ROUND_CEILING))

    inputs = scaled("calibrated_input_tokens")
    outputs = scaled("calibrated_output_tokens")
    inflight_input = concurrency * max(policy.default_reserve_input, EMBED_RESERVE_FLOOR)
    output_reserve = (
        policy.model_reserve_output.get(model, policy.default_reserve_output)
        if model
        else policy.default_reserve_output
    )
    inflight_output = concurrency * output_reserve
    return dict(
        period=period,
        days=days,
        daily_turns=daily_turns,
        turns=turns,
        safety_factor=float(factor),
        concurrency=concurrency,
        usage_input_cap=inputs,
        usage_output_cap=outputs,
        inflight_input=inflight_input,
        inflight_output=inflight_output,
        total_call_cap=scaled("calls") + concurrency,
        total_input_cap=inputs + inflight_input,
        total_output_cap=outputs + inflight_output,
        cap_micro_usd=0,
    )
