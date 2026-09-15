"""Legacy module-monkeypatch resolution confined to the outer boundary."""

from __future__ import annotations

import sys
from typing import Any, TypeVar, cast


T = TypeVar("T")
_MISSING = object()


class ProviderModuleHooks:
    """Resolve V1 monkeypatch anchors without leaking module lookup into Core."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def resolve(self, name: str, default: T) -> T:
        module_names: list[str] = []
        provider_module = type(self._provider).__module__ or ""
        if provider_module:
            module_names.append(provider_module)
        if provider_module != "scope_recall.provider":
            module_names.append("scope_recall.provider")
        seen: set[int] = set()
        for module_name in module_names:
            module = sys.modules.get(module_name)
            if module is None or id(module) in seen:
                continue
            seen.add(id(module))
            value = getattr(module, name, _MISSING)
            if value is not _MISSING and value is not None:
                return cast(T, value)
        return default
