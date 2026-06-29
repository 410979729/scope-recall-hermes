from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .gating import compact_text
from .governance import normalize_memory_type
from .graph import normalize_entity
from .journal_store import JournalEntry

__all__ = [
    "JournalDigestCandidate",
    "_DOMAIN_TOPIC_HINTS",
    "_GENERIC_TOPIC_ENTITIES",
    "_classify_target_and_type",
    "_digest_role_summary",
    "_entry_entities",
    "_heuristic_candidate_content",
    "_looks_like_historical_template_noise",
    "_segment_session_entries",
    "_topic_entities",
    "_topic_label",
    "_topic_signature",
    "_topic_tags",
    "_unique",
    "candidate_metadata",
    "heuristic_journal_candidates",
]


@dataclass
class JournalDigestCandidate:
    content: str
    target: str = "memory"
    memory_type: str = "summary"
    importance: float = 0.65
    confidence: float = 0.70
    entities: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    reason: str = ""
    entry_ids: list[int] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)


def _unique(values: list[str], *, limit: int = 16) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        clean = str(value or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        output.append(clean)
        if len(output) >= limit:
            break
    return output


def _entry_entities(entries: list[JournalEntry]) -> list[str]:
    from .graph import extract_entities

    values: list[str] = []
    for entry in entries:
        values.extend(extract_entities(entry.content))
    return _unique([entity for entity in (normalize_entity(value) for value in values) if entity], limit=12)


_GENERIC_TOPIC_ENTITIES = {
    "scope-recall",
    "scope",
    "recall",
    "memory",
    "memories",
    "journal",
    "digest",
    "plugin",
    "assistant",
    "user",
    "记忆",
    "插件",
    "已验证",
    "验证",
    "已确定",
    "确定",
    "任务",
    "主题",
    "同一个",
}


def _topic_entities(entries: list[JournalEntry]) -> list[str]:
    entities = _entry_entities(entries)
    specific = [entity for entity in entities if entity not in _GENERIC_TOPIC_ENTITIES and not entity.startswith("session")]
    return _unique(specific or entities, limit=8)


def _topic_tags(entries: list[JournalEntry]) -> list[str]:
    tags = [f"topic:{entity}" for entity in _topic_entities(entries)[:6]]
    session_tags = [f"session:{session_id}" for session_id in _unique([entry.session_id for entry in entries], limit=4)]
    return _unique([*tags, *session_tags], limit=12)


def _topic_label(entries: list[JournalEntry], fallback: str) -> str:
    topics = _topic_entities(entries)
    if topics:
        return ", ".join(topics[:4])
    return fallback


_DOMAIN_TOPIC_HINTS = {
    "release",
    "gate",
    "ci",
    "wheel",
    "manifest",
    "version",
    "check.release",
    "pytest",
    "rrf",
    "bm25",
    "retrieval",
    "vector",
    "lancedb",
    "tailscale",
    "remote",
    "network",
    "firewall",
    "credential",
    "secret",
    "journal",
    "digest",
    "merge",
    "upsert",
    "scope-recall",
    "发布",
    "版本",
    "召回",
    "向量",
    "远程",
    "客户",
    "授权",
    "网络",
    "防火墙",
    "记忆",
    "日记",
    "合并",
}


def _topic_signature(entries: list[JournalEntry]) -> set[str]:
    text = "\n".join(entry.content for entry in entries).lower()
    signature = {hint for hint in _DOMAIN_TOPIC_HINTS if hint.lower() in text}
    signature.update(_topic_entities(entries)[:8])
    return {item for item in signature if item}


def _segment_session_entries(entries: list[JournalEntry]) -> list[list[JournalEntry]]:
    segments: list[list[JournalEntry]] = []
    current: list[JournalEntry] = []
    current_signature: set[str] = set()
    for entry in entries:
        probe = [entry]
        probe_signature = _topic_signature(probe)
        if entry.role == "user" and current:
            overlap = current_signature & probe_signature
            if current_signature and probe_signature and not overlap:
                segments.append(current)
                current = []
                current_signature = set()
        current.append(entry)
        current_signature |= probe_signature
    if current:
        segments.append(current)
    return segments


def _classify_target_and_type(text: str) -> tuple[str, str, list[str]]:
    lowered = text.lower()
    if any(token in lowered for token in ["prefers", "preference", "joy prefers", "用户偏好", "希望", "偏好"]):
        return "user", "preference", ["preference"]
    if any(token in lowered for token in ["deploy", "restart", "systemctl", "端口", "服务", "重启", "部署", "排障"]):
        return "ops", "workflow", ["ops", "workflow"]
    if any(token in lowered for token in ["scope-recall", "plugin", "插件", "memory", "记忆", "journal", "digest", "merge", "upsert"]):
        return "memory", "decision", ["memory-governance", "journal-digest"]
    return "memory", "summary", ["journal-digest"]


def _looks_like_historical_template_noise(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if lowered.startswith("operations workflow summary from journal digest:") or lowered.startswith("operations workflow summary"):
        return True
    if lowered.startswith("journal digest memory"):
        return True
    return False


def _digest_role_summary(entries: list[JournalEntry], role: str, *, limit: int) -> str:
    chunks = [
        entry.content.strip()
        for entry in entries
        if entry.role == role and entry.content.strip() and not _looks_like_historical_template_noise(entry.content)
    ]
    if not chunks:
        return ""
    return compact_text("；".join(chunks), limit)


def _heuristic_candidate_content(target: str, topic_label: str, entries: list[JournalEntry]) -> str:
    user_summary = _digest_role_summary(entries, "user", limit=300)
    assistant_summary = _digest_role_summary(entries, "assistant", limit=520)
    parts: list[str] = []
    if target == "ops":
        parts.append("可复用运维流程")
    elif target == "memory":
        parts.append("可复用记忆治理决策")
    else:
        parts.append("可复用对话事实摘要")
    if topic_label:
        parts.append(f"主题：{topic_label}")
    if user_summary:
        parts.append(f"用户意图/约束：{user_summary}")
    if assistant_summary:
        parts.append(f"处理/结论：{assistant_summary}")
    return "。".join(parts) + "。"


def heuristic_journal_candidates(entries: list[JournalEntry]) -> list[JournalDigestCandidate]:
    if not entries:
        return []
    # Production-safe fallback: keep related consecutive turns together, but do
    # not let a long Telegram/Hermes session become one global memory bucket.
    groups: dict[str, list[JournalEntry]] = {}
    for entry in entries:
        key = f"session:{entry.session_id or 'unknown'}"
        groups.setdefault(key, []).append(entry)

    candidates: list[JournalDigestCandidate] = []
    for key, session_entries in groups.items():
        for segment_index, group_entries in enumerate(_segment_session_entries(session_entries), start=1):
            digest_entries = [
                entry
                for entry in group_entries
                if entry.role != "tool" and not _looks_like_historical_template_noise(entry.content)
            ]
            if not digest_entries or not any(entry.role == "user" for entry in digest_entries):
                continue
            combined = "\n".join(f"{entry.role}: {entry.content}" for entry in digest_entries)
            target, memory_type, tags = _classify_target_and_type(combined)
            session_ids = _unique([entry.session_id for entry in digest_entries], limit=12)
            entry_ids = [entry.id for entry in digest_entries]
            entities = _entry_entities(digest_entries)
            segment_key = f"{key}:segment:{segment_index}"
            topic_label = _topic_label(digest_entries, segment_key.replace("session:", "session "))
            content = _heuristic_candidate_content(target, topic_label, digest_entries)
            candidates.append(
                JournalDigestCandidate(
                    content=content,
                    target=target,
                    memory_type=memory_type,
                    importance=0.78 if target in {"memory", "ops"} else 0.62,
                    confidence=0.78,
                    entities=entities,
                    tags=_unique([*tags, *_topic_tags(digest_entries), key, segment_key], limit=16),
                    reason="journal digest grouped related consecutive conversation turns",
                    entry_ids=entry_ids,
                    session_ids=session_ids,
                )
            )
    return candidates


def candidate_metadata(candidate: JournalDigestCandidate, run_id: str) -> dict[str, Any]:
    return {
        "memory_type": normalize_memory_type(candidate.memory_type, "summary"),
        "importance": max(0.0, min(1.0, float(candidate.importance))),
        "confidence": max(0.0, min(1.0, float(candidate.confidence))),
        "entities": candidate.entities,
        "tags": _unique([*candidate.tags, "journal-digest"], limit=20),
        "journal_run_id": run_id,
        "journal_entry_ids": candidate.entry_ids[:200],
        "journal_session_ids": candidate.session_ids[:40],
        "journal_reason": candidate.reason,
    }
