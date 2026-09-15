"""Runtime auxiliary-model boundary for Scope Recall v1.1."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .auxiliary import (
        AuxiliaryRuntime,
        AuxiliaryRuntimeConfig,
        DEFAULT_LEDGER_NAME,
        auxiliary_runtime_status,
        build_auxiliary_runtime,
    )
    from .model_budget import (
        AuxiliaryBudgetLedger,
        BudgetPolicy,
        ModelPricing,
        default_budget_policy,
        initialize_auxiliary_budget_ledger,
        read_auxiliary_budget_status,
    )

__all__ = [
    "AuxiliaryBudgetLedger",
    "AuxiliaryRuntime",
    "AuxiliaryRuntimeConfig",
    "BudgetPolicy",
    "DEFAULT_LEDGER_NAME",
    "ModelPricing",
    "auxiliary_runtime_status",
    "build_auxiliary_runtime",
    "default_budget_policy",
    "initialize_auxiliary_budget_ledger",
    "read_auxiliary_budget_status",
]

_LAZY_EXPORTS = {
    "AuxiliaryRuntime": (".auxiliary", "AuxiliaryRuntime"),
    "AuxiliaryRuntimeConfig": (".auxiliary", "AuxiliaryRuntimeConfig"),
    "DEFAULT_LEDGER_NAME": (".auxiliary", "DEFAULT_LEDGER_NAME"),
    "auxiliary_runtime_status": (".auxiliary", "auxiliary_runtime_status"),
    "build_auxiliary_runtime": (".auxiliary", "build_auxiliary_runtime"),
    "AuxiliaryBudgetLedger": (".model_budget", "AuxiliaryBudgetLedger"),
    "BudgetPolicy": (".model_budget", "BudgetPolicy"),
    "ModelPricing": (".model_budget", "ModelPricing"),
    "default_budget_policy": (".model_budget", "default_budget_policy"),
    "initialize_auxiliary_budget_ledger": (".model_budget", "initialize_auxiliary_budget_ledger"),
    "read_auxiliary_budget_status": (".model_budget", "read_auxiliary_budget_status"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    import importlib

    module = importlib.import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
