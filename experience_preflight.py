from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .capture_filters import sanitize_report_text
from .experience_models import RISKY_CAPABILITY_CLASSES
from .experience_store import search_playbooks
from .gating import compact_text


def _experience_config(config: Mapping[str, Any]) -> dict[str, Any]:
    raw = config.get("experience") if isinstance(config, Mapping) else {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _bool_config(config: Mapping[str, Any], key: str, default: bool) -> bool:
    value = config.get(key, default)
    if isinstance(value, str):
        if default:
            return value.strip().lower() not in {"0", "false", "no", "off"}
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _float_config(config: Mapping[str, Any], key: str, default: float) -> float:
    try:
        return float(config.get(key, default))
    except (TypeError, ValueError):
        return default


def _int_config(config: Mapping[str, Any], key: str, default: int) -> int:
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return default


def _query_is_low_signal(query: str, *, min_chars: int) -> bool:
    text = str(query or "").strip()
    if len(text) < min_chars:
        return True
    if re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text):
        cjk_signal_chars = re.findall(r"[\w\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text, flags=re.UNICODE)
        return len(cjk_signal_chars) < min_chars
    meaningful_terms = [term for term in text.replace("-", " ").replace("_", " ").split() if len(term) >= 3]
    return len(meaningful_terms) < 2


def _risky_capabilities(playbook: Mapping[str, Any]) -> list[str]:
    risky: list[str] = []
    for step in playbook.get("steps") or []:
        if not isinstance(step, Mapping):
            continue
        capability = str(step.get("capability_class") or "")
        if capability in RISKY_CAPABILITY_CLASSES and capability not in risky:
            risky.append(capability)
    return risky


def _safe_text(value: Any) -> str:
    return sanitize_report_text(value).strip()


def _policy_bool(policy: Mapping[str, Any], key: str, default: bool | None = None) -> bool | None:
    if key not in policy:
        return default
    value = policy.get(key)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default
    if value is None:
        return default
    return bool(value)


def _all_capabilities(playbook: Mapping[str, Any]) -> list[str]:
    capabilities: list[str] = []
    for step in playbook.get("steps") or []:
        if not isinstance(step, Mapping):
            continue
        capability = str(step.get("capability_class") or "")
        if capability and capability not in capabilities:
            capabilities.append(capability)
    return capabilities


def _policy_sequence(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [str(item) for item in value if str(item)]
    return []


def _no_reuse_result(*, reasons: Sequence[str], selected: Mapping[str, Any] | None = None, results: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "decision": "no_reuse",
        "reasons": list(reasons),
        "requires_live_check": True,
        "packet": "",
        "results": list(results or []),
    }
    if selected is not None:
        payload["playbook"] = dict(selected)
    return payload


def render_experience_packet(playbook: Mapping[str, Any], *, decision: str, reasons: Sequence[str], max_chars: int) -> str:
    lines: list[str] = [
        "## Scope Recall Experience Kernel",
        f"Decision: {decision}",
        f"Playbook: {_safe_text(playbook.get('title', ''))} ({_safe_text(playbook.get('id', ''))})",
        f"Task class: {_safe_text(playbook.get('task_class', ''))}",
        f"Confidence: {float(playbook.get('confidence') or 0.0):.2f}",
    ]
    if reasons:
        lines.append("Reasons: " + ", ".join(str(reason) for reason in reasons))
    lines.extend(["", "Required live checks:"])
    for item in playbook.get("preconditions") or []:
        if isinstance(item, Mapping):
            check = _safe_text(item.get("check") or item.get("id") or "")
            evidence = _safe_text(item.get("evidence_required") or "")
            if check:
                lines.append(f"- {check}" + (f" — evidence: {evidence}" if evidence else ""))
        elif _safe_text(item):
            lines.append(f"- {_safe_text(item)}")
    lines.extend(["", "Steps:"])
    for step in playbook.get("steps") or []:
        if not isinstance(step, Mapping):
            continue
        number = step.get("number")
        capability = _safe_text(step.get("capability_class") or "unknown")
        action = _safe_text(step.get("action") or "")
        evidence = _safe_text(step.get("evidence_required") or "")
        if action:
            lines.append(f"{number}. [{capability}] {action}" + (f" — evidence: {evidence}" if evidence else ""))
    pitfalls = playbook.get("pitfalls") or []
    if pitfalls:
        lines.extend(["", "Pitfalls:"])
        for item in pitfalls[:5]:
            if isinstance(item, Mapping):
                signal = _safe_text(item.get("signal") or "")
                correction = _safe_text(item.get("correction") or item.get("mistake") or "")
                if signal or correction:
                    lines.append(f"- {signal}: {correction}" if signal else f"- {correction}")
            elif _safe_text(item):
                lines.append(f"- {_safe_text(item)}")
    verification = [_safe_text(item) for item in (playbook.get("verification") or []) if _safe_text(item)]
    if verification:
        lines.extend(["", "Verification:"])
        lines.extend(f"- {item}" for item in verification[:8])
    lines.extend(["", "Rule: use this as a scaffold only; live evidence and current user instruction override old experience."])
    return compact_text("\n".join(lines), max_chars)


def experience_preflight(
    conn: Any,
    *,
    query: str,
    accessible_scope_ids: Sequence[str],
    config: Mapping[str, Any] | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    cfg = _experience_config(config or {})
    if not _bool_config(cfg, "enabled", True):
        return {"decision": "no_reuse", "reasons": ["experience_disabled"], "packet": "", "results": []}
    min_query_chars = _int_config(cfg, "min_query_chars", 8)
    if _query_is_low_signal(query, min_chars=min_query_chars):
        return {"decision": "no_reuse", "reasons": ["low_signal"], "packet": "", "results": []}

    results = search_playbooks(conn, query=query, accessible_scope_ids=accessible_scope_ids, limit=limit, status="promoted")
    if not results:
        other_results = search_playbooks(conn, query=query, accessible_scope_ids=accessible_scope_ids, limit=1)
        reason = "no_promoted_playbook" if other_results else "no_matching_playbook"
        return {"decision": "no_reuse", "reasons": [reason], "packet": "", "results": []}

    selected = results[0]
    corrupt_fields = selected.get("payload_corrupt_fields") or []
    if corrupt_fields:
        return _no_reuse_result(reasons=["corrupt_playbook_payload"], selected=selected, results=results)

    reasons: list[str] = []
    no_reuse_reasons: list[str] = []
    decision = "guided_reuse"
    direct_threshold = _float_config(cfg, "direct_reuse_min_confidence", 0.82)
    allow_risky_direct = _bool_config(cfg, "allow_risky_direct_reuse", False)
    risky = _risky_capabilities(selected)
    raw_policy = selected.get("reuse_policy")
    raw_governance = selected.get("skill_governance")
    governance: Mapping[str, Any] = raw_governance if isinstance(raw_governance, Mapping) else {}
    if governance.get("open_conflicts"):
        no_reuse_reasons.append("open_skill_conflict")
    if governance.get("missing_anchors"):
        reasons.append("missing_skill_anchor")
    policy: Mapping[str, Any] = raw_policy if isinstance(raw_policy, Mapping) else {}
    default_decision = str(policy.get("default_decision") or "").strip().lower()
    if default_decision and default_decision not in {"direct_reuse", "guided_reuse", "no_reuse"}:
        no_reuse_reasons.append("invalid_reuse_policy")
    elif default_decision == "no_reuse":
        no_reuse_reasons.append("policy_default_no_reuse")
    elif default_decision == "guided_reuse":
        reasons.append("policy_default_guided_reuse")
    if _policy_bool(policy, "allow_direct_reuse") is False:
        reasons.append("policy_disallows_direct_reuse")
    if _policy_bool(policy, "requires_operator_review") is True:
        reasons.append("policy_requires_operator_review")
    allowed_capabilities = set(_policy_sequence(policy.get("allowed_capability_classes")))
    if allowed_capabilities:
        disallowed = [capability for capability in _all_capabilities(selected) if capability not in allowed_capabilities]
        if disallowed:
            no_reuse_reasons.append("policy_disallows_capability_class")
    try:
        raw_max_staleness = policy.get("max_staleness") if "max_staleness" in policy else None
        max_staleness = int(raw_max_staleness) if raw_max_staleness is not None else None
    except (TypeError, ValueError):
        no_reuse_reasons.append("invalid_reuse_policy")
        max_staleness = None
    if max_staleness is not None and int(selected.get("stale_count") or 0) > max_staleness:
        no_reuse_reasons.append("policy_max_staleness_exceeded")
    if selected.get("environment_constraints"):
        reasons.append("environment_constraints_require_live_check")
    if float(selected.get("confidence") or 0.0) < direct_threshold:
        reasons.append("low_confidence")
    if int(selected.get("stale_count") or 0) > 0:
        reasons.append("stale_history")
    if risky and not allow_risky_direct:
        reasons.append("capability_requires_review")
    if no_reuse_reasons:
        return _no_reuse_result(reasons=no_reuse_reasons + reasons, selected=selected, results=results)
    if not reasons:
        decision = "direct_reuse"
        reasons.append("promoted_confident_match")
    packet = render_experience_packet(selected, decision=decision, reasons=reasons, max_chars=_int_config(cfg, "packet_max_chars", 1400))
    return {
        "decision": decision,
        "reasons": reasons,
        "requires_live_check": True,
        "playbook": selected,
        "packet": packet,
        "results": results,
    }
