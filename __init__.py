"""scope-recall current-turn memory provider plugin.

Top-level import intentionally avoids Hermes runtime modules so ``import
scope_recall`` works in clean wheel/venv checks. Hermes-specific imports are
lazy-loaded only when Hermes calls ``register()``. Keep the literal
``register_memory_provider`` string in this docstring for Hermes' cheap
user-plugin discovery heuristic; the actual call lives in provider.py.
"""

from typing import Any


def register(ctx: Any) -> Any:
    from .provider import register as _register

    return _register(ctx)


__all__ = ["register"]
