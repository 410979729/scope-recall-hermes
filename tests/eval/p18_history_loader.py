"""Public, offline P18 raw-history import preparation for the existing Core.

This module deliberately stops at source capture.  It does not extract claims,
preseed answers, call a model, or stand in for a host adapter.  Only the C arm
has a supported path because it is the existing Core import path; A/B/D return
an explicit unsupported result.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from scope_recall.contracts import ImportProvenance, SourceEvent, TrustedContext, import_source_fingerprint, validate_payload
from scope_recall.core.composition import MemoryCore


SCHEMA = "scope-recall.p18.synthetic-raw-history.v1"
SUPPORTED_ARM = "C"
_ORIGINAL_ORIGINS = frozenset({
    "human_direct",
    "assistant_visible",
    "tool_observation",
    "external_document",
    "host_generated",
    "memory_reinjection",
    "origin_unknown",
})
_ROLES = frozenset({"user", "assistant", "tool", "system", "document", "unknown"})
_CONTROL_FIELDS = frozenset({
    "case_id",
    "case_index",
    "case_index_control_only",
    "group_id",
    "core_class",
    "condition",
    "answerability",
    "required_facts",
    "prohibited_errors",
    "gold",
    "expected",
    "oracle",
    "control_only",
})
_MANIFEST_FIELDS = frozenset({
    "schema",
    "dataset_id",
    "persona_id",
    "records",
    "source_fingerprints",
    "manifest_sha256",
})
_RECORD_FIELDS = frozenset({
    "order",
    "source_event_key",
    "source_revision",
    "role",
    "source_original_origin",
    "text",
    "occurred_at",
    "recorded_at",
    "time_precision",
    "evidence_refs",
})

# This is deliberately small, public, and synthetic.  It is not a holdout or
# answer fixture and contains no expected claims or model outputs.
PUBLIC_SYNTHETIC_HISTORY: tuple[dict[str, Any], ...] = (
    {
        "order": 1,
        "source_event_key": "P18-public-persona/001",
        "source_revision": 1,
        "role": "user",
        "source_original_origin": "human_direct",
        "text": "TEST synthetic persona prefers the blue cover.",
        "occurred_at": "2026-09-06T12:00:00Z",
        "recorded_at": "2026-09-06T12:00:01Z",
        "time_precision": "instant",
        "evidence_refs": [],
    },
    {
        "order": 2,
        "source_event_key": "P18-public-persona/002",
        "source_revision": 1,
        "role": "assistant",
        "source_original_origin": "assistant_visible",
        "text": "TEST synthetic persona history was captured for Core import only.",
        "occurred_at": "2026-09-06T12:00:02Z",
        "recorded_at": "2026-09-06T12:00:03Z",
        "time_precision": "instant",
        "evidence_refs": [],
    },
)


class HistoryLoaderError(ValueError):
    """Input or trust-boundary rejection for the public loader."""


@dataclass(frozen=True)
class HistoryEventDTO:
    """A validated SourceEvent plus its explicit source order."""

    order: int
    event: SourceEvent
    fingerprint: str


@dataclass(frozen=True)
class ValidatedHistory:
    dataset_id: str
    persona_id: str
    manifest_sha256: str
    events: tuple[HistoryEventDTO, ...]


@dataclass(frozen=True)
class HistoryLoadResult:
    status: str
    arm_id: str
    reason: str | None
    records_seen: int
    inserted: int
    duplicates: int
    source_refs: tuple[str, ...]
    queued_work_items: int


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _manifest_body(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in manifest.items() if key not in {"manifest_sha256", "source_fingerprints"}}


def _reject_control_fields(value: object, path: str = "manifest") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in _CONTROL_FIELDS:
                raise HistoryLoaderError(f"control/oracle field is not allowed: {path}.{key}")
            _reject_control_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_control_fields(child, f"{path}[{index}]")


def _require_text(value: object, field: str, *, max_length: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise HistoryLoaderError(f"invalid {field}")
    return value


def _validate_raw_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(raw) - _RECORD_FIELDS
    if unknown:
        raise HistoryLoaderError(f"unsupported raw-history fields: {sorted(unknown)}")
    if set(raw) != _RECORD_FIELDS:
        raise HistoryLoaderError("raw-history record fields are incomplete")
    order = raw.get("order")
    if type(order) is not int or order < 1:
        raise HistoryLoaderError("invalid source order")
    _require_text(raw.get("source_event_key"), "source_event_key")
    revision = raw.get("source_revision")
    if type(revision) is not int or revision < 1:
        raise HistoryLoaderError("invalid source_revision")
    role = raw.get("role")
    if role not in _ROLES:
        raise HistoryLoaderError("source role is required and must be explicit")
    original = raw.get("source_original_origin")
    if original not in _ORIGINAL_ORIGINS:
        raise HistoryLoaderError("source_original_origin is required; unknown is not human")
    text = raw.get("text")
    if not isinstance(text, str) or len(text) > 65536:
        raise HistoryLoaderError("invalid source text")
    occurred_at = raw.get("occurred_at")
    if occurred_at is not None and not isinstance(occurred_at, str):
        raise HistoryLoaderError("invalid occurred_at")
    _require_text(raw.get("recorded_at"), "recorded_at", max_length=80)
    precision = raw.get("time_precision")
    if precision not in {"instant", "day", "approximate", "unknown"}:
        raise HistoryLoaderError("invalid time_precision")
    refs = raw.get("evidence_refs")
    if type(refs) is not list or any(not isinstance(item, str) or not item.strip() for item in refs):
        raise HistoryLoaderError("invalid evidence_refs")
    return dict(raw)


def _event_from_validated_raw(dataset_id: str, raw: Mapping[str, Any]) -> HistoryEventDTO:
    event: SourceEvent = {
        "protocol_version": "1.1",
        "source_event_key": raw["source_event_key"],
        "source_revision": raw["source_revision"],
        "origin": "imported",
        "role": raw["role"],
        "content": raw["text"],
        "occurred_at": raw["occurred_at"],
        "recorded_at": raw["recorded_at"],
        "time_precision": raw["time_precision"],
        "capture_state": "complete",
        "evidence_refs": list(dict.fromkeys(raw["evidence_refs"])),
        "source_original_origin": raw["source_original_origin"],
        "dataset_id": dataset_id,
    }
    try:
        validate_payload("source_event", event)
    except Exception as exc:
        raise HistoryLoaderError(f"invalid source event: {exc}") from exc
    return HistoryEventDTO(order=raw["order"], event=event, fingerprint=import_source_fingerprint(event))


def build_public_manifest(records: Sequence[Mapping[str, Any]] = PUBLIC_SYNTHETIC_HISTORY) -> dict[str, Any]:
    """Build a hash-bound public manifest without claims or expected answers."""

    raw_records = [_validate_raw_record(dict(record)) for record in records]
    base: dict[str, Any] = {
        "schema": SCHEMA,
        "dataset_id": "P18-SYNTHETIC-PUBLIC",
        "persona_id": "TEST-independent-synthetic-persona",
        "records": raw_records,
    }
    events = tuple(_event_from_validated_raw(base["dataset_id"], raw) for raw in raw_records)
    base["source_fingerprints"] = sorted(item.fingerprint for item in events)
    base["manifest_sha256"] = hashlib.sha256(_canonical(_manifest_body(base))).hexdigest()
    return base


def validate_history_manifest(manifest: Mapping[str, Any]) -> ValidatedHistory:
    """Verify public manifest identity, hash, source fingerprints, and fields."""

    if not isinstance(manifest, Mapping):
        raise HistoryLoaderError("manifest must be an object")
    _reject_control_fields(manifest)
    unknown = set(manifest) - _MANIFEST_FIELDS
    if unknown or set(manifest) != _MANIFEST_FIELDS:
        raise HistoryLoaderError("manifest fields are incomplete or unsupported")
    if manifest.get("schema") != SCHEMA:
        raise HistoryLoaderError("unsupported history manifest schema")
    dataset_id = _require_text(manifest.get("dataset_id"), "dataset_id", max_length=100)
    if not dataset_id.startswith("P18-SYNTHETIC-"):
        raise HistoryLoaderError("only public synthetic datasets are supported")
    persona_id = _require_text(manifest.get("persona_id"), "persona_id", max_length=240)
    if not persona_id.startswith("TEST-"):
        raise HistoryLoaderError("history persona must be an independent TEST persona")
    records = manifest.get("records")
    if type(records) is not list or not records or len(records) > 64:
        raise HistoryLoaderError("public history records must be a non-empty bounded list")
    validated_records = tuple(_validate_raw_record(record) for record in records if isinstance(record, Mapping))
    events = tuple(_event_from_validated_raw(dataset_id, record) for record in validated_records)
    if len(events) != len(records):
        raise HistoryLoaderError("each history record must be an object")
    orders = tuple(item.order for item in events)
    if orders != tuple(range(1, len(events) + 1)):
        raise HistoryLoaderError("source order must be contiguous and deterministic")
    identities = [(item.event["source_event_key"], item.event["source_revision"]) for item in events]
    if len(set(identities)) != len(identities):
        raise HistoryLoaderError("duplicate source identity")
    fingerprints = manifest.get("source_fingerprints")
    if type(fingerprints) is not list or fingerprints != sorted(fingerprints) or fingerprints != sorted(item.fingerprint for item in events):
        raise HistoryLoaderError("source fingerprint list mismatch")
    manifest_sha = manifest.get("manifest_sha256")
    if not isinstance(manifest_sha, str) or hashlib.sha256(_canonical(_manifest_body(manifest))).hexdigest() != manifest_sha:
        raise HistoryLoaderError("manifest hash mismatch")
    return ValidatedHistory(dataset_id, persona_id, manifest_sha, events)


def history_event_dtos(manifest: Mapping[str, Any]) -> tuple[HistoryEventDTO, ...]:
    """Return validated imported SourceEvent DTOs in original source order."""

    return validate_history_manifest(manifest).events


def load_raw_history(core: MemoryCore, context: TrustedContext, manifest: Mapping[str, Any], *, scope_id: str, arm_id: str) -> HistoryLoadResult:
    """Capture one verified synthetic history through the normal Core C path."""

    if arm_id not in {"A", "B", "C", "D"}:
        raise HistoryLoaderError("arm_id must be one of A/B/C/D")
    if arm_id != SUPPORTED_ARM:
        return HistoryLoadResult("UNSUPPORTED", arm_id, "raw_history_core_import_only", 0, 0, 0, (), 0)
    if not isinstance(core, MemoryCore):
        raise HistoryLoaderError("only the existing MemoryCore import path is supported")
    if not context.binding.test_mode:
        raise HistoryLoaderError("synthetic history import requires an isolated test binding")
    if scope_id not in context.allowed_scope_ids:
        raise HistoryLoaderError("scope is not authorized by the trusted context")
    validated = validate_history_manifest(manifest)
    fingerprints = frozenset(item.fingerprint for item in validated.events)
    refs: list[str] = []
    inserted = duplicates = 0
    for item in validated.events:
        imported_context = TrustedContext(
            context.binding,
            context.session_id,
            context.allowed_scope_ids,
            "imported",
            project_id=context.project_id,
            branch_id=context.branch_id,
            recent_messages=context.recent_messages,
            task_anchor=context.task_anchor,
            environment_revision=context.environment_revision,
            import_provenance=ImportProvenance(item.event["source_original_origin"], validated.manifest_sha256, fingerprints),
        )
        receipt = core.record_event(imported_context, item.event, scope_id=scope_id, remaining_seconds=5)
        if receipt.disposition not in {"inserted", "duplicate"} or not receipt.event_refs:
            raise HistoryLoaderError(f"raw history capture failed: {receipt.disposition}")
        refs.append(receipt.event_refs[0].ref)
        inserted += int(receipt.disposition == "inserted")
        duplicates += int(receipt.disposition == "duplicate")
    with core.storage.read(context) as tx:
        conn = tx._check()
        marks = ",".join("?" for _ in refs)
        queued = int(conn.execute(f"SELECT count(*) FROM work_items WHERE subject_ref IN ({marks})", tuple(refs)).fetchone()[0]) if refs else 0
    return HistoryLoadResult("PASS", arm_id, None, len(validated.events), inserted, duplicates, tuple(refs), queued)


__all__ = [
    "HistoryEventDTO",
    "HistoryLoadResult",
    "HistoryLoaderError",
    "PUBLIC_SYNTHETIC_HISTORY",
    "ValidatedHistory",
    "build_public_manifest",
    "history_event_dtos",
    "load_raw_history",
    "validate_history_manifest",
]
