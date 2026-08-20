"""scope-recall current-turn memory provider plugin.

Top-level import intentionally avoids Hermes runtime modules so ``import
scope_recall`` works in clean wheel/venv checks. Hermes-specific imports are
lazy-loaded only when Hermes calls ``register()``. Keep the literal
``register_memory_provider`` string in this docstring for Hermes' cheap
user-plugin discovery heuristic; the actual call lives in provider.py.
"""

import importlib
import sys
from threading import RLock
from typing import Any

_REGISTER_LOCK = RLock()


def register(ctx: Any) -> Any:
    """Register the provider, repairing only failed eager-loader residue.

    Hermes eagerly executes user-plugin submodules before this package entry
    point. If a dependency is visited later in that pass, Python can retain a
    half-initialized module in ``sys.modules``. Purging the plugin's submodules
    is safe only at that failed first-load boundary; healthy modules are reused
    so concurrent agent instances keep one coherent module graph.
    """

    with _REGISTER_LOCK:
        provider_name = f"{__name__}.provider"
        provider_module = sys.modules.get(provider_name)
        if provider_module is not None and not callable(
            getattr(provider_module, "register", None)
        ):
            prefix = f"{__name__}."
            for module_name in tuple(sys.modules):
                if module_name.startswith(prefix):
                    sys.modules.pop(module_name, None)

        provider_module = importlib.import_module(".provider", __name__)
        _register = provider_module.register

    return _register(ctx)


__all__ = ["register"]
