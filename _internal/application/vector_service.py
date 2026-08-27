"""Typed vector companion application service."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class VectorGateway(Protocol):
    def setup(self) -> None: ...

    def status_payload(self) -> dict[str, object]: ...

    def embed_query_variants(
        self, queries: tuple[str, ...]
    ) -> tuple[tuple[float, ...], ...]: ...

    def mark_needs_repair(self, reason: str) -> None: ...


class VectorApplication:
    def __init__(self, gateway: VectorGateway) -> None:
        self._gateway = gateway

    def setup(self) -> None:
        self._gateway.setup()

    def status_payload(self) -> dict[str, object]:
        return self._gateway.status_payload()

    def embed_query_variants(
        self, queries: tuple[str, ...]
    ) -> tuple[tuple[float, ...], ...]:
        return self._gateway.embed_query_variants(queries)

    def mark_needs_repair(self, reason: str) -> None:
        self._gateway.mark_needs_repair(reason)
