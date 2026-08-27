"""Read-only historical fact splitting and explicitly approved atomic apply.

Shadow planning never creates Claims, alters prompts, or enters ordinary recall.
Only :func:`apply_split_plan` may cross the write boundary, and it does so after
an explicit plan-bound approval plus source version/content/lifecycle CAS.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
import math
import sqlite3
from typing import Any

from .capture_filters import (
    contains_secret_like_text,
    sanitize_capture_text,
    sanitize_report_text,
)
from .evolution_policy import evaluate_evolution_policy
from .fact_actions import (
    ClaimDraft,
    EvidenceReference,
    EvolutionAction,
    EvolutionPlan,
    EvolutionProposal,
)
from .fact_authority import route_fact_authority
from .fact_executor import (
    FactExecutionContext,
    FactExecutionError,
    FactProvenanceReference,
    execute_fact_plan,
)
from .fact_repository import (
    FACT_EXECUTOR_MUTATION_AUTHORITY,
    FactProjectionInvariantError,
    assert_canonical_projection_pair,
)
from .graph import load_metadata
from .lifecycle_registry import FACT_EVOLUTION_SUPERSEDE
from .lifecycle_service import LifecycleConflictError, transition_memory_lifecycle
from .sql_store import now_iso


MAX_SPLIT_CANDIDATES = 32
MAX_SPLIT_EVIDENCE_SPANS = 256
MAX_PROJECTION_CHARS = 8_000
MAX_EXTRACTOR_POLICY_VERSION_CHARS = 120
_SOURCE_LIFECYCLES = frozenset({"active", "promoted", "candidate"})


class SplitPlanError(ValueError):
    """A shadow plan is invalid, stale, or not explicitly authorized."""


class SplitPlanConflictError(SplitPlanError):
    """The source or durable action state no longer matches the frozen plan."""


class SplitPlanApplyError(SplitPlanError):
    """An approved split failed and its whole transaction was rolled back."""


@dataclass(frozen=True, slots=True)
class SplitCandidateClaim:
    """One candidate Claim proposed by a frozen extractor policy."""

    memory_type: str
    claim: ClaimDraft
    confidence: float


@dataclass(frozen=True, slots=True)
class SplitEvidenceSpan:
    """A content-free pointer into the frozen source memory text."""

    candidate_index: int
    start: int
    end: int
    span_sha256: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_index": self.candidate_index,
            "start": self.start,
            "end": self.end,
            "span_sha256": self.span_sha256,
        }


@dataclass(frozen=True, slots=True)
class SplitPlan:
    """Frozen, non-authoritative result of read-only historical extraction."""

    source_memory_id: str
    source_scope_id: str
    source_target: str
    expected_updated_at: str
    expected_lifecycle: str
    source_content_hash: str
    candidate_claims: tuple[SplitCandidateClaim, ...]
    evidence_spans: tuple[SplitEvidenceSpan, ...]
    projection_texts: tuple[str, ...]
    plan_hash: str
    extractor_policy_version: str

    def as_decontented_artifact(self) -> dict[str, Any]:
        """Return the safe default artifact without claim values or projections."""

        return {
            "artifact_kind": "fact_split_shadow",
            "authority": "none",
            "source_memory_id_hash": _sha256_text(self.source_memory_id),
            "source_scope_id_hash": _sha256_text(self.source_scope_id),
            "source_target": self.source_target,
            "expected_updated_at": self.expected_updated_at,
            "expected_lifecycle": self.expected_lifecycle,
            "source_content_hash": self.source_content_hash,
            "candidate_claims": [
                {
                    "candidate_index": index,
                    "memory_type": item.memory_type,
                    "fact_key": item.claim.fact_key,
                    "claim_sha256": _sha256_json(_candidate_material(item)),
                    "confidence": item.confidence,
                }
                for index, item in enumerate(self.candidate_claims)
            ],
            "evidence_spans": [item.as_dict() for item in self.evidence_spans],
            "projection_text_sha256": [
                _sha256_text(text) for text in self.projection_texts
            ],
            "plan_hash": self.plan_hash,
            "extractor_policy_version": self.extractor_policy_version,
        }

    def as_local_private_artifact(self) -> dict[str, Any]:
        """Return full plan material only for an explicitly private local sink."""

        payload = self.as_decontented_artifact()
        payload["artifact_privacy"] = "local_private"
        payload["source_memory_id"] = self.source_memory_id
        payload["source_scope_id"] = self.source_scope_id
        payload["candidate_claims"] = [
            _candidate_material(item) for item in self.candidate_claims
        ]
        payload["projection_texts"] = list(self.projection_texts)
        return payload


@dataclass(frozen=True, slots=True)
class SplitPlanApproval:
    """Explicit human/policy decision bound to one exact shadow plan."""

    plan_hash: str
    approval_id: str
    approved_by: str
    approved_at: str
    decision: str = "approved"


@dataclass(frozen=True, slots=True)
class SplitApplyResult:
    """Decontented receipt for one atomic split batch."""

    plan_hash: str
    source_memory_id: str
    status: str
    projection_pairs: tuple[Mapping[str, str], ...]
    source_transition_event_id: str
    replayed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_hash": self.plan_hash,
            "source_memory_id": self.source_memory_id,
            "status": self.status,
            "projection_pairs": [dict(item) for item in self.projection_pairs],
            "source_transition_event_id": self.source_transition_event_id,
            "replayed": self.replayed,
        }


SplitFaultInjector = Callable[[str], None]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return _sha256_text(encoded)


def _candidate_material(candidate: SplitCandidateClaim) -> dict[str, Any]:
    return {
        "memory_type": candidate.memory_type,
        "claim": candidate.claim.as_dict(),
        "confidence": candidate.confidence,
    }


def _plan_material(
    *,
    source_memory_id: str,
    source_scope_id: str,
    source_target: str,
    expected_updated_at: str,
    expected_lifecycle: str,
    source_content_hash: str,
    candidate_claims: Sequence[SplitCandidateClaim],
    evidence_spans: Sequence[SplitEvidenceSpan],
    projection_texts: Sequence[str],
    extractor_policy_version: str,
) -> dict[str, Any]:
    return {
        "source_memory_id": source_memory_id,
        "source_scope_id": source_scope_id,
        "source_target": source_target,
        "expected_updated_at": expected_updated_at,
        "expected_lifecycle": expected_lifecycle,
        "source_content_hash": source_content_hash,
        "candidate_claims": [_candidate_material(item) for item in candidate_claims],
        "evidence_spans": [item.as_dict() for item in evidence_spans],
        "projection_texts": list(projection_texts),
        "extractor_policy_version": extractor_policy_version,
    }


def fact_split_shadow_enabled(runtime_config: Mapping[str, Any] | None) -> bool:
    """Return true only for the exact frozen shadow flag."""

    root = runtime_config if isinstance(runtime_config, Mapping) else {}
    raw = root.get("fact_backfill")
    config = raw if isinstance(raw, Mapping) else {}
    return config.get("shadow_enabled") is True


def _validated_scope_ids(
    values: Sequence[str],
    *,
    field_name: str,
) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise SplitPlanError(f"{field_name} must be a sequence of identifiers")
    scopes = frozenset(str(item).strip() for item in values if str(item).strip())
    if not scopes:
        raise SplitPlanError(f"at least one {field_name} is required")
    if len(scopes) > 64 or any(len(item) > 240 for item in scopes):
        raise SplitPlanError(f"{field_name} must contain at most 64 bounded identifiers")
    return scopes


def _validated_policy_version(value: Any) -> str:
    raw = str(value or "").strip()
    if contains_secret_like_text(raw):
        raise SplitPlanError("secret-like extractor policy version is not allowed")
    version = sanitize_report_text(raw).strip()
    if not version or len(version) > MAX_EXTRACTOR_POLICY_VERSION_CHARS:
        raise SplitPlanError("extractor_policy_version is required and bounded")
    if version != raw:
        raise SplitPlanError("extractor_policy_version must be a safe public identifier")
    return version


def _validated_projection_text(value: Any) -> str:
    text = sanitize_capture_text(value).strip()
    if not text or len(text) > MAX_PROJECTION_CHARS:
        raise SplitPlanError("projection text is required and bounded")
    if contains_secret_like_text(text):
        raise SplitPlanError("secret-like projection text is not allowed")
    return text


def _evidence_for_candidate(
    source_content: str,
    *,
    candidate_index: int,
    evidence_spans: Sequence[SplitEvidenceSpan],
    source_memory_id: str,
    subject: str,
) -> tuple[EvidenceReference, ...]:
    output: list[EvidenceReference] = []
    for span in evidence_spans:
        if span.candidate_index != candidate_index:
            continue
        quote = sanitize_capture_text(source_content[span.start : span.end]).strip()
        output.append(
            EvidenceReference(
                source_type="manual_correction",
                source_id=f"split:{source_memory_id}:{candidate_index}:{span.start}:{span.end}",
                quote=quote,
                speaker_subject=subject,
            )
        )
    return tuple(output)


def build_split_plan(
    conn: sqlite3.Connection,
    *,
    source_memory_id: str,
    candidate_claims: Sequence[SplitCandidateClaim],
    evidence_spans: Sequence[SplitEvidenceSpan],
    projection_texts: Sequence[str],
    extractor_policy_version: str,
    runtime_config: Mapping[str, Any] | None,
    readable_scope_ids: Sequence[str],
) -> SplitPlan:
    """Build one content-frozen SplitPlan without performing any write."""

    if not fact_split_shadow_enabled(runtime_config):
        raise SplitPlanError("fact_backfill.shadow_enabled is not enabled")
    memory_id = str(source_memory_id or "").strip()
    if not memory_id or len(memory_id) > 160:
        raise SplitPlanError("source_memory_id is required and bounded")
    scopes = _validated_scope_ids(
        readable_scope_ids,
        field_name="readable scope",
    )
    row = conn.execute(
        """
        SELECT id, scope_id, target, content, updated_at, metadata
        FROM memories WHERE id = ?
        """,
        (memory_id,),
    ).fetchone()
    if row is None:
        raise SplitPlanError("source memory does not exist")
    source_scope_id = str(row["scope_id"] or "")
    if source_scope_id not in scopes:
        raise SplitPlanError("source memory is outside readable scopes")
    source_target = str(row["target"] or "memory").strip().lower()
    if source_target == "general":
        raise SplitPlanError("general scratch context cannot enter Fact authority")
    source_content = str(row["content"] or "")
    source_updated_at = str(row["updated_at"] or "")
    source_metadata = load_metadata(row["metadata"])
    source_lifecycle = str(source_metadata.get("lifecycle") or "active").strip().lower()
    if source_lifecycle not in _SOURCE_LIFECYCLES:
        raise SplitPlanError("source lifecycle is not eligible for historical split")
    if conn.execute(
        "SELECT 1 FROM fact_claims WHERE memory_id = ? LIMIT 1",
        (memory_id,),
    ).fetchone() is not None:
        raise SplitPlanError("source memory is already Fact-owned")

    candidates = tuple(candidate_claims)
    if not 2 <= len(candidates) <= MAX_SPLIT_CANDIDATES:
        raise SplitPlanError("historical split requires between 2 and 32 candidates")
    projections = tuple(_validated_projection_text(item) for item in projection_texts)
    if len(projections) != len(candidates):
        raise SplitPlanError("projection_texts must align one-to-one with candidates")

    spans_input = tuple(evidence_spans)
    if not spans_input or len(spans_input) > MAX_SPLIT_EVIDENCE_SPANS:
        raise SplitPlanError("evidence_spans must be non-empty and bounded")
    normalized_spans: list[SplitEvidenceSpan] = []
    for span in spans_input:
        if type(span.candidate_index) is not int or not 0 <= span.candidate_index < len(candidates):
            raise SplitPlanError("evidence span candidate_index is invalid")
        if type(span.start) is not int or type(span.end) is not int:
            raise SplitPlanError("evidence span bounds must be integers")
        if not 0 <= span.start < span.end <= len(source_content):
            raise SplitPlanError("evidence span is outside source content")
        quote = sanitize_capture_text(source_content[span.start : span.end]).strip()
        if not quote or contains_secret_like_text(quote):
            raise SplitPlanError("evidence span is empty or secret-like")
        digest = _sha256_text(quote)
        if span.span_sha256 and span.span_sha256 != digest:
            raise SplitPlanError("evidence span hash does not match source content")
        normalized_spans.append(replace(span, span_sha256=digest))
    spans = tuple(normalized_spans)

    seen_single_fact_keys: set[str] = set()
    seen_claim_values: set[tuple[str, str]] = set()
    for index, candidate in enumerate(candidates):
        route = route_fact_authority(candidate.memory_type)
        if not route.claim_backed or route.canonical_type != candidate.memory_type:
            raise SplitPlanError("split candidate requires an explicit Claim-backed type")
        if candidate.claim.scope_id != source_scope_id:
            raise SplitPlanError("split candidate scope does not match source scope")
        rebuilt_claim = ClaimDraft.from_parts(
            subject=candidate.claim.subject,
            predicate=candidate.claim.predicate,
            value=candidate.claim.display_value,
            scope_id=candidate.claim.scope_id,
            cardinality=candidate.claim.cardinality,
            valid_from=candidate.claim.valid_from,
            valid_to=candidate.claim.valid_to,
        )
        if rebuilt_claim.as_dict() != candidate.claim.as_dict():
            raise SplitPlanError("split candidate Claim identity is not canonical")
        if candidate.claim.valid_to:
            raise SplitPlanError("finite historical claims remain unresolved in 2.0.x")
        confidence = float(candidate.confidence)
        if not math.isfinite(confidence) or not 0.65 <= confidence <= 1.0:
            raise SplitPlanError("split candidate confidence must be between 0.65 and 1")
        if candidate.claim.cardinality == "single":
            if candidate.claim.fact_key in seen_single_fact_keys:
                raise SplitPlanError("split candidates conflict in one single-valued slot")
            seen_single_fact_keys.add(candidate.claim.fact_key)
        value_identity = (candidate.claim.fact_key, candidate.claim.value_fingerprint)
        if value_identity in seen_claim_values:
            raise SplitPlanError("split candidates contain a duplicate Claim")
        seen_claim_values.add(value_identity)
        evidence = _evidence_for_candidate(
            source_content,
            candidate_index=index,
            evidence_spans=spans,
            source_memory_id=memory_id,
            subject=candidate.claim.subject,
        )
        if not evidence:
            raise SplitPlanError("each split candidate requires an evidence span")
        proposal = EvolutionProposal(
            action=EvolutionAction.ADD,
            raw_action="add",
            claim=candidate.claim,
            evidence_refs=evidence,
            confidence=confidence,
            reason="approved historical split candidate",
            source="fact_split_shadow",
        )
        policy = evaluate_evolution_policy(proposal)
        if not policy.allowed:
            raise SplitPlanError(
                "split candidate evidence is not claim-supporting: "
                + ",".join(policy.reason_codes)
            )

    policy_version = _validated_policy_version(extractor_policy_version)
    content_hash = _sha256_text(source_content)
    material = _plan_material(
        source_memory_id=memory_id,
        source_scope_id=source_scope_id,
        source_target=source_target,
        expected_updated_at=source_updated_at,
        expected_lifecycle=source_lifecycle,
        source_content_hash=content_hash,
        candidate_claims=candidates,
        evidence_spans=spans,
        projection_texts=projections,
        extractor_policy_version=policy_version,
    )
    return SplitPlan(
        source_memory_id=memory_id,
        source_scope_id=source_scope_id,
        source_target=source_target,
        expected_updated_at=source_updated_at,
        expected_lifecycle=source_lifecycle,
        source_content_hash=content_hash,
        candidate_claims=candidates,
        evidence_spans=spans,
        projection_texts=projections,
        plan_hash=_sha256_json(material),
        extractor_policy_version=policy_version,
    )


def _verify_plan_hash(plan: SplitPlan) -> None:
    expected = _sha256_json(
        _plan_material(
            source_memory_id=plan.source_memory_id,
            source_scope_id=plan.source_scope_id,
            source_target=plan.source_target,
            expected_updated_at=plan.expected_updated_at,
            expected_lifecycle=plan.expected_lifecycle,
            source_content_hash=plan.source_content_hash,
            candidate_claims=plan.candidate_claims,
            evidence_spans=plan.evidence_spans,
            projection_texts=plan.projection_texts,
            extractor_policy_version=plan.extractor_policy_version,
        )
    )
    if expected != plan.plan_hash:
        raise SplitPlanConflictError("SplitPlan hash does not match its frozen material")


def _validated_approval(
    plan: SplitPlan,
    approval: SplitPlanApproval,
) -> tuple[str, str]:
    if approval.decision != "approved" or approval.plan_hash != plan.plan_hash:
        raise SplitPlanError("explicit approval is not bound to this SplitPlan")
    raw_approval_id = str(approval.approval_id or "").strip()
    raw_approved_by = str(approval.approved_by or "").strip()
    if contains_secret_like_text(raw_approval_id) or contains_secret_like_text(
        raw_approved_by
    ):
        raise SplitPlanError("secret-like approval identity is not allowed")
    approval_id = sanitize_report_text(raw_approval_id).strip()
    approved_by = sanitize_report_text(raw_approved_by).strip()
    if not approval_id or not approved_by:
        raise SplitPlanError("approval_id and approved_by are required")
    if len(approval_id) > 200 or len(approved_by) > 160:
        raise SplitPlanError("approval identity is too long")
    if approval_id != raw_approval_id or approved_by != raw_approved_by:
        raise SplitPlanError("approval identity must not contain private path material")
    try:
        approved_at = datetime.fromisoformat(
            str(approval.approved_at or "").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise SplitPlanError("approved_at must be ISO-8601") from exc
    if approved_at.tzinfo is None or approved_at.utcoffset() is None:
        raise SplitPlanError("approved_at must include a timezone")
    normalized_at = approved_at.isoformat()
    return (
        _sha256_json(
            {
                "approval_id": approval_id,
                "approved_at": normalized_at,
                "approved_by": approved_by,
                "decision": "approved",
                "plan_hash": plan.plan_hash,
            }
        ),
        approved_by,
    )


def _action_id(plan_hash: str, candidate_index: int) -> str:
    return f"fact_split_{_sha256_text(f'{plan_hash}:{candidate_index}')[:40]}"


def _fault(injector: SplitFaultInjector | None, stage: str) -> None:
    if injector is not None:
        injector(stage)


def _source_row(conn: sqlite3.Connection, memory_id: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT id, scope_id, target, content, updated_at, metadata
        FROM memories WHERE id = ?
        """,
        (memory_id,),
    ).fetchone()
    if row is None:
        raise SplitPlanConflictError("source memory no longer exists")
    return row


def _load_replay(
    conn: sqlite3.Connection,
    *,
    plan: SplitPlan,
    source_metadata: Mapping[str, Any],
    approval_hash: str,
) -> SplitApplyResult | None:
    if str(source_metadata.get("split_plan_hash") or "") != plan.plan_hash:
        return None
    if str(source_metadata.get("lifecycle") or "") != "superseded":
        raise SplitPlanConflictError("prior split marker exists without superseded source")
    if str(source_metadata.get("split_approval_hash") or "") != approval_hash:
        raise SplitPlanConflictError("prior split used a different approval")
    raw_pairs = source_metadata.get("split_projection_pairs")
    if not isinstance(raw_pairs, list) or len(raw_pairs) != len(plan.candidate_claims):
        raise SplitPlanConflictError("prior split projection receipt is incomplete")
    pairs: list[Mapping[str, str]] = []
    for index, raw_pair in enumerate(raw_pairs):
        if not isinstance(raw_pair, Mapping):
            raise SplitPlanConflictError("prior split projection receipt is invalid")
        action_id = _action_id(plan.plan_hash, index)
        row = conn.execute(
            """
            SELECT applied, receipt_json FROM fact_action_receipts
            WHERE action_id = ?
            """,
            (action_id,),
        ).fetchone()
        if row is None or int(row[0]) != 1:
            raise SplitPlanConflictError("prior split action receipt is missing")
        try:
            receipt = json.loads(str(row[1] or "{}"))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise SplitPlanConflictError("prior split action receipt is invalid") from exc
        receipt_pairs = receipt.get("projection_pairs") if isinstance(receipt, Mapping) else None
        if not isinstance(receipt_pairs, list) or len(receipt_pairs) != 1:
            raise SplitPlanConflictError("prior split action projection pair is invalid")
        receipt_pair = receipt_pairs[0]
        if not isinstance(receipt_pair, Mapping) or dict(receipt_pair) != dict(raw_pair):
            raise SplitPlanConflictError("prior split projection pair diverged")
        pair = {str(key): str(value) for key, value in raw_pair.items()}
        try:
            expected_pair = assert_canonical_projection_pair(
                conn,
                memory_id=pair.get("memory_id", ""),
                claim_id=pair.get("claim_id", ""),
                scope_id=plan.source_scope_id,
                fact_key=plan.candidate_claims[index].claim.fact_key,
                memory_type=plan.candidate_claims[index].memory_type,
            )
        except FactProjectionInvariantError as exc:
            raise SplitPlanConflictError(
                "prior split projection invariant no longer holds"
            ) from exc
        if pair != expected_pair:
            raise SplitPlanConflictError("prior split projection pair no longer matches")
        pairs.append(pair)
    transition_rows = conn.execute(
        """
        SELECT id FROM governance_audit_events
        WHERE batch_id = ? AND target_id = ?
          AND event_type = 'fact_evolution' AND action = 'supersede_old'
          AND dry_run = 0
        ORDER BY created_at, id
        """,
        (plan.plan_hash, plan.source_memory_id),
    ).fetchall()
    if len(transition_rows) != 1:
        raise SplitPlanConflictError("prior split source transition receipt is missing or ambiguous")
    return SplitApplyResult(
        plan_hash=plan.plan_hash,
        source_memory_id=plan.source_memory_id,
        status="replayed",
        projection_pairs=tuple(pairs),
        source_transition_event_id=str(transition_rows[0][0] or ""),
        replayed=True,
    )


def apply_split_plan(
    conn: sqlite3.Connection,
    *,
    plan: SplitPlan,
    approval: SplitPlanApproval,
    runtime_config: Mapping[str, Any] | None,
    writable_scope_ids: Sequence[str],
    fault_injector: SplitFaultInjector | None = None,
) -> SplitApplyResult:
    """Atomically create independent Claim/Projection pairs then retire source."""

    if not fact_split_shadow_enabled(runtime_config):
        raise SplitPlanError("fact_backfill.shadow_enabled is not enabled")
    _verify_plan_hash(plan)
    approval_hash, approved_by = _validated_approval(plan, approval)
    writable = _validated_scope_ids(
        writable_scope_ids,
        field_name="writable scope",
    )
    if plan.source_scope_id not in writable:
        raise SplitPlanError("source scope is not writable")

    started_outer_transaction = not conn.in_transaction
    if started_outer_transaction:
        conn.execute("BEGIN IMMEDIATE")
    savepoint = f"fact_split_{plan.plan_hash[:16]}"
    conn.execute(f"SAVEPOINT {savepoint}")
    savepoint_active = True
    try:
        source_row = _source_row(conn, plan.source_memory_id)
        source_metadata = load_metadata(source_row["metadata"])
        if str(source_row["scope_id"] or "") != plan.source_scope_id:
            raise SplitPlanConflictError("source scope changed after shadow extraction")
        if str(source_row["target"] or "").strip().lower() != plan.source_target:
            raise SplitPlanConflictError("source target changed after shadow extraction")
        if _sha256_text(str(source_row["content"] or "")) != plan.source_content_hash:
            raise SplitPlanConflictError("source content changed after shadow extraction")
        replay = _load_replay(
            conn,
            plan=plan,
            source_metadata=source_metadata,
            approval_hash=approval_hash,
        )
        if replay is not None:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            savepoint_active = False
            if started_outer_transaction:
                conn.commit()
            return replay

        if str(source_row["updated_at"] or "") != plan.expected_updated_at:
            raise SplitPlanConflictError("source updated_at changed after shadow extraction")
        current_lifecycle = str(source_metadata.get("lifecycle") or "active").strip().lower()
        if current_lifecycle != plan.expected_lifecycle:
            raise SplitPlanConflictError("source lifecycle changed after shadow extraction")
        if conn.execute(
            "SELECT 1 FROM fact_claims WHERE memory_id = ? LIMIT 1",
            (plan.source_memory_id,),
        ).fetchone() is not None:
            raise SplitPlanConflictError("source became Fact-owned after shadow extraction")
        rebuilt_plan = build_split_plan(
            conn,
            source_memory_id=plan.source_memory_id,
            candidate_claims=plan.candidate_claims,
            evidence_spans=plan.evidence_spans,
            projection_texts=plan.projection_texts,
            extractor_policy_version=plan.extractor_policy_version,
            runtime_config=runtime_config,
            readable_scope_ids=tuple(sorted(writable)),
        )
        if rebuilt_plan != plan:
            raise SplitPlanConflictError(
                "SplitPlan no longer matches the canonical read-only builder"
            )
        _fault(fault_injector, "after_source_cas")

        source_content = str(source_row["content"] or "")
        timestamp = now_iso()
        projection_pairs: list[Mapping[str, str]] = []
        for index, candidate in enumerate(plan.candidate_claims):
            evidence = _evidence_for_candidate(
                source_content,
                candidate_index=index,
                evidence_spans=plan.evidence_spans,
                source_memory_id=plan.source_memory_id,
                subject=candidate.claim.subject,
            )
            proposal = EvolutionProposal(
                action=EvolutionAction.ADD,
                raw_action="add",
                claim=candidate.claim,
                evidence_refs=evidence,
                confidence=candidate.confidence,
                reason="explicitly approved historical split",
                source="fact_split_apply",
            )
            policy = evaluate_evolution_policy(proposal)
            if not policy.allowed:
                raise SplitPlanApplyError(
                    "approved split candidate no longer passes evidence policy"
                )
            action_id = _action_id(plan.plan_hash, index)
            result = execute_fact_plan(
                conn,
                EvolutionPlan(
                    proposal=proposal,
                    action_id=action_id,
                    idempotency_key=action_id,
                    policy_mode="reviewed_apply",
                    expected_versions={},
                ),
                policy,
                FactExecutionContext(
                    scope_id=plan.source_scope_id,
                    writable_scope_ids=tuple(sorted(writable)),
                    actor=approved_by,
                    timestamp=timestamp,
                    source="fact_split_apply",
                    target=plan.source_target,
                    memory_content=plan.projection_texts[index],
                    metadata={
                        "memory_type": candidate.memory_type,
                        "split_plan_hash": plan.plan_hash,
                        "split_source_memory_id": plan.source_memory_id,
                        "split_candidate_index": index,
                        "split_approval_hash": approval_hash,
                        "extractor_policy_version": plan.extractor_policy_version,
                    },
                    provenance_refs=(
                        FactProvenanceReference(
                            source_type="split_plan",
                            source_ref=plan.source_memory_id,
                            metadata={
                                "plan_hash": plan.plan_hash,
                                "candidate_index": index,
                            },
                        ),
                    ),
                ),
            )
            if not result.applied:
                raise SplitPlanApplyError("Fact Executor did not apply split candidate")
            raw_pairs = result.receipt.get("projection_pairs")
            if not isinstance(raw_pairs, list) or len(raw_pairs) != 1:
                raise SplitPlanApplyError("Fact Executor returned no canonical projection pair")
            projection_pairs.append(dict(raw_pairs[0]))
            _fault(fault_injector, f"after_candidate_{index}")

        _fault(fault_injector, "before_source_transition")
        transition = transition_memory_lifecycle(
            conn,
            memory_id=plan.source_memory_id,
            lifecycle="superseded",
            metadata_updates={
                "split_plan_hash": plan.plan_hash,
                "split_source_content_hash": plan.source_content_hash,
                "split_projection_pairs": [dict(item) for item in projection_pairs],
                "split_approval_hash": approval_hash,
                "split_extractor_policy_version": plan.extractor_policy_version,
            },
            expected_updated_at=plan.expected_updated_at,
            expected_lifecycle=plan.expected_lifecycle,
            actor=approved_by,
            reason="approved historical fact split completed atomically",
            operation_id=FACT_EVOLUTION_SUPERSEDE,
            batch_id=plan.plan_hash,
            timestamp=timestamp,
            fact_mutation_authority=FACT_EXECUTOR_MUTATION_AUTHORITY,
        )
        transition_event_id = str(transition.get("event_id") or "")
        _fault(fault_injector, "after_source_transition")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        savepoint_active = False
        status = "applied_pending_outer_commit"
        if started_outer_transaction:
            conn.commit()
            status = "applied"
        return SplitApplyResult(
            plan_hash=plan.plan_hash,
            source_memory_id=plan.source_memory_id,
            status=status,
            projection_pairs=tuple(projection_pairs),
            source_transition_event_id=transition_event_id,
        )
    except SplitPlanError:
        if savepoint_active and conn.in_transaction:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if started_outer_transaction:
            conn.rollback()
        raise
    except (
        FactExecutionError,
        FactProjectionInvariantError,
        LifecycleConflictError,
        sqlite3.Error,
    ) as exc:
        if savepoint_active and conn.in_transaction:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if started_outer_transaction:
            conn.rollback()
        raise SplitPlanApplyError(sanitize_report_text(str(exc))[:500]) from exc
    except Exception as exc:
        if savepoint_active and conn.in_transaction:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if started_outer_transaction:
            conn.rollback()
        raise SplitPlanApplyError(sanitize_report_text(str(exc))[:500]) from exc


__all__ = [
    "MAX_SPLIT_CANDIDATES",
    "MAX_SPLIT_EVIDENCE_SPANS",
    "SplitApplyResult",
    "SplitCandidateClaim",
    "SplitEvidenceSpan",
    "SplitPlan",
    "SplitPlanApplyError",
    "SplitPlanApproval",
    "SplitPlanConflictError",
    "SplitPlanError",
    "apply_split_plan",
    "build_split_plan",
    "fact_split_shadow_enabled",
]
