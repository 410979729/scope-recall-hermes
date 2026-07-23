"""Atomic executor for evidence-authorized factual evolution plans.

This is the sole coordinator allowed to mutate the memory row, temporal ledger,
freshness, graph, governance audit, vector outbox, and idempotency receipt as one
SQLite unit. It never performs external vector I/O or calls a model provider.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any

from .capture_filters import (
    contains_secret_like_text,
    sanitize_report_text,
    sanitize_structured_value,
)
from .evolution_policy import EvolutionPolicyDecision
from .fact_actions import EvolutionAction, EvolutionPlan, EvolutionResult
from .fact_repository import (
    FACT_EXECUTOR_MUTATION_AUTHORITY,
    TemporalConflictError,
    close_claim_interval,
    insert_claim,
    link_claim_evidence,
    retract_claim,
)
from .freshness import upsert_memory_freshness
from .graph_relations import upsert_relation
from .lifecycle_service import LifecycleConflictError, transition_memory_lifecycle
from .sql_store import record_governance_audit_event, store_row
from .vector_generation import current_generation_id, enqueue_vector_event


class FactExecutionError(RuntimeError):
    """A mandatory execution surface failed and the unit was rolled back."""


class FactExecutionConflictError(FactExecutionError):
    """The reviewed request no longer matches truth or an idempotency receipt."""


@dataclass(frozen=True, slots=True)
class FactProvenanceReference:
    """Trusted runtime provenance attached without granting policy authority."""

    source_type: str
    source_ref: str
    excerpt: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        source_type = str(self.source_type or "").strip()
        source_ref = str(self.source_ref or "").strip()
        excerpt = str(self.excerpt or "").strip()
        safe_metadata, _ = sanitize_structured_value(dict(self.metadata))
        if not source_type or not source_ref:
            raise FactExecutionError("provenance source_type and source_ref are required")
        if len(source_type) > 64 or len(source_ref) > 500 or len(excerpt) > 1000:
            raise FactExecutionError("provenance reference exceeds bounded size")
        if contains_secret_like_text(excerpt):
            raise FactExecutionError("secret-like provenance excerpt is not allowed")
        return {
            "source_type": source_type,
            "source_ref": source_ref,
            "excerpt": sanitize_report_text(excerpt),
            "metadata": safe_metadata if isinstance(safe_metadata, Mapping) else {},
        }


_AUDIT_ONLY_REQUEST_KEYS = frozenset({"digest_run_id", "journal_run_id"})


def _semantic_request_value(value: Any) -> Any:
    """Remove only scheduler-run audit labels from replay equivalence material."""

    if isinstance(value, Mapping):
        return {
            str(key): _semantic_request_value(item)
            for key, item in value.items()
            if str(key) not in _AUDIT_ONLY_REQUEST_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_semantic_request_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class FactExecutionContext:
    """Trusted, explicit runtime context supplied outside the model proposal."""

    scope_id: str
    writable_scope_ids: tuple[str, ...]
    actor: str
    timestamp: str
    source: str = "fact_evolution"
    target: str = "memory"
    session_id: str = ""
    platform: str = ""
    user_id: str = ""
    chat_id: str = ""
    thread_id: str = ""
    gateway_session_key: str = ""
    agent_identity: str = ""
    agent_workspace: str = ""
    new_memory_id: str = ""
    new_claim_id: str = ""
    memory_content: str = ""
    effective_at: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    provenance_refs: tuple[FactProvenanceReference, ...] = ()

    def request_binding(self) -> dict[str, Any]:
        safe_metadata, _ = sanitize_structured_value(dict(self.metadata))
        semantic_metadata = _semantic_request_value(
            safe_metadata if isinstance(safe_metadata, Mapping) else {}
        )
        semantic_provenance = [
            _semantic_request_value(reference.as_dict())
            for reference in self.provenance_refs
        ]
        return {
            "scope_id": self.scope_id,
            "writable_scope_ids": sorted(set(self.writable_scope_ids)),
            "source": self.source,
            "target": self.target,
            "session_id": self.session_id,
            "platform": self.platform,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "gateway_session_key": self.gateway_session_key,
            "agent_identity": self.agent_identity,
            "agent_workspace": self.agent_workspace,
            "new_memory_id": self.new_memory_id,
            "new_claim_id": self.new_claim_id,
            "memory_content": self.memory_content,
            "effective_at": self.effective_at,
            "metadata": semantic_metadata,
            "provenance_refs": semantic_provenance,
        }


FaultInjector = Callable[[str], None]


def _canonical_json(value: Any) -> str:
    safe, _ = sanitize_structured_value(value)
    return json.dumps(
        safe,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _request_hash(
    plan: EvolutionPlan,
    policy: EvolutionPolicyDecision,
    context: FactExecutionContext,
) -> str:
    material = _canonical_json(
        {
            "plan": plan.as_dict(),
            "policy": policy.as_dict(),
            "context": context.request_binding(),
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, material: str) -> str:
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _normalize_timestamp(value: str, *, field_name: str) -> tuple[str, datetime]:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        raise FactExecutionError(f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise FactExecutionError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FactExecutionError(f"{field_name} must include a timezone")
    normalized = parsed.astimezone(timezone.utc)
    return normalized.isoformat(), normalized


def _validate_executable_valid_time(
    plan: EvolutionPlan,
    *,
    timestamp_dt: datetime,
) -> None:
    """Reject intervals the current static lifecycle cannot represent safely."""

    action = plan.proposal.action
    claim = plan.proposal.claim
    if claim is None or action not in {EvolutionAction.ADD, EvolutionAction.SUPERSEDE}:
        return
    if claim.valid_to:
        if action is EvolutionAction.ADD:
            raise FactExecutionConflictError(
                "finite historical ADD is not supported by the current lifecycle model"
            )
        raise FactExecutionConflictError(
            "finite successor interval is not supported by the current lifecycle model"
        )
    if not claim.valid_from:
        return
    _normalized_valid_from, valid_from_dt = _normalize_timestamp(
        claim.valid_from,
        field_name="claim.valid_from",
    )
    if valid_from_dt > timestamp_dt:
        raise FactExecutionConflictError(
            f"future-effective {action.value} is not supported without a scheduler"
        )


def _retract_effective_at(
    plan: EvolutionPlan,
    context: FactExecutionContext,
    *,
    timestamp: str,
    timestamp_dt: datetime,
) -> str:
    """Bind an omitted retraction boundary to its trusted transaction time."""

    if plan.proposal.action is not EvolutionAction.RETRACT:
        return ""
    effective_at, effective_dt = _normalize_timestamp(
        context.effective_at or timestamp,
        field_name="effective_at",
    )
    if effective_dt > timestamp_dt:
        raise FactExecutionConflictError(
            "future-effective retract is not supported without a scheduler"
        )
    return effective_at


def _fault(injector: FaultInjector | None, stage: str) -> None:
    if injector is not None:
        injector(stage)


def _review_result(
    plan: EvolutionPlan,
    policy: EvolutionPolicyDecision,
    *,
    status: str,
    reason: str,
) -> EvolutionResult:
    return EvolutionResult(
        action_id=plan.action_id,
        action=EvolutionAction.REVIEW,
        status=status,
        applied=False,
        receipt={
            "requested_action": plan.proposal.action.value,
            "effective_action": EvolutionAction.REVIEW.value,
            "policy_mode": plan.policy_mode,
            "reason_codes": list(policy.reason_codes),
        },
        error=sanitize_report_text(reason)[:500],
    )


def _validate_static_boundary(
    plan: EvolutionPlan,
    policy: EvolutionPolicyDecision,
    context: FactExecutionContext,
) -> tuple[str, datetime, str]:
    if not plan.action_id.strip() or not plan.idempotency_key.strip():
        raise FactExecutionError("action_id and idempotency_key are required")
    if len(plan.action_id) > 160 or len(plan.idempotency_key) > 240:
        raise FactExecutionError("action or idempotency identifier is too long")
    if not context.scope_id.strip():
        raise FactExecutionError("scope_id is required")
    writable = {str(scope).strip() for scope in context.writable_scope_ids if str(scope).strip()}
    if context.scope_id not in writable:
        raise FactExecutionConflictError("scope is not writable")
    claim = plan.proposal.claim
    if claim is not None and claim.scope_id != context.scope_id:
        raise FactExecutionConflictError("claim scope does not match execution scope")
    if policy.requested_action is not plan.proposal.action:
        raise FactExecutionConflictError("policy does not bind the requested action")
    if policy.allowed and policy.effective_action is not plan.proposal.action:
        raise FactExecutionConflictError("allowed policy changed the action")
    for provenance in context.provenance_refs:
        provenance.as_dict()
    timestamp, timestamp_dt = _normalize_timestamp(
        context.timestamp,
        field_name="timestamp",
    )
    _validate_executable_valid_time(plan, timestamp_dt=timestamp_dt)
    effective_at = _retract_effective_at(
        plan,
        context,
        timestamp=timestamp,
        timestamp_dt=timestamp_dt,
    )
    return timestamp, timestamp_dt, effective_at


def _target_rows(
    conn: sqlite3.Connection,
    plan: EvolutionPlan,
    context: FactExecutionContext,
) -> dict[str, sqlite3.Row]:
    target_ids = list(dict.fromkeys(plan.proposal.target_ids))
    if not target_ids:
        return {}
    placeholders = ",".join("?" for _ in target_ids)
    rows = conn.execute(
        f"SELECT id, scope_id, updated_at, metadata FROM memories WHERE id IN ({placeholders})",
        target_ids,
    ).fetchall()
    by_id = {str(row["id"]): row for row in rows}
    if set(by_id) != set(target_ids):
        raise FactExecutionConflictError("one or more target memories no longer exist")
    for memory_id, row in by_id.items():
        if str(row["scope_id"] or "") != context.scope_id:
            raise FactExecutionConflictError(f"target memory is outside scope: {memory_id}")
        expected = str(plan.expected_versions.get(memory_id) or "")
        if expected and str(row["updated_at"] or "") != expected:
            raise FactExecutionConflictError(f"memory {memory_id} changed after review")
    if plan.proposal.action in {EvolutionAction.SUPERSEDE, EvolutionAction.RETRACT}:
        missing = [memory_id for memory_id in target_ids if not plan.expected_versions.get(memory_id)]
        if missing:
            raise FactExecutionConflictError("high-risk action requires expected target versions")
    return by_id


def _target_lifecycle(row: sqlite3.Row) -> str:
    try:
        metadata = json.loads(str(row["metadata"] or "{}"))
    except (json.JSONDecodeError, TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, Mapping):
        metadata = {}
    return str(metadata.get("lifecycle") or "active").strip().lower()


def _target_claim_ids(
    conn: sqlite3.Connection,
    plan: EvolutionPlan,
    context: FactExecutionContext,
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    expected_fact_key = plan.proposal.claim.fact_key if plan.proposal.claim is not None else ""
    for memory_id in plan.proposal.target_ids:
        params: list[Any] = [memory_id, context.scope_id]
        where = (
            "memory_id = ? AND scope_id = ? AND status = 'current' "
            "AND retired_at IS NULL"
        )
        if expected_fact_key:
            where += " AND fact_key = ?"
            params.append(expected_fact_key)
        rows = conn.execute(
            f"SELECT claim_id FROM fact_claims WHERE {where} ORDER BY claim_id",
            params,
        ).fetchall()
        claim_ids = [str(row[0]) for row in rows]
        if not claim_ids:
            raise FactExecutionConflictError(
                f"target memory has no matching current fact claim: {memory_id}"
            )
        result[memory_id] = claim_ids
    return result


def _load_replay(
    conn: sqlite3.Connection,
    *,
    plan: EvolutionPlan,
    request_hash: str,
) -> EvolutionResult | None:
    row = conn.execute(
        """
        SELECT action_id, idempotency_key, request_hash, effective_action,
               applied, receipt_json
        FROM fact_action_receipts
        WHERE idempotency_key = ? OR action_id = ?
        ORDER BY CASE WHEN idempotency_key = ? THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (plan.idempotency_key, plan.action_id, plan.idempotency_key),
    ).fetchone()
    if row is None:
        return None
    if str(row["request_hash"] or "") != request_hash:
        raise FactExecutionConflictError("idempotency key or action id was reused for a different request")
    try:
        receipt = json.loads(str(row["receipt_json"] or "{}"))
    except json.JSONDecodeError:
        receipt = {}
    if not isinstance(receipt, dict):
        receipt = {}
    receipt["replayed"] = True
    return EvolutionResult(
        action_id=str(row["action_id"]),
        action=EvolutionAction(str(row["effective_action"])),
        status="replayed",
        applied=bool(row["applied"]),
        receipt=receipt,
    )


def _link_runtime_provenance(
    conn: sqlite3.Connection,
    *,
    claim_ids: list[str],
    context: FactExecutionContext,
    action_id: str,
    timestamp: str,
    receipt: dict[str, Any],
) -> None:
    for claim_id in claim_ids:
        for reference in context.provenance_refs:
            payload = reference.as_dict()
            metadata = dict(payload["metadata"])
            metadata["action_id"] = action_id
            linked = link_claim_evidence(
                conn,
                claim_id=claim_id,
                source_type=str(payload["source_type"]),
                source_ref=str(payload["source_ref"]),
                excerpt=str(payload["excerpt"]),
                recorded_at=timestamp,
                metadata=metadata,
            )
            receipt["evidence_ids"].append(linked.evidence_id)


def _record_vector_outbox_event(
    receipt: dict[str, Any],
    *,
    event_key: str,
    generation_id: str,
    memory_id: str,
    operation: str,
) -> None:
    if not event_key:
        return
    receipt["vector_outbox_keys"].append(event_key)
    receipt["vector_outbox_events"].append(
        {
            "event_key": event_key,
            "generation_id": generation_id,
            "memory_id": memory_id,
            "operation": operation,
        }
    )


def _new_memory_and_claim(
    conn: sqlite3.Connection,
    *,
    plan: EvolutionPlan,
    policy: EvolutionPolicyDecision,
    context: FactExecutionContext,
    timestamp: str,
    timestamp_dt: datetime,
    fault_injector: FaultInjector | None,
    receipt: dict[str, Any],
) -> tuple[str, str]:
    claim = plan.proposal.claim
    if claim is None:
        raise FactExecutionError("claim is required for successor creation")
    memory_id = context.new_memory_id or _stable_id("factmem", plan.action_id)
    claim_id = context.new_claim_id or _stable_id("claim", plan.action_id)
    content = context.memory_content.strip() or (
        f"{claim.subject} {claim.predicate}: {claim.display_value}"
    )
    metadata = dict(context.metadata)
    metadata.update(
        {
            "memory_type": "fact",
            "fact_key": claim.fact_key,
            "fact_claim_id": claim_id,
            "evolution_action_id": plan.action_id,
            "evolution_action": plan.proposal.action.value,
            "lifecycle": "active",
        }
    )
    safe_metadata, _ = sanitize_structured_value(metadata)
    metadata = safe_metadata if isinstance(safe_metadata, dict) else {"memory_type": "fact"}
    stored_id, _summary, _updated_at, inserted = store_row(
        conn,
        memory_id=memory_id,
        scope_id=context.scope_id,
        platform=context.platform,
        user_id=context.user_id,
        chat_id=context.chat_id,
        thread_id=context.thread_id,
        gateway_session_key=context.gateway_session_key,
        agent_identity=context.agent_identity,
        agent_workspace=context.agent_workspace,
        session_id=context.session_id,
        source=context.source,
        target=context.target,
        content=content,
        metadata=_canonical_json(metadata),
        allow_duplicate=True,
        commit=False,
        timestamp=timestamp,
        enqueue_vector_intent=False,
    )
    if not inserted or stored_id != memory_id:
        raise FactExecutionConflictError("successor memory insert did not create the expected row")
    receipt["memory_ids"].append(memory_id)
    _fault(fault_injector, "after_memory_insert")

    source_ref = plan.proposal.evidence_refs[0].source_id if plan.proposal.evidence_refs else ""
    inserted_claim = insert_claim(
        conn,
        claim_id=claim_id,
        memory_id=memory_id,
        scope_id=context.scope_id,
        subject=claim.subject,
        predicate=claim.predicate,
        value=claim.display_value,
        cardinality=claim.cardinality,
        assertion_kind="direct" if policy.direct_source_count else "validated",
        valid_from=claim.valid_from or None,
        valid_to=claim.valid_to or None,
        recorded_at=timestamp,
        confidence=plan.proposal.confidence,
        source_type=context.source or plan.proposal.source or "fact_evolution",
        source_ref=source_ref,
        metadata={"action_id": plan.action_id},
    )
    receipt["claim_ids"].append(inserted_claim.claim_id)
    _fault(fault_injector, "after_claim_insert")

    for evidence in plan.proposal.evidence_refs:
        linked = link_claim_evidence(
            conn,
            claim_id=claim_id,
            source_type=evidence.source_type,
            source_ref=evidence.source_id,
            excerpt=evidence.quote,
            recorded_at=timestamp,
            metadata={"action_id": plan.action_id},
        )
        receipt["evidence_ids"].append(linked.evidence_id)

    _link_runtime_provenance(
        conn,
        claim_ids=[claim_id],
        context=context,
        action_id=plan.action_id,
        timestamp=timestamp,
        receipt=receipt,
    )

    freshness_id = upsert_memory_freshness(
        conn,
        memory_id=memory_id,
        metadata=metadata,
        content=content,
        now=timestamp_dt,
        commit=False,
    )
    if freshness_id:
        receipt["freshness_ids"].append(freshness_id)

    generation_id = current_generation_id(conn)
    if generation_id:
        event_key = _stable_id(
            "factvec",
            f"{plan.action_id}:{generation_id}:{memory_id}:upsert",
        )
        enqueue_vector_event(
            conn,
            event_key=event_key,
            generation_id=generation_id,
            memory_id=memory_id,
            operation="upsert",
            payload={"updated_at": timestamp, "reason": "fact evolution"},
            timestamp=timestamp,
        )
        _record_vector_outbox_event(
            receipt,
            event_key=event_key,
            generation_id=generation_id,
            memory_id=memory_id,
            operation="upsert",
        )

    event_id = _stable_id("factgov", f"{plan.action_id}:successor")
    record_governance_audit_event(
        conn,
        event_id=event_id,
        event_type="fact_evolution",
        action=plan.proposal.action.value,
        scope_id=context.scope_id,
        target_id=memory_id,
        batch_id=plan.action_id,
        before={},
        after={
            "memory_id": memory_id,
            "claim_id": claim_id,
            "fact_key": claim.fact_key,
        },
        reason=plan.proposal.reason,
        actor=context.actor,
        dry_run=False,
        created_at=timestamp,
    )
    receipt["audit_event_ids"].append(event_id)
    return memory_id, claim_id


def _link_enrichment(
    conn: sqlite3.Connection,
    *,
    plan: EvolutionPlan,
    context: FactExecutionContext,
    target_claim_ids: dict[str, list[str]],
    timestamp: str,
    receipt: dict[str, Any],
) -> None:
    for memory_id, claim_ids in target_claim_ids.items():
        for claim_id in claim_ids:
            for evidence in plan.proposal.evidence_refs:
                linked = link_claim_evidence(
                    conn,
                    claim_id=claim_id,
                    source_type=evidence.source_type,
                    source_ref=evidence.source_id,
                    excerpt=evidence.quote,
                    recorded_at=timestamp,
                    metadata={"action_id": plan.action_id},
                )
                receipt["evidence_ids"].append(linked.evidence_id)
        _link_runtime_provenance(
            conn,
            claim_ids=claim_ids,
            context=context,
            action_id=plan.action_id,
            timestamp=timestamp,
            receipt=receipt,
        )
        event_id = _stable_id("factgov", f"{plan.action_id}:enrich:{memory_id}")
        record_governance_audit_event(
            conn,
            event_id=event_id,
            event_type="fact_evolution",
            action="enrich",
            scope_id=context.scope_id,
            target_id=memory_id,
            batch_id=plan.action_id,
            before={},
            after={"claim_ids": claim_ids, "evidence_linked": len(plan.proposal.evidence_refs)},
            reason=plan.proposal.reason,
            actor=context.actor,
            created_at=timestamp,
        )
        receipt["audit_event_ids"].append(event_id)
        receipt["memory_ids"].append(memory_id)
        receipt["claim_ids"].extend(claim_ids)


def _store_receipt(
    conn: sqlite3.Connection,
    *,
    plan: EvolutionPlan,
    policy: EvolutionPolicyDecision,
    context: FactExecutionContext,
    request_hash: str,
    receipt: Mapping[str, Any],
    timestamp: str,
) -> None:
    conn.execute(
        """
        INSERT INTO fact_action_receipts(
            action_id, idempotency_key, request_hash, scope_id,
            requested_action, effective_action, status, applied,
            policy_json, receipt_json, error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'applied', 1, ?, ?, '', ?, ?)
        """,
        (
            plan.action_id,
            plan.idempotency_key,
            request_hash,
            context.scope_id,
            plan.proposal.action.value,
            policy.effective_action.value,
            _canonical_json(policy.as_dict()),
            _canonical_json(dict(receipt)),
            timestamp,
            timestamp,
        ),
    )


def execute_fact_plan(
    conn: sqlite3.Connection,
    plan: EvolutionPlan,
    policy: EvolutionPolicyDecision,
    context: FactExecutionContext,
    *,
    fault_injector: FaultInjector | None = None,
) -> EvolutionResult:
    """Preview or atomically apply one policy-authorized evolution plan."""

    timestamp, timestamp_dt, effective_at = _validate_static_boundary(
        plan,
        policy,
        context,
    )
    if plan.policy_mode == "preview":
        return EvolutionResult(
            action_id=plan.action_id,
            action=policy.effective_action,
            status="preview",
            applied=False,
            receipt={
                "requested_action": plan.proposal.action.value,
                "effective_action": policy.effective_action.value,
                "reason_codes": list(policy.reason_codes),
                "would_write": bool(policy.allowed and plan.proposal.action is not EvolutionAction.NOOP),
            },
        )
    if not policy.allowed or policy.effective_action is EvolutionAction.REVIEW:
        return _review_result(
            plan,
            policy,
            status="review",
            reason="policy requires review",
        )
    if plan.policy_mode not in {"reviewed_apply", "auto_apply"}:
        return _review_result(
            plan,
            policy,
            status="blocked",
            reason="unsupported apply mode",
        )
    if plan.policy_mode == "auto_apply" and policy.risk_tier == "high":
        return _review_result(
            plan,
            policy,
            status="review",
            reason="high-risk actions require reviewed_apply",
        )
    if plan.proposal.action is EvolutionAction.NOOP:
        return EvolutionResult(
            action_id=plan.action_id,
            action=EvolutionAction.NOOP,
            status="noop",
            applied=False,
            receipt={"reason_codes": list(policy.reason_codes)},
        )

    request_hash = _request_hash(plan, policy, context)
    started_outer_transaction = not conn.in_transaction
    if started_outer_transaction:
        conn.execute("BEGIN IMMEDIATE")
    savepoint = f"fact_exec_{hashlib.sha256(plan.action_id.encode('utf-8')).hexdigest()[:16]}"
    conn.execute(f"SAVEPOINT {savepoint}")
    savepoint_active = True
    try:
        replay = _load_replay(conn, plan=plan, request_hash=request_hash)
        if replay is not None:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            savepoint_active = False
            if started_outer_transaction:
                conn.commit()
            return replay

        target_rows = _target_rows(conn, plan, context)
        target_claim_ids = _target_claim_ids(conn, plan, context) if target_rows else {}
        _fault(fault_injector, "after_preconditions")
        receipt: dict[str, Any] = {
            "action_id": plan.action_id,
            "idempotency_key": plan.idempotency_key,
            "requested_action": plan.proposal.action.value,
            "effective_action": policy.effective_action.value,
            "expected_versions": dict(plan.expected_versions),
            "effective_at": effective_at or None,
            "memory_ids": [],
            "claim_ids": [],
            "evidence_ids": [],
            "freshness_ids": [],
            "relation_count": 0,
            "audit_event_ids": [],
            "vector_outbox_keys": [],
            "vector_outbox_events": [],
            "replayed": False,
        }

        action = plan.proposal.action
        if action is EvolutionAction.SUPERSEDE:
            claim = plan.proposal.claim
            if claim is None or (claim.cardinality == "single" and not claim.valid_from):
                raise FactExecutionConflictError(
                    "single-value supersede requires an explicit valid_from boundary"
                )
            successor_claim_id = context.new_claim_id or _stable_id("claim", plan.action_id)
            for memory_id, claim_ids in target_claim_ids.items():
                for claim_id in claim_ids:
                    close_claim_interval(
                        conn,
                        claim_id=claim_id,
                        retired_at=timestamp,
                        valid_to=claim.valid_from or None,
                        status="superseded",
                        superseded_by_claim_id=successor_claim_id,
                    )
                transition = transition_memory_lifecycle(
                    conn,
                    memory_id=memory_id,
                    lifecycle="superseded",
                    metadata_updates={
                        "superseded_by": context.new_memory_id
                        or _stable_id("factmem", plan.action_id),
                        "evolution_action_id": plan.action_id,
                    },
                    expected_updated_at=str(plan.expected_versions[memory_id]),
                    expected_lifecycle=_target_lifecycle(target_rows[memory_id]),
                    actor=context.actor,
                    reason=plan.proposal.reason,
                    event_type="fact_evolution",
                    action="supersede_old",
                    batch_id=plan.action_id,
                    timestamp=timestamp,
                    fact_mutation_authority=FACT_EXECUTOR_MUTATION_AUTHORITY,
                )
                receipt["audit_event_ids"].append(str(transition["event_id"]))
                _record_vector_outbox_event(
                    receipt,
                    event_key=str(transition.get("vector_outbox_key") or ""),
                    generation_id=str(transition.get("generation_id") or ""),
                    memory_id=memory_id,
                    operation=str(transition.get("vector_operation") or "delete"),
                )
                receipt["memory_ids"].append(memory_id)
                receipt["claim_ids"].extend(claim_ids)
            _fault(fault_injector, "after_claim_close")
            new_memory_id, _new_claim_id = _new_memory_and_claim(
                conn,
                plan=plan,
                policy=policy,
                context=context,
                timestamp=timestamp,
                timestamp_dt=timestamp_dt,
                fault_injector=fault_injector,
                receipt=receipt,
            )
            for old_memory_id in plan.proposal.target_ids:
                if upsert_relation(
                    conn,
                    source_memory_id=new_memory_id,
                    target_memory_id=old_memory_id,
                    relation_type="supersedes",
                    confidence=plan.proposal.confidence,
                    note=f"fact evolution action {plan.action_id}",
                    created_at=timestamp,
                ):
                    receipt["relation_count"] += 1

        elif action is EvolutionAction.ADD:
            _new_memory_and_claim(
                conn,
                plan=plan,
                policy=policy,
                context=context,
                timestamp=timestamp,
                timestamp_dt=timestamp_dt,
                fault_injector=fault_injector,
                receipt=receipt,
            )

        elif action is EvolutionAction.ENRICH:
            _link_enrichment(
                conn,
                plan=plan,
                context=context,
                target_claim_ids=target_claim_ids,
                timestamp=timestamp,
                receipt=receipt,
            )

        elif action is EvolutionAction.RETRACT:
            for memory_id, claim_ids in target_claim_ids.items():
                _link_runtime_provenance(
                    conn,
                    claim_ids=claim_ids,
                    context=context,
                    action_id=plan.action_id,
                    timestamp=timestamp,
                    receipt=receipt,
                )
                for claim_id in claim_ids:
                    retract_claim(
                        conn,
                        claim_id=claim_id,
                        retired_at=timestamp,
                        valid_to=effective_at,
                    )
                transition = transition_memory_lifecycle(
                    conn,
                    memory_id=memory_id,
                    lifecycle="obsolete",
                    metadata_updates={"evolution_action_id": plan.action_id},
                    expected_updated_at=str(plan.expected_versions[memory_id]),
                    expected_lifecycle=_target_lifecycle(target_rows[memory_id]),
                    actor=context.actor,
                    reason=plan.proposal.reason,
                    event_type="fact_evolution",
                    action="retract",
                    batch_id=plan.action_id,
                    timestamp=timestamp,
                    fact_mutation_authority=FACT_EXECUTOR_MUTATION_AUTHORITY,
                )
                receipt["audit_event_ids"].append(str(transition["event_id"]))
                _record_vector_outbox_event(
                    receipt,
                    event_key=str(transition.get("vector_outbox_key") or ""),
                    generation_id=str(transition.get("generation_id") or ""),
                    memory_id=memory_id,
                    operation=str(transition.get("vector_operation") or "delete"),
                )
                receipt["memory_ids"].append(memory_id)
                receipt["claim_ids"].extend(claim_ids)
        else:  # REVIEW and NOOP returned before entering the transaction.
            raise FactExecutionError(f"unsupported executable action: {action.value}")

        _fault(fault_injector, "after_companions")
        receipt["memory_ids"] = list(dict.fromkeys(receipt["memory_ids"]))
        receipt["claim_ids"] = list(dict.fromkeys(receipt["claim_ids"]))
        receipt["evidence_ids"] = list(dict.fromkeys(receipt["evidence_ids"]))
        receipt["audit_event_ids"] = list(dict.fromkeys(receipt["audit_event_ids"]))
        _fault(fault_injector, "before_receipt")
        _store_receipt(
            conn,
            plan=plan,
            policy=policy,
            context=context,
            request_hash=request_hash,
            receipt=receipt,
            timestamp=timestamp,
        )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        savepoint_active = False
        status = "applied_pending_outer_commit"
        if started_outer_transaction:
            conn.commit()
            status = "applied"
        return EvolutionResult(
            action_id=plan.action_id,
            action=policy.effective_action,
            status=status,
            applied=True,
            receipt=receipt,
        )
    except FactExecutionConflictError:
        if savepoint_active and conn.in_transaction:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if started_outer_transaction:
            conn.rollback()
        raise
    except (TemporalConflictError, LifecycleConflictError) as exc:
        if savepoint_active and conn.in_transaction:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if started_outer_transaction:
            conn.rollback()
        raise FactExecutionConflictError(sanitize_report_text(str(exc))[:500]) from exc
    except Exception as exc:
        if savepoint_active and conn.in_transaction:
            try:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            except sqlite3.Error as rollback_exc:
                exc.add_note(f"fact executor savepoint rollback failed: {rollback_exc}")
        if started_outer_transaction:
            conn.rollback()
        raise FactExecutionError(sanitize_report_text(str(exc))[:500]) from exc


__all__ = [
    "FactExecutionConflictError",
    "FactExecutionContext",
    "FactExecutionError",
    "FactProvenanceReference",
    "execute_fact_plan",
]
