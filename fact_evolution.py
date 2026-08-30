"""Shared pipeline adapter from structured candidates to the fact executor.

Digest, journal, and maintenance-tool lanes all bind untrusted proposals to the
same trusted scope, policy mode, CAS versions, and idempotency identity here.
The adapter itself owns no writes; :mod:`fact_executor` remains the sole atomic
mutation coordinator.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
import hashlib
import json
import sqlite3
from typing import Any

from .capture_filters import sanitize_structured_value
from .evolution_policy import evaluate_evolution_policy
from .fact_actions import (
    ClaimDraft,
    EvolutionAction,
    EvolutionPlan,
    EvolutionProposal,
    EvolutionResult,
)
from .fact_authority import (
    is_fact_projection_marker,
    memory_type_is_claim_backed,
    route_fact_authority,
)
from .fact_executor import FactExecutionContext, FactProvenanceReference, execute_fact_plan
from .sql_store import now_iso


_ALLOWED_MODES = frozenset({"preview", "auto_apply", "reviewed_apply"})
_UNATTENDED_LANES = frozenset({"nightly", "journal"})


def memory_type_uses_fact_evolution(value: Any) -> bool:
    """Return whether an explicit canonical type may enter Fact authority."""

    return memory_type_is_claim_backed(value)


def _targets_are_current_fact_projections(
    conn: sqlite3.Connection,
    *,
    target_ids: Sequence[str],
    trusted_scope_id: str,
) -> bool:
    """Prove that every internal projection target owns one current Claim."""

    unique_ids = tuple(
        dict.fromkeys(str(item).strip() for item in target_ids if str(item).strip())
    )
    if not unique_ids:
        return False
    placeholders = ",".join("?" for _ in unique_ids)
    rows = conn.execute(
        f"""
        SELECT memory_id, COUNT(*) AS claim_count
        FROM fact_claims
        WHERE memory_id IN ({placeholders})
          AND scope_id = ?
          AND status = 'current'
          AND retired_at IS NULL
        GROUP BY memory_id
        """,
        (*unique_ids, trusted_scope_id),
    ).fetchall()
    counts = {str(row["memory_id"]): int(row["claim_count"]) for row in rows}
    return all(counts.get(memory_id) == 1 for memory_id in unique_ids)


def _authority_allows_proposal(
    conn: sqlite3.Connection,
    *,
    proposal: EvolutionProposal,
    memory_type: Any,
    trusted_scope_id: str,
) -> tuple[bool, str, dict[str, str]]:
    """Apply the strict type gate, with a ledger-bound legacy anchor escape."""

    route = route_fact_authority(memory_type)
    if route.claim_backed:
        return True, "", route.as_dict()
    if (
        is_fact_projection_marker(memory_type)
        and proposal.action
        in {
            EvolutionAction.ENRICH,
            EvolutionAction.SUPERSEDE,
            EvolutionAction.RETRACT,
        }
        and _targets_are_current_fact_projections(
            conn,
            target_ids=proposal.target_ids,
            trusted_scope_id=trusted_scope_id,
        )
    ):
        return True, "", {
            "canonical_type": "",
            "lane": "claim_backed_existing_projection",
            "reason_code": "existing_claim_authority",
        }
    return False, route.reason_code, route.as_dict()


def fact_evolution_enabled(runtime_config: Mapping[str, Any] | None) -> bool:
    """Return true only for the explicit, strictly boolean feature gate."""

    root = runtime_config if isinstance(runtime_config, Mapping) else {}
    raw = root.get("fact_evolution")
    config = raw if isinstance(raw, Mapping) else {}
    return config.get("enabled") is True


def evolution_policy_mode(
    runtime_config: Mapping[str, Any] | None,
    *,
    lane: str,
    dry_run: bool,
) -> str:
    """Resolve an execution mode with preview as every invalid/default state."""

    if dry_run:
        return "preview"
    root = runtime_config if isinstance(runtime_config, Mapping) else {}
    raw = root.get("fact_evolution")
    config = raw if isinstance(raw, Mapping) else {}
    if not fact_evolution_enabled(runtime_config):
        return "preview"
    mode = str(config.get(f"{lane}_mode") or config.get("mode") or "preview").strip().lower()
    if mode not in _ALLOWED_MODES:
        return "preview"
    # A scheduled lane cannot manufacture the meaning of "reviewed".  Only a
    # maintenance caller may bind reviewed_apply after its own explicit gate.
    if lane in _UNATTENDED_LANES and mode == "reviewed_apply":
        return "preview"
    return mode


def _safe_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _canonical_json(value: Any) -> str:
    safe, _ = sanitize_structured_value(value)
    return json.dumps(
        safe,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _stable_pipeline_id(prefix: str, material: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:40]}"


def pipeline_idempotency_key(
    *,
    lane: str,
    run_id: str,
    source_key: str,
    scope_id: str,
) -> str:
    """Derive a stable source identity independent of scheduler invocation IDs."""

    # ``run_id`` remains in the public adapter signature for audit metadata and
    # compatibility, but it must never determine exactly-once identity.
    _ = run_id
    return _stable_pipeline_id(
        "fact_idem",
        {
            "lane": lane,
            "source_key": source_key,
            "scope_id": scope_id,
        },
    )


def pipeline_receipt_exists(
    conn: sqlite3.Connection,
    *,
    lane: str,
    run_id: str,
    source_key: str,
    scope_id: str,
) -> bool:
    """Return whether a prior source-bound execution receipt exists."""

    key = pipeline_idempotency_key(
        lane=lane,
        run_id=run_id,
        source_key=source_key,
        scope_id=scope_id,
    )
    return (
        conn.execute(
            "SELECT 1 FROM fact_action_receipts WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        is not None
    )


def _proposal_payload(proposal: EvolutionProposal) -> dict[str, Any]:
    return proposal.as_dict()


def _bind_proposal_scope(
    proposal: EvolutionProposal,
    *,
    trusted_scope_id: str,
) -> EvolutionProposal:
    """Replace any model/parser claim scope with the trusted target route."""

    scope_id = str(trusted_scope_id or "").strip()
    if not scope_id or proposal.claim is None or proposal.claim.scope_id == scope_id:
        return proposal
    return replace(
        proposal,
        claim=replace(proposal.claim, scope_id=scope_id),
    )


def _bind_retract_target_claim(
    conn: sqlite3.Connection,
    *,
    proposal: EvolutionProposal,
    trusted_scope_id: str,
) -> EvolutionProposal:
    """Replace caller RETRACT claim hints with one current ledger target claim."""

    if proposal.action is not EvolutionAction.RETRACT:
        return proposal
    if len(proposal.target_ids) != 1:
        return replace(proposal, claim=None)
    rows = conn.execute(
        """
        SELECT subject_key, predicate_key, value, cardinality, valid_from, valid_to
        FROM fact_claims
        WHERE memory_id = ? AND scope_id = ?
          AND status = 'current' AND retired_at IS NULL
        ORDER BY claim_id
        LIMIT 2
        """,
        (proposal.target_ids[0], trusted_scope_id),
    ).fetchall()
    if len(rows) != 1:
        return replace(proposal, claim=None)
    row = rows[0]
    try:
        target_claim = ClaimDraft.from_parts(
            subject=str(row["subject_key"] or ""),
            predicate=str(row["predicate_key"] or ""),
            value=str(row["value"] or ""),
            scope_id=trusted_scope_id,
            cardinality=str(row["cardinality"] or "single"),
            valid_from=str(row["valid_from"] or ""),
            valid_to=str(row["valid_to"] or ""),
        )
    except (TypeError, ValueError):
        return replace(proposal, claim=None)
    return replace(proposal, claim=target_claim)


def _target_snapshot(
    conn: sqlite3.Connection,
    *,
    proposal: EvolutionProposal,
    writable_scope_ids: set[str],
    trusted_scope_id: str,
) -> tuple[set[str], dict[str, str]]:
    """Return CAS-bound targets only from the routed execution scope."""

    if not proposal.target_ids:
        return set(), {}
    placeholders = ",".join("?" for _ in proposal.target_ids)
    rows = conn.execute(
        f"SELECT id, scope_id, updated_at FROM memories WHERE id IN ({placeholders})",
        tuple(proposal.target_ids),
    ).fetchall()
    allowed: set[str] = set()
    versions: dict[str, str] = {}
    for row in rows:
        memory_id = str(row["id"])
        scope_id = str(row["scope_id"] or "")
        if scope_id != trusted_scope_id or scope_id not in writable_scope_ids:
            continue
        allowed.add(memory_id)
        versions[memory_id] = str(row["updated_at"] or "")
    return allowed, versions


def _receipt_expected_versions(
    conn: sqlite3.Connection,
    *,
    idempotency_key: str,
) -> dict[str, str] | None:
    """Reuse the original CAS binding so a completed target action can replay."""

    row = conn.execute(
        "SELECT receipt_json FROM fact_action_receipts WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(str(row["receipt_json"] or "{}"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    raw = payload.get("expected_versions") if isinstance(payload, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    return {
        str(memory_id): str(version)
        for memory_id, version in raw.items()
        if str(memory_id) and str(version)
    }


def execute_pipeline_proposal(
    conn: sqlite3.Connection,
    *,
    proposal: EvolutionProposal,
    lane: str,
    run_id: str,
    source_key: str,
    trusted_scope_id: str,
    writable_scope_ids: Sequence[str],
    actor: str,
    source: str,
    target: str,
    content: str,
    metadata: Mapping[str, Any] | None,
    runtime_config: Mapping[str, Any] | None,
    dry_run: bool,
    effective_at: str = "",
    provenance_refs: Sequence[Mapping[str, Any]] = (),
    session_id: str = "",
    platform: str = "",
    user_id: str = "",
    chat_id: str = "",
    thread_id: str = "",
    gateway_session_key: str = "",
    agent_identity: str = "",
    agent_workspace: str = "",
) -> EvolutionResult:
    """Bind and execute one structured proposal through the common core."""

    writable = {str(item).strip() for item in writable_scope_ids if str(item).strip()}
    execution_scope = str(trusted_scope_id or "").strip()
    proposal = _bind_proposal_scope(
        proposal,
        trusted_scope_id=execution_scope,
    )
    proposal = _bind_retract_target_claim(
        conn,
        proposal=proposal,
        trusted_scope_id=execution_scope,
    )
    identity_material = {
        "lane": lane,
        "source_key": source_key,
        "scope_id": execution_scope,
        "proposal": _proposal_payload(proposal),
    }
    action_id = _stable_pipeline_id("fact_action", identity_material)
    idempotency_key = pipeline_idempotency_key(
        lane=lane,
        run_id=run_id,
        source_key=source_key,
        scope_id=execution_scope,
    )
    raw_metadata = metadata if isinstance(metadata, Mapping) else {}
    authority_allowed, authority_reason, authority_route = _authority_allows_proposal(
        conn,
        proposal=proposal,
        memory_type=raw_metadata.get("memory_type"),
        trusted_scope_id=execution_scope,
    )
    if not authority_allowed:
        return EvolutionResult(
            action_id=action_id,
            action=EvolutionAction.REVIEW,
            status="review",
            applied=False,
            receipt={
                "reason_codes": [
                    "memory_type_not_claim_authoritative",
                    authority_reason,
                ],
                "authority_route": authority_route,
            },
        )
    allowed_targets, expected_versions = _target_snapshot(
        conn,
        proposal=proposal,
        writable_scope_ids=writable,
        trusted_scope_id=execution_scope,
    )
    mode = evolution_policy_mode(runtime_config, lane=lane, dry_run=dry_run)
    replay_versions = _receipt_expected_versions(
        conn,
        idempotency_key=idempotency_key,
    )
    if replay_versions is not None:
        expected_versions = replay_versions
    plan = EvolutionPlan(
        proposal=proposal,
        action_id=action_id,
        idempotency_key=idempotency_key,
        policy_mode=mode,
        expected_versions=expected_versions,
    )
    policy = evaluate_evolution_policy(
        proposal,
        allowed_target_ids=allowed_targets,
        runtime_evidence_authorized=(
            lane != "tool"
            and (lane != "maintenance" or mode == "reviewed_apply")
        ),
    )
    safe_metadata, _ = sanitize_structured_value(dict(raw_metadata))
    trusted_provenance = tuple(
        FactProvenanceReference(
            source_type=str(item.get("source_type") or ""),
            source_ref=str(item.get("source_ref") or ""),
            excerpt=str(item.get("excerpt") or ""),
            metadata=_safe_mapping(item.get("metadata")),
        )
        for item in provenance_refs
        if isinstance(item, Mapping)
    )
    context = FactExecutionContext(
        scope_id=execution_scope,
        writable_scope_ids=tuple(sorted(writable)),
        actor=actor,
        timestamp=now_iso(),
        source=source,
        target=target,
        session_id=session_id,
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        gateway_session_key=gateway_session_key,
        agent_identity=agent_identity,
        agent_workspace=agent_workspace,
        memory_content=content,
        effective_at=str(effective_at or "").strip(),
        metadata=safe_metadata if isinstance(safe_metadata, Mapping) else {},
        provenance_refs=trusted_provenance,
    )
    return execute_fact_plan(conn, plan, policy, context)


__all__ = [
    "evolution_policy_mode",
    "execute_pipeline_proposal",
    "memory_type_uses_fact_evolution",
    "pipeline_idempotency_key",
    "pipeline_receipt_exists",
]
