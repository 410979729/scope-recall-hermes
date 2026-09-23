from __future__ import annotations

import json
import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast, get_args

from jsonschema import Draft202012Validator, FormatChecker


Origin = Literal[
    "human_direct", "assistant_visible", "tool_observation", "external_document",
    "host_generated", "memory_reinjection", "imported", "origin_unknown",
]
PrincipalKind = Literal["human", "assistant", "tool", "host", "document", "unknown"]
PrincipalResolution = Literal["verified", "unresolved"]
OriginalOrigin = Literal[
    "human_direct", "assistant_visible", "tool_observation", "external_document",
    "host_generated", "memory_reinjection", "origin_unknown",
]
DisplayOrder = Literal["observed", "unknown"]
StatementKind = Literal[
    "assertion", "decision", "request", "proposal", "hypothetical",
    "quotation", "fictional", "unknown",
]
Basis = Literal["direct_report", "observed", "derived_summary", "inferred_suggestion", "unknown"]
JsonValue = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


class Segment(TypedDict):
    group_key: str
    index: int
    total: int | None
    truncated: bool


class ArtifactVersionPayload(TypedDict):
    artifact_ref: str
    revision: int


class DisplaySnapshotPayload(TypedDict):
    order: DisplayOrder
    items: list[ArtifactVersionPayload]


SOURCE_CONTEXT_MAX_LEN = 64
SOURCE_CONTEXTS_MAX_ITEMS = 8
ENTRY_LABELS_MAX_ITEMS = 16


class SourceContext(TypedDict):
    platform: str
    chat_type: str


class EntryLabel(TypedDict):
    """Which agent a shared store's item came in through, as a reader is shown it."""
    id: str
    name: str


class SourcePrincipal(TypedDict):
    """Per-source principal metadata supplied by a trusted host boundary.

    ``principal_ref`` is an opaque installation-local authority key.  The
    optional ``display_name`` is presentation metadata only and must never be
    used to grant access or to manufacture an evidence quote.
    """

    kind: PrincipalKind
    resolution: PrincipalResolution
    principal_ref: NotRequired[str]
    display_name: NotRequired[str]


class SourceEvent(TypedDict):
    protocol_version: Literal["1.1"]
    source_event_key: str
    source_revision: int
    origin: Origin
    role: Literal["user", "assistant", "tool", "system", "document", "unknown"]
    content: str
    occurred_at: str | None
    recorded_at: str
    time_precision: Literal["instant", "day", "approximate", "unknown"]
    capture_state: Literal["complete", "partial", "gap"]
    evidence_refs: list[str]
    artifact_refs: NotRequired[list[str]]
    source_original_origin: NotRequired[OriginalOrigin]
    dataset_id: NotRequired[str]
    segment: NotRequired[Segment]
    display_snapshot: NotRequired[DisplaySnapshotPayload]
    source_context: NotRequired[SourceContext]
    source_principal: NotRequired[SourcePrincipal]


class RecallRequest(TypedDict):
    protocol_version: Literal["1.1"]
    request_id: str
    query: str
    mode: Literal["auto", "current", "history", "as_of", "method"]
    as_of: NotRequired[str]
    max_items: int
    budget_tokens: int
    focus_refs: NotRequired[list[str]]


class RecallItem(TypedDict):
    ref: str
    revision: int
    kind: Literal["event", "episode", "claim", "procedure", "artifact"]
    content: str
    temporal_status: Literal["current", "historical", "disputed", "unknown"]
    origin: str
    applicability: str
    evidence_refs: list[str]
    expandable: bool
    basis: Basis
    #: Qualification state of a derived claim, and the gate that decided it.
    #: Present only for claims. "proposed" means consolidation extracted it but
    #: qualification did not promote it: usable as a lead, never as settled
    #: fact. Without these the reader cannot tell the two apart.
    claim_state: NotRequired[Literal["proposed", "active", "superseded", "disputed", "retracted"]]
    qualification_reason: NotRequired[str]
    source_contexts: NotRequired[list[SourceContext]]
    #: When the item was said or observed: the source's occurrence time, or for
    #: a claim its newest evidence.  Without it a reader holding two answers to
    #: one question cannot tell which came later.
    occurred_at: NotRequired[str]
    #: In a shared store, the agents the item came in through: one for a source,
    #: every entry behind its evidence for a claim.  Another entry's memory is
    #: that agent's experience, not the reader's.  Absent in a local store.
    entries: NotRequired[list[EntryLabel]]


class RecallPacket(TypedDict):
    protocol_version: Literal["1.1"]
    request_id: str
    status: Literal["ok", "no_match", "partial", "unavailable"]
    memory_epoch: int | None
    items: list[RecallItem]
    gaps: list[str]
    diagnostic_ref: str | None
    answerability: Literal["supported", "partial", "ambiguous", "unknown"]
    coverage: Literal["complete_for_query", "partial", "unknown"]
    unmet_needs: list[str]


class EvidenceSpan(TypedDict):
    source_ref: str
    source_revision: int
    quote: str
    location: NotRequired[str]


class SupportedText(TypedDict):
    text: str
    evidence_refs: list[str]


class Procedure(TypedDict):
    conditions: list[str]
    non_applicable: list[str]
    method: list[str]
    verification_basis: Literal["user_accepted", "observed_once", "inferred_suggestion", "unknown"]
    counterexample_refs: list[str]


class Intention(TypedDict):
    cue: str
    target: str
    conditions: list[str]
    state: Literal["pending", "completed", "cancelled", "expired"]
    state_evidence_refs: list[str]


class Alias(TypedDict):
    name: str
    target_ref: str
    scope_description: str


class ClaimProposal(TypedDict):
    kind: Literal["fact", "preference", "constraint", "decision", "procedure", "intention", "alias"]
    subject: str
    predicate: str
    value_text: str
    conditions: list[str]
    statement_kind: StatementKind
    valid_from: str | None
    valid_to: str | None
    evidence_spans: list[EvidenceSpan]
    procedure: NotRequired[Procedure]
    intention: NotRequired[Intention]
    alias: NotRequired[Alias]


class ResumeState(TypedDict):
    episode_ref: str | None
    goal: SupportedText
    decisions: list[SupportedText]
    verified_progress: list[SupportedText]
    open_items: list[SupportedText]
    blockers: list[SupportedText]
    next_step: str | None
    next_step_basis: Literal["user_requested", "existing_plan", "assistant_suggestion", "unknown"]
    artifact_refs: list[str]
    source_watermark: str
    evidence_refs: list[str]


class ReferenceBinding(TypedDict):
    mention: str
    candidate_refs: list[str]
    resolved_ref: str | None
    resolution: Literal["resolved", "ambiguous", "unresolved"]
    evidence_refs: list[str]


class ConsolidationResult(TypedDict):
    protocol_version: Literal["1.1"]
    source_refs: list[str]
    claim_proposals: list[ClaimProposal]
    resume_proposals: list[ResumeState]
    reference_proposals: list[ReferenceBinding]


class ContractError(ValueError):
    def __init__(self, code: str, field: str = "payload") -> None:
        self.code = code
        self.field = field
        super().__init__(f"{code}: invalid {field}")


_SCHEMAS = frozenset({
    "source_event", "recall_request", "recall_packet", "revise_request",
    "forget_request", "consolidation_result", "task_receipt",
    "profile_request", "entity_request", "profile_view", "entity_view", "trace_request", "trace_view",
})
_MODEL_REQUESTS = frozenset({
    "recall_request", "revise_request", "forget_request",
    "profile_request", "entity_request", "trace_request",
})
#: The instants a model writes into a request, rewritten to UTC before validation.
_MODEL_INSTANTS = {"recall_request": ("as_of",), "revise_request": ("valid_from",)}
_ORIGINS = frozenset(get_args(Origin))
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 32
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+]00:00)\Z")


def bounded_source_context(value: object) -> SourceContext | None:
    """Accept only the exact optional platform/chat_type pair; never guess."""

    if type(value) is not dict or set(value) != {"platform", "chat_type"}:
        return None
    platform = value.get("platform")
    chat_type = value.get("chat_type")
    if type(platform) is not str or type(chat_type) is not str:
        return None
    platform = platform.strip()
    chat_type = chat_type.strip()
    if not 1 <= len(platform) <= SOURCE_CONTEXT_MAX_LEN:
        return None
    if not 1 <= len(chat_type) <= SOURCE_CONTEXT_MAX_LEN:
        return None
    return {"platform": platform, "chat_type": chat_type}


def bounded_source_contexts(value: object) -> list[SourceContext] | None:
    """De-duplicate visible source contexts; omit the field when none are known."""

    if type(value) is not list:
        return None
    collected: list[SourceContext] = []
    for item in value:
        context = bounded_source_context(item)
        if context is None or context in collected:
            continue
        collected.append(context)
        if len(collected) >= SOURCE_CONTEXTS_MAX_ITEMS:
            break
    return collected or None


def bounded_entry_labels(value: object) -> list[EntryLabel] | None:
    """Distinct entries, each exactly ``{id, name}``, ordered by id; omitted when none are known."""

    if type(value) is not list:
        return None
    collected: list[EntryLabel] = []
    for item in value:
        if type(item) is not dict or set(item) != {"id", "name"}:
            continue
        entry_id, name = item["id"], item["name"]
        if type(entry_id) is not str or not ENTRY_ID.fullmatch(entry_id):
            continue
        if type(name) is not str or not name.strip() or len(name) > 32:
            continue
        label: EntryLabel = {"id": entry_id, "name": name}
        if label in collected:
            continue
        collected.append(label)
        if len(collected) >= ENTRY_LABELS_MAX_ITEMS:
            break
    return sorted(collected, key=lambda label: label["id"]) or None


def _utc_time(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if not _TIME.fullmatch(value):
        return False
    try:
        return datetime.fromisoformat(value).utcoffset() == timedelta(0)
    except ValueError:
        return False


#: RFC 3339 date-time with a numeric offset: local time, fraction, sign, hours, minutes.
_NUMERIC_OFFSET_TIME = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?([+-])(\d{2}):(\d{2})", re.ASCII)


def utc_instant(value: object) -> object:
    """The same instant written in UTC, when a model used a non-zero offset.

    The contract accepts only ``Z``/``+00:00``, so a model writing
    ``2026-09-16T10:00:00+08:00`` was refused although it named a correct
    instant; a model reads its memory's times in its host's zone and writes
    them back that way.  Bare dates, naive times, zero offsets and impossible
    dates or offsets are returned unchanged, so the validator still rejects
    them as before.
    """
    match = _NUMERIC_OFFSET_TIME.fullmatch(value) if type(value) is str else None
    if match is None:
        return value
    local, fraction, sign, hours, minutes = match.groups()
    offset = timedelta(hours=int(hours), minutes=int(minutes))
    if not offset or int(hours) > 23 or int(minutes) > 59:
        return value
    try:
        instant = datetime.fromisoformat(local) - (offset if sign == "+" else -offset)
    except (ValueError, OverflowError):
        return value
    return instant.isoformat(timespec="seconds") + (fraction or "") + "Z"


def _json_tree(value: object, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise ContractError("INPUT_INVALID", "nesting")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_tree(item, depth + 1)
        return
    if type(value) is dict and all(type(k) is str for k in value):
        for item in value.values():
            _json_tree(item, depth + 1)
        return
    raise ContractError("INPUT_INVALID", "json_value")


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("INPUT_INVALID", "duplicate_key")
        result[key] = value
    return result


def decode_payload(value: str | bytes | dict) -> dict:
    try:
        if isinstance(value, (str, bytes)):
            raw = value.encode("utf-8") if isinstance(value, str) else value
            if len(raw) > MAX_PAYLOAD_BYTES:
                raise ContractError("INPUT_INVALID", "size")
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        _json_tree(value)
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES or type(value) is not dict:
            raise ContractError("INPUT_INVALID", "size_or_root")
        return json.loads(encoded)
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("INPUT_INVALID") from None


def _profile_view_items(payload: dict) -> list:
    sections = payload.get("sections") or {}
    items = []
    for name in ("facts", "preferences", "constraints", "decisions", "pending_intentions"):
        items.extend(sections.get(name) or [])
    items.extend(payload.get("disputed") or [])
    return items


def _validate_read_view_honesty(payload: dict, *, items: list) -> None:
    if items and payload.get("memory_epoch") is None:
        raise ContractError("INPUT_INVALID", "memory_epoch")
    if payload.get("scan_capped") and payload.get("coverage") == "complete_for_query":
        raise ContractError("INPUT_INVALID", "coverage")
    if payload.get("status") == "no_match" and payload.get("coverage") == "complete_for_query":
        raise ContractError("INPUT_INVALID", "coverage")


def _check_source_event(payload: dict) -> None:
    if (payload["occurred_at"] is None) != (payload["time_precision"] == "unknown"):
        raise ContractError("INPUT_INVALID", "time_precision")
    if payload["origin"] != "imported" and "source_original_origin" in payload:
        raise ContractError("INPUT_INVALID", "source_original_origin")
    if segment := payload.get("segment"):
        if segment["total"] is not None and segment["index"] >= segment["total"]:
            raise ContractError("INPUT_INVALID", "segment_index")
        if (segment["total"] is None or segment["truncated"]) and payload["capture_state"] == "complete":
            raise ContractError("INPUT_INVALID", "segment_completeness")
    if snapshot := payload.get("display_snapshot"):
        if not {item["artifact_ref"] for item in snapshot["items"]} <= set(payload.get("artifact_refs", [])):
            raise ContractError("INPUT_INVALID", "display_artifact_refs")


def _check_recall_packet(payload: dict) -> None:
    if payload["items"] and payload["memory_epoch"] is None:
        raise ContractError("INPUT_INVALID", "memory_epoch")


def _check_forget_request(payload: dict) -> None:
    if not set(payload["expected_revisions"]) <= set(payload["target_refs"]):
        raise ContractError("INPUT_INVALID", "expected_revisions")


def _check_consolidation_result(payload: dict) -> None:
    for claim in payload["claim_proposals"]:
        start, end = claim["valid_from"], claim["valid_to"]
        if start is not None and end is not None and datetime.fromisoformat(end) <= datetime.fromisoformat(start):
            raise ContractError("DERIVATION_INVALID", "valid_interval")
    for binding in payload["reference_proposals"]:
        if binding["resolution"] == "resolved" and binding["resolved_ref"] not in binding["candidate_refs"]:
            raise ContractError("DERIVATION_INVALID", "resolved_ref")
        if binding["resolution"] == "ambiguous" and len(binding["candidate_refs"]) < 2:
            raise ContractError("DERIVATION_INVALID", "candidate_refs")


#: Cross-field rules JSON Schema cannot express, keyed by schema name.
_CROSS_FIELD_CHECKS = {
    "source_event": _check_source_event,
    "recall_packet": _check_recall_packet,
    "profile_view": lambda payload: _validate_read_view_honesty(payload, items=_profile_view_items(payload)),
    "entity_view": lambda payload: _validate_read_view_honesty(payload, items=payload.get("statements") or []),
    "forget_request": _check_forget_request,
    "consolidation_result": _check_consolidation_result,
}


def _offending_field(error) -> str:
    """The property a validation error is about, or the keyword when it is about the whole payload.

    The failing keyword alone does not say what to fix.  A recall asking for a
    day's memory with ``as_of`` written in a local offset came back as
    ``INPUT_INVALID`` / ``format``, and an evaluation missing one property came
    back as ``required``: in both cases the name is already in the error, in its
    path or in the keyword's own value, and was thrown away.  The same field is
    what a failed derivation feeds back to the model on its one repair attempt.
    """
    named = [part for part in error.absolute_path if isinstance(part, str)]
    if named:
        return named[-1]
    if error.validator == "required" and isinstance(error.instance, dict):
        missing = [name for name in (error.validator_value or ()) if name not in error.instance]
        if missing:
            return missing[0]
    return str(error.validator or "schema")


def validate_payload(name: str, value: str | bytes | dict) -> dict:
    if name not in _SCHEMAS:
        raise ContractError("INPUT_INVALID", "schema_name")
    payload = decode_payload(value)
    schema = json.loads((Path(__file__).parent / "contracts" / f"{name}.schema.json").read_text(encoding="utf-8"))
    checker = FormatChecker()
    checker.checks("date-time")(_utc_time)
    error = next(Draft202012Validator(schema, format_checker=checker).iter_errors(payload), None)
    if error is not None:
        raise ContractError("INPUT_INVALID", _offending_field(error))
    check = _CROSS_FIELD_CHECKS.get(name)
    if check is not None:
        check(payload)
    return payload


@dataclass(frozen=True)
class ArtifactVersion:
    artifact_ref: str
    revision: int

    def __post_init__(self) -> None:
        if type(self.artifact_ref) is not str or not self.artifact_ref.strip() or len(self.artifact_ref) > 240:
            raise ContractError("INPUT_INVALID", "artifact_ref")
        if type(self.revision) is not int or self.revision < 1:
            raise ContractError("INPUT_INVALID", "artifact_revision")


@dataclass(frozen=True)
class DisplaySnapshot:
    order: DisplayOrder
    items: tuple[ArtifactVersion, ...]

    def __post_init__(self) -> None:
        if self.order not in get_args(DisplayOrder) or type(self.items) is not tuple or len(self.items) > 32:
            raise ContractError("INPUT_INVALID", "display_snapshot")
        if any(not isinstance(item, ArtifactVersion) for item in self.items):
            raise ContractError("INPUT_INVALID", "display_snapshot")

    def to_payload(self) -> DisplaySnapshotPayload:
        return {"order": self.order, "items": [{"artifact_ref": item.artifact_ref, "revision": item.revision} for item in self.items]}


#: ``local`` is one host's own store, bound to its directory and exact scope set.
#: ``shared`` is one store several entries attach to: its identity is a fixed id
#: that survives a move, and each entry binds a subset of its scopes.
INSTALLATION_KINDS = frozenset({"local", "shared"})
#: An entry names the agent a source came in through.  Operator-assigned, never
#: model-supplied; ``local`` is what every row of a store that predates entries carries.
ENTRY_ID = re.compile(r"[a-z][a-z0-9-]{1,31}")
#: What one local binding can carry.
MAX_BINDING_SCOPES = 128
#: What one shared binding can carry.  A shared store's worker binds every scope
#: the store holds, and each instance brings its own: the three pilot instances
#: have 221 distinct scopes between them, all five 369.  Attaching an entry is
#: refused before the store would pass this.
MAX_SHARED_SCOPES = 1024


@dataclass(frozen=True)
class InstanceBinding:
    agent_id: str
    installation_id: str
    data_directory: Path
    scope_ids: frozenset[str]
    test_mode: bool = False
    installation_kind: str = "local"

    def __post_init__(self) -> None:
        for value in (self.agent_id, self.installation_id):
            if type(value) is not str or not value.strip() or len(value) > 240:
                raise ContractError("IDENTITY_UNBOUND")
        if not isinstance(self.data_directory, Path) or not self.data_directory.is_absolute():
            raise ContractError("IDENTITY_UNBOUND", "data_directory")
        if type(self.installation_kind) is not str or self.installation_kind not in INSTALLATION_KINDS:
            raise ContractError("IDENTITY_UNBOUND", "installation_kind")
        limit = MAX_SHARED_SCOPES if self.installation_kind == "shared" else MAX_BINDING_SCOPES
        if type(self.scope_ids) is not frozenset or not self.scope_ids or len(self.scope_ids) > limit:
            raise ContractError("IDENTITY_UNBOUND", "scope_ids")
        if any(type(s) is not str or not s.strip() or len(s) > 240 for s in self.scope_ids):
            raise ContractError("IDENTITY_UNBOUND", "scope_ids")
        if type(self.test_mode) is not bool:
            raise ContractError("IDENTITY_UNBOUND", "test_mode")


@dataclass(frozen=True)
class ImportProvenance:
    """Runtime attestation from an authorized importer, never a model field.

    The importer verifies the manifest and its source records before constructing
    this value. A role label or source_original_origin in raw input is insufficient.
    """
    original_origin: OriginalOrigin
    manifest_sha256: str
    source_fingerprints: frozenset[str]

    def __post_init__(self) -> None:
        if (self.original_origin not in get_args(OriginalOrigin) or type(self.manifest_sha256) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", self.manifest_sha256) or type(self.source_fingerprints) is not frozenset
            or not 1 <= len(self.source_fingerprints) <= 200 or any(type(s) is not str or not re.fullmatch(r"[0-9a-f]{64}",s) for s in self.source_fingerprints)):
            raise ContractError("IDENTITY_UNBOUND", "import_provenance")


@dataclass(frozen=True)
class TrustedSourcePrincipal:
    """Host-attested source actor, kept separate from model-visible text."""

    kind: PrincipalKind
    resolution: PrincipalResolution
    principal_ref: str | None = None
    display_name: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in get_args(PrincipalKind) or self.resolution not in get_args(PrincipalResolution):
            raise ContractError("IDENTITY_UNBOUND", "source_principal")
        if self.resolution == "verified":
            if type(self.principal_ref) is not str or not self.principal_ref.strip() or len(self.principal_ref) > 240:
                raise ContractError("IDENTITY_UNBOUND", "source_principal")
        elif self.principal_ref is not None:
            raise ContractError("IDENTITY_UNBOUND", "source_principal")
        if self.display_name is not None and (
            type(self.display_name) is not str
            or not self.display_name.strip()
            or len(self.display_name) > 120
        ):
            raise ContractError("IDENTITY_UNBOUND", "source_principal")

    def to_payload(self) -> SourcePrincipal:
        payload: SourcePrincipal = {"kind": self.kind, "resolution": self.resolution}
        if self.principal_ref is not None:
            payload["principal_ref"] = self.principal_ref
        if self.display_name is not None:
            payload["display_name"] = self.display_name
        return payload


def import_source_fingerprint(event: SourceEvent) -> str:
    """Bind attestation to exact source identity, origin, time and content.

    A later transport receipt timestamp is not a new source version.
    """
    value = validate_payload("source_event", cast(dict[str, JsonValue], event))
    body = {k:v for k,v in value.items() if k != "recorded_at"}
    return hashlib.sha256(json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class TrustedContext:
    binding: InstanceBinding
    session_id: str
    allowed_scope_ids: frozenset[str]
    actor_origin: Origin
    project_id: str | None = None
    branch_id: str | None = None
    recent_messages: tuple[str, ...] = ()
    display_snapshot: DisplaySnapshot | None = None
    import_provenance: ImportProvenance | None = None
    task_anchor: str | None = None
    environment_revision: str | None = None
    source_principal: TrustedSourcePrincipal | None = None
    #: The entry this context speaks for, filled by an adapter from its attachment
    #: pointer.  ``None`` outside a shared store, and for the shared worker.
    entry_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.binding, InstanceBinding):
            raise ContractError("IDENTITY_UNBOUND")
        if self.entry_id is not None and (type(self.entry_id) is not str or not ENTRY_ID.fullmatch(self.entry_id)):
            raise ContractError("IDENTITY_UNBOUND", "entry_id")
        if type(self.session_id) is not str or not self.session_id.strip() or len(self.session_id) > 240:
            raise ContractError("IDENTITY_UNBOUND", "session_id")
        if type(self.allowed_scope_ids) is not frozenset or not self.allowed_scope_ids <= self.binding.scope_ids:
            raise ContractError("ACCESS_DENIED")
        if type(self.actor_origin) is not str or self.actor_origin not in _ORIGINS:
            raise ContractError("IDENTITY_UNBOUND", "actor_origin")
        for key in (self.project_id, self.branch_id,self.task_anchor,self.environment_revision):
            if key is not None and (type(key) is not str or not key.strip() or len(key) > 240):
                raise ContractError("IDENTITY_UNBOUND", "context_key")
        if type(self.recent_messages) is not tuple or len(self.recent_messages) > 8 or any(type(x) is not str or len(x) > 8192 for x in self.recent_messages):
            raise ContractError("INPUT_INVALID", "context_budget")
        if self.display_snapshot is not None and not isinstance(self.display_snapshot, DisplaySnapshot):
            raise ContractError("INPUT_INVALID", "display_snapshot")
        if self.import_provenance is not None and (self.actor_origin != "imported" or not isinstance(self.import_provenance, ImportProvenance)):
            raise ContractError("IDENTITY_UNBOUND", "import_provenance")
        if self.source_principal is not None and not isinstance(self.source_principal, TrustedSourcePrincipal):
            raise ContractError("IDENTITY_UNBOUND", "source_principal")


def validate_model_request(name: str, value: str | bytes | dict, context: TrustedContext) -> dict:
    if name not in _MODEL_REQUESTS:
        raise ContractError("ACCESS_DENIED")
    if not isinstance(context, TrustedContext) or not context.allowed_scope_ids:
        raise ContractError("ACCESS_DENIED")
    payload = decode_payload(value)
    for key in _MODEL_INSTANTS.get(name, ()):
        if key in payload:
            payload[key] = utc_instant(payload[key])
    return validate_payload(name, payload)


def validate_capture(value: str | bytes | dict, context: TrustedContext) -> SourceEvent:
    if not isinstance(context, TrustedContext) or not context.allowed_scope_ids:
        raise ContractError("ACCESS_DENIED")
    event = validate_payload("source_event", value)
    if event["origin"] != context.actor_origin:
        raise ContractError("ACCESS_DENIED", "origin")
    trusted_principal = context.source_principal.to_payload() if context.source_principal is not None else None
    supplied_principal = event.get("source_principal")
    if supplied_principal is not None and supplied_principal != trusted_principal:
        raise ContractError("ACCESS_DENIED", "source_principal")
    if trusted_principal is not None:
        event["source_principal"] = trusted_principal
    if "dataset_id" in event and not context.binding.test_mode:
        raise ContractError("ACCESS_DENIED", "dataset_id")
    snapshot = event.get("display_snapshot")
    if snapshot and snapshot["order"] == "observed":
        if context.display_snapshot is None or snapshot != context.display_snapshot.to_payload():
            raise ContractError("ACCESS_DENIED", "display_snapshot")
    return cast(SourceEvent, event)


@dataclass(frozen=True)
class SourceSnapshot:
    ref: str
    revision: int
    content: str
    scope_id: str
    origin: Origin
    read_blocked: bool = False
    verified_original_origin: OriginalOrigin | None = None

    def __post_init__(self) -> None:
        if any(type(value) is not str or not value.strip() or len(value) > 240 for value in (self.ref, self.scope_id)):
            raise ContractError("INPUT_INVALID", "source_identity")
        if type(self.revision) is not int or self.revision < 1 or type(self.read_blocked) is not bool:
            raise ContractError("INPUT_INVALID", "source_revision")
        if type(self.content) is not str or type(self.origin) is not str or self.origin not in _ORIGINS:
            raise ContractError("INPUT_INVALID", "source_snapshot")
        if self.verified_original_origin is not None and (self.origin!='imported' or self.verified_original_origin not in get_args(OriginalOrigin)):
            raise ContractError('INPUT_INVALID','verified_original_origin')


def validate_proposal_references(
    value: str | bytes | dict,
    sources: tuple[SourceSnapshot, ...],
    context: TrustedContext,
) -> ConsolidationResult:
    if not isinstance(context, TrustedContext) or not context.allowed_scope_ids:
        raise ContractError("ACCESS_DENIED")
    result = validate_payload("consolidation_result", value)
    available: dict[str, SourceSnapshot] = {}
    for source in sources:
        if source.scope_id in context.allowed_scope_ids and not source.read_blocked:
            key = f"{source.ref}@{source.revision}"
            if key in available:
                raise ContractError("DERIVATION_INVALID", "duplicate_source")
            available[key] = source
    declared = set(result["source_refs"])
    if not declared <= available.keys():
        raise ContractError("SOURCE_MISSING")
    for claim in result["claim_proposals"]:
        for span in claim["evidence_spans"]:
            key = f"{span['source_ref']}@{span['source_revision']}"
            # Two findings once shared one name, so every rejection read
            # ``evidence_span`` whether the model had cited a source it never
            # declared or quoted one it had.  Only the second is about the words
            # it wrote, and only the second is what the quote ladder can repair.
            if key not in declared:
                raise ContractError("DERIVATION_INVALID", "evidence_undeclared_source")
            if span["quote"] not in available[key].content:
                raise ContractError("DERIVATION_INVALID", "evidence_quote")
        for ref in claim.get("procedure", {}).get("counterexample_refs", []):
            if ref not in declared:
                raise ContractError("SOURCE_MISSING")
        for ref in claim.get("intention", {}).get("state_evidence_refs", []):
            if ref not in declared:
                raise ContractError("SOURCE_MISSING")
    for resume in result["resume_proposals"]:
        refs = list(resume["evidence_refs"]) + resume["goal"]["evidence_refs"]
        for field in ("decisions", "verified_progress", "open_items", "blockers"):
            for item in resume[field]:
                refs.extend(item["evidence_refs"])
        if not set(refs) <= declared:
            raise ContractError("SOURCE_MISSING")
        for item in resume["verified_progress"]:
            if not any((available[ref].verified_original_origin or available[ref].origin) in {"human_direct", "tool_observation"} for ref in item["evidence_refs"]):
                raise ContractError("DERIVATION_INVALID", "verified_progress_origin")
    for binding in result["reference_proposals"]:
        if not set(binding["evidence_refs"]) <= declared:
            raise ContractError("SOURCE_MISSING")
    return cast(ConsolidationResult, result)
