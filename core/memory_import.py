"""Import normalized local evidence through capture without granting owner authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Literal, get_args

from ..contracts import (
    ContractError,
    ImportProvenance,
    OriginalOrigin,
    SourceEvent,
    TrustedSourcePrincipal,
    import_source_fingerprint,
)
from .capture import record_event
from .events import prepare_capture


def digest(value) -> str:
    """Hash a canonical identity without persisting its plaintext components."""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ImportRecord:
    """One original occurrence supplied by a local reader; absent times remain unknown.

    Project labels group imports but confer no filesystem authorization. Readers
    may attest human origin only for explicit user messages in conversation exports.
    """

    key: str
    content: str
    role: Literal["user", "assistant", "tool", "system", "document", "unknown"]
    occurred_at: str | None = None
    project: str | None = None
    session: str | None = None
    original_origin: OriginalOrigin = "origin_unknown"
    private_content_omitted: bool = False

    def __post_init__(self) -> None:
        if type(self.key) is not str or not self.key.strip() or type(self.content) is not str:
            raise ContractError("INPUT_INVALID", "import_record")
        if type(self.private_content_omitted) is not bool:
            raise ContractError("INPUT_INVALID", "import_private_marker")
        if self.role not in {"user", "assistant", "tool", "system", "document", "unknown"}:
            raise ContractError("INPUT_INVALID", "import_role")
        if self.original_origin not in get_args(OriginalOrigin) or (
            self.original_origin == "human_direct" and self.role != "user"
        ):
            raise ContractError("INPUT_INVALID", "import_origin")
        if any(
            value is not None and (type(value) is not str or not value.strip())
            for value in (self.project, self.session, self.occurred_at)
        ):
            raise ContractError("INPUT_INVALID", "import_metadata")


def _actor(context, item, source, source_id, scope_id):
    if scope_id not in context.allowed_scope_ids:
        raise ContractError("ACCESS_DENIED", "import_scope")
    if not source_id.strip() or not item.key.strip():
        raise ContractError("INPUT_INVALID", "import_identity")
    kind: Literal["human", "assistant", "unknown"] = (
        "human"
        if item.original_origin == "human_direct"
        else "assistant"
        if item.original_origin == "assistant_visible"
        else "unknown"
    )
    # An export's human is kept in its account namespace, never equated with
    # the local owner's principal merely because they approved an import.
    principal = TrustedSourcePrincipal(kind, "verified", "principal:import:" + digest([source, source_id, kind]))
    return replace(
        context,
        actor_origin="imported",
        import_provenance=None,
        allowed_scope_ids=frozenset({scope_id}),
        project_id=context.project_id or item.project,
        session_id="import:" + digest([source, source_id, item.session or "memories"]),
        source_principal=principal,
    )


def _versioned_capture(core, context, prepared):
    """Reuse an admitted historical envelope or allocate the next source version.

    Comparing admitted text makes a sanitized re-export idempotent. Reusing
    the stored envelope preserves its original occurrence, masking and gaps.
    Capture still rechecks privacy, deletion and concurrent version conflicts.
    """
    key = prepared.events[0]["source_event_key"]
    group = prepared.events[0].get("segment", {}).get("group_key", key)
    digests = tuple(hashlib.sha256(e["content"].encode()).hexdigest() for e in prepared.events)
    with core.storage.read(context) as tx:
        rows = (
            tx._check()
            .execute(
                "SELECT event_id,source_revision,content_sha256,import_provenance_sha256 FROM source_events "
                "WHERE source_group_key=? ORDER BY source_revision,segment_index",
                (group,),
            )
            .fetchall()
        )
        versions = {}
        for row in rows:
            versions.setdefault(row["source_revision"], []).append(row)
        for revision, parts in versions.items():
            if tuple(row["content_sha256"] for row in parts) != digests:
                continue
            saved = tuple(source for row in parts if (source := tx.source(row["event_id"], revision)) is not None)
            if len(saved) != len(parts) or not all(
                source is not None
                and all(
                    source.event.get(field) == prepared.events[index].get(field)
                    for field in ("role", "source_original_origin", "source_principal", "dataset_id")
                )
                for index, source in enumerate(saved)
            ):
                continue
            return replace(prepared, events=tuple(source.event for source in saved), gaps=saved[0].capture_gaps), parts[
                0
            ]["import_provenance_sha256"]
    revision = max(versions, default=0) + 1
    return replace(prepared, events=tuple({**event, "source_revision": revision} for event in prepared.events)), None


def import_record(core, context, item: ImportRecord, *, source: str, source_id: str, scope_id: str):
    """Persist one normalized occurrence through the shared, bounded capture boundary.

    Reader attestation covers the admitted segments. It stays outside ordinary
    host retry queues. Every call releases its own writer lease; derived work
    uses the existing admission queues and worker budgets without model calls.
    """
    actor = _actor(context, item, source, source_id, scope_id)
    key = "memory-import:" + digest(
        [scope_id, actor.project_id, actor.branch_id, source, source_id, item.project, item.session, item.key]
    )
    event: SourceEvent = {
        "protocol_version": "1.1",
        "source_event_key": key,
        "source_revision": 1,
        "origin": "imported",
        "role": item.role,
        "content": item.content,
        "occurred_at": item.occurred_at,
        "recorded_at": core.clock.utc_now(),
        "time_precision": "instant" if item.occurred_at else "unknown",
        "capture_state": "complete",
        "evidence_refs": [],
        "source_original_origin": item.original_origin,
        "source_principal": actor.source_principal.to_payload(),
        "dataset_id": f"memory-import:{source}:{digest(source_id)[:20]}",
    }
    prepared = prepare_capture(event, actor, private_content_omitted=item.private_content_omitted)
    if len(prepared.events) > 200:
        raise ContractError("INPUT_INVALID", "import_record_budget")
    if not prepared.rejection:
        prepared, previous_manifest = _versioned_capture(core, actor, prepared)
        fingerprints = frozenset(import_source_fingerprint(e) for e in prepared.events)
        provenance = ImportProvenance(
            item.original_origin, previous_manifest or digest([key, sorted(fingerprints)]), fingerprints
        )
        actor = replace(actor, import_provenance=provenance)
    return record_event(
        core.storage,
        core.clock,
        actor,
        event,
        scope_id=scope_id,
        admission_policy=core.config.admission_policy,
        _prepared=prepared,
    )
