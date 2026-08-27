"""Vector infrastructure behind the typed application service."""

from __future__ import annotations

import sys
from typing import Any, cast

from ..application.vector_service import VectorGateway
from .ports import RuntimeAdapterPort
from .vector_view import RuntimeVectorView
from ...vector_runtime import (
    mark_vector_needs_repair as default_mark_vector_needs_repair,
)
from ...vector_runtime import setup_vector_layer as default_setup_vector_layer


def _provider_hook(host: Any, name: str, default: Any) -> Any:
    module = sys.modules.get(type(host).__module__)
    candidate = getattr(module, name, None) if module is not None else None
    return candidate if callable(candidate) else default


class ProviderVectorAdapter:
    def __init__(self, host: RuntimeAdapterPort, view: RuntimeVectorView) -> None:
        self._host = host
        self._view = view

    def setup(self) -> None:
        operation = _provider_hook(
            self._host, "setup_vector_layer", default_setup_vector_layer
        )
        operation(self._host)

    def status_payload(self) -> dict[str, object]:
        return cast(dict[str, object], self._view.vector_status_view())

    def embed_query_variants(
        self, queries: tuple[str, ...]
    ) -> tuple[tuple[float, ...], ...]:
        rows = self._view.embed_query_variants(list(queries))
        return tuple(tuple(float(value) for value in row) for row in rows)

    def mark_needs_repair(self, reason: str) -> None:
        operation = _provider_hook(
            self._host,
            "mark_vector_needs_repair",
            default_mark_vector_needs_repair,
        )
        operation(self._host, reason)


def bind_vector_gateway(
    host: RuntimeAdapterPort, view: RuntimeVectorView
) -> VectorGateway:
    return ProviderVectorAdapter(host, view)

