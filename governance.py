from __future__ import annotations

import re
import json
from dataclasses import dataclass
from typing import Any

from .gating import compact_text, dedup_key
from .graph import clamp_float, extract_entities, normalize_entity
from .scoring import semantic_similarity

_SENTENCE_RE = re.compile(r"(?<=[.!?。！？])\s+")
_PREFERENCE_RE = re.compile(
    r"\b(?P<subject>[A-Z][\w-]*|user|joy)\s+(?:prefers?|likes?|wants?|希望|喜欢|喜歡|偏好)\s+(?P<object>[^.!?。！？]+)",
    re.IGNORECASE,
)
_DEPLOY_RE = re.compile(
    r"\b(?:(?:the\s+)?(?:current\s+)?(?:production|prod)\s+)?(?:deploy|deployment|rollout|release)\s+(?:command\s+)?(?:is|uses?|=)\s+(?P<command>[^.!?。！？]+)",
    re.IGNORECASE,
)
_IDENTITY_RE = re.compile(r"\b(?P<subject>[A-Z][\w-]*)\s+is\s+(?P<object>[^.!?。！？]+)", re.IGNORECASE)
_NEGATION_RE = re.compile(r"\b(no longer|not|never|不再|不要|不是|取消|avoid|stop)\b", re.IGNORECASE)
_MEMORY_TYPES = {
    "factual",
    "preference",
    "procedure",
    "workflow",
    "tool_trace",
    "project",
    "summary",
    "pitfall",
    "decision",
    "episodic",
    "resource",
    "constraint",
}


@dataclass
class ExtractionCandidate:
    content: str
    target: str
    category: str
    confidence: float


def split_sentences(text: str) -> list[str]:
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    parts = _SENTENCE_RE.split(cleaned)
    output: list[str] = []
    for part in parts:
        for sub in re.split(r"[\n;；]+", part):
            sub = sub.strip()
            if sub:
                output.append(sub)
    return output


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        clean = str(value or "").strip().lower()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        output.append(clean)
    return output


def _authority_for_source(source: str = "") -> str:
    normalized = str(source or "").strip().lower()
    if normalized == "tool-store" or normalized.startswith("tool"):
        return "agent_tool"
    if normalized == "turn-user":
        return "user_turn"
    if normalized == "turn-assistant":
        return "raw_assistant"
    if normalized == "turn-extracted":
        return "rule_extracted"
    if normalized == "turn-llm-extracted":
        return "llm_extracted"
    if normalized.startswith("legacy"):
        return "legacy_import"
    if normalized == "builtin-curated":
        return "curated_memory"
    return "unknown"


_SOURCE_TRUST_PRIORS = {
    "curated_memory": 0.92,
    "agent_tool": 0.72,
    "user_turn": 0.66,
    "llm_extracted": 0.62,
    "rule_extracted": 0.58,
    "raw_assistant": 0.36,
    "legacy_import": 0.45,
    "unknown": 0.35,
}


def _source_trust_for_authority(authority: str) -> float:
    return _SOURCE_TRUST_PRIORS.get(authority, _SOURCE_TRUST_PRIORS["unknown"])



def normalize_memory_type(value: Any, fallback: str = "factual") -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "fact": "factual",
        "profile": "preference",
        "pref": "preference",
        "ops": "procedure",
        "process": "procedure",
        "scratch": "episodic",
        "observation": "episodic",
        "trace": "tool_trace",
        "lesson": "pitfall",
        "decide": "decision",
        "doc": "resource",
        "document": "resource",
        "rule": "constraint",
        "policy": "constraint",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in _MEMORY_TYPES else fallback


def classify_memory(text: str, target: str = "memory", source: str = "") -> dict[str, Any]:
    lowered = (text or "").lower()
    normalized_target = str(target or "memory").strip().lower()
    category = "general"
    tier = "working"
    kind = "semantic_fact"
    memory_type = "factual"
    lifecycle = "promoted"
    sensitivity = "normal"
    confidence = 0.55
    importance = 0.55
    trust = 0.5
    expires_at = None
    authority = _authority_for_source(source)
    source_trust = _source_trust_for_authority(authority)

    if normalized_target == "general":
        category = "general"
        tier = "working"
        kind = "raw_observation"
        memory_type = "episodic"
        lifecycle = "scratch"
        confidence = 0.5
        importance = 0.35
    elif normalized_target == "user" or any(word in lowered for word in ("prefer", "prefers", "likes", "wants", "希望", "喜欢", "偏好")):
        category = "preference"
        tier = "core"
        kind = "user_preference"
        memory_type = "preference"
        confidence = 0.86
        importance = 0.86
        trust = 0.62
    elif normalized_target == "ops" or any(word in lowered for word in ("deploy", "rollout", "restart", "gateway", "command", "production", "prod")):
        category = "procedure"
        tier = "core"
        kind = "ops_procedure"
        memory_type = "procedure"
        confidence = 0.8
        importance = 0.82
        trust = 0.58
    elif normalized_target == "project":
        category = "project"
        tier = "core"
        kind = "project_fact"
        memory_type = "project"
        confidence = 0.78
        importance = 0.78
        trust = 0.56
    elif normalized_target == "memory":
        category = "fact"
        tier = "core"
        kind = "environment_fact"
        memory_type = "factual"
        confidence = 0.72
        importance = 0.68

    if any(word in lowered for word in ("temporary", "temp", "one-off", "scratch", "临时", "一次性")):
        tier = "working"
        if normalized_target == "general":
            kind = "raw_observation"
            lifecycle = "scratch"
        else:
            kind = "temporary_state"
            lifecycle = "candidate"
            memory_type = "episodic"
        expires_at = "stale-review"
        confidence = min(confidence, 0.62)
        importance = min(importance, 0.45)
    if any(word in lowered for word in ("token", "password", "secret", "api key", "apikey")):
        sensitivity = "sensitive"

    if source_trust >= 0.8:
        trust = max(trust, source_trust)
    else:
        trust = clamp_float((trust * 0.6) + (source_trust * 0.4), default=trust)

    entities = extract_entities(text or "", target=normalized_target)
    entity_tags = [f"entity:{entity}" for entity in entities[:6]]
    tags = _unique_strings(
        [
            f"target:{normalized_target}",
            f"kind:{kind}",
            f"type:{memory_type}",
            f"source:{source or 'unknown'}",
            *entity_tags,
        ]
    )
    scope_mode = "local" if normalized_target == "general" else "shared"
    return {
        "category": category,
        "tier": tier,
        "kind": kind,
        "memory_type": memory_type,
        "lifecycle": lifecycle,
        "authority": authority,
        "source_trust": source_trust,
        "confidence": confidence,
        "importance": importance,
        "trust": trust,
        "sensitivity": sensitivity,
        "expires_at": expires_at,
        "entities": entities,
        "tags": tags,
        "scope_mode": scope_mode,
    }


def merge_metadata(metadata_payload: dict[str, Any], raw_metadata: Any) -> dict[str, Any]:
    """Merge caller metadata without allowing loose callers to weaken policy fields."""

    if not raw_metadata:
        return metadata_payload
    try:
        user_metadata = json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
    except Exception:
        metadata_payload["raw_metadata"] = str(raw_metadata)
        return metadata_payload
    if not isinstance(user_metadata, dict):
        metadata_payload["raw_metadata"] = str(raw_metadata)
        return metadata_payload

    for meta_key, value in user_metadata.items():
        if meta_key == "entities":
            current_value = metadata_payload.get("entities")
            base_values = current_value if isinstance(current_value, list) else []
            incoming_values = value if isinstance(value, list) else [value]
            metadata_payload["entities"] = sorted(
                {
                    normalized
                    for normalized in (normalize_entity(item) for item in [*base_values, *incoming_values])
                    if normalized
                }
            )
        elif meta_key == "tags":
            current_value = metadata_payload.get("tags")
            base_values = current_value if isinstance(current_value, list) else []
            incoming_values = value if isinstance(value, list) else [value]
            metadata_payload["tags"] = _unique_strings(
                [str(item).strip().lower() for item in [*base_values, *incoming_values] if str(item).strip()]
            )
        elif meta_key == "memory_type":
            metadata_payload["memory_type"] = normalize_memory_type(value, str(metadata_payload.get("memory_type") or "factual"))
        elif meta_key in {"importance", "trust"}:
            metadata_payload[meta_key] = clamp_float(value, default=float(metadata_payload.get(meta_key) or 0.5))
        elif meta_key in {"kind", "lifecycle", "authority", "confidence", "sensitivity", "expires_at", "category", "tier", "scope_mode"}:
            continue
        else:
            metadata_payload[meta_key] = value
    return metadata_payload


def extract_candidates(text: str) -> list[ExtractionCandidate]:
    candidates: list[ExtractionCandidate] = []
    for sentence in split_sentences(text):
        stripped = sentence.strip().rstrip(".!?。！？")
        if not stripped:
            continue
        pref = _PREFERENCE_RE.search(stripped)
        if pref:
            subject = pref.group("subject").strip()
            obj = pref.group("object").strip()
            candidates.append(
                ExtractionCandidate(
                    content=compact_text(f"{subject} prefers {obj}.", 360),
                    target="user",
                    category="preference",
                    confidence=0.86,
                )
            )
            continue
        deploy = _DEPLOY_RE.search(stripped)
        if deploy:
            command = deploy.group("command").strip()
            candidates.append(
                ExtractionCandidate(
                    content=compact_text(f"Production deploy command is {command}.", 360),
                    target="ops",
                    category="procedure",
                    confidence=0.82,
                )
            )
            continue
        identity = _IDENTITY_RE.search(stripped)
        if identity and len(stripped.split()) <= 18:
            candidates.append(
                ExtractionCandidate(
                    content=compact_text(stripped + ".", 360),
                    target="project",
                    category="fact",
                    confidence=0.68,
                )
            )
    return candidates


def is_conflicting(existing: str, candidate: str) -> bool:
    if dedup_key(existing) == dedup_key(candidate):
        return False
    if semantic_similarity(existing, candidate) < 0.35:
        return False
    return bool(_NEGATION_RE.search(existing or "")) != bool(_NEGATION_RE.search(candidate or ""))


def merge_memory_text(existing: str, candidate: str) -> str:
    existing = (existing or "").strip()
    candidate = (candidate or "").strip()
    if not existing:
        return candidate
    if not candidate:
        return existing
    if dedup_key(existing) == dedup_key(candidate):
        return existing
    if candidate.lower() in existing.lower():
        return existing
    if existing.lower() in candidate.lower():
        return candidate
    return compact_text(f"{existing.rstrip('.。')} / {candidate.rstrip('.。')}.", 900)
