"""Read-only inspection of the exact Recall Packet used by production search."""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from ._internal.recall.compiler import (
    CURRENT_TRUTH_STATES,
    STALE_TRUTH_STATES,
    RecallPacket,
)
from ._internal.recall.pipeline import humanize_recall_components
from .models import RecallItem

INSPECTOR_SCHEMA = "scope_recall.recall_inspector.v1"
_TIMELINE_FIELDS = (
    "valid_from",
    "valid_to",
    "recorded_at",
    "temporal_valid_from",
    "temporal_valid_to",
    "temporal_recorded_at",
)


class RecallInspectorPort(Protocol):
    def recall_service_view(self) -> Any: ...


def _safe_float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return round(number, 4)


def _timeline(item: RecallItem, metadata: Mapping[str, object]) -> dict[str, str]:
    timeline = {"updated_at": str(item.updated_at or "")}
    for key in _TIMELINE_FIELDS:
        value = str(metadata.get(key) or "").strip()
        if value:
            timeline[key] = value[:128]
    return timeline


def _confidence(item: RecallItem, metadata: Mapping[str, object]) -> dict[str, float]:
    output: dict[str, float] = {"retrieval_score": round(float(item.score), 4)}
    for public_key, metadata_key in (
        ("trust", "trust"),
        ("importance", "importance"),
        ("claim_confidence", "claim_confidence"),
    ):
        value = _safe_float(metadata.get(metadata_key))
        if value is not None:
            output[public_key] = value
    return output


def _truth_label(truth_state: str) -> str:
    normalized = str(truth_state or "untracked").strip().lower()
    if normalized in CURRENT_TRUTH_STATES:
        return "current"
    if normalized in STALE_TRUTH_STATES:
        return "historical"
    return "untracked"


def _action_plans(memory_id: str) -> dict[str, dict[str, object]]:
    return {
        "correction": {
            "tool": "scope_recall_memory",
            "arguments": {"action": "update", "id": memory_id},
            "effect": "writes only when the operator supplies replacement content and executes it",
        },
        "archive": {
            "tool": "scope_recall_memory",
            "arguments": {"action": "forget", "id": memory_id},
            "effect": "audited soft archive only when the operator executes it",
        },
        "purge_impact": {
            "tool": "scope_recall_purge",
            "arguments": {"action": "plan", "id": memory_id},
            "effect": "zero-write impact plan; deny and erase require separate confirmations",
            "requires_maintenance_tools": True,
        },
    }


def _render_text(payload: Mapping[str, object]) -> str:
    raw_result_count = payload.get("result_count")
    result_count = raw_result_count if isinstance(raw_result_count, int) else 0
    lines = [
        "Scope Recall Inspector (read-only)",
        f"Results: {result_count}",
    ]
    raw_results = payload.get("results")
    results = raw_results if isinstance(raw_results, list) else []
    for index, raw in enumerate(results, start=1):
        item = raw if isinstance(raw, Mapping) else {}
        truth = item.get("truth")
        truth = truth if isinstance(truth, Mapping) else {}
        token_cost = item.get("token_cost")
        token_cost = token_cost if isinstance(token_cost, Mapping) else {}
        lines.append(
            f"{index}. {str(item.get('summary') or '')[:240]} "
            f"[{str(truth.get('classification') or 'untracked')}; "
            f"tokens={int(token_cost.get('estimated_tokens') or 0)}]"
        )
        why_hit = item.get("why_hit")
        if isinstance(why_hit, Mapping):
            reasons = why_hit.get("why_included")
            if isinstance(reasons, list):
                lines.append("   Why: " + "; ".join(str(reason) for reason in reasons[:8]))
    return "\n".join(lines)[:12000]


def inspect_recall(
    port: RecallInspectorPort,
    *,
    query: str,
    limit: int = 5,
    recall_mode: str = "advisory",
    include_content: bool = False,
    output_format: str = "json",
) -> dict[str, object]:
    """Run production recall once and explain its exact active packet.

    This use case deliberately has no connection, SQL, vector, or mutation
    capability. It consumes the public recall service and the packet that the
    production orchestrator placed in the current ContextVar.
    """

    recall = port.recall_service_view()
    if recall is None:
        raise RuntimeError("recall service is unavailable")
    results = recall.search_memories(
        str(query),
        limit=max(1, min(20, int(limit))),
        recall_mode=str(recall_mode),
    )
    packet = getattr(recall, "last_recall_packet", None)
    if not isinstance(packet, RecallPacket):
        raise RuntimeError("production recall packet is unavailable")

    packet_items = {entry.item.id: entry for entry in packet.items}
    inspected: list[dict[str, object]] = []
    for result in results:
        entry = packet_items.get(str(result.id))
        if entry is None:
            raise RuntimeError("production recall result is absent from its active packet")
        metadata = dict(result.metadata or {})
        item_payload: dict[str, object] = {
            "id": str(result.id),
            "summary": str(result.summary),
            "source": str(result.source),
            "target": str(result.target),
            "why_hit": humanize_recall_components(metadata),
            "truth": {
                "state": entry.truth_state,
                "classification": _truth_label(entry.truth_state),
                "conflict": bool(entry.conflict),
            },
            "provenance": {
                "source": str(result.source),
                "evidence_kinds": list(entry.evidence_kinds),
            },
            "token_cost": {"estimated_tokens": int(entry.estimated_tokens)},
            "confidence": _confidence(result, metadata),
            "timeline": _timeline(result, metadata),
            "action_plans": _action_plans(str(result.id)),
        }
        if include_content:
            item_payload["content"] = str(result.content)
        inspected.append(item_payload)

    packet_metrics = dict(packet.aggregate_metrics())
    packet_metrics["candidate_fingerprint"] = packet.candidate_fingerprint
    payload: dict[str, object] = {
        "schema": INSPECTOR_SCHEMA,
        "read_only": True,
        "recall_mode": str(recall_mode),
        "include_content": bool(include_content),
        "result_count": len(inspected),
        "packet": packet_metrics,
        "results": inspected,
    }
    if str(output_format).strip().lower() == "text":
        payload["rendered_text"] = _render_text(payload)
    return payload


__all__ = ["INSPECTOR_SCHEMA", "RecallInspectorPort", "inspect_recall"]
