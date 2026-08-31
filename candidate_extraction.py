"""Dry-run candidate extraction from event evidence packets.

The extractor is intentionally conservative: it proposes review candidates from
sanitized event evidence, rejects ephemeral release/task progress, and never
writes to durable memory by itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from .capture_filters import classify_transport_noise
from .event_digest import EvidencePacket

_EPHEMERAL_RELEASE_RE = re.compile(
    r"(?:\bissue\s*#?\d+\b|\bcommit\s+[0-9a-f]{7,40}\b|\b[0-9a-f]{7,40}\b|\b\d+\s+tests?\s+passed\b|\b\d+\s+passed\b|\bPR\s*#?\d+\b|\btag\s+v?\d+\.\d+\.\d+\b)",
    re.IGNORECASE,
)
_LOW_VALUE_RE = re.compile(
    r"(?:\btoday\b|\btemporary\b|\bcurrent task\b|\bthis session\b|\bstatus update\b|临时|当前任务|本轮|今天|状态更新)",
    re.IGNORECASE,
)
_USER_PREFERENCE_RE = re.compile(
    r"(?:\buser\s+prefers?\b|\buser\s+wants?\b|用户(?:偏好|希望|要求|喜欢)|以后|不要再|always|prefer)",
    re.IGNORECASE,
)
_OPS_RE = re.compile(r"(?:systemd|service|port|host|gateway|rollback|backup|部署|服务|端口|回滚|备份)", re.IGNORECASE)
_PROJECT_RE = re.compile(r"(?:project|repo|repository|plugin|package|release gate|项目|仓库|插件)", re.IGNORECASE)


@dataclass(frozen=True)
class ExtractedCandidate:
    target: str
    content: str
    memory_type: str
    confidence: float
    evidence_refs: list[str]
    risk_flags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateExtractionResult:
    ok: bool
    dry_run: bool
    candidates: list[ExtractedCandidate]
    rejection_reasons: list[str]
    packet_metadata: dict[str, Any] = field(default_factory=dict)


def _classify_target_and_type(content: str) -> tuple[str, str, float] | None:
    if _USER_PREFERENCE_RE.search(content):
        return "user", "preference", 0.78
    if _OPS_RE.search(content):
        return "ops", "workflow", 0.68
    if _PROJECT_RE.search(content):
        return "project", "factual", 0.66
    return None


def _risk_flags(content: str) -> list[str]:
    flags: list[str] = []
    if re.search(r"\b(?:password|token|secret|api[_ -]?key|credential)\b", content, re.IGNORECASE):
        flags.append("secret_keyword")
    if re.search(r"\b(?:delete|drop|wipe|hard[- ]?delete|生产|production)\b", content, re.IGNORECASE):
        flags.append("high_impact_ops")
    return flags


def extract_candidates_from_packet(packet: EvidencePacket, *, dry_run: bool = True) -> CandidateExtractionResult:
    """Return candidate proposals from one evidence packet without mutating storage."""
    rejection_reasons = list(packet.rejection_reasons)
    if not packet.ok:
        return CandidateExtractionResult(
            ok=False,
            dry_run=dry_run,
            candidates=[],
            rejection_reasons=rejection_reasons,
            packet_metadata=dict(packet.metadata or {}),
        )
    content = packet.content.strip()
    transport = classify_transport_noise(content)
    rejection_reasons.extend(
        f"transport_noise:{code}" for code in transport.reason_codes
    )
    if _EPHEMERAL_RELEASE_RE.search(content):
        rejection_reasons.append("ephemeral_release_state")
    if _LOW_VALUE_RE.search(content) and not _USER_PREFERENCE_RE.search(content):
        rejection_reasons.append("low_value_progress")
    if rejection_reasons:
        return CandidateExtractionResult(
            ok=True,
            dry_run=dry_run,
            candidates=[],
            rejection_reasons=sorted(set(rejection_reasons)),
            packet_metadata=dict(packet.metadata or {}),
        )
    classification = _classify_target_and_type(content)
    if classification is None:
        return CandidateExtractionResult(
            ok=True,
            dry_run=dry_run,
            candidates=[],
            rejection_reasons=["unclassified_event_candidate"],
            packet_metadata=dict(packet.metadata or {}),
        )
    target, memory_type, confidence = classification
    flags = _risk_flags(content)
    candidate = ExtractedCandidate(
        target=target,
        content=content,
        memory_type=memory_type,
        confidence=confidence,
        evidence_refs=list(packet.evidence_refs),
        risk_flags=flags,
        metadata={
            "source": "event_digest",
            "event_kind": packet.metadata.get("event_kind"),
            "scope_id": packet.metadata.get("scope_id"),
            "dry_run": dry_run,
        },
    )
    return CandidateExtractionResult(
        ok=True,
        dry_run=dry_run,
        candidates=[candidate],
        rejection_reasons=[],
        packet_metadata=dict(packet.metadata or {}),
    )
