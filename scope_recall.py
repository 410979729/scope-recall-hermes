from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).with_name("__init__.py")
_SPEC = importlib.util.spec_from_file_location(
    "scope_recall_plugin_runtime",
    _MODULE_PATH,
    submodule_search_locations=[str(_MODULE_PATH.parent)],
)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Unable to load scope-recall runtime from {_MODULE_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault(_SPEC.name, _MODULE)
_SPEC.loader.exec_module(_MODULE)

for _name in dir(_MODULE):
    if _name.startswith("__") and _name not in {"__all__", "__doc__", "__path__"}:
        continue
    globals()[_name] = getattr(_MODULE, _name)

if "__all__" not in globals():
    __all__ = [name for name in globals() if not name.startswith("_")]
