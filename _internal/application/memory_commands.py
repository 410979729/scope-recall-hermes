"""Provider-neutral memory command use cases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class StoreMemoryRequest:
    content: str
    source: str
    target: str
    session_id: str
    metadata: dict[str, object] | None = None
    allow_duplicate: bool = False
    semantic_merge: bool = False
    scope_mode: str | None = None


@dataclass(frozen=True, slots=True)
class UpdateMemoryRequest:
    memory_id: str
    content: str
    target: str | None = None


@dataclass(frozen=True, slots=True)
class MergeMemoriesRequest:
    target_id: str
    source_ids: tuple[str, ...]
    content: str | None = None
    target: str | None = None


@dataclass(frozen=True, slots=True)
class ArchiveMemoriesRequest:
    ids: tuple[str, ...]
    reason: str = "scope_recall_forget"
    actor: str = "scope_recall_forget"
    batch_id: str = ""


@dataclass(frozen=True, slots=True)
class DeleteMemoriesRequest:
    ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FeedbackMemoryRequest:
    memory_id: str
    rating: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class GovernMemoriesRequest:
    dry_run: bool = True
    scope_only: bool = True


@dataclass(frozen=True, slots=True)
class DedupeMemoriesRequest:
    dry_run: bool = True
    scope_only: bool = True


@dataclass(frozen=True, slots=True)
class FactOwnedMemoryIdsRequest:
    ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PrivacyPurgeRequest:
    action: str
    ids: tuple[str, ...] = ()
    operation_id: str = ""
    confirmation: str = ""


@runtime_checkable
class MemoryCommandGateway(Protocol):
    def store(self, request: StoreMemoryRequest) -> tuple[str, bool, str]: ...

    def update(self, request: UpdateMemoryRequest) -> tuple[bool, str, str]: ...

    def merge(self, request: MergeMemoriesRequest) -> dict[str, object]: ...

    def archive(self, request: ArchiveMemoriesRequest) -> dict[str, object]: ...

    def delete(self, request: DeleteMemoriesRequest) -> int: ...

    def feedback(self, request: FeedbackMemoryRequest) -> dict[str, object]: ...

    def govern(self, request: GovernMemoriesRequest) -> dict[str, object]: ...

    def dedupe(self, request: DedupeMemoriesRequest) -> dict[str, object]: ...

    def repair(self) -> dict[str, object]: ...

    def fact_owned(self, request: FactOwnedMemoryIdsRequest) -> list[str]: ...

    def purge(self, request: PrivacyPurgeRequest) -> dict[str, object]: ...


class MemoryCommandApplication:
    """Typed command use cases; infrastructure is supplied by one gateway."""

    def __init__(self, gateway: MemoryCommandGateway) -> None:
        self._gateway = gateway

    def store(self, request: StoreMemoryRequest) -> tuple[str, bool, str]:
        return self._gateway.store(request)

    def update(self, request: UpdateMemoryRequest) -> tuple[bool, str, str]:
        return self._gateway.update(request)

    def merge(self, request: MergeMemoriesRequest) -> dict[str, object]:
        return self._gateway.merge(request)

    def archive(self, request: ArchiveMemoriesRequest) -> dict[str, object]:
        return self._gateway.archive(request)

    def delete(self, request: DeleteMemoriesRequest) -> int:
        return self._gateway.delete(request)

    def feedback(self, request: FeedbackMemoryRequest) -> dict[str, object]:
        return self._gateway.feedback(request)

    def govern(self, request: GovernMemoriesRequest) -> dict[str, object]:
        return self._gateway.govern(request)

    def dedupe(self, request: DedupeMemoriesRequest) -> dict[str, object]:
        return self._gateway.dedupe(request)

    def repair(self) -> dict[str, object]:
        return self._gateway.repair()

    def fact_owned(self, request: FactOwnedMemoryIdsRequest) -> list[str]:
        return self._gateway.fact_owned(request)

    def purge(self, request: PrivacyPurgeRequest) -> dict[str, object]:
        return self._gateway.purge(request)
