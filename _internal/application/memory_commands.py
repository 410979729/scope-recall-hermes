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
class ReviewMemoryCandidateRequest:
    memory_id: str
    action: str
    dry_run: bool = True
    expected_updated_at: str = ""
    expected_lifecycle: str = ""


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
class DeleteMemoriesResult:
    """Truthful hard-delete outcome used by new application/tool callers.

    Provider-facing compatibility wrappers may still collapse this object to
    ``deleted_count``.  Public callers must retain the actual deleted/skipped
    partition and companion-erasure state.
    """

    requested_ids: tuple[str, ...]
    deleted_ids: tuple[str, ...]
    skipped_ids: tuple[str, ...]
    deleted_count: int
    vector_pending: bool
    companion_erasure_pending: bool
    data_retained: bool
    mutation_applied: bool
    vector_outbox_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.deleted_count != len(self.deleted_ids):
            raise ValueError("deleted_count must equal len(deleted_ids)")
        if set(self.deleted_ids) & set(self.skipped_ids):
            raise ValueError("deleted_ids and skipped_ids must be disjoint")
        if len(self.requested_ids) != len(set(self.requested_ids)):
            raise ValueError("requested_ids must be stable unique IDs")
        if len(self.vector_outbox_keys) != len(set(self.vector_outbox_keys)):
            raise ValueError("vector_outbox_keys must be stable unique keys")
        combined = set(self.deleted_ids) | set(self.skipped_ids)
        if set(self.requested_ids) != combined:
            raise ValueError(
                "requested_ids must equal the deleted/skipped partition"
            )


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
    def review_candidate(self, request: ReviewMemoryCandidateRequest) -> dict[str, object]: ...

    def store(self, request: StoreMemoryRequest) -> tuple[str, bool, str]: ...

    def update(self, request: UpdateMemoryRequest) -> tuple[bool, str, str]: ...

    def merge(self, request: MergeMemoriesRequest) -> dict[str, object]: ...

    def archive(self, request: ArchiveMemoriesRequest) -> dict[str, object]: ...

    def delete(self, request: DeleteMemoriesRequest) -> DeleteMemoriesResult: ...

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

    def review_candidate(self, request: ReviewMemoryCandidateRequest) -> dict[str, object]:
        return self._gateway.review_candidate(request)

    def update(self, request: UpdateMemoryRequest) -> tuple[bool, str, str]:
        return self._gateway.update(request)

    def merge(self, request: MergeMemoriesRequest) -> dict[str, object]:
        return self._gateway.merge(request)

    def archive(self, request: ArchiveMemoriesRequest) -> dict[str, object]:
        return self._gateway.archive(request)

    def delete(self, request: DeleteMemoriesRequest) -> DeleteMemoriesResult:
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
