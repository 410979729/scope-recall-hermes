"""Canonical lifecycle operation definitions and registry-derived contracts.

Program 1B first observes the existing V1 receipt identities, then makes this
registry the only place where lifecycle operations are defined.  The registry
does not persist ``operation_id`` in V1 receipts: callers still receive the
historical ``event_type`` and ``action`` values declared here.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping


class UnknownLifecycleOperationError(LookupError):
    """The requested lifecycle operation is not registered."""


class InvalidLifecycleRegistryError(RuntimeError):
    """The static lifecycle registry violates its construction contract."""


class InvalidLifecycleTransitionError(ValueError):
    """A registered operation cannot perform the requested state transition."""


@dataclass(frozen=True, slots=True)
class LifecycleOperation:
    operation_id: str
    domain: str
    allowed_from_states: frozenset[str]
    target_state: str
    legacy_event_type: str
    legacy_action: str
    authorization_policy: str
    fact_authority_required: bool
    reversible: bool
    rollback_operation_id: str | None
    projection_effects: tuple[str, ...]
    receipt_policy: str


@dataclass(frozen=True, slots=True)
class LifecycleProducerBinding:
    producer: str
    operation_ids: tuple[str, ...]


ARCHIVE_COVERAGE_RECEIPT: Final = "v1_archive_coverage"
HISTORICAL_ARCHIVE_COVERAGE_RECEIPT: Final = "v1_historical_archive_coverage"
GOVERNANCE_RECEIPT: Final = "v1_governance_audit"
SYNTHETIC_RECEIPT: Final = "v1_synthetic_governance_audit"

ANY_STATE: Final = "*"
CURRENT_STATE: Final = "$current"
REQUESTED_STATE: Final = "$requested"
RECEIPT_BEFORE_STATE: Final = "$receipt_before"
DELETED_STATE: Final = "$deleted"

TRANSITION_PROJECTIONS: Final = (
    "sqlite_truth",
    "fts_visibility",
    "entities",
    "relations",
    "freshness",
    "vector_outbox",
    "governance_audit",
)
DELETE_PROJECTIONS: Final = (
    "sqlite_truth",
    "vector_outbox",
    "governance_audit",
)
AUDIT_ONLY_PROJECTIONS: Final = ("governance_audit",)


MEMORY_CLEANUP_ARCHIVE: Final = "memory.cleanup.archive"
MEMORY_CLEANUP_RESTORE: Final = "memory.cleanup.restore"
FORGETTING_ARCHIVE: Final = "memory.forgetting.archive"
FORGETTING_RESTORE: Final = "memory.forgetting.restore"
SCOPE_FORGET_ARCHIVE: Final = "memory.scope_forget.archive"
SCOPE_FORGET_RESTORE: Final = "memory.scope_forget.restore"
AUTO_ADJUDICATION_ARCHIVE: Final = "memory.auto_adjudication.archive"
AUTO_ADJUDICATION_RESTORE: Final = "memory.auto_adjudication.restore"
AUTO_ADJUDICATION_PROMOTE: Final = "memory.auto_adjudication.promote"
CANDIDATE_PROMOTION_ARCHIVE: Final = "memory.candidate_promotion.archive"
CANDIDATE_PROMOTION_PROMOTE: Final = "memory.candidate_promotion.promote"
CANDIDATE_REVIEW_ARCHIVE: Final = "memory.candidate_review.archive"
CANDIDATE_REVIEW_PROMOTE: Final = "memory.candidate_review.promote"
CANDIDATE_REVIEW_SUPERSEDE: Final = "memory.candidate_review.supersede"
FACT_EVOLUTION_SUPERSEDE: Final = "fact.evolution.supersede_memory"
FACT_EVOLUTION_RETRACT: Final = "fact.evolution.retract_memory"
GOVERNANCE_CLASSIFY_METADATA: Final = "memory.governance.classify_metadata"
LEGACY_HYGIENE_ARCHIVE: Final = "memory.legacy_hygiene.archive_scratch"
LEGACY_HYGIENE_NORMALIZE: Final = "memory.legacy_hygiene.normalize_metadata"
BENCHMARK_MARK_LIFECYCLE: Final = "benchmark.fixture.mark_lifecycle"
BENCHMARK_ARCHIVE: Final = "benchmark.fixture.archive"
HARD_DELETE_DEFAULT: Final = "memory.hard_delete.compatibility_default"
HARD_DELETE_FORGETTING: Final = "memory.forgetting.hard_delete"
HARD_DELETE_MERGE_SOURCE: Final = "memory.merge_source.hard_delete"
HARD_DELETE_EXPLICIT: Final = "memory.explicit.hard_delete"
HARD_DELETE_DEDUPE: Final = "memory.dedupe.hard_delete"
HARD_DELETE_NIGHTLY_DEDUPE: Final = "memory.nightly_dedupe.hard_delete"
LEGACY_ARCHIVE_BACKFILL: Final = "memory.archive_coverage.legacy_backfill"
QUALITY_LINT_ARCHIVE_RECEIPT: Final = "memory.archive_coverage.quality_lint"
QUALITY_CLEANUP_ARCHIVE_RECEIPT: Final = "memory.archive_coverage.quality_cleanup"


def _operation(
    operation_id: str,
    *,
    domain: str = "memory",
    allowed_from_states: tuple[str, ...] = (ANY_STATE,),
    target_state: str,
    legacy_event_type: str,
    legacy_action: str,
    authorization_policy: str,
    fact_authority_required: bool = False,
    reversible: bool = False,
    rollback_operation_id: str | None = None,
    projection_effects: tuple[str, ...] = TRANSITION_PROJECTIONS,
    receipt_policy: str = GOVERNANCE_RECEIPT,
) -> LifecycleOperation:
    return LifecycleOperation(
        operation_id=operation_id,
        domain=domain,
        allowed_from_states=frozenset(allowed_from_states),
        target_state=target_state,
        legacy_event_type=legacy_event_type,
        legacy_action=legacy_action,
        authorization_policy=authorization_policy,
        fact_authority_required=fact_authority_required,
        reversible=reversible,
        rollback_operation_id=rollback_operation_id,
        projection_effects=projection_effects,
        receipt_policy=receipt_policy,
    )


_OPERATIONS: Final = (
    _operation(
        MEMORY_CLEANUP_ARCHIVE,
        target_state="archived",
        legacy_event_type="memory_cleanup",
        legacy_action="soft_archive",
        authorization_policy="maintenance_apply",
        reversible=True,
        rollback_operation_id=MEMORY_CLEANUP_RESTORE,
        receipt_policy=ARCHIVE_COVERAGE_RECEIPT,
    ),
    _operation(
        MEMORY_CLEANUP_RESTORE,
        allowed_from_states=("archived",),
        target_state=RECEIPT_BEFORE_STATE,
        legacy_event_type="memory_cleanup",
        legacy_action="rollback_soft_archive",
        authorization_policy="evidence_bound_rollback",
    ),
    _operation(
        FORGETTING_ARCHIVE,
        target_state="archived",
        legacy_event_type="forgetting",
        legacy_action="soft_archive",
        authorization_policy="maintenance_apply",
        reversible=True,
        rollback_operation_id=FORGETTING_RESTORE,
        receipt_policy=ARCHIVE_COVERAGE_RECEIPT,
    ),
    _operation(
        FORGETTING_RESTORE,
        allowed_from_states=("archived",),
        target_state=RECEIPT_BEFORE_STATE,
        legacy_event_type="forgetting",
        legacy_action="rollback_soft_archive",
        authorization_policy="evidence_bound_rollback",
    ),
    _operation(
        SCOPE_FORGET_ARCHIVE,
        target_state="archived",
        legacy_event_type="scope_recall_forget",
        legacy_action="soft_archive",
        authorization_policy="explicit_operator_request",
        reversible=True,
        rollback_operation_id=SCOPE_FORGET_RESTORE,
        receipt_policy=ARCHIVE_COVERAGE_RECEIPT,
    ),
    _operation(
        SCOPE_FORGET_RESTORE,
        allowed_from_states=("archived",),
        target_state=RECEIPT_BEFORE_STATE,
        legacy_event_type="scope_recall_forget",
        legacy_action="rollback_soft_archive",
        authorization_policy="evidence_bound_rollback",
    ),
    _operation(
        AUTO_ADJUDICATION_ARCHIVE,
        allowed_from_states=("candidate",),
        target_state="archived",
        legacy_event_type="memory_auto_adjudication",
        legacy_action="archive",
        authorization_policy="scheduled_adjudication",
        reversible=True,
        rollback_operation_id=AUTO_ADJUDICATION_RESTORE,
        receipt_policy=ARCHIVE_COVERAGE_RECEIPT,
    ),
    _operation(
        AUTO_ADJUDICATION_RESTORE,
        allowed_from_states=("archived",),
        target_state=RECEIPT_BEFORE_STATE,
        legacy_event_type="memory_auto_adjudication",
        legacy_action="rollback_soft_archive",
        authorization_policy="evidence_bound_rollback",
    ),
    _operation(
        AUTO_ADJUDICATION_PROMOTE,
        allowed_from_states=("candidate",),
        target_state="promoted",
        legacy_event_type="memory_auto_adjudication",
        legacy_action="promote",
        authorization_policy="scheduled_adjudication",
    ),
    _operation(
        CANDIDATE_PROMOTION_ARCHIVE,
        allowed_from_states=("candidate",),
        target_state="archived",
        legacy_event_type="memory_candidate_promotion",
        legacy_action="archive",
        authorization_policy="maintenance_apply",
        receipt_policy=ARCHIVE_COVERAGE_RECEIPT,
    ),
    _operation(
        CANDIDATE_PROMOTION_PROMOTE,
        allowed_from_states=("candidate",),
        target_state="promoted",
        legacy_event_type="memory_candidate_promotion",
        legacy_action="promote",
        authorization_policy="maintenance_apply",
    ),
    _operation(
        CANDIDATE_REVIEW_ARCHIVE,
        allowed_from_states=("candidate",),
        target_state="archived",
        legacy_event_type="memory_candidate_review",
        legacy_action="archive",
        authorization_policy="explicit_operator_review",
    ),
    _operation(
        CANDIDATE_REVIEW_PROMOTE,
        allowed_from_states=("candidate",),
        target_state="promoted",
        legacy_event_type="memory_candidate_review",
        legacy_action="promote",
        authorization_policy="explicit_operator_review",
    ),
    _operation(
        CANDIDATE_REVIEW_SUPERSEDE,
        allowed_from_states=("candidate",),
        target_state="superseded",
        legacy_event_type="memory_candidate_review",
        legacy_action="supersede",
        authorization_policy="explicit_operator_review",
    ),
    _operation(
        FACT_EVOLUTION_SUPERSEDE,
        domain="fact",
        target_state="superseded",
        legacy_event_type="fact_evolution",
        legacy_action="supersede_old",
        authorization_policy="fact_executor",
        fact_authority_required=True,
    ),
    _operation(
        FACT_EVOLUTION_RETRACT,
        domain="fact",
        target_state="obsolete",
        legacy_event_type="fact_evolution",
        legacy_action="retract",
        authorization_policy="fact_executor",
        fact_authority_required=True,
    ),
    _operation(
        GOVERNANCE_CLASSIFY_METADATA,
        target_state=CURRENT_STATE,
        legacy_event_type="memory_governance",
        legacy_action="classify_metadata",
        authorization_policy="maintenance_apply",
    ),
    _operation(
        LEGACY_HYGIENE_ARCHIVE,
        target_state="archived",
        legacy_event_type="legacy_hygiene",
        legacy_action="archive_legacy_scratch",
        authorization_policy="migration_apply",
    ),
    _operation(
        LEGACY_HYGIENE_NORMALIZE,
        target_state=CURRENT_STATE,
        legacy_event_type="legacy_hygiene",
        legacy_action="normalize_durable_metadata",
        authorization_policy="migration_apply",
    ),
    _operation(
        BENCHMARK_MARK_LIFECYCLE,
        domain="test_fixture",
        target_state=REQUESTED_STATE,
        legacy_event_type="benchmark_fixture_lifecycle",
        legacy_action="mark_fixture_lifecycle",
        authorization_policy="isolated_benchmark_fixture",
        receipt_policy=SYNTHETIC_RECEIPT,
    ),
    _operation(
        BENCHMARK_ARCHIVE,
        domain="test_fixture",
        target_state="archived",
        legacy_event_type="benchmark_fixture_lifecycle",
        legacy_action="archive_fixture",
        authorization_policy="isolated_benchmark_fixture",
        receipt_policy=SYNTHETIC_RECEIPT,
    ),
    *(
        _operation(
            operation_id,
            target_state=DELETED_STATE,
            legacy_event_type=event_type,
            legacy_action="hard_delete",
            authorization_policy=authorization_policy,
            projection_effects=DELETE_PROJECTIONS,
        )
        for operation_id, event_type, authorization_policy in (
            (HARD_DELETE_DEFAULT, "memory_hard_delete", "compatibility_adapter"),
            (HARD_DELETE_FORGETTING, "forgetting", "maintenance_apply"),
            (
                HARD_DELETE_MERGE_SOURCE,
                "scope_recall_merge_source_delete",
                "atomic_merge",
            ),
            (HARD_DELETE_EXPLICIT, "scope_recall_hard_delete", "explicit_operator_request"),
            (HARD_DELETE_DEDUPE, "memory_dedupe_hard_delete", "maintenance_apply"),
            (
                HARD_DELETE_NIGHTLY_DEDUPE,
                "nightly_duplicate_hard_delete",
                "scheduled_maintenance",
            ),
        )
    ),
    _operation(
        LEGACY_ARCHIVE_BACKFILL,
        domain="compatibility_receipt",
        allowed_from_states=("archived",),
        target_state="archived",
        legacy_event_type="memory_cleanup",
        legacy_action="legacy_archive_backfill",
        authorization_policy="maintenance_apply",
        projection_effects=AUDIT_ONLY_PROJECTIONS,
        receipt_policy=HISTORICAL_ARCHIVE_COVERAGE_RECEIPT,
    ),
    _operation(
        QUALITY_LINT_ARCHIVE_RECEIPT,
        domain="compatibility_receipt",
        target_state="archived",
        legacy_event_type="memory_quality_lint",
        legacy_action="archive_lint_hit",
        authorization_policy="historical_receipt",
        projection_effects=AUDIT_ONLY_PROJECTIONS,
        receipt_policy=HISTORICAL_ARCHIVE_COVERAGE_RECEIPT,
    ),
    _operation(
        QUALITY_CLEANUP_ARCHIVE_RECEIPT,
        domain="compatibility_receipt",
        target_state="archived",
        legacy_event_type="memory_quality_cleanup",
        legacy_action="archive_active_lint_hit",
        authorization_policy="historical_receipt",
        projection_effects=AUDIT_ONLY_PROJECTIONS,
        receipt_policy=HISTORICAL_ARCHIVE_COVERAGE_RECEIPT,
    ),
)


def _build_registry() -> Mapping[str, LifecycleOperation]:
    registry: dict[str, LifecycleOperation] = {}
    for operation in _OPERATIONS:
        if operation.operation_id in registry:
            raise InvalidLifecycleRegistryError(
                f"duplicate lifecycle operation_id: {operation.operation_id}"
            )
        registry[operation.operation_id] = operation
    return MappingProxyType(registry)


LIFECYCLE_REGISTRY: Final = _build_registry()


LIFECYCLE_PRODUCER_CENSUS: Final = (
    LifecycleProducerBinding(
        "candidate_review.py:review_candidate",
        (
            CANDIDATE_REVIEW_PROMOTE,
            CANDIDATE_REVIEW_ARCHIVE,
            CANDIDATE_REVIEW_SUPERSEDE,
        ),
    ),
    LifecycleProducerBinding(
        "auto_adjudication.py:_transition",
        (AUTO_ADJUDICATION_PROMOTE, AUTO_ADJUDICATION_ARCHIVE),
    ),
    LifecycleProducerBinding(
        "forgetting.py",
        (FORGETTING_ARCHIVE, HARD_DELETE_FORGETTING),
    ),
    LifecycleProducerBinding(
        "governance_cleanup.py",
        (MEMORY_CLEANUP_ARCHIVE, LEGACY_ARCHIVE_BACKFILL),
    ),
    LifecycleProducerBinding(
        "fact_executor.py:execute_evolution_plan",
        (FACT_EVOLUTION_SUPERSEDE, FACT_EVOLUTION_RETRACT),
    ),
    LifecycleProducerBinding(
        "governance_rollback.py:rollback_cleanup_batch",
        (
            MEMORY_CLEANUP_RESTORE,
            FORGETTING_RESTORE,
            SCOPE_FORGET_RESTORE,
            AUTO_ADJUDICATION_RESTORE,
        ),
    ),
    LifecycleProducerBinding(
        "memory_ops.py",
        (
            GOVERNANCE_CLASSIFY_METADATA,
            SCOPE_FORGET_ARCHIVE,
            HARD_DELETE_MERGE_SOURCE,
            HARD_DELETE_EXPLICIT,
            HARD_DELETE_DEDUPE,
        ),
    ),
    LifecycleProducerBinding(
        "nightly_digest.py:cleanup_exact_duplicates",
        (HARD_DELETE_NIGHTLY_DEDUPE,),
    ),
    LifecycleProducerBinding(
        "scripts/promote.memory_candidates.py",
        (CANDIDATE_PROMOTION_PROMOTE, CANDIDATE_PROMOTION_ARCHIVE),
    ),
    LifecycleProducerBinding(
        "scripts/migrate.legacy_hygiene.py",
        (LEGACY_HYGIENE_ARCHIVE, LEGACY_HYGIENE_NORMALIZE),
    ),
    LifecycleProducerBinding(
        "scripts/benchmark.golden.py",
        (BENCHMARK_MARK_LIFECYCLE,),
    ),
    LifecycleProducerBinding(
        "scripts/benchmark.retrieval_regression.py",
        (BENCHMARK_ARCHIVE,),
    ),
    LifecycleProducerBinding(
        "lifecycle_service.py:legacy_default",
        (HARD_DELETE_DEFAULT,),
    ),
)


def resolve_lifecycle_operation(operation_id: str) -> LifecycleOperation:
    normalized = str(operation_id or "").strip()
    try:
        return LIFECYCLE_REGISTRY[normalized]
    except KeyError as exc:
        raise UnknownLifecycleOperationError(
            f"unregistered lifecycle operation_id: {normalized or '<empty>'}"
        ) from exc


def archive_coverage_receipts() -> frozenset[tuple[str, str]]:
    policies = {
        ARCHIVE_COVERAGE_RECEIPT,
        HISTORICAL_ARCHIVE_COVERAGE_RECEIPT,
    }
    return frozenset(
        (operation.legacy_event_type, operation.legacy_action)
        for operation in LIFECYCLE_REGISTRY.values()
        if operation.receipt_policy in policies
    )


def default_rollback_event_actions() -> Mapping[str, str]:
    result: dict[str, str] = {}
    for operation in LIFECYCLE_REGISTRY.values():
        if not operation.reversible or not operation.rollback_operation_id:
            continue
        existing = result.get(operation.legacy_event_type)
        if existing is not None and existing != operation.legacy_action:
            raise InvalidLifecycleRegistryError(
                "default rollback event_type has multiple source actions: "
                f"{operation.legacy_event_type}"
            )
        result[operation.legacy_event_type] = operation.legacy_action
    return MappingProxyType(result)


def rollback_operation_for_event_type(event_type: str) -> LifecycleOperation:
    normalized = str(event_type or "").strip()
    candidates = [
        operation
        for operation in LIFECYCLE_REGISTRY.values()
        if operation.legacy_event_type == normalized
        and operation.reversible
        and operation.rollback_operation_id
    ]
    if len(candidates) != 1:
        raise UnknownLifecycleOperationError(
            f"event_type has no unique registered rollback operation: {normalized or '<empty>'}"
        )
    rollback_id = candidates[0].rollback_operation_id
    assert rollback_id is not None
    return resolve_lifecycle_operation(rollback_id)


def validate_lifecycle_transition(
    operation: LifecycleOperation,
    *,
    current_state: str,
    target_state: str,
) -> None:
    """Validate state movement without weakening receipt-bound restore semantics."""

    current = str(current_state or "active").strip().lower()
    target = str(target_state or "").strip().lower()
    if ANY_STATE not in operation.allowed_from_states and current not in operation.allowed_from_states:
        raise InvalidLifecycleTransitionError(
            f"{operation.operation_id} refuses lifecycle transition from {current}"
        )
    expected = operation.target_state
    if expected == DELETED_STATE:
        if target != DELETED_STATE:
            raise InvalidLifecycleTransitionError(
                f"{operation.operation_id} is destructive, not a lifecycle transition"
            )
        return
    if expected == CURRENT_STATE:
        allowed = target == current
    elif expected == REQUESTED_STATE:
        allowed = bool(target)
    elif expected == RECEIPT_BEFORE_STATE:
        allowed = current == "archived" and bool(target) and target != "archived"
    else:
        allowed = target == expected
    if not allowed:
        raise InvalidLifecycleTransitionError(
            f"{operation.operation_id} targets {expected}, not {target or '<empty>'}"
        )


def validate_lifecycle_registry() -> tuple[str, ...]:
    errors: list[str] = []
    for operation in LIFECYCLE_REGISTRY.values():
        values = (
            operation.operation_id,
            operation.domain,
            operation.target_state,
            operation.legacy_event_type,
            operation.legacy_action,
            operation.authorization_policy,
            operation.receipt_policy,
        )
        if any(not value.strip() for value in values):
            errors.append(f"{operation.operation_id}: required text field is empty")
        if not operation.allowed_from_states:
            errors.append(f"{operation.operation_id}: allowed_from_states is empty")
        if not operation.projection_effects:
            errors.append(f"{operation.operation_id}: projection_effects is empty")
        if operation.reversible != bool(operation.rollback_operation_id):
            errors.append(
                f"{operation.operation_id}: reversible and rollback_operation_id disagree"
            )
        if (
            operation.rollback_operation_id
            and operation.rollback_operation_id not in LIFECYCLE_REGISTRY
        ):
            errors.append(
                f"{operation.operation_id}: unknown rollback operation "
                f"{operation.rollback_operation_id}"
            )
    for binding in LIFECYCLE_PRODUCER_CENSUS:
        if not binding.producer.strip() or not binding.operation_ids:
            errors.append("producer census contains an empty binding")
        for operation_id in binding.operation_ids:
            if operation_id not in LIFECYCLE_REGISTRY:
                errors.append(
                    f"{binding.producer}: unknown operation_id {operation_id}"
                )
    return tuple(errors)


def lifecycle_registry_report() -> dict[str, object]:
    errors = validate_lifecycle_registry()
    return {
        "status": "ready" if not errors else "invalid",
        "operation_count": len(LIFECYCLE_REGISTRY),
        "producer_count": len(LIFECYCLE_PRODUCER_CENSUS),
        "archive_coverage_receipt_count": len(archive_coverage_receipts()),
        "default_rollback_event_count": len(default_rollback_event_actions()),
        "errors": list(errors),
    }
