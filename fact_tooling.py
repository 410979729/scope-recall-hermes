"""Optional structured fact envelopes for public store/update tools.

Legacy tool calls never enter this module.  Structured calls bind caller hints to
provider-owned scopes and configuration before delegating to :mod:`fact_evolution`.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any

from .capture_filters import (
    contains_secret_like_text,
    sanitize_report_text,
    sanitize_structured_value,
)
from .fact_actions import (
    MAX_TARGET_ID_CHARS,
    MAX_TARGET_IDS,
    EvolutionAction,
    EvolutionProposal,
    parse_evolution_proposal,
)
from .fact_evolution import (
    execute_pipeline_proposal,
    memory_type_uses_fact_evolution,
    pipeline_receipt_exists,
)
from .graph import load_metadata
from .lifecycle_policy import PROFILE_HIDDEN_LIFECYCLES
from ._internal.runtime.tool_port import bind_fact_tool_port


class FactToolError(ValueError):
    """A structured tool envelope violates a trusted runtime boundary."""


def _fact_port(provider: Any):
    return bind_fact_tool_port(provider)


MAX_FACT_CONTENT_CHARS = 8_000
MAX_IDEMPOTENCY_KEY_CHARS = 200


def has_structured_fact_hint(args: Mapping[str, Any]) -> bool:
    """Return true only when the caller explicitly supplied a fact envelope."""

    return "claim" in args or "evolution" in args


def _mapping_arg(args: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = args.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise FactToolError(f"{key} must be an object")
    return {str(item_key): item_value for item_key, item_value in value.items()}


def _scope_id_for_mode(provider: Any, scope_mode: str) -> str:
    port = _fact_port(provider)
    scope_id = str(port.scope_id_for_mode(scope_mode) or "")
    if not scope_id:
        raise FactToolError("structured fact scope is unavailable")
    return scope_id


def _writable_scope_ids(provider: Any) -> list[str]:
    port = _fact_port(provider)
    output = [str(item).strip() for item in port.writable_scope_ids() if str(item).strip()]
    if not output:
        raise FactToolError("structured fact writable scopes are unavailable")
    return output


def _canonical_source_key(
    *,
    args: Mapping[str, Any],
    evolution: Mapping[str, Any],
    operation: str,
    target_id: str = "",
) -> str:
    raw_content = str(args.get("content") or "")
    if len(raw_content) > MAX_FACT_CONTENT_CHARS:
        raise FactToolError(
            f"content exceeds {MAX_FACT_CONTENT_CHARS} characters"
        )
    raw_explicit = str(evolution.get("idempotency_key") or "").strip()
    if len(raw_explicit) > MAX_IDEMPOTENCY_KEY_CHARS:
        raise FactToolError(
            f"evolution.idempotency_key exceeds {MAX_IDEMPOTENCY_KEY_CHARS} characters"
        )
    explicit = sanitize_report_text(raw_explicit).strip()
    if explicit:
        if contains_secret_like_text(explicit):
            raise FactToolError("secret-like idempotency key is not allowed")
        return f"{operation}:{target_id}:{explicit}"
    safe, _ = sanitize_structured_value(
        {
            "operation": operation,
            "target_id": target_id,
            "content": args.get("content"),
            "claim": args.get("claim"),
            "evolution": {
                key: value
                for key, value in evolution.items()
                if key not in {"mode", "policy_mode", "reviewed_apply"}
            },
        }
    )
    digest = hashlib.sha256(
        json.dumps(
            safe,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return f"{operation}:{target_id}:{digest}"


def _proposal(
    args: Mapping[str, Any],
    *,
    trusted_scope_id: str,
    default_action: EvolutionAction,
    operation: str,
    allowed_target_ids: list[str] | None = None,
) -> tuple[EvolutionProposal, dict[str, Any], str]:
    claim = _mapping_arg(args, "claim")
    evolution = _mapping_arg(args, "evolution")
    payload = {
        key: value
        for key, value in evolution.items()
        if key not in {"mode", "policy_mode", "reviewed_apply", "idempotency_key"}
    }
    payload.setdefault("action", default_action.value)
    if claim:
        payload["claim"] = claim
    payload["source"] = f"tool-{operation}"
    if operation == "update" and allowed_target_ids:
        payload.setdefault("target_ids", list(allowed_target_ids))
    proposal = parse_evolution_proposal(
        payload,
        trusted_scope_id=trusted_scope_id,
        allowed_target_ids=allowed_target_ids,
    )
    source_key = _canonical_source_key(
        args=args,
        evolution=evolution,
        operation=operation,
        target_id=allowed_target_ids[0] if allowed_target_ids else "",
    )
    return proposal, evolution, source_key


def _tool_run_id(provider: Any) -> str:
    return f"tool:{str(_fact_port(provider).session_id() or '')}"


def _runtime_provenance(provider: Any, *, operation: str) -> list[dict[str, Any]]:
    session_id = str(_fact_port(provider).session_id() or "").strip()
    source_ref = session_id or f"tool-{operation}-session"
    return [
        {
            "source_type": "tool_session",
            "source_ref": source_ref,
            "metadata": {"operation": operation},
        }
    ]


def _execute(
    provider: Any,
    *,
    args: Mapping[str, Any],
    proposal: EvolutionProposal,
    source_key: str,
    scope_id: str,
    target: str,
    content: str,
    metadata: Mapping[str, Any],
    operation: str,
    dry_run: bool = False,
    lane: str = "tool",
):
    port = _fact_port(provider)
    scope = port.scope_object()
    with port.query_lock():
        return execute_pipeline_proposal(
            port.query_connection(),
            proposal=proposal,
            lane=lane,
            run_id=_tool_run_id(port),
            source_key=source_key,
            trusted_scope_id=scope_id,
            writable_scope_ids=_writable_scope_ids(port),
            actor=f"scope-recall-tool-{operation}",
            source=f"tool-{operation}",
            target=target,
            content=content,
            metadata=metadata,
            runtime_config=port.config_view(),
            dry_run=dry_run,
            provenance_refs=_runtime_provenance(port, operation=operation),
            session_id=str(port.session_id() or ""),
            platform=str(getattr(scope, "platform", "") or ""),
            user_id=str(getattr(scope, "user_id", "") or ""),
            chat_id=str(getattr(scope, "chat_id", "") or ""),
            thread_id=str(getattr(scope, "thread_id", "") or ""),
            gateway_session_key=str(
                getattr(scope, "gateway_session_key", "") or ""
            ),
            agent_identity=str(getattr(scope, "agent_identity", "") or ""),
            agent_workspace=str(getattr(scope, "agent_workspace", "") or ""),
        )


def _receipt_memory_ids(result: Any) -> list[str]:
    receipt = result.receipt if isinstance(result.receipt, Mapping) else {}
    value = receipt.get("memory_ids")
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def execute_structured_store(
    provider: Any,
    *,
    args: Mapping[str, Any],
    content: str,
    target: str,
    scope_mode: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Execute an explicit factual store envelope without changing legacy calls."""

    memory_type = str(args.get("memory_type") or "factual")
    if not memory_type_uses_fact_evolution(memory_type):
        raise FactToolError("structured evolution requires a factual memory_type")
    scope_id = _scope_id_for_mode(provider, scope_mode)
    proposal, _evolution, source_key = _proposal(
        args,
        trusted_scope_id=scope_id,
        default_action=EvolutionAction.ADD,
        operation="store",
    )
    if proposal.action not in {
        EvolutionAction.ADD,
        EvolutionAction.NOOP,
        EvolutionAction.REVIEW,
    }:
        raise FactToolError("structured store supports add/noop/review; target actions use update")
    safe_metadata = dict(metadata)
    safe_metadata["memory_type"] = memory_type
    safe_metadata["fact_evolution_action"] = proposal.action.value
    result = _execute(
        provider,
        args=args,
        proposal=proposal,
        source_key=source_key,
        scope_id=scope_id,
        target=target,
        content=content,
        metadata=safe_metadata,
        operation="store",
    )
    memory_ids = _receipt_memory_ids(result)
    memory_id = memory_ids[-1] if memory_ids else ""
    evolution_payload = result.as_dict()
    return {
        "stored": bool(result.applied and result.action is EvolutionAction.ADD),
        "duplicate": False,
        "merged": False,
        "skipped": result.status in {"noop", "blocked"},
        "applied": bool(result.applied),
        "id": memory_id,
        "target": target,
        "scope_mode": scope_mode,
        "evolution": evolution_payload,
        "receipt": {
            "action": "fact_evolution_store",
            "provider": "scope-recall",
            "id": memory_id,
            "status": result.status,
            "action_id": result.action_id,
        },
    }


def _row_scope_mode(provider: Any, scope_id: str) -> str:
    port = _fact_port(provider)
    if scope_id == str(port.shared_pool_scope_id() or ""):
        return "shared_pool"
    if scope_id == str(port.shared_scope_id() or ""):
        return "shared"
    return "local"


def _expected_scope_for_target(
    provider: Any,
    *,
    current_scope_mode: str,
    target: str,
    source: str,
) -> tuple[str, str]:
    desired_mode = str(_fact_port(provider).scope_mode_for(target, source))
    if current_scope_mode == "shared_pool" and desired_mode == "shared":
        desired_mode = "shared_pool"
    return desired_mode, _scope_id_for_mode(provider, desired_mode)


def _inherited_update_metadata(existing: Mapping[str, Any], supplied: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(supplied)
    for key in (
        "memory_type",
        "importance",
        "trust",
        "feedback_count",
        "helpful_count",
        "unhelpful_count",
        "entities",
        "tags",
    ):
        if key not in output and key in existing:
            output[key] = existing[key]
    return output


def execute_structured_update(
    provider: Any,
    *,
    args: Mapping[str, Any],
    memory_id: str,
    content: str,
    target: str | None,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply a temporal target action instead of overwriting a factual row."""

    writable = _writable_scope_ids(provider)
    placeholders = ",".join("?" for _ in writable)
    with _fact_port(provider).query_lock():
        row = _fact_port(provider).query_connection().execute(
            f"SELECT id, source, target, scope_id, metadata FROM memories "
            f"WHERE id = ? AND scope_id IN ({placeholders})",
            (memory_id, *writable),
        ).fetchone()
    if row is None:
        raise FactToolError("id not found")
    existing_metadata = load_metadata(row["metadata"])
    lifecycle = str(existing_metadata.get("lifecycle") or "active").strip().lower()
    memory_type = str(
        args.get("memory_type") or existing_metadata.get("memory_type") or ""
    )
    if not memory_type_uses_fact_evolution(memory_type):
        raise FactToolError("structured evolution requires a factual memory_type")
    current_scope_id = str(row["scope_id"] or "")
    current_scope_mode = _row_scope_mode(provider, current_scope_id)
    actual_target = str(target or row["target"] or "memory")
    desired_mode, desired_scope_id = _expected_scope_for_target(
        provider,
        current_scope_mode=current_scope_mode,
        target=actual_target,
        source=str(row["source"] or ""),
    )
    if desired_scope_id != current_scope_id:
        raise FactToolError(
            "target changes between shared durable and local scratch scopes are not allowed"
        )
    proposal, _evolution, source_key = _proposal(
        args,
        trusted_scope_id=current_scope_id,
        default_action=EvolutionAction.SUPERSEDE,
        operation="update",
        allowed_target_ids=[memory_id],
    )
    if proposal.action is EvolutionAction.ADD:
        raise FactToolError("structured update cannot use add; use supersede or enrich")
    with _fact_port(provider).query_lock():
        is_replay = pipeline_receipt_exists(
            _fact_port(provider).query_connection(),
            lane="tool",
            run_id=_tool_run_id(provider),
            source_key=source_key,
            scope_id=current_scope_id,
        )
    if lifecycle in (PROFILE_HIDDEN_LIFECYCLES - {"scratch"}) and not is_replay:
        raise FactToolError(
            f"memory lifecycle '{lifecycle}' requires explicit restore or review"
        )
    safe_metadata = _inherited_update_metadata(existing_metadata, metadata)
    safe_metadata["memory_type"] = memory_type
    safe_metadata["fact_evolution_action"] = proposal.action.value
    result = _execute(
        provider,
        args=args,
        proposal=proposal,
        source_key=source_key,
        scope_id=current_scope_id,
        target=actual_target,
        content=content,
        metadata=safe_metadata,
        operation="update",
    )
    memory_ids = _receipt_memory_ids(result)
    successor_ids = [item for item in memory_ids if item != memory_id]
    successor_id = successor_ids[-1] if successor_ids else ""
    updated_at = ""
    if successor_id:
        with _fact_port(provider).query_lock():
            successor = _fact_port(provider).query_connection().execute(
                "SELECT updated_at FROM memories WHERE id = ?",
                (successor_id,),
            ).fetchone()
        if successor is not None:
            updated_at = str(successor["updated_at"] or "")
    evolution_payload = result.as_dict()
    return {
        "updated": bool(result.applied),
        "applied": bool(result.applied),
        "id": memory_id,
        "successor_id": successor_id,
        "target": actual_target,
        "scope_mode": desired_mode,
        "summary": content[:240],
        "updated_at": updated_at,
        "evolution": evolution_payload,
        "receipt": {
            "action": "fact_evolution_update",
            "provider": "scope-recall",
            "id": memory_id,
            "successor_id": successor_id,
            "status": result.status,
            "action_id": result.action_id,
        },
    }


def _maintenance_target_binding(
    provider: Any,
    proposal_payload: Mapping[str, Any],
) -> tuple[str, str, list[str]]:
    """Bind a maintenance proposal to one provider-owned writable scope."""

    raw_target_ids = proposal_payload.get("target_ids")
    if raw_target_ids in (None, ""):
        target_ids: list[str] = []
    elif isinstance(raw_target_ids, list):
        if len(raw_target_ids) > MAX_TARGET_IDS:
            raise FactToolError(
                f"proposal.target_ids exceeds {MAX_TARGET_IDS} entries"
            )
        target_ids = list(
            dict.fromkeys(
                str(item).strip() for item in raw_target_ids if str(item).strip()
            )
        )
        if any(len(item) > MAX_TARGET_ID_CHARS for item in target_ids):
            raise FactToolError(
                f"proposal target id exceeds {MAX_TARGET_ID_CHARS} characters"
            )
    else:
        raise FactToolError("proposal.target_ids must be an array")

    if target_ids:
        writable = _writable_scope_ids(provider)
        id_placeholders = ",".join("?" for _ in target_ids)
        scope_placeholders = ",".join("?" for _ in writable)
        with _fact_port(provider).query_lock():
            rows = _fact_port(provider).query_connection().execute(
                f"SELECT id, scope_id, target, source FROM memories "
                f"WHERE id IN ({id_placeholders}) "
                f"AND scope_id IN ({scope_placeholders})",
                (*target_ids, *writable),
            ).fetchall()
        if len(rows) != len(target_ids):
            raise FactToolError("proposal target is not found in writable scopes")
        scopes = {str(row["scope_id"] or "") for row in rows}
        if len(scopes) != 1 or not next(iter(scopes), ""):
            raise FactToolError("proposal targets must share one writable scope")
        scope_id = next(iter(scopes))
        row_targets = {str(row["target"] or "memory") for row in rows}
        supplied_target = str(proposal_payload.get("target") or "").strip().lower()
        target = supplied_target or (next(iter(row_targets)) if len(row_targets) == 1 else "memory")
        current_scope_mode = _row_scope_mode(provider, scope_id)
        expected_scope_ids = {
            _expected_scope_for_target(
                provider,
                current_scope_mode=current_scope_mode,
                target=target,
                source=str(row["source"] or ""),
            )[1]
            for row in rows
        }
        if expected_scope_ids != {scope_id}:
            raise FactToolError(
                "proposal target is outside the canonical target scope"
            )
        return scope_id, target, target_ids

    target = str(proposal_payload.get("target") or "memory").strip().lower()
    if target not in {"user", "memory", "project", "ops"}:
        raise FactToolError("proposal.target must be user, memory, project, or ops")
    scope_mode = str(_fact_port(provider).scope_mode_for(target, "tool-evolve"))
    return _scope_id_for_mode(provider, scope_mode), target, []


def execute_maintenance_evolution(
    provider: Any,
    *,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    """Review or explicitly apply one maintenance-gated fact proposal."""

    proposal_payload = _mapping_arg(args, "proposal")
    if not proposal_payload:
        raise FactToolError("proposal is required")
    raw_dry_run = args.get("dry_run", True)
    if not isinstance(raw_dry_run, bool):
        raise FactToolError("dry_run must be a boolean")
    dry_run = raw_dry_run

    scope_id, target, target_ids = _maintenance_target_binding(
        provider,
        proposal_payload,
    )
    memory_type = str(proposal_payload.get("memory_type") or "factual")
    if not memory_type_uses_fact_evolution(memory_type):
        raise FactToolError("maintenance evolution requires a factual memory_type")

    parser_payload = {
        key: value
        for key, value in proposal_payload.items()
        if key not in {"content", "target", "memory_type", "idempotency_key"}
    }
    parser_payload["source"] = "tool-evolve"
    proposal = parse_evolution_proposal(
        parser_payload,
        trusted_scope_id=scope_id,
        allowed_target_ids=target_ids or None,
    )
    content = _fact_port(provider).clean_text(str(proposal_payload.get("content") or ""))
    if proposal.action in {EvolutionAction.ADD, EvolutionAction.SUPERSEDE} and not content:
        raise FactToolError("proposal.content is required for add or supersede")

    source_key = _canonical_source_key(
        args={
            "content": content,
            "claim": proposal_payload.get("claim"),
        },
        evolution=proposal_payload,
        operation="evolve",
        target_id=target_ids[0] if target_ids else "",
    )
    result = _execute(
        provider,
        args=args,
        proposal=proposal,
        source_key=source_key,
        scope_id=scope_id,
        target=target,
        content=content,
        metadata={
            "memory_type": memory_type,
            "fact_evolution_action": proposal.action.value,
            "maintenance_review": True,
        },
        operation="evolve",
        dry_run=dry_run,
        lane="maintenance",
    )
    payload = result.as_dict()
    payload.update(
        {
            "dry_run": dry_run,
            "target": target,
            "scope_mode": _row_scope_mode(provider, scope_id),
        }
    )
    return payload


__all__ = [
    "FactToolError",
    "execute_maintenance_evolution",
    "execute_structured_store",
    "execute_structured_update",
    "has_structured_fact_hint",
]
