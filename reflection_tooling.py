"""Runtime orchestration for bounded reflection and reviewed mental-model candidates.

The public reflection path is read-only unless all three write gates are open:
``propose_memory=true``, maintenance tools enabled, and
``reflection.write_candidates=true``. Even then, synthesis is stored only as a
hidden ``needs_review`` candidate after deterministic evidence thresholds pass.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, cast

from .capture_filters import should_capture_text
from .gating import config_bool
from .nightly_llm import resolve_llm_config
from .reflection import (
    ReflectionBudget,
    ReflectionEvidence,
    build_reflection_evidence_pack,
    merge_reflection_evidence_packs,
)
from .reflection_llm import (
    ReflectionSynthesis,
    ReflectionTransport,
    synthesize_reflection,
)
from .reflection_grounding import grounded_candidate_synthesis
from .sql_store import record_governance_audit_event, store_row


class ReflectionToolError(ValueError):
    """Fail-closed validation error safe for the public tool boundary."""


def _reflection_config(provider: Any) -> dict[str, Any]:
    raw = getattr(provider, "_config", {}).get("reflection")
    return dict(raw) if isinstance(raw, dict) else {}


def _strict_int(
    value: Any,
    *,
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    candidate = default if value is None else value
    if type(candidate) is not int:
        raise ReflectionToolError(f"{name} must be an integer")
    if candidate < minimum or candidate > maximum:
        raise ReflectionToolError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return candidate


def _strict_float(
    value: Any,
    *,
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    candidate = default if value is None else value
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
        raise ReflectionToolError(f"{name} must be numeric")
    output = float(candidate)
    if output < minimum or output > maximum:
        raise ReflectionToolError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return output


def _strict_bool(args: dict[str, Any], key: str, default: bool = False) -> bool:
    value = args.get(key, default)
    if type(value) is not bool:
        raise ReflectionToolError(f"{key} must be a boolean")
    return value


def _configured_budget(config: dict[str, Any], args: dict[str, Any]) -> ReflectionBudget:
    configured = ReflectionBudget(
        max_evidence=_strict_int(
            config.get("max_evidence"),
            name="reflection.max_evidence",
            default=24,
            minimum=1,
            maximum=64,
        ),
        max_chars=_strict_int(
            config.get("max_chars"),
            name="reflection.max_chars",
            default=12_000,
            minimum=128,
            maximum=50_000,
        ),
        max_item_chars=_strict_int(
            config.get("max_item_chars"),
            name="reflection.max_item_chars",
            default=2_000,
            minimum=40,
            maximum=4_000,
        ),
        recall_limit=_strict_int(
            config.get("recall_limit"),
            name="reflection.recall_limit",
            default=24,
            minimum=1,
            maximum=100,
        ),
        fact_limit=_strict_int(
            config.get("fact_limit"),
            name="reflection.fact_limit",
            default=24,
            minimum=1,
            maximum=100,
        ),
    )
    raw_budget = args.get("budget")
    if raw_budget is None:
        return configured
    if not isinstance(raw_budget, dict):
        raise ReflectionToolError("budget must be an object")
    allowed = {
        "max_evidence",
        "max_chars",
        "max_item_chars",
        "recall_limit",
        "fact_limit",
    }
    unknown = set(raw_budget) - allowed
    if unknown:
        raise ReflectionToolError(
            "budget contains unknown fields: " + ", ".join(sorted(unknown))
        )

    def capped(name: str, configured_value: int, minimum: int) -> int:
        value = _strict_int(
            raw_budget.get(name),
            name=f"budget.{name}",
            default=configured_value,
            minimum=minimum,
            maximum=configured_value,
        )
        return min(value, configured_value)

    return ReflectionBudget(
        max_evidence=capped("max_evidence", configured.max_evidence, 1),
        max_chars=capped("max_chars", configured.max_chars, 128),
        max_item_chars=capped("max_item_chars", configured.max_item_chars, 40),
        recall_limit=capped("recall_limit", configured.recall_limit, 1),
        fact_limit=capped("fact_limit", configured.fact_limit, 1),
    )


def _resolve_transport(
    provider: Any,
    config: dict[str, Any],
) -> Callable[[str], str] | None:
    injected = getattr(provider, "_reflection_transport", None)
    if callable(injected):
        return cast(Callable[[str], str], injected)

    # Reflection does not silently inherit an arbitrary model. Operators must
    # opt into a named provider/model so an enabled tool cannot make unexpected
    # network calls after an unrelated Hermes routing change.
    model = str(config.get("model") or "").strip()
    provider_name = str(config.get("provider") or "").strip()
    if not model or not provider_name:
        return None
    options = SimpleNamespace(
        provider=provider_name,
        model=model,
        base_url=str(config.get("base_url") or ""),
        endpoint=str(config.get("endpoint") or ""),
        append_v1=config.get("append_v1"),
        api_key=str(config.get("api_key") or ""),
        api_key_env=str(config.get("api_key_env") or ""),
        api_mode=str(config.get("api_mode") or ""),
    )
    resolved = resolve_llm_config(
        Path(getattr(provider, "_hermes_home", Path.home() / ".hermes")),
        options,
    )
    if not str(resolved.get("api_key") or ""):
        return None
    return ReflectionTransport(
        model=str(resolved.get("model") or ""),
        base_url=str(resolved.get("base_url") or ""),
        api_key=str(resolved.get("api_key") or ""),
        api_mode=str(resolved.get("api_mode") or "chat_completions"),
        endpoint=str(resolved.get("endpoint") or ""),
        append_v1=bool(resolved.get("append_v1", True)),
        timeout=_strict_float(
            config.get("timeout"),
            name="reflection.timeout",
            default=30.0,
            minimum=1.0,
            maximum=120.0,
        ),
        max_attempts=_strict_int(
            config.get("max_attempts"),
            name="reflection.max_attempts",
            default=1,
            minimum=1,
            maximum=3,
        ),
        retry_delay=_strict_float(
            config.get("retry_delay"),
            name="reflection.retry_delay",
            default=0.0,
            minimum=0.0,
            maximum=10.0,
        ),
    )


def _evidence_source_identity(evidence: ReflectionEvidence) -> str:
    """Return the strongest available provenance root, not a derived row ID."""

    metadata = evidence.metadata if isinstance(evidence.metadata, dict) else {}
    provenance_root = str(metadata.get("provenance_root") or "").strip()
    if provenance_root:
        return f"provenance:{provenance_root}"
    source_ref = str(metadata.get("source_ref") or "").strip()
    source_type = str(metadata.get("source_type") or "").strip()
    if source_ref:
        return f"source:{source_type}:{source_ref}"
    origin_message_id = str(metadata.get("origin_message_id") or "").strip()
    if origin_message_id:
        return f"message:{origin_message_id}"
    digest_session_id = str(metadata.get("digest_session_id") or "").strip()
    if digest_session_id:
        return f"digest-session:{digest_session_id}"
    journal_entry_ids = metadata.get("journal_entry_ids")
    if isinstance(journal_entry_ids, list) and journal_entry_ids:
        roots = sorted({str(item).strip() for item in journal_entry_ids if str(item).strip()})
        if roots:
            return "journal-entries:" + hashlib.sha256(
                json.dumps(roots, separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:24]
    journal_session_ids = metadata.get("journal_session_ids")
    if isinstance(journal_session_ids, list) and journal_session_ids:
        roots = sorted({str(item).strip() for item in journal_session_ids if str(item).strip()})
        if roots:
            return "journal-sessions:" + hashlib.sha256(
                json.dumps(roots, separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:24]
    return f"memory:{evidence.memory_id or evidence.evidence_id}"


def _candidate_quality(
    synthesis: ReflectionSynthesis,
    evidence: tuple[ReflectionEvidence, ...],
    config: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    evidence_by_id = {item.evidence_id: item for item in evidence}
    cited = [evidence_by_id[citation] for citation in synthesis.citations]
    citation_count = len(cited)
    source_count = len({_evidence_source_identity(item) for item in cited})
    confidence = min((float(item.score) for item in cited), default=0.0)
    quality = {
        "citation_count": citation_count,
        "source_count": source_count,
        "confidence": round(confidence, 4),
    }
    min_citations = _strict_int(
        config.get("candidate_min_citations"),
        name="reflection.candidate_min_citations",
        default=2,
        minimum=1,
        maximum=16,
    )
    min_sources = _strict_int(
        config.get("candidate_min_sources"),
        name="reflection.candidate_min_sources",
        default=2,
        minimum=1,
        maximum=16,
    )
    min_confidence = _strict_float(
        config.get("candidate_min_confidence"),
        name="reflection.candidate_min_confidence",
        default=0.8,
        minimum=0.0,
        maximum=1.0,
    )
    if citation_count < min_citations:
        return quality, "insufficient_citations"
    if source_count < min_sources:
        return quality, "insufficient_source_diversity"
    if confidence < min_confidence:
        return quality, "insufficient_confidence"
    return quality, ""


def _candidate_id(
    *,
    scope_id: str,
    synthesis: ReflectionSynthesis,
) -> str:
    material = json.dumps(
        {
            "scope_id": scope_id,
            "answer": synthesis.answer,
            "citations": list(synthesis.citations),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "reflection-candidate-" + hashlib.sha256(material.encode()).hexdigest()[:24]


def _candidate_metadata(
    *,
    query: str,
    synthesis: ReflectionSynthesis,
    quality: dict[str, Any],
) -> dict[str, Any]:
    return {
        "lifecycle": "candidate",
        "candidate_status": "needs_review",
        "memory_type": "mental_model",
        "reflection_candidate": True,
        "confidence": quality["confidence"],
        "importance": min(0.8, max(0.2, float(quality["confidence"]))),
        "evidence_refs": list(synthesis.citations),
        "digest_quality": {"recommended_action": "candidate"},
        "reflection": {
            "query": query,
            "observations": [statement.as_dict() for statement in synthesis.observations],
            "inferences": [statement.as_dict() for statement in synthesis.inferences],
            "uncertainties": [statement.as_dict() for statement in synthesis.uncertainties],
            "quality": quality,
        },
    }


def _store_candidate(
    provider: Any,
    *,
    query: str,
    synthesis: ReflectionSynthesis,
    quality: dict[str, Any],
) -> dict[str, Any]:
    scope_id = str(getattr(provider, "_shared_scope_id", "") or "").strip()
    writable = {str(item) for item in getattr(provider, "_writable_scope_ids", [])}
    if not scope_id or scope_id not in writable:
        raise ReflectionToolError("reflection candidate scope is not writable")
    scope = getattr(provider, "_scope", None)
    if scope is None:
        raise ReflectionToolError("reflection runtime scope is unavailable")
    candidate_id = _candidate_id(scope_id=scope_id, synthesis=synthesis)
    conn: sqlite3.Connection = provider._require_conn()
    existing = conn.execute(
        "SELECT id, content, metadata FROM memories WHERE id = ? AND scope_id = ?",
        (candidate_id, scope_id),
    ).fetchone()
    if existing is not None:
        if str(existing["content"]) != synthesis.answer:
            raise ReflectionToolError("reflection candidate id collision")
        return {
            "requested": True,
            "created": False,
            "idempotent": True,
            "id": candidate_id,
            "status": "needs_review",
            "quality": quality,
        }

    metadata = _candidate_metadata(
        query=query,
        synthesis=synthesis,
        quality=quality,
    )
    outer_transaction = conn.in_transaction
    savepoint = "scope_recall_reflection_candidate"
    if outer_transaction:
        conn.execute(f"SAVEPOINT {savepoint}")
    else:
        conn.execute("BEGIN IMMEDIATE")
    try:
        stored_id, summary, updated_at, inserted = store_row(
            conn,
            memory_id=candidate_id,
            scope_id=scope_id,
            platform=str(scope.platform),
            user_id=str(scope.user_id),
            chat_id=str(scope.chat_id),
            thread_id=str(scope.thread_id),
            gateway_session_key=str(scope.gateway_session_key),
            agent_identity=str(scope.agent_identity),
            agent_workspace=str(scope.agent_workspace),
            session_id=str(getattr(provider, "_session_id", "")),
            source="reflection",
            target="memory",
            content=synthesis.answer,
            metadata=json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            allow_duplicate=True,
            commit=False,
        )
        if not inserted or not stored_id:
            raise ReflectionToolError("reflection candidate was not inserted")
        record_governance_audit_event(
            conn,
            event_id=f"{candidate_id}-audit",
            event_type="reflection_candidate",
            action="insert_candidate",
            scope_id=scope_id,
            target_id=candidate_id,
            before={},
            after={
                "id": candidate_id,
                "summary": summary,
                "updated_at": updated_at,
                "lifecycle": "candidate",
                "candidate_status": "needs_review",
                "memory_type": "mental_model",
                "evidence_refs": list(synthesis.citations),
                "quality": quality,
            },
            reason="explicit_reflection_candidate_proposal",
            actor="scope-recall:reflection",
            dry_run=False,
        )
        if outer_transaction:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            conn.commit()
    except Exception:
        if outer_transaction:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            conn.rollback()
        raise
    return {
        "requested": True,
        "created": True,
        "idempotent": False,
        "id": candidate_id,
        "status": "needs_review",
        "quality": quality,
    }


def run_reflection_tool(provider: Any, *, args: dict[str, Any]) -> dict[str, Any]:
    """Run one bounded reflection request and optionally store a review candidate."""
    config = _reflection_config(provider)
    if not config_bool(config, "enabled", False):
        raise ReflectionToolError(
            "scope_recall_reflect requires reflection.enabled=true"
        )
    query_limit = _strict_int(
        getattr(provider, "_config", {}).get("query_char_limit"),
        name="query_char_limit",
        default=1_000,
        minimum=1,
        maximum=20_000,
    )
    query = str(args.get("query") or "").strip()
    if not query:
        raise ReflectionToolError("query is required")
    if len(query) > query_limit:
        raise ReflectionToolError(f"query exceeds {query_limit} characters")
    include_trace = _strict_bool(args, "include_trace", False)
    propose_memory = _strict_bool(args, "propose_memory", False)
    if propose_memory and not config_bool(
        getattr(provider, "_config", {}),
        "maintenance_tools_enabled",
        False,
    ):
        raise ReflectionToolError(
            "propose_memory=true requires maintenance_tools_enabled=true"
        )
    if propose_memory and not config_bool(config, "write_candidates", False):
        raise ReflectionToolError(
            "propose_memory=true requires reflection.write_candidates=true"
        )

    budget = _configured_budget(config, args)
    max_hops = _strict_int(
        config.get("max_hops"),
        name="reflection.max_hops",
        default=1,
        minimum=0,
        maximum=1,
    )
    conn: sqlite3.Connection = provider._require_conn()
    before_changes = conn.total_changes
    pack = build_reflection_evidence_pack(
        provider,
        query=query,
        budget=budget,
    )
    if not pack.evidence:
        return {
            "ok": False,
            "status": "unavailable",
            "reason": "no_evidence",
            "query": query,
            "hops_used": 0,
        }
    transport = _resolve_transport(provider, config)
    if transport is None:
        return {
            "ok": False,
            "status": "unavailable",
            "reason": "llm_unavailable",
            "query": query,
            "hops_used": 0,
        }

    synthesis = synthesize_reflection(pack, transport=transport)
    hops_used = 0
    followed_query = ""
    if max_hops and synthesis.followup_queries:
        requested_followup = synthesis.followup_queries[0]
        if " ".join(requested_followup.split()).casefold() != " ".join(query.split()).casefold():
            followed_query = requested_followup
            supplemental = build_reflection_evidence_pack(
                provider,
                query=followed_query,
                budget=budget,
                query_intent=pack.intent,
            )
            pack = merge_reflection_evidence_packs(
                pack,
                supplemental,
                budget=budget,
            )
            synthesis = synthesize_reflection(pack, transport=transport)
            hops_used = 1

    candidate: dict[str, Any] | None = None
    if propose_memory:
        grounding = grounded_candidate_synthesis(synthesis, pack.evidence)
        grounding_summary = {
            "answer_coverage": grounding.answer.coverage,
            "answer_content_tokens": grounding.answer.content_token_count,
            "answer_unsupported_tokens": grounding.answer.unsupported_token_count,
            "observation_count": grounding.observation_count,
            "supported_observation_count": grounding.supported_observation_count,
        }
        candidate_synthesis = grounding.synthesis
        if grounding.reason or candidate_synthesis is None:
            candidate = {
                "requested": True,
                "created": False,
                "reason": grounding.reason or "unsupported_candidate",
                "grounding": grounding_summary,
            }
        else:
            capture_result = should_capture_text(
                candidate_synthesis.answer,
                getattr(provider, "_config", {}),
            )
            if not capture_result.allowed:
                candidate = {
                    "requested": True,
                    "created": False,
                    "reason": "storage_policy_rejected",
                    "policy_reason": capture_result.reason,
                    "grounding": grounding_summary,
                }
            else:
                quality, failure_reason = _candidate_quality(
                    candidate_synthesis,
                    pack.evidence,
                    config,
                )
                if failure_reason:
                    candidate = {
                        "requested": True,
                        "created": False,
                        "reason": failure_reason,
                        "quality": quality,
                        "grounding": grounding_summary,
                    }
                else:
                    assert quality is not None
                    with provider._lock:
                        candidate = _store_candidate(
                            provider,
                            query=query,
                            synthesis=candidate_synthesis,
                            quality=quality,
                        )
                    candidate["grounding"] = grounding_summary

    write_delta = conn.total_changes - before_changes
    if not propose_memory and write_delta:
        raise ReflectionToolError("read-only reflection changed SQLite state")
    payload: dict[str, Any] = {
        "ok": True,
        "status": "complete",
        "query": query,
        "intent": pack.intent,
        "answer": synthesis.answer,
        "observations": [item.as_dict() for item in synthesis.observations],
        "inferences": [item.as_dict() for item in synthesis.inferences],
        "uncertainties": [item.as_dict() for item in synthesis.uncertainties],
        "citations": list(synthesis.citations),
        "hops_used": hops_used,
        "followup_ignored": bool(synthesis.followup_queries),
    }
    if candidate is not None:
        payload["candidate"] = candidate
    if include_trace:
        payload["trace"] = {
            "retrieval_queries": [query] + ([followed_query] if followed_query else []),
            "evidence_count": len(pack.evidence),
            "evidence_chars": pack.char_count,
            "truncated": bool(pack.trace.get("truncated", False)),
            "write_delta": write_delta,
            "allowed_citations": [item.evidence_id for item in pack.evidence],
        }
    return payload


__all__ = [
    "ReflectionToolError",
    "run_reflection_tool",
]
