"""scope-recall current-turn memory provider plugin.

Top-level import intentionally avoids Hermes runtime modules so ``import
scope_recall`` works in clean wheel/venv checks. Hermes-specific imports are
lazy-loaded only when Hermes calls ``register()``. Keep the literal
``register_memory_provider`` string in this docstring for Hermes' cheap
user-plugin discovery heuristic; the actual call lives in
``scope_recall.adapters.hermes.register``.
"""

from typing import Any


def register(ctx: Any) -> Any:
    """Register the bounded Hermes adapter without importing legacy provider code."""

    from scope_recall.adapters.hermes.register import register as register_adapter

    return register_adapter(ctx)


__all__ = ["register"]
